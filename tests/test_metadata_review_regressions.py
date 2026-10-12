"""Metadata integrity regressions use fake captures and isolated history files."""
from __future__ import annotations

import gc
import threading
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from app import experiment_metadata as metadata_module
from app.devices.andor_adapter import AndorSDK2Setup
from app.experiment_metadata import ExperimentMetadataService
from app.lightfield_metadata import LightFieldRecorder


@pytest.fixture
def service(tmp_path, monkeypatch):
    history = tmp_path / "local_review_metadata_history.sqlite"
    monkeypatch.setenv("SPECTRALSWEEP_HISTORY_PATH", str(history))
    return ExperimentMetadataService(tmp_path, history)


def snapshot(backend="lightfield", exposure=10, observed="first"):
    return {"captured_utc": observed, "identity": {"backend": backend},
            "observed": {"exposure_ms": exposure}}


def fake_andor(run):
    return SimpleNamespace(
        _abort=threading.Event(), _busy=threading.Event(),
        _exposure_ms=10., _accumulations=1,
        options=SimpleNamespace(operation_timeout_s=1., timeout_margin_s=1.),
        _metadata_recorder=LightFieldRecorder(run),
        read_metadata_snapshot=lambda: snapshot("andor_sdk2"),
        _call=lambda *args, **kwargs: np.array([[1., 2.]]),
        _capture_frames_on_owner=lambda: None,
    )


def test_andor_manifest_failure_cannot_terminalize_as_completed(service):
    run = service.begin("andor_audit", "fake-sample")
    setup = fake_andor(run)
    with patch.object(metadata_module, "_atomic_json", side_effect=OSError("transient disk failure")):
        frame = AndorSDK2Setup.acquire_2d(setup)
    assert frame.tolist() == [[1., 2.]]
    assert not setup._busy.is_set()
    assert run.read_events() == []
    terminal = run.complete()
    saved = service.load_metadata(run.path)
    assert terminal["status"] == saved["status"] == "failed"
    assert saved["metadata_status"] == "degraded"
    assert saved["metadata_error"]["event"] == "capture_started"


def test_andor_result_storage_failure_is_visible_after_detector_cleanup(service):
    run = service.begin("andor_audit", "fake-sample")
    setup = fake_andor(run)
    append = setup._metadata_recorder._append
    def fail_result(record):
        if record["event"] == "capture_completed":
            raise OSError("result journal unavailable")
        return append(record)
    with patch.object(setup._metadata_recorder, "_append", side_effect=fail_result):
        frame = AndorSDK2Setup.acquire_2d(setup)
    assert frame.tolist() == [[1., 2.]]
    assert not setup._busy.is_set()
    terminal = run.complete()
    assert terminal["status"] == "failed"
    assert terminal["metadata_error"]["event"] == "capture_completed"


def test_optional_andor_readback_remains_tolerated(service):
    run = service.begin("andor_audit", "fake-sample")
    setup = fake_andor(run)
    # Optional fields report unavailability inside their snapshot. This is
    # different from failing to durably store that valid snapshot.
    setup.read_metadata_snapshot = lambda: {
        "captured_utc": "attempt", "identity": {"backend": "andor_sdk2"},
        "observed": {}, "unavailable": {"control_snapshot": "optional read failed"},
    }
    assert AndorSDK2Setup.acquire_2d(setup).tolist() == [[1., 2.]]
    assert not setup._busy.is_set()
    assert run.complete()["status"] == "completed"
    assert [event["event"] for event in run.read_events()] == ["capture_started", "capture_completed"]


def test_multi_setup_compatibility_references_do_not_overwrite_each_other(service):
    run = service.begin("multi_setup_audit", "fake-sample")
    first, second = LightFieldRecorder(run), LightFieldRecorder(run)
    first.finish(first.begin(snapshot("lightfield", 10, "first"), 1, "measurement"))
    second.finish(second.begin(snapshot("andor_sdk2", 20, "second"), 1, "measurement"))
    saved = service.load_metadata(run.path)
    compatibility = saved["observed"]["lightfield"]["settings_snapshots"]
    assert [(row["settings_id"], row["shared_settings_id"]) for row in compatibility] == [(1, 1), (2, 2)]
    assert [(row["settings_id"], row["values"]["identity"]["backend"])
            for row in saved["settings"]["snapshots"]] == [(1, "lightfield"), (2, "andor_sdk2")]
    starts = [event for event in run.read_events() if event["event"] == "capture_started"]
    assert [event["settings_id"] for event in starts] == [1, 2]


def test_reconstructed_recorder_reuses_compatibility_link(service):
    run = service.begin("multi_setup_audit", "fake-sample")
    first = LightFieldRecorder(run)
    first.finish(first.begin(snapshot(), 1, "measurement"))
    rebuilt = LightFieldRecorder(run)
    rebuilt.finish(rebuilt.begin(snapshot(observed="later"), 1, "measurement"))
    run.complete()
    compatibility = service.load_metadata(run.path)["observed"]["lightfield"]["settings_snapshots"]
    assert len(compatibility) == 1
    assert compatibility[0]["settings_id"] == compatibility[0]["shared_settings_id"] == 1


def test_legacy_compatibility_rows_survive_new_settings_allocation(service):
    run = service.begin("legacy_audit", "fake-sample")
    legacy = {"settings_id": 7, "first_observed_utc": "old", "values": {"exposure_ms": 3}}
    run.update_observed({"lightfield": {"settings_snapshots": [legacy]}})
    recorder = LightFieldRecorder(run)
    recorder.finish(recorder.begin(snapshot(), 1, "measurement"))
    rows = service.load_metadata(run.path)["observed"]["lightfield"]["settings_snapshots"]
    assert rows[0] == legacy
    assert rows[1]["settings_id"] == 8
    assert rows[1]["shared_settings_id"] == 1


def test_reopen_preserves_hardware_capture_count_and_output_links(service):
    run = service.begin("recovery_audit", "fake-sample")
    recorder = LightFieldRecorder(run)
    for index in range(2):
        recorder.context = {"acquisition_id": f"hardware-{index}", "output_file": f"data-{index}.csv"}
        recorder.finish(recorder.begin(snapshot(observed=str(index)), 1, "measurement"))
    run.complete()
    path = run.path
    del recorder, run
    gc.collect()
    recovered = service.open_run(path)
    assert recovered.metadata["acquisitions"] == {
        "count": 2, "total_count": 2, "recent_ids": ["hardware-0", "hardware-1"],
        "outputs": [{"acquisition_id": "hardware-0", "file": "data-0.csv"},
                    {"acquisition_id": "hardware-1", "file": "data-1.csv"}],
    }


def test_reopen_mixed_capture_events_keep_only_recent_hundred(service):
    run = service.begin("recovery_audit", "fake-sample")
    for index in range(103):
        event = "capture" if index % 2 == 0 else "capture_started"
        run.record_event(event, acquisition_id=f"capture-{index}", output={"file": f"data-{index}.csv"})
        run.record_event("capture_completed", acquisition_id=f"capture-{index}")
    assert run.metadata["acquisitions"]["count"] == 103
    path = run.path
    del run
    gc.collect()
    recovered = service.open_run(path)
    state = recovered.metadata["acquisitions"]
    assert state["count"] == state["total_count"] == 103
    assert state["recent_ids"] == [f"capture-{index}" for index in range(3, 103)]
    assert state["outputs"] == [{"acquisition_id": f"capture-{index}", "file": f"data-{index}.csv"}
                                for index in range(3, 103)]
