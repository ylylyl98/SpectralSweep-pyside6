from __future__ import annotations

import csv
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication

from app.devices.aps100_attodry1000_adapter import MockAPS100Adapter
from ui.mcd_panel import (
    MCDPanel,
    _ContinuousMCDWorker,
    _mcd_coordinates,
    _vtg_vbg_from_doping_efield,
    build_mcd_filename_base,
)
from utils.config import MCDConfig


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
        self.adapter = spectrum
        self.setup = None


class _MagnetController:
    def __init__(self, adapter):
        self.adapter = adapter


class _PanelMagnetController(QObject):
    connected = Signal(object)
    disconnected = Signal()
    snapshot_updated = Signal(object)
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

    def pause(self):
        pass

    def enter_driven_mode(self):
        pass

    def enter_persistent_mode(self, **_kwargs):
        pass


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
        "subfolder": "MCD Data",
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
        self.assertIn("MCD Data", panel._path_preview.text())

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
            "_advanced_toggle",
            "_start_t", "_stop_t", "_start_settle",
            "_rotator", "_angle_a", "_angle_b", "_rotation_settle",
            "_exposure", "_center", "_frames",
            "_sweep_mode",
            "_apply_voltages", "_gate_ratio",
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
        magnet._heater = True
        panel._on_snapshot(magnet.read_snapshot())
        self.assertIn("MCD ready", panel._mode_notice.text())

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
        self.assertEqual(len(params["conditions"]), 1)

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
            self.assertEqual(csv_path.parent, Path(temp_dir) / "Device01" / "MCD Data")
            self.assertIn("1.8KREF", csv_path.name)
            self.assertNotIn("532nm", csv_path.name)
            self.assertIn("D0.5V_E1.5V", csv_path.name)
            self.assertEqual(list(csv_path.parent.glob("*.csv")), [csv_path])

            with csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(
                rows[0][:7],
                [
                    "Bfield_T",
                    "rotation_angle_deg",
                    "Vtg_V",
                    "Vbg_V",
                    "Vbias_V",
                    "Doping_V",
                    "Efield_V",
                ],
            )
            self.assertEqual(rows[0][7:], ["700.000000", "701.000000", "702.000000"])
            self.assertGreaterEqual(len(rows), 5)
            angles = [float(row[1]) for row in rows[1:5]]
            self.assertEqual(angles, [45.0, 135.0, 135.0, 45.0])
            for row in rows[1:]:
                self.assertAlmostEqual(float(row[2]), 1.0)
                self.assertAlmostEqual(float(row[3]), -0.25)
                self.assertAlmostEqual(float(row[5]), 0.5)
                self.assertAlmostEqual(float(row[6]), 1.5)

            metadata_path = csv_path.with_suffix(".meta.json")
            event_path = csv_path.with_suffix(".log")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "completed")
            self.assertIn("aps100_slow_rate_profile", metadata)
            self.assertTrue(event_path.exists())
            self.assertFalse(any(csv_path.parent.glob("mcd_*.csv")))
            self.assertEqual(magnet.get_sweep_state(), "sweep paused")

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
            output_dir = Path(temp_dir) / "Device01" / "MCD Data"
            csv_path = next(output_dir.glob("*.csv"))
            with csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(len(rows), 3)  # header plus two completed spectra
            metadata = json.loads(
                csv_path.with_suffix(".meta.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["status"], "stopped")
            self.assertEqual(metadata["spectra_written"], 2)
            self.assertEqual(magnet.get_sweep_state(), "sweep paused")

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
            fields = [float(row[0]) for row in rows[1:]]
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
            event_log = csv_path.with_suffix(".log").read_text(encoding="utf-8")
            self.assertIn("leg=1/2", event_log)
            self.assertIn("direction=up", event_log)
            self.assertIn("direction=down", event_log)

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
            output_dir = Path(temp_dir) / "Device01" / "MCD Data"
            csv_path = next(output_dir.glob("*.csv"))
            metadata = json.loads(
                csv_path.with_suffix(".meta.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["status"], "stopped")
            self.assertEqual(metadata["sweep_mode"], "round_trip")
            self.assertGreaterEqual(len(metadata["legs"]), 1)
            self.assertEqual(magnet.get_sweep_state(), "sweep paused")

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
            summary = json.loads(
                Path(result["summary_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                [c["status"] for c in summary["conditions"]],
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
            output_dir = Path(temp_dir) / "Device01" / "MCD Data"
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
            summary = json.loads(
                Path(results[0]["summary_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                [c["status"] for c in summary["conditions"]],
                ["completed", "stopped", "not_started"],
            )
            self.assertEqual(magnet.get_sweep_state(), "sweep paused")

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
            event_log = csv_path.with_suffix(".log").read_text(encoding="utf-8")
            self.assertIn("voltage_pre_ramp", event_log)
            self.assertIn("voltage_settled", event_log)
            self.assertIn("Ibg=1e-09", event_log)
            self.assertIn("Itg=2e-09", event_log)


if __name__ == "__main__":
    unittest.main()
