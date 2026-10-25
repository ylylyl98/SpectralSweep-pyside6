"""Large plans stay lossless in both sidecars and the local history index."""
import copy
import json

import pytest

from app.experiment_metadata import ExperimentMetadataService
from app.metadata_storage import compact_metadata, expand_metadata, TABLE_STORAGE_FORMAT


def schedule(count=300):
    return [{
        "ctx": {"exposure_ms": 50., "accumulations": 25, "setup": "lightfield"},
        "row": {"label": "gate sweep", "frames": 1, "_gate_point": {"Vbg": i * .05}},
        "gate_point": {"Vbg": i * .05},
        "group_instances": {"recipe": i // 100},
        "execution_order": [{"kind": "group", "parameters": ["setup", "exposure"]}],
        "literal": {"$ref": "#/settings", "$table": {"kind": "values"}},
    } for i in range(count)]


def test_schedule_storage_reduces_size_and_restores_exact_values():
    value = {"settings": {"requested": {"executed_plan": {"acquisition_schedule": schedule()}}}}
    original = copy.deepcopy(value)
    encoded = compact_metadata(value)
    assert encoded["metadata_storage"]["format"] == TABLE_STORAGE_FORMAT
    assert len(json.dumps(encoded)) < len(json.dumps(value)) / 3
    assert expand_metadata(json.loads(json.dumps(encoded))) == original
    assert value == original


@pytest.mark.parametrize("field", ["all_points", "valid_points"])
def test_other_page_record_collections_are_lossless(field):
    value = {"settings": {"requested": {field: schedule()}}}
    encoded = compact_metadata(value)
    assert encoded["metadata_storage"]["format"] == TABLE_STORAGE_FORMAT
    assert len(json.dumps(encoded)) < len(json.dumps(value)) / 3
    assert expand_metadata(json.loads(json.dumps(encoded))) == value


@pytest.mark.parametrize("field", ["snapshots", "conditions", "sequence"])
def test_heterogeneous_records_keep_shared_calibration_deduplication(field):
    # Settings change shape between backends/versions. An opaque table of
    # their whole values would hide repeated calibration arrays from pointers.
    calibration = {"axis": [i * .25 for i in range(512)]}
    snapshots = [{"settings_id": i, "values": {f"mode_{i % 2}": i,
                  "calibration": copy.deepcopy(calibration)}} for i in range(100)]
    value = {"settings": {field: snapshots}}
    encoded = compact_metadata(value)
    assert len(json.dumps(encoded)) < len(json.dumps(value)) / 5
    assert expand_metadata(encoded) == value


def test_duplicate_plans_and_escaped_paths_expand_independently():
    plan = {"acquisition_schedule": schedule()}
    value = {"a/b~c": plan, "copy": copy.deepcopy(plan)}
    encoded = compact_metadata(value)
    assert encoded["metadata_storage"]["tables"] == ["/a~1b~0c/acquisition_schedule"]
    restored = expand_metadata(encoded)
    assert restored == value
    restored["copy"]["acquisition_schedule"][0]["ctx"]["setup"] = "changed"
    assert restored["a/b~c"]["acquisition_schedule"][0]["ctx"]["setup"] == "lightfield"
    assert restored["copy"]["acquisition_schedule"][1]["ctx"]["setup"] == "lightfield"


def test_sidecar_and_history_decode_schedule_without_changing_run_plan(tmp_path):
    service = ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite")
    run = service.begin("dual_gate_sweep", "sample", settings={
        "executed_plan": {"acquisition_schedule": schedule()},
    })
    assert json.loads(run.path.read_text(encoding="utf-8"))["metadata_storage"]["format"] == TABLE_STORAGE_FORMAT
    assert service.load_metadata(run.path, migrate=False) == run.metadata
    assert service.history.query("sample", "dual_gate_sweep")[0]["settings"] == run.metadata["settings"]
    run.record_capture(acquisition_id="point-1")
    run.complete()
    assert service.load_metadata(run.path, migrate=False) == run.metadata


def test_undeclared_table_lookalike_is_literal_data():
    value = {"acquisition_schedule": {"$table": {"count": 1, "columns": {}}}}
    assert expand_metadata(compact_metadata(value)) == value


@pytest.mark.parametrize("literal_first", [False, True])
@pytest.mark.parametrize("parent", [False, True])
def test_literal_matching_encoded_table_is_never_deduplicated_as_a_table(literal_first, parent):
    from app.metadata_tables import encode_records
    rows = schedule()
    literal = {"$table": encode_records(rows)}
    if parent:
        plan, literal = {"acquisition_schedule": rows}, {"acquisition_schedule": literal}
        key = "plan"
    else:
        plan, key = rows, "acquisition_schedule"
    pairs = [(key, plan), ("literal", literal)]
    value = dict(reversed(pairs) if literal_first else pairs)
    assert expand_metadata(compact_metadata(value)) == value


@pytest.mark.parametrize("paths", [["/missing"], ["/acquisition_schedule", "/acquisition_schedule"], [False]])
def test_invalid_table_manifest_is_rejected(paths):
    encoded = compact_metadata({"acquisition_schedule": schedule()})
    encoded["metadata_storage"]["tables"] = paths
    with pytest.raises(ValueError):
        expand_metadata(encoded)


def test_pointer_cannot_target_table_internals():
    encoded = compact_metadata({"acquisition_schedule": schedule()})
    source = "/acquisition_schedule/$table/count"
    encoded["copy"] = {"$ref": "#" + source}
    encoded["metadata_storage"]["references"]["/copy"] = source
    with pytest.raises(ValueError, match="inside a table"):
        expand_metadata(encoded)
