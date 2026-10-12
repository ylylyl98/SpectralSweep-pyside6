"""Lossless, file-local references for repeated metadata objects and arrays.

Only pointers declared in the storage manifest are expanded. A user's ordinary
``{"$ref": ...}`` setting remains literal data, and no pointer opens a file or URL.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Mapping

STORAGE_KEY = "metadata_storage"
STORAGE_FORMAT = "local-json-pointers-v1"


def _token(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def compact_metadata(value: Mapping[str, Any], *, minimum_bytes: int = 256) -> dict[str, Any]:
    """Store repeated containers once without mutating the caller's values."""
    if STORAGE_KEY in value:
        # Do not overwrite an existing user field or re-encode an encoded file.
        return copy.deepcopy(dict(value))
    seen: dict[bytes, str] = {}
    references: dict[str, str] = {}

    def encode(item: Any, path: str) -> Any:
        if isinstance(item, (dict, list)):
            if path:
                canonical = json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
                if len(canonical) >= minimum_bytes:
                    digest = hashlib.sha256(canonical).digest()
                    if digest in seen:
                        source = seen[digest]
                        references[path] = source
                        return {"$ref": "#" + source}
                    seen[digest] = path
            if isinstance(item, dict):
                return {key: encode(child, path + "/" + _token(str(key))) for key, child in item.items()}
            return [encode(child, path + "/" + str(index)) for index, child in enumerate(item)]
        return item

    result = encode(dict(value), "")
    if references:
        result[STORAGE_KEY] = {"format": STORAGE_FORMAT, "references": references}
    return result


def expand_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    """Expand declared local pointers; reject missing, malformed or cyclic ones."""
    storage = value.get(STORAGE_KEY)
    if not isinstance(storage, Mapping) or storage.get("format") != STORAGE_FORMAT:
        return copy.deepcopy(dict(value))
    references = storage.get("references")
    if not isinstance(references, dict) or any(
        not isinstance(at, str) or not at.startswith("/") or
        not isinstance(source, str) or not source.startswith("/")
        for at, source in references.items()
    ):
        raise ValueError("Invalid metadata storage references")

    def lookup(path: str) -> Any:
        node: Any = value
        try:
            for part in path.split("/")[1:]:
                token = part.replace("~1", "/").replace("~0", "~")
                if isinstance(node, list):
                    if not token.isascii() or not token.isdigit() or str(int(token)) != token:
                        raise ValueError("Noncanonical metadata array index")
                    node = node[int(token)]
                else:
                    node = node[token]
            return node
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid metadata reference: {path}") from exc

    for at, source in references.items():
        if lookup(at) != {"$ref": "#" + source}:
            raise ValueError(f"Invalid metadata reference declaration: {at}")
        lookup(source)

    def decode(item: Any, path: str, active: frozenset[str]) -> Any:
        if path in references:
            if path in active:
                raise ValueError("Cyclic metadata storage reference")
            source = references[path]
            return decode(lookup(source), source, active | {path})
        if isinstance(item, dict):
            return {key: decode(child, path + "/" + _token(str(key)), active)
                    for key, child in item.items() if path or key != STORAGE_KEY}
        if isinstance(item, list):
            return [decode(child, path + "/" + str(index), active) for index, child in enumerate(item)]
        return item

    return decode(dict(value), "", frozenset())
