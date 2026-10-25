"""Capture events stay durable without repeatedly serializing the run plan."""
import gc
import json
from unittest.mock import patch

from app import experiment_metadata as metadata_module
from app.experiment_metadata import ExperimentMetadataService
from app.lightfield_metadata import LightFieldRecorder


def test_capture_journal_does_not_rewrite_large_plan_and_recovers_after_reopen(tmp_path):
    schedule = [
        {"point": index, "gates": {"Vbg": index / 1000, "Vtg": 0.0}}
        for index in range(1000)
    ]
    service = ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite")
    run = service.begin("dual_gate_sweep", "sample", settings={
        "executed_plan": {"acquisition_schedule": schedule},
    })
    initial = ExperimentMetadataService.load_metadata(run.path, migrate=False)
    assert initial["event_log"]["path"] == run.event_path.name
    initial_bytes = run.path.read_bytes()

    # Cross the former 25-event checkpoint boundary several times. Any full
    # manifest/history write here would make capture cost scale with plan size.
    with patch.object(metadata_module, "_atomic_json", side_effect=AssertionError("rewrote plan")), \
            patch.object(service.history, "upsert", side_effect=AssertionError("reindexed plan")), \
            patch.object(metadata_module.os, "fsync", wraps=metadata_module.os.fsync) as sync:
        for index in range(61):
            result = run.record_capture(acquisition_id=f"point-{index}", output={"file": "sweep.csv"})
            assert result is not None
        assert sync.call_count == 61

    assert run.path.read_bytes() == initial_bytes
    events = [json.loads(line) for line in run.event_path.read_text(encoding="utf-8").splitlines()]
    assert [event["event_index"] for event in events] == list(range(1, 62))
    assert run.metadata["acquisitions"]["count"] == 61
    path = run.path
    del run
    gc.collect()

    reopened = service.open_run(path)
    assert reopened.metadata["acquisitions"]["count"] == 61
    assert reopened.metadata["acquisitions"]["recent_ids"] == [f"point-{i}" for i in range(61)]
    assert reopened.metadata["settings"]["requested"]["executed_plan"]["acquisition_schedule"] == schedule
    reopened.record_capture(acquisition_id="point-61")
    with patch.object(metadata_module, "_atomic_json", wraps=metadata_module._atomic_json) as write, \
            patch.object(service.history, "upsert", wraps=service.history.upsert) as index:
        reopened.complete({"message": "Complete"})
    assert write.call_count == index.call_count == 1
    saved = ExperimentMetadataService.load_metadata(path, migrate=False)
    assert saved["status"] == "completed"
    assert saved["acquisitions"]["count"] == 62
    assert saved["settings"]["requested"]["executed_plan"]["acquisition_schedule"] == schedule
    assert any(entry["path"] == reopened.event_path.name for entry in saved["files"])


def test_state_mutations_still_persist_journal_counters(tmp_path):
    run = ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite").begin("dual_gate_sweep", "sample")
    run.record_capture(acquisition_id="first")
    with patch.object(metadata_module, "_atomic_json", wraps=metadata_module._atomic_json) as write:
        run.update_summary({"operator_note": "checkpoint"})
    assert write.call_count == 1
    saved = ExperimentMetadataService.load_metadata(run.path, migrate=False)
    assert saved["acquisitions"]["count"] == 1
    assert saved["summary"]["operator_note"] == "checkpoint"


def test_recovery_registers_journal_when_finishing_without_another_event(tmp_path):
    service = ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite")
    run = service.begin("dual_gate_sweep", "sample")
    run.record_capture(acquisition_id="last-point-before-interruption")
    path, journal = run.path, run.event_path.name
    snapshot = service.load_metadata(path)
    assert "last saved snapshot" in service.format_history_preview(snapshot)
    del run
    gc.collect()

    reopened = service.open_run(path)
    reopened.complete()
    saved = service.load_metadata(path)
    assert saved["acquisitions"]["count"] == 1
    assert any(entry["path"] == journal for entry in saved["files"])
    assert "last saved snapshot" not in service.format_history_preview(saved)


def test_gate_context_and_output_changes_do_not_rewrite_optical_settings(tmp_path):
    run = ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite").begin("dual_gate_sweep", "sample")
    recorder = LightFieldRecorder(run)
    snapshot = {"captured_utc": "2026-10-25T00:00:00Z", "acquisition": {"exposure_ms": 10}}
    recorder.finish(recorder.begin(snapshot, 1, "measurement"))
    with patch.object(metadata_module, "_atomic_json", side_effect=AssertionError("rewrote plan")):
        for index in range(32):
            recorder.context = {"Vbg_set": index / 10, "Vtg_set": -index / 10,
                                "point_index": index + 1, "output_file": f"sweep-{index}.csv"}
            record = recorder.begin(snapshot, 1, "measurement")
            assert record is not None
            recorder.finish(record)
    assert len(run.metadata["settings"]["snapshots"]) == 1
    assert run.read_events(limit=100)[-2]["context"]["Vbg_set"] == 3.1
    assert run.metadata["acquisitions"]["count"] == 33

    # A new optical recipe still persists its settings before acquisition.
    with patch.object(metadata_module, "_atomic_json", wraps=metadata_module._atomic_json) as write:
        recorder.begin({**snapshot, "acquisition": {"exposure_ms": 20}}, 1, "measurement")
    assert write.call_count == 1
