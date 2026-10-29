"""Long editable sequences reuse derived data without weakening validation."""
import os
from unittest.mock import patch

import pandas as pd
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from ui import presets_panel as panel


@pytest.fixture
def widget():
    app = QApplication.instance() or QApplication([])
    instance = panel.PresetsPanel(bind_controller=False, initialize_plan=False)
    yield instance
    instance._preview_timer.stop()
    instance.close()


def definition(values="1:1:100000", parameter="Exposure Time (ms)"):
    return panel._normalize_loop(pd.DataFrame([
        dict(Enable=True, Parameter=parameter, Values=values, Group=1),
    ]))


def test_numeric_cache_does_not_share_mutable_values_or_parameter_rules():
    values = panel._parse_values("1,2,3", "Exposure Time (ms)")
    values[0] = -99
    assert panel._parse_values("1,2,3", "Exposure Time (ms)") == [1, 2, 3]
    assert panel._parse_values("-1,2", "Rotation1 Angle (deg)") == [-1, 2]
    with pytest.raises(ValueError, match="positive"):
        panel._parse_values("-1,2", "Exposure Time (ms)")
    with pytest.raises(ValueError, match="integers"):
        panel._parse_values("1,2.5", "Accumulations (EPF)")
    with pytest.raises(ValueError, match="finite"):
        panel._parse_values("nan", "Rotation1 Angle (deg)")


def test_group_cache_is_immutable_and_invalidates_values_parameters_and_selection(widget):
    frame = definition("1,2,3")
    first = widget._draft_loop_groups(frame)
    assert widget._draft_loop_groups(frame.copy()) is first
    with pytest.raises(TypeError):
        first[0]["contexts"][0] = 9
    with pytest.raises(TypeError):
        first[0]["label"] = "modified"
    frame.loc[0, "Values"] = "1,2,4"
    changed = widget._draft_loop_groups(frame)
    assert changed is not first
    assert len(changed[0]["contexts"]) == 3
    frame.loc[0, "Parameter"] = "Rotation1 Angle (deg)"
    renamed = widget._draft_loop_groups(frame)
    assert renamed[0]["parameters"] == ("Rotation1 Angle (deg)",)
    frame.attrs["loop_run_positions"] = {1: [1]}
    selected = widget._draft_loop_groups(frame)
    assert len(selected[0]["contexts"]) == 1
    assert selected[0]["positions"] == (1,)
    frame.attrs["loop_run_positions"] = {1: "reselect"}
    with pytest.raises(ValueError, match="select values again"):
        widget._draft_loop_groups(frame)


def test_group_cache_invalidates_mode_enable_and_stage_profile(widget):
    frame = definition("1,2")
    first = widget._draft_loop_groups(frame)
    widget._mode_combo.blockSignals(True)
    widget._mode_combo.setCurrentText("Zip")
    widget._mode_combo.blockSignals(False)
    assert widget._draft_loop_groups(frame) is not first
    frame.loc[0, "Enable"] = False
    assert widget._draft_loop_groups(frame) == ()
    stage = definition("60", "Stage Position")
    with patch.object(panel, "_active_stage_profile", return_value=panel.get_linear_stage_profile("elliptec")):
        assert widget._draft_loop_groups(stage)
    with patch.object(panel, "_active_stage_profile", return_value=panel.get_linear_stage_profile("esp300")):
        with pytest.raises(ValueError, match="50"):
            widget._draft_loop_groups(stage)


def test_long_order_refresh_builds_groups_once_and_keeps_gate_constraints(widget):
    widget._loop_table.blockSignals(True)
    panel._populate_loop_table(widget._loop_table, definition())
    widget._loop_table.blockSignals(False)
    widget._draft_loop_groups_cache = None
    with patch.object(panel, "_loop_group_combinations", wraps=panel._loop_group_combinations) as build:
        widget._refresh_execution_order_table()
        widget._refresh_execution_order_table()
    assert build.call_count == 1
    assert widget._execution_order_table.rowCount() == 3
    frame = definition("1,2")
    groups = widget._draft_loop_groups(frame)
    expected = panel._normalize_execution_order(["conditions", "group:1", "points"], frame)
    assert panel._normalize_execution_order(["conditions", "group:1", "points"], frame, groups=groups) == expected
    # Mutating an output must not poison the cached group membership.
    expected[1]["parameters"].append("unknown")
    assert groups[0]["parameters"] == ("Exposure Time (ms)",)
    with pytest.raises(ValueError, match="inside Gate conditions"):
        panel._normalize_execution_order(["points", "conditions", "group:1"], frame, groups=groups)


def test_cached_groups_do_not_skip_changed_batch_budget(widget):
    widget._loop_table.blockSignals(True)
    panel._populate_loop_table(widget._loop_table, definition())
    widget._loop_table.blockSignals(False)
    widget._draft_loop_groups()
    widget._batch_table.blockSignals(True)
    widget._batch_table.item(0, panel.BATCH_SCHEMA.index("frames")).setText("2")
    widget._batch_table.blockSignals(False)
    assert any("limited" in issue for issue in widget._draft_validation_issues())


def test_lightweight_metadata_and_preview_calls_do_not_copy_execution_plan(widget):
    class NoIteration(list):
        def __iter__(self):
            raise AssertionError("Presentation copied the complete plan")

    widget._final_seq = NoIteration([{}])
    widget._acquisition_schedule = NoIteration([{}])
    assert "executed_plan" not in widget._current_run_meta(include_executed_plan=False)
    widget._final_seq = [{}]
    widget._acquisition_schedule = []
    assert widget._current_run_meta()["executed_plan"]["sequence"] == [{}]
    with patch.object(widget, "_current_run_meta", wraps=widget._current_run_meta) as metadata:
        widget._readiness_issues()
        widget._refresh_filename_preview()
    assert metadata.call_count == 2
    assert all(call.kwargs == {"include_executed_plan": False} for call in metadata.call_args_list)


def test_pending_apply_blocks_readiness_and_sequence_rebuild(widget):
    widget._plan_apply_pending = True
    with patch.object(panel, "_loop_group_combinations", side_effect=AssertionError("pending plan rebuilt")):
        widget._refresh_readiness()
        widget._refresh_sequence_preview()
        assert widget._readiness_issues() == ["Building sequence…"]
    assert not widget._run_btn.isEnabled()
    assert not widget._apply_btn.isEnabled()


def test_large_run_labels_are_lazy_and_keep_original_positions(widget):
    from ui.loop_run_selection import LoopSelectionDialog
    from PySide6.QtCore import Qt

    widget._loop_table.blockSignals(True)
    panel._populate_loop_table(widget._loop_table, definition("1:1:100000"))
    widget._loop_table.blockSignals(False)
    widget._loop_selection_sync_key = None
    with patch.object(panel._LoopRunLabels, "__getitem__", autospec=True,
                      wraps=panel._LoopRunLabels.__getitem__) as label:
        widget._sync_loop_run_selectors()
        assert label.call_count == 0
    combo = widget._loop_table.cellWidget(0, 4)
    assert isinstance(combo.labels, panel._LoopRunLabels)
    combo.prepare_choices()
    assert combo.count() <= 102
    assert combo.labels[99999] == "100000"
    dialog = LoopSelectionDialog(combo.labels, [0], widget)
    try:
        index = dialog.model.index(99999)
        assert dialog.model.data(index) == "100000"
        assert dialog.model.setData(index, Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
        assert dialog.positions() == [0, 99999]
        widget._set_loop_run_selection(0, dialog.positions())
        assert widget._loop_table._loop_run_positions == {1: [0, 99999]}
    finally:
        dialog.close()


def test_lazy_labels_keep_duplicate_occurrences_and_paired_wavelengths():
    values = panel._LoopRunLabels([1.25, 1.25, 2.5], "Exposure Time (ms)")
    assert values[:] == ["1.25 (#1)", "1.25 (#2)", "2.5"]
    assert values[-1] == "2.5"
    setups = panel._LoopRunLabels(["lightfield", "winspec_ingaas"], panel._SETUP_PARAM, [[750, 1150]])
    assert list(setups) == ["PIXIS · 750 nm", "WinSpec · 1150 nm"]


def test_metadata_groups_match_full_counts_selection_and_validation():
    frame = definition("1,2,3")
    frame.attrs["loop_run_positions"] = {1: [0, 2]}
    full = panel._loop_group_combinations(frame)
    metadata = panel._loop_group_combinations(frame, metadata_only=True)
    assert len(metadata[0]["contexts"]) == len(full[0]["contexts"]) == 2
    assert metadata[0]["positions"] == full[0]["positions"]
    invalid = definition("60", "Stage Position")
    with patch.object(panel, "_active_stage_profile", return_value=panel.get_linear_stage_profile("esp300")):
        with pytest.raises(ValueError, match="50"):
            panel._loop_group_combinations(invalid, metadata_only=True)


def test_baseline_copy_detaches_lists_and_falls_back_for_mutable_values():
    from ui.loop_run_selection import _copy_baseline

    baseline = {"id": {"parameter": "custom", "values": [1, "x", {"nested": [2]}]}}
    copied = _copy_baseline(baseline)
    baseline["id"]["values"][0] = 9
    baseline["id"]["values"][2]["nested"][0] = 8
    assert copied["id"]["values"] == [1, "x", {"nested": [2]}]


def test_ten_thousand_value_edit_defers_preview_but_invalid_draft_still_blocks(widget):
    assert panel._LIVE_DRAFT_SPECTRA_LIMIT == panel._ASYNC_PLAN_THRESHOLD == 2000
    widget._loop_table.blockSignals(True)
    panel._populate_loop_table(widget._loop_table, definition("1:1:10000"))
    widget._loop_table.blockSignals(False)
    applied = widget._acquisition_schedule
    with patch.object(panel, "_build_plan", side_effect=AssertionError("compiled live draft")), \
         patch.object(panel, "_build_acquisition_schedule", side_effect=AssertionError("compiled live schedule")), \
         patch.object(panel, "_build_nested_execution_schedule", side_effect=AssertionError("compiled nested draft")):
        widget._on_draft_edited()
        widget._refresh_filename_preview()
        assert not widget._draft_issues
        assert widget._apply_btn.isEnabled()
        assert not widget._run_btn.isEnabled()
        assert widget._tree._last_plan["preview_state"] == "Applied sequence · apply to preview large draft"
        assert widget._tree._last_plan["acquisition_schedule"] is applied
        assert "2,000" in widget._tree.toolTip()
        assert "Apply this large draft" in widget._filename_preview_lbl.text()
        widget._loop_table.blockSignals(True)
        widget._loop_table.item(0, 2).setText("invalid sequence")
        widget._loop_table.blockSignals(False)
        widget._on_draft_edited()
    assert widget._draft_issues
    assert not widget._apply_btn.isEnabled()
    assert not widget._run_btn.isEnabled()
    assert widget._acquisition_schedule is applied
