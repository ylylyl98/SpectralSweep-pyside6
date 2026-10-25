"""BFP exports retain frozen provenance without repeated manifest rewrites."""
import hashlib
import json
from unittest.mock import patch

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from app import experiment_metadata as metadata_module
from app.experiment_metadata import ExperimentMetadataService
from ui.bfp_panel_integrated import _BRCWidget, _FRCWidget, _input_identity
from utils.bfp_io import save_binned_csv, save_full_image_csv


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(params=["binned", "full"])
def widget(request, tmp_path, app):
    root = tmp_path / "sample" / "inputs"
    root.mkdir(parents=True)
    wavelength = np.linspace(700., 704., 5)
    sample, background = root / "sample.csv", root / "background.csv"
    if request.param == "binned":
        save_binned_csv(sample, wavelength, np.arange(5.) + 2)
        save_binned_csv(background, wavelength, np.arange(5.) + 1)
        value = _BRCWidget()
    else:
        y = np.arange(4.)
        image = np.arange(20.).reshape(4, 5) + 1
        save_full_image_csv(sample, wavelength, y, image)
        save_full_image_csv(background, wavelength, y, image / 2)
        value = _FRCWidget()
    value._sample_edit.setText(str(sample))
    value._bg_edit.setText(str(background))
    value.compute()
    assert value._rc_processing_context is not None
    yield value
    value.close()


def test_first_export_batches_writes_and_unchanged_export_is_metadata_noop(widget):
    context = widget._rc_processing_context
    original_hash = context["inputs"]["sample"]["sha256"]
    with patch.object(metadata_module, "_atomic_json", wraps=metadata_module._atomic_json) as write:
        widget.save_csv()
    assert write.call_count == 3  # initial provenance, both input links, complete output
    run = widget._rc_run
    saved = ExperimentMetadataService.load_metadata(run.path, migrate=False)
    frozen = saved["settings"]["requested"]["processing_context"]
    assert frozen["inputs"]["sample"]["sha256"] == original_hash
    assert frozen["result"] == context["result"]
    event = next(item for item in run.read_events() if item["event"] == "processing")
    assert event["processing"]["processing_context_ref"] == {
        "metadata_file": run.path.name, "json_pointer": "/settings/requested/processing_context",
    }
    assert "wavelength_nm" not in json.dumps(event)
    output = next(item for item in saved["files"] if item["role"] == "processed")
    content = (run.service.output_root / output["path"]).read_bytes()
    assert output["sha256"] == hashlib.sha256(content).hexdigest()
    before, events = run.path.read_bytes(), run.event_path.read_bytes()
    with patch.object(metadata_module, "_atomic_json", wraps=metadata_module._atomic_json) as write:
        widget.save_csv()
    assert write.call_count == 0
    assert run.path.read_bytes() == before
    assert run.event_path.read_bytes() == events
    assert (run.service.output_root / output["path"]).read_bytes() == content


def test_input_identity_is_frozen_at_compute_and_metadata_write_can_retry(widget):
    frozen_hash = widget._rc_processing_context["inputs"]["sample"]["sha256"]
    widget.save_csv()
    run = widget._rc_run
    output = next(item for item in run.metadata["files"] if item["role"] == "processed")
    path = run.service.output_root / output["path"]
    changed = path.read_bytes() + b"\n"
    path.write_bytes(changed)
    with patch.object(run, "_write", side_effect=OSError("temporary metadata failure")):
        with pytest.raises(OSError):
            widget._finish_rc_export(path, "processed")
    with patch.object(run, "_write", wraps=run._write) as write:
        widget._finish_rc_export(path, "processed")
    assert write.call_count == 1
    saved = ExperimentMetadataService.load_metadata(run.path)
    assert saved["settings"]["requested"]["processing_context"]["inputs"]["sample"]["sha256"] == frozen_hash
    recorded = next(item for item in saved["files"] if item["role"] == "processed")
    assert recorded["sha256"] == hashlib.sha256(changed).hexdigest()


def test_input_lookup_prefers_direct_sidecar_without_expanding_unrelated_plans(tmp_path):
    source = tmp_path / "sample.csv"
    source.write_text("wavelength_nm,intensity_counts\n700,1\n", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    direct = source.with_name(source.name + ".experiment.metadata.json")
    direct.write_text(json.dumps({"experiment_id": "parent", "files": [
        {"path": source.name, "role": "raw", "sha256": digest},
    ]}), encoding="utf-8")
    (tmp_path / "unrelated.experiment.metadata.json").write_text("invalid JSON", encoding="utf-8")
    with patch.object(ExperimentMetadataService, "load_metadata", wraps=ExperimentMetadataService.load_metadata) as load:
        identity = _input_identity(source, "sample")
    assert identity["parent_experiment_id"] == "parent"
    assert identity["sha256"] == digest
    assert load.call_count == 1


def test_invalid_and_stale_sidecars_do_not_hide_matching_parent(tmp_path):
    source = tmp_path / "sample.csv"
    source.write_bytes(b"current input")
    direct = source.with_name(source.name + ".experiment.metadata.json")
    direct.write_text("invalid JSON", encoding="utf-8")
    source.with_suffix(".experiment.metadata.json").write_text(json.dumps({
        "experiment_id": "old", "files": [{"path": source.name, "role": "raw", "sha256": "old hash"}],
    }), encoding="utf-8")
    (tmp_path / "valid.experiment.metadata.json").write_text(json.dumps({
        "experiment_id": "current", "files": [{"path": source.name, "role": "raw",
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}],
    }), encoding="utf-8")
    assert _input_identity(source, "sample")["parent_experiment_id"] == "current"
