from __future__ import annotations

import os
import unittest

import pandas as pd

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtWidgets import QApplication, QTreeWidgetItem

from ui.preview_widget import RunPlanTree


def _item_text(item: QTreeWidgetItem) -> str:
    tree = item.treeWidget()
    columns = tree.columnCount() if tree is not None else 4
    return "\n".join(item.text(column) for column in range(columns))


def _tree_texts(item: QTreeWidgetItem):
    values = [_item_text(item)]
    for child_i in range(item.childCount()):
        values.extend(_tree_texts(item.child(child_i)))
    return values


def _find_item(item: QTreeWidgetItem, text: str):
    if text in _item_text(item):
        return item
    for child_i in range(item.childCount()):
        found = _find_item(item.child(child_i), text)
        if found is not None:
            return found
    return None


class DualGateSequencePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _preview_data(self):
        sequence = [
            {"Center Wavelength (nm)": 750.0, "Rotation1 Angle (deg)": 0.0},
            {"Center Wavelength (nm)": 810.0, "Rotation1 Angle (deg)": 45.0},
        ]
        batch = pd.DataFrame(
            [
                {
                    "Run": True,
                    "When": "Center_Wavelength == 810",
                    "MeasurePower": False,
                    "condition_label": "row-one",
                    "repeat": 2,
                    "frames": 201,
                    "Vtg_start": 19.0,
                    "Vtg_stop": -21.0,
                    "Vbg_start": -21.0,
                    "Vbg_stop": 19.0,
                    "Vbias_start": -2.0,
                    "Vbias_stop": -2.0,
                },
                {
                    "Run": True,
                    "When": "",
                    "MeasurePower": True,
                    "condition_label": "row-two",
                    "repeat": 1,
                    "frames": 11,
                    "Vtg_start": -1.0,
                    "Vtg_stop": 1.0,
                    "Vbg_start": 1.0,
                    "Vbg_stop": -1.0,
                    "Vbias_start": "",
                    "Vbias_stop": "",
                },
            ]
        )
        schedule = [
            {"seq_i": 1, "row_i": 0, "ctx": sequence[1], "row": batch.iloc[0].to_dict()},
            {"seq_i": 0, "row_i": 1, "ctx": sequence[0], "row": batch.iloc[1].to_dict()},
            {"seq_i": 1, "row_i": 1, "ctx": sequence[1], "row": batch.iloc[1].to_dict()},
        ]
        loop_definition = pd.DataFrame(
            [
                {
                    "Enable": True,
                    "Parameter": "Center Wavelength (nm)",
                    "Values": "750, 810",
                    "Group": 1,
                },
                {
                    "Enable": True,
                    "Parameter": "Rotation1 Angle (deg)",
                    "Values": "0, 45",
                    "Group": 2,
                },
            ]
        )
        return sequence, batch, schedule, loop_definition

    def _update(self, tree, *, grouping="batch_first", schedule=None, **progress):
        sequence, batch, default_schedule, loop_definition = self._preview_data()
        tree.update_plan(
            sequence,
            batch,
            total_acq=4,
            acquisition_schedule=schedule or default_schedule,
            acquisition_grouping=grouping,
            loop_definition=loop_definition,
            loop_mode="Zip",
            param_order=["Center Wavelength (nm)", "Rotation1 Angle (deg)"],
            **progress,
        )
        return sequence, batch, default_schedule, loop_definition

    def test_batch_first_preview_is_a_flat_complete_checklist(self):
        tree = RunPlanTree()
        tree.resize(480, 300)
        self._update(tree)
        tree.show()
        self.app.processEvents()

        self.assertEqual(tree.columnCount(), 4)
        self.assertEqual(
            [tree.headerItem().text(i) for i in range(4)],
            ["Status", "Batch row", "Loop setting", "Measurement details"],
        )
        self.assertEqual(
            tree.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        root = tree.topLevelItem(0)
        self.assertTrue(root.isExpanded())
        self.assertTrue(tree.isFirstColumnSpanned(0, QModelIndex()))
        self.assertEqual(tree.columnWidth(0), 46)
        self.assertLessEqual(tree.columnWidth(1), 148)
        self.assertEqual(tree.columnWidth(2), 88)
        joined = "\n".join(_tree_texts(root))
        self.assertIn("Batch row → Loop settings · 1 skipped by When", joined)
        self.assertIn("[ROW 1] row-one", joined)
        self.assertIn("[LOOP 2]", joined)
        self.assertIn("CW=810, R1=45", joined)
        self.assertIn("Vtg 19.0000 → -21.0000 V (Δ 0.2000 V)", joined)
        self.assertIn("Vbg -21.0000 → 19.0000 V (Δ 0.2000 V)", joined)
        self.assertIn("Vbias -2.0000 → -2.0000 V", joined)
        self.assertIn("201 frames · Repeat 2 · Power No", joined)
        self.assertIn("Skipped: When (Center_Wavelength == 810) is False", joined)
        self.assertIn("Zip · 2 zipped setting(s)", joined)

        execution = root.child(0)
        first_group = execution.child(0)
        runnable_step = _find_item(first_group, "○ 1")
        self.assertTrue(execution.isExpanded())
        self.assertTrue(first_group.isExpanded())
        self.assertEqual(first_group.background(0).color().name(), "#dbeafe")
        self.assertEqual(runnable_step.background(1).color().name(), "#dbeafe")
        self.assertEqual(runnable_step.background(2).color().name(), "#ede9fe")
        self.assertEqual(runnable_step.toolTip(0), "")
        self.assertTrue(
            runnable_step.text(3).startswith(
                "Vtg 19→-21 V · Vbg -21→19 V · Vbias -2→-2 V"
            )
        )
        self.assertIn("Loop: CW=810, R1=45", runnable_step.text(3))
        self.assertIn("201 frames · Repeat 2 · Power No", runnable_step.text(3))
        self.assertIn("Δ 0.2000 V", runnable_step.toolTip(3))
        self.assertFalse(_find_item(root, "Loop variables").isExpanded())
        self.assertFalse(_find_item(root, "Batch sweep definitions").isExpanded())

    def test_loop_first_changes_grouping_without_hiding_either_context(self):
        sequence, _, default_schedule, _ = self._preview_data()
        loop_first_schedule = [default_schedule[1], default_schedule[0], default_schedule[2]]
        tree = RunPlanTree()
        self._update(tree, grouping="loop_first", schedule=loop_first_schedule)

        root = tree.topLevelItem(0)
        joined = "\n".join(_tree_texts(root))
        self.assertIn("Loop setting → Batch rows", joined)
        self.assertIn("[LOOP 1] CW=750, R1=0", joined)
        self.assertIn("[ROW 2] row-two", joined)
        self.assertIn("Vtg -1.0000 → 1.0000 V", joined)
        self.assertIn("⊘", joined)
        first_group = root.child(0).child(0)
        self.assertEqual(first_group.background(0).color().name(), "#ede9fe")
        step = _find_item(first_group, "[ROW 2]")
        self.assertEqual(step.background(1).color().name(), "#dbeafe")
        self.assertEqual(step.background(2).color().name(), "#ede9fe")

    def test_live_status_updates_in_place_with_repetitions_and_frames(self):
        tree = RunPlanTree()
        self._update(
            tree,
            done=0,
            current_seq_i=0,
            current_rep_i=0,
            current_frame_i=67,
            current_frame_total=201,
            run_outcome="running",
        )
        root = tree.topLevelItem(0)
        step_one = tree._flat_steps[0]["item"]
        self.assertIn("▶ 1  RUNNING", step_one.text(0))
        self.assertIn("Files: ▶1 ○2 · frame 67/201", step_one.text(3))
        self.assertEqual(step_one.background(0).color().name(), "#fef3c7")

        loop_inputs = _find_item(root, "Loop variables")
        loop_inputs.setExpanded(True)
        self._update(
            tree,
            done=2,
            current_seq_i=1,
            current_rep_i=0,
            current_frame_i=1,
            current_frame_total=11,
            run_outcome="running",
        )
        self.assertIs(step_one, tree._flat_steps[0]["item"])
        self.assertEqual(step_one.text(0), "✓ 1")
        self.assertIn("Files: ✓1 ✓2", step_one.text(3))
        self.assertEqual(step_one.background(0).color().name(), "#dcfce7")
        self.assertTrue(loop_inputs.isExpanded())
        self.assertIn("▶ 2  RUNNING", tree._flat_steps[1]["item"].text(0))

        self._update(tree, done=4, current_seq_i=2, run_outcome="completed")
        self.assertTrue(
            all(record["item"].text(0).startswith("✓") for record in tree._flat_steps.values())
        )
        self.assertIn("Complete · 4/4 files", tree.topLevelItem(0).text(0))

    def test_cartesian_summary_reports_resolved_product(self):
        batch = pd.DataFrame(
            [
                {
                    "Run": True,
                    "When": "",
                    "condition_label": "row",
                    "repeat": 1,
                    "frames": 2,
                    "Vtg_start": 0,
                    "Vtg_stop": 1,
                    "Vbg_start": 0,
                    "Vbg_stop": 1,
                    "Vbias_start": "",
                    "Vbias_stop": "",
                }
            ]
        )
        sequence = [
            {"Rotation1 Angle (deg)": rot1, "Rotation2 Angle (deg)": rot2}
            for rot1 in (0, 45)
            for rot2 in (0, 30)
        ]
        schedule = [
            {"seq_i": i, "row_i": 0, "ctx": ctx, "row": batch.iloc[0].to_dict()}
            for i, ctx in enumerate(sequence)
        ]
        loop_definition = pd.DataFrame(
            [
                {"Enable": True, "Parameter": "Rotation1 Angle (deg)", "Values": "0, 45", "Group": 1},
                {"Enable": True, "Parameter": "Rotation2 Angle (deg)", "Values": "0, 30", "Group": 2},
            ]
        )
        tree = RunPlanTree()
        tree.update_plan(
            sequence,
            batch,
            total_acq=4,
            acquisition_schedule=schedule,
            acquisition_grouping="batch_first",
            loop_definition=loop_definition,
            loop_mode="Synchronize",
            param_order=["Rotation1 Angle (deg)", "Rotation2 Angle (deg)"],
        )
        joined = "\n".join(_tree_texts(tree.topLevelItem(0)))
        self.assertIn("Cartesian · 2 × 2 = 4 setting(s)", joined)

    def test_current_step_can_be_marked_stopped_or_failed(self):
        tree = RunPlanTree()
        self._update(tree, done=0, current_seq_i=0, run_outcome="stopped")
        self.assertIn("■ 1  STOPPED", tree._flat_steps[0]["item"].text(0))

        self._update(tree, done=0, current_seq_i=0, run_outcome="failed")
        self.assertIn("✕ 1  FAILED", tree._flat_steps[0]["item"].text(0))


if __name__ == "__main__":
    unittest.main()
