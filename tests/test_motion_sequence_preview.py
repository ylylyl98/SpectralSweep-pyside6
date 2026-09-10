import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ui.motion_sequence_preview import MotionSequencePreviewDialog


class MotionSequencePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def rows():
        return [
            {"sequence": 10, "condition_index": 2, "label": "edge", "D": 1.0,
             "F": 2.0, "Vtg": 1.5, "Vbg": -0.5, "Vbias": 0.1,
             "rot1": 15.0, "rot2": 25.0, "repeat": 2, "point_count": 8,
             "source_token": "second"},
            {"sequence": 11, "condition_index": 1, "label": "center", "D": 3.0,
             "F": 4.0, "Vtg": 3.5, "Vbg": -0.5, "Vbias": 0.0,
             "rot1": 30.0, "rot2": 40.0, "repeat": 1, "point_count": 8,
             "source_token": "first"},
        ]

    def test_default_selection_contains_every_row_and_preserves_fields(self):
        dialog = MotionSequencePreviewDialog(self.rows(), point_count=8)
        selected = dialog.selected_rows()
        self.assertEqual(len(selected), 2)
        self.assertEqual([row["repeat"] for row in selected], [2, 1])
        self.assertEqual(selected[0]["source_token"], "second")
        self.assertEqual(dialog.selected_indices(), [0, 1])

    def test_deselect_and_select_all_update_result(self):
        dialog = MotionSequencePreviewDialog(self.rows())
        dialog.table = dialog._table
        dialog.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        self.assertEqual(len(dialog.selected_sequence()), 1)
        self.assertEqual(dialog.result_indices(), [1])
        dialog.select_all()
        self.assertEqual(dialog.selected_indices(), [0, 1])

    def test_reorder_preserves_source_mapping_and_repeat(self):
        dialog = MotionSequencePreviewDialog(self.rows())
        dialog._table.selectRow(1)
        self.assertTrue(dialog.move_selected(-1))
        ordered = dialog.selected_sequence()
        self.assertEqual([row["source_token"] for row in ordered], ["first", "second"])
        self.assertEqual([row["repeat"] for row in ordered], [1, 2])
        self.assertEqual(dialog.selected_indices(), [1, 0])

    def test_empty_selection_blocks_apply_inline(self):
        dialog = MotionSequencePreviewDialog(self.rows())
        dialog._set_all(False)
        self.assertIsNone(dialog.apply_selection())
        self.assertIn("at least one", dialog._status.text().lower())
        self.assertEqual(dialog.result_rows(), [])

    def test_apply_returns_ordered_custom_sequence_and_accepts(self):
        dialog = MotionSequencePreviewDialog(self.rows())
        dialog._table.selectRow(1)
        dialog.move_selected(-1)
        result = dialog.apply_selection()
        self.assertEqual([row["source_token"] for row in result], ["first", "second"])
        self.assertEqual(dialog.result_indices(), [1, 0])
        self.assertEqual(dialog.result_rows()[0]["repeat"], 1)
        self.assertEqual(dialog.result(), 1)  # QDialog.Accepted


if __name__ == "__main__":
    unittest.main()
