from __future__ import annotations

import csv
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QLineEdit

from ui.main_window import _SharedSampleIdBinder
from ui.power_sweep_panel import PowerSweepPanel, _PowerSweepWorker


class _FakeMotionAdapter:
    minimum_position = 0.0
    maximum_position = 50.0
    position_unit = "mm"

    def __init__(self, position: float = 7.0):
        self.position = position
        self.moves = []

    def move_to(self, value: float):
        self.position = float(value)
        self.moves.append(float(value))

    def get_position(self) -> float:
        return self.position


class _FakeStageController(QObject):
    connected = Signal(str)
    disconnected = Signal()

    def __init__(self, connected: bool = True):
        super().__init__()
        self.is_connected = connected
        self.adapter = _FakeMotionAdapter()


class _FakeRotationController(QObject):
    connected = Signal(str, str)
    disconnected = Signal(str)

    def __init__(self):
        super().__init__()
        self.adapters = {
            "rot1": _FakeMotionAdapter(position=12.0),
            "rot2": _FakeMotionAdapter(position=24.0),
        }

    def is_connected(self, slot: str) -> bool:
        return slot in self.adapters

    def adapter(self, slot: str):
        return self.adapters.get(slot)


class _FakeSpectrum:
    def calibration_wavelengths(self, force=False):
        return np.array([700.0, 701.0, 702.0])

    def acquire(self):
        return (
            np.array([700.0, 701.0, 702.0]),
            np.array([1.0, 2.0, 3.0]),
        )


class _FakeLF6Controller(QObject):
    connected = Signal(list)
    disconnected = Signal()

    def __init__(self):
        super().__init__()
        self.is_connected = True
        self.adapter = _FakeSpectrum()
        self.setup = None


def _worker_params(out_path: str, motion_key: str) -> dict:
    return {
        "positions": np.array([1.0, 2.0]),
        "motion_key": motion_key,
        "motion_settle_s": 0.0,
        "return_motion_to_start": True,
        "center_nm": 730.0,
        "exp_ms": 10.0,
        "frames": 1,
        "pm_wl_nm": 730.0,
        "out_path": out_path,
        "base_name": f"motion_{motion_key}",
        "apply_gates": False,
        "return_to_zero": False,
    }


class MotionSweepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_validation_allows_missing_optional_power_meter(self):
        panel = PowerSweepPanel(
            lf6_ctrl=_FakeLF6Controller(),
            stage_ctrl=_FakeStageController(),
            rotation_ctrl=_FakeRotationController(),
            pm_ctrl=None,
            smu_ctrl=None,
        )
        panel._apply_gates_chk.setChecked(False)

        self.assertTrue(panel._validate())
        self.assertTrue(panel._pm_notice_lbl.isVisibleTo(panel))

    def test_rot1_sweep_without_pm_omits_power_and_restores_position(self):
        stage = _FakeStageController()
        rotation = _FakeRotationController()
        lf6 = _FakeLF6Controller()
        initial = rotation.adapter("rot1").get_position()

        with tempfile.TemporaryDirectory() as tmp:
            worker = _PowerSweepWorker(
                _worker_params(tmp, "rot1"),
                stage,
                rotation,
                None,
                lf6,
                None,
            )
            worker._run_sweep(worker._p)

            output = Path(tmp) / "motion_rot1.csv"
            with output.open("r", encoding="utf-8", newline="") as fh:
                rows = list(csv.reader(fh))

        self.assertNotIn("Power_uW", rows[0])
        self.assertIn("rot1_deg", rows[0])
        self.assertIn("rot1_deg_actual", rows[0])
        self.assertEqual(rotation.adapter("rot1").moves, [1.0, 2.0, initial])
        self.assertEqual(stage.adapter.moves, [])

    def test_shared_sample_id_binder_uses_latest_edit(self):
        edits = [QLineEdit("old-a"), QLineEdit("old-b"), QLineEdit()]
        binder = _SharedSampleIdBinder(edits, initial="initial")
        self.assertEqual([edit.text() for edit in edits], ["initial"] * 3)

        edits[1].setText("latest-sample")

        self.assertEqual(binder.value, "latest-sample")
        self.assertEqual(
            [edit.text() for edit in edits],
            ["latest-sample"] * 3,
        )


if __name__ == "__main__":
    unittest.main()
