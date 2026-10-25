"""UI work must scale with changed data, without touching instruments."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from tests import test_sweep_line_calculator  # optional hardware-library stub
from PySide6.QtWidgets import QApplication
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

    def test_full_sequence_pages_details_and_keeps_manual_page_during_progress(self):
        batch = panel._DEFAULT_BATCH.copy(deep=True)
        batch.loc[:, "frames"] = 1001
        schedule = panel._build_nested_execution_schedule(panel._DEFAULT_LOOP, batch)
        tree = RunPlanTree()
        self.addCleanup(tree.close)
        tree.update_plan([], batch, acquisition_schedule=schedule)
        tree.show_full_sequence()
        child = tree._full_sequence_tree
        self.assertEqual(len(child._flat_steps), 200)
        self.assertFalse(child._page_previous.isEnabled())
        child._page_next.click()
        self.assertEqual(min(child._flat_steps), 200)
        self.assertFalse(child._follow_current)
        tree.update_plan([], batch, acquisition_schedule=schedule,
                         current_seq_i=800, completed_points=800, run_outcome="running")
        self.assertEqual(min(child._flat_steps), 200)
        child._follow_check.setChecked(True)
        self.assertEqual(min(child._flat_steps), 800)
        self.assertTrue(child._flat_steps[800]["item"].text(0).startswith("▶"))
        child._page_spin.setValue(child._page_spin.maximum())
        self.assertIn(len(schedule) - 1, child._flat_steps)
        self.assertFalse(child._page_next.isEnabled())
        self.assertLessEqual(len(child._flat_steps), 200)

    def test_long_flat_preview_is_bounded_and_reuses_plan_on_progress(self):
        row = dict(Run=True, When="", repeat=1, frames=2, condition_label="row",
                   Vbg_start=0, Vbg_stop=1, Vtg_start=0, Vtg_stop=1)
        sequence = [{"Rotation1 Angle (deg)": i} for i in range(2000)]
        batch = [row]
        schedule = [dict(seq_i=i, row_i=0, ctx=ctx, row=row)
                    for i, ctx in enumerate(sequence)]
        tree = RunPlanTree()
        self.addCleanup(tree.close)
        kwargs = dict(acquisition_schedule=schedule, total_acq=len(schedule), run_outcome="running")
        tree.update_plan(sequence, batch, **kwargs)
        self.assertEqual(len(tree._flat_steps), 8)
        with patch.object(tree, "_prepare_flat_schedule", wraps=tree._prepare_flat_schedule) as prepare:
            for i in (0, 1, 500, 1999):
                tree.update_plan(sequence, batch, current_seq_i=i, done=i, **kwargs)
                self.assertLessEqual(len(tree._flat_steps), 8)
                self.assertIn(i, tree._flat_steps)
            self.assertEqual(prepare.call_count, 0)
        self.assertIn("2000 runnable step(s)", tree.topLevelItem(0).text(0))
        tree.show_full_sequence()
        child = tree._full_sequence_tree
        self.assertEqual(len(child._flat_steps), 200)
        self.assertIn(1999, child._flat_steps)
        child._page_spin.setValue(1)
        self.assertEqual(min(child._flat_steps), 0)
        tree.update_plan(sequence, batch, done=2000, run_outcome="completed",
                         acquisition_schedule=schedule, total_acq=2000)
        self.assertTrue(all(record["item"].text(0).startswith("✓") for record in child._flat_steps.values()))

    def test_repeated_frame_progress_does_not_repaint_unchanged_flat_steps(self):
        row = dict(Run=True, repeat=100000, frames=2, condition_label="row")
        sequence, batch = [{}], [row]
        schedule = [dict(seq_i=0, row_i=0, ctx={}, row=row)]
        tree = RunPlanTree()
        self.addCleanup(tree.close)
        kwargs = dict(acquisition_schedule=schedule, total_acq=100000,
                      current_seq_i=0, done=54321, current_rep_i=54321,
                      current_frame_i=1, current_frame_total=2, run_outcome="running")
        tree.update_plan(sequence, batch, **kwargs)
        detail = tree._flat_steps[0]["item"].text(3)
        self.assertIn("54321/100000 saved", detail)
        self.assertLess(len(detail), 400)
        changed = []
        tree.model().dataChanged.connect(lambda *args: changed.append(args))
        tree.update_plan(sequence, batch, **kwargs)
        self.assertEqual(changed, [])

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
