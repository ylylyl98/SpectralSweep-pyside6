from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from ui.bfp_panel_integrated import BFPPanel
from ui.instrument_panel import InstrumentPanel
from ui.megasweep_panel import CoordSystem, MegaSweepPanel
from ui.power_sweep_panel import PowerSweepPanel
from ui.presets_panel import PresetsPanel
from ui.settings_panel import SettingsPanel
from ui.spectrum_panel import SpectrumPanel
from utils.config import cfg


class _FakeLF6Controller(QObject):
    connected = Signal(list)
    disconnected = Signal()
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self.connect_calls = 0
        self.disconnect_calls = 0

    def connect_instrument(self, **_kwargs):
        self.connect_calls += 1

    def disconnect_instrument(self):
        self.disconnect_calls += 1


class SessionStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dual_gate_restores_draft_and_last_applied_tables_separately(self):
        panel = PresetsPanel()
        panel._loop_table.item(0, 2).setText("861")
        panel._on_apply()
        panel._loop_table.item(0, 2).setText("899")
        panel._sample_edit.setText("device-a")
        panel._initial_voltage_settle_spin.setValue(600.0)
        panel._voltage_settle_spin.setValue(120.0)
        state = panel.capture_session_state()

        restored = PresetsPanel()
        restored.restore_session_state(state)
        self.assertEqual(restored._sample_edit.text(), "device-a")
        self.assertAlmostEqual(restored._initial_voltage_settle_spin.value(), 600.0)
        self.assertAlmostEqual(restored._voltage_settle_spin.value(), 120.0)
        self.assertEqual(restored._loop_table.item(0, 2).text(), "899")
        self.assertEqual(str(restored._loop_src.iloc[0]["Values"]), "861")

        restored._refresh_tables()
        self.assertEqual(restored._loop_table.item(0, 2).text(), "861")

    def test_dual_gate_legacy_settle_restores_to_both_timing_controls(self):
        panel = PresetsPanel()
        state = panel.capture_session_state()
        state.pop("initial_voltage_settle_s")
        state["voltage_settle_s"] = 3.5

        restored = PresetsPanel()
        restored.restore_session_state(state)

        self.assertAlmostEqual(restored._initial_voltage_settle_spin.value(), 3.5)
        self.assertAlmostEqual(restored._voltage_settle_spin.value(), 3.5)

    def test_dual_gate_keeps_unapplied_raw_table_drafts(self):
        panel = PresetsPanel()
        panel._loop_table.item(0, 2).setText("")
        panel._loop_table.item(0, 3).setText("partial")
        state = panel.capture_session_state()

        restored = PresetsPanel()
        restored.restore_session_state(state)

        self.assertEqual(restored._loop_table.item(0, 2).text(), "")
        self.assertEqual(restored._loop_table.item(0, 3).text(), "partial")

    def test_dual_gate_restores_drift_minimized_acquisition_order(self):
        panel = PresetsPanel()
        panel._acquisition_group_combo.setCurrentIndex(
            panel._acquisition_group_combo.findData("batch_first")
        )
        panel._on_apply()

        state = panel.capture_session_state()
        restored = PresetsPanel()
        restored.restore_session_state(state)

        self.assertEqual(
            restored._acquisition_group_combo.currentData(), "batch_first"
        )
        self.assertEqual(restored._applied_acquisition_grouping, "batch_first")

    def test_workflow_panels_restore_representative_setup(self):
        power = PowerSweepPanel()
        power._pos_input.setText("(2, 8, 4)")
        power._motion_combo.setCurrentIndex(
            power._motion_combo.findData("rot2")
        )
        power._motion_settle_spin.setValue(0.75)
        power._center_spin.setValue(731.2)
        power._apply_gates_chk.setChecked(False)
        power._devid_edit.setText("power-device")
        power_state = power.capture_session_state()
        power_restored = PowerSweepPanel()
        power_restored.restore_session_state(power_state)
        self.assertEqual(power_restored._pos_input.text(), "(2, 8, 4)")
        self.assertEqual(power_restored._motion_combo.currentData(), "rot2")
        self.assertAlmostEqual(
            power_restored._motion_settle_spin.value(), 0.75
        )
        self.assertAlmostEqual(power_restored._center_spin.value(), 731.2)
        self.assertFalse(power_restored._apply_gates_chk.isChecked())
        self.assertEqual(power_restored._devid_edit.text(), "power-device")

        mega = MegaSweepPanel()
        mega._coord_widget._physical.setChecked(True)
        mega._coord_widget._ratio_spin.setValue(0.75)
        mega._axis_a._start.setValue(-2.5)
        mega._timing_widget._settle.setValue(0.8)
        mega._sample_edit.setText("mega-device")
        mega_state = mega.capture_session_state()
        mega_restored = MegaSweepPanel()
        mega_restored.restore_session_state(mega_state)
        self.assertEqual(
            mega_restored._coord_widget.coord_system(),
            CoordSystem.PHYSICAL,
        )
        self.assertAlmostEqual(mega_restored._coord_widget.ratio(), 0.75)
        self.assertAlmostEqual(mega_restored._axis_a._start.value(), -2.5)
        self.assertAlmostEqual(mega_restored._timing_widget.settle(), 0.8)
        self.assertEqual(mega_restored._sample_edit.text(), "mega-device")
        mega._optical_widget._table.item(0, 1).setText("   ")
        mega_state = mega.capture_session_state()
        mega_restored.restore_session_state(mega_state)
        self.assertEqual(mega_restored._optical_widget._table.item(0, 1).text(), "   ")

    def test_bfp_state_is_per_workflow_and_display_preferences_restore(self):
        original_default = cfg.lf6.center_nm
        panel = BFPPanel()
        panel._center_spin.setValue(original_default + 11)
        panel._roi_combo.setCurrentText("Bin all")
        panel._warmup_chk.setChecked(False)
        panel._display._cmap_combo.setCurrentText("plasma")
        state = panel.capture_session_state()
        self.assertEqual(cfg.lf6.center_nm, original_default)

        restored = BFPPanel()
        restored.restore_session_state(state)
        self.assertAlmostEqual(
            restored._center_spin.value(),
            original_default + 11,
        )
        self.assertEqual(restored._roi_combo.currentText(), "Bin all")
        self.assertFalse(restored._warmup_chk.isChecked())
        self.assertEqual(restored._display._cmap_combo.currentText(), "plasma")

    def test_bfp_empty_background_profile_clears_cached_data(self):
        panel = BFPPanel()
        panel._bg_panel._path_edit.setText("")
        panel._bg_panel._bg_data = object()
        panel._bg_panel._bg_wls = [1.0]
        state = panel.capture_session_state()
        panel._bg_panel._path_edit.setText("other-sample.csv")
        panel._bg_panel._bg_data = object()
        panel.restore_session_state(state)
        self.assertEqual(panel._bg_panel._path_edit.text(), "")
        self.assertIsNone(panel._bg_panel._bg_data)
        self.assertEqual(len(panel._bg_panel._bg_wls), 0)

    def test_settings_and_spectrum_preferences_restore(self):
        settings = SettingsPanel()
        settings._base_out_edit.setText("D:/new-output")
        settings._exposure.setValue(1234.0)
        settings._andor_grating.setValue(7)
        settings._andor_si_fan.setCurrentText("low")
        state = settings.capture_session_state()
        restored_settings = SettingsPanel()
        restored_settings.restore_session_state(state)
        self.assertEqual(restored_settings._base_out_edit.text(), "D:/new-output")
        self.assertEqual(restored_settings._exposure.value(), 1234.0)
        self.assertEqual(restored_settings._andor_grating.value(), 7)
        self.assertEqual(restored_settings._andor_si_fan.currentText(), "low")

        spectrum = SpectrumPanel()
        spectrum._spec_plot._autoscale_chk.setChecked(False)
        spectrum._frame_plot._cmap_combo.setCurrentText("magma")
        spectrum._tabs.setCurrentIndex(1)
        spectrum_state = spectrum.capture_session_state()
        restored_spectrum = SpectrumPanel()
        restored_spectrum.restore_session_state(spectrum_state)
        self.assertFalse(restored_spectrum._spec_plot._autoscale_chk.isChecked())
        self.assertEqual(
            restored_spectrum._frame_plot._cmap_combo.currentText(),
            "magma",
        )
        self.assertEqual(restored_spectrum._tabs.currentIndex(), 1)

        spectrum._andor_controls.grating.addItem("test grating", 19)
        spectrum._andor_controls.grating.setCurrentText("test grating")
        spectrum._andor_controls.slit.setValue(123.0)
        spectrum._andor_controls.read_mode.setCurrentText("2D image")
        spectrum._andor_controls.roi_hstart.setValue(11)
        spectrum._andor_controls.hbin.setValue(4)
        andor_state = spectrum.capture_session_state()
        spectrum._andor_controls.slit.setValue(456.0)
        spectrum._andor_controls.restore_session_state(andor_state["andor"])
        self.assertAlmostEqual(spectrum._andor_controls.slit.value(), 123.0)
        self.assertEqual(spectrum._andor_controls.grating.currentData(), 19)
        self.assertEqual(spectrum._andor_controls.read_mode.currentData(), "image")
        self.assertEqual(spectrum._andor_controls.roi_hstart.value(), 11)
        self.assertEqual(spectrum._andor_controls.hbin.value(), 4)
        restarted = SpectrumPanel()
        restarted.restore_session_state(andor_state)
        self.assertEqual(restarted._andor_controls.grating.currentData(), 19)
        self.assertAlmostEqual(restarted._andor_controls.slit.value(), 123.0)

    def test_instrument_restore_never_connects_or_disconnects(self):
        controller = _FakeLF6Controller()
        panel = InstrumentPanel(lf6_ctrl=controller)
        panel._sections["lf6"]._mock_chk.setChecked(False)
        state = panel.capture_session_state()

        restored_controller = _FakeLF6Controller()
        restored = InstrumentPanel(lf6_ctrl=restored_controller)
        restored.restore_session_state(state)
        self.assertFalse(restored._sections["lf6"]._mock_chk.isChecked())
        self.assertEqual(restored_controller.connect_calls, 0)
        self.assertEqual(restored_controller.disconnect_calls, 0)

    def test_instrument_sidebar_presets_restore_without_hardware_side_effects(self):
        controller = _FakeLF6Controller()
        panel = InstrumentPanel(lf6_ctrl=controller)
        lf6 = panel._sections["lf6"]
        lf6._andor_grating.setValue(9)
        lf6._andor_slit.setValue(321.0)
        lf6._andor_read_mode.setCurrentText("2D full sensor")
        lf6._andor_hbin.setValue(3)
        state = panel.capture_session_state()
        restored_controller = _FakeLF6Controller()
        restored = InstrumentPanel(lf6_ctrl=restored_controller)
        restored.restore_session_state(state)
        restored_lf6 = restored._sections["lf6"]
        self.assertEqual(restored_lf6._andor_grating.value(), 9)
        self.assertAlmostEqual(restored_lf6._andor_slit.value(), 321.0)
        self.assertEqual(restored_lf6._andor_read_mode.currentData(), "image")
        self.assertEqual(restored_lf6._andor_hbin.value(), 3)
        self.assertEqual(restored_controller.connect_calls, 0)
        self.assertEqual(restored_controller.disconnect_calls, 0)


if __name__ == "__main__":
    unittest.main()
