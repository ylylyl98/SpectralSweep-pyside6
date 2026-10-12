"""UI work must scale with changed data, without touching instruments."""
import os
import unittest
import time
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from tests import test_sweep_line_calculator  # optional hardware-library stub
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from ui import presets_panel as panel
from ui.preview_widget import RunPlanTree
from ui import megasweep_panel as mega


class UIPerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def widget(self):
        widget = panel.PresetsPanel()
        self.addCleanup(widget.close)
        return widget

    def test_nested_vectors_are_resolved_once_per_enabled_row(self):
        batch = panel._DEFAULT_BATCH.copy(deep=True)
        batch.loc[:, "frames"] = 101
        with patch.object(panel, "_resolve_sweep_vectors", wraps=panel._resolve_sweep_vectors) as resolve:
            schedule = panel._build_nested_execution_schedule(panel._DEFAULT_LOOP, batch)
        self.assertGreater(len(schedule), 100)
        self.assertLessEqual(resolve.call_count, int(batch["Run"].sum()))
        with patch.object(panel, "_resolve_sweep_vectors", wraps=panel._resolve_sweep_vectors) as resolve:
            panel._validate_safe_jumps([], batch, 100, schedule)
        self.assertLessEqual(resolve.call_count, int(batch["Run"].sum()))

    def test_settle_and_metadata_edits_reuse_plan_but_safety_change_revalidates(self):
        widget = self.widget()
        schedule = widget._acquisition_schedule
        with patch.object(panel, "_build_plan", wraps=panel._build_plan) as build:
            widget._voltage_settle_spin.setValue(widget._voltage_settle_spin.value() + .1)
        self.assertEqual(build.call_count, 0)
        self.assertIs(widget._acquisition_schedule, schedule)
        widget._refresh_readiness()
        with patch.object(panel, "_validate_safe_jumps", wraps=panel._validate_safe_jumps) as validate:
            widget._point_edit.setText("cache-test")
            widget._refresh_readiness()
            self.assertEqual(validate.call_count, 0)
            widget._safe_jump_spin.setValue(widget._safe_jump_spin.value() + .1)
            self.assertGreater(validate.call_count, 0)

    def test_filename_preview_reuses_visible_draft_schedule(self):
        widget = self.widget()
        widget._nested_schedule_enabled = True
        widget._execution_order = panel._normalize_execution_order(None, widget._loop_src)
        widget._batch_table.item(0, panel.BATCH_SCHEMA.index("Vbg_stop")).setText("1.5")
        with patch.object(panel, "_build_nested_execution_schedule", wraps=panel._build_nested_execution_schedule) as build:
            widget._refresh_filename_preview()
            widget._refresh_filename_preview()
        self.assertEqual(build.call_count, 0)

    def test_progress_does_not_serialize_schedule_or_refresh_hidden_dialog(self):
        class NoReprDict(dict):
            def __repr__(self):
                raise AssertionError("Progress serialized the complete schedule")
        schedule = [NoReprDict(task) for task in panel._build_nested_execution_schedule(panel._DEFAULT_LOOP, panel._DEFAULT_BATCH)]
        tree = RunPlanTree()
        self.addCleanup(tree.close)
        kwargs = dict(acquisition_schedule=schedule, total_acq=1, run_outcome="running")
        tree.update_plan([], panel._DEFAULT_BATCH, **kwargs)
        tree.show_full_sequence()
        tree._full_sequence_dialog.hide()
        with patch.object(tree._full_sequence_tree, "update_plan") as update:
            tree.update_plan([], panel._DEFAULT_BATCH, current_seq_i=1, completed_points=1, **kwargs)
        self.assertEqual(update.call_count, 0)
        tree.show_full_sequence()
        self.assertEqual(tree._full_sequence_tree._last_plan["completed_points"], 1)

    def test_mega_metadata_and_timing_reuse_geometry_but_axis_changes_rebuild(self):
        widget = mega.MegaSweepPanel()
        self.addCleanup(widget.close)
        widget._refresh_preview()
        with patch.object(mega, "build_sweep_points", wraps=mega.build_sweep_points) as build, patch.object(widget._preview, "update_plan", wraps=widget._preview.update_plan) as draw:
            widget._sample_edit.setText("geometry-cache")
            widget._refresh_preview()
            widget._exp_spin.setValue(widget._exp_spin.value() + 1)
            widget._refresh_preview()
            self.assertEqual(build.call_count, 0)
            self.assertEqual(draw.call_count, 0)
            widget._axis_a._stop.setValue(widget._axis_a._stop.value() + 1)
            widget._refresh_preview()
            self.assertEqual(build.call_count, 1)
            self.assertEqual(draw.call_count, 1)

    def test_full_sequence_populates_in_chunks_and_keeps_qt_events_running(self):
        batch = panel._DEFAULT_BATCH.copy(deep=True)
        batch.loc[:, "frames"] = 1001
        schedule = panel._build_nested_execution_schedule(panel._DEFAULT_LOOP, batch)
        tree = RunPlanTree()
        self.addCleanup(tree.close)
        tree.update_plan([], batch, acquisition_schedule=schedule)
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start(5)
        self.addCleanup(timer.stop)
        tree.show_full_sequence()
        child = tree._full_sequence_tree
        self.assertLess(len(child._flat_steps), len(schedule))
        deadline = time.perf_counter() + 5
        while len(child._flat_steps) < len(schedule) and time.perf_counter() < deadline:
            QTest.qWait(10)
        self.assertEqual(len(child._flat_steps), len(schedule))
        self.assertGreater(len(ticks), 2)

    def test_clearing_nested_tree_restores_unscheduled_preview(self):
        tree = RunPlanTree()
        self.addCleanup(tree.close)
        schedule = panel._build_nested_execution_schedule(panel._DEFAULT_LOOP, panel._DEFAULT_BATCH)
        tree.update_plan([], panel._DEFAULT_BATCH, acquisition_schedule=schedule)
        tree.clear_plan()
        tree.update_plan([{}], panel._DEFAULT_BATCH)
        self.assertGreater(tree.topLevelItemCount(), 0)


if __name__ == "__main__":
    unittest.main()
