"""Acquisition tables preserve exact JSON data without repeated plan objects."""
import copy
import json
import math

import pytest

from app.metadata_tables import decode_records, encode_records


def packed(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def test_large_nested_schedule_round_trips_and_shares_repeated_fields():
    rows = []
    order = [{"kind": "group", "id": "setup", "label": "Measurement setup"},
             {"kind": "points", "id": "points", "label": "Gate points"}]
    for index in range(16000):
        point = {"point_i": index % 400, "Vbg": (index % 400) / 10,
                 "Vtg": (index % 400) / 10 - 20, "Vbias": None}
        rows.append({"seq_i": index, "ctx": {"exposure_ms": 50, "accumulations": 25},
                     "row": {"_gate_point": dict(point), "frames": 1, "repeat": 1,
                             "description": "retained full condition description" * 4},
                     "gate_point": point, "execution_order": copy.deepcopy(order),
                     "group_instances": {"setup": index // 400}})
    before = packed(rows)
    table = encode_records(rows)
    assert table is not None
    assert table["columns"]["execution_order"]["kind"] == "constant"
    assert table["columns"]["row"]["kind"] == "records"
    assert len(packed(table)) < len(before) / 5
    restored = decode_records(json.loads(packed(table)))
    assert restored == rows
    assert packed(rows) == before
    restored[0]["execution_order"][0]["label"] = "changed"
    restored[0]["gate_point"]["Vbg"] = -123
    assert restored[1]["execution_order"][0]["label"] == "Measurement setup"
    assert restored[0]["row"]["_gate_point"]["Vbg"] == 0
    assert rows[0]["execution_order"][0]["label"] == "Measurement setup"


def test_scalar_types_signed_zero_and_literal_column_like_data_are_preserved():
    values = [True, 1, 1.0, False, 0, 0.0, -0.0, None, "1", 0.1 + 0.2]
    rows = [{"value": values[index % len(values)],
             "literal": {"kind": "constant", "value": [1, 2]}} for index in range(100)]
    table = encode_records(rows)
    restored = decode_records(json.loads(packed(table)))
    assert packed(restored) == packed(rows)
    assert [type(row["value"]) for row in restored] == [type(row["value"]) for row in rows]
    assert math.copysign(1, restored[6]["value"]) == -1
    restored[0]["literal"]["value"].append(3)
    assert restored[1]["literal"]["value"] == [1, 2]
    table["columns"]["literal"]["value"]["value"].append(4)
    assert rows[0]["literal"]["value"] == [1, 2]


@pytest.mark.parametrize("values,kind", [
    (["long repeated value" * 10] * 100, "constant"),
    (["long repeated value A" * 10, "long repeated value B" * 10] * 50, "dictionary"),
    (["A"] * 100 + ["B"] * 100, "runs"),
    (list(range(100)), "values"),
])
def test_column_chooses_compact_representation(values, kind):
    rows = [{"value": value} for value in values]
    table = encode_records(rows)
    assert table["columns"]["value"]["kind"] == kind
    assert decode_records(table) == rows


@pytest.mark.parametrize("rows", [[], [{"x": 1}] * 63, [{"x": 1}] * 63 + [{"y": 1}],
                                 [[1]] * 64, [{1: "value"}] * 64])
def test_ineligible_shapes_are_not_encoded(rows):
    assert encode_records(rows) is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), (1, 2), {1: "invalid"}, object()])
def test_non_json_values_are_rejected(value):
    with pytest.raises(ValueError):
        encode_records([{"value": value}] * 64)


@pytest.mark.parametrize("column", [
    {}, {"kind": "unknown"}, {"kind": True},
    {"kind": "constant"}, {"kind": "constant", "value": 1, "extra": 2},
    {"kind": "constant", "value": float("nan")},
    {"kind": "values", "values": [1]}, {"kind": "values", "values": "ab"},
    {"kind": "dictionary", "values": [1], "indices": [0, True]},
    {"kind": "dictionary", "values": [1], "indices": [0, -1]},
    {"kind": "dictionary", "values": [1], "indices": [0, 1]},
    {"kind": "dictionary", "values": [1], "indices": [0]},
    {"kind": "dictionary", "values": "a", "indices": [0, 0]},
    {"kind": "runs", "runs": [[1, "a"]]},
    {"kind": "runs", "runs": [[3, "a"]]},
    {"kind": "runs", "runs": [[True, "a"], [1, "b"]]},
    {"kind": "runs", "runs": [[0, "a"], [2, "b"]]},
    {"kind": "runs", "runs": [[2, "a", "extra"]]},
    {"kind": "records", "columns": []},
    {"kind": "records", "columns": {1: {"kind": "constant", "value": 1}}},
    {"kind": "records", "columns": {"x": {"kind": "values", "values": [1]}}},
])
def test_malformed_columns_are_rejected(column):
    with pytest.raises(ValueError):
        decode_records({"count": 2, "columns": {"value": column}})


@pytest.mark.parametrize("table", [
    [], {}, {"count": 1}, {"count": 1, "columns": {}, "extra": 1},
    {"count": True, "columns": {}}, {"count": -1, "columns": {}},
    {"count": 2.0, "columns": {}}, {"count": 2, "columns": []},
    {"count": 2, "columns": {1: {"kind": "constant", "value": 1}}},
])
def test_malformed_tables_are_rejected(table):
    with pytest.raises(ValueError):
        decode_records(table)


def test_empty_record_columns_and_cyclic_values():
    assert decode_records(encode_records([{} for _ in range(64)])) == [{}] * 64
    cycle = []
    cycle.append(cycle)
    with pytest.raises(ValueError, match="Cyclic"):
        encode_records([{"cycle": cycle}] * 64)
    with pytest.raises(ValueError, match="Cyclic"):
        decode_records({"count": 2, "columns": {"x": {"kind": "constant", "value": cycle}}})


def test_uniform_float_columns_preserve_signed_zero_and_extreme_values():
    values = [0.0, -0.0, 5e-324, -5e-324, 1.7976931348623157e308,
              -1.7976931348623157e308, 0.1 + 0.2, 1.0]
    rows = [{"value": values[index % len(values)]} for index in range(128)]
    assert packed(decode_records(encode_records(rows))) == packed(rows)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_uniform_float_columns_still_reject_nonfinite_values(invalid):
    rows = [{"value": 1.0} for _ in range(128)]
    rows[-1]["value"] = invalid
    with pytest.raises(ValueError, match="finite"):
        encode_records(rows)


def test_uniform_records_ignore_key_order_without_changing_exact_values():
    rows = []
    for index in range(128):
        fields = [("first", -0.0), ("second", True), ("third", 1.0)]
        rows.append({"record": dict(fields if index % 2 else reversed(fields))})
    table = encode_records(rows)
    assert table["columns"]["record"]["kind"] == "constant"
    assert json.dumps(decode_records(table), sort_keys=True) == json.dumps(rows, sort_keys=True)


def test_later_nested_record_cannot_hide_non_native_string_keys():
    class StringKey(str):
        pass

    rows = [{"record": {"key": 1}} for _ in range(128)]
    rows[-1]["record"] = {StringKey("key"): 1}
    with pytest.raises(ValueError, match="keys must be strings"):
        encode_records(rows)


def test_irregular_nested_columns_use_lossless_fallback():
    values = [{"one": [1, 2]}, {"two": [True]}, [0.0, -0.0], [1], None,
              {"one": {"kind": "records", "columns": {"literal": 1}}}]
    rows = [{"value": copy.deepcopy(values[index % len(values)])} for index in range(128)]
    before = packed(rows)
    table = encode_records(rows)
    assert packed(decode_records(table)) == before
    assert packed(rows) == before


def test_cross_row_shared_ancestors_are_valid_and_decode_without_aliases():
    leaf = {"label": "leaf", "child": None}
    middle = {"label": "middle", "child": leaf}
    parent = {"label": "parent", "child": middle}
    rows = [{"node": parent if index % 2 == 0 else middle} for index in range(128)]
    before = packed(rows)
    restored = decode_records(encode_records(rows))
    assert packed(restored) == before
    restored[0]["node"]["child"]["label"] = "changed"
    assert restored[1]["node"]["label"] == "middle"
    assert restored[2]["node"]["child"]["label"] == "middle"
    assert packed(rows) == before


@pytest.mark.parametrize("kind", ["records", "lists"])
def test_uniform_columns_reject_cycles_across_distinct_containers(kind):
    if kind == "records":
        first, second = {"child": None}, {"child": None}
        first["child"], second["child"] = second, first
    else:
        first, second = [], []
        first.append(second)
        second.append(first)
    rows = [{"value": first if index % 2 == 0 else second} for index in range(128)]
    with pytest.raises(ValueError, match="Cyclic"):
        encode_records(rows)
