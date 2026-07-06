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
        state = panel.capture_session_state()

        restored = PresetsPanel()
        restored.restore_session_state(state)
        self.assertEqual(restored._sample_edit.text(), "device-a")
        self.assertEqual(restored._loop_table.item(0, 2).text(), "899")
        self.assertEqual(str(restored._loop_src.iloc[0]["Values"]), "861")

        restored._refresh_tables()
        self.assertEqual(restored._loop_table.item(0, 2).text(), "861")

    def test_workflow_panels_restore_representative_setup(self):
        power = PowerSweepPanel()
        power._pos_input.setText("(2, 8, 4)")
        power._center_spin.setValue(731.2)
        power._apply_gates_chk.setChecked(False)
        power._devid_edit.setText("power-device")
        power_state = power.capture_session_state()
        power_restored = PowerSweepPanel()
        power_restored.restore_session_state(power_state)
        self.assertEqual(power_restored._pos_input.text(), "(2, 8, 4)")
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

    def test_settings_and_spectrum_preferences_restore(self):
        settings = SettingsPanel()
        settings._base_out_edit.setText("D:/new-output")
        settings._exposure.setValue(1234.0)
        state = settings.capture_session_state()
        restored_settings = SettingsPanel()
        restored_settings.restore_session_state(state)
        self.assertEqual(restored_settings._base_out_edit.text(), "D:/new-output")
        self.assertEqual(restored_settings._exposure.value(), 1234.0)

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


if __name__ == "__main__":
    unittest.main()
