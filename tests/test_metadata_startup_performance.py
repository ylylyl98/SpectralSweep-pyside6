"""Startup CPU reductions keep synchronous, exact metadata persistence."""
import copy
from collections import UserDict
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


def test_portable_fast_path_preserves_enum_and_typed_values(tmp_path):
    class TextChoice(str, Enum):
        VALUE = "spectrum"

    class Count(IntEnum):
        VALUE = 3

    array = np.array([[1.2345678901234567, -0.0]], dtype=np.float64)
    table = pd.DataFrame({"value": [1.2345678901234567, float("nan")]})
    value = {"nested": (TextChoice.VALUE, Count.VALUE, np.int64(7), array, table),
             "numbers": [True, 1, 1.0, -0.0, 1.2345678901234567, float("nan"), float("inf")]}
    result = metadata._portable_settings(value, tmp_path)
    assert result["nested"] == ["spectrum", 3, metadata._jsonable(np.int64(7)),
                                metadata._jsonable(array), metadata._jsonable(table)]
    assert result["nested"][2]["shape"] == []
    assert result["nested"][3]["dtype"] == "float64"
    assert result["nested"][3]["shape"] == [1, 2]
    assert [type(item) for item in result["numbers"][:4]] == [bool, int, float, float]
    assert result["numbers"][3].hex() == "-0x0.0p+0"
    assert result["numbers"][4].hex() == value["numbers"][4].hex()
    assert result["numbers"][5:] == [None, None]
    np.testing.assert_array_equal(array, [[1.2345678901234567, -0.0]])
    assert np.signbit(array[0, 1]) and np.isnan(table.iloc[1, 0])
    json.dumps(result, allow_nan=False)


def test_portable_nested_container_subclasses_are_independent_of_input(tmp_path):
    class Values(list):
        pass

    class Pair(tuple):
        pass

    inside = tmp_path / "folder with spaces" / "校准.csv"
    source = UserDict({1: Values([Pair((inside, {"relative": "folder/recipe.xml"})),
                                {"enabled": True, "gain": 1.2345678901234567}])})
    before = copy.deepcopy(source)
    result = metadata._portable_settings(source, tmp_path)
    assert result == {"1": [["folder with spaces/校准.csv", {"relative": "folder/recipe.xml"}],
                           {"enabled": True, "gain": 1.2345678901234567}]}
    assert type(result) is dict and type(result["1"]) is list and type(result["1"][0]) is list
    result["1"][0][1]["relative"] = "changed"
    result["1"][1]["gain"] = 0
    assert source == before
    assert source[1][0][0] is inside


def test_portable_path_strings_keep_home_and_normalized_boundary_behavior(tmp_path):
    root = tmp_path / "output"
    root.mkdir()
    inside = str(root / "unused" / ".." / "校准.csv")
    outside = str(root / ".." / "private-location" / "recipe.xml")
    source = {"paths": [inside, outside, "~/home-calibration.json", str(root)]}
    before = copy.deepcopy(source)
    original_expanduser = metadata.os.path.expanduser

    def expanduser(value):
        if value == "~/home-calibration.json":
            return str(root / "home-calibration.json")
        return original_expanduser(value)

    with patch.object(metadata.os.path, "expanduser", side_effect=expanduser):
        result = metadata._portable_settings(source, root)
    assert result == {"paths": ["校准.csv", "recipe.xml", "home-calibration.json", "."]}
    assert source == before
    assert "private-location" not in json.dumps(result)


def test_portable_nonpath_text_in_native_sequences_never_uses_pathlib(tmp_path):
    class TextChoice(str, Enum):
        VALUE = "lightfield"

    source = ("", "C:relative.csv", "https://example.invalid/calibration", "路径/relative.csv",
              ["Measurement setup", "x:y", TextChoice.VALUE])
    with patch.object(Path, "expanduser", side_effect=AssertionError("text is not a local path")), \
            patch.object(Path, "resolve", side_effect=AssertionError("text is not a local path")):
        result = metadata._portable_settings(source, tmp_path)
    assert result == ["", "C:relative.csv", "https://example.invalid/calibration", "路径/relative.csv",
                      ["Measurement setup", "x:y", "lightfield"]]
    assert type(source) is tuple and source[4][2] is TextChoice.VALUE


def test_portable_shared_containers_expand_to_independent_output_values(tmp_path):
    shared = {"axis": [1.2345678901234567, -0.0], "calibration": tmp_path / "calibration.json"}
    source = {"first": shared, "second": (shared,)}
    before = copy.deepcopy(source)
    result = metadata._portable_settings(source, tmp_path)
    assert result["first"] == result["second"][0]
    assert result["first"] is not result["second"][0]
    assert result["first"]["axis"] is not result["second"][0]["axis"]
    result["first"]["axis"][0] = 0
    result["first"]["calibration"] = "changed"
    assert result["second"][0]["axis"][0].hex() == shared["axis"][0].hex()
    assert result["second"][0]["axis"][1].hex() == "-0x0.0p+0"
    assert result["second"][0]["calibration"] == "calibration.json"
    assert source == before
    assert source["first"] is source["second"][0]
