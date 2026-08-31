from __future__ import annotations

import csv
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QGroupBox, QMessageBox

from app.devices.aps100_attodry1000_adapter import MockAPS100Adapter, decode_status_byte
from ui.mcd_panel import (
    MCD_SCALAR_FIELDS,
    MCDPanel,
    _ContinuousMCDWorker,
    _mcd_coordinates,
    _vtg_vbg_from_doping_efield,
    build_mcd_filename_base,
)
from utils.config import MCDConfig, cfg


class _Spectrum:
    def __init__(self, rotation, on_acquire=None):
        self.rotation = rotation
        self.on_acquire = on_acquire
        self.acquire_count = 0

    def calibration_wavelengths(self, force=False):
        del force
        return np.array([700.0, 701.0, 702.0])

    def acquire(self):
        time.sleep(0.02)
        self.acquire_count += 1
        if self.on_acquire is not None:
            self.on_acquire(self.acquire_count)
        wl = self.calibration_wavelengths()
        sign = 1.0 if self.rotation.position < 90 else -1.0
        return wl, np.array([10.0, 20.0, 30.0]) + sign


class _Rotation:
    def __init__(self):
        self.position = 0.0

    def move_to(self, position):
        self.position = float(position)

    def get_position(self):
        return self.position


class _RotationController:
    def __init__(self, rotation):
        self.rotation = rotation

    def adapter(self, slot):
        return self.rotation if slot == "rot1" else None


class _LF6Controller:
    def __init__(self, spectrum):
        self.is_connected = True
        self.is_ready = True
        self.is_busy = False
        self.adapter = spectrum
        self.setup = None
        self.ensure_calls = 0
        self.abort_calls = 0

    def ensure_ready(self, **_kwargs):
        self.ensure_calls += 1

    def abort_acquisition(self):
        self.abort_calls += 1
        return True


class _MagnetController:
    def __init__(self, adapter):
        self.adapter = adapter


class _PanelMagnetController(QObject):
    connected = Signal(object)
    disconnected = Signal()
    snapshot_updated = Signal(object)
    rates_updated = Signal(object)
    safe_move_audit = Signal(object)
    transition_progress = Signal(str, float)
    operation_finished = Signal(str)
    error = Signal(str)
    fault = Signal(str)

    is_connected = False
    exclusive_owner = ""

    def connect_instrument(self, *_args, **_kwargs):
        pass

    def disconnect_instrument(self):
        pass

    def take_remote(self):
        pass

    def refresh_snapshot(self):
        pass

    def refresh_rates(self):
        pass

    def pause(self):
        pass

    def enter_driven_mode(self):
        pass

    def enter_persistent_mode(self, **_kwargs):
        pass

    def safe_move_to_field(self, target_t, **kwargs):
        self.safe_move_call = (float(target_t), dict(kwargs))

    def acquire_exclusive(self, owner):
        self.exclusive_owner = str(owner)
        return True

    def release_exclusive(self, owner):
        if self.exclusive_owner == str(owner):
            self.exclusive_owner = ""


class _EmptyController:
    is_connected = False
    adapter = None
    setup = None
    device = None


class _RecordingDevice:
    def __init__(self, bias_calls):
        self.bias_calls = bias_calls
        self.gates = None

    def set_gates(self, **kwargs):
        self.gates = kwargs

    def set_bias(self, **kwargs):
        self.bias_calls.append(kwargs)

    def read_current_gates(self):
        return 0.10, 0.20

    def read_current_bias(self):
        return 0.05

    def read_currents(self):
        return 1e-9, 2e-9, 3e-9


class _RecordingSMU:
    def __init__(self, has_bias=True):
        self.is_connected = True
        self._has_bias = has_bias
        self.bias_calls = []
        self.device = _RecordingDevice(self.bias_calls)

    def has_vbias(self):
        return self._has_bias


def _workflow_params(base_output_dir: str) -> dict:
    return {
        "start_t": -0.01,
        "stop_t": 0.01,
        "field_tolerance_t": 0.0002,
        "move_timeout_s": 10.0,
        "start_settle_s": 0.0,
        "rotator": "rot1",
        "angle_a_deg": 45.0,
        "angle_b_deg": 135.0,
        "rotation_settle_s": 0.0,
        "exposure_ms": 1.0,
        "center_nm": 701.0,
        "frames": 1,
        "apply_voltages": False,
        "vbg_v": -0.25,
        "vtg_v": 1.0,
        "vbias_v": 0.01,
        "gate_ratio": 2.0,
        "voltage_ramp_step_v": 0.1,
        "voltage_step_delay_s": 0.0,
        "voltage_settle_s": 0.0,
        "sample_id": "Device01",
        "point": "p1",
        "condition_label": "",
        "temperature": "1.8",
        "measurement_mode": "Ref",
        "laser_nm": "532",
        "power_uw": "10",
        "power_coefficient": 2.0,
        "decimal_style": "dot",
        "filename_parts": ["temp_mode", "laser_power", "center", "exposure"],
        "base_output_dir": base_output_dir,
        "subfolder": "mcd",
    }


class MCDWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_panel_defaults_to_ref_and_previews_mcd_data_subfolder(self):
        panel = MCDPanel(
            _PanelMagnetController(),
            _EmptyController(),
            _EmptyController(),
            _EmptyController(),
        )
        self.addCleanup(panel.close)
        panel._mode.setCurrentText("Ref")
        panel._sample_id.setText("Device01")
        panel._update_coordinates_and_filename()

        self.assertEqual(panel._mode.currentText(), "Ref")
        self.assertIn("Device01", panel._filename_preview.text())
        self.assertIn("mcd", panel._path_preview.text())

    def test_panel_controls_have_explanatory_tooltips(self):
        panel = MCDPanel(
            _PanelMagnetController(),
            _EmptyController(),
            _EmptyController(),
            _EmptyController(),
        )
        self.addCleanup(panel.close)
        names = [
            "_resource", "_mock", "_connect_btn", "_disconnect_btn",
            "_remote_btn", "_refresh_btn", "_identity", "_field",
            "_output", "_heater", "_sweep", "_fault", "_pause_btn",
            "_driven_btn", "_persistent_btn", "_zero_leads", "_transition_status",
            "_safe_target", "_safe_rate", "_safe_final_mode", "_safe_settle",
            "_safe_rates_refresh_btn", "_safe_vmag_limit", "_safe_move_btn",
            "_safe_zero_btn", "_safe_state",
            "_advanced_toggle", "_metadata_toggle",
            "_start_t", "_stop_t", "_start_settle", "_sweep_rate_label",
            "_rotator", "_angle_a", "_angle_b", "_rotation_settle",
            "_exposure", "_center", "_frames",
            "_sweep_mode",
            "_gate_ratio", "_initial_voltage_settle", "_voltage_settle",
            "_condition_table", "_add_condition_btn", "_remove_condition_btn",
            "_sample_id", "_point", "_condition", "_temperature", "_mode",
            "_laser", "_power", "_power_coefficient",
            "_mode_notice", "_filename_preview", "_path_preview",
            "_run_btn", "_stop_btn", "_progress", "_run_status", "_log",
            "_show_spectrum_chk", "_clear_log_btn",
        ]
        missing = [
            name for name in names
            if not getattr(panel, name).toolTip().strip()
        ]
        self.assertEqual(missing, [])
        for checkbox in panel._filename_part_checks.values():
            self.assertTrue(checkbox.toolTip().strip())
        self.assertEqual(
            panel._sweep_rate_label.text(),
            "Normal range-dependent rates (RATE 0–4)",
        )
        self.assertIn("RATE 5 FAST mode is not used", panel._sweep_rate_label.toolTip())

    def test_layout_defaults_and_spectrum_toggle(self):
        panel = MCDPanel(
            _PanelMagnetController(),
            _EmptyController(),
            _EmptyController(),
            _EmptyController(),
        )
        self.addCleanup(panel.close)
        self.assertTrue(panel._show_spectrum_chk.isChecked())
        self.assertFalse(panel._plot.isHidden())
        panel._show_spectrum_chk.setChecked(False)
        self.assertTrue(panel._plot.isHidden())
        panel._show_spectrum_chk.setChecked(True)
        self.assertFalse(panel._plot.isHidden())
        titles = {box.title() for box in panel.findChildren(QGroupBox)}
        self.assertTrue({
            "Sample / Device", "Field Sweep", "Rotation",
            "LightField", "Gate / SMU", "Filename",
            "Run control", "Status / Progress / Log", "Continuous APS100 MCD",
            "Safe Magnet Control",
        }.issubset(titles))

    def test_advanced_transition_toggle_hides_and_shows(self):
        panel = MCDPanel(
            _PanelMagnetController(),
            _EmptyController(),
            _EmptyController(),
            _EmptyController(),
        )
        self.addCleanup(panel.close)
        panel.show()
        self.app.processEvents()
        self.assertFalse(panel._transition_widget.isVisible())
        panel._advanced_toggle.setChecked(True)
        self.assertTrue(panel._transition_widget.isVisible())
        panel._advanced_toggle.setChecked(False)
        self.assertFalse(panel._transition_widget.isVisible())

    def test_optional_filename_metadata_is_collapsed_and_expandable(self):
        panel = MCDPanel(
            _PanelMagnetController(),
            _EmptyController(),
            _EmptyController(),
            _EmptyController(),
        )
        self.addCleanup(panel.close)
        panel.show()
        self.app.processEvents()
        self.assertFalse(panel._metadata_widget.isVisible())
        panel._metadata_toggle.setChecked(True)
        self.assertTrue(panel._metadata_widget.isVisible())
        panel._metadata_toggle.setChecked(False)
        self.assertFalse(panel._metadata_widget.isVisible())

    def test_mode_notice_reflects_magnet_mode(self):
        panel = MCDPanel(
            _PanelMagnetController(),
            _EmptyController(),
            _EmptyController(),
            _EmptyController(),
        )
        self.addCleanup(panel.close)
        magnet = MockAPS100Adapter()
        magnet.connect()
        magnet._heater = False
        panel._on_snapshot(magnet.read_snapshot())
        self.assertIn("requires driven mode", panel._mode_notice.text())
        self.assertIn("stored", panel._field_caption.text().lower())
        magnet._heater = True
        panel._on_snapshot(magnet.read_snapshot())
        self.assertIn("MCD ready", panel._mode_notice.text())
        self.assertIn("live", panel._field_caption.text().lower())

    def test_safe_transition_progress_updates_existing_telemetry_without_polling(self):
        panel = MCDPanel(
            _PanelMagnetController(),
            _EmptyController(),
            _EmptyController(),
            _EmptyController(),
        )
        self.addCleanup(panel.close)
        magnet = MockAPS100Adapter()
        magnet.connect()
        magnet._heater = False
        magnet._field_t = 0.2
        panel._on_snapshot(magnet.read_snapshot())

        stored_field_text = panel._field.text()
        panel._safe_magnet_operation_active = True
        panel._on_transition("matching leads", 0.1)
        self.assertEqual(panel._field.text(), stored_field_text)
        self.assertIn("+0.100000 T", panel._output.text())
        self.assertIn("stored", panel._field_caption.text().lower())

        panel._on_transition("ramping field", 0.15)
        self.assertIn("+0.150000 T", panel._field.text())
        self.assertIn("+0.150000 T", panel._output.text())
        self.assertIn("live", panel._field_caption.text().lower())
        self.assertEqual(panel._sweep.text(), "ramping")

        panel._on_transition("ramping field", float("nan"))
        self.assertIn("telemetry unavailable", panel._transition_status.text())
        self.assertIn("+0.150000 T", panel._field.text())

        magnet._heater = True
        magnet._field_t = magnet._output_t = 0.2
        panel._on_snapshot(magnet.read_snapshot())
        self.assertIn("+0.200000 T", panel._field.text())
        self.assertIn("+0.200000 T", panel._output.text())

    def test_driven_mode_completion_shows_explicit_heater_notice(self):
        panel = MCDPanel(
            _PanelMagnetController(),
            _EmptyController(),
            _EmptyController(),
            _EmptyController(),
        )
        self.addCleanup(panel.close)

        with patch("ui.mcd_panel.QMessageBox.information") as information:
            panel._on_magnet_operation("enter_driven_mode")

        self.assertIn("heater ON", panel._transition_status.text())
        self.assertIn("heater is ON", panel._mode_notice.text())
        information.assert_called_once()
        self.assertIn("ready for an MCD field sweep", information.call_args.args[2])

    def test_safe_move_dispatches_atomic_persistent_request_and_locks_ui(self):
        magnet = _PanelMagnetController()
        magnet.is_connected = True
        panel = MCDPanel(
            magnet,
            _EmptyController(),
            _EmptyController(),
            _EmptyController(),
        )
        self.addCleanup(panel.close)
        adapter = MockAPS100Adapter()
        adapter.connect()
        panel._on_snapshot(adapter.read_snapshot())
        panel._safe_target.setValue(8.0)
        panel._safe_final_mode.setCurrentIndex(
            panel._safe_final_mode.findData("persistent")
        )

        with patch.object(
            cfg.magnet, "persistent_zero_max_magnet_voltage_v", 0.1
        ), patch(
            "ui.mcd_panel.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            panel._start_safe_magnet_move()

        target, kwargs = magnet.safe_move_call
        self.assertEqual(target, 8.0)
        self.assertEqual(kwargs["final_mode"], "persistent")
        self.assertNotIn("rate_t_per_min", kwargs)
        self.assertIn("stored aps100", panel._safe_rate.text().lower())
        self.assertTrue(kwargs["zero_leads"])
        self.assertFalse(kwargs["persistent_field_confirmed"])
        self.assertEqual(magnet.exclusive_owner, "safe_magnet_control")
        self.assertFalse(panel._safe_move_btn.isEnabled())

        panel._on_magnet_operation("safe_move:persistent:+8.000000")
        self.assertEqual(magnet.exclusive_owner, "")
        self.assertIn("Persistent", panel._safe_state.text())

    def test_safe_return_to_zero_finishes_persistent(self):
        magnet = _PanelMagnetController()
        magnet.is_connected = True
        panel = MCDPanel(
            magnet,
            _EmptyController(),
            _EmptyController(),
            _EmptyController(),
        )
        self.addCleanup(panel.close)
        adapter = MockAPS100Adapter()
        adapter.connect()
        panel._on_snapshot(adapter.read_snapshot())
        panel._safe_target.setValue(8.0)
        panel._safe_final_mode.setCurrentIndex(
            panel._safe_final_mode.findData("driven")
        )
        with patch.object(
            cfg.magnet, "persistent_zero_max_magnet_voltage_v", 0.1
        ), patch(
            "ui.mcd_panel.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            panel._safe_return_to_zero()
        target, kwargs = magnet.safe_move_call
        self.assertEqual(target, 0.0)
        self.assertEqual(kwargs["final_mode"], "persistent")

    def test_safe_move_audit_is_persisted(self):
        panel = MCDPanel(
            _PanelMagnetController(),
            _EmptyController(),
            _EmptyController(),
            _EmptyController(),
        )
        self.addCleanup(panel.close)
        payload = {
            "schema": "aps100_safe_move_audit_v1",
            "outcome": "completed",
            "request": {"target_t": 8.0, "final_mode": "persistent"},
            "commands": [{"kind": "write", "command": "SWEEP ZERO"}],
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            cfg.filename, "base_out", temp_dir
        ):
            panel._on_safe_move_audit(payload)
            audits = list((Path(temp_dir) / "magnet_audit").glob("*.json"))
            self.assertEqual(len(audits), 1)
            saved = json.loads(audits[0].read_text(encoding="utf-8"))
        self.assertEqual(saved, payload)

    def test_layout_state_round_trips(self):
        panel = MCDPanel(
            _PanelMagnetController(),
            _EmptyController(),
            _EmptyController(),
            _EmptyController(),
        )
        restored = MCDPanel(
            _PanelMagnetController(),
            _EmptyController(),
            _EmptyController(),
            _EmptyController(),
        )
        self.addCleanup(panel.close)
        self.addCleanup(restored.close)
        panel.resize(1200, 700)
        panel.show()
        restored.resize(1200, 700)
        restored.show()
        self.app.processEvents()
        panel._splitter.setSizes([500, 600])
        panel._plot_log_splitter.setSizes([240, 360])
        outer = panel._splitter.sizes()
        inner = panel._plot_log_splitter.sizes()
        state = panel.capture_session_state()
        self.assertEqual(state["splitter_sizes"], list(outer))
        self.assertEqual(state["plot_log_sizes"], list(inner))
        self.assertTrue(state["spectrum_visible"])
        restored.restore_session_state(state)
        self.app.processEvents()
        self.assertEqual(restored._splitter.sizes(), outer)
        self.assertEqual(restored._plot_log_splitter.sizes(), inner)
        self.assertTrue(restored._show_spectrum_chk.isChecked())
        self.assertFalse(restored._plot.isHidden())

        panel._show_spectrum_chk.setChecked(False)
        hidden_state = panel.capture_session_state()
        self.assertFalse(hidden_state["spectrum_visible"])
        restored.restore_session_state(hidden_state)
        self.assertFalse(restored._show_spectrum_chk.isChecked())
        self.assertTrue(restored._plot.isHidden())

    def test_round_trip_ui_controls_and_params(self):
        panel = MCDPanel(
            _PanelMagnetController(),
            _EmptyController(),
            _EmptyController(),
            _EmptyController(),
        )
        self.addCleanup(panel.close)
        self.assertEqual(panel._sweep_mode.currentData(), "one_way")
        params = panel._collect_params(require_sample=False)
        self.assertEqual(params["sweep_mode"], "one_way")
        self.assertEqual(
            sum(bool(row["enabled"]) for row in params["conditions"]),
            1,
        )

        panel._sweep_mode.setCurrentIndex(1)
        self.assertEqual(panel._sweep_mode.currentData(), "round_trip")
        params = panel._collect_params(require_sample=False)
        self.assertEqual(params["sweep_mode"], "round_trip")

    def test_filename_supports_modes_decimal_styles_and_gate_coordinates(self):
        params = {
            "sample_id": "Device01",
            "point": "p1",
            "condition_label": "",
            "temperature": "1.8",
            "measurement_mode": "Ref",
            "laser_nm": "532",
            "power_uw": "10",
            "power_coefficient": 2.0,
            "decimal_style": "dot",
            "filename_parts": ["temp_mode", "laser_power", "center", "exposure"],
            "center_nm": 750.25,
            "exposure_ms": 125.0,
            "frames": 3,
            "start_t": -0.25,
            "stop_t": 0.5,
            "rotator": "rot2",
            "angle_a_deg": 45.5,
            "angle_b_deg": 135.5,
            "vtg_v": 1.0,
            "vbg_v": -0.25,
            "vbias_v": 0.01,
            "gate_ratio": 2.0,
        }
        dotted = build_mcd_filename_base(params)
        self.assertEqual(MCDConfig().measurement_mode, "Ref")
        self.assertIn("1.8KREF", dotted)
        self.assertNotIn("532nm", dotted)
        self.assertIn("B-0.25to+0.5T", dotted)
        self.assertIn("D0.5V_E1.5V", dotted)

        params["measurement_mode"] = "PL"
        params["decimal_style"] = "p"
        legacy = build_mcd_filename_base(params)
        self.assertIn("1.8KPL_532nm20.000uW", legacy)
        self.assertIn("B-0p25to+0p5T", legacy)

    def test_continuous_two_angle_run_writes_one_descriptive_raw_csv(self):
        magnet = MockAPS100Adapter(time_scale=6.0)
        magnet.connect()
        # Begin outside the requested MCD window.  The workflow must reach the
        # start field before tightening LLIM/ULIM around the measurement sweep.
        magnet._field_t = 0.03
        magnet._output_t = 0.03
        rotation = _Rotation()
        with tempfile.TemporaryDirectory() as temp_dir:
            params = _workflow_params(temp_dir)
            worker = _ContinuousMCDWorker(
                params,
                _MagnetController(magnet),
                _LF6Controller(_Spectrum(rotation)),
                _RotationController(rotation),
                None,
            )
            result = worker._run()
            csv_path = Path(result["csv_path"])
            self.assertEqual(csv_path.parent, Path(temp_dir) / "Device01" / "mcd")
            self.assertIn("1.8KREF", csv_path.name)
            self.assertNotIn("532nm", csv_path.name)
            self.assertIn("D0.5V_E1.5V", csv_path.name)
            self.assertEqual(list(csv_path.parent.glob("*.csv")), [csv_path])

            with csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(rows[0][:16], MCD_SCALAR_FIELDS)
            self.assertEqual(rows[0][16:], ["700.000000", "701.000000", "702.000000"])
            self.assertGreaterEqual(len(rows), 5)
            angles = [float(row[4]) for row in rows[1:5]]
            self.assertEqual(angles, [45.0, 135.0, 135.0, 45.0])
            for row in rows[1:]:
                self.assertEqual(row[2], "forward")
                self.assertEqual(row[3], "up")
                self.assertAlmostEqual(float(row[7]), (float(row[5]) + float(row[6])) / 2.0)
                self.assertAlmostEqual(float(row[8]), 1.0)
                self.assertAlmostEqual(float(row[9]), -0.25)
                self.assertAlmostEqual(float(row[11]), 0.5)
                self.assertAlmostEqual(float(row[12]), 1.5)
                self.assertEqual(row[13:16], ["", "", ""])

            metadata_path = csv_path.with_suffix(".meta.json")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "completed")
            self.assertIn("aps100_slow_rate_profile", metadata)
            self.assertFalse(any(csv_path.parent.glob("mcd_*.csv")))
            self.assertEqual(magnet.get_sweep_state(), "pause")

    def test_stop_preserves_partial_csv_and_marks_metadata_stopped(self):
        magnet = MockAPS100Adapter(time_scale=60.0)
        magnet.connect()
        rotation = _Rotation()
        holder = {}

        def stop_after_second_spectrum(count):
            if count == 2:
                holder["worker"].request_stop()

        with tempfile.TemporaryDirectory() as temp_dir:
            spectrum = _Spectrum(rotation, on_acquire=stop_after_second_spectrum)
            worker = _ContinuousMCDWorker(
                _workflow_params(temp_dir),
                _MagnetController(magnet),
                _LF6Controller(spectrum),
                _RotationController(rotation),
                None,
            )
            holder["worker"] = worker
            results = []
            worker.finished.connect(results.append)
            worker.run()

            self.assertTrue(results[0]["stopped"])
            output_dir = Path(temp_dir) / "Device01" / "mcd"
            csv_path = next(output_dir.glob("*.csv"))
            with csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(len(rows), 3)  # header plus two completed spectra
            metadata = json.loads(
                csv_path.with_suffix(".meta.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["status"], "stopped")
            self.assertEqual(metadata["spectra_written"], 2)
            self.assertEqual(magnet.get_sweep_state(), "pause")

    def test_round_trip_writes_both_directions_and_metadata(self):
        magnet = MockAPS100Adapter(time_scale=60.0)
        magnet.connect()
        rotation = _Rotation()
        with tempfile.TemporaryDirectory() as temp_dir:
            params = _workflow_params(temp_dir)
            params["sweep_mode"] = "round_trip"
            worker = _ContinuousMCDWorker(
                params,
                _MagnetController(magnet),
                _LF6Controller(_Spectrum(rotation)),
                _RotationController(rotation),
                None,
            )
            result = worker._run()
            csv_path = Path(result["csv_path"])
            with csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            fields = [float(row[7]) for row in rows[1:]]
            self.assertGreaterEqual(len(fields), 4)
            self.assertGreater(max(fields), 0.005)
            self.assertLess(min(fields), -0.005)
            self.assertLess(fields[-1], 0.0)
            metadata = json.loads(
                csv_path.with_suffix(".meta.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["status"], "completed")
            self.assertEqual(metadata["sweep_mode"], "round_trip")
            self.assertEqual(
                [leg["direction"] for leg in metadata["legs"]],
                ["up", "down"],
            )

    def test_stop_during_round_trip_marks_stopped_and_pauses(self):
        magnet = MockAPS100Adapter(time_scale=60.0)
        magnet.connect()
        rotation = _Rotation()
        holder = {}

        def stop_after_third_spectrum(count):
            if count == 3:
                holder["worker"].request_stop()

        with tempfile.TemporaryDirectory() as temp_dir:
            params = _workflow_params(temp_dir)
            params["sweep_mode"] = "round_trip"
            spectrum = _Spectrum(rotation, on_acquire=stop_after_third_spectrum)
            worker = _ContinuousMCDWorker(
                params,
                _MagnetController(magnet),
                _LF6Controller(spectrum),
                _RotationController(rotation),
                None,
            )
            holder["worker"] = worker
            results = []
            worker.finished.connect(results.append)
            worker.run()

            self.assertTrue(results[0]["stopped"])
            output_dir = Path(temp_dir) / "Device01" / "mcd"
            csv_path = next(output_dir.glob("*.csv"))
            metadata = json.loads(
                csv_path.with_suffix(".meta.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["status"], "stopped")
            self.assertEqual(metadata["sweep_mode"], "round_trip")
            self.assertGreaterEqual(len(metadata["legs"]), 1)
            self.assertEqual(magnet.get_sweep_state(), "pause")

    def test_doping_efield_inverse_transform(self):
        vtg, vbg = _vtg_vbg_from_doping_efield(1.0, -2.0, 2.0)
        self.assertAlmostEqual(vtg, -0.5)
        self.assertAlmostEqual(vbg, 0.75)
        # Round trip: Vtg/Vbg -> D/E -> inverse reproduces Vtg/Vbg.
        doping, efield = _mcd_coordinates(vtg, vbg, 2.0)
        back_vtg, back_vbg = _vtg_vbg_from_doping_efield(doping, efield, 2.0)
        self.assertAlmostEqual(back_vtg, vtg)
        self.assertAlmostEqual(back_vbg, vbg)
        with self.assertRaises(ValueError):
            _vtg_vbg_from_doping_efield(1.0, 1.0, 0.0)

    def test_condition_table_bidirectional_sync(self):
        panel = MCDPanel(
            _PanelMagnetController(),
            _EmptyController(),
            _EmptyController(),
            _EmptyController(),
        )
        self.addCleanup(panel.close)
        panel._gate_ratio.setValue(2.0)
        panel._condition_table.item(0, 1).setText("1")
        panel._condition_table.item(0, 2).setText("-0.25")
        self.assertAlmostEqual(
            float(panel._condition_table.item(0, 4).text()), 0.5
        )
        self.assertAlmostEqual(
            float(panel._condition_table.item(0, 5).text()), 1.5
        )

        panel._condition_table.item(0, 4).setText("2")
        panel._condition_table.item(0, 5).setText("0")
        self.assertAlmostEqual(
            float(panel._condition_table.item(0, 1).text()), 1.0
        )
        self.assertAlmostEqual(
            float(panel._condition_table.item(0, 2).text()), 0.5
        )

        panel._gate_ratio.setValue(0.0)
        flags = panel._condition_table.item(0, 4).flags()
        self.assertFalse(bool(flags & Qt.ItemFlag.ItemIsEditable))

    def test_gate_ratio_factors_sync_like_2100(self):
        panel = MCDPanel(
            _PanelMagnetController(),
            _EmptyController(),
            _EmptyController(),
            _EmptyController(),
        )
        self.addCleanup(panel.close)
        panel.gate_vtg_factor.setValue(2.0)
        panel.gate_vbg_factor.setValue(1.0)
        self.assertAlmostEqual(panel._gate_ratio.value(), 0.5)
        self.assertIn("r = 0.5", panel.gate_ratio_value.text())

        # Doping/E-field entry uses the factor-derived ratio.
        panel._seed_condition_table([])
        panel._gate_entry_mode.setCurrentIndex(
            panel._gate_entry_mode.findData("doping_efield")
        )
        panel._gate_entry_a.setText("1")
        panel._gate_entry_b.setText("0")
        panel._gate_entry_add.click()
        rows = panel._condition_rows()
        self.assertAlmostEqual(rows[0]["vtg_v"], 0.5)
        self.assertAlmostEqual(rows[0]["vbg_v"], 1.0)

        # Direct r edits canonicalize the factors to 1 : r.
        panel._gate_ratio.setValue(2.0)
        self.assertAlmostEqual(panel.gate_vtg_factor.value(), 1.0)
        self.assertAlmostEqual(panel.gate_vbg_factor.value(), 2.0)
        self.assertAlmostEqual(panel._gate_ratio.value(), 2.0)

    def test_gates_are_always_ramped_before_measurement(self):
        panel = MCDPanel(
            _PanelMagnetController(),
            _EmptyController(),
            _EmptyController(),
            _EmptyController(),
        )
        self.addCleanup(panel.close)
        params = panel._collect_params(require_sample=False)
        self.assertTrue(params["apply_voltages"])
        self.assertFalse(hasattr(panel, "_apply_voltages"))

    def test_gate_settle_controls_flow_into_params(self):
        panel = MCDPanel(
            _PanelMagnetController(),
            _EmptyController(),
            _EmptyController(),
            _EmptyController(),
        )
        self.addCleanup(panel.close)
        panel._initial_voltage_settle.setValue(2.5)
        panel._voltage_settle.setValue(1.5)
        params = panel._collect_params(require_sample=False)
        self.assertAlmostEqual(params["initial_voltage_settle_s"], 2.5)
        self.assertAlmostEqual(params["voltage_settle_s"], 1.5)

    def test_gate_settle_uses_first_and_later_delays(self):
        magnet = MockAPS100Adapter(time_scale=60.0)
        magnet.connect()
        rotation = _Rotation()
        smu = _RecordingSMU(has_bias=True)
        with tempfile.TemporaryDirectory() as temp_dir:
            params = _workflow_params(temp_dir)
            params["apply_voltages"] = True
            params["initial_voltage_settle_s"] = 0.01
            params["voltage_settle_s"] = 0.0
            worker = _ContinuousMCDWorker(
                params,
                _MagnetController(magnet),
                _LF6Controller(_Spectrum(rotation)),
                _RotationController(rotation),
                smu,
            )
            first = worker._apply_voltages({**params, "condition_index": 1})
            later = worker._apply_voltages({**params, "condition_index": 2})
            self.assertAlmostEqual(first["gate_settle_s"], 0.01)
            self.assertAlmostEqual(later["gate_settle_s"], 0.0)
            self.assertEqual(first["gate_settle_phase"], "first")
            self.assertEqual(later["gate_settle_phase"], "later")

    def test_gate_entry_adds_direct_and_doping_efield_rows(self):
        panel = MCDPanel(
            _PanelMagnetController(),
            _EmptyController(),
            _EmptyController(),
            _EmptyController(),
        )
        self.addCleanup(panel.close)
        panel._seed_condition_table([])
        panel._gate_ratio.setValue(2.0)

        # Direct Vtg / Vbg with a comma list plus a single Vbg value.
        panel._gate_entry_mode.setCurrentIndex(
            panel._gate_entry_mode.findData("direct")
        )
        panel._gate_entry_a.setText("1,2")
        panel._gate_entry_b.setText("-0.25")
        panel._gate_entry_add.click()
        rows = panel._condition_rows()
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["vtg_v"], 1.0)
        self.assertAlmostEqual(rows[0]["vbg_v"], -0.25)
        self.assertAlmostEqual(rows[0]["doping_v"], 0.5)
        self.assertAlmostEqual(rows[0]["efield_v"], 1.5)
        self.assertAlmostEqual(rows[1]["vtg_v"], 2.0)
        self.assertAlmostEqual(rows[1]["vbg_v"], -0.25)

        # Doping / E-field: Vtg/Vbg are back-computed from the gate ratio.
        panel._gate_entry_mode.setCurrentIndex(
            panel._gate_entry_mode.findData("doping_efield")
        )
        panel._gate_entry_a.setText("1")
        panel._gate_entry_b.setText("0")
        panel._gate_entry_add.click()
        rows = panel._condition_rows()
        self.assertEqual(len(rows), 3)
        self.assertAlmostEqual(rows[2]["vtg_v"], 0.5)
        self.assertAlmostEqual(rows[2]["vbg_v"], 0.25)
        self.assertAlmostEqual(rows[2]["doping_v"], 1.0)
        self.assertAlmostEqual(rows[2]["efield_v"], 0.0)

    def test_gate_entry_doping_mode_requires_nonzero_ratio(self):
        panel = MCDPanel(
            _PanelMagnetController(),
            _EmptyController(),
            _EmptyController(),
            _EmptyController(),
        )
        self.addCleanup(panel.close)
        panel._gate_ratio.setValue(0.0)
        panel._gate_entry_mode.setCurrentIndex(
            panel._gate_entry_mode.findData("doping_efield")
        )
        panel._gate_entry_a.setText("1")
        panel._gate_entry_b.setText("0")
        panel._update_gate_entry()
        self.assertFalse(panel._gate_entry_add.isEnabled())
        self.assertIn("non-zero", panel._gate_entry_status.text())

    def test_multiple_conditions_write_one_csv_each(self):
        magnet = MockAPS100Adapter(time_scale=60.0)
        magnet.connect()
        rotation = _Rotation()
        with tempfile.TemporaryDirectory() as temp_dir:
            params = _workflow_params(temp_dir)
            params["conditions"] = [
                {"enabled": True, "vtg_v": 1.0, "vbg_v": -0.25, "vbias_v": 0.0},
                {"enabled": True, "vtg_v": 0.5, "vbg_v": 0.5, "vbias_v": 0.0},
                {"enabled": False, "vtg_v": 9.0, "vbg_v": 0.0, "vbias_v": 0.0},
            ]
            worker = _ContinuousMCDWorker(
                params,
                _MagnetController(magnet),
                _LF6Controller(_Spectrum(rotation)),
                _RotationController(rotation),
                None,
            )
            result = worker._run()
            self.assertEqual(len(result["csv_paths"]), 2)
            paths = [Path(p) for p in result["csv_paths"]]
            self.assertNotEqual(paths[0].name, paths[1].name)
            self.assertIn("D0.5V_E1.5V", paths[0].name)
            self.assertIn("D1.5V_E-0.5V", paths[1].name)
            for path in paths:
                metadata = json.loads(
                    path.with_suffix(".meta.json").read_text(encoding="utf-8")
                )
                self.assertEqual(metadata["status"], "completed")
                self.assertEqual(metadata["condition_count"], 2)
            self.assertEqual(
                [c["status"] for c in result["conditions"]],
                ["completed", "completed"],
            )

    def test_stop_during_second_condition_marks_statuses(self):
        magnet = MockAPS100Adapter(time_scale=60.0)
        magnet.connect()
        rotation = _Rotation()
        holder = {}

        def on_log(message):
            if "Condition 2/3" in message:
                holder["stop_next"] = True

        def stop_when_flagged(count):
            del count
            if holder.get("stop_next"):
                holder["worker"].request_stop()

        with tempfile.TemporaryDirectory() as temp_dir:
            params = _workflow_params(temp_dir)
            params["conditions"] = [
                {"enabled": True, "vtg_v": 1.0, "vbg_v": -0.25, "vbias_v": 0.0},
                {"enabled": True, "vtg_v": 0.5, "vbg_v": 0.5, "vbias_v": 0.0},
                {"enabled": True, "vtg_v": 0.0, "vbg_v": 0.0, "vbias_v": 0.0},
            ]
            spectrum = _Spectrum(rotation, on_acquire=stop_when_flagged)
            worker = _ContinuousMCDWorker(
                params,
                _MagnetController(magnet),
                _LF6Controller(spectrum),
                _RotationController(rotation),
                None,
            )
            holder["worker"] = worker
            worker.log.connect(on_log)
            results = []
            worker.finished.connect(results.append)
            worker.run()

            self.assertTrue(results[0]["stopped"])
            output_dir = Path(temp_dir) / "Device01" / "mcd"
            csvs = sorted(output_dir.glob("*.csv"))
            self.assertEqual(len(csvs), 2)
            first_meta = json.loads(
                csvs[0].with_suffix(".meta.json").read_text(encoding="utf-8")
            )
            second_meta = json.loads(
                csvs[1].with_suffix(".meta.json").read_text(encoding="utf-8")
            )
            self.assertEqual(first_meta["status"], "completed")
            self.assertEqual(second_meta["status"], "stopped")
            self.assertEqual(
                [c["status"] for c in results[0]["conditions"]],
                ["completed", "stopped", "not_started"],
            )
            self.assertEqual(magnet.get_sweep_state(), "pause")

    def test_vbias_skipped_when_no_channel_connected(self):
        magnet = MockAPS100Adapter(time_scale=60.0)
        magnet.connect()
        rotation = _Rotation()
        smu = _RecordingSMU(has_bias=False)
        with tempfile.TemporaryDirectory() as temp_dir:
            params = _workflow_params(temp_dir)
            params["apply_voltages"] = True
            worker = _ContinuousMCDWorker(
                params,
                _MagnetController(magnet),
                _LF6Controller(_Spectrum(rotation)),
                _RotationController(rotation),
                smu,
            )
            result = worker._run()
            metadata = json.loads(
                Path(result["csv_path"]).with_suffix(".meta.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(metadata["applied_bias"], False)
            self.assertEqual(
                metadata["bias_skipped_reason"], "no connected Vbias channel"
            )
            self.assertEqual(smu.bias_calls, [])

    def test_vbias_applied_when_channel_connected(self):
        magnet = MockAPS100Adapter(time_scale=60.0)
        magnet.connect()
        rotation = _Rotation()
        smu = _RecordingSMU(has_bias=True)
        with tempfile.TemporaryDirectory() as temp_dir:
            params = _workflow_params(temp_dir)
            params["apply_voltages"] = True
            worker = _ContinuousMCDWorker(
                params,
                _MagnetController(magnet),
                _LF6Controller(_Spectrum(rotation)),
                _RotationController(rotation),
                smu,
            )
            result = worker._run()
            metadata = json.loads(
                Path(result["csv_path"]).with_suffix(".meta.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(metadata["applied_bias"], True)
            self.assertIsNone(metadata["bias_skipped_reason"])
            self.assertEqual(len(smu.bias_calls), 1)
            self.assertAlmostEqual(smu.bias_calls[0]["Vbias"], 0.01)

    def test_voltage_readback_logged_after_ramp(self):
        magnet = MockAPS100Adapter(time_scale=60.0)
        magnet.connect()
        rotation = _Rotation()
        smu = _RecordingSMU(has_bias=True)
        with tempfile.TemporaryDirectory() as temp_dir:
            params = _workflow_params(temp_dir)
            params["apply_voltages"] = True
            worker = _ContinuousMCDWorker(
                params,
                _MagnetController(magnet),
                _LF6Controller(_Spectrum(rotation)),
                _RotationController(rotation),
                smu,
            )
            result = worker._run()
            csv_path = Path(result["csv_path"])
            metadata = json.loads(
                csv_path.with_suffix(".meta.json").read_text(encoding="utf-8")
            )
            readback = metadata["post_ramp_readback"]
            self.assertAlmostEqual(readback["Ibg_A"], 1e-9)
            self.assertAlmostEqual(readback["Itg_A"], 2e-9)
            self.assertAlmostEqual(readback["Ibias_A"], 3e-9)

    def test_lightfield_not_ready_blocks_before_remote_or_magnet_mutation(self):
        magnet = MockAPS100Adapter(time_scale=60.0)
        magnet.connect()
        rotation = _Rotation()
        optical = _LF6Controller(_Spectrum(rotation))
        optical.is_ready = False
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = _ContinuousMCDWorker(
                _workflow_params(temp_dir),
                _MagnetController(magnet), optical,
                _RotationController(rotation), None,
            )
            with self.assertRaisesRegex(RuntimeError, "LightField is not ready"):
                worker._run()
        self.assertEqual(optical.ensure_calls, 1)
        self.assertFalse(magnet._remote)

    def test_mid_sweep_fault_fails_closed_and_preserves_partial_artifacts(self):
        class FaultMagnet(MockAPS100Adapter):
            faulted = False

            def get_status(self):
                if self.faulted:
                    return decode_status_byte(0b00000100)
                return super().get_status()

        magnet = FaultMagnet(time_scale=60.0)
        magnet.connect()
        rotation = _Rotation()
        spectrum = _Spectrum(rotation, on_acquire=lambda _count: setattr(magnet, "faulted", True))
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = _ContinuousMCDWorker(
                _workflow_params(temp_dir), _MagnetController(magnet),
                _LF6Controller(spectrum), _RotationController(rotation), None,
            )
            results = []
            worker.finished.connect(results.append)
            worker.run()
            self.assertIn("quench", results[0]["error"])
            output_dir = Path(temp_dir) / "Device01" / "mcd"
            self.assertTrue(any(output_dir.glob("*.csv")))
            self.assertEqual(magnet.get_sweep_state(), "pause")

    def test_stalled_sweep_hits_progress_watchdog_and_pauses(self):
        class StalledMagnet(MockAPS100Adapter):
            def _update(self):
                self._last_update = time.monotonic()

        magnet = StalledMagnet(time_scale=60.0)
        magnet.connect()
        magnet._field_t = magnet._output_t = -0.01
        rotation = _Rotation()
        with tempfile.TemporaryDirectory() as temp_dir:
            params = _workflow_params(temp_dir)
            params["sweep_progress_timeout_s"] = 0.02
            worker = _ContinuousMCDWorker(
                params, _MagnetController(magnet),
                _LF6Controller(_Spectrum(rotation)),
                _RotationController(rotation), None,
            )
            results = []
            worker.finished.connect(results.append)
            worker.run()
            self.assertIn("no measurable progress", results[0]["error"])
            self.assertEqual(magnet.get_sweep_state(), "pause")

    def test_firmware_167_standby_state_is_accepted_at_start(self):
        class StandbyAPS100(MockAPS100Adapter):
            def get_sweep_state(self):
                self._update()
                if self._target_t is None:
                    return "standby"
                return super().get_sweep_state()

        magnet = StandbyAPS100(time_scale=6.0)
        magnet.connect()
        rotation = _Rotation()
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = _ContinuousMCDWorker(
                _workflow_params(temp_dir),
                _MagnetController(magnet),
                _LF6Controller(_Spectrum(rotation)),
                _RotationController(rotation),
                None,
            )
            result = worker._run()
            self.assertIn("csv_path", result)
            self.assertEqual(magnet.get_sweep_state(), "standby")

    def test_progress_is_monotonic_while_repositioning_to_start_field(self):
        magnet = MockAPS100Adapter(time_scale=6.0)
        magnet.connect()
        # The magnet sits above the requested window, so the workflow must
        # move down to start_t before the measurement leg begins.
        magnet._field_t = 0.03
        magnet._output_t = 0.03
        rotation = _Rotation()
        percents = []
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = _ContinuousMCDWorker(
                _workflow_params(temp_dir),
                _MagnetController(magnet),
                _LF6Controller(_Spectrum(rotation)),
                _RotationController(rotation),
                None,
            )
            worker.progress.connect(
                lambda _field, percent, _cond, _count: percents.append(percent)
            )
            result = worker._run()
            self.assertIn("csv_path", result)
        self.assertEqual(percents, sorted(percents))

    def test_field_telemetry_polls_during_acquisition(self):
        magnet = MockAPS100Adapter(time_scale=6.0)
        magnet.connect()
        state = {"acquiring": False, "polls_during": 0}
        original_read = magnet.get_field_t

        def counting_read():
            if state["acquiring"]:
                state["polls_during"] += 1
            return original_read()

        magnet.get_field_t = counting_read

        class SlowSpectrum(_Spectrum):
            def acquire(self):
                state["acquiring"] = True
                try:
                    time.sleep(0.6)
                    return super().acquire()
                finally:
                    state["acquiring"] = False

        rotation = _Rotation()
        with tempfile.TemporaryDirectory() as temp_dir:
            params = _workflow_params(temp_dir)
            params["field_telemetry_s"] = 0.0
            worker = _ContinuousMCDWorker(
                params,
                _MagnetController(magnet),
                _LF6Controller(SlowSpectrum(rotation)),
                _RotationController(rotation),
                None,
            )
            result = worker._run()
            self.assertIn("csv_path", result)
        self.assertGreaterEqual(state["polls_during"], 1)

    def test_unexpected_pause_before_endpoint_fails_closed(self):
        magnet = MockAPS100Adapter(time_scale=6.0)
        magnet.connect()
        rotation = _Rotation()
        spectrum = _Spectrum(rotation, on_acquire=lambda _count: magnet.pause())
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = _ContinuousMCDWorker(
                _workflow_params(temp_dir), _MagnetController(magnet),
                _LF6Controller(spectrum), _RotationController(rotation), None,
            )
            results = []
            worker.finished.connect(results.append)
            worker.run()
            self.assertIn("paused", results[0]["error"])


if __name__ == "__main__":
    unittest.main()
