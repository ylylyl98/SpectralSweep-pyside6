import os
import unittest
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from ui.motion_conditions_widget import MotionConditionsWidget
from utils.mcd_common import parse_numeric_spec

class MotionConditionsWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])
    def test_parser_tuple_and_comma_list(self):
        self.assertEqual(parse_numeric_spec("0, 45, 90", "x"), [0,45,90])
        self.assertEqual(parse_numeric_spec("(0, 1, .5)", "x"), [0,.5,1])
    def test_invalid_disables_add_and_valid_preview(self):
        w=MotionConditionsWidget(); w.a.setText("bad"); self.assertFalse(w._add_button.isEnabled()); w.a.setText("1,2"); w.b.setText("3,4"); self.assertTrue(w._add_button.isEnabled())
    def test_add_appends_and_preserves_zero_row(self):
        w=MotionConditionsWidget(); w.a.setText("0"); w.b.setText("0"); w.add_batch(); before=w.table.rowCount(); w.a.setText("1"); w.add_batch(); self.assertEqual(w.table.rowCount(), before+1); self.assertEqual(w.conditions()[0]["vtg_v"],0)
    def test_edit_voltage_syncs_coordinates_and_state(self):
        w=MotionConditionsWidget(); w.add_batch(); w.table.item(0,4).setText("2"); self.assertAlmostEqual(float(w.table.item(0,2).text()),2); state=w.state(); w2=MotionConditionsWidget(); w2.restore_state(state); self.assertEqual(w2.table.rowCount(),1)
    def test_disabled_rows_and_sequence_repeats(self):
        w=MotionConditionsWidget(); w.a.setText("1,2"); w.b.setText("3,4"); w.add_batch(); w.table.item(0,0).setText("0"); w.repeats.setValue(2); self.assertEqual(len(w.sequence([0,10])),4)

    def test_preview_uses_both_arrays_and_hides_pairing_for_scalar_broadcast(self):
        w = MotionConditionsWidget()
        w.a.setText("[0, 1, 2]"); w.b.setText("5")
        self.assertTrue(w.expansion.isHidden())
        self.assertIn("D=[0, 1, 2]", w._preview_label.text())
        self.assertIn("3 condition(s)", w._preview_label.text())
        w.b.setText("[5, 6, 7]")
        self.assertFalse(w.expansion.isHidden())
        self.assertIn("3 condition(s)", w._preview_label.text())

    def test_gate_ratio_is_visible_with_advanced_collapsed(self):
        w = MotionConditionsWidget()
        self.assertFalse(w.ratio.isHidden())
        self.assertIn(w._ratio_equation_label.text(), w.ratio.toolTip())
        self.assertFalse(w._advanced_body.isVisible())

    def test_checkbox_and_invalid_row_are_authoritative(self):
        w = MotionConditionsWidget(); w.mode.setCurrentIndex(1); w.a.setText("1"); w.b.setText("2"); w.add_batch()
        w.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        self.assertEqual(w.conditions(), [])
        w.table.item(0, 4).setText("bad")
        with self.assertRaisesRegex(ValueError, "Condition row 1"):
            w.conditions()

    def test_unequal_arrays_keep_pairing_visible_for_grid_recovery(self):
        w = MotionConditionsWidget(); w.a.setText("[1, 2]"); w.b.setText("[3, 4, 5]")
        self.assertFalse(w.expansion.isHidden())
        self.assertFalse(w._add_button.isEnabled())
        w.expansion.setCurrentIndex(w.expansion.findData("grid"))
        self.assertTrue(w._add_button.isEnabled())

    def test_label_and_current_ratio_are_returned_and_zero_ratio_error_blocks(self):
        w = MotionConditionsWidget(); w.mode.setCurrentIndex(1); w.a.setText("0"); w.b.setText("0"); w.add_batch()
        w.table.item(0, 1).setText("center")
        w.ratio.setValue(0.0); w.table.item(0, 2).setText("5")
        with self.assertRaisesRegex(ValueError, "Condition row 1"):
            w.conditions()
        w.ratio.setValue(1.0)
        self.assertEqual(w.conditions()[0]["label"], "center")
        self.assertEqual(w.conditions()[0]["gate_ratio"], 1.0)

    def test_structured_provenance_survives_duplicate_reorder_undo_and_state(self):
        w = MotionConditionsWidget(); w.a.setText("(1, 3, 1)"); w.b.setText("[4, 5, 6]"); w.add_batch()
        original = w.table.item(0, 1).data(Qt.ItemDataRole.UserRole)
        w.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        w.table.selectRow(0); w.duplicate()
        self.assertEqual(w.table.item(w.table.rowCount() - 1, 0).checkState(), Qt.CheckState.Unchecked)
        self.assertEqual(w.table.item(w.table.rowCount() - 1, 1).data(Qt.ItemDataRole.UserRole), original)
        w.reorder(-1)
        state = w.state(); restored = MotionConditionsWidget(); restored.restore_state(state)
        self.assertEqual(restored._snapshot(), w._snapshot())
        w.undo_add(); self.assertEqual(w.table.rowCount(), 4)
        w.undo_add(); self.assertEqual(w.table.rowCount(), 3)

    def test_changed_emits_for_table_and_sequence_controls(self):
        w = MotionConditionsWidget(); seen = []; w.changed.connect(lambda: seen.append(True))
        w.add_batch(); baseline = len(seen)
        w.table.item(0, 1).setText("first")
        w.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        w.bias.setValue(1); w.order.setCurrentIndex(1); w.repeats.setValue(2)
        w.rot1_plan.setText("1, 2"); w.rot2_plan.setText("(3, 5, 1)")
        self.assertGreaterEqual(len(seen), baseline + 7)

    def test_independent_rotation_plan_resolution(self):
        w = MotionConditionsWidget(); w.rot1_plan.setText("(0, 2, 1)"); w.rot2_plan.setText("[10, 20]")
        plans = w.rotation_plans({"rot1": 4, "rot2": 8})
        self.assertEqual(plans["rot1"]["values"], [0.0, 1.0, 2.0])
        self.assertEqual(plans["rot2"]["values"], [10.0, 20.0])

    def test_ratio_recalculates_coordinates_without_erasing_generation_provenance(self):
        w = MotionConditionsWidget(); w.a.setText("1"); w.b.setText("3"); w.add_batch()
        provenance = w.table.item(0, 1).data(Qt.ItemDataRole.UserRole)["provenance"]
        w.ratio.setValue(2.0)
        self.assertAlmostEqual(float(w.table.item(0, 2).text()), 0.0)
        self.assertAlmostEqual(float(w.table.item(0, 3).text()), 4.0)
        self.assertEqual(provenance["gate_ratio"], 1.0)
        w.table.item(0, 4).setText("1")
        self.assertTrue(w.table.item(0, 1).data(Qt.ItemDataRole.UserRole)["edited"])

if __name__ == "__main__": unittest.main()
