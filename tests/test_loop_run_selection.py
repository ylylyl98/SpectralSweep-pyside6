"""Run selection resolves full paired arrays without changing applied/running plans."""
import json
import os
from copy import deepcopy
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pandas as pd
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox
from ui import presets_panel as panel


def definition():
    return pd.DataFrame([
        dict(Enable=True, Parameter="Measurement setup", Values="PIXIS, WinSpec", Group=1),
        dict(Enable=True, Parameter="Center Wavelength (nm)", Values="730, 1150", Group=1),
        dict(Enable=True, Parameter="Exposure Time (ms)", Values="100, 200, 500, 1000", Group=2),
    ])


@pytest.fixture(scope="module", autouse=True)
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def widget(app):
    result = panel.PresetsPanel()
    state = result.capture_session_state()
    state.update(draft_loop=definition().to_dict("records"), loop_mode="Customized",
                 execution_order=[], nested_schedule_enabled=False)
    result.restore_session_state(state)
    result._on_apply()
    yield result
    result.close()


def choose(widget, row, positions):
    widget._set_loop_run_selection(row, positions)


def test_selector_pairs_winspec_and_center_without_rewriting_arrays(widget):
    before = deepcopy(widget._acquisition_schedule)
    choose(widget, 0, [1])
    assert widget._loop_table.item(0, 2).text() == "PIXIS, WinSpec"
    assert widget._loop_table.item(1, 2).text() == "730, 1150"
    assert widget._tables_dirty
    assert widget._acquisition_schedule == before
    draft = widget._tree._last_plan["acquisition_schedule"]
    assert len(draft) == 4
    assert {(t["ctx"]["Measurement setup"], t["ctx"]["Center Wavelength (nm)"]) for t in draft} == {("winspec_ingaas", 1150.0)}
    assert isinstance(widget._loop_table.cellWidget(1, 4), QComboBox)
    widget._on_apply()
    assert len(widget._acquisition_schedule) == 4
    choose(widget, 0, None)
    assert len(widget._tree._last_plan["acquisition_schedule"]) == 8


@pytest.mark.parametrize("replacement", ["100, 500, 200, 1000", "50, 100, 200, 500, 1000", "100, 200, 1000"])
def test_array_structural_edits_require_reselection_instead_of_redirecting(widget, replacement):
    choose(widget, 2, [2])
    widget._on_apply()
    before = deepcopy(widget._acquisition_schedule)
    widget._loop_table.item(2, 2).setText(replacement)
    assert not widget._apply_btn.isEnabled()
    assert "select" in widget._draft_detail_lbl.text().lower()
    assert widget._acquisition_schedule == before
    assert "draft unavailable" in widget._tree._last_plan["preview_state"]
    choose(widget, 2, [0])
    assert widget._apply_btn.isEnabled()
    assert {t["ctx"]["Exposure Time (ms)"] for t in widget._tree._last_plan["acquisition_schedule"]} == {float(replacement.split(",")[0])}


def test_explicit_single_does_not_turn_into_all_when_values_are_added(widget):
    widget._loop_table.item(2, 2).setText("2")
    choose(widget, 2, [0])
    widget._on_apply()
    widget._loop_table.item(2, 2).setText("2, 3")
    assert not widget._apply_btn.isEnabled()
    choose(widget, 2, None)
    assert {t["ctx"]["Exposure Time (ms)"] for t in widget._tree._last_plan["acquisition_schedule"]} == {2.0, 3.0}


def test_all_includes_new_values_without_reselection(widget):
    widget._loop_table.item(2, 2).setText("100, 200, 500, 1000, 2000")
    assert widget._apply_btn.isEnabled()
    assert len(widget._tree._last_plan["acquisition_schedule"]) == 10


def test_invalid_values_survive_another_selector_and_repair_clears_error(widget):
    widget._loop_table.item(2, 2).setText("bad")
    choose(widget, 0, [1])
    assert widget._loop_table.item(2, 2).text() == "bad"
    assert not widget._apply_btn.isEnabled()
    widget._loop_table.item(2, 2).setText("100, 200, 500, 1000")
    assert widget._apply_btn.isEnabled()


def test_full_group_mismatch_is_rejected_even_if_selected_position_exists(widget):
    choose(widget, 0, [0])
    widget._loop_table.item(1, 2).setText("730")
    assert not widget._apply_btn.isEnabled()
    assert "mismatch" in widget._draft_detail_lbl.toolTip().lower()


def test_selection_session_preserves_draft_applied_and_discard(widget):
    choose(widget, 0, [1])
    widget._on_apply()
    choose(widget, 2, [2])
    state = json.loads(json.dumps(widget.capture_session_state()))
    restored = panel.PresetsPanel()
    try:
        restored.restore_session_state(state)
        assert len(restored._acquisition_schedule) == 4
        assert len(restored._tree._last_plan["acquisition_schedule"]) == 1
        assert restored._tables_dirty
        restored._on_discard()
        assert len(restored._tree._last_plan["acquisition_schedule"]) == 4
        assert not restored._tables_dirty
    finally:
        restored.close()


def test_selection_does_not_mutate_a_running_snapshot(widget):
    before = deepcopy(widget._acquisition_schedule)
    widget._run_thread = SimpleNamespace(isRunning=lambda: True)
    shown = widget._tree._last_plan
    try:
        choose(widget, 0, [1])
        widget._on_apply()
        assert widget._acquisition_schedule == before
        assert widget._tree._last_plan is shown
        assert not widget._apply_btn.isEnabled()
    finally:
        widget._run_thread = None


def test_row_move_keeps_selection_and_execution_order(widget):
    choose(widget, 0, [1])
    widget._on_apply()
    widget._loop_table.selectRow(0)
    widget._move_loop_row(2)
    widget._on_apply()
    assert len(widget._acquisition_schedule) == 4
    assert {t["ctx"]["Measurement setup"] for t in widget._acquisition_schedule} == {"winspec_ingaas"}


def test_group_renumber_preserves_selection_but_regroup_requires_review(widget):
    choose(widget, 0, [1])
    blocked = widget._loop_table.blockSignals(True)
    for row in (0, 1):
        widget._loop_table.item(row, 3).setText("7")
    widget._loop_table.blockSignals(blocked)
    widget._on_draft_edited()
    assert not widget._draft_issues
    assert len(widget._tree._last_plan["acquisition_schedule"]) == 4
    widget._mode_combo.setCurrentText("Synchronize")
    assert not widget._apply_btn.isEnabled()


def test_empty_selection_cannot_fall_back_to_a_default_context(widget):
    choose(widget, 0, [])
    assert not widget._apply_btn.isEnabled()
    assert not widget._run_btn.isEnabled()


def test_nested_selection_keeps_original_occurrence_identity_and_order():
    loop = definition()
    loop.loc[0, "Values"] = "WinSpec, WinSpec"
    loop.loc[1, "Values"] = "1150, 1150"
    loop.attrs["loop_run_positions"] = {1: [1], 2: [2]}
    batch = panel._DEFAULT_BATCH.copy(deep=True)
    batch.loc[0, "frames"] = 3
    batch.loc[0, "repeat"] = 2
    order = ["conditions", "points", "group:1", "group:2"]
    tasks = panel._build_nested_execution_schedule(loop, batch, mode="Customized", execution_order=order)
    assert len(tasks) == 6
    assert {t["group_instances"]["group:1"] for t in tasks} == {1}
    assert {t["group_instances"]["group:2"] for t in tasks} == {2}
    assert all([e["id"] for e in t["execution_order"]] == order for t in tasks)


def test_selection_preserves_motion_zero_and_stage_linspace():
    loop = pd.DataFrame([
        dict(Enable=True, Parameter="Rotation1 Angle (deg)", Values="0, 90, -90", Group=1),
        dict(Enable=True, Parameter="Stage Position", Values="linspace(0, 5, 6)", Group=2),
    ])
    loop.attrs["loop_run_positions"] = {1: [0, 2], 2: [5]}
    seq, _, _ = panel._build_plan(loop, panel._DEFAULT_BATCH, "Customized")
    assert seq == [{"Rotation1 Angle (deg)": 0.0, "Stage Position": 5.0},
                   {"Rotation1 Angle (deg)": -90.0, "Stage Position": 5.0}]


def test_when_filters_selected_context_to_zero_tasks(widget):
    widget._batch_table.item(0, panel.BATCH_SCHEMA.index("When")).setText("Exposure_Time == 200")
    choose(widget, 2, [2])
    widget._on_apply()
    assert widget._acquisition_schedule == []
    assert not widget._run_btn.isEnabled()


def test_restoring_in_place_replaces_selection_even_if_table_cells_are_unchanged(widget):
    choose(widget, 0, [1])
    widget._on_apply()
    saved = widget.capture_session_state()
    choose(widget, 0, None)
    widget.restore_session_state(saved)
    assert not widget._tables_dirty
    assert len(widget._tree._last_plan["acquisition_schedule"]) == 4


def test_editing_setup_keeps_selected_motion_values(widget):
    widget._add_loop_row()
    row = widget._loop_table.rowCount() - 1
    combo = widget._loop_table.cellWidget(row, 1)
    combo.setCurrentIndex(combo.findData("Rotation1 Angle (deg)"))
    widget._loop_table.item(row, 2).setText("0, 90, 180")
    widget._loop_table.item(row, 3).setText("3")
    checkbox = widget._loop_table.cellWidget(row, 0).findChild(panel.QCheckBox)
    checkbox.setChecked(True)
    choose(widget, row, [1])
    widget._on_apply()
    widget._loop_table.item(0, 2).setText("Andor Silicon, WinSpec")
    assert {t["ctx"]["Rotation1 Angle (deg)"] for t in widget._tree._last_plan["acquisition_schedule"]} == {90.0}


def test_run_metadata_records_full_arrays_and_selection(widget):
    choose(widget, 0, [1])
    widget._on_apply()
    plan = widget._current_run_meta()["executed_plan"]
    assert plan["loop_definition"].iloc[0]["Values"] == "PIXIS, WinSpec"
    assert plan["loop_run_positions"] == {1: [1]}


def test_keyboard_dropdown_selects_actual_paired_position(widget, app):
    widget.resize(1400, 1000)
    widget.show()
    app.processEvents()
    combo = widget._loop_table.cellWidget(0, 4)
    combo.setFocus()
    combo.showPopup()
    app.processEvents()
    QTest.keyClick(combo.view(), Qt.Key.Key_Down)
    QTest.keyClick(combo.view(), Qt.Key.Key_Down)
    QTest.keyClick(combo.view(), Qt.Key.Key_Return)
    app.processEvents()
    assert len(widget._tree._last_plan["acquisition_schedule"]) == 4
    assert {t["ctx"]["Center Wavelength (nm)"] for t in widget._tree._last_plan["acquisition_schedule"]} == {1150.0}


@pytest.mark.parametrize("positions", [[0.5], ["0"], 0])
def test_malformed_session_selection_becomes_a_reselectable_draft(widget, positions):
    choose(widget, 0, [1])
    widget._on_apply()
    state = widget.capture_session_state()
    record = next(r for r in state["loop_run_selections"] if r["positions"] is not None)
    record["positions"] = positions
    widget.restore_session_state(state)
    assert not widget._apply_btn.isEnabled()
    assert "select" in widget._draft_detail_lbl.text().lower()
    assert len(widget._acquisition_schedule) == 4
    choose(widget, 0, [0])
    assert widget._apply_btn.isEnabled()


def test_close_numeric_values_have_distinguishable_picker_labels(widget):
    widget._loop_table.item(2, 2).setText("123.45671, 123.45672")
    labels = widget._loop_table.cellWidget(2, 4).labels
    assert labels == ["123.45671", "123.45672"]


def test_disabled_row_does_not_require_reselection_and_keeps_its_choice(widget):
    choose(widget, 2, [2])
    checkbox = widget._loop_table.cellWidget(2, 0).findChild(panel.QCheckBox)
    checkbox.setChecked(False)
    widget._loop_table.item(2, 2).setText("bad")
    assert not widget._draft_issues
    assert not widget._loop_table.cellWidget(2, 4).isEnabled()
    widget._loop_table.item(2, 2).setText("100, 200, 500, 1000")
    checkbox.setChecked(True)
    assert not widget._draft_issues
    assert {t["ctx"]["Exposure Time (ms)"] for t in widget._tree._last_plan["acquisition_schedule"]} == {500.0}


def test_run_lifecycle_disables_then_restores_selectors_without_an_edit(widget):
    running = [True]
    widget._run_thread = SimpleNamespace(isRunning=lambda: running[0])
    try:
        widget._refresh_readiness()
        assert not widget._loop_table.cellWidget(0, 4).isEnabled()
        running[0] = False
        widget._refresh_readiness()
        assert widget._loop_table.cellWidget(0, 4).isEnabled()
    finally:
        widget._run_thread = None


def test_duplicate_saved_indices_display_one_selected_occurrence(widget):
    choose(widget, 0, [1])
    widget._on_apply()
    state = widget.capture_session_state()
    record = next(r for r in state["loop_run_selections"] if r["positions"] is not None)
    record["positions"] = [1, 1]
    widget.restore_session_state(state)
    assert "WinSpec" in widget._loop_table.cellWidget(0, 4).currentText()
    assert len(widget._tree._last_plan["acquisition_schedule"]) == 4


def test_search_picker_accepts_original_positions_beyond_combo_limit(widget):
    from ui.loop_run_selection import LoopSelectionDialog
    from PySide6.QtCore import QSortFilterProxyModel
    widget._loop_table.item(2, 2).setText(", ".join(str(i) for i in range(1, 202)))
    combo = widget._loop_table.cellWidget(2, 4)
    combo.showPopup()
    assert combo.count() <= 102
    combo.hidePopup()
    dialog = LoopSelectionDialog(combo.labels, [0], widget)
    try:
        search = dialog.findChild(panel.QLineEdit)
        search.setText("201")
        proxy = dialog.findChild(QSortFilterProxyModel)
        index = proxy.index(0, 0)
        assert proxy.data(index) == "201"
        assert proxy.setData(index, Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
        assert dialog.positions() == [0, 200]
        dialog.accept()
        choose(widget, 2, dialog.positions())
        assert {t["ctx"]["Exposure Time (ms)"] for t in widget._tree._last_plan["acquisition_schedule"]} == {1.0, 201.0}
    finally:
        dialog.close()
