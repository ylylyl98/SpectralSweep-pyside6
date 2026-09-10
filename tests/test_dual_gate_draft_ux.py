"""Draft/editor/preview regressions. No hardware is connected or started."""
import os
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication
from ui import presets_panel as panel


class DraftUXTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = panel.PresetsPanel()
        self.addCleanup(self.widget.close)
        self.widget._loop_src = panel._DEFAULT_LOOP.copy(deep=True)
        self.widget._batch_src = panel._DEFAULT_BATCH.copy(deep=True)
        self.widget._applied_mode = "Synchronize"
        self.widget._applied_acquisition_grouping = "loop_first"
        self.widget._applied_execution_order = None
        self.widget._on_discard()
        self.widget._update_plan()
        self.widget._refresh_draft_state()

    def cell(self, name):
        return self.widget._batch_table.item(0, panel.BATCH_SCHEMA.index(name))

    def test_invalid_numeric_edit_preserves_applied_plan_and_can_be_discarded(self):
        widget = self.widget
        applied = deepcopy(widget._acquisition_schedule)
        for name, text in (("Vbg_stop", "bad"), ("frames", "inf"), ("repeat", "0")):
            with self.subTest(name=name):
                self.cell(name).setText(text)
                self.assertTrue(widget._tables_dirty)
                self.assertTrue(widget._discard_btn.isEnabled())
                self.assertFalse(widget._apply_btn.isEnabled())
                self.assertFalse(widget._run_btn.isEnabled())
                self.assertIn(name, widget._draft_detail_lbl.text())
                self.assertEqual(self.cell(name).background().color(), QColor("#fde8e7"))
                self.assertEqual(widget._acquisition_schedule, applied)
                self.assertEqual(widget._tree._last_plan["acquisition_schedule"], applied)
                self.assertIn("draft unavailable", widget._tree._last_plan["preview_state"])
                widget._on_apply()  # Direct invocation must also reject invalid drafts.
                self.assertEqual(widget._acquisition_schedule, applied)
                widget._on_discard()
                self.assertFalse(widget._tables_dirty)

    def test_validation_restores_original_cell_style_without_recursive_edits(self):
        widget = self.widget
        item = self.cell("Vbg_stop")
        blocked = widget._batch_table.blockSignals(True)
        item.setBackground(QColor("#ddeeff"))
        item.setToolTip("Original voltage help")
        widget._batch_table.blockSignals(blocked)
        events = []
        widget._batch_table.itemChanged.connect(lambda _: events.append(1))
        item.setText("bad")
        self.assertEqual(len(events), 1)
        item.setText("0.0")
        self.assertEqual(len(events), 2)
        self.assertEqual(item.background().color(), QColor("#ddeeff"))
        self.assertEqual(item.toolTip(), "Original voltage help")
        self.assertFalse(widget._tables_dirty)

    def test_draft_preview_and_full_view_use_actual_values_then_undo_restores_applied(self):
        widget = self.widget
        applied = deepcopy(widget._acquisition_schedule)
        widget._tree.show_full_sequence()
        self.addCleanup(widget._tree._full_sequence_dialog.close)
        self.cell("Vbg_stop").setText("0.25")
        draft = widget._tree._last_plan
        self.assertEqual(float(draft["acquisition_schedule"][0]["row"]["Vbg_stop"]), 0.25)
        self.assertEqual(draft["preview_state"], "Draft sequence")
        self.assertEqual(widget._tree._full_sequence_tree._last_plan["acquisition_schedule"], draft["acquisition_schedule"])
        self.assertEqual(widget._acquisition_schedule, applied)
        widget._undo_batch_edit()
        self.assertFalse(widget._tables_dirty)
        for tree in (widget._tree, widget._tree._full_sequence_tree):
            self.assertEqual(tree._last_plan["acquisition_schedule"], applied)
            self.assertEqual(tree._last_plan["preview_state"], "Applied sequence")

    def test_running_draft_edits_cannot_replace_plan_or_progress(self):
        widget = self.widget
        widget._run_thread = SimpleNamespace(isRunning=lambda: True)
        self.addCleanup(setattr, widget, "_run_thread", None)
        widget._run_outcome = "running"
        widget._on_progress(1, 3)
        shown = widget._tree._last_plan
        applied = deepcopy(widget._acquisition_schedule)
        self.cell("Vbg_stop").setText("0.25")
        self.assertTrue(widget._tables_dirty)
        self.assertFalse(widget._apply_btn.isEnabled())
        self.assertIs(widget._tree._last_plan, shown)
        widget._on_apply()
        widget._update_plan()
        widget._on_discard()
        self.assertEqual(widget._acquisition_schedule, applied)
        self.assertEqual(widget._done_acq, 1)
        self.assertEqual(widget._run_outcome, "running")
        self.assertIs(widget._tree._last_plan, shown)

    def test_preview_is_cached_and_large_draft_is_deferred_without_expansion(self):
        widget = self.widget
        with patch.object(panel, "_build_plan", wraps=panel._build_plan) as build:
            self.cell("Vbg_stop").setText("0.25")
            self.assertEqual(build.call_count, 1)
            widget._refresh_draft_state()
            self.assertEqual(build.call_count, 1)
            self.cell("repeat").setText("100000")
            self.assertEqual(build.call_count, 1)
        self.assertIn("apply to preview large draft", widget._tree._last_plan["preview_state"])
        self.assertTrue(widget._apply_btn.isEnabled())
        self.assertEqual(widget._total_acq, 1)

    def test_order_move_selects_moved_level_and_preview_uses_new_order(self):
        widget = self.widget
        widget.resize(1200, 800)
        widget.show()
        self.app.processEvents()
        widget._move_execution_order(0, 1)
        self.app.processEvents()
        self.assertEqual(widget._execution_order_table.currentRow(), 1)
        self.assertEqual(widget._execution_order_table.item(1, 0).data(Qt.ItemDataRole.UserRole)["kind"], "group")
        self.assertTrue(widget._execution_order_table.visualItemRect(widget._execution_order_table.item(1, 0)).intersects(widget._execution_order_table.viewport().rect()))
        self.assertEqual(widget._draft_change_reasons(), ["Execution order changed"])
        self.assertEqual(widget._tree._last_plan["acquisition_schedule"][0]["execution_order"], widget._execution_order)
        self.assertIsNone(widget._applied_execution_order)

    def test_loop_and_gate_pending_categories_are_independent(self):
        widget = self.widget
        widget._loop_table.item(0, 2).setText("800")
        self.assertEqual(widget._draft_change_reasons(), ["Loop variables changed"])
        self.cell("condition_label").setText("new condition")
        self.assertEqual(widget._draft_change_reasons(), ["Loop variables changed", "Gate conditions changed"])

    def test_loop_validation_marks_values_and_clears_after_correction(self):
        item = self.widget._loop_table.item(0, 2)
        item.setText("bad")
        self.assertIn("Loop row 1", self.widget._draft_detail_lbl.text())
        self.assertEqual(item.background().color(), QColor("#fde8e7"))
        item.setText("860")
        self.assertFalse(self.widget._tables_dirty)
        self.assertIsNone(item.data(Qt.ItemDataRole.BackgroundRole))

    def test_order_preview_splitter_persists_user_allocation(self):
        widget = self.widget
        widget.resize(1400, 1100)
        widget.show()
        self.app.processEvents()
        widget._preview_splitter.setSizes([300, 220])
        sizes = widget._preview_splitter.sizes()
        state = widget.capture_session_state()
        self.assertEqual(state["preview_splitter_sizes"], sizes)
        widget._preview_splitter.setSizes([180, 340])
        widget.restore_session_state(state)
        self.app.processEvents()
        self.assertEqual(widget._preview_splitter.sizes(), sizes)


if __name__ == "__main__":
    unittest.main()
