"""Metadata saves preserve provenance without repeating payloads or disk writes."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from app import experiment_metadata as metadata_module
from app.experiment_metadata import ExperimentMetadataService
from app.lightfield_metadata import LightFieldRecorder
from app.devices.andor_adapter import AndorSDK2Setup


@pytest.fixture
def run(tmp_path):
    return ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite").begin("audit", "sample")


def test_large_repeated_metadata_is_stored_once_and_loaded_losslessly(tmp_path):
    block = {"unique_payload_marker": list(range(600))}
    settings = {"plan": block, "conditions": [{"sequence": block}]}
    run = ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite").begin(
        "audit", "sample", settings=settings)
    raw_text = run.path.read_text(encoding="utf-8")
    assert raw_text.count('"unique_payload_marker"') == 1
    loaded = ExperimentMetadataService.load_metadata(run.path, migrate=False)
    assert loaded == run.metadata
    indexed = run.service.history.query("sample", "audit")[0]["settings"]
    assert indexed == run.metadata["settings"]


def test_storage_references_preserve_special_keys_and_independent_values():
    from app.metadata_storage import compact_metadata, expand_metadata
    value = {"a/b~c": {"value": list(range(300))}, "copy": {"value": list(range(300))},
             "literal": {"$ref": "#/a/b~c"}}
    encoded = compact_metadata(value)
    restored = expand_metadata(json.loads(json.dumps(encoded)))
    assert restored == value
    restored["copy"]["value"][0] = -1
    assert restored["a/b~c"]["value"][0] == 0
    assert value["copy"]["value"][0] == 0


@pytest.mark.parametrize("source", ["/missing", "/source/-1", "/source/01", "/source/2", "https://example.com/data"])
def test_storage_rejects_invalid_reference_targets(source):
    from app.metadata_storage import expand_metadata
    value = {"source": [1, 2], "copy": {"$ref": "#" + source},
             "metadata_storage": {"format": "local-json-pointers-v1", "references": {"/copy": source}}}
    with pytest.raises(ValueError):
        expand_metadata(value)


def test_storage_rejects_cycles():
    from app.metadata_storage import expand_metadata
    value = {"one": {"$ref": "#/two"}, "two": {"$ref": "#/one"},
             "metadata_storage": {"format": "local-json-pointers-v1", "references": {"/one": "/two", "/two": "/one"}}}
    with pytest.raises(ValueError, match="Cyclic"):
        expand_metadata(value)


@pytest.mark.parametrize("method", ["update_observed", "update_applied", "update_summary"])
def test_unchanged_updates_do_not_write(run, method):
    update = getattr(run, method)
    update({"value": 1})
    with patch.object(metadata_module, "_atomic_json", wraps=metadata_module._atomic_json) as write:
        update({"value": 1})
    assert write.call_count == 0


def test_identical_compatibility_state_does_not_append_empty_event(run):
    payload = {"status": "running", "spectra": [1, 2]}
    assert run.record_compatibility_state("legacy.json", payload) == payload
    before = run.event_path.read_bytes()
    assert run.record_compatibility_state("legacy.json", payload) == payload
    assert run.event_path.read_bytes() == before


def snapshot(exposure=10, previous=None):
    return {"captured_utc": "2026-10-08T00:00:00Z", "identity": {"backend": "winspec_ingaas"},
            "observed": {"winspec": {"exposure_ms": exposure}, "last_frame": previous or {}},
            "calibration": {"record": {"id": "cal-1"}}}


def test_new_lightfield_settings_have_one_complete_manifest_write(run):
    recorder = LightFieldRecorder(run)
    for exposure in (10, 20):
        with patch.object(metadata_module, "_atomic_json", wraps=metadata_module._atomic_json) as write:
            record = recorder.begin(snapshot(exposure), 1, "measurement")
        assert write.call_count == 1
        saved = ExperimentMetadataService.load_metadata(run.path)
        assert saved["observed"]["lightfield"]["settings_snapshots"][-1]["shared_settings_id"] is not None
        recorder.finish(record)


def test_andor_temperature_is_event_observation_not_settings_identity(run):
    recorder = LightFieldRecorder(run)
    observed = {"temperature_c": -70., "temperature_status": "stabilized",
                "stored_calibration_nm": [700., 701.], "exposure_ms": 10}
    setup = SimpleNamespace(identity={"backend": "andor_sdk2"}, _exposure_ms=10,
        _accumulations=1, _center_nm=701, _roi_mode="LineSensor",
        get_control_snapshot=lambda **kwargs: copy.deepcopy(observed))
    for temperature in (-70., -69.9, -70.1):
        observed["temperature_c"] = temperature
        record = recorder.begin(AndorSDK2Setup.read_metadata_snapshot(setup), 1, "measurement")
        recorder.finish(record)
    assert len(run.metadata["settings"]["snapshots"]) == 1
    starts = [e for e in run.read_events() if e["event"] == "capture_started"]
    assert [e["temperature_c"] for e in starts] == [-70., -69.9, -70.1]
    assert all(e["temperature_status"] == "stabilized" for e in starts)


def test_previous_winspec_frame_uses_acquisition_link(run):
    recorder = LightFieldRecorder(run)
    first = recorder.begin(snapshot(), 1, "measurement")
    frame = {"raw_accumulated_counts": list(range(512)), "elapsed_s": 1}
    recorder.finish(first, frame_observation=frame)
    second = recorder.begin(snapshot(previous=frame), 1, "measurement")
    assert second["previous_acquisition_id"] == first["acquisition_id"]
    assert "previous_frame_observation" not in second
    completed = [e for e in run.read_events() if e["event"] == "capture_completed"]
    assert completed[0]["frame_observation"] == frame


def test_bfp_registers_frozen_input_with_one_write(run, tmp_path):
    from ui.bfp_panel_integrated import _frozen_input
    source = tmp_path / "sample.csv"
    source.write_text("x,y\n1,2\n", encoding="utf-8")
    identity = {"role": "sample", "sha256": "a" * 64, "size_bytes": source.stat().st_size}
    with patch.object(metadata_module, "_atomic_json", wraps=metadata_module._atomic_json) as write:
        _frozen_input(run, source, identity, "csv")
    assert write.call_count == 1
    assert run.metadata["files"][-1]["input_role"] == "sample"


@pytest.fixture
def panel(tmp_path, monkeypatch):
    from tests.test_spectrum_references import _Controller
    from ui.spectrum_panel import SpectrumPanel
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("SPECTRALSWEEP_HISTORY_PATH", str(tmp_path / "history.sqlite"))
    widget = SpectrumPanel(_Controller())
    widget._last_data_kind = "spectrum_1d"
    widget._last_data = np.array([1., 2.])
    widget._last_wavelength = np.array([700., 701.])
    widget._last_acquisition_snapshot = {"exposure_ms": 10, "completed_utc": "2026-10-08T00:00:00Z"}
    yield widget
    widget.close()
    widget.deleteLater()


@pytest.mark.parametrize("kind", ["spectrum_1d", "frame_2d"])
def test_spectrum_repeated_save_reuses_record_without_metadata_writes(panel, tmp_path, kind):
    panel._last_data_kind = kind
    if kind == "frame_2d":
        panel._last_data = np.array([[1., 2.], [3., 4.]])
    output = tmp_path / "spectrum.csv"
    panel.save_current(output)
    paths = list(tmp_path.glob("*.experiment.metadata.json"))
    assert len(paths) == 1
    journal = list(tmp_path.glob("*.experiment.events.jsonl"))[0].read_bytes()
    with patch.object(metadata_module, "_atomic_json", wraps=metadata_module._atomic_json) as write:
        panel.save_current(output)
    assert write.call_count == 0
    assert list(tmp_path.glob("*.experiment.metadata.json")) == paths
    assert list(tmp_path.glob("*.experiment.events.jsonl"))[0].read_bytes() == journal
    assert len(ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite").query_history("LF6-1", "spectrum_preview")) == 1


def test_spectrum_overwrite_updates_the_one_matching_metadata(panel, tmp_path):
    output = tmp_path / "spectrum.csv"
    panel.save_current(output)
    panel._last_data = np.array([3., 4.])
    panel._last_acquisition_snapshot = {"exposure_ms": 20}
    panel.save_current(output)
    paths = list(tmp_path.glob("*.experiment.metadata.json"))
    assert len(paths) == 1
    saved = ExperimentMetadataService.load_metadata(paths[0])
    assert saved["settings"]["requested"]["acquisition_time_snapshot"]["exposure_ms"] == 20


def test_spectrum_same_stem_different_extensions_have_distinct_records(panel, tmp_path):
    panel.save_current(tmp_path / "spectrum.csv")
    panel._last_acquisition_snapshot = {"exposure_ms": 20}
    panel.save_current(tmp_path / "spectrum.txt")
    paths = list(tmp_path.glob("*.experiment.metadata.json"))
    assert len(paths) == 2
    saved = [ExperimentMetadataService.load_metadata(path) for path in paths]
    assert sorted(data["settings"]["requested"]["acquisition_time_snapshot"]["exposure_ms"] for data in saved) == [10, 20]
    assert all(len([entry for entry in data["files"] if entry["role"] == "raw"]) == 1 for data in saved)


@pytest.mark.parametrize("experiment_type", ["unrelated", "spectrum_preview"])
def test_spectrum_legacy_lookup_skips_malformed_candidates(panel, tmp_path, experiment_type):
    (tmp_path / "broken.experiment.metadata.json").write_text(
        json.dumps({"experiment_type": experiment_type, "files": [None]}), encoding="utf-8")
    output = tmp_path / "spectrum.csv"
    output.write_text("old data", encoding="utf-8")
    panel.save_current(output)
    saved = ExperimentMetadataService.load_metadata(tmp_path / "spectrum.csv.experiment.metadata.json")
    assert saved["status"] == "completed"


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_spectrum_successful_overwrite_refreshes_previous_failure_status(panel, tmp_path, status):
    output = tmp_path / "spectrum.csv"
    output.write_text("wavelength_nm,intensity_counts\n700,1\n701,2\n", encoding="utf-8")
    old = ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite").begin("spectrum_preview", "LF6-1")
    old.register_file(output, kind="spectrum_1d")
    old.fail("old failure") if status == "failed" else old.cancel("old cancellation")
    panel.save_current(output)
    saved = ExperimentMetadataService.load_metadata(old.path)
    assert saved["status"] == saved["result"]["status"] == "completed"
    assert saved["result"]["error"] is None and saved["result"]["cancellation"] is None
    assert "error" not in saved and "cancellation_reason" not in saved
    assert len(list(tmp_path.glob("*.experiment.metadata.json"))) == 1


def test_retry_saved_spectrum_is_idempotent(panel, tmp_path):
    from utils.config import cfg
    with patch.object(cfg.filename, "base_out", str(tmp_path)):
        reference = panel.add_reference([700., 701.], [1., 2.], auto_save=True)
        original_path = reference["path"]
        with patch.object(panel, "_save_spectrum_data", wraps=panel._save_spectrum_data) as save:
            assert panel.retry_reference_save(reference["id"])
        assert save.call_count == 0
        assert reference["path"] == original_path


def test_new_spectrum_exports_do_not_parse_existing_directory_metadata(panel, tmp_path):
    with patch.object(ExperimentMetadataService, "load_metadata", wraps=ExperimentMetadataService.load_metadata) as load:
        for index in range(5):
            panel.save_current(tmp_path / f"spectrum-{index}.csv")
    assert load.call_count == 0


def test_spectrum_retry_reuses_destination_after_metadata_failure(panel, tmp_path):
    from utils.config import cfg
    with patch.object(cfg.filename, "base_out", str(tmp_path)):
        with patch.object(metadata_module, "_atomic_json", side_effect=OSError("disk full")):
            reference = panel.add_reference([700., 701.], [1., 2.], auto_save=True)
        assert not reference["saved"]
        assert panel.retry_reference_save(reference["id"])
        assert len(list(tmp_path.rglob("*.csv"))) == 1
        assert len(list(tmp_path.rglob("*.experiment.metadata.json"))) == 1


def test_spectrum_automatic_destination_reserves_orphan_metadata_path(panel, tmp_path):
    reference = {"save_root": str(tmp_path), "sample_id": "sample",
                 "settings_snapshot": {"completed_utc": "2026-10-08T00:00:00Z"}}
    first = panel._auto_save_path(reference)
    first.with_name(f"{first.name}.experiment.metadata.json").write_text("{}", encoding="utf-8")
    assert panel._auto_save_path(reference) != first


def test_spectrum_retry_after_terminal_manifest_failure_preserves_one_record(panel, tmp_path):
    from utils.config import cfg
    original = metadata_module._atomic_json
    calls = 0
    def fail_terminal(path, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("temporary finalization failure")
        return original(path, data)
    with patch.object(cfg.filename, "base_out", str(tmp_path)):
        with patch.object(metadata_module, "_atomic_json", side_effect=fail_terminal):
            reference = panel.add_reference([700., 701.], [1., 2.], auto_save=True)
        assert not reference["saved"]
        assert panel.retry_reference_save(reference["id"])
        paths = list(tmp_path.rglob("*.experiment.metadata.json"))
        assert len(paths) == len(list(tmp_path.rglob("*.csv"))) == 1
        saved = ExperimentMetadataService.load_metadata(paths[0])
        assert saved["status"] == "completed"
        assert len([entry for entry in saved["files"] if entry["role"] == "raw"]) == 1


def test_mcd2100_finalization_does_not_rewrite_identical_legacy_payload(tmp_path):
    from app.engine.mcd2100_worker import MCD2100Worker
    from tests.test_mcd2100_workflow import _ContinuousFakeOptical, _ContinuousClock
    shared = ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite").begin("mcd", "sample")
    clock = _ContinuousClock()
    values = []
    original = MCD2100Worker._write_metadata
    def trace(path, value):
        values.append(copy.deepcopy(value))
        return original(path, value)
    with patch.object(MCD2100Worker, "_write_metadata", staticmethod(trace)):
        result = MCD2100Worker(object(), _ContinuousFakeOptical(), 0, .01, [0], tmp_path,
            poll_interval_s=.01, gate_timeout_s=.1, operation_timeout_s=.2,
            cleanup_timeout_s=.2, sleep=clock.sleep, clock=clock, metadata_run=shared).run()
    assert result["status"] == "FAILED"
    clean = [{k: v for k, v in value.items() if not str(k).startswith("_")} for value in values]
    assert all(a != b for a, b in zip(clean, clean[1:]))
