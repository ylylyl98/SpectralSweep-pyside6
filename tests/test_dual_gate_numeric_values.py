"""Shared input semantics, run selections, and pre-versioned sample recovery."""
from copy import deepcopy
import json

import pandas as pd
import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from ui import presets_panel as panel
from ui.loop_run_selection import LoopRunSelections


def row(param, values, group=1, row_id="row-1"):
    return dict(Enable=True, Parameter=param, Values=values, Group=group, _run_id=row_id)


@pytest.mark.parametrize("expression", ["(1,5,5)", "linspace(1,5,5)", "1:1:5", "[1,2,3,4,5]"])
@pytest.mark.parametrize("parameter", panel._SPECTROMETER_PARAMS + ("Rotation1 Angle (deg)", "Rotation2 Angle (deg)", "Stage Position"))
def test_calculator_and_each_numeric_loop_parameter_resolve_identically(expression, parameter):
    assert panel._parse_values(expression, parameter) == panel._parse_sweep_constants(expression) == [1, 2, 3, 4, 5]


def test_parameter_constraints_remain_after_sequence_expansion():
    with pytest.raises(ValueError, match="between -200 and 200"):
        panel._parse_sweep_constants("linspace(0,201,3)")
    with pytest.raises(ValueError, match="At most 500"):
        panel._parse_sweep_constants("linspace(0,1,501)")
    with pytest.raises(ValueError, match="positive integers"):
        panel._parse_values("linspace(1,2,3)", "Accumulations (EPF)")
    with pytest.raises(ValueError, match="positive values"):
        panel._parse_values("linspace(-1,1,3)", "Exposure Time (ms)")
    loop = pd.DataFrame([row("Stage Position", "linspace(0,1e6,3)")])
    with pytest.raises(ValueError, match="outside the active"):
        panel._build_plan(loop, panel._DEFAULT_BATCH)
    assert panel._parse_values("PIXIS, WinSpec", "Measurement setup") == ["lightfield", "winspec_ingaas"]


def test_equivalent_syntax_keeps_paired_run_selection_and_execution_order():
    rows = [row("Rotation1 Angle (deg)", "0,45,90"),
            row("Exposure Time (ms)", "100,200,300", row_id="row-2")]
    selections = LoopRunSelections()
    selections.synchronize(rows, "Customized", panel._parse_values)
    selections.choose(1, [0, 2])
    rows[0]["Values"] = "linspace(0,90,3)"
    rows[1]["Values"] = "(100,300,3)"
    selections.synchronize(rows, "Customized", panel._parse_values)
    assert selections.positions() == {1: [0, 2]}
    loop = pd.DataFrame(rows)
    loop.attrs["loop_run_positions"] = selections.positions()
    sequence, _, _ = panel._build_plan(loop, panel._DEFAULT_BATCH, "Customized")
    assert sequence == [{"Rotation1 Angle (deg)": 0, "Exposure Time (ms)": 100},
                        {"Rotation1 Angle (deg)": 90, "Exposure Time (ms)": 300}]
    loop.loc[1, "Values"] = "linspace(100,300,4)"
    with pytest.raises(ValueError, match="value-count mismatch"):
        panel._build_plan(loop, panel._DEFAULT_BATCH, "Customized")


@pytest.mark.parametrize("parameter, expression, expected", [
    ("Rotation1 Angle (deg)", "linspace(-4,4,5)", [-4, 4, 5]),
    ("Exposure Time (ms)", "(100,200,5)", [100, 200, 5]),
    ("Stage Position", "linspace(0,5,6)", [0, 1, 2, 3, 4, 5]),
    ("Stage Position", "(0,5,1.5)", [0, 5, 1.5]),
    ("Rotation2 Angle (deg)", "(0,45,90,180)", [0, 45, 90, 180]),
    ("Rotation1 Angle (deg)", "0,,90,", [0, 90]),
])
def test_legacy_migration_preserves_actual_values_and_input_state(parameter, expression, expected):
    original = {"draft_loop": [row(parameter, expression)],
                "applied_loop": [row(parameter, expression)]}
    before = deepcopy(original)
    migrated = panel._migrate_legacy_loop_values(original)
    assert original == before
    for key in ("draft_loop", "applied_loop"):
        assert panel._parse_values(migrated[key][0]["Values"], parameter) == expected
        assert migrated[key][0]["_run_id"] == "row-1"
    assert panel._migrate_legacy_loop_values(migrated) == migrated


def test_migration_keeps_text_setups_and_unfinished_drafts():
    rows = [row("Measurement setup", "PIXIS, WinSpec"),
            row("Stage Position", "linspace(0,", row_id="row-2")]
    assert panel._migrate_legacy_loop_values({"draft_loop": rows})["draft_loop"] == rows


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def widget(app):
    widget = panel.PresetsPanel()
    yield widget
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_legacy_session_keeps_draft_applied_and_selected_positions(widget):
    state = widget.capture_session_state()
    state.update(draft_loop=[row("Rotation1 Angle (deg)", "-4,4,5")],
                 loop_mode="Customized", nested_schedule_enabled=False, execution_order=[])
    widget.restore_session_state(state)
    widget._set_loop_run_selection(0, [1])
    widget._on_apply()
    widget._set_loop_run_selection(0, [2])
    legacy = json.loads(json.dumps(widget.capture_session_state()))
    legacy.pop("numeric_values_version")
    for key in ("draft_loop", "applied_loop"):
        legacy[key][0]["Values"] = "linspace(-4,4,5)"
    widget.restore_session_state(legacy)
    assert widget._loop_run_selections.positions() == {1: [2]}
    assert widget._final_seq == [{"Rotation1 Angle (deg)": 4}]
    assert {task["ctx"]["Rotation1 Angle (deg)"] for task in widget._tree._last_plan["acquisition_schedule"]} == {5}
    saved = json.loads(json.dumps(widget.capture_session_state()))
    assert saved["numeric_values_version"] == panel._NUMERIC_VALUES_VERSION
    assert saved["draft_loop"][0]["Values"] == "[-4.0, 4.0, 5.0]"
    widget.restore_session_state(saved)
    assert widget._loop_run_selections.positions() == {1: [2]}
    widget._on_discard()
    assert widget._loop_run_selections.positions() == {1: [1]}


def test_new_session_and_calculator_keep_linspace_semantics(widget):
    state = widget.capture_session_state()
    state.update(draft_loop=[row("Rotation1 Angle (deg)", "(-4,4,5)")],
                 loop_mode="Customized", nested_schedule_enabled=False, execution_order=[])
    widget.restore_session_state(state)
    widget._on_apply()
    assert [ctx["Rotation1 Angle (deg)"] for ctx in widget._final_seq] == [-4, -2, 0, 2, 4]
    calc = widget._sweep_calc
    calc._constant_edit.setText("linspace(-4,4,5)")
    calc._recalculate()
    assert len(calc._calculated_rows) == 5
    saved = json.loads(json.dumps(widget.capture_session_state()))
    widget.restore_session_state(saved)
    assert widget._loop_table.item(0, 2).text() == "(-4,4,5)"
    assert len(widget._final_seq) == 5
    assert calc._constant_edit.text() == "linspace(-4,4,5)"
    for tooltip in (calc._constant_edit.toolTip(), widget._loop_table.horizontalHeaderItem(2).toolTip()):
        assert "point count" in tooltip
        assert "start:step:stop" in tooltip


def test_invalid_linspace_draft_cannot_replace_applied_plan(widget):
    before = deepcopy(widget._acquisition_schedule)
    widget._loop_table.item(0, 2).setText("linspace(800,900,2.5)")
    assert not widget._apply_btn.isEnabled()
    assert "point count" in widget._loop_table.item(0, 2).toolTip()
    widget._on_apply()
    assert widget._acquisition_schedule == before
