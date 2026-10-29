"""Filename previews stay bounded and keep the resolved run selection/order."""
import os
from types import MethodType, SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pandas as pd
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ui import presets_panel as panel


class _Text:
    def __init__(self):
        self.value = ""

    def setText(self, value):
        self.value = value

    setPlainText = setText

    def setToolTip(self, _value):
        pass


def _large_loop():
    return pd.DataFrame([
        dict(Enable=True, Parameter=name,
             Values=", ".join(str(i) for i in range(start, start + 100)), Group=group)
        for group, (name, start) in enumerate([
            ("Center Wavelength (nm)", 600), ("Exposure Time (ms)", 100),
            ("Accumulations (EPF)", 1), ("Rotation1 Angle (deg)", 0),
        ], 1)
    ])


def _preview_double():
    fake = SimpleNamespace(
        _loop_table=object(), _batch_table=object(),
        _mode_combo=SimpleNamespace(currentText=lambda: "Synchronize"),
        _current_acquisition_grouping=lambda: "loop_first",
        _legacy_loop_order=[], _nested_schedule_enabled=False,
        _tables_dirty=True, _tree=SimpleNamespace(_last_plan={}),
        _filename_preview_lbl=_Text(), _save_path_preview_lbl=_Text(),
        _preview_note_lbl=_Text(), _upcoming_preview=_Text(),
        _sync_filename_parts_from_loop_table=lambda: None,
    )
    fake._draft_loop_groups = MethodType(panel.PresetsPanel._draft_loop_groups, fake)
    fake._draft_loop_and_batch = MethodType(panel.PresetsPanel._draft_loop_and_batch, fake)
    return fake


@pytest.mark.parametrize("batch_enabled", [True, False])
def test_huge_cartesian_draft_is_rejected_before_entering_full_planner(batch_enabled):
    # Four 100-value rows represent 100,000,000 contexts. Intercept allocation
    # at the planner boundary while keeping the real preview path/validation.
    fake = _preview_double()
    batch = panel._DEFAULT_BATCH.copy(deep=True)
    batch.loc[:, "Run"] = batch_enabled
    with patch.object(panel, "_read_loop_table", return_value=_large_loop()), \
         patch.object(panel, "_read_batch_table", return_value=batch), \
         patch.object(panel, "_build_plan", side_effect=AssertionError("unbounded planner entered")) as build:
        panel.PresetsPanel._refresh_filename_preview(fake)
    assert build.call_count == 0
    assert "invalid" in fake._filename_preview_lbl.value.lower()
    assert "100,000" in fake._filename_preview_lbl.value
    assert not fake._upcoming_preview.value


def test_many_gate_points_defer_before_resolving_nested_vectors():
    fake = _preview_double()
    fake._nested_schedule_enabled = True
    loop = _large_loop().iloc[:1].copy()
    loop.loc[:, "Values"] = "730"
    batch = panel._DEFAULT_BATCH.copy(deep=True)
    batch.loc[:, "frames"] = 20_001
    with patch.object(panel, "_read_loop_table", return_value=loop), \
         patch.object(panel, "_read_batch_table", return_value=batch), \
         patch.object(panel, "_build_plan", side_effect=AssertionError("unbounded planner entered")) as build:
        panel.PresetsPanel._refresh_filename_preview(fake)
    assert build.call_count == 0
    assert "apply" in fake._upcoming_preview.value.lower()


def test_selected_positions_bound_large_definition_by_the_executed_subset():
    fake = _preview_double()
    loop = _large_loop()
    loop.attrs["loop_run_positions"] = {1: [1], 2: [1], 3: [1], 4: [1]}
    with patch.object(panel, "_read_loop_table", return_value=loop), \
         patch.object(panel, "_read_batch_table", return_value=panel._DEFAULT_BATCH):
        _definition, _rows, sequence, _enabled = fake._draft_loop_and_batch()
    assert sequence == [{
        "Center Wavelength (nm)": 601.0, "Exposure Time (ms)": 101.0,
        "Accumulations (EPF)": 2.0, "Rotation1 Angle (deg)": 1.0,
    }]


@pytest.mark.parametrize("state,dirty", [("Applied sequence", False), ("Draft sequence", True)])
def test_resolved_large_preview_is_reused_without_replanning(state, dirty):
    fake = _preview_double()
    loop = panel._normalize_loop(_large_loop())
    batch = panel._normalize_batch(panel._DEFAULT_BATCH)
    enabled = batch[batch["Run"]].reset_index(drop=True)
    resolved = [{"Center Wavelength (nm)": 600.0}]
    fake._tables_dirty = dirty
    fake._tree._last_plan = dict(
        preview_state=state, final_sequence=resolved, df_batch=enabled,
        loop_definition=loop, loop_mode="Synchronize", acquisition_grouping="loop_first",
    )
    with patch.object(panel, "_read_loop_table", return_value=loop), \
         patch.object(panel, "_read_batch_table", return_value=batch), \
         patch.object(panel, "_build_plan", side_effect=AssertionError("resolved plan rebuilt")):
        _definition, _rows, sequence, _enabled = fake._draft_loop_and_batch()
    assert sequence is resolved


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def widget(app):
    result = panel.PresetsPanel()
    state = result.capture_session_state()
    state.update(
        draft_loop=[
            dict(Enable=True, Parameter="Measurement setup", Values="PIXIS, WinSpec", Group=1),
            dict(Enable=True, Parameter="Center Wavelength (nm)", Values="730, 1150", Group=1),
            dict(Enable=True, Parameter="Exposure Time (ms)", Values="100, 200", Group=2),
        ],
        loop_mode="Customized", execution_order=[], nested_schedule_enabled=False,
    )
    result.restore_session_state(state)
    yield result
    result._preview_timer.stop()
    result.close()


def test_small_panel_preview_uses_first_when_match_of_selected_paired_values(widget):
    widget._set_loop_run_selection(0, [1])
    widget._batch_table.item(0, panel.BATCH_SCHEMA.index("When")).setText("Exposure_Time == 200")
    widget._refresh_filename_preview()
    assert "1150" in widget._filename_preview_lbl.text()
    assert "0p2s" in widget._filename_preview_lbl.text()
    upcoming = widget._upcoming_preview.toPlainText().splitlines()
    assert len(upcoming) == 1
    assert "1150" in upcoming[0] and "0p2s" in upcoming[0]
    assert "730" not in upcoming[0]


@pytest.mark.parametrize("batch_enabled", [True, False])
def test_over_budget_panel_draft_disables_apply_without_preview_expansion(widget, batch_enabled):
    applied = widget._acquisition_schedule
    blocked = widget._loop_table.blockSignals(True)
    try:
        panel._populate_loop_table(widget._loop_table, _large_loop())
    finally:
        widget._loop_table.blockSignals(blocked)
    widget._mode_combo.blockSignals(True)
    widget._mode_combo.setCurrentText("Synchronize")
    widget._mode_combo.blockSignals(False)
    widget._batch_table.item(0, panel.BATCH_SCHEMA.index("Run")).setCheckState(
        Qt.CheckState.Checked if batch_enabled else Qt.CheckState.Unchecked)
    with patch.object(panel, "_build_plan", side_effect=AssertionError("unbounded planner entered")):
        widget._on_draft_edited()
        widget._refresh_filename_preview()
    assert "draft unavailable" in widget._tree._last_plan["preview_state"]
    assert "100,000" in widget._filename_preview_lbl.text()
    assert not widget._apply_btn.isEnabled()
    assert widget._acquisition_schedule is applied


@pytest.mark.parametrize("applied", [False, True])
def test_small_panel_preview_reuses_resolved_sequence(widget, applied):
    widget._set_loop_run_selection(0, [1])
    widget._batch_table.item(0, panel.BATCH_SCHEMA.index("When")).setText("Exposure_Time == 200")
    if applied:
        widget._on_apply()
    with patch.object(panel, "_build_plan", side_effect=AssertionError("resolved plan rebuilt")):
        widget._refresh_filename_preview()
    assert "1150" in widget._filename_preview_lbl.text()
    assert "0p2s" in widget._filename_preview_lbl.text()
    assert "invalid" not in widget._filename_preview_lbl.text().lower()


def test_nested_panel_upcoming_preview_keeps_condition_point_selection_order(widget):
    widget._set_loop_run_selection(0, [1])
    widget._set_loop_run_selection(2, [1])
    widget._nested_schedule_enabled = True
    widget._execution_order = ["conditions", "points", "group:1", "group:2"]
    widget._batch_table.item(0, panel.BATCH_SCHEMA.index("frames")).setText("3")
    widget._batch_table.item(0, panel.BATCH_SCHEMA.index("When")).setText("Exposure_Time == 200")
    widget._on_draft_edited()
    with patch.object(panel, "_build_plan", side_effect=AssertionError("resolved plan rebuilt")), \
         patch.object(panel, "_build_nested_execution_schedule", side_effect=AssertionError("resolved schedule rebuilt")):
        widget._refresh_filename_preview()
    upcoming = widget._upcoming_preview.toPlainText().splitlines()
    assert len(upcoming) == 3
    assert all("1150" in name and "0p2s" in name and "WinSpec" in name for name in upcoming)
