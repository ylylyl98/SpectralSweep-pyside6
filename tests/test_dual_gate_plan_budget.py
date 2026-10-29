"""Large recipes fail before allocation; Apply reuses its validated result."""
from copy import deepcopy
from unittest.mock import patch

import pandas as pd
import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from ui import presets_panel as panel


def rotation_rows(values="linspace(0,180,1000)"):
    return pd.DataFrame([
        dict(Enable=True, Parameter=parameter, Values=values, Group=index,
             _run_id=f"budget-{index}")
        for index, parameter in enumerate(
            ("Rotation1 Angle (deg)", "Rotation2 Angle (deg)"), 1
        )
    ])


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("batch_enabled", [False, True])
def test_cartesian_limit_is_checked_before_product_allocation(nested, batch_enabled):
    batch = panel._DEFAULT_BATCH.copy()
    batch.loc[:, "Run"] = batch_enabled
    build = panel._build_nested_execution_schedule if nested else panel._build_plan
    with patch.object(panel.itertools, "product", side_effect=AssertionError("expanded product")):
        with pytest.raises(ValueError, match="1,000,000 loop contexts"):
            build(rotation_rows(), batch)


@pytest.mark.parametrize("nested", [False, True])
def test_frames_repeats_and_false_when_do_not_bypass_budget(nested):
    batch = panel._DEFAULT_BATCH.copy()
    batch.loc[:, "frames"] = 1001
    batch.loc[:, "repeat"] = 100
    batch.loc[:, "When"] = "False"
    build = panel._build_nested_execution_schedule if nested else panel._build_plan
    with patch.object(panel.itertools, "product", side_effect=AssertionError("expanded product")), \
         patch.object(panel, "_resolve_sweep_vectors", side_effect=AssertionError("allocated vectors")):
        with pytest.raises(ValueError, match="100,100 spectra"):
            build(rotation_rows("0"), batch)


def test_nested_budget_counts_filtered_short_condition_candidates():
    batch = pd.concat([panel._DEFAULT_BATCH] * 2, ignore_index=True)
    batch.loc[:, "frames"] = [50_000, 1]
    batch.loc[:, "repeat"] = [1, 2]
    estimate = panel._validate_plan_budget(1, batch)
    assert estimate["potential_spectra"] == 50_002
    with patch.object(panel.itertools, "product", side_effect=AssertionError("expanded product")):
        with pytest.raises(ValueError, match="150,000 planning steps"):
            panel._build_nested_execution_schedule(rotation_rows("0"), batch)


def test_exact_total_limit_and_disabled_rows_are_allowed():
    batch = pd.concat([panel._DEFAULT_BATCH] * 2, ignore_index=True)
    batch.loc[:, "Run"] = [True, False]
    batch.loc[:, "frames"] = [100, 1_000_000]
    batch.loc[:, "repeat"] = [10, 1_000_000]
    assert panel._validate_plan_budget(100, batch, nested=True) == {
        "contexts": 100, "potential_spectra": 100_000, "candidate_steps": 100_000,
    }


def test_run_selection_limits_the_effective_product():
    rows = rotation_rows()
    rows.attrs["loop_run_positions"] = {1: [0], 2: [999]}
    sequence, _, total = panel._build_plan(rows, panel._DEFAULT_BATCH)
    assert sequence == [{"Rotation1 Angle (deg)": 0.0, "Rotation2 Angle (deg)": 180.0}]
    assert total == 1


def test_direct_schedule_builder_checks_budget_before_when_filtering():
    batch = panel._DEFAULT_BATCH.copy()
    batch.loc[:, "frames"] = 100_001
    batch.loc[:, "When"] = "False"
    with patch.object(panel, "_when_ok", side_effect=AssertionError("evaluated When")):
        with pytest.raises(ValueError, match="100,001 spectra"):
            panel._build_acquisition_schedule([{}], batch)


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def widget(app):
    result = panel.PresetsPanel()
    yield result
    result._preview_timer.stop()
    result.close()
    result.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def draft_state(widget, *, values="linspace(0,180,1000)"):
    state = widget.capture_session_state()
    state.update(draft_loop=rotation_rows(values).to_dict("records"),
                 loop_mode="Synchronize", execution_order=[], nested_schedule_enabled=False)
    return state


def test_over_budget_apply_preserves_previous_plan_and_run_is_blocked(widget):
    widget.restore_session_state(draft_state(widget))
    applied = widget._acquisition_schedule
    assert not widget._apply_btn.isEnabled()
    assert any("1,000,000 loop contexts" in issue for issue in widget._draft_issues)
    with patch.object(panel, "_build_plan", side_effect=AssertionError("entered planner")):
        widget._on_apply()
    assert widget._acquisition_schedule is applied
    _, error = widget._validate_before_run()
    assert "1,000,000 loop contexts" in error


def test_over_budget_restored_applied_plan_is_editable_and_not_runnable(widget):
    state = draft_state(widget)
    state["applied_loop"] = deepcopy(state["draft_loop"])
    state["applied_loop_mode"] = "Synchronize"
    state["applied_execution_order"] = []
    with patch.object(panel.itertools, "product", side_effect=AssertionError("expanded product")):
        widget.restore_session_state(state)
    assert widget._loop_table.item(0, 2).text() == "linspace(0,180,1000)"
    assert not widget._acquisition_schedule
    assert not widget._final_seq
    assert "1,000,000 loop contexts" in widget._summary_lbl.text()
    assert not widget._apply_btn.isEnabled()


@pytest.mark.parametrize("nested", [False, True])
def test_apply_builds_the_validated_sequence_and_each_schedule_once(widget, nested):
    state = draft_state(widget, values="0,90")
    if nested:
        state.update(nested_schedule_enabled=True,
                     execution_order=[{"kind": "group", "id": "group:1"},
                                      {"kind": "group", "id": "group:2"},
                                      {"kind": "conditions", "id": "conditions"},
                                      {"kind": "points", "id": "points"}])
    widget.restore_session_state(state)
    assert widget._apply_btn.isEnabled()
    with patch.object(panel, "_build_plan", wraps=panel._build_plan) as build, \
         patch.object(panel, "_build_acquisition_schedule", wraps=panel._build_acquisition_schedule) as schedule, \
         patch.object(panel, "_build_nested_execution_schedule", wraps=panel._build_nested_execution_schedule) as nested_schedule:
        widget._on_apply()
    assert build.call_count == 1
    # Nested plans no longer build and discard a second, legacy schedule.
    assert schedule.call_count == int(not nested)
    assert nested_schedule.call_count == int(nested)
    assert len(widget._final_seq) == 4
    assert len(widget._acquisition_schedule) == 4
    assert not widget._tables_dirty
