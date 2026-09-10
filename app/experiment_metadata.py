"""Portable experiment metadata and local history index.

The JSON sidecar written by this module is deliberately independent of Qt and
the rest of SpectralSweep.  It is therefore safe to consume from a catalog
scanner using only the standard library.  SQLite is an optional convenience
index: an unavailable or corrupt index must never prevent a sidecar from
being finalized.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import uuid
import math
import threading
import hashlib
import subprocess
import logging
import weakref
import copy
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Protocol

# ``schema_version`` stays at 1 for readers shipped with older SpectralSweep
# releases.  New records advertise the additive event/settings contract in
# ``metadata_schema``; old readers can safely ignore those fields.
SCHEMA_VERSION = 1
METADATA_SCHEMA_VERSION = 2
RUNNING = "running"
COMPLETED = "completed"
CANCELLED = "cancelled"
FAILED = "failed"
TERMINAL_STATUSES = {COMPLETED, CANCELLED, FAILED}
log = logging.getLogger(__name__)

# A process-local run must have one owner.  Apart from preventing duplicate
# event indexes this keeps late export callbacks from replacing a fresh
# metadata snapshot with the stale copy held by another handle.
_OPEN_RUNS: "weakref.WeakValueDictionary[str, ExperimentRun]" = weakref.WeakValueDictionary()
_OPEN_RUNS_LOCK = threading.RLock()


class ExperimentSettingsAdapter(Protocol):
    """Minimal optional panel contract; legacy panels can remain unmodified."""

    def get_experiment_type(self) -> str: ...
    def get_settings_snapshot(self) -> Mapping[str, Any]: ...

    def apply_saved_settings(self, settings: Mapping[str, Any]) -> Mapping[str, Any]: ...

# These names are recorded for provenance but are never restored from history.
SAFETY_KEYWORDS = (
    "safety", "maximum", "minimum", "limit", "interlock", "ip", "address",
    "serial", "firmware", "com_port", "visa", "resource", "compliance",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _jsonable(value: Any) -> Any:
    """Convert values to lossless, JSON-native provenance values.

    NumPy arrays used to fall through to ``str(value)`` which truncates large
    arrays with an ellipsis.  Arrays now carry dtype/shape and every element.
    Unsupported objects raise instead of silently turning into an ambiguous
    string.  The function intentionally avoids importing NumPy so metadata
    remains usable in catalog-only installations.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, set):
        return [_jsonable(v) for v in sorted(value, key=repr)]
    # DataFrames are common for batch tables and executed plans.  Walking
    # ``__dict__`` exposes pandas internals (Flags, managers, references) and
    # is neither portable nor lossless.  Keep a small explicit table schema.
    if type(value).__name__ == "DataFrame" and hasattr(value, "to_dict"):
        try:
            return {
                "__type__": "dataframe",
                "columns": [str(column) for column in value.columns.tolist()],
                "index": _jsonable(value.index.tolist()),
                "dtypes": {str(column): str(dtype) for column, dtype in value.dtypes.items()},
                "data": _jsonable(value.astype(object).where(value.notna(), None).to_dict(orient="records")),
            }
        except Exception as exc:
            raise TypeError(f"Could not serialize DataFrame: {exc}") from exc
    # ndarray-like values must be checked before ``item``.  A scalar ndarray
    # has item(), while a multidimensional ndarray may expose a huge repr.
    shape = getattr(value, "shape", None)
    dtype = getattr(value, "dtype", None)
    tolist = getattr(value, "tolist", None)
    if shape is not None and dtype is not None and callable(tolist):
        try:
            return {
                "__type__": "ndarray",
                "dtype": str(dtype),
                "shape": [int(dim) for dim in shape],
                "data": _jsonable(tolist()),
            }
        except Exception as exc:
            raise TypeError(f"Could not serialize array value: {exc}") from exc
    if hasattr(value, "item"):
        try:
            return _jsonable(value.item())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        # Explicit object state is useful for simple legacy settings objects,
        # but a type tag prevents a future reader mistaking it for a scalar.
        try:
            return {"__type__": "object", "class": type(value).__name__,
                    "attributes": _jsonable(vars(value))}
        except Exception as exc:
            raise TypeError(f"Could not serialize object {type(value).__name__}: {exc}") from exc
    raise TypeError(f"Unsupported metadata value type: {type(value).__name__}")


def _from_jsonable(value: Any) -> Any:
    """Decode the lossless markers emitted by :func:`_jsonable`.

    NumPy is optional; when unavailable an ndarray is returned as a typed
    mapping so catalog tools still retain the complete data.
    """
    if isinstance(value, list):
        return [_from_jsonable(item) for item in value]
    if isinstance(value, dict):
        if value.get("__type__") == "dataframe":
            data = _from_jsonable(value.get("data", []))
            columns = list(value.get("columns", []))
            index = _from_jsonable(value.get("index", []))
            try:
                import pandas as pd
                frame = pd.DataFrame(data, columns=columns)
                if index:
                    frame.index = index
                for column, dtype in dict(value.get("dtypes", {})).items():
                    try:
                        frame[column] = frame[column].astype(dtype)
                    except (TypeError, ValueError):
                        pass
                return frame
            except Exception:
                return {"columns": columns, "index": index, "dtypes": value.get("dtypes", {}), "data": data}
        if value.get("__type__") == "ndarray":
            data = _from_jsonable(value.get("data"))
            try:
                import numpy as np
                return np.asarray(data, dtype=value.get("dtype")).reshape(tuple(value.get("shape", ())))
            except Exception:
                return {"dtype": value.get("dtype"), "shape": value.get("shape"), "data": data}
        if value.get("__type__") == "object":
            return dict(value.get("attributes", {}))
        return {key: _from_jsonable(item) for key, item in value.items()}
    return value


def _sha256(path: Path) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


@lru_cache(maxsize=1)
def _software_provenance() -> dict[str, Any]:
    """Read cheap process-wide software identity once, without hardware I/O."""
    result: dict[str, Any] = {"name": "SpectralSweep", "version": None,
                              "commit": None, "dirty": None}
    try:
        root = Path(__file__).resolve().parents[1]
        commit = subprocess.run(("git", "-C", str(root), "rev-parse", "HEAD"),
                                capture_output=True, text=True, timeout=1,
                                check=False).stdout.strip()
        dirty = subprocess.run(("git", "-C", str(root), "status", "--porcelain"),
                               capture_output=True, text=True, timeout=1,
                               check=False).stdout
        result.update({"commit": commit or None, "dirty": bool(dirty.strip()),
                       "repository": root.name})
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(_jsonable(value), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _default_history_path() -> Path:
    override = os.environ.get("SPECTRALSWEEP_HISTORY_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "SpectralSweep" / "experiment_history.sqlite"


def _portable_path(path: Path, root: Path) -> str:
    path = path.resolve()
    root = root.resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Output file is outside experiment root: {path}") from exc
    return relative.as_posix()


def _portable_settings(value: Any, root: Path, key: str = "") -> Any:
    """Keep settings JSON portable even when a legacy panel includes paths."""
    if isinstance(value, Mapping):
        return {str(k): _portable_settings(v, root, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_portable_settings(v, root, key) for v in value]
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            try:
                return candidate.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                # Preserve the fact that a path-like setting existed without
                # leaking the machine-specific location into the sidecar.
                return candidate.name
    return _jsonable(value)


def instrument_inventory(**controllers: Any) -> list[dict[str, Any]]:
    """Normalize the shared controller identities stored in experiment setup."""
    result = []
    for role, controller in controllers.items():
        if controller is None:
            continue
        identity = getattr(controller, "identity", None)
        if callable(identity):
            try:
                identity = identity()
            except Exception:
                identity = None
        if not isinstance(identity, Mapping):
            identity = {}
        connected = getattr(controller, "is_connected", False)
        if callable(connected):
            try:
                connected = connected()
            except Exception:
                connected = False
        result.append({"role": str(role), "identity": _jsonable(dict(identity)),
                       "connected": bool(connected)})
    return result


class ExperimentHistory:
    """Small SQLite index.  All methods are best-effort and return empty data
    when the local database is unavailable; portable JSON remains authoritative.
    """

    def __init__(self, path: Optional[str | Path] = None):
        self.path = Path(path) if path is not None else _default_history_path()
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=2)
        try:
            with self._schema_lock:
                if not self._schema_ready:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute(
                        """CREATE TABLE IF NOT EXISTS experiments (
                            experiment_id TEXT PRIMARY KEY,
                            device_id TEXT NOT NULL,
                            experiment_type TEXT NOT NULL,
                            started_utc TEXT NOT NULL,
                            completed_utc TEXT,
                            status TEXT NOT NULL,
                            metadata_path TEXT NOT NULL,
                            settings_json TEXT NOT NULL,
                            summary_json TEXT NOT NULL
                        )"""
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_experiments_device_type "
                        "ON experiments(device_id, experiment_type, started_utc DESC)"
                    )
                    connection.commit()
                    self._schema_ready = True
            return connection
        except BaseException:
            connection.close()
            raise

    def upsert(self, metadata: Mapping[str, Any], *, local_metadata_path: Optional[str | Path] = None) -> None:
        db = None
        try:
            db = self._connect()
            db.execute(
                    """INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(experiment_id) DO UPDATE SET
                    device_id=excluded.device_id,
                    experiment_type=excluded.experiment_type,
                    started_utc=excluded.started_utc,
                    completed_utc=excluded.completed_utc,
                    status=excluded.status,
                    metadata_path=excluded.metadata_path,
                    settings_json=excluded.settings_json,
                    summary_json=excluded.summary_json""",
                    (
                        metadata["experiment_id"], metadata["device_id"],
                        metadata["experiment_type"], metadata["started_utc"],
                        metadata.get("completed_utc"), metadata["status"],
                        str(Path(local_metadata_path).resolve()) if local_metadata_path is not None else metadata["metadata_path"],
                        json.dumps(metadata.get("settings", {}), sort_keys=True),
                        json.dumps(metadata.get("summary", {}), sort_keys=True),
                    ),
            )
            db.commit()
        except (OSError, sqlite3.Error):
            return
        finally:
            if db is not None:
                db.close()

    def query(self, device_id: str, experiment_type: str, limit: int = 100) -> list[dict[str, Any]]:
        db = None
        try:
            db = self._connect()
            rows = db.execute(
                    "SELECT experiment_id, device_id, experiment_type, started_utc, "
                    "completed_utc, status, metadata_path, settings_json, summary_json "
                    "FROM experiments WHERE device_id=? AND experiment_type=? "
                    "ORDER BY started_utc DESC LIMIT ?",
                    (str(device_id), str(experiment_type), max(1, int(limit))),
            ).fetchall()
        except (OSError, sqlite3.Error, ValueError):
            return []
        finally:
            if db is not None:
                db.close()
        result = []
        for row in rows:
            item = dict(zip(("experiment_id", "device_id", "experiment_type", "started_utc",
                             "completed_utc", "status", "metadata_path"), row[:7]))
            item["started_at"] = item["started_utc"]
            item["completed_at"] = item["completed_utc"]
            for key, raw in (("settings", row[7]), ("summary", row[8])):
                try:
                    item[key] = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    item[key] = {}
            result.append(item)
        return result


class ExperimentRun:
    def __init__(self, service: "ExperimentMetadataService", metadata: dict[str, Any], path: Path, *, allow_post_completion: bool = False):
        self.service = service
        self.metadata = metadata
        self.path = path
        self.experiment_id = metadata["experiment_id"]
        self._lock = threading.RLock()
        self._terminal = metadata.get("status") in TERMINAL_STATUSES
        self._allow_post_completion = bool(allow_post_completion)
        self._event_index = 0
        self._compatibility_states: dict[str, dict[str, Any]] = {}
        self._events_since_checkpoint = 0
        self._checkpoint_interval = 25
        event_name = self.path.name.replace(".experiment.metadata.json", ".experiment.events.jsonl")
        if event_name == self.path.name:
            event_name = f"{self.path.stem}.experiment.events.jsonl"
        self._event_path = self.path.with_name(event_name)
        # Recover a crash-truncated tail once when opening the run.  Doing
        # this in every capture would turn a long sweep into quadratic I/O.
        self._repair_event_tail()
        durable_events = []
        if self._event_path.exists():
            try:
                with self._event_path.open(encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            event_record = json.loads(line)
                            durable_events.append(event_record)
                            self._event_index = max(self._event_index, int(event_record.get("event_index", 0)))
                        except (ValueError, TypeError, json.JSONDecodeError):
                            continue
            except OSError:
                pass
        self._rebuild_durable_summary(durable_events)
        self._replay_compatibility_events(durable_events)
        with _OPEN_RUNS_LOCK:
            # ``ExperimentMetadataService.begin/open_run`` hold this lock
            # through construction, so this insertion cannot race another
            # writer.  The constructor is kept private in practice.
            _OPEN_RUNS[str(self.path)] = self
        self._write()

    def _write(self) -> None:
        with self._lock:
            _atomic_json(self.path, self.metadata)
            self.service.history.upsert(self.metadata, local_metadata_path=self.path)

    def _rebuild_durable_summary(self, events: Iterable[Mapping[str, Any]]) -> None:
        """Recover bounded acquisition counters after reopening a run."""
        captures = [event for event in events
                    if str(event.get("event")) == "capture" and event.get("acquisition_id")]
        state = self.metadata.setdefault("acquisitions", {"count": 0, "outputs": []})
        state["count"] = len(captures)
        state["total_count"] = len(captures)
        state["recent_ids"] = [str(event["acquisition_id"]) for event in captures[-100:]]
        state["outputs"] = [
            {"acquisition_id": str(event["acquisition_id"]), **dict(event.get("output", {}))}
            for event in captures[-100:] if isinstance(event.get("output"), Mapping)
        ]

    def register_file(self, path: str | Path, role: str = "raw", kind: Optional[str] = None,
                      details: Optional[Mapping[str, Any]] = None,
                      *, external: bool = False,
                      frozen_identity: Optional[Mapping[str, Any]] = None) -> str:
        if self._terminal and not self._allow_post_completion:
            raise RuntimeError("Cannot register files after experiment is terminal")
        path = Path(path)
        # Processing inputs must describe the bytes used by the computation.
        # The source may be replaced before export, so callers can provide the
        # compute-time identity and avoid silently hashing a different file.
        frozen = dict(frozen_identity or {})
        external_digest = (str(frozen.get("sha256")) if frozen.get("sha256") else None)
        if external and not frozen:
            external_digest = _sha256(path)
        try:
            portable = _portable_path(path, self.service.output_root)
        except ValueError:
            if not external:
                raise
            # Keep external inputs portable and collision-resistant.  The
            # original machine path is deliberately omitted from the sidecar.
            token = external_digest[:16] if external_digest else uuid.uuid5(
                uuid.NAMESPACE_URL, str(path.resolve())).hex[:16]
            portable = f"external/{token}_{path.name}"
        entry: dict[str, Any] = {"path": portable, "role": str(role)}
        if external:
            entry["external"] = True
            entry["name"] = path.name
            digest = external_digest
            if digest:
                entry["sha256"] = digest
            if frozen:
                if frozen.get("size_bytes") is not None:
                    entry["size_bytes"] = int(frozen["size_bytes"])
                entry["identity_captured_utc"] = frozen.get("captured_utc")
                entry["identity_source"] = "compute_time"
        if kind:
            entry["kind"] = str(kind)
        if details:
            entry.update({str(key): _jsonable(value) for key, value in details.items()
                          if str(key) not in {"path", "role"} and value is not None})
        files = self.metadata.setdefault("files", [])
        existing = next((item for item in files if item.get("path") == entry["path"]), None)
        if existing is None:
            files.append(entry)
        else:
            # Registration is incremental and idempotent: later observers can
            # add direction/partial details without duplicating the file.
            existing.update(entry)
        self._write()
        return entry["path"]

    @property
    def event_path(self) -> Path:
        return self._event_path

    def record_event(self, event: str, *, acquisition_id: Optional[str] = None,
                     condition_id: Optional[str] = None,
                     settings_id: Optional[int | str] = None,
                     output: Optional[Mapping[str, Any]] = None,
                     **values: Any) -> Optional[dict[str, Any]]:
        """Append one backend-neutral event to this run's JSONL journal.

        The journal is intentionally separate from the authoritative JSON so
        long acquisitions do not rewrite large arrays or plans at every point.
        It is safe for worker threads and may be read after a partial run.
        """
        with self._lock:
            if self._terminal and not self._allow_post_completion:
                return None
            self._event_index += 1
            appended = False
            record: dict[str, Any] = {
                "event_id": str(uuid.uuid4()), "event_index": self._event_index,
                "event": str(event), "experiment_id": self.experiment_id,
                "run_id": self.experiment_id, "utc": utc_now(),
            }
            for key, value in (("acquisition_id", acquisition_id),
                               ("condition_id", condition_id),
                               ("settings_id", settings_id)):
                if value is not None:
                    record[key] = str(value) if key != "settings_id" else value
            if output is not None:
                record["output"] = _jsonable(dict(output))
            record.update({str(k): _jsonable(v) for k, v in values.items() if v is not None})
            try:
                self._event_path.parent.mkdir(parents=True, exist_ok=True)
                with self._event_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, allow_nan=False, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                appended = True
                if not self.metadata.get("event_log"):
                    self.metadata["event_log"] = {
                        "path": _portable_path(self._event_path, self.service.output_root),
                        "format": "jsonl", "append_only": True,
                        "index": "event_index",
                    }
                if not any(item.get("path") == self.metadata["event_log"]["path"]
                           for item in self.metadata.setdefault("files", [])):
                    self._register_event_file()
                # Counters describe durable events only.  They are checkpointed
                # periodically and always at terminalization, avoiding a full
                # manifest/index rewrite for every capture.
                if str(event) in {"capture", "capture_started"} and acquisition_id is not None:
                    acquisition_state = self.metadata.setdefault("acquisitions", {"count": 0, "outputs": []})
                    acquisition_state["count"] = int(acquisition_state.get("count", 0)) + 1
                    acquisition_state["total_count"] = acquisition_state["count"]
                    acquisition_state.setdefault("recent_ids", []).append(str(acquisition_id))
                    acquisition_state["recent_ids"] = acquisition_state["recent_ids"][-100:]
                    if output is not None:
                        acquisition_state.setdefault("outputs", []).append({
                            "acquisition_id": str(acquisition_id), **_jsonable(dict(output))})
                        acquisition_state["outputs"] = acquisition_state["outputs"][-100:]
                self._events_since_checkpoint += 1
                if self._events_since_checkpoint >= self._checkpoint_interval:
                    self._events_since_checkpoint = 0
                    self._write()
            except Exception as exc:
                # The JSONL line is authoritative once fsync returned.  Keep
                # its index even if the subsequent manifest checkpoint failed;
                # otherwise the next event would reuse an existing index.
                if not appended:
                    self._event_index -= 1
                self.metadata.setdefault("metadata_warnings", []).append({
                    "event": str(event), "error": f"{type(exc).__name__}: {exc}"})
                self.metadata["metadata_status"] = "degraded"
                self.metadata["metadata_error"] = {"event": str(event), "error": f"{type(exc).__name__}: {exc}"}
                log.warning("Could not append experiment event", exc_info=True)
                return None
            return record

    @staticmethod
    def _apply_compatibility_delta(state: dict[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(state)
        for key in event.get("removed", []) or []:
            result.pop(str(key), None)
        for key, value in dict(event.get("changes", {})).items():
            result[str(key)] = _from_jsonable(value)
        for key, tail in dict(event.get("list_tails", {})).items():
            result.setdefault(str(key), [])
            result[str(key)] = list(result[str(key)]) + list(_from_jsonable(tail))
        return result

    def _replay_compatibility_events(self, events: Iterable[Mapping[str, Any]]) -> None:
        for event in events:
            if event.get("event") != "compatibility_state" or not event.get("key"):
                continue
            key = str(event["key"])
            self._compatibility_states[key] = self._apply_compatibility_delta(
                self._compatibility_states.get(key, {}), event
            )

    def record_compatibility_state(self, key: str, payload: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        """Durably record a compact compatibility projection delta."""
        with self._lock:
            name = str(key)
            new = dict(_jsonable(dict(payload)))
            old = self._compatibility_states.get(name, {})
            changes, removed, tails = {}, [], {}
            for field, value in new.items():
                if isinstance(value, list) and isinstance(old.get(field), list) and value[:len(old[field])] == old[field]:
                    if len(value) > len(old[field]):
                        tails[field] = value[len(old[field]):]
                elif field not in old or old[field] != value:
                    changes[field] = value
            removed = [field for field in old if field not in new]
            event = self.record_event("compatibility_state", key=name,
                                      changes=changes, removed=removed, list_tails=tails)
            if event is None:
                return None
            self._compatibility_states[name] = new
            return copy.deepcopy(new)

    def reconstruct_compatibility_state(self, key: str) -> dict[str, Any]:
        state: dict[str, Any] = {}
        try:
            with self._event_path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("event") == "compatibility_state" and str(event.get("key")) == str(key):
                        state = self._apply_compatibility_delta(state, event)
        except OSError:
            pass
        return state

    # Public spelling used by adapters that do not need the semantic helpers.
    append_event = record_event

    def _register_event_file(self, *, kind: str = "experiment_events") -> None:
        files = self.metadata.setdefault("files", [])
        portable = _portable_path(self._event_path, self.service.output_root)
        existing = next((item for item in files if item.get("path") == portable), None)
        if existing is None:
            files.append({"path": portable, "role": "metadata", "kind": kind})
        elif kind != "experiment_events":
            existing["kind"] = kind
        # The event itself is already durable.  Persist the association once;
        # repeating this write for every event defeats the append-only design.
        self._write()

    def _repair_event_tail(self) -> None:
        if not self._event_path.exists():
            return
        try:
            data = self._event_path.read_bytes()
        except OSError:
            return
        if not data or data.endswith(b"\n"):
            return
        split = data.rfind(b"\n")
        tail = data[split + 1:] if split >= 0 else data
        try:
            json.loads(tail.decode("utf-8"))
            # A complete final record without a newline is valid JSONL after
            # normalization; add the delimiter before the next append.
            self._event_path.write_bytes(data + b"\n")
            return
        except (UnicodeDecodeError, json.JSONDecodeError):
            quarantine = self._event_path.with_suffix(self._event_path.suffix + ".partial")
            try:
                quarantine.write_bytes(tail)
                self._event_path.write_bytes(data[:split + 1] if split >= 0 else b"")
            except OSError:
                raise

    def register_settings_snapshot(self, settings: Mapping[str, Any], *, source: str = "instrument",
                                    observed_utc: Optional[str] = None) -> int:
        with self._lock:
            canonical_json = json.dumps(_jsonable(settings), sort_keys=True, separators=(",", ":"), allow_nan=False)
            canonical = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
            snapshots = self.metadata.setdefault("settings", {}).setdefault("snapshots", [])
            for item in snapshots:
                if item.get("canonical") == canonical:
                    return int(item["settings_id"])
            settings_id = len(snapshots) + 1
            snapshots.append({"settings_id": settings_id, "source": str(source),
                              "observed_utc": observed_utc or utc_now(),
                              "canonical": canonical, "values": _jsonable(settings),
                              "canonical_format": "sha256-json"})
            self._write()
            return settings_id

    add_settings_snapshot = register_settings_snapshot

    def register_condition(self, condition: Mapping[str, Any], *, condition_id: Optional[str] = None,
                           requested: Optional[Mapping[str, Any]] = None,
                           applied: Optional[Mapping[str, Any]] = None,
                           observed: Optional[Mapping[str, Any]] = None) -> str:
        """Register one immutable plan condition with requested/applied/readback layers."""
        with self._lock:
            conditions = self.metadata.setdefault("conditions", [])
            cid = str(condition_id or f"condition-{len(conditions) + 1}")
            if not any(str(item.get("condition_id")) == cid for item in conditions):
                conditions.append({"condition_id": cid,
                                   "requested": _jsonable(dict(requested or condition)),
                                   "applied": _jsonable(dict(applied or {})),
                                   "observed": _jsonable(dict(observed or {})),
                                   "values": _jsonable(dict(condition))})
                self._write()
            return cid

    add_condition = register_condition

    def record_capture(self, *, acquisition_id: Optional[str] = None,
                       condition_id: Optional[str] = None, settings_id: Optional[int] = None,
                       purpose: str = "measurement", output: Optional[Mapping[str, Any]] = None,
                       **values: Any) -> Optional[dict[str, Any]]:
        acquisition_id = acquisition_id or str(uuid.uuid4())
        return self.record_event("capture", acquisition_id=acquisition_id,
                                 condition_id=condition_id, settings_id=settings_id,
                                 purpose=purpose, output=output, **values)

    def record_observation(self, values: Mapping[str, Any], **links: Any) -> Optional[dict[str, Any]]:
        return self.record_event("observation", observations=values, **links)

    def record_processing(self, values: Mapping[str, Any], **links: Any) -> Optional[dict[str, Any]]:
        return self.record_event("processing", processing=values, **links)

    def record_export(self, values: Mapping[str, Any], **links: Any) -> Optional[dict[str, Any]]:
        return self.record_event("export", export=values, **links)

    def read_events(self, *, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        """Read a bounded slice, tolerating a truncated last JSONL line."""
        if limit <= 0:
            return []
        records = []
        try:
            with self._event_path.open(encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if offset > 0:
                        offset -= 1
                        continue
                    records.append(record)
                    if len(records) >= limit:
                        break
        except OSError:
            pass
        return records

    def update_observed(self, values: Mapping[str, Any]) -> None:
        if self._terminal:
            raise RuntimeError("Cannot update a terminal experiment")
        self.metadata.setdefault("observed", {}).update(_jsonable(values))
        self._write()

    def update_applied(self, values: Mapping[str, Any]) -> None:
        if self._terminal:
            raise RuntimeError("Cannot update a terminal experiment")
        self.metadata.setdefault("settings", {}).setdefault("applied", {}).update(_jsonable(values))
        self._write()

    def update_summary(self, values: Mapping[str, Any]) -> None:
        if self._terminal:
            raise RuntimeError("Cannot update a terminal experiment")
        self.metadata.setdefault("summary", {}).update(_jsonable(values))
        self._write()

    def complete(self, summary: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        return self._finish(COMPLETED, summary=summary)

    def cancel(self, reason: Optional[str] = None) -> dict[str, Any]:
        return self._finish(CANCELLED, reason=reason)

    def fail(self, error: Any) -> dict[str, Any]:
        text = str(error)
        self.metadata["error"] = {"type": type(error).__name__, "message": text}
        return self._finish(FAILED)

    def mark_metadata_failure(self, error: Any, *, event: Optional[str] = None) -> None:
        """Persist a recorder failure while allowing hardware cleanup to continue."""
        with self._lock:
            self.metadata["metadata_status"] = "degraded"
            self.metadata["metadata_error"] = {
                "type": type(error).__name__, "message": str(error),
                **({"event": str(event)} if event else {}),
            }
            self.metadata.setdefault("metadata_warnings", []).append(self.metadata["metadata_error"])
            try:
                self._write()
            except Exception:
                log.warning("Could not persist metadata failure marker", exc_info=True)

    def _finish(self, status: str, *, summary: Optional[Mapping[str, Any]] = None,
                reason: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            if status not in TERMINAL_STATUSES:
                raise ValueError(f"Unsupported terminal status: {status}")
            if self._terminal:
                raise RuntimeError("Experiment is already terminal")
            if self.metadata.get("metadata_status") == "degraded":
                status = FAILED
                self.metadata.setdefault("error", {
                    "type": "MetadataRecordingError",
                    "message": "One or more metadata events could not be durably recorded",
                })
            self.metadata["status"] = status
            self.metadata["completed_utc"] = utc_now()
            self.metadata["completed_at"] = self.metadata["completed_utc"]
            if summary:
                self.metadata.setdefault("summary", {}).update(_jsonable(summary))
            self.metadata["result"] = {
                "status": status,
                "summary": self.metadata.get("summary", {}),
                "cancellation": None,
                "error": self.metadata.get("error"),
            }
            if reason:
                self.metadata["cancellation_reason"] = str(reason)
                self.metadata["result"]["cancellation"] = {"reason": str(reason)}
            self.metadata["result"]["error"] = self.metadata.get("error")
            self._write()
            self._terminal = True
            try:
                from .experiment_lifecycle import ExperimentTerminalEvent, publish
                publish(ExperimentTerminalEvent(self.experiment_id, self.metadata.get("experiment_type", ""), status))
            except Exception:
                pass
            return dict(self.metadata)

    def __enter__(self) -> "ExperimentRun":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is None:
            if self.metadata.get("status") == RUNNING:
                self.complete()
            return False
        if isinstance(exc, (KeyboardInterrupt,)):
            self.cancel(str(exc))
        else:
            self.fail(exc)
        return False


class ExperimentMetadataService:
    """Create and update schema-v1 experiment sidecars."""

    def __init__(self, output_root: str | Path, history_path: Optional[str | Path] = None):
        self.output_root = Path(output_root).expanduser().resolve()
        self.history = ExperimentHistory(history_path)

    def begin(
        self,
        experiment_type: str,
        device_id: str,
        *,
        output_dir: Optional[str | Path] = None,
        metadata_path: Optional[str | Path] = None,
        settings: Optional[Mapping[str, Any]] = None,
        instruments: Optional[Iterable[Mapping[str, Any]]] = None,
        device_label: Optional[str] = None,
        sample_id: Optional[str] = None,
        allow_post_completion: bool = False,
        software: Optional[Mapping[str, Any]] = None,
        safety_policy: Optional[Mapping[str, Any]] = None,
    ) -> ExperimentRun:
        device_id = str(device_id).strip()
        if not device_id:
            raise ValueError("device_id is required before an experiment starts")
        exp_type = str(experiment_type).strip()
        if not exp_type:
            raise ValueError("experiment_type is required")
        root = Path(output_dir).expanduser().resolve() if output_dir else self.output_root
        root.mkdir(parents=True, exist_ok=True)
        path = Path(metadata_path).expanduser().resolve() if metadata_path else None
        experiment_id = str(uuid.uuid4())
        path = path or (root / f"{experiment_id}.experiment.metadata.json")
        if path.parent != root and root not in path.parents:
            raise ValueError("metadata_path must be inside output_dir")
        with _OPEN_RUNS_LOCK:
            existing = _OPEN_RUNS.get(str(path))
            if existing is not None:
                existing._allow_post_completion = bool(existing._allow_post_completion or allow_post_completion)
                return existing
        if metadata_path is not None and path.exists():
            return self.open_run(path, allow_post_completion=allow_post_completion)
        portable_requested = _portable_settings(settings or {}, root)
        requested_plan = {}
        for plan_key in ("plan", "schedule", "acquisition_schedule", "sequence"):
            if isinstance(portable_requested, Mapping) and plan_key in portable_requested:
                requested_plan[plan_key] = portable_requested[plan_key]
        raw_conditions = portable_requested.get("conditions", []) if isinstance(portable_requested, Mapping) else []
        initial_conditions = []
        if isinstance(raw_conditions, list):
            for index, condition in enumerate(raw_conditions, 1):
                if isinstance(condition, Mapping):
                    initial_conditions.append({"condition_id": f"condition-{index}",
                                               "requested": condition, "applied": {},
                                               "observed": {}, "values": condition})
        metadata: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "metadata_schema": METADATA_SCHEMA_VERSION,
            "experiment_id": experiment_id,
            "run_id": experiment_id,
            "experiment_type": exp_type,
            "started_utc": utc_now(),
            "started_at": utc_now(),
            "status": RUNNING,
            "device_id": device_id,
            "device": {"device_id": device_id},
            "files": [],
            "instruments": _jsonable(list(instruments or [])),
            "software": {**_software_provenance(), **_jsonable(dict(software or {}))},
            "safety_policy": _portable_settings(safety_policy or {}, root),
            "settings": {"schema_version": SCHEMA_VERSION, "requested": portable_requested,
                         "applied": {}, "observed": {}, "snapshots": [],
                         "loadable": self.loadable_settings(portable_requested)},
            "observed": {},
            "conditions": [],
            "plan": requested_plan,
            "acquisitions": {"count": 0, "outputs": []},
            "summary": {},
            "result": {},
        }
        metadata["conditions"] = initial_conditions
        if device_label:
            metadata["device"]["device_label"] = str(device_label)
        if sample_id:
            metadata["device"]["sample_id"] = str(sample_id)
        metadata["metadata_path"] = _portable_path(path, self.output_root)
        # Include the sidecar association in the initial durable write.  Calling
        # register_file() after construction used to rewrite the same JSON and
        # SQLite row immediately, doubling run-start filesystem work.
        metadata["files"].append({
            "path": _portable_path(path, self.output_root),
            "role": "metadata",
            "kind": "experiment_metadata",
        })
        # Keep creation atomic with the registry lookup for callers that use
        # ``begin(..., metadata_path=...)`` concurrently.
        with _OPEN_RUNS_LOCK:
            existing = _OPEN_RUNS.get(str(path))
            if existing is not None:
                existing._allow_post_completion = bool(
                    existing._allow_post_completion or allow_post_completion
                )
                return existing
            return ExperimentRun(
                self,
                metadata,
                path,
                allow_post_completion=allow_post_completion,
            )

    @staticmethod
    def loadable_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
        """Return settings safe for a later apply operation.

        Safety/connection provenance remains in the sidecar's requested
        snapshot but is excluded from this normalized loadable subset.
        """
        def clean(value: Any, key: str = "") -> Any:
            if any(word in key.lower() for word in SAFETY_KEYWORDS):
                return None
            if isinstance(value, Mapping):
                return {str(k): cleaned for k, v in value.items()
                        if (cleaned := clean(v, str(k))) is not None}
            if isinstance(value, (list, tuple)):
                return [clean(v, key) for v in value]
            return _jsonable(value)
        return {str(k): value for k, v in settings.items()
                if (value := clean(v, str(k))) is not None}

    def query_history(self, device_id: str, experiment_type: str, limit: int = 100) -> list[dict[str, Any]]:
        return self.history.query(device_id, experiment_type, limit)

    def open_run(self, path: str | Path, *, allow_post_completion: bool = False) -> ExperimentRun:
        """Reopen a sidecar and continue a recoverable partial event journal."""
        sidecar = Path(path).expanduser().resolve()
        metadata = self.load_metadata(sidecar)
        output_root = self.output_root
        try:
            sidecar.relative_to(output_root)
        except ValueError:
            raise ValueError("metadata path must be inside this service output root")
        # Hold the registry lock through construction.  The constructor does
        # recovery and the initial write, so releasing the lock before it is
        # called lets two callers create competing writers for one journal.
        with _OPEN_RUNS_LOCK:
            existing = _OPEN_RUNS.get(str(sidecar))
            if existing is not None:
                existing._allow_post_completion = bool(
                    existing._allow_post_completion or allow_post_completion
                )
                return existing
            return ExperimentRun(self, metadata, sidecar, allow_post_completion=allow_post_completion)

    @staticmethod
    def load_metadata(path: str | Path, *, migrate: bool = True) -> dict[str, Any]:
        """Load current or legacy sidecars without touching instruments."""
        with Path(path).open(encoding="utf-8") as stream:
            raw = json.load(stream)
        if not isinstance(raw, Mapping):
            raise ValueError("Experiment metadata root must be an object")
        data = dict(raw)
        if migrate:
            data.setdefault("metadata_schema", METADATA_SCHEMA_VERSION if data.get("schema_version", 1) >= 1 else 1)
            data.setdefault("run_id", data.get("experiment_id"))
            data.setdefault("conditions", [])
            data.setdefault("acquisitions", {"count": 0, "outputs": []})
            settings = data.setdefault("settings", {})
            if isinstance(settings, Mapping):
                settings.setdefault("requested", dict(settings)) if not settings.get("requested") else None
                settings.setdefault("applied", {})
                settings.setdefault("observed", {})
                settings.setdefault("snapshots", [])
        return data

    @staticmethod
    def format_history_preview(metadata: Mapping[str, Any], *, event_limit: int = 20) -> str:
        """Create a bounded, readable history view shared by UI and scripts."""
        sections = []
        for title, key in (("Setup", "settings"), ("Plan", "plan"),
                           ("Conditions", "conditions"), ("Acquisitions", "acquisitions"),
                           ("Instruments", "instruments"),
                           ("Calibration", "calibration"), ("Observations", "observed"),
                           ("Files", "files"), ("Status", "result")):
            value = metadata.get(key, {})
            if value not in ({}, [], None):
                sections.append(f"[{title}]\n{json.dumps(value, indent=2, sort_keys=True, default=str)}")
        events = metadata.get("events_preview")
        if events:
            sections.append(f"[Events]\n{json.dumps(list(events)[:max(0, int(event_limit))], indent=2, sort_keys=True, default=str)}")
        return "\n\n".join(sections)

    @staticmethod
    def preview_settings(metadata: Mapping[str, Any]) -> dict[str, Any]:
        settings = metadata.get("settings", {})
        if isinstance(settings, Mapping) and isinstance(settings.get("loadable"), Mapping):
            requested = settings.get("requested", {})
            requested_keys = set(requested) if isinstance(requested, Mapping) else set()
            return {
                "loadable": dict(settings["loadable"]),
                "skipped": sorted(requested_keys - set(settings["loadable"])),
            }
        requested = settings.get("requested", settings) if isinstance(settings, Mapping) else {}
        loadable = ExperimentMetadataService.loadable_settings(requested if isinstance(requested, Mapping) else {})
        skipped = sorted(set(requested) - set(loadable)) if isinstance(requested, Mapping) else []
        return {"loadable": loadable, "skipped": skipped}

    @staticmethod
    def apply_saved_settings(metadata: Mapping[str, Any], adapter: Any) -> dict[str, Any]:
        """Apply only through a panel's explicit safe adapter method."""
        preview = ExperimentMetadataService.preview_settings(metadata)
        method = getattr(adapter, "apply_saved_experiment_settings", None)
        if not callable(method):
            return {"applied": [], "skipped": sorted(preview["loadable"]) + list(preview.get("skipped", []))}
        report = method(preview["loadable"])
        if not isinstance(report, Mapping):
            return {"applied": [], "skipped": sorted(preview["loadable"])}
        return {
            "applied": list(report.get("applied", [])),
            "skipped": list(report.get("skipped", [])) + list(preview.get("skipped", [])),
        }


# Short aliases used by panels and external tooling.
MetadataService = ExperimentMetadataService
HistoryStore = ExperimentHistory


__all__ = [
    "SCHEMA_VERSION", "METADATA_SCHEMA_VERSION", "RUNNING", "COMPLETED", "CANCELLED", "FAILED",
    "ExperimentHistory", "ExperimentMetadataService", "ExperimentRun",
    "ExperimentSettingsAdapter",
    "MetadataService", "HistoryStore", "_jsonable", "_from_jsonable",
    "instrument_inventory",
]
