"""Local metadata timing must not consume or reset the guarded SDK allowance."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import lightfield_capture_guard as guard_module
from app.lightfield_capture_guard import (
    LightFieldAcquisitionStalled, LightFieldCaptureGuard,
    LightFieldCaptureOwnershipError, LightFieldMetadataStalled,
    capture_operation, capture_status, get_capture_guard,
)
from app.lightfield_metadata import managed_capture_dataset


class Clock:
    now = 100.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(guard_module, "time", value)
    monkeypatch.setattr(LightFieldCaptureGuard, "_watch", lambda *_: None)
    return value


class Recorder:
    active = True

    def __init__(self):
        self.events = []
        self.begin_work = lambda: None
        self.finish_work = lambda: None
        self.run = SimpleNamespace(mark_metadata_failure=Mock())
        self._run = lambda: self.run

    def begin(self, snapshot, frames, purpose):
        self.begin_work()
        self.events.append("capture_started")
        return {"acquisition_id": "test-capture"}

    def finish(self, record, *, error=None, **kwargs):
        if error is None:
            self.finish_work()
        self.events.append("capture_failed" if error else "capture_completed")


def make_setup(tmp_path, *, budget=1.0):
    dataset = SimpleNamespace(GetFrame=Mock(return_value=object()), Dispose=Mock())
    setup = SimpleNamespace(
        _capture_wait_timeout_s=budget,
        _capture_diagnostics_path=tmp_path / "capture.jsonl",
        _metadata_recorder=Recorder(),
        read_metadata_snapshot=Mock(return_value={"captured_utc": "test"}),
        verify_acquisition_settings=Mock(),
        _frame_dims=lambda _: (3, 1),
        experiment=SimpleNamespace(Capture=Mock(return_value=dataset)),
    )
    return setup, dataset


def acquire(setup):
    with managed_capture_dataset(setup, 1) as (dataset, _):
        return dataset


def test_slow_begin_and_finish_keep_sdk_budget_and_dispose_once(clock, tmp_path):
    setup, dataset = make_setup(tmp_path)
    recorder = setup._metadata_recorder
    recorder.begin_work = lambda: clock.advance(40)
    recorder.finish_work = lambda: clock.advance(50)
    setup.experiment.Capture.side_effect = lambda _: clock.advance(.25) or dataset

    assert acquire(setup) is dataset
    status = capture_status(setup)
    assert not status["stalled"] and not status["active"]
    assert status["sdk_elapsed_s"] == pytest.approx(.25)
    assert status["metadata_elapsed_s"] == pytest.approx(90)
    assert status["elapsed_s"] == pytest.approx(90.25)
    dataset.Dispose.assert_called_once()


def test_local_work_preserves_prior_sdk_time_instead_of_resetting_budget(clock, tmp_path):
    setup, dataset = make_setup(tmp_path)
    setup.read_metadata_snapshot.side_effect = lambda: clock.advance(.6) or {}
    setup._metadata_recorder.begin_work = lambda: clock.advance(40)
    setup.experiment.Capture.side_effect = lambda _: clock.advance(.5) or dataset

    with pytest.raises(LightFieldAcquisitionStalled) as caught:
        acquire(setup)
    assert not isinstance(caught.value, LightFieldMetadataStalled)
    status = capture_status(setup)
    assert status["timeout_domain"] == "sdk"
    assert status["stalled_phase"] == "capture"
    assert status["sdk_elapsed_s"] == pytest.approx(1.1)
    dataset.Dispose.assert_called_once()


@pytest.mark.parametrize("phase", ["begin", "finish"])
def test_local_timeout_is_classified_and_diagnostic_uses_local_budget(clock, tmp_path, monkeypatch, phase):
    monkeypatch.setattr(guard_module, "METADATA_WAIT_TIMEOUT_S", 2.0)
    setup, dataset = make_setup(tmp_path)
    setattr(setup._metadata_recorder, phase + "_work", lambda: clock.advance(3))

    with pytest.raises(LightFieldMetadataStalled, match="metadata persistence"):
        acquire(setup)
    status = capture_status(setup)
    assert status["timeout_domain"] == "metadata"
    assert status["stalled_phase"] == ("record_capture_started" if phase == "begin" else "record_capture_completed")
    assert status["timeout_s"] == status["metadata_timeout_s"] == 2
    assert status["sdk_timeout_s"] == 1
    assert status["wait_elapsed_s"] == 3
    assert status["recovery_required"] and not status["active"]
    assert get_capture_guard(setup)._current._diagnostic_done.wait(2)
    diagnostic = json.loads((tmp_path / "capture.jsonl").read_text().splitlines()[0])
    assert diagnostic["timeout_domain"] == "metadata"
    assert diagnostic["timeout_s"] == 2
    assert diagnostic["stalled_phase"] == status["stalled_phase"]
    if phase == "begin":
        setup.experiment.Capture.assert_not_called()
        dataset.Dispose.assert_not_called()
    else:
        dataset.Dispose.assert_called_once()


def test_snapshot_sdk_getter_keeps_original_guard(clock, tmp_path):
    setup, dataset = make_setup(tmp_path)
    setup.read_metadata_snapshot.side_effect = lambda: clock.advance(2) or {}
    with pytest.raises(LightFieldAcquisitionStalled) as caught:
        acquire(setup)
    assert not isinstance(caught.value, LightFieldMetadataStalled)
    assert capture_status(setup)["stalled_phase"] == "metadata_snapshot"
    assert capture_status(setup)["timeout_domain"] == "sdk"
    setup.experiment.Capture.assert_not_called()
    dataset.Dispose.assert_not_called()


def test_slow_failure_marker_is_local_and_does_not_poison_sdk(clock, tmp_path):
    setup, dataset = make_setup(tmp_path)
    setup.read_metadata_snapshot.side_effect = OSError("optional readback failed")
    setup._metadata_recorder.run.mark_metadata_failure.side_effect = lambda *a, **k: clock.advance(40)
    assert acquire(setup) is dataset
    assert not capture_status(setup)["stalled"]
    assert capture_status(setup)["metadata_elapsed_s"] == 40


def test_failed_local_scope_restores_time_and_preserves_original_exception(clock, tmp_path):
    setup, _ = make_setup(tmp_path)
    with capture_operation(setup) as operation:
        with pytest.raises(OSError, match="disk full"):
            with operation.local_metadata("record_capture_started"):
                clock.advance(40)
                raise OSError("disk full")
        assert capture_status(setup)["active_domain"] == "sdk"
        clock.advance(.5)
        operation.check()
    assert not capture_status(setup)["recovery_required"]


def test_expired_sdk_cannot_be_rescued_and_failure_logging_keeps_first_incident(clock, tmp_path, monkeypatch):
    monkeypatch.setattr(guard_module, "METADATA_WAIT_TIMEOUT_S", 2.0)
    setup, _ = make_setup(tmp_path)
    with pytest.raises(LightFieldAcquisitionStalled) as caught:
        with capture_operation(setup) as operation:
            operation.stage("capture")
            clock.advance(2)
            with pytest.raises(LightFieldAcquisitionStalled):
                with operation.local_metadata("record_capture_started"):
                    pytest.fail("expired SDK must not enter local work")
            first = capture_status(setup)
            with operation.local_metadata("record_capture_failed", check=False):
                clock.advance(10)
            after = capture_status(setup)
            assert after["incident_id"] == first["incident_id"]
            assert after["timeout_domain"] == "sdk"
            assert after["stalled_phase"] == "capture"
    assert not isinstance(caught.value, LightFieldMetadataStalled)


def test_local_scope_blocks_sdk_access_and_keeps_nested_budget(clock, tmp_path):
    setup, _ = make_setup(tmp_path)
    guard = get_capture_guard(setup)
    with capture_operation(setup) as operation:
        with operation.local_metadata("outer"):
            with pytest.raises(LightFieldCaptureOwnershipError):
                guard.check_close()
            with pytest.raises(LightFieldCaptureOwnershipError):
                guard.check_available()
            with pytest.raises(LightFieldCaptureOwnershipError):
                operation.stage("capture")
            clock.advance(40)
            with operation.local_metadata("inner"):
                clock.advance(30)
            assert capture_status(setup)["phase"] == "outer"
        assert capture_status(setup)["metadata_elapsed_s"] == 70
    assert not capture_status(setup)["recovery_required"]


def test_frame_count_refinement_includes_only_completed_local_time(clock, tmp_path):
    setup = SimpleNamespace(_acquisition_recipe={"exposure_ms": 1000, "frames": 10})
    with capture_operation(setup) as operation:
        started = operation.started
        with operation.local_metadata("record_capture_started"):
            clock.advance(40)
        operation.set_frames(3)
        assert operation.timeout_s == 105
        assert operation.deadline == started + 40 + 105


def test_independent_monitor_classifies_blocked_local_io_and_keeps_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(guard_module, "METADATA_WAIT_TIMEOUT_S", .15)
    setup, _ = make_setup(tmp_path, budget=1)
    entered, release = threading.Event(), threading.Event()
    operations, failures = [], []

    def owner():
        try:
            with capture_operation(setup) as operation:
                operations.append(operation)
                with operation.local_metadata("record_capture_started"):
                    entered.set()
                    assert release.wait(3)
        except Exception as exc:
            failures.append(exc)

    thread = threading.Thread(target=owner)
    thread.start()
    try:
        assert entered.wait(1)
        assert operations[0]._diagnostic_done.wait(2)
        status = capture_status(setup)
        assert thread.is_alive() and status["active"]
        assert status["timeout_domain"] == "metadata"
        assert status["wait_elapsed_s"] >= .15
        with pytest.raises(LightFieldCaptureOwnershipError):
            get_capture_guard(setup).check_close()
        with pytest.raises(LightFieldMetadataStalled):
            with capture_operation(setup):
                pytest.fail("another capture must remain blocked")
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], LightFieldMetadataStalled)
    assert not capture_status(setup)["active"]
    assert capture_status(setup)["recovery_required"]


def test_monitor_resumes_short_sdk_deadline_after_longer_local_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(guard_module, "METADATA_WAIT_TIMEOUT_S", 2)
    # Keep the real monitor thread, but control elapsed domain time: Windows
    # Event.wait(.5) can return just before .5 seconds on the monotonic clock.
    clock = Clock()
    monkeypatch.setattr(guard_module, "time", clock)
    setup, _ = make_setup(tmp_path, budget=.4)
    entered_local, release_local = threading.Event(), threading.Event()
    entered_sdk, release_sdk = threading.Event(), threading.Event()
    operations, failures = [], []

    def owner():
        try:
            with capture_operation(setup) as operation:
                operations.append(operation)
                with operation.local_metadata("record_capture_started"):
                    entered_local.set()
                    assert release_local.wait(3)
                operation.stage("capture")
                entered_sdk.set()
                assert release_sdk.wait(3)
        except Exception as exc:
            failures.append(exc)

    thread = threading.Thread(target=owner)
    thread.start()
    try:
        assert entered_local.wait(1)
        clock.advance(.6)
        operations[0]._wake.set()
        assert not operations[0]._diagnostic_done.wait(.05)
        assert not capture_status(setup)["stalled"]
        release_local.set()
        assert entered_sdk.wait(1)
        clock.advance(.5)
        assert operations[0]._diagnostic_done.wait(1.5)
        status = capture_status(setup)
        assert status["timeout_domain"] == "sdk"
        assert status["stalled_phase"] == "capture"
        assert status["metadata_elapsed_s"] == pytest.approx(.6)
        assert status["sdk_elapsed_s"] == pytest.approx(.5)
    finally:
        release_local.set()
        release_sdk.set()
        thread.join(2)
    assert len(failures) == 1 and not isinstance(failures[0], LightFieldMetadataStalled)
