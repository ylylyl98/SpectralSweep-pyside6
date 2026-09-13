"""Regression coverage for nested schedule review repairs; no live hardware."""
import csv
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QCheckBox
from ui import presets_panel as panel
from ui.preview_widget import RunPlanTree
from tests import test_smu_resilience as fixtures


def loops(*parameters):
    return pd.DataFrame([
        dict(Enable=True, Parameter=parameter, Values=values, Group=index + 1)
        for index, (parameter, values) in enumerate(parameters)
    ])


def batch(**overrides):
    return pd.DataFrame([fixtures.SMUResilienceTests._batch_row(**overrides)])


class NestedReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def panel(self):
        widget = panel.PresetsPanel()
        self.addCleanup(widget.close)
        return widget

    def test_disable_outer_group_preserves_inner_membership_and_label(self):
        definition = loops(("Stage Position", "1,2"), ("Rotation1 Angle (deg)", "0,90"))
        order = panel._normalize_execution_order(["group:1", "conditions", "points", "group:2"], definition)
        definition.loc[0, "Enable"] = False
        updated = panel._normalize_execution_order(order, definition)
        self.assertEqual([item["kind"] for item in updated], ["conditions", "points", "group"])
        self.assertEqual(updated[-1]["parameters"], ["Rotation1 Angle (deg)"])
        self.assertIn("RotIn", updated[-1]["label"])

    def test_regrouping_requires_explicit_reset(self):
        definition = loops(("Stage Position", "1,2"), ("Rotation1 Angle (deg)", "0,90"))
        order = panel._normalize_execution_order(["group:1", "conditions", "points", "group:2"], definition)
        with self.assertRaisesRegex(ValueError, "Reset execution order"):
            panel._normalize_execution_order(order, definition, "Zip")

    def test_session_keeps_applied_and_draft_orders_separate(self):
        widget = self.panel()
        definition = loops(("Center Wavelength (nm)", "750,810"))
        state = widget.capture_session_state()
        applied = panel._normalize_execution_order(["group:1", "conditions", "points"], definition)
        draft = panel._normalize_execution_order(["conditions", "points", "group:1"], definition)
        state.update(applied_loop=definition.to_dict("records"), draft_loop=definition.to_dict("records"),
                     applied_execution_order=applied, execution_order=draft, nested_schedule_enabled=True)
        widget.restore_session_state(state)
        self.assertEqual(widget._applied_execution_order, applied)
        self.assertEqual(widget._acquisition_schedule[0]["execution_order"], applied)
        self.assertTrue(widget._draft_is_different())
        # Changing settling must not apply the draft order as a side effect.
        widget._update_plan()
        self.assertEqual(widget._acquisition_schedule[0]["execution_order"], applied)
        saved = widget.capture_session_state()
        self.assertEqual(saved["applied_execution_order"], applied)
        self.assertEqual(saved["execution_order"], draft)
        widget._on_discard()
        self.assertEqual(widget._execution_order, applied)
        self.assertFalse(widget._draft_is_different())

    def test_legacy_batch_first_order_is_visible(self):
        widget = self.panel()
        state = widget.capture_session_state()
        state.update(acquisition_grouping="batch_first", applied_acquisition_grouping="batch_first")
        state.pop("execution_order", None)
        state.pop("applied_execution_order", None)
        widget.restore_session_state(state)
        self.assertEqual(widget._execution_order_entries()[0]["kind"], "conditions")
        self.assertIn("Gate conditions", widget._execution_order_table.item(0, 0).text())
        self.assertFalse(any(task.get("nested") for task in widget._acquisition_schedule))

    def test_loop_checkbox_refreshes_order_immediately(self):
        widget = self.panel()
        for index in range(widget._loop_table.rowCount()):
            check = widget._loop_table.cellWidget(index, 0).findChild(QCheckBox)
            check.setChecked(False)
        self.assertEqual(widget._execution_order_table.rowCount(), 2)
        self.assertIn("Gate conditions", widget._execution_order_table.item(0, 0).text())

    def test_run_footer_stays_visible_when_details_expand(self):
        widget = self.panel()
        widget.resize(1400, 900)
        widget.show()
        widget._filename_details_toggle.setChecked(True)
        widget._log_toggle.setChecked(True)
        self.app.processEvents()
        for button in (widget._run_btn, widget._stop_btn, widget._spectrum_btn):
            self.assertFalse(widget._results_scroll.isAncestorOf(button))
            top_left = button.mapTo(widget, QPoint(0, 0))
            self.assertGreaterEqual(top_left.y(), 0)
            self.assertLessEqual(top_left.y() + button.height(), widget.height())
            self.assertTrue(button.isVisible())

    def test_nested_preview_uses_points_not_file_counter(self):
        definition = loops(("Rotation1 Angle (deg)", "0,90"))
        rows = batch(frames=3, repeat=2, Vbg_stop=2)
        schedule = panel._build_nested_execution_schedule(definition, rows, execution_order=["conditions", "points", "group:1"])
        tree = RunPlanTree()
        tree._show_full_sequence = True
        self.addCleanup(tree.close)
        kwargs = dict(acquisition_schedule=schedule, total_acq=4, done=0,
                      current_seq_i=4, completed_points=4, run_outcome="running")
        tree.update_plan([], rows, **kwargs)
        self.assertEqual(list(tree._flat_steps), list(range(12)))
        self.assertTrue(all(tree._flat_steps[index]["item"].text(0).startswith("\u2713") for index in range(4)))
        self.assertEqual(sum(record["item"].text(0).startswith("\u25b6") for record in tree._flat_steps.values()), 1)
        # True hierarchy: condition -> point -> angle -> acquisition.
        item = tree._flat_steps[0]["item"]
        self.assertIn("RotIn=0", item.parent().text(0))
        self.assertIn("Gate point 1/3", item.parent().parent().text(0))
        self.assertIn("repetition 1/2", item.parent().parent().parent().text(0))
        kwargs.update(done=4, completed_points=12, run_outcome="completed")
        tree.update_plan([], rows, **kwargs)
        self.assertTrue(all(record["item"].text(0).startswith("\u2713") for record in tree._flat_steps.values()))

    def run_fake(self, definition, rows, order, *, drift=False, power=1e-6, strict_failure=False):
        schedule = panel._build_nested_execution_schedule(definition, rows, execution_order=order)
        sequence, rows, _ = panel._build_plan(definition, rows)
        events, finished, contexts = [], [], []
        device = fixtures._HealthyRunDevice()
        device.set_gates = lambda **values: events.append(("gate", values))
        device.set_operation_context = lambda **values: contexts.append(values)
        lf = fixtures._FakeLFController()
        lf.configure_for_acquisition = lambda **settings: events.append(("configure", settings))
        acquired = 0
        def acquire():
            nonlocal acquired
            acquired += 1
            events.append(("acquire", acquired))
            return np.array([750., 751.]) + (acquired - 1 if drift else 0), np.array([1., 2.])
        lf.adapter.acquire = acquire
        def strict():
            raise RuntimeError("readback failed")
        axis = SimpleNamespace(move_to=lambda value: None, get_position=lambda: 0.)
        if strict_failure:
            axis.get_position_strict = strict
        metadata = fixtures.SMUResilienceTests._run_meta()
        metadata.update(initial_voltage_settle_s=0., voltage_settle_s=0., power_correction_factor=1.,
                        spectrometer_defaults={"Center Wavelength (nm)": 700., "Exposure Time (ms)": 5., "Accumulations (EPF)": 2})
        with tempfile.TemporaryDirectory() as folder:
            worker = panel._RunWorker(sequence, rows, lf6_ctrl=lf,
                smu_ctrl=SimpleNamespace(is_connected=True, device=device),
                rotation_ctrl=SimpleNamespace(is_connected=lambda slot: True, adapter=lambda slot: axis),
                pm_ctrl=SimpleNamespace(is_connected=True, adapter=SimpleNamespace(get_power=lambda: power)),
                out_dir=Path(folder), run_meta=metadata, filename_parts=["device_id"],
                stop_event=threading.Event(), acquisition_schedule=schedule)
            worker.finished.connect(lambda *args: finished.append(args))
            with patch.object(panel.cfg.lf6, "center_nm", 999.), patch.object(panel.cfg.ramp, "delay_s", 0.):
                worker.run()
            contents = []
            for path in (Path(folder) / "PL").glob("*.csv"):
                with path.open(newline="", encoding="utf-8") as stream:
                    contents.append(list(csv.DictReader(stream)))
        return events, finished, contexts, contents

    def test_disabled_rows_do_not_change_nested_task_lookup(self):
        rows = pd.concat([
            batch(Run=False, condition_label="disabled", frames=1),
            batch(condition_label="enabled", frames=2, Vbg_stop=1),
        ], ignore_index=True)
        events, finished, contexts, contents = self.run_fake(loops(), rows, ["conditions", "points"])
        self.assertTrue(finished[-1][0], finished)
        self.assertEqual(len([event for event in events if event[0] == "acquire"]), 2)
        self.assertEqual([len(rows) for rows in contents], [2])
        self.assertTrue(all(context["condition"] == "enabled" for context in contexts))

    def test_filename_defaults_match_captured_settings(self):
        metadata = fixtures.SMUResilienceTests._run_meta()
        metadata["spectrometer_defaults"] = {
            "Center Wavelength (nm)": 700., "Exposure Time (ms)": 5., "Accumulations (EPF)": 2,
        }
        with patch.object(panel.cfg.lf6, "center_nm", 999.):
            context = panel._filename_context_from_row(metadata, {}, {})
        self.assertEqual((context.center_nm, context.exposure_ms, context.accumulations), (700., 5., 2))

    def test_spectrometer_parameters_follow_independent_levels(self):
        events, finished, _, _ = self.run_fake(
            loops(("Exposure Time (ms)", "10"), ("Center Wavelength (nm)", "800,810")),
            batch(frames=1), ["group:1", "conditions", "points", "group:2"])
        self.assertTrue(finished[-1][0], finished)
        first_gate = next(index for index, event in enumerate(events) if event[0] == "gate")
        configs = [(index, event[1]) for index, event in enumerate(events) if event[0] == "configure"]
        self.assertLess(configs[0][0], first_gate)
        self.assertEqual(configs[0][1], dict(center_nm=700., exposure_ms=10., frames=2))
        self.assertGreater(configs[1][0], first_gate)
        self.assertEqual(configs[1][1]["center_nm"], 800.)

    def test_gate_steps_use_direct_moves_and_metadata_indices_are_bounded(self):
        events, finished, contexts, _ = self.run_fake(loops(), batch(frames=3, repeat=2, Vbg_stop=2), ["conditions", "points"])
        self.assertTrue(finished[-1][0], finished)
        jumps = [event[1]["ramp_step"] for event in events if event[0] == "gate"]
        self.assertGreater(jumps[0], 0.)
        self.assertEqual(jumps[1:3], [0., 0.])
        self.assertGreater(jumps[3], 0.)
        self.assertTrue(all(context["sequence"] <= context["sequence_total"] for context in contexts))

    def test_nonfinite_requested_power_fails_before_acquisition(self):
        events, finished, _, contents = self.run_fake(loops(), batch(MeasurePower=True), ["conditions", "points"], power=float("nan"))
        self.assertFalse(finished[-1][0])
        self.assertIn("Power read failed", finished[-1][1])
        self.assertFalse(any(event[0] == "acquire" for event in events))
        self.assertEqual(contents, [])

    def test_strict_motion_failure_prevents_acquisition(self):
        events, finished, _, contents = self.run_fake(loops(("Rotation1 Angle (deg)", "0")), batch(),
            ["conditions", "points", "group:1"], strict_failure=True)
        self.assertFalse(finished[-1][0])
        self.assertIn("readback failed", finished[-1][1])
        self.assertFalse(any(event[0] == "acquire" for event in events))
        self.assertEqual(contents, [])

    def test_wavelength_drift_does_not_write_mislabelled_second_row(self):
        _, finished, _, contents = self.run_fake(loops(), batch(frames=2, Vbg_stop=1), ["conditions", "points"], drift=True)
        self.assertFalse(finished[-1][0])
        self.assertIn("Wavelength calibration changed", finished[-1][1])
        self.assertEqual([len(rows) for rows in contents], [1])


if __name__ == "__main__":
    unittest.main()
