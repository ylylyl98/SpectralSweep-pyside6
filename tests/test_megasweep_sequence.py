from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui.megasweep_panel import (
    CoordSystem,
    MegaSweepPanel,
    OpticalCondition,
    _MegaSweepWorker,
)


class _FakeIV:
    def __init__(self):
        self.gates = (0.0, 0.0)
        self.bias = 0.0
        self.zero_ramps = 0

    def set_gates(self, Vtg, Vbg, **_kwargs):
        self.gates = (float(Vtg), float(Vbg))

    def set_bias(self, Vbias, **_kwargs):
        self.bias = float(Vbias)

    def read_gates(self):
        return self.gates[1], self.gates[0]

    def read_current_bias(self):
        return self.bias

    def read_currents(self):
        return 1e-9, 2e-9, 3e-9

    def ramp_all_to_zero(self, **_kwargs):
        self.gates = (0.0, 0.0)
        self.bias = 0.0
        self.zero_ramps += 1


class _FakeSMUController:
    is_connected = True

    def __init__(self):
        self.device = _FakeIV()


class _FakeSpectrometer:
    def __init__(self):
        self.centers = []
        self.exposures = []
        self.frames = []
        self.acquire_count = 0

    def change_spectra_center(self, value):
        self.centers.append(float(value))

    def change_expose_time(self, value):
        self.exposures.append(float(value))

    def set_accumulations(self, value):
        self.frames.append(int(value))

    def get_wavelength_calibration(self):
        center = self.centers[-1]
        return np.array([center - 1.0, center, center + 1.0])

    def acquire(self):
        self.acquire_count += 1
        return np.array([10.0, 20.0, 30.0])


class _FakeLF6Controller:
    is_connected = True

    def __init__(self):
        self.adapter = _FakeSpectrometer()
        self.setup = self.adapter

    def set_center_wavelength_when_ready(self, value):
        self.adapter.change_spectra_center(value)

    def configure_for_acquisition(self, *, center_nm, exposure_ms, frames):
        self.adapter.change_spectra_center(center_nm)
        self.adapter.change_expose_time(exposure_ms)
        self.adapter.set_accumulations(frames)


def _point(a: float, b: float) -> dict:
    return {
        "axis_a": a,
        "axis_b": b,
        "axis_values": {"Vtg": a, "Vbg": b, "Doping": a + b, "E-field": a - b},
        "raw": (a, b, 0.0),
    }


def _params(out_path: Path) -> dict:
    points = [_point(0.0, 0.0), _point(0.1, 0.2)]
    return {
        "coord": CoordSystem.RAW,
        "axis_a": "Vtg",
        "axis_b": "Vbg",
        "axis_a_desc": {"start": 0.0, "stop": 0.1, "step": 0.1, "mode": "Step Size", "points": 2},
        "axis_b_desc": {"start": 0.0, "stop": 0.2, "step": 0.2, "mode": "Step Size", "points": 2},
        "axis_a_vals": np.array([0.0, 0.1]),
        "axis_b_vals": np.array([0.0, 0.2]),
        "all_points": points,
        "valid_points": points,
        "fixed": {"Vbias": 0.0},
        "safety": {
            "vtg_min": -1.0,
            "vtg_max": 1.0,
            "vbg_min": -1.0,
            "vbg_max": 1.0,
            "vbias_min": -1.0,
            "vbias_max": 1.0,
        },
        "ratio": 1.0,
        "snake": False,
        "settle": 0.0,
        "extra_overhead_s": 0.0,
        "ramp_step": 0.1,
        "step_delay_s": 0.0,
        "sample": "device",
        "tag": "sequence",
        "laser_nm": "",
        "power_uw": "",
        "vbias_available": True,
        "smu_connected": True,
        "lf6_connected": True,
        "out_path": out_path,
        "center_nm": 720.0,
        "exp_ms": 30.0,
        "frames": 2,
        "base_name": "unused",
        "optical_conditions": [
            {"enabled": True, "name": "red", "center_nm": 720.0, "exposure_ms": 30.0, "frames": 2},
            {"enabled": True, "name": "blue", "center_nm": 750.0, "exposure_ms": 40.0, "frames": 3},
        ],
    }


class MegaSweepSequenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_sequence_state_round_trip_and_legacy_restore(self):
        panel = MegaSweepPanel()
        self.addCleanup(panel.close)
        panel._optical_widget.set_conditions([
            OpticalCondition(True, "first", 721.5, 31.0, 4),
            OpticalCondition(False, "second", 755.0, 80.0, 9),
        ])

        state = panel.capture_session_state()
        restored = MegaSweepPanel()
        self.addCleanup(restored.close)
        restored.restore_session_state(state)
        self.assertEqual(
            restored._optical_widget.conditions(),
            panel._optical_widget.conditions(),
        )

        legacy = MegaSweepPanel()
        self.addCleanup(legacy.close)
        legacy.restore_session_state({
            "optical": {"center_nm": 812.0, "exposure_ms": 55.0, "frames": 7}
        })
        self.assertEqual(
            legacy._optical_widget.conditions(),
            [OpticalCondition(True, "C1", 812.0, 55.0, 7)],
        )

    def test_worker_creates_one_complete_file_per_condition(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            smu = _FakeSMUController()
            lf6 = _FakeLF6Controller()
            worker = _MegaSweepWorker(_params(Path(temp_dir)), smu, lf6)
            progress = []
            maps = []
            worker.progress.connect(lambda done, total: progress.append((done, total)))
            worker.map_started.connect(
                lambda index, count, description: maps.append((index, count, description))
            )

            with patch(
                "ui.megasweep_panel._get_wavelengths",
                side_effect=lambda _spec, _lf6, center, tol_nm: np.array(
                    [center - 1.0, center, center + 1.0]
                ),
            ):
                worker._run_sweep(worker._p)

            csv_files = sorted(Path(temp_dir).glob("*.csv"))
            meta_files = sorted(Path(temp_dir).glob("*.meta.txt"))
            self.assertEqual(len(csv_files), 2)
            self.assertEqual(len(meta_files), 2)
            self.assertIn("C01_red", csv_files[0].name)
            self.assertIn("C02_blue", csv_files[1].name)
            self.assertTrue(all(len(path.read_text().splitlines()) == 3 for path in csv_files))
            self.assertTrue(all("# Status: Complete" in path.read_text() for path in meta_files))
            self.assertTrue(all("# CompletedPoints: 2" in path.read_text() for path in meta_files))
            self.assertEqual(lf6.adapter.centers, [720.0, 750.0])
            self.assertEqual(lf6.adapter.exposures, [30.0, 40.0])
            self.assertEqual(lf6.adapter.frames, [2, 3])
            self.assertEqual(smu.device.zero_ramps, 2)
            self.assertEqual(progress[-1], (4, 4))
            self.assertEqual([item[:2] for item in maps], [(1, 2), (2, 2)])

    def test_stop_preserves_partial_map_and_does_not_start_next_map(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            smu = _FakeSMUController()
            lf6 = _FakeLF6Controller()
            worker = _MegaSweepWorker(_params(Path(temp_dir)), smu, lf6)
            worker.progress.connect(
                lambda done, _total: worker.request_stop() if done == 1 else None
            )

            with patch(
                "ui.megasweep_panel._get_wavelengths",
                return_value=np.array([719.0, 720.0, 721.0]),
            ):
                worker._run_sweep(worker._p)

            csv_files = list(Path(temp_dir).glob("*.csv"))
            meta_files = list(Path(temp_dir).glob("*.meta.txt"))
            self.assertEqual(len(csv_files), 1)
            self.assertEqual(len(csv_files[0].read_text().splitlines()), 2)
            self.assertEqual(len(meta_files), 1)
            metadata = meta_files[0].read_text()
            self.assertIn("# Status: Stopped", metadata)
            self.assertIn("# CompletedPoints: 1", metadata)
            self.assertEqual(lf6.adapter.centers, [720.0])
            self.assertEqual(smu.device.zero_ramps, 1)


if __name__ == "__main__":
    unittest.main()
