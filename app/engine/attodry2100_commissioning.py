"""Durable evidence helpers for attoDRY2100 commissioning.

The helper deliberately talks to the accepted controller surface only.  It is
not a second device-control path and performs no SDK calls itself.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional


SCHEMA_VERSION = 1


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "__dict__"):
        return _json_safe(vars(value))
    return str(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class CommissioningEvidenceRecorder:
    """Atomic JSON checkpoint containing every evidence event in order."""

    def __init__(self, path: str | Path, *, run_id: str = "") -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._state = {
            "schema_version": SCHEMA_VERSION,
            "run_id": str(run_id),
            "events": [],
        }
        self._checkpoint()

    @property
    def state(self) -> Mapping[str, Any]:
        with self._lock:
            return _json_safe(self._state)

    def record(self, event: str, **details: Any) -> Mapping[str, Any]:
        with self._lock:
            item = {
                "sequence": len(self._state["events"]) + 1,
                "timestamp_utc": _utc_now(),
                "event": str(event),
                **_json_safe(details),
            }
            self._state["events"].append(item)
            self._checkpoint()
            return dict(item)

    def record_snapshot(self, label: str, snapshot: Any) -> Mapping[str, Any]:
        return self.record("snapshot", label=label, snapshot=snapshot)

    def record_stabilization(
        self, target_t: float, *, stable: bool, snapshot: Any = None, reason: str = ""
    ) -> Mapping[str, Any]:
        return self.record(
            "stabilization_decision",
            target_field_t=target_t,
            stable=bool(stable),
            snapshot=snapshot,
            reason=reason,
        )

    def _checkpoint(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(_json_safe(self._state), stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None and temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass


class AttoDRY2100CommissioningEvidence:
    """Record commissioning observations through public controller methods."""

    def __init__(self, controller: Any, path: str | Path, *, run_id: str = "") -> None:
        self.controller = controller
        self.recorder = CommissioningEvidenceRecorder(path, run_id=run_id)
        self.recorder.record("commissioning_started")

    def snapshot(self, label: str) -> Any:
        try:
            value = self.controller.read_snapshot()
        except BaseException as exc:
            self.recorder.record(
                "snapshot_error", label=label, error_type=type(exc).__name__, error=str(exc)
            )
            raise
        self.recorder.record_snapshot(label, value)
        return value

    def stabilization(
        self, target_t: float, *, stable: bool, snapshot: Any = None, reason: str = ""
    ) -> None:
        self.recorder.record_stabilization(
            target_t, stable=stable, snapshot=snapshot, reason=reason
        )

    def stop_and_observe(self) -> tuple[Any, Any]:
        self.recorder.record("stop_requested")
        try:
            acknowledgement = self.controller.stop_field_control()
        except BaseException as exc:
            self.recorder.record(
                "stop_error", error_type=type(exc).__name__, error=str(exc)
            )
            raise
        self.recorder.record("stop_ack", acknowledgement=acknowledgement)
        try:
            observation = self.controller.read_snapshot()
        except BaseException as exc:
            self.recorder.record(
                "stop_verification_error",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
        self.recorder.record(
            "stop_verification_observation", snapshot=observation
        )
        return acknowledgement, observation

    @staticmethod
    def _resolve(value: Any) -> Any:
        result = getattr(value, "result", None)
        return result() if callable(result) else value

    def disconnect(self) -> Any:
        try:
            result = self._resolve(self.controller.disconnect_async())
        except BaseException as exc:
            self.recorder.record(
                "disconnect_error", error_type=type(exc).__name__, error=str(exc)
            )
            raise
        self.recorder.record("disconnect_result", result=result)
        return result

    def shutdown(self) -> Any:
        try:
            result = self.controller.shutdown()
        except BaseException as exc:
            self.recorder.record(
                "shutdown_error", error_type=type(exc).__name__, error=str(exc)
            )
            raise
        self.recorder.record("shutdown_result", result=result)
        return result

    def cleanup(self) -> None:
        """Attempt both lifecycle steps, checkpointing each result/error."""
        errors = []
        for name, operation in (("disconnect", self.disconnect), ("shutdown", self.shutdown)):
            try:
                operation()
            except BaseException as exc:
                errors.append(exc)
                self.recorder.record(
                    "cleanup_error",
                    operation=name,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        if errors:
            raise errors[0]
