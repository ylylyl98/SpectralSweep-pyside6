"""MCD output durability without per-point full rewrites or GUI finalization."""
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PySide6.QtCore import QThread, QTimer, Qt
from PySide6.QtWidgets import QApplication

from app import experiment_metadata as metadata_module
from app.engine.mcd_metadata import condition_metadata_summary, finalize_mcd_metadata
from app.engine.mcd2100_worker import MCD2100Worker
from app.experiment_metadata import ExperimentMetadataService
from ui.mcd_panel import _ContinuousMCDWorker
from ui.mcd2100_panel import _Runner


def make_run(tmp_path):
    return ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite").begin(
        "mcd2100", "test-sample", settings={"requested_angles": [0, 90]})


@pytest.mark.parametrize("status", ["COMPLETED", "CANCELLED", "FAILED"])
def test_terminal_output_inventory_is_one_manifest_and_history_write(tmp_path, status):
    run = make_run(tmp_path)
    paths = []
    for index in range(12):
        path = tmp_path / f"condition-{index}.csv"
        path.write_text("field,count\n0,1\n", encoding="utf-8")
        path.with_suffix(".meta.json").write_text('{"status":"completed"}', encoding="utf-8")
        paths.append(str(path))
    result = {"status": status, "csv_paths": paths, "spectra_written": 12,
              "error": "test failure" if status == "FAILED" else None}
    with patch.object(metadata_module, "_atomic_json", wraps=metadata_module._atomic_json) as write, \
            patch.object(run.service.history, "upsert", wraps=run.service.history.upsert) as index:
        finalize_mcd_metadata(run, result)
    assert write.call_count == index.call_count == 1
    saved = run.service.load_metadata(run.path)
    assert saved["status"] == status.lower()
    assert len([entry for entry in saved["files"] if entry["role"] == "raw"]) == 12
    assert len([entry for entry in saved["files"] if entry.get("compatibility_projection")]) == 12
    assert result["metadata_finalized"] is True


def test_live_projection_appends_durable_deltas_and_terminal_json_stays_complete(tmp_path):
    run = make_run(tmp_path)
    worker = object.__new__(MCD2100Worker)
    worker.metadata_run = run
    path = tmp_path / "mcd.meta.json"
    payload = {"status": "RUNNING", "spectra_written": 0, "snapshots": [],
               "gate_conditions": [{"Vtg": value, "Vbg": -value} for value in range(1000)]}
    worker._write_legacy_metadata(path, payload)
    first_bytes = path.read_bytes()
    with patch.object(worker, "_write_metadata", wraps=worker._write_metadata) as legacy, \
            patch.object(metadata_module, "_atomic_json", side_effect=AssertionError("full manifest rewrite")), \
            patch.object(metadata_module.os, "fsync", wraps=metadata_module.os.fsync) as sync:
        for point in range(100):
            payload["spectra_written"] = point + 1
            payload["snapshots"].append({"field_t": point / 100, "temperature_k": 4.0})
            worker._write_legacy_metadata(path, payload, materialize=False)
        assert legacy.call_count == 0
        assert sync.call_count == 100
    assert path.read_bytes() == first_bytes
    assert run.reconstruct_compatibility_state(path.name) == payload
    events = [json.loads(line) for line in run.event_path.read_text(encoding="utf-8").splitlines()]
    assert all("gate_conditions" not in item["changes"] for item in events[1:])
    assert all(len(item["list_tails"]["snapshots"]) == 1 for item in events[1:])
    payload["status"] = "COMPLETED"
    worker._write_legacy_metadata(path, payload)
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert run.reconstruct_compatibility_state(path.name) == payload


def test_projection_without_shared_run_still_writes_each_checkpoint(tmp_path):
    worker = object.__new__(MCD2100Worker)
    worker.metadata_run = None
    path = tmp_path / "legacy.meta.json"
    worker._write_legacy_metadata(path, {"spectra_written": 3}, materialize=False)
    assert json.loads(path.read_text()) == {"spectra_written": 3}


def test_failed_durable_delta_is_not_reported_as_saved(tmp_path):
    worker = object.__new__(MCD2100Worker)
    worker.metadata_run = SimpleNamespace(record_compatibility_state=Mock(return_value=None))
    with pytest.raises(RuntimeError, match="could not be recorded"):
        worker._write_legacy_metadata(tmp_path / "legacy.meta.json", {"spectra_written": 3}, materialize=False)
    assert not (tmp_path / "legacy.meta.json").exists()


def test_condition_event_links_large_metadata_instead_of_duplicating_it():
    metadata = {"status": "completed", "spectra_written": 250,
                "condition": {"Vtg": 1, "Vbg": 2},
                "parameters": {"conditions": [{"Vtg": i, "Vbg": i / 2} for i in range(5000)]},
                "snapshots": [{"field_t": i / 100} for i in range(5000)]}
    summary = condition_metadata_summary(metadata, "condition-1.meta.json")
    assert summary == {"compatibility_key": "condition-1.meta.json", "status": "completed",
                       "spectra_written": 250, "condition": {"Vtg": 1, "Vbg": 2}}
    assert len(json.dumps(summary)) < 200
    assert len(json.dumps(metadata)) > 200_000


def test_shared_continuous_workflow_preserves_instrument_order_and_legacy_output(tmp_path):
    from tests.test_mcd2100_workflow import (
        _ContinuousClock, _ContinuousFakeController, _ContinuousFakeOptical, _safe_snapshot,
    )
    results, command_sequences, writes = [], [], []
    for shared in (False, True):
        folder = tmp_path / ("shared" if shared else "legacy")
        folder.mkdir()
        run = make_run(folder) if shared else None
        fields = [_safe_snapshot(0)] * 5 + [_safe_snapshot(0), _safe_snapshot(.004),
                                                   _safe_snapshot(.006), _safe_snapshot(.01)]
        controller, optical, clock = _ContinuousFakeController(fields), _ContinuousFakeOptical(), _ContinuousClock()
        worker = MCD2100Worker(controller, optical, 0, .01, [0], folder,
                              poll_interval_s=.01, gate_timeout_s=.3, operation_timeout_s=2,
                              cleanup_timeout_s=1, sleep=clock.sleep, clock=clock, metadata_run=run)
        with patch.object(worker, "_write_metadata", wraps=worker._write_metadata) as write, \
                patch("app.engine.mcd2100_worker._utc_now", return_value="2026-10-25T00:00:00Z"):
            result = worker.run()
        assert result["status"] == "COMPLETED", result
        writes.append(write.call_count)
        results.append(result)
        command_sequences.append((controller.events, optical.events))
        legacy = json.loads(Path(result["metadata_path"]).read_text(encoding="utf-8"))
        assert legacy["status"] == "COMPLETED"
        assert legacy["spectra_written"] == result["spectra_written"]
        if run is not None:
            assert run.reconstruct_compatibility_state(Path(result["metadata_path"]).name) == legacy
            completed = [event for event in run.read_events(limit=1000) if event["event"] == "condition_completed"]
            assert completed[0]["condition_detail"]["compatibility_key"] == Path(result["metadata_path"]).name
            assert "snapshots" not in completed[0]["condition_detail"]
    assert command_sequences[0] == command_sequences[1]
    assert Path(results[0]["csv_path"]).read_bytes() == Path(results[1]["csv_path"]).read_bytes()
    assert writes[1] < writes[0]


@pytest.mark.parametrize("backend", ["mcd", "mcd2100"])
@pytest.mark.parametrize("fail", [False, True])
def test_finalization_runs_on_worker_and_holds_completion_until_durable(tmp_path, backend, fail):
    app = QApplication.instance() or QApplication([])
    run = make_run(tmp_path)
    result = {"status": "COMPLETED", "spectra_written": 0}
    if backend == "mcd":
        runner = _ContinuousMCDWorker({}, SimpleNamespace(adapter=None), None, None, None,
                                      metadata_run=run)
        runner._run = lambda: dict(result)
    else:
        runner = _Runner(SimpleNamespace(metadata_run=run, run=lambda: dict(result)))
    entered, release = threading.Event(), threading.Event()
    owner_ids, finished, timer_ticks = [], [], []
    original = run.complete

    def slow_complete(*args, **kwargs):
        owner_ids.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        if fail:
            raise OSError("metadata storage unavailable")
        return original(*args, **kwargs)

    thread = QThread()
    runner.moveToThread(thread)
    thread.started.connect(runner.run)
    runner.finished.connect(finished.append, Qt.ConnectionType.DirectConnection)
    runner.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
    thread.finished.connect(runner.deleteLater)
    with patch.object(run, "complete", side_effect=slow_complete):
        thread.start()
        try:
            assert entered.wait(2)
            assert thread.isRunning() and not finished
            assert len(owner_ids) == 1 and owner_ids[0] != threading.get_ident()
            QTimer.singleShot(0, lambda: timer_ticks.append(True))
            app.processEvents()
            assert timer_ticks == [True]
        finally:
            release.set()
            assert thread.wait(3000)
    assert len(finished) == 1
    if fail:
        assert "metadata storage unavailable" in finished[0]["metadata_error"]
        assert not finished[0].get("metadata_finalized")
        assert finished[0]["status"] == "FAILED"
        assert finished[0]["measurement_status"] == "COMPLETED"
        assert finished[0]["metadata_status"] == "failed"
    else:
        assert finished[0]["metadata_finalized"]


def test_history_failure_cannot_leave_completed_sidecar_or_success_result(tmp_path):
    run = make_run(tmp_path)
    result = {"status": "COMPLETED", "spectra_written": 2}
    error = OSError("history index unavailable")
    with patch.object(run.service.history, "upsert", side_effect=error):
        with pytest.raises(OSError) as caught:
            finalize_mcd_metadata(run, result)
    assert caught.value is error
    assert result["status"] == "FAILED"
    assert result["measurement_status"] == "COMPLETED"
    assert result["metadata_status"] == "failed"
    assert result["metadata_error"] == "history index unavailable"
    assert not result["metadata_finalized"]
    saved = run.service.load_metadata(run.path)
    assert saved["status"] == "failed"
    assert saved["metadata_status"] == "degraded"
    assert saved["metadata_error"]["message"] == "history index unavailable"
    assert saved["metadata_error"]["event"] == "finalization"


def test_degraded_returned_failure_is_overall_failure_and_keeps_hardware_result(tmp_path):
    run = make_run(tmp_path)
    run.metadata["metadata_status"] = "degraded"
    result = {"status": "COMPLETED", "spectra_written": 2}
    with pytest.raises(RuntimeError, match="marked failed"):
        finalize_mcd_metadata(run, result)
    assert result["status"] == "FAILED"
    assert result["measurement_status"] == "COMPLETED"
    assert result["metadata_status"] == "failed"
    assert not result["metadata_finalized"]
    assert run.service.load_metadata(run.path)["status"] == "failed"


def test_failure_marker_error_does_not_hide_original_finalization_error(tmp_path):
    run = make_run(tmp_path)
    result = {"status": "COMPLETED"}
    original = OSError("original full disk failure")
    with patch.object(run, "complete", side_effect=original), \
            patch.object(run, "mark_metadata_failure", side_effect=RuntimeError("marker failed")):
        with pytest.raises(OSError) as caught:
            finalize_mcd_metadata(run, result)
    assert caught.value is original
    assert result["metadata_error"] == "original full disk failure"
    assert result["metadata_failure_persistence_error"] == "marker failed"
    assert run.service.load_metadata(run.path)["status"] == "failed"


def test_partial_mcd_output_is_registered_even_when_run_raises(tmp_path):
    run = make_run(tmp_path)
    path = tmp_path / "partial.csv"
    path.write_text("field,count\n0,1\n", encoding="utf-8")
    worker = _ContinuousMCDWorker({}, SimpleNamespace(adapter=None), None, None, None,
                                  metadata_run=run)
    worker._output_csv_paths = [str(path)]
    worker._run = Mock(side_effect=RuntimeError("test acquisition failure"))
    emitted = []
    worker.finished.connect(emitted.append, Qt.ConnectionType.DirectConnection)
    worker.run()
    saved = run.service.load_metadata(run.path)
    assert saved["status"] == "failed"
    assert any(entry["path"] == path.name and entry["role"] == "raw" for entry in saved["files"])
    assert emitted[0]["metadata_finalized"]
