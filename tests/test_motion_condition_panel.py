import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog

from ui.power_sweep_panel import PowerSweepPanel


class MotionConditionPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def panel(self):
        panel = PowerSweepPanel()
        panel.show()
        self.app.processEvents()
        self.addCleanup(panel.close)
        self.addCleanup(panel._parse_timer.stop)
        return panel

    def _conditions(self, panel):
        panel._conditions_grp.setChecked(True)
        panel._condition_editor.setEnabled(True)
        panel._condition_editor.mode.setCurrentIndex(1)
        panel._condition_editor.a.setText("1, 2")
        panel._condition_editor.b.setText("0")
        panel._condition_editor.add_batch()
        panel._pos_input.setText("0, 1")
        panel._update_position_preview()

    def test_summary_is_one_compact_line_and_tracks_position_count(self):
        panel = self.panel()
        self._conditions(panel)
        self.assertIn("2 sweeps × 2 points = 4 spectra", panel._condition_preview_lbl.text())
        self.assertEqual(panel._condition_preview_lbl.text().count("\n"), 0)
        panel._pos_input.setText("0")
        panel._update_position_preview()
        self.assertIn("2 sweeps × 1 points = 2 spectra", panel._condition_preview_lbl.text())

    def test_inner_axis_excludes_outer_plan_and_restores_it_on_switch(self):
        panel = self.panel()
        self._conditions(panel)
        editor = panel._condition_editor
        editor.rot1_plan.setText("[10, 20]")
        editor.rot2_plan.setText("[30, 40]")
        for axis, other in (("rot1", "rot2"), ("rot2", "rot1")):
            panel._motion_buttons[axis].click()
            plans = editor.rotation_plans()
            self.assertEqual(plans[axis]["values"], [])
            self.assertEqual(len(plans[other]["values"]), 2)
            self.assertFalse(editor._selected_rotation_edit(axis).isEnabled())
            self.assertTrue(editor._selected_rotation_edit(other).isEnabled())
        panel._motion_buttons["stage"].click()
        self.assertEqual(editor.rotation_plans()["rot1"]["values"], [10.0, 20.0])
        self.assertEqual(editor.rotation_plans()["rot2"]["values"], [30.0, 40.0])

    def test_preview_handler_reorders_actual_dialog_rows_and_state_roundtrips(self):
        panel = self.panel()
        self._conditions(panel)
        captured = {}

        class FakeDialog:
            Accepted = QDialog.DialogCode.Accepted

            def __init__(self, rows, **kwargs):
                captured.setdefault("calls", []).append(rows)
                self.rows = rows

            def exec(self):
                return QDialog.DialogCode.Accepted

            def result_rows(self):
                # Choose only the second planned source on the first open;
                # subsequent opens return the dialog's checked rows.
                if len(captured["calls"]) == 1:
                    return [self.rows[1]]
                return [row for row in self.rows if row.get("enabled", True)]

        with patch("ui.power_sweep_panel.MotionSequencePreviewDialog", FakeDialog):
            panel._show_condition_preview()
            self.assertEqual([row["condition_index"] for row in panel._custom_condition_sequence], [1])
            panel._show_condition_preview()
        reopened = captured["calls"][1]
        self.assertEqual([row["_source_index"] for row in reopened], [1, 0])
        self.assertEqual([row["enabled"] for row in reopened], [True, False])
        self.assertEqual(reopened[0]["condition_index"], 2)
        self.assertIn("custom selection", panel._condition_preview_lbl.text())
        state = panel.capture_session_state()
        restored = self.panel()
        restored.restore_session_state(state)
        self.assertEqual([row["condition_index"] for row in restored._custom_condition_sequence], [1])

    def test_invalid_stage_values_show_zero_sequence_count(self):
        panel = self.panel()
        panel._conditions_grp.setChecked(True)
        panel._condition_editor.setEnabled(True)
        panel._condition_editor.add_row()
        panel._pos_input.setText("not a stage value")
        panel._update_position_preview()
        self.assertIn("0 sweeps × 0 points = 0 spectra", panel._condition_preview_lbl.text())
        self.assertEqual(panel._positions.size, 0)

    def test_power_input_labels_follow_visibility_without_duplicate_condition_inputs(self):
        panel = self.panel()
        self.assertFalse(hasattr(panel, "_condition_a_edit"))
        self.assertFalse(hasattr(panel, "_condition_b_edit"))
        panel._input_mode.setCurrentIndex(panel._input_mode.findData("power"))
        panel._power_kind.setCurrentIndex(panel._power_kind.findData("list"))
        panel._update_power_input_visibility()
        self.assertTrue(panel._power_range_edit.isVisible())
        self.assertTrue(panel._motion_form.labelForField(panel._power_range_edit).isVisible())
        panel._input_mode.setCurrentIndex(panel._input_mode.findData("position"))
        panel._update_power_input_visibility()
        self.assertFalse(panel._power_range_edit.isVisible())
        self.assertFalse(panel._motion_form.labelForField(panel._power_range_edit).isVisible())

    def test_simple_mode_is_preserved_and_acquisition_locks_condition_editor(self):
        panel = self.panel()
        panel._conditions_grp.setChecked(True)
        panel._condition_editor.setEnabled(True)
        panel._sweep_busy = True
        panel._conditions_ui_was_enabled = panel._conditions_grp.isEnabled()
        panel._conditions_grp.setEnabled(False)
        self.assertFalse(panel._condition_editor.isEnabled())
        panel._sweep_busy = False
        panel._conditions_grp.setEnabled(True)
        panel._condition_editor.setEnabled(True)
        panel._conditions_grp.setChecked(False)
        state = panel.capture_session_state()
        restored = self.panel()
        restored.restore_session_state(state)
        self.assertFalse(restored._conditions_grp.isChecked())
        self.assertEqual(restored._condition_preview_lbl.text(), "Disabled")


if __name__ == "__main__":
    unittest.main()
