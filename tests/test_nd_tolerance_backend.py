"""Actual stage readback behavior for ND calibration."""

import os
import unittest
import copy

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.nd_calibration_widget import NDCalibrationWorker, install_calibration
from utils.config import cfg


class _Stage:
    minimum_position = 0.0
    maximum_position = 10.0

    def __init__(self, offset=0.0):
        self.offset = float(offset)
        self.position = 0.0
        self.moves = []

    def move_to(self, target):
        self.moves.append(float(target))
        self.position = float(target) + self.offset

    def get_position(self):
        return self.position


class _PM:
    def __init__(self):
        self.reads = 0

    def get_power(self):
        self.reads += 1
        return 1e-6


class NDActualReadbackTests(unittest.TestCase):
    def test_scan_accepts_actual_offset_and_saves_actual_before_power(self):
        stage = _Stage(offset=-0.175781)
        pm = _PM()
        worker = NDCalibrationWorker(stage, pm, [0], settle_s=0, samples=1)
        results, errors = [], []
        worker.finished.connect(results.append)
        worker.error.connect(errors.append)
        worker.run()
        self.assertEqual(errors, [])
        self.assertEqual(pm.reads, 1)
        self.assertAlmostEqual(results[0][0][0], -0.175781)

    def test_reference_accepts_actual_offset(self):
        stage = _Stage(offset=0.0585938)
        pm = _PM()
        worker = NDCalibrationWorker(stage, pm, [0], settle_s=0, samples=1,
                                     reference_only=True)
        results, errors = [], []
        worker.finished.connect(results.append)
        worker.error.connect(errors.append)
        worker.run()
        self.assertEqual(errors, [])
        self.assertAlmostEqual(results[0][0], 0.0585938)

    def test_nonfinite_readback_stops_before_power(self):
        class BadStage(_Stage):
            def get_position(self):
                return float("nan")

        stage = BadStage()
        pm = _PM()
        worker = NDCalibrationWorker(stage, pm, [0], settle_s=0, samples=1)
        results, errors = [], []
        worker.finished.connect(results.append)
        worker.error.connect(errors.append)
        worker.run()
        self.assertEqual(pm.reads, 0)
        self.assertIsNone(results[0])
        self.assertIn("non-finite", errors[0])

    def test_duplicate_actual_scan_cannot_replace_existing_calibration(self):
        class StuckStage(_Stage):
            def get_position(self):
                return 1.0

        old = copy.deepcopy(cfg.nd_calibration)
        try:
            cfg.nd_calibration.positions = [10.0, 20.0]
            cfg.nd_calibration.powers = [100.0, 10.0]
            worker = NDCalibrationWorker(StuckStage(), _PM(), [0, 1], settle_s=0, samples=1)
            results = []
            worker.finished.connect(results.append)
            worker.run()
            with self.assertRaisesRegex(ValueError, "unique"):
                install_calibration(results[0], persist=False)
            self.assertEqual(cfg.nd_calibration.positions, [10.0, 20.0])
            self.assertEqual(cfg.nd_calibration.powers, [100.0, 10.0])
        finally:
            cfg.nd_calibration = old


if __name__ == "__main__":
    unittest.main()
