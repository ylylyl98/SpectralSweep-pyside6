from __future__ import annotations

import json
import math
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


INCIDENT_FILENAME = "hardware_incidents.jsonl"
SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    try:
        if hasattr(value, "item"):
            return _json_safe(value.item())
    except Exception:
        pass
    return str(value)


class HardwareIncidentRecorder:
    """Append-only hardware incident journal stored beside acquisition CSVs."""

    _write_lock = threading.Lock()

    def __init__(self, out_dir: Path, filename: str = INCIDENT_FILENAME) -> None:
        self.path = Path(out_dir) / filename

    def write(self, incident: Mapping[str, Any]) -> Path:
        record: Dict[str, Any] = dict(incident)
        record.setdefault("schema_version", SCHEMA_VERSION)
        record.setdefault("incident_id", uuid.uuid4().hex)
        record.setdefault("recorded_at_utc", utc_now_iso())
        payload = json.dumps(
            _json_safe(record),
            ensure_ascii=False,
            separators=(",", ":"),
        )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(payload + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        return self.path


def build_hardware_incident(
    error: BaseException,
    *,
    stage: str,
    run_context: Optional[Mapping[str, Any]] = None,
    cleanup: Optional[Mapping[str, Any]] = None,
    traceback_text: str = "",
) -> Dict[str, Any]:
    details_fn = getattr(error, "to_incident_dict", None)
    if callable(details_fn):
        hardware = details_fn()
    else:
        hardware = {
            "error_type": type(error).__name__,
            "error": str(error),
        }

    context = dict(run_context or {})
    error_context = hardware.get("context")
    if isinstance(error_context, Mapping):
        context = {**dict(error_context), **context}

    return {
        "schema_version": SCHEMA_VERSION,
        "incident_id": uuid.uuid4().hex,
        "recorded_at_utc": utc_now_iso(),
        "kind": "smu_communication_failure",
        "stage": str(stage),
        "summary": str(error),
        "run_context": context,
        "hardware": hardware,
        "cleanup": dict(cleanup or {}),
        "traceback": str(traceback_text or ""),
        "operator_action": (
            "Do not resume this run. Verify the instrument and wiring, then "
            "disconnect and reconnect the SMUs so the software can reinitialize them."
        ),
    }


def incident_display_text(incident: Mapping[str, Any]) -> str:
    hardware = incident.get("hardware") or {}
    diagnosis = hardware.get("diagnosis") or {}
    context = incident.get("run_context") or {}
    role = hardware.get("role") or "unknown SMU"
    address = hardware.get("address") or "unknown address"
    operation = hardware.get("operation") or "unknown operation"
    command = hardware.get("command") or "unknown command"
    timeout_ms = hardware.get("timeout_ms")
    frame = context.get("frame")
    frame_total = context.get("frame_total")
    frame_text = (
        f"{frame}/{frame_total}"
        if frame is not None and frame_total is not None
        else "unknown"
    )
    reason = diagnosis.get("summary") or str(incident.get("summary") or "")
    timeout_text = f" after a {timeout_ms} ms timeout" if timeout_ms else ""
    return (
        f"{role} ({address}) stopped responding during {operation} at frame "
        f"{frame_text}{timeout_text}. Failed command: {command}. {reason}"
    )
