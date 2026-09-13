from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch
from pathlib import Path

import numpy as np
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import QApplication

from ui.spectrum_panel import SpectrumPanel
from ui.instrument_panel import InstrumentPanel, _record_spectrum_readback
from utils.config import cfg


class _Controller(QObject):
    connected = Signal(list)
    disconnected = Signal()
    spectrum_ready = Signal(object, object)
    frame_ready = Signal(object)
    settings_applied = Signal()
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self.identity = {"model": "fake-lf6", "serial_number": "LF6-1"}
        self.apply_calls = []
        self.acquire_calls = 0
        self.paused = set()

    def apply_settings(self, **values):
        self.apply_calls.append(values)

    def acquire_single(self):
        self.acquire_calls += 1

    def acquire_2d(self):
        pass

    def abort_acquisition(self):
        return True

    def set_temperature_monitor_paused(self, source, paused):
        if paused:
            self.paused.add(source)
        else:
            self.paused.discard(source)


class SpectrumReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_add_spectrum_acquires_then_auto_saves_reference_and_metadata(self):
        ctrl = _Controller()
        panel = SpectrumPanel(ctrl)
        ctrl.connected.emit([])
        with tempfile.TemporaryDirectory() as directory:
            old_root, old_sample = cfg.filename.base_out, cfg.session.sample_id
            cfg.filename.base_out = directory
            cfg.session.sample_id = "sample A"
            try:
                panel._add_spectrum_btn.click()
                self.assertEqual(len(ctrl.apply_calls), 1)
                self.assertEqual(ctrl.acquire_calls, 0)
                ctrl.settings_applied.emit()
                self.assertEqual(ctrl.acquire_calls, 1)
                ctrl.spectrum_ready.emit(np.array([700., 701.]), np.array([3., 4.]))
                self.assertEqual(len(panel.references), 1)
                ref = panel.references[0]
                self.assertTrue(ref["saved"])
                output = Path(ref["path"])
                self.assertEqual(output.parent.name, date.today().isoformat())
                self.assertEqual(output.parent.parent.name, "Spectrum")
                self.assertEqual(output.parent.parent.parent.name, "sample A")
                self.assertTrue(output.exists())
                metadata = json.loads(Path(ref["metadata_path"]).read_text(encoding="utf-8"))
                requested = metadata["settings"]["requested"]
                self.assertIn("acquisition_time_snapshot", requested)
                self.assertIn("requested", requested["acquisition_time_snapshot"])
                self.assertIn("applied", requested["acquisition_time_snapshot"])
            finally:
                cfg.filename.base_out, cfg.session.sample_id = old_root, old_sample

    def test_add_during_run_freezes_last_completed_frame_without_new_acquisition(self):
        ctrl = _Controller()
        panel = SpectrumPanel(ctrl)
        ctrl.connected.emit([])
        panel._run_1d_btn.click()
        ctrl.settings_applied.emit()
        ctrl.spectrum_ready.emit(np.array([1., 2.]), np.array([8., 9.]))
        calls = ctrl.acquire_calls
        with tempfile.TemporaryDirectory() as directory:
            old_root = cfg.filename.base_out
            cfg.filename.base_out = directory
            try:
                panel._add_spectrum_btn.click()
                self.assertEqual(ctrl.acquire_calls, calls)
                np.testing.assert_array_equal(panel.references[-1]["counts"], [8., 9.])
            finally:
                cfg.filename.base_out = old_root
        panel._stop_btn.click()

    def test_load_spectra_accepts_multiple_grids_and_preserves_original_counts(self):
        ctrl = _Controller()
        panel = SpectrumPanel(ctrl)
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "alpha.csv"
            second = Path(directory) / "beta.csv"
            first.write_text("wavelength_nm,intensity_counts\n700,1\n701,2\n", encoding="utf-8")
            second.write_text("wavelength_nm,intensity_counts\n700,10\n700.5,20\n701,30\n", encoding="utf-8")
            panel.load_spectra([first, second])
        self.assertEqual([r["name"] for r in panel.references], ["alpha", "beta"])
        np.testing.assert_array_equal(panel.references[0]["counts"], [1., 2.])
        np.testing.assert_array_equal(panel.references[1]["wavelength"], [700., 700.5, 701.])
        self.assertEqual(panel.references[0]["settings_snapshot"], {})

    def test_auto_save_uses_unique_sequence_and_retry_preserves_unsaved_record(self):
        ctrl = _Controller()
        panel = SpectrumPanel(ctrl)
        with tempfile.TemporaryDirectory() as directory:
            old_root = cfg.filename.base_out
            cfg.filename.base_out = directory
            try:
                first = panel.add_reference([700., 701.], [1., 2.], auto_save=True)
                second = panel.add_reference([700., 701.], [3., 4.], auto_save=True)
                self.assertNotEqual(first["path"], second["path"])
                self.assertTrue(first["saved"] and second["saved"])
                # A transient save error leaves the reference available for retry.
                original = panel._save_spectrum_data
                panel._save_spectrum_data = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full"))
                failed = panel.add_reference([700., 701.], [5., 6.], auto_save=True)
                self.assertFalse(failed["saved"])
                panel._save_spectrum_data = original
                self.assertTrue(panel.retry_reference_save(failed["id"]))
                self.assertTrue(failed["saved"])
            finally:
                cfg.filename.base_out = old_root

    def test_invalid_csv_has_clear_error_and_name_color_visibility_are_editable(self):
        ctrl = _Controller()
        panel = SpectrumPanel(ctrl)
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / "invalid.csv"
            invalid.write_text("header\nno numeric data\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "wavelength and intensity"):
                panel.load_spectra([invalid])
        ref = panel.add_reference([700., 701.], [1., 2.], auto_save=False)
        self.assertTrue(panel.rename_reference(ref["id"], "Dark reference"))
        self.assertTrue(panel.set_reference_color(ref["id"], "#00aa11"))
        item = panel._reference_list.item(0)
        ref["_row_checkbox"].setChecked(False)
        self.assertFalse(panel.references[0]["visible"])

    def test_actual_readback_is_timestamped_and_distinct_from_requested_settings(self):
        ctrl = _Controller()
        ctrl.readback = {"center_nm": 701.25, "exposure_ms": 10.5, "accumulations": 2}
        panel = SpectrumPanel(ctrl)
        with tempfile.TemporaryDirectory() as directory:
            old_root = cfg.filename.base_out
            cfg.filename.base_out = directory
            try:
                ctrl.connected.emit([])
                panel._center.setValue(700.0)
                panel._exposure.setValue(10.0)
                panel._accumulations.setValue(1)
                panel._add_spectrum_btn.click()
                ctrl.settings_applied.emit()
                ctrl.spectrum_ready.emit(np.array([700., 701.]), np.array([1., 2.]))
                metadata = json.loads(Path(panel.references[0]["metadata_path"]).read_text(encoding="utf-8"))
                settings = metadata["settings"]["requested"]
                self.assertEqual(settings["acquisition_time_snapshot"]["requested"]["center_nm"], 700.0)
                self.assertEqual(settings["observed"]["values"]["center_nm"], 701.25)
                self.assertIn("timestamp_utc", settings["observed"])
            finally:
                cfg.filename.base_out = old_root

    def test_row_visibility_and_batch_visibility_preserve_reference_identity(self):
        ctrl = _Controller()
        panel = SpectrumPanel(ctrl)
        ref = panel.add_reference([700., 701.], [1., 2.], name="Keep me", color="#123456", auto_save=False)
        item = panel._reference_list.item(0)
        row_checkbox = ref["_row_checkbox"]
        row_checkbox.setChecked(False)
        self.assertFalse(ref["visible"])
        self.assertEqual(ref["name"], "Keep me")
        self.assertEqual(ref["color"], "#123456")
        self.assertIsNone(ref["path"])
        panel._show_all_btn.click()
        self.assertTrue(ref["visible"])
        panel._hide_all_btn.click()
        self.assertFalse(ref["visible"])
        self.assertEqual(item.data(Qt.ItemDataRole.UserRole), ref["id"])

    def test_sidebar_provider_is_frozen_at_frame_and_kept_through_save_retry(self):
        ctrl = _Controller()
        panel = SpectrumPanel(ctrl)
        state = {"instruments": {"smu": {"connected": True, "readbacks": {"Vbg_meas": 1.2}}}}
        panel.set_sidebar_snapshot_provider(lambda: state.copy())
        ctrl.connected.emit([])
        with tempfile.TemporaryDirectory() as directory:
            old_root = cfg.filename.base_out
            cfg.filename.base_out = directory
            try:
                original_save = panel._save_spectrum_data
                panel._save_spectrum_data = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full"))
                panel._add_spectrum_btn.click()
                ctrl.settings_applied.emit()
                ctrl.spectrum_ready.emit(np.array([700., 701.]), np.array([1., 2.]))
                # The later sidebar change must not alter the frame's metadata.
                state["instruments"]["smu"]["readbacks"]["Vbg_meas"] = 9.9
                ref = panel.references[0]
                self.assertFalse(ref["saved"])
                panel._save_spectrum_data = original_save
                self.assertTrue(panel.retry_reference_save(ref["id"]))
                metadata = json.loads(Path(ref["metadata_path"]).read_text(encoding="utf-8"))
                frozen = metadata["context"]["sidebar_readbacks"]
                self.assertEqual(frozen["instruments"]["smu"]["readbacks"]["Vbg_meas"], 1.2)
            finally:
                cfg.filename.base_out = old_root

    def test_instrument_sidebar_snapshot_contains_cached_value_timestamp_and_connection(self):
        section = SimpleNamespace(
            _ctrl=SimpleNamespace(is_connected=True),
            _spectrum_readbacks={},
        )
        _record_spectrum_readback(section, "power_meter", {"power_w": 2.5e-6})
        sidebar = InstrumentPanel.__new__(InstrumentPanel)
        sidebar._sections = {"pm100d": section}
        snapshot = sidebar.capture_spectrum_readbacks()
        reading = snapshot["instruments"]["pm100d"]["readbacks"]["power_meter"]
        self.assertTrue(snapshot["instruments"]["pm100d"]["connected"])
        self.assertEqual(reading["value"]["power_w"], 2.5e-6)
        self.assertIn("timestamp_utc", reading)

    def test_save_dialog_freezes_data_when_a_new_frame_arrives_during_dialog(self):
        ctrl = _Controller()
        panel = SpectrumPanel(ctrl)
        panel.push_spectrum([700., 701.], [1., 2.], {"center_nm": 700.})
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "saved.csv"

            def dialog_side_effect(*_args, **_kwargs):
                panel.push_spectrum([800., 801.], [9., 10.], {"center_nm": 800.})
                return str(output), "CSV (*.csv)"

            with patch("ui.spectrum_panel.QFileDialog.getSaveFileName", side_effect=dialog_side_effect):
                panel._save_current_dialog()
            rows = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(rows[1].split(","), ["700.0", "1.0"])

    def test_normalize_changes_live_and_reference_display_but_preserves_raw_counts(self):
        ctrl = _Controller()
        panel = SpectrumPanel(ctrl)
        ref = panel.add_reference([700., 701.], [2., 4.], auto_save=False)
        panel.push_spectrum([700., 701.], [10., 20.], {"center_nm": 700.})
        panel._normalize_references_chk.setChecked(True)
        np.testing.assert_array_equal(ref["counts"], [2., 4.])
        np.testing.assert_allclose(panel._display_live_counts(), [0.5, 1.0])
        np.testing.assert_allclose(panel._display_counts(ref), [0.5, 1.0])

    def test_live_curve_pen_stays_fixed_blue_thick_and_solid_through_reference_changes(self):
        ctrl = _Controller()
        panel = SpectrumPanel(ctrl)
        live_curve = panel._spec_plot._curve
        live_pen = live_curve.opts["pen"]
        live_color = live_pen.color().name().upper()

        self.assertEqual(live_color, "#1565C0")
        self.assertGreater(live_pen.widthF(), 1.8)
        self.assertEqual(live_pen.style(), Qt.PenStyle.SolidLine)
        self.assertGreater(live_curve.zValue(), 0)
        self.assertNotIn(live_color, {color.upper() for color in panel._reference_colors})

        panel.push_spectrum([700., 701.], [1., 2.])
        ctrl.connected.emit([])
        panel._run_1d_btn.click()
        ctrl.settings_applied.emit()
        ctrl.spectrum_ready.emit(np.array([700., 701.]), np.array([3., 4.]))
        self.assertEqual(live_curve.opts["pen"].color().name().upper(), live_color)

        added = panel.add_reference([700., 701.], [5., 6.], auto_save=False)
        self.assertNotEqual(added["color"].upper(), live_color)
        panel._refresh_reference_curves()
        self.assertEqual(live_curve.opts["pen"].color().name().upper(), live_color)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "loaded.csv"
            path.write_text("wavelength_nm,intensity_counts\n700,7\n701,8\n", encoding="utf-8")
            panel.load_spectra([path])
        self.assertTrue(all(ref["color"].upper() != live_color for ref in panel.references))
        panel.remove_reference(added["id"])
        self.assertEqual(live_curve.opts["pen"].color().name().upper(), live_color)
        panel._stop_btn.click()

    def test_retry_uses_frame_identity_power_sample_and_inventory_after_live_changes(self):
        ctrl = _Controller()
        panel = SpectrumPanel(ctrl)
        state = {"instruments": {"smu": {"connected": True, "readbacks": {"Vbg_meas": 1.2}}}}
        panel.set_sidebar_snapshot_provider(lambda: state.copy())
        ctrl.connected.emit([])
        with tempfile.TemporaryDirectory() as directory:
            old_root = cfg.filename.base_out
            old_sample = cfg.session.sample_id
            old_factor = cfg.pm100d.correction_factor
            cfg.filename.base_out, cfg.session.sample_id = directory, "old-sample"
            cfg.pm100d.correction_factor = 1.25
            original_save = panel._save_spectrum_data
            panel._save_spectrum_data = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full"))
            try:
                panel._add_spectrum_btn.click()
                ctrl.settings_applied.emit()
                ctrl.spectrum_ready.emit(np.array([700., 701.]), np.array([1., 2.]))
                ref = panel.references[0]
                ctrl.identity = {"model": "new-model", "serial_number": "new-serial"}
                cfg.session.sample_id = "new-sample"
                cfg.pm100d.correction_factor = 9.0
                state["instruments"]["smu"]["readbacks"]["Vbg_meas"] = 9.9
                panel._save_spectrum_data = original_save
                self.assertTrue(panel.retry_reference_save(ref["id"]))
                metadata = json.loads(Path(ref["metadata_path"]).read_text(encoding="utf-8"))
                self.assertEqual(metadata["device"]["sample_id"], "old-sample")
                self.assertEqual(metadata["device_id"], "LF6-1")
                settings = metadata["settings"]["requested"]
                self.assertEqual(settings["power_correction_factor"], 1.25)
                self.assertEqual(settings["instrument_identity"]["serial_number"], "LF6-1")
                self.assertEqual(settings["instrument_inventory"][0]["identity"]["serial_number"], "LF6-1")
                self.assertEqual(metadata["context"]["sidebar_readbacks"]["instruments"]["smu"]["readbacks"]["Vbg_meas"], 1.2)
            finally:
                cfg.filename.base_out, cfg.session.sample_id, cfg.pm100d.correction_factor = old_root, old_sample, old_factor


if __name__ == "__main__":
    unittest.main()
