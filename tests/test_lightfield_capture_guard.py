"""Offline capture ownership/deadline tests. No Qt, pythonnet or SDK objects."""
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import weakref

import pytest

from app.lightfield_capture_guard import (
    LightFieldAcquisitionStalled, LightFieldCaptureGuard,
    LightFieldCaptureOwnershipError, capture_operation, capture_status,
    get_capture_guard,
)


class Setup:
    def __getattr__(self, name):
        raise AssertionError(f"No SDK lookup is permitted: {name}")


def make_setup(**values):
    setup = Setup()
    setup.__dict__.update(values)
    return setup


def expire(op):
    with op._guard._lock:
        op.deadline = time.monotonic() - 1


@pytest.mark.parametrize("late_error", [False, True])
def test_blocked_capture_is_quarantined_without_cancel_and_late_result_is_rejected(tmp_path, late_error):
    setup = make_setup(_capture_wait_timeout_s=.05,
                       _capture_diagnostics_path=tmp_path / "capture.jsonl")
    started, release = threading.Event(), threading.Event()
    ops, errors, commands = [], [], []

    def capture():
        try:
            with capture_operation(setup) as op:
                ops.append(op)
                op.stage("capture")
                commands.append("Capture")
                started.set()
                try:
                    assert release.wait(3)
                    if late_error:
                        raise ValueError("SDK returned late failure")
                finally:
                    op.stage("dispose", check=False)
                    commands.append("Dispose")
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=capture)
    thread.start()
    try:
        assert started.wait(1)
        op = ops[0]
        assert op._diagnostic_done.wait(2), "Independent monitor must detect the blocked owner"
        state = capture_status(setup)
        assert state["active"] and state["stalled"] and state["recovery_required"]
        assert state["phase"] == state["stalled_phase"] == "capture"
        assert thread.is_alive() and commands == ["Capture"]
        with pytest.raises(LightFieldAcquisitionStalled):
            with capture_operation(setup):
                pytest.fail("quarantined capture must never start")
        records = [json.loads(line) for line in (tmp_path / "capture.jsonl").read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["event"] == "lightfield_capture_stalled"
        assert str(thread.ident) in records[0]["thread_stacks"]
        assert set(records[0]["thread_stacks"][str(thread.ident)][-1]) == {"file", "line", "function"}
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], LightFieldAcquisitionStalled)
    if late_error:
        assert isinstance(errors[0].__cause__, ValueError)
    assert commands == ["Capture", "Dispose"]
    state = capture_status(setup)
    assert not state["active"] and state["recovery_required"]
    assert state["phase"] == "dispose" and state["stalled_phase"] == "capture"
    assert len((tmp_path / "capture.jsonl").read_text().splitlines()) == 1


def test_nested_helpers_share_token_deadline_and_ownership():
    setup = make_setup()
    with capture_operation(setup, purpose="measurement") as outer:
        deadline = outer.deadline
        with capture_operation(setup) as inner:
            assert inner is outer and inner.depth == 2
            inner.stage("capture")
        assert capture_status(setup)["active"]
        assert outer.depth == 1 and outer.deadline == deadline
    assert not capture_status(setup)["active"]
    assert not capture_status(setup)["recovery_required"]


def test_other_thread_cannot_enter_update_or_check_active_operation():
    setup = make_setup()
    errors = []
    with capture_operation(setup) as op:
        def compete():
            for action in (lambda: get_capture_guard(setup)._enter(setup, 1, "measurement"),
                           lambda: op.stage("wrong"), op.check,
                           lambda: op.update_context(point_index=999)):
                try:
                    action()
                except Exception as exc:
                    errors.append(exc)
        thread = threading.Thread(target=compete)
        thread.start()
        thread.join(1)
        assert not thread.is_alive()
        assert len(errors) == 4
        assert all(isinstance(exc, LightFieldCaptureOwnershipError) for exc in errors)
        assert capture_status(setup)["phase"] == "starting"
        assert op.depth == 1


def test_stale_operation_and_watcher_cannot_poison_a_new_capture():
    setup = make_setup()
    guard = get_capture_guard(setup)
    with capture_operation(setup) as old:
        pass
    with capture_operation(setup) as current:
        old.deadline = time.monotonic() - 1
        with pytest.raises(LightFieldCaptureOwnershipError):
            old.stage("stale")
        with pytest.raises(LightFieldCaptureOwnershipError):
            old.check()
        # Simulate a watcher waking after its operation has been replaced.
        old._done.clear()
        guard._watch(old)
        assert not guard._expire_locked(old, time.monotonic())
        assert capture_status(setup)["operation_token"] == current.token
        assert not capture_status(setup)["stalled"]


@pytest.mark.parametrize("recipe,frames,expected", [
    ({"exposure_ms": 300, "frames": 10}, 1, 30),
    ({"exposure_ms": 10000, "frames": 10}, 4, 1215),
    ({}, 1, 30),
])
def test_budget_uses_cached_exposure_accumulations_and_requested_frames(recipe, frames, expected):
    setup = make_setup(_acquisition_recipe=recipe)
    with capture_operation(setup, frames=frames) as op:
        assert op.timeout_s == expected
    assert not capture_status(setup)["stalled"]


def test_explicit_wait_budget_supports_unknown_external_trigger():
    setup = make_setup(_capture_wait_timeout_s=3600)
    with capture_operation(setup) as op:
        assert op.timeout_s == 3600


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "bad"])
def test_invalid_override_rejected_before_operation_starts(value):
    setup = make_setup(_capture_wait_timeout_s=value)
    with pytest.raises(ValueError, match="finite and positive"):
        with capture_operation(setup):
            pytest.fail("invalid budget must not reach SDK")
    assert not capture_status(setup)["active"]


def test_completion_checks_absolute_deadline_even_when_watcher_is_delayed(monkeypatch):
    monkeypatch.setattr(LightFieldCaptureGuard, "_watch", lambda *_: None)
    setup = make_setup()
    with pytest.raises(LightFieldAcquisitionStalled, match="Hardware stop is not confirmed"):
        with capture_operation(setup) as op:
            op.stage("capture")
            expire(op)
    assert op._diagnostic_done.wait(1)
    assert capture_status(setup)["recovery_required"]
    assert not capture_status(setup)["active"]


def test_cleanup_is_allowed_after_expiration_but_normal_stage_is_rejected():
    setup = make_setup()
    cleanup = []
    with pytest.raises(LightFieldAcquisitionStalled):
        with capture_operation(setup) as op:
            op.stage("capture")
            expire(op)
            try:
                op.stage("post_verify")
            finally:
                op.stage("dispose", check=False)
                cleanup.append("Dispose")
    assert cleanup == ["Dispose"]
    assert op._diagnostic_done.wait(1)
    state = capture_status(setup)
    assert state["stalled_phase"] == "capture" and state["phase"] == "dispose"


def test_timeout_during_nested_entry_does_not_leak_depth_or_active_owner():
    setup = make_setup()
    with pytest.raises(LightFieldAcquisitionStalled):
        with capture_operation(setup) as op:
            expire(op)
            with capture_operation(setup):
                pytest.fail("expired nested operation cannot start")
    assert op.depth == 0
    assert not capture_status(setup)["active"]
    assert op._diagnostic_done.wait(1)


def test_stall_is_sticky_until_setup_is_replaced():
    setup = make_setup()
    with pytest.raises(LightFieldAcquisitionStalled):
        with capture_operation(setup) as op:
            expire(op)
    assert op._diagnostic_done.wait(1)
    with pytest.raises(LightFieldAcquisitionStalled):
        with capture_operation(setup):
            pytest.fail("old connection remains quarantined")
    with capture_operation(make_setup()):
        pass


def test_original_non_timeout_exception_is_preserved():
    setup = make_setup()
    original = ValueError("SDK failure")
    with pytest.raises(ValueError) as caught:
        with capture_operation(setup):
            raise original
    assert caught.value is original
    assert not capture_status(setup)["active"]
    assert not capture_status(setup)["recovery_required"]


def test_diagnostic_write_failure_does_not_release_quarantine(tmp_path):
    setup = make_setup(_capture_diagnostics_path=tmp_path)  # opening a directory fails
    with pytest.raises(LightFieldAcquisitionStalled):
        with capture_operation(setup) as op:
            expire(op)
    assert op._diagnostic_done.wait(1)
    state = capture_status(setup)
    assert state["stalled"] and state["recovery_required"]
    assert state["diagnostic_error"]


def test_cached_metadata_context_is_allowlisted_and_snapshot_is_independent():
    class Run:
        experiment_id = "not-an-instance-attribute"
    run = Run()
    run.experiment_id = "run-123"
    recorder = SimpleNamespace(_run=weakref.ref(run), context={
        "point_index": 134, "output_file": Path("data.csv"), "password": "secret",
        "acquisition_id": "capture-123", "unsafe": object(),
    })
    setup = make_setup(_metadata_recorder=recorder)
    assert capture_status(setup) == {}
    with capture_operation(setup) as op:
        op.update_context(acquisition_id="capture-456", token="secret")
        state = capture_status(setup)
        assert state["context"]["run_id"] == "run-123"
        assert state["context"]["acquisition_id"] == "capture-456"
        assert state["context"]["output_file"] == "data.csv"
        assert "password" not in state["context"] and "token" not in state["context"]
        state["context"]["point_index"] = 99
        assert capture_status(setup)["context"]["point_index"] == 134


def test_mock_diagnostics_are_opt_in_and_real_setup_path_is_local(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    for setup, expected in ((make_setup(), None), (make_setup(auto=object()), tmp_path)):
        with capture_operation(setup) as op:
            if expected is None:
                assert op.diagnostic_path is None
            else:
                assert op.diagnostic_path.parent == tmp_path / "SpectralSweep" / "diagnostics"


def test_setting_and_close_guards_distinguish_owner_and_drained_quarantine():
    setup = make_setup()
    guard = get_capture_guard(setup)
    guard.check_available()
    guard.check_close()
    with pytest.raises(LightFieldAcquisitionStalled):
        with capture_operation(setup) as op:
            guard.check_available()  # owner can run its own required settings
            with pytest.raises(LightFieldCaptureOwnershipError, match="still active"):
                guard.check_close()
            op.quarantine("dataset Dispose failed")
            with pytest.raises(LightFieldAcquisitionStalled, match="Dispose failed"):
                guard.check_available()
    assert op._diagnostic_done.wait(1)
    guard.check_close()  # explicit disconnect can recover a drained fault
    with pytest.raises(LightFieldAcquisitionStalled):
        guard.check_available()


def test_frame_count_refines_budget_without_restarting_start_time():
    setup = make_setup(_acquisition_recipe={"exposure_ms": 1000, "frames": 10})
    with capture_operation(setup) as op:
        started = op.started
        assert op.timeout_s == 45
        op.set_frames(3)
        assert op.started == started and op.deadline == started + 105
        assert capture_status(setup)["context"]["capture_frames_requested"] == 3


def test_frame_count_cannot_resurrect_expired_operation():
    setup = make_setup(_acquisition_recipe={"exposure_ms": 1000, "frames": 10})
    with pytest.raises(LightFieldAcquisitionStalled):
        with capture_operation(setup) as op:
            expire(op)
            op.set_frames(10000)
    assert op._diagnostic_done.wait(1)
    assert op.timeout_s == 45


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_final_handoff_timeout_runs_cleanup_while_still_owned(monkeypatch, cleanup_fails):
    monkeypatch.setattr(LightFieldCaptureGuard, "_watch", lambda *_: None)
    setup = make_setup()
    calls = []

    def dispose(op):
        assert capture_status(setup)["active"]
        op.stage("dispose", check=False)
        calls.append("Dispose")
        with pytest.raises(LightFieldCaptureOwnershipError):
            get_capture_guard(setup).check_close()
        if cleanup_fails:
            raise RuntimeError("Dispose failed")

    with pytest.raises(LightFieldAcquisitionStalled) as caught:
        with capture_operation(setup, on_stalled_exit=dispose) as op:
            op.stage("return_dataset")
            expire(op)  # Only the final context boundary detects this.
    assert op._diagnostic_done.wait(1)
    assert calls == ["Dispose"] and not capture_status(setup)["active"]
    if cleanup_fails:
        assert isinstance(caught.value.__cause__, RuntimeError)


def test_healthy_handoff_does_not_dispose_transferred_result():
    setup = make_setup()
    calls = []
    with capture_operation(setup, on_stalled_exit=lambda _: calls.append("Dispose")):
        pass
    assert calls == []
