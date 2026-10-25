"""Startup CPU reductions keep synchronous, exact metadata persistence."""
import copy
from enum import Enum, IntEnum
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from app import experiment_metadata as metadata
from app import metadata_storage as storage


def _settings():
    return {"executed_plan": {"acquisition_schedule": [
        {"index": index, "ctx": {"label": "repeat recipe", "frames": 1},
         "point": {"Vbg": index * .025, "optional": None}}
        for index in range(128)
    ]}}


def test_sidecar_and_history_share_one_encoding_per_write(tmp_path):
    service = metadata.ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite")
    settings = _settings()
    original = copy.deepcopy(settings)
    with patch.object(storage, "encode_records", wraps=storage.encode_records) as encode:
        run = service.begin("audit", "sample", settings=settings)
        assert encode.call_count == 1
        assert service.load_metadata(run.path, migrate=False) == run.metadata
        assert service.history.query("sample", "audit")[0]["settings"] == run.metadata["settings"]

        # Each write owns a fresh cache: mutable plans never reuse stale tables.
        run.metadata["settings"]["requested"]["executed_plan"]["acquisition_schedule"][0]["point"]["Vbg"] = -0.125
        run._write()
        assert encode.call_count == 2
        assert service.load_metadata(run.path, migrate=False) == run.metadata
        assert service.history.query("sample", "audit")[0]["settings"] == run.metadata["settings"]
    assert settings == original


def test_standalone_compaction_does_not_cache_across_calls():
    value = _settings()
    with patch.object(storage, "encode_records", wraps=storage.encode_records) as encode:
        first = storage.compact_metadata(value)
        value["executed_plan"]["acquisition_schedule"][0]["point"]["Vbg"] = 7.25
        second = storage.compact_metadata(value)
    assert encode.call_count == 2
    assert storage.expand_metadata(first) != value
    assert storage.expand_metadata(second) == value


def test_every_write_still_fsyncs_then_replaces_then_indexes(tmp_path):
    service = metadata.ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite")
    run = service.begin("audit", "sample", settings=_settings())
    actions = []
    fsync, replace, upsert = metadata.os.fsync, metadata.os.replace, service.history.upsert

    def sync(fd):
        actions.append("fsync")
        return fsync(fd)

    def publish(source, target):
        actions.append("replace")
        return replace(source, target)

    def index(value, **kwargs):
        actions.append("history")
        # The authoritative sidecar must already contain this snapshot.
        assert service.load_metadata(run.path, migrate=False) == value
        return upsert(value, **kwargs)

    with patch.object(metadata.os, "fsync", side_effect=sync), \
            patch.object(metadata.os, "replace", side_effect=publish), \
            patch.object(service.history, "upsert", side_effect=index):
        run._write()
        run._write()
    assert actions == ["fsync", "replace", "history"] * 2


def test_fsync_failure_does_not_publish_or_index(tmp_path):
    service = metadata.ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite")
    run = service.begin("audit", "sample", settings=_settings())
    before = run.path.read_bytes()
    run.metadata["summary"]["changed"] = True
    with patch.object(metadata.os, "fsync", side_effect=OSError("disk error")), \
            patch.object(service.history, "upsert") as upsert:
        with pytest.raises(OSError, match="disk error"):
            run._write()
        upsert.assert_not_called()
    assert run.path.read_bytes() == before


def test_plain_settings_strings_do_not_invoke_pathlib(tmp_path):
    value = {"label": "Measurement setup", "mode": "lightfield", "relative": "folder/file.csv",
             "count": 1, "on": True, "missing": None, "values": [1.25, -0.0, "", "x:y"]}
    with patch.object(Path, "expanduser", side_effect=AssertionError("unnecessary path parsing")), \
            patch.object(Path, "resolve", side_effect=AssertionError("unnecessary filesystem lookup")):
        assert metadata._portable_settings(value, tmp_path) == value


def test_absolute_paths_keep_portable_settings_behavior(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    value = {"inside": root / "folder" / "spectrum.csv", "outside": str(tmp_path / "recipe.xml"),
             "root": str(root), "relative": Path("recipe.xml")}
    assert metadata._portable_settings(value, root) == {
        "inside": "folder/spectrum.csv", "outside": "recipe.xml", "root": ".", "relative": "recipe.xml"}


def test_json_native_fast_path_preserves_special_value_normalization(tmp_path):
    class Choice(Enum):
        VALUE = "choice"

    class Count(IntEnum):
        VALUE = 3

    value = {1: (True, 1, 1.0, -0.0, None, float("nan"), float("inf")),
             "enum": [Choice.VALUE, Count.VALUE], "path": tmp_path,
             "scalar": np.int64(7), "array": np.array([.125, .25], dtype=np.float64),
             "table": pd.DataFrame({"value": [1.5, float("nan")]})}
    result = metadata._jsonable(value)
    assert result["1"] == [True, 1, 1.0, -0.0, None, None, None]
    assert [type(v) for v in result["1"][:4]] == [bool, int, float, float]
    assert json.dumps(result["1"]).startswith("[true, 1, 1.0, -0.0")
    assert result["enum"] == ["choice", 3]
    assert result["path"] == str(tmp_path)
    assert result["scalar"] == {"__type__": "ndarray", "dtype": "int64", "shape": [], "data": 7}
    assert result["array"] == {"__type__": "ndarray", "dtype": "float64", "shape": [2], "data": [.125, .25]}
    assert result["table"]["data"] == [{"value": 1.5}, {"value": None}]
    json.dumps(result, allow_nan=False)
