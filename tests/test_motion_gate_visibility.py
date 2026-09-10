import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Keep this UI test from reading or writing the user's real configuration.
_CONFIG_DIR = tempfile.mkdtemp(prefix="spectralsweep-gate-ui-")
os.environ["SPECTRALSWEEP_CONFIG_PATH"] = str(Path(_CONFIG_DIR) / "config.json")

from PySide6.QtWidgets import QApplication

from tests.test_motion_sweep import (
    _FakeLF6Controller, _FakeRotationController, _FakeStageController,
)
from ui.power_sweep_panel import PowerSweepPanel
from utils.config import cfg


class MotionGateVisibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_across_conditions_hides_legacy_voltage_targets_only(self):
        panel = PowerSweepPanel(
            lf6_ctrl=_FakeLF6Controller(),
            stage_ctrl=_FakeStageController(),
            rotation_ctrl=_FakeRotationController(),
        )
        self.assertFalse(hasattr(panel, "_ramp_step_spin"))
        self.assertFalse(hasattr(panel, "_settle_spin"))
        self.assertEqual([field.isHidden() for field in panel._gate_voltage_spins], [False, False, False])

        panel._conditions_grp.setChecked(True)
        self.app.processEvents()
        self.assertEqual([field.isHidden() for field in panel._gate_voltage_spins], [True, True, True])
        self.assertEqual(
            [field.isHidden() for field in (
                panel._motion_settle_spin,
                panel._apply_gates_chk, panel._return_zero_chk,
            )],
            [False, False, False],
        )
        panel._conditions_grp.setChecked(False)
        self.app.processEvents()
        self.assertEqual([field.isHidden() for field in panel._gate_voltage_spins], [False, False, False])

    def test_run_defaults_freeze_shared_ramp_values_and_return_zero_is_independent(self):
        with patch.object(cfg.ramp, "step_V", 0.23), patch.object(cfg.ramp, "delay_s", 0.17), \
             patch.object(cfg.ramp, "settle_s", 0.41), patch.object(cfg.ramp, "vbias_step_V", 0.07):
            defaults = PowerSweepPanel._frozen_ramp_defaults()
        self.assertEqual(defaults, {
            "ramp_step_V": 0.23, "step_delay_s": 0.17,
            "settle_s": 0.41, "vbias_step_V": 0.07,
        })
        panel = PowerSweepPanel(lf6_ctrl=_FakeLF6Controller(), stage_ctrl=_FakeStageController(),
                                rotation_ctrl=_FakeRotationController())
        self.assertTrue(panel._return_zero_chk.isChecked())


if __name__ == "__main__":
    unittest.main()
