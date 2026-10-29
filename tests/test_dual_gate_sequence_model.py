"""Sequence review uses the worker's order without changing or expanding it."""
import copy
import threading
from dataclasses import FrozenInstanceError

import pytest

from app.dual_gate_sequence_model import DualGateSequenceModel, prepare_flat_sequence_index


def flat_plan(grouping="loop_first"):
    contexts = [{"Center Wavelength (nm)": 750}, {"Center Wavelength (nm)": 810}]
    rows = [dict(Run=True, condition_label="always", frames=3, repeat=2, Vtg_start=0,
                 Vtg_stop=2, Vbg_start=1, Vbg_stop=3, When=""),
            dict(Run=True, condition_label="red", frames=2, repeat=1, Vtg_start=5,
                 Vtg_stop=6, Vbg_start=5, Vbg_stop=6, When="Center_Wavelength > 800")]
    pairs = [(s, r) for s in range(2) for r in range(2)] if grouping == "loop_first" else [(s, r) for r in range(2) for s in range(2)]
    tasks = [dict(seq_i=s, row_i=r, ctx=contexts[s], row=rows[r]) for s, r in pairs if r == 0 or s == 1]
    return contexts, rows, tasks


def nested_plan(count=12):
    order = [dict(kind="conditions", label="Gate conditions"),
             dict(kind="points", label="Gate points"),
             dict(kind="group", id="group:1", label="Rotation")]
    return [dict(nested=True, seq_i=i, row_i=0,
                 ctx={"Rotation1 Angle (deg)": (i % 2) * 90},
                 row=dict(condition_label="scan", frames=1, repeat=1, When=""),
                 gate_point=dict(point_i=(i // 2) % 3, point_total=3, Vtg=(i // 2) % 3, Vbg=0, Vbias=None),
                 repeat_i=i // 6, repeat_total=2, execution_order=order,
                 group_values={"group:1": {"Rotation1 Angle (deg)": (i % 2) * 90}},
                 group_instances={"group:1": i % 2}) for i in range(count)]


def test_flat_order_skips_and_units_match_compiled_schedule():
    for grouping in ("loop_first", "batch_first"):
        contexts, rows, tasks = flat_plan(grouping)
        before = copy.deepcopy((contexts, rows, tasks))
        model = DualGateSequenceModel(page_size=8)
        model.update_plan(contexts, rows, acquisition_schedule=tasks, acquisition_grouping=grouping, total_acq=5)
        assert model.totalSteps == 3 and model.rowCount() == 3
        assert [row["condition"] for row in model._rows] == [task["row"]["condition_label"] for task in tasks]
        assert "sweep steps" in model.summary and "1 skipped" in model.summary
        assert model._rows[0]["acquisition"] == "3 frames × 2 repeats"
        model.setShowSkipped(True)
        assert model.rowCount() == 4
        skipped_index = next(i for i, row in enumerate(model._rows) if row["sourceIndex"] < 0)
        model.selectRow(skipped_index)
        assert model.selectedDetail["title"] == "Skipped combination"
        assert "Center_Wavelength > 800" in model.selectedDetail["when"]
        assert "750" in model.selectedDetail["loop"]
        model.goToStep("3")
        assert model.selectedDetail["title"] == "Step 3"
        assert (contexts, rows, tasks) == before


def test_progress_notifies_only_changed_presentation_properties():
    contexts, rows, tasks = flat_plan()
    model = DualGateSequenceModel()
    kwargs = dict(acquisition_schedule=tasks, current_seq_i=0,
                  current_frame_total=3, run_outcome="running", total_acq=5)
    model.update_plan(contexts, rows, current_frame_i=1, **kwargs)
    counts = dict.fromkeys(("overview", "detail", "focus", "following", "filters", "nesting"), 0)
    for name, signal in (("overview", model.overviewChanged), ("detail", model.selectedDetailChanged),
                         ("focus", model.focusStepChanged), ("following", model.followingStepChanged),
                         ("filters", model.filtersChanged), ("nesting", model.nestingChanged)):
        signal.connect(lambda name=name: counts.__setitem__(name, counts[name] + 1))
    model.update_plan(contexts, rows, current_frame_i=2, **kwargs)
    assert counts == dict(overview=0, detail=1, focus=1, following=0, filters=0, nesting=0)
    assert model.focusStep["orderPath"][3]["value"] == "Frame 2 / 3"
    model.update_plan(contexts, rows, current_frame_i=2, done=1, **kwargs)
    assert counts == dict(overview=1, detail=1, focus=1, following=0, filters=0, nesting=0)
    kwargs["run_outcome"] = "failed"
    model.update_plan(contexts, rows, current_frame_i=2, done=1, **kwargs)
    assert counts["overview"] == 2 and counts["focus"] == 2 and counts["detail"] == 2
    assert model.focusStep["statusKind"] == "failed"


def test_nested_points_files_repeats_and_details_are_distinct():
    tasks, rows = nested_plan(), [dict(Run=True, condition_label="scan", When="", frames=3, repeat=2)]
    model = DualGateSequenceModel()
    model.update_plan([], rows, acquisition_schedule=tasks, total_acq=4, done=1,
                      current_seq_i=4, completed_points=4, run_outcome="running")
    assert "4/12 spectra" in model.summary and "1/4 files" in model.summary
    assert [row["statusKind"] for row in model._rows[:5]] == ["done"] * 4 + ["now"]
    assert not model.supportsSkipped
    assert model.selectedDetail["title"] == "Step 5"
    assert "Gate point 3/3" in model.selectedDetail["path"]
    assert "1 spectrum" in model.selectedDetail["acquisition"]
    model.goToStep("7")
    assert "repeat 2/2" in model.selectedDetail["condition"]
    assert "Repeat: 1 → 2" in model.selectedDetail["changes"]


def test_user_browsing_is_not_overridden_by_progress_and_follow_is_explicit():
    tasks, contexts, rows = nested_plan(120), [], []
    model = DualGateSequenceModel(page_size=8)
    model.update_plan(contexts, rows, acquisition_schedule=tasks, current_seq_i=1, run_outcome="running")
    model.goToStep("85")
    assert model.page == 11 and not model.followCurrent
    model.update_plan(contexts, rows, acquisition_schedule=tasks, current_seq_i=100, completed_points=100, run_outcome="running")
    assert model.page == 11 and model.selectedDetail["title"] == "Step 85"
    model.setFollowCurrent(True)
    assert model.page == 13 and model.selectedDetail["title"] == "Step 101"
    model.goToStep("wrong")
    assert model.jumpError and model.page == 13
    model.goToPage(999)
    assert model.page == model.pageCount


def test_frame_updates_do_not_reset_delegates_or_repeat_noop_notifications():
    contexts, rows, tasks = flat_plan()
    model = DualGateSequenceModel()
    resets, changes = [], []
    model.modelReset.connect(lambda: resets.append(True))
    model.changed.connect(lambda: changes.append(True))
    kwargs = dict(acquisition_schedule=tasks, total_acq=5, current_seq_i=0, current_frame_total=3, run_outcome="running")
    model.update_plan(contexts, rows, **kwargs)
    resets.clear(); changes.clear()
    model.update_plan(contexts, rows, **kwargs)
    assert not resets and not changes
    model.update_plan(contexts, rows, current_frame_i=2, **kwargs)
    assert not resets and changes
    assert "frame 2/3" in model._rows[0]["acquisition"]


def test_large_nested_schedule_reads_only_the_page_and_selected_neighbor():
    template = nested_plan(1)[0]
    class Schedule:
        def __init__(self): self.reads = 0
        def __len__(self): return 100_000
        def __getitem__(self, index):
            if not 0 <= index < len(self): raise IndexError(index)
            self.reads += 1
            return dict(template, seq_i=index)
        def __iter__(self): raise AssertionError("Preview expanded the full schedule")
    tasks, contexts, rows = Schedule(), [], []
    model = DualGateSequenceModel(page_size=200)
    kwargs = dict(acquisition_schedule=tasks, total_acq=100_000)
    model.update_plan(contexts, rows, **kwargs)
    assert model.rowCount() == 200 and tasks.reads < 210
    tasks.reads = 0
    model.goToStep("100000")
    assert model.selectedDetail["title"] == "Step 100,000"
    assert tasks.reads < 210
    tasks.reads = 0
    model.update_plan(contexts, rows, done=5, **kwargs)
    assert tasks.reads < 210


def test_empty_and_disabled_filter_rules_are_explained():
    model = DualGateSequenceModel()
    model.update_plan([{}], [dict(Run=False, condition_label="disabled", When="False")], acquisition_schedule=[])
    assert model.rowCount() == 0 and model.pageCount == 1
    assert model.filters == [dict(condition="disabled", when="False", state="disabled")]
    model.goToStep("1")
    assert model.jumpError
    model.clear()
    assert model.rowCount() == 0 and not model.selectedDetail


def test_focus_and_following_have_explicit_nested_units_and_cross_page_boundary():
    tasks, contexts, rows = nested_plan(12), [], []
    tasks[7]["ctx"].update({"Center Wavelength (nm)": 750, "Exposure Time (ms)": 25})
    model = DualGateSequenceModel(page_size=8)
    model.update_plan(contexts, rows, acquisition_schedule=tasks, current_seq_i=7,
                      completed_points=7, run_outcome="running", done=2, total_acq=4,
                      preview_state="Applied sequence")
    focus = model.focusStep
    assert focus["caption"] == "Current step"
    assert focus["stepTitle"] == "Spectrum 8 / 12"
    assert focus["conditionLabel"] == "scan"
    assert focus["positionText"] == "Repeat 2 / 2 · Point 1 / 3"
    assert focus["acquisitionText"] == "1 spectrum · Power Off"
    assert dict((entry["label"], entry["value"]) for entry in focus["settings"]) == {
        "Rotation in": "90°", "Wavelength": "750 nm", "Exposure": "25 ms"}
    assert focus["gates"] == [{"label": "Vtg", "value": "0 V"}, {"label": "Vbg", "value": "0 V"}]
    assert model.followingStep["stepTitle"] == "Spectrum 9 / 12"
    assert "Vtg: 0 V → 1 V" in model.followingStep["changes"]
    assert model.canPreviousStep and model.canNextStep
    model.nextStep()
    assert model.page == 2 and model.selectedDetail["pageRow"] == 0
    assert model.focusStep["stepTitle"] == "Spectrum 9 / 12"
    assert model.focusStep["caption"] == "Selected step" and not model.followCurrent
    model.previousStep()
    assert model.page == 1 and model.selectedDetail["pageRow"] == 7
    assert model.overview == {
        "phase": "Running", "planState": "Applied sequence", "stepsText": "12 spectra",
        "filesText": "2 / 4 files", "unitHint": "Each step acquires one spectrum"}


def test_flat_cards_preserve_sweep_ends_counts_and_power_changes():
    contexts, rows, tasks = flat_plan()
    tasks = copy.deepcopy(tasks)
    tasks[1]["row"] = dict(tasks[1]["row"], frames=5, repeat=3, MeasurePower=True)
    tasks[1]["ctx"]["Exposure Time (ms)"] = 50
    model = DualGateSequenceModel()
    model.update_plan(contexts, rows, acquisition_schedule=tasks, current_seq_i=0,
                      current_frame_i=2, current_frame_total=3, run_outcome="running")
    focus, next_step = model.focusStep, model.followingStep
    assert focus["stepTitle"] == "Sweep 1 / 3"
    assert focus["positionText"] == "3 frames × 2 repeats"
    assert focus["gates"][0] == {"label": "Vtg", "value": "0 → 2 V"}
    assert "Full gate sweep" in focus["acquisitionText"] and "frame 2/3" in focus["acquisitionText"]
    assert focus["acquisitionText"].endswith("Power Off")
    assert next_step["stepTitle"] == "Sweep 2 / 3"
    assert "Frames: 3 → 5" in next_step["changes"]
    assert "Repeats: 2 → 3" in next_step["changes"]
    assert "Power: Off → On" in next_step["changes"]
    assert "Exposure: — → 50 ms" in next_step["changes"]
    model.nextStep()
    assert model.focusStep["acquisitionText"].endswith("Power On")
    assert "full gate sweep" in model.overview["unitHint"]
    assert model.overview["stepsText"] == "3 sweeps"


def test_changes_keep_occurrence_identity_with_equal_values_and_handle_nan_containers():
    tasks = nested_plan(2)
    tasks[0]["ctx"] = {"Rotation1 Angle (deg)": 0, "optional": float("nan"),
                        "metadata": {"position": [1, float("nan")]}}
    tasks[1]["ctx"] = copy.deepcopy(tasks[0]["ctx"])
    tasks[1]["group_instances"] = {"group:1": 1}
    model = DualGateSequenceModel()
    model.update_plan([], [], acquisition_schedule=tasks)
    assert model.focusStep["caption"] == "First step"
    assert model.followingStep["changes"] == ["New loop occurrence (same values)"]
    assert model.followingStep["unchangedText"] == "Other requested values unchanged"
    tasks[1]["group_instances"] = tasks[0]["group_instances"]
    tasks[1]["ctx"] = tasks[0]["ctx"]
    model.update_plan([], [], acquisition_schedule=tasks)
    assert model.followingStep["changes"] == []
    assert model.followingStep["unchangedText"] == "Requested values unchanged"
    tasks[1]["ctx"] = dict(tasks[0]["ctx"], metadata={"position": [2, float("nan")]})
    model.update_plan([], [], acquisition_schedule=tasks)
    assert len(model.followingStep["changes"]) == 1
    assert model.followingStep["changes"][0].startswith("metadata:")


def test_point_identity_changes_are_visible_even_when_targets_match():
    tasks = nested_plan(2)
    tasks[1]["ctx"] = tasks[0]["ctx"]
    tasks[1]["group_instances"] = tasks[0]["group_instances"]
    tasks[1]["gate_point"]["point_i"] = 1
    model = DualGateSequenceModel()
    model.update_plan([], [], acquisition_schedule=tasks)
    assert model.followingStep["changes"] == ["Point: 1/3 → 2/3"]
    assert model.followingStep["unchangedText"] == "Other requested values unchanged"


def test_changed_context_does_not_repeat_occurrence_explanation():
    model = DualGateSequenceModel()
    model.update_plan([], [], acquisition_schedule=nested_plan(2))
    assert model.followingStep["changes"] == ["Rotation in: 0° → 90°"]
    model.nextStep()
    assert model.focusStep["changes"] == ["Rotation in: 0° → 90°"]
    assert model.selectedDetail["loop"] == "RotIn=90"
    assert "RotIn: 0 → 90" in model.selectedDetail["changes"]


def test_focus_captions_and_empty_navigation_are_explicit():
    tasks, contexts, rows = nested_plan(12), [], []
    model = DualGateSequenceModel()
    model.update_plan(contexts, rows, acquisition_schedule=tasks)
    assert model.focusStep["caption"] == "First step"
    assert not model.canPreviousStep and model.canNextStep
    model.goToStep("10")
    assert model.focusStep["caption"] == "Selected step"
    for outcome, caption in (("running", "Current step"), ("failed", "Failed step"), ("stopped", "Stopped step")):
        model.update_plan(contexts, rows, acquisition_schedule=tasks, current_seq_i=1, run_outcome=outcome)
        assert model.focusStep["caption"] == "Selected step"
        model.setFollowCurrent(True)
        assert model.focusStep["caption"] == caption
        model.goToStep("10")
    model.setFollowCurrent(True)
    model.update_plan(contexts, rows, acquisition_schedule=tasks, run_outcome="completed")
    assert model.focusStep["caption"] == "Last step"
    assert model.focusStep["stepTitle"] == "Spectrum 12 / 12"
    assert not model.followingStep and not model.canNextStep
    model.clear()
    assert model.focusStep == model.followingStep == {}
    assert not model.canPreviousStep and not model.canNextStep
    model.previousStep()
    model.nextStep()
    assert model.rowCount() == 0 and model.overview["stepsText"] == "0 sweeps"


def test_following_is_a_real_execution_step_when_selected_candidate_was_skipped():
    contexts, rows, tasks = flat_plan()
    model = DualGateSequenceModel(page_size=2)
    model.update_plan(contexts, rows, acquisition_schedule=tasks)
    model.setShowSkipped(True)
    model.nextStep()
    assert model.focusStep["stepTitle"] == "Skipped combination"
    assert model.focusStep["statusKind"] == "skipped"
    assert "No acquisition" in model.focusStep["acquisitionText"]
    assert model.followingStep["stepTitle"] == "Sweep 2 / 3"
    model.nextStep()
    assert model.focusStep["stepTitle"] == "Sweep 2 / 3"


def test_bounded_cards_add_only_constant_neighbor_reads():
    template = nested_plan(1)[0]
    class Schedule:
        def __init__(self): self.reads = 0
        def __len__(self): return 100_000
        def __getitem__(self, index):
            if not 0 <= index < len(self): raise IndexError(index)
            self.reads += 1
            return dict(template, seq_i=index)
        def __iter__(self): raise AssertionError("Preview expanded the full schedule")
    tasks = Schedule()
    model = DualGateSequenceModel(page_size=200)
    model.update_plan([], [], acquisition_schedule=tasks)
    assert tasks.reads <= 205
    tasks.reads = 0
    model.goToStep("600")
    assert tasks.reads <= 203
    assert model.followingStep["stepTitle"] == "Spectrum 601 / 100,000"


def test_nested_order_path_follows_actual_interleaving_and_linked_groups():
    tasks = nested_plan(2)
    for task in tasks:
        task["execution_order"] = [dict(kind="group", id="linked", label="Bound settings"),
                                   *task["execution_order"]]
        task["group_values"]["linked"] = {"Center Wavelength (nm)": 750, "Exposure Time (ms)": 20}
        task["group_instances"]["linked"] = 0
    model = DualGateSequenceModel()
    model.update_plan([], [], acquisition_schedule=tasks, current_seq_i=1, run_outcome="running")
    path = model.focusStep["orderPath"]
    assert [node["kind"] for node in path] == ["group", "conditions", "points", "group", "acquire"]
    assert all(set(node) == {"label", "value", "detail", "kind", "fields"} for node in path)
    assert path[0] == {"label": "Linked settings", "value": "", "detail": "Values move together · Occurrence 1",
                       "kind": "group", "fields": [{"label": "Wavelength", "value": "750 nm"},
                                                     {"label": "Exposure", "value": "20 ms"}]}
    assert path[1]["value"] == "scan" and path[1]["detail"] == "Repeat 1 / 2"
    assert path[2]["value"] == "Point 1 / 3"
    assert path[2]["fields"][0] == {"label": "Vtg", "value": "0 V"}
    assert path[3]["label"] == "Rotation in" and path[3]["value"] == "90°"
    assert path[3]["detail"] == "Occurrence 2"
    assert path[-1]["label"] == "Acquire" and path[-1]["value"] == "1 spectrum"


def test_flat_order_path_grouping_and_live_counts_do_not_invent_static_position():
    for grouping, expected in (("loop_first", ["loop", "conditions"]), ("batch_first", ["conditions", "loop"])):
        contexts, rows, tasks = flat_plan(grouping)
        model = DualGateSequenceModel()
        model.update_plan(contexts, rows, acquisition_schedule=tasks, acquisition_grouping=grouping)
        path = model.focusStep["orderPath"]
        assert [node["kind"] for node in path] == [*expected, "repeat", "points", "acquire"]
        assert path[2]["value"] == "2 repeats"
        assert path[3]["value"] == "3 points per sweep"
        assert path[3]["fields"][0]["value"] == "0 → 2 V"
        assert "1 /" not in path[2]["value"] + path[3]["value"]
        model.update_plan(contexts, rows, acquisition_schedule=tasks, acquisition_grouping=grouping,
                          current_seq_i=0, current_rep_i=1, current_frame_i=2,
                          current_frame_total=3, run_outcome="running")
        path = model.focusStep["orderPath"]
        assert path[2]["value"] == "2 / 2"
        assert path[3]["value"] == "Frame 2 / 3"
        model.nextStep()
        assert model.focusStep["orderPath"][2]["value"].endswith("repeats")


def test_transition_names_outer_change_and_proves_inner_return_from_explicit_indices():
    tasks = nested_plan(3)
    # Gate points are outside rotation: the third acquisition advances a point
    # and explicitly returns the inner rotation occurrence to index zero.
    model = DualGateSequenceModel()
    model.update_plan([], [], acquisition_schedule=tasks)
    assert model.followingStep["transitionText"] == "Next Rotation in: 90°"
    model.nextStep()
    assert model.followingStep["transitionText"] == "Next Gate points: Point 2 / 3 · Rotation in returns to its first occurrence"
    # A lower inner occurrence index alone does not prove a return to the first.
    tasks = nested_plan(2)
    tasks[0]["group_instances"]["group:1"] = 3
    tasks[1]["group_instances"]["group:1"] = 1
    tasks[1]["gate_point"]["point_i"] = 1
    model.update_plan([], [], acquisition_schedule=tasks)
    assert model.followingStep["transitionText"] == "Next Gate points: Point 2 / 3"
    assert "returns" not in model.followingStep["transitionText"]


def test_transition_preserves_same_value_occurrences_and_unknown_identity():
    tasks = nested_plan(2)
    tasks[1]["ctx"] = tasks[0]["ctx"]
    tasks[1]["group_values"] = tasks[0]["group_values"]
    model = DualGateSequenceModel()
    model.update_plan([], [], acquisition_schedule=tasks)
    assert model.followingStep["transitionText"] == "Next Rotation in: 0° (new occurrence, same values)"
    tasks[1]["group_instances"] = tasks[0]["group_instances"]
    model.update_plan([], [], acquisition_schedule=tasks)
    assert model.followingStep["transitionText"] == "Next: acquire at the same requested values"


def test_transition_only_reports_known_condition_repeat_and_point_returns():
    tasks = nested_plan(2)
    order = [dict(kind="group", id="group:1"), dict(kind="conditions"), dict(kind="points")]
    for task in tasks:
        task["execution_order"] = order
    tasks[0].update(row_i=1, repeat_i=1)
    tasks[0]["gate_point"]["point_i"] = 2
    model = DualGateSequenceModel()
    model.update_plan([], [], acquisition_schedule=tasks)
    assert model.followingStep["transitionText"] == (
        "Next Rotation in: 90° · Condition returns to Row 1 · Repeat returns to 1 · Gate point returns to 1")


@pytest.mark.parametrize("grouping", ["loop_first", "batch_first"])
def test_prepared_flat_index_preserves_order_skips_progress_and_navigation(grouping):
    contexts, rows, tasks = flat_plan(grouping)
    prepared = prepare_flat_sequence_index(tasks, sequence_count=len(contexts), batch_count=len(rows), grouping=grouping)
    assert prepared.schedule is tasks
    before = copy.deepcopy(tasks)
    reference, model = DualGateSequenceModel(page_size=2), DualGateSequenceModel(page_size=2)
    kwargs = dict(acquisition_schedule=tasks, acquisition_grouping=grouping,
                  total_acq=5, done=2, current_seq_i=1, run_outcome="running")
    reference.update_plan(contexts, rows, **kwargs)
    model.update_plan(contexts, rows, prepared_sequence_index=prepared, **kwargs)
    assert model._lookup is prepared.lookup and model._prefix is prepared.prefix
    assert model._execution_positions is prepared.execution_positions
    for operation, args in (("setShowSkipped", (True,)), ("nextStep", ()), ("goToStep", ("3",)),
                            ("previousPage", ()), ("setFollowCurrent", (True,))):
        getattr(reference, operation)(*args)
        getattr(model, operation)(*args)
        assert model._rows == reference._rows
        assert model.selectedDetail == reference.selectedDetail
        assert model.focusStep == reference.focusStep
        assert model.followingStep == reference.followingStep
    with pytest.raises(TypeError):
        prepared.lookup[0] = 99
    with pytest.raises(TypeError):
        prepared.prefix[0] = 99
    with pytest.raises(FrozenInstanceError):
        prepared.schedule_count = 99
    model.clear()
    assert prepared.prefix[0] == 0 and prepared.schedule is tasks and tasks == before


def test_prepared_flat_index_rejects_other_schedule_and_incompatible_dimensions():
    contexts, rows, tasks = flat_plan()
    prepared = prepare_flat_sequence_index(tasks, sequence_count=len(contexts), batch_count=len(rows))
    for schedule, sequence, batch, grouping in (
            (copy.deepcopy(tasks), contexts, rows, "loop_first"),
            (tasks, contexts + [{}], rows, "loop_first"),
            (tasks, contexts, rows[:1], "loop_first"),
            (tasks, contexts, rows, "batch_first")):
        model = DualGateSequenceModel()
        model.update_plan(sequence, batch, acquisition_schedule=schedule,
                          acquisition_grouping=grouping, prepared_sequence_index=prepared)
        assert model._lookup is not prepared.lookup and model._prefix is not prepared.prefix
    tasks.append(copy.deepcopy(tasks[-1]))
    model = DualGateSequenceModel()
    model.update_plan(contexts, rows, acquisition_schedule=tasks, prepared_sequence_index=prepared)
    assert model._lookup is not prepared.lookup


def test_prepared_flat_index_install_reads_only_page_and_neighbors():
    class Schedule:
        def __init__(self): self.reads = 0; self.can_iterate = True
        def __len__(self): return 100_000
        def __getitem__(self, index):
            if not 0 <= index < len(self): raise IndexError(index)
            self.reads += 1
            return dict(seq_i=index, row_i=0, ctx={"Center Wavelength (nm)": 750},
                        row=dict(condition_label="scan", frames=1, repeat=1))
        def __iter__(self):
            assert self.can_iterate, "GUI installation iterated the full schedule"
            for index in range(len(self)):
                yield self[index]
    tasks = Schedule()
    prepared = prepare_flat_sequence_index(tasks, sequence_count=len(tasks), batch_count=1)
    assert tasks.reads == len(tasks)
    tasks.can_iterate, tasks.reads = False, 0
    contexts = range(len(tasks))
    model = DualGateSequenceModel(page_size=8)
    model.update_plan(contexts, [dict(Run=True)], acquisition_schedule=tasks,
                      prepared_sequence_index=prepared, current_seq_i=49_999, run_outcome="running")
    assert tasks.reads <= 12
    assert model.focusStep["stepTitle"] == "Sweep 50,000 / 100,000"
    assert model.followingStep["stepTitle"] == "Sweep 50,001 / 100,000"
    tasks.reads = 0
    model.setShowSkipped(True)
    model.goToStep("100000")
    assert tasks.reads <= 24


def test_prepared_flat_index_cancels_without_publishing_partial_data():
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(InterruptedError, match="cancelled"):
        prepare_flat_sequence_index([dict(seq_i=0, row_i=0)], sequence_count=1,
                                    batch_count=1, cancelled=cancelled)
    with pytest.raises(InterruptedError, match="cancelled"):
        prepare_flat_sequence_index([], sequence_count=0, batch_count=0, cancelled=cancelled)


@pytest.mark.parametrize("index_first", [False, True])
def test_deepcopy_preserves_prepared_index_binding_without_mutating_original(index_first):
    contexts, rows, tasks = flat_plan()
    before = copy.deepcopy(tasks)
    prepared = prepare_flat_sequence_index(tasks, sequence_count=len(contexts), batch_count=len(rows))
    entries = [("acquisition_schedule", tasks), ("prepared_sequence_index", prepared)]
    snapshot = dict(reversed(entries) if index_first else entries)
    snapshot["same_index"] = prepared
    copied = copy.deepcopy(snapshot)
    copied_index, copied_schedule = copied["prepared_sequence_index"], copied["acquisition_schedule"]
    assert copied_index is not prepared and copied_index is copied["same_index"]
    assert copied_index.schedule is copied_schedule and copied_schedule is not tasks
    assert copied_index.matches(copied_schedule, len(contexts), len(rows), "loop_first")
    assert copied_index.lookup is prepared.lookup
    assert copied_index.prefix is prepared.prefix
    assert copied_index.execution_positions is prepared.execution_positions
    model = DualGateSequenceModel()
    model.update_plan(contexts, rows, **{key: copied[key] for key, _value in entries})
    assert model._lookup is copied_index.lookup and model._prefix is copied_index.prefix
    copied_schedule[0]["ctx"]["Center Wavelength (nm)"] = 760
    assert tasks == before and prepared.schedule is tasks
    with pytest.raises(TypeError):
        copied_index.lookup[0] = 99


def test_deepcopy_prepared_index_handles_schedule_back_reference():
    contexts, rows, tasks = flat_plan()
    prepared = prepare_flat_sequence_index(tasks, sequence_count=len(contexts), batch_count=len(rows))
    tasks[0]["presentation_index"] = prepared
    copied = copy.deepcopy(prepared)
    assert copied.schedule[0]["presentation_index"] is copied
    assert copied.schedule is not tasks and prepared.schedule is tasks
