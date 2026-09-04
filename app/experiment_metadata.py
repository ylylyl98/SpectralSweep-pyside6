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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Protocol

SCHEMA_VERSION = 1
RUNNING = "running"
COMPLETED = "completed"
CANCELLED = "cancelled"
FAILED = "failed"
TERMINAL_STATUSES = {COMPLETED, CANCELLED, FAILED}


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
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "item"):
        try:
            return _jsonable(value.item())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return str(value)


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
        self._terminal = False
        self._allow_post_completion = bool(allow_post_completion)
        self._write()

    def _write(self) -> None:
        with self._lock:
            _atomic_json(self.path, self.metadata)
            self.service.history.upsert(self.metadata, local_metadata_path=self.path)

    def register_file(self, path: str | Path, role: str = "raw", kind: Optional[str] = None,
                      details: Optional[Mapping[str, Any]] = None) -> str:
        if self._terminal and not self._allow_post_completion:
            raise RuntimeError("Cannot register files after experiment is terminal")
        path = Path(path)
        entry: dict[str, Any] = {"path": _portable_path(path, self.service.output_root), "role": str(role)}
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

    def update_observed(self, values: Mapping[str, Any]) -> None:
        if self._terminal:
            raise RuntimeError("Cannot update a terminal experiment")
        self.metadata.setdefault("observed", {}).update(_jsonable(values))
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

    def _finish(self, status: str, *, summary: Optional[Mapping[str, Any]] = None,
                reason: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            if status not in TERMINAL_STATUSES:
                raise ValueError(f"Unsupported terminal status: {status}")
            if self._terminal:
                raise RuntimeError("Experiment is already terminal")
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
        portable_requested = _portable_settings(settings or {}, root)
        metadata: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "experiment_id": experiment_id,
            "experiment_type": exp_type,
            "started_utc": utc_now(),
            "started_at": utc_now(),
            "status": RUNNING,
            "device_id": device_id,
            "device": {"device_id": device_id},
            "files": [],
            "instruments": _jsonable(list(instruments or [])),
            "software": {"name": "SpectralSweep", "version": None, **_jsonable(dict(software or {}))},
            "safety_policy": _portable_settings(safety_policy or {}, root),
            "settings": {"schema_version": SCHEMA_VERSION, "requested": portable_requested, "loadable": self.loadable_settings(portable_requested)},
            "observed": {},
            "summary": {},
            "result": {},
        }
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
    "SCHEMA_VERSION", "RUNNING", "COMPLETED", "CANCELLED", "FAILED",
    "ExperimentHistory", "ExperimentMetadataService", "ExperimentRun",
    "ExperimentSettingsAdapter",
    "MetadataService", "HistoryStore",
]
