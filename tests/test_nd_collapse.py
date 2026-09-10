"""Compact Shared ND calibration section behavior."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui.power_sweep_panel import PowerSweepPanel


class NDCollapseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_shared_calibration_body_collapses_and_axis_changes_preserve_state(self):
        panel = PowerSweepPanel()
        try:
            self.assertFalse(panel._cal_grp.isChecked())
            self.assertFalse(panel._cal_grp.isHidden())
            self.assertTrue(panel._cal_widget.isHidden())

            panel._cal_grp.setChecked(True)
            self.assertFalse(panel._cal_widget.isHidden())
            panel._cal_grp.setChecked(False)
            self.assertTrue(panel._cal_widget.isHidden())

            # Switching motion axes must not expand calibration implicitly.
            rotation_index = panel._motion_combo.findData("rot1")
            if rotation_index >= 0:
                panel._motion_combo.setCurrentIndex(rotation_index)
                panel._motion_combo.setCurrentIndex(panel._motion_combo.findData("stage"))
            self.assertFalse(panel._cal_grp.isChecked())
            self.assertTrue(panel._cal_widget.isHidden())

            # An active calibration always keeps its stop controls reachable.
            panel._on_calibration_busy(True)
            panel._cal_grp.setChecked(False)
            self.assertTrue(panel._cal_grp.isChecked())
            self.assertFalse(panel._cal_widget.isHidden())
        finally:
            panel._calibration_busy = False
            panel.deleteLater()


if __name__ == "__main__":
    unittest.main()
