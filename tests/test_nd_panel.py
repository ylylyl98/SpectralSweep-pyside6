import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from app.nd_calibration import positions_for_power, predict_power
from ui.power_sweep_panel import PowerSweepPanel
from ui.instrument_panel import InstrumentPanel, _PM100DSection
from utils.config import cfg


class TestNDPanel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._old_nd = (list(cfg.nd_calibration.positions), list(cfg.nd_calibration.powers), cfg.nd_calibration.wavelength_nm)

    def tearDown(self):
        cfg.nd_calibration.positions, cfg.nd_calibration.powers, cfg.nd_calibration.wavelength_nm = self._old_nd

    def test_relative_prediction_and_inverse(self):
        x = np.array([0.0, 1.0, 2.0])
        y = np.array([1.0, 10.0, 100.0])
        self.assertTrue(np.allclose(predict_power(x, x, y), y))
        self.assertTrue(np.allclose(positions_for_power([1, 10, 100], x, y), x))

    def test_invalid_power_input_clears_positions(self):
        panel = PowerSweepPanel()
        panel._input_mode.setCurrentIndex(1)
        panel._power_range_edit.setText("not a power list")
        panel._update_position_preview()
        self.assertEqual(panel._positions.size, 0)
        self.assertIsNone(panel._target_powers)

    def test_freeze_keeps_target_provenance(self):
        panel = PowerSweepPanel()
        old = (list(cfg.nd_calibration.positions), list(cfg.nd_calibration.powers))
        try:
            cfg.nd_calibration.positions = [0.0, 1.0, 2.0]
            cfg.nd_calibration.powers = [100.0, 10.0, 1.0]
            panel._cal_widget._last_reference = (1.0, 10.0)
            panel._input_mode.setCurrentIndex(1)
            panel._power_start_spin.setValue(1.0)
            panel._power_end_spin.setValue(10.0)
            panel._power_count_spin.setValue(2)
            panel._update_position_preview()
            expected = panel._target_powers.copy()
            panel._freeze_generated_positions()
            self.assertEqual(panel._target_powers.tolist(), expected.tolist())
            self.assertEqual(panel._input_mode.currentData(), "position")
        finally:
            cfg.nd_calibration.positions, cfg.nd_calibration.powers = old

    def _calibrated_panel(self):
        cfg.nd_calibration.positions = [0.0, 1.0, 2.0]
        cfg.nd_calibration.powers = [100.0, 10.0, 1.0]
        panel = PowerSweepPanel()
        panel._cal_widget._last_reference = (1.0, 20.0)
        return panel

    def test_plot_updates_planned_points_when_targets_and_reference_change(self):
        panel = self._calibrated_panel()
        panel._pos_input.setText("[0, 1, 2]")
        panel._update_position_preview()
        first = panel._cal_planned_points.getData()[1].copy()
        panel._cal_widget._last_reference = (1.0, 40.0)
        panel._update_position_preview()
        second = panel._cal_planned_points.getData()[1]
        self.assertFalse(np.allclose(first, second))
        self.assertEqual(len(second), 3)

    def test_target_power_mode_requires_session_reference(self):
        panel = self._calibrated_panel()
        panel._cal_widget._last_reference = (None, None)
        panel._input_mode.setCurrentIndex(1)
        panel._power_start_spin.setValue(2.0)
        panel._power_end_spin.setValue(20.0)
        panel._power_count_spin.setValue(2)
        panel._update_position_preview()
        self.assertEqual(panel._positions.size, 0)
        self.assertIn("reference", panel._pos_preview_lbl.text().lower())

    def test_freeze_survives_calibration_refresh_but_invalid_edit_clears_plan(self):
        panel = self._calibrated_panel()
        panel._input_mode.setCurrentIndex(1)
        panel._power_start_spin.setValue(2.0)
        panel._power_end_spin.setValue(20.0)
        panel._power_count_spin.setValue(2)
        panel._update_position_preview()
        targets = panel._target_powers.copy()
        panel._freeze_generated_positions()
        cfg.nd_calibration.powers = [200.0, 20.0, 2.0]
        panel._update_position_preview()
        np.testing.assert_allclose(panel._target_powers, targets)
        panel._pos_input.setText("not valid")
        panel._update_position_preview()
        self.assertEqual(panel._positions.size, 0)
        self.assertIsNone(panel._target_powers)

    def test_freeze_records_generation_calibration_snapshot(self):
        panel = self._calibrated_panel()
        panel._input_mode.setCurrentIndex(1)
        panel._power_start_spin.setValue(2.0)
        panel._power_end_spin.setValue(20.0)
        panel._power_count_spin.setValue(2)
        panel._update_position_preview()
        panel._freeze_generated_positions()
        self.assertEqual(panel._freeze_source["generation_calibration"]["powers"], [100.0, 10.0, 1.0])

    def test_metadata_failure_releases_busy_state(self):
        panel = PowerSweepPanel()
        panel._devid_edit.setText("sample")
        panel._apply_gates_chk.setChecked(False)
        panel._positions = np.array([0.0])
        states = []
        panel.busy_changed.connect(states.append)
        with patch.object(panel, "_validate", return_value=True), patch("ui.power_sweep_panel.ExperimentMetadataService.begin", side_effect=OSError("metadata")), patch("ui.power_sweep_panel.QMessageBox.critical"):
            panel._on_run()
        self.assertEqual(states, [True, False])
        self.assertFalse(panel._sweep_busy)
        self.assertIsNone(panel._thread)

    def test_shutdown_is_idempotent_without_workers(self):
        panel = PowerSweepPanel()
        self.assertTrue(panel.shutdown(10))
        self.assertTrue(panel.shutdown(10))

    def test_pm_polling_is_suspended_during_shared_busy(self):
        class Controller(QObject):
            devices_scanned = Signal(list)
            connected = Signal(object)
            disconnected = Signal()
            error = Signal(str)
            def scan_devices(self): pass
            def disconnect_instrument(self): pass
        section = _PM100DSection(Controller())
        section._poll_timer.start(1)
        section.set_external_busy(True)
        section._do_read()
        self.assertTrue(section._external_busy)
        self.assertFalse(section._poll_timer.isActive())
        self.assertIsNone(section._pm_thread)

    def test_instrument_panel_forwards_shared_busy_to_pm_section(self):
        class Controller(QObject):
            devices_scanned = Signal(list)
            connected = Signal(object)
            disconnected = Signal()
            error = Signal(str)
            def scan_devices(self): pass
            def disconnect_instrument(self): pass
        panel = InstrumentPanel(pm_ctrl=Controller())
        section = panel._sections["pm100d"]
        panel.set_external_busy(True)
        self.assertTrue(section._external_busy)
        panel.set_external_busy(False)
        self.assertFalse(section._external_busy)

    def test_calibration_busy_emits_single_aggregate_lock_transition(self):
        panel = PowerSweepPanel()
        states = []
        panel.busy_changed.connect(states.append)
        panel._on_calibration_busy(True)
        panel._on_calibration_busy(True)
        panel._on_calibration_busy(False)
        self.assertEqual(states, [True, False])

    def test_calibration_and_sweep_wavelengths_start_in_sync(self):
        cfg.nd_calibration.wavelength_nm = 700.0
        panel = PowerSweepPanel()
        self.assertEqual(panel._pm_wl_spin.value(), 700.0)
        self.assertEqual(panel._cal_widget.wavelength_spin.value(), 700.0)
        panel._pm_wl_spin.setValue(711.0)
        self.assertEqual(panel._cal_widget.wavelength_spin.value(), 711.0)


if __name__ == "__main__":
    unittest.main()
