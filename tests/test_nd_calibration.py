import os
import unittest
import copy
import time
import tempfile
import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.nd_calibration import make_power_points, positions_for_power, predict_power, save_calibration
from ui.nd_calibration_widget import NDCalibrationWorker, NDCalibrationWidget, install_calibration
from utils.config import AppConfig, cfg
import utils.config as config_module


class FakeStage:
    minimum_position = 0.0
    maximum_position = 10.0

    def __init__(self, offset=0.0, backend_key="elliptec", position_unit="stage units"):
        self.offset = offset
        self.backend_key = backend_key
        self.position_unit = position_unit
        self.position = 0.0
        self.moves = []

    def move_to(self, position):
        self.moves.append(float(position))
        self.position = float(position) + self.offset

    def get_position(self):
        return self.position


class FakePowerMeter:
    def __init__(self, readings=(1e-6,)):
        self.readings = list(readings)
        self.wavelength = None
        self.index = 0

    def configure_wavelength(self, wavelength):
        self.wavelength = float(wavelength)

    def get_power(self):
        value = self.readings[min(self.index, len(self.readings) - 1)]
        self.index += 1
        return value


class FakeController:
    def __init__(self, adapter):
        self.adapter = adapter
        self.is_connected = True


class NDCalibrationTests(unittest.TestCase):
    def setUp(self):
        self._calibration = copy.deepcopy(cfg.nd_calibration)
        self._factor = cfg.pm100d.correction_factor
        self._config_directory = tempfile.TemporaryDirectory()
        self._original_config_file = config_module._CONFIG_FILE
        config_module._CONFIG_FILE = os.path.join(self._config_directory.name, "config.json")

    def tearDown(self):
        cfg.nd_calibration = self._calibration
        cfg.pm100d.correction_factor = self._factor
        config_module._CONFIG_FILE = self._original_config_file
        self._config_directory.cleanup()

    def test_reference_scales_relative_curve_and_inverse_matches(self):
        positions = [0, 10, 20]
        powers = [100, 10, 1]
        predicted = predict_power([0, 10, 20], positions, powers,
                                  reference_position=0, reference_power=200)
        np.testing.assert_allclose(predicted, [200, 20, 2])
        np.testing.assert_allclose(
            positions_for_power([2, 20, 200], positions, powers,
                                reference_position=0, reference_power=200),
            [20, 10, 0],
        )

    def test_inverse_accepts_reference_scaled_calibration_endpoints(self):
        # Floating point reference scaling can put the upper endpoint a few
        # ulps outside the interpolated curve.  That is still an exact target.
        np.testing.assert_allclose(
            positions_for_power([2, 20], [0, 10], [2, 20],
                                 reference_position=0, reference_power=2),
            [0, 10],
        )

    def test_log_points_and_non_monotonic_calibration_rejected(self):
        np.testing.assert_allclose(make_power_points(1, 100, 3, "log"), [1, 10, 100])
        with self.assertRaises(ValueError):
            predict_power(1, [0, 1, 2], [1, 2, 1])

    def test_plateaus_and_nan_reference_are_rejected(self):
        with self.assertRaises(ValueError):
            predict_power(1, [0, 1, 2], [1, 2, 2])
        with self.assertRaises(ValueError):
            predict_power(1, [0, 1, 2], [1, 2, 4], reference_position=np.nan, reference_power=1)

    def test_save_validates_raw_and_factor_before_mutating_config(self):
        cfg.nd_calibration.profile_name = "before"
        with self.assertRaises(ValueError):
            save_calibration([0, 1, 2], [1, 2, 4], raw_powers=[1, np.nan, 1], persist=False)
        self.assertEqual(cfg.nd_calibration.profile_name, "before")
        with self.assertRaises(ValueError):
            install_calibration([(0, 1, 1), (1, 2, 1)], correction_factor=0, persist=False)
        self.assertEqual(cfg.nd_calibration.profile_name, "before")

    def test_save_rolls_back_when_persistence_fails(self):
        cfg.nd_calibration.profile_name = "before"
        original_positions = list(cfg.nd_calibration.positions)
        original_save = cfg.save
        cfg.save = lambda: (_ for _ in ()).throw(OSError("disk full"))
        try:
            with self.assertRaises(OSError):
                save_calibration([0, 1], [1, 2], profile_name="after", persist=True)
        finally:
            cfg.save = original_save
        self.assertEqual(cfg.nd_calibration.profile_name, "before")
        self.assertEqual(cfg.nd_calibration.positions, original_positions)

    def test_worker_configures_wavelength_and_stores_actual_readback(self):
        stage = FakeStage(offset=0.0005)
        pm = FakePowerMeter([1e-6, 2e-6])
        worker = NDCalibrationWorker(stage, pm, [1, 2], settle_s=0, samples=1,
                                     correction_factor=2, wavelength_nm=700)
        results, errors = [], []
        worker.finished.connect(results.append)
        worker.error.connect(errors.append)
        worker.run()
        self.assertFalse(errors)
        self.assertEqual(pm.wavelength, 700)
        self.assertEqual([point[0] for point in results[0]], [1.0005, 2.0005])
        self.assertEqual(results[0][0][1:], (2.0, 1.0))

    def test_worker_checks_range_before_motion_and_rejects_invalid_reading(self):
        stage = FakeStage()
        pm = FakePowerMeter([0.0])
        worker = NDCalibrationWorker(stage, pm, [-1, 2], settle_s=0, samples=1)
        results, errors = [], []
        worker.finished.connect(results.append); worker.error.connect(errors.append)
        worker.run()
        self.assertEqual(stage.moves, [])
        self.assertIsNone(results[0])
        self.assertTrue(errors)

        stage = FakeStage()
        worker = NDCalibrationWorker(stage, pm, [1], settle_s=0, samples=1)
        results, errors = [], []
        worker.finished.connect(results.append); worker.error.connect(errors.append)
        worker.run()
        self.assertIsNone(results[0])
        self.assertIn("non-finite or non-positive", errors[0])

    def test_cancelled_worker_returns_no_partial_curve(self):
        stage = FakeStage()
        pm = FakePowerMeter([1e-6])
        worker = NDCalibrationWorker(stage, pm, [1, 2], settle_s=0, samples=1)
        worker.request_stop()
        results, errors = [], []
        worker.finished.connect(results.append); worker.error.connect(errors.append)
        worker.run()
        self.assertEqual(results, [None])
        self.assertEqual(errors, [])
        self.assertEqual(stage.moves, [])

    def test_widget_owns_busy_until_thread_finished(self):
        app = QApplication.instance() or QApplication([])
        cfg.pm100d.correction_factor = 1.0
        stage = FakeStage()
        pm = FakePowerMeter([2e-6, 1e-6])
        widget = NDCalibrationWidget(FakeController(stage), FakeController(pm))
        widget.positions_edit.setText("[0, 1]")
        widget.settle_spin.setValue(0)
        widget.averages_spin.setValue(1)
        busy = []
        widget.busy_changed.connect(busy.append)
        widget.start_scan()
        deadline = time.monotonic() + 2.0
        while widget._thread is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()
        self.assertIsNone(widget._thread)
        self.assertIsNone(widget._worker)
        self.assertEqual(busy, [True, False])
        self.assertTrue(widget.scan_button.isEnabled())
        widget.shutdown(100)


if __name__ == "__main__":
    unittest.main()
