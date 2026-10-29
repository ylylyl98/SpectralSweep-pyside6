"""Lossless JSON tables for large, uniformly shaped acquisition records.

Only the storage manifest's caller decides which values are tables. Ordinary
user dictionaries, including ones with ``kind`` keys, remain literal values.
"""
from __future__ import annotations

import copy
from functools import lru_cache
import json
import math
from typing import Any


@lru_cache(maxsize=1024)
def _text_size(value: str) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _array_size(sizes) -> int:
    sizes = list(sizes)
    return 2 + sum(sizes) + max(0, len(sizes) - 1)


def _object_size(fields) -> int:
    fields = list(fields)
    return 2 + sum(_text_size(key) + 1 + size for key, size in fields) + max(0, len(fields) - 1)


class _Values:
    """Intern structural identities and sizes without serializing ancestors."""

    def __init__(self):
        self._intern = {}
        self._containers = {}
        self._active = set()

    def inspect(self, value: Any) -> tuple[int, int]:
        kind = type(value)
        if kind in (dict, list):
            identity = id(value)
            if identity in self._active:
                raise ValueError("Cyclic metadata table value")
            cached = self._containers.get(identity)
            if cached is not None:
                return cached[1]
            self._active.add(identity)
            try:
                if kind is dict:
                    if any(type(key) is not str for key in value):
                        raise ValueError("Metadata table keys must be strings")
                    children = [(key, self.inspect(child)) for key, child in value.items()]
                    signature = ("dict", tuple(sorted((key, info[0]) for key, info in children)))
                else:
                    children = [self.inspect(child) for child in value]
                    signature = ("list", tuple(info[0] for info in children))
                result = self._intern.get(signature)
                if result is None:
                    size = (_object_size((key, info[1]) for key, info in children) if kind is dict else
                            _array_size(info[1] for info in children))
                    result = (len(self._intern), size)
                    self._intern[signature] = result
                # Retain the object so a later temporary cannot reuse its id.
                self._containers[identity] = (value, result)
                return result
            finally:
                self._active.remove(identity)
        if kind is float:
            if not math.isfinite(value):
                raise ValueError("Metadata table floats must be finite")
            signature = ("float", value.hex())
        elif kind in (str, int, bool, type(None)):
            signature = (kind.__name__, value)
        else:
            raise ValueError("Metadata tables require JSON values")
        if signature not in self._intern:
            size = len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
            self._intern[signature] = (len(self._intern), size)
        return self._intern[signature]


def _same_keys(rows: list[dict]) -> bool:
    if not rows or type(rows[0]) is not dict:
        return False
    keys = rows[0].keys()
    return all(type(row) is dict and row.keys() == keys
               and all(type(key) is str for key in row) for row in rows)


def _inspect_column(values: list[Any], cache: _Values, ancestors=frozenset()):
    """Index a uniform column once, retaining its child columns for encoding.

    A plan repeats the same dictionary/list shapes at each point. Sorting keys
    and recursively indexing each parent, then revisiting its children while
    choosing column encodings, duplicates that work. Index child columns first
    and build the parent identities from their already validated tokens.
    """
    kind = type(values[0])
    if kind in (str, int, bool, type(None), float) and all(type(value) is kind for value in values):
        infos, seen = [], {}
        for value in values:
            # Native float equality merges signed zero, unlike JSON provenance.
            key = value.hex() if kind is float and value == 0.0 else value
            info = seen.get(key)
            if info is None:
                info = cache.inspect(value)
                seen[key] = info
            infos.append(info)
        return infos, None

    if kind in (dict, list) and all(value is values[0] for value in values):
        return [cache.inspect(values[0])] * len(values), None
    record_keys = tuple(values[0]) if kind is dict and _same_keys(values) else None
    list_length = len(values[0]) if kind is list else None
    is_list = list_length is not None and all(
        type(value) is list and len(value) == list_length for value in values)
    if record_keys is None and not is_list:
        return [cache.inspect(value) for value in values], None

    identities = {id(value) for value in values}
    if identities & ancestors:
        # A parent in another row may be a shared but acyclic child. Let the
        # per-object traversal distinguish that case from an actual cycle.
        return [cache.inspect(value) for value in values], None
    nested = ancestors | identities
    keys = record_keys if record_keys is not None else range(list_length)
    children = {}
    for key in keys:
        items = [value[key] for value in values]
        infos, subcolumns = _inspect_column(items, cache, nested)
        children[key] = (items, infos, subcolumns)
    child_infos = [(key, children[key][1]) for key in (sorted(keys) if kind is dict else keys)]
    overhead = (_object_size((key, 0) for key in keys) if kind is dict else
                2 + max(0, len(keys) - 1))
    infos = []
    for index, value in enumerate(values):
        if kind is dict:
            signature = ("dict", tuple((key, info[index][0]) for key, info in child_infos))
        else:
            signature = ("list", tuple(info[index][0] for key, info in child_infos))
        result = cache._intern.get(signature)
        if result is None:
            result = (len(cache._intern), overhead + sum(info[index][1] for key, info in child_infos))
            cache._intern[signature] = result
        cache._containers[id(value)] = (value, result)
        infos.append(result)
    return infos, children if kind is dict else None


def _column(values: list[Any], cache: _Values, infos=None, children=None) -> tuple[dict, int]:
    if infos is None:
        infos, children = _inspect_column(values, cache)

    def candidate(kind: str, payload: dict, sizes):
        return ({"kind": kind, **payload},
                _object_size([("kind", _text_size(kind)), *sizes]))

    if all(info[0] == infos[0][0] for info in infos):
        return candidate("constant", {"value": values[0]}, [("value", infos[0][1])])

    best = candidate("values", {"values": values}, [("values", _array_size(info[1] for info in infos))])

    unique, unique_sizes, indices, positions = [], [], [], {}
    runs, run_sizes = [], []
    previous = None
    for value, (token, size) in zip(values, infos):
        if token not in positions:
            positions[token] = len(unique)
            unique.append(value)
            unique_sizes.append(size)
        indices.append(positions[token])
        if runs and token == previous:
            runs[-1][0] += 1
        else:
            runs.append([1, value])
            run_sizes.append(size)
        previous = token

    if len(unique) < len(values):
        option = candidate("dictionary", {"values": unique, "indices": indices}, [
            ("values", _array_size(unique_sizes)),
            ("indices", _array_size(len(str(index)) for index in indices)),
        ])
        if option[1] < best[1]:
            best = option
    option = candidate("runs", {"runs": runs}, [("runs", _array_size(
        len(str(run[0])) + size + 3 for run, size in zip(runs, run_sizes)
    ))])
    if option[1] < best[1]:
        best = option

    if children is not None or _same_keys(values):
        if children is None:
            columns = {key: _column([value[key] for value in values], cache) for key in values[0]}
        else:
            columns = {key: _column(items, cache, info, subcolumns)
                       for key, (items, info, subcolumns) in children.items()}
        option = candidate("records", {"columns": {key: col[0] for key, col in columns.items()}}, [
            ("columns", _object_size((key, col[1]) for key, col in columns.items())),
        ])
        if option[1] < best[1]:
            best = option
    return best


def encode_records(rows: list[dict]) -> dict | None:
    """Encode 64+ same-key JSON records when the table is smaller.

    ``None`` means the shape/size is unsuitable. Unsupported JSON values raise
    ``ValueError``; the caller must not silently persist lossy representations.
    """
    if type(rows) is not list or len(rows) < 64 or not _same_keys(rows):
        return None
    cache = _Values()
    # The uniform keys already establish every root record's structure. Its
    # JSON length is key/punctuation overhead plus cell sizes; no root-record
    # identity is needed by the column encoder.
    original_size = 2 + len(rows) - 1 + len(rows) * _object_size((key, 0) for key in rows[0])
    columns = {}
    for key in rows[0]:
        values = [row[key] for row in rows]
        infos, children = _inspect_column(values, cache)
        original_size += sum(info[1] for info in infos)
        columns[key] = _column(values, cache, infos, children)
    table = {"count": len(rows), "columns": {key: col[0] for key, col in columns.items()}}
    size = _object_size([
        ("count", len(str(len(rows)))),
        ("columns", _object_size((key, col[1]) for key, col in columns.items())),
    ])
    return copy.deepcopy(table) if size < original_size else None


def _clone(value):
    if type(value) is dict:
        return {key: _clone(child) for key, child in value.items()}
    if type(value) is list:
        return [_clone(child) for child in value]
    return value


def _fields(value, names):
    if type(value) is not dict or set(value) != set(names):
        raise ValueError("Invalid metadata table fields")


def _decode_column(column, count, cache):
    if type(column) is not dict or type(column.get("kind")) is not str:
        raise ValueError("Invalid metadata table column")
    kind = column["kind"]
    if kind == "constant":
        _fields(column, ("kind", "value"))
        cache.inspect(column["value"])
        return [_clone(column["value"]) for _ in range(count)]
    if kind == "values":
        _fields(column, ("kind", "values"))
        values = column["values"]
        if type(values) is not list or len(values) != count:
            raise ValueError("Invalid metadata table values length")
        cache.inspect(values)
        return [_clone(value) for value in values]
    if kind == "dictionary":
        _fields(column, ("kind", "values", "indices"))
        values, indices = column["values"], column["indices"]
        if (type(values) is not list or type(indices) is not list or len(indices) != count
                or any(type(index) is not int or not 0 <= index < len(values) for index in indices)):
            raise ValueError("Invalid metadata table dictionary indices")
        cache.inspect(values)
        return [_clone(values[index]) for index in indices]
    if kind == "runs":
        _fields(column, ("kind", "runs"))
        runs = column["runs"]
        if (type(runs) is not list or any(
                type(run) is not list or len(run) != 2
                or type(run[0]) is not int or run[0] <= 0 for run in runs)):
            raise ValueError("Invalid metadata table runs")
        if sum(run[0] for run in runs) != count:
            raise ValueError("Invalid metadata table runs length")
        cache.inspect(runs)
        return [_clone(value) for length, value in runs for _ in range(length)]
    if kind == "records":
        _fields(column, ("kind", "columns"))
        return _decode_columns(column["columns"], count, cache)
    raise ValueError("Unknown metadata table column kind")


def _decode_columns(columns, count, cache):
    if type(columns) is not dict or any(type(key) is not str for key in columns):
        raise ValueError("Invalid metadata table columns")
    decoded = {key: _decode_column(column, count, cache) for key, column in columns.items()}
    return [{key: values[index] for key, values in decoded.items()} for index in range(count)]


def decode_records(table: dict) -> list[dict]:
    """Validate and expand an explicitly marked table into independent rows."""
    _fields(table, ("count", "columns"))
    count = table["count"]
    if type(count) is not int or count < 0:
        raise ValueError("Invalid metadata table count")
    return _decode_columns(table["columns"], count, _Values())
