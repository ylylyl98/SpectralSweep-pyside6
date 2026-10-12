"""Small adapter used by existing save paths.

Keeping this bridge separate means legacy CSV writers stay unchanged while all
new output receives the same schema-v1 sidecar and history entry.
"""
from __future__ import annotations

from pathlib import Path
import json
import hashlib
from typing import Any, Iterable, Mapping, Optional

from .experiment_metadata import (
    ExperimentMetadataService, ExperimentRun, METADATA_SCHEMA_VERSION,
    _jsonable, _portable_settings, _sha256, utc_now,
)


def _raw_output_paths(metadata: Mapping[str, Any]) -> set[str]:
    files = metadata.get("files", [])
    if not isinstance(files, list) or any(not isinstance(item, Mapping) for item in files):
        raise ValueError("Invalid output metadata file list")
    paths = [item.get("path") for item in files if item.get("role") == "raw"]
    if any(not isinstance(path, str) for path in paths):
        raise ValueError("Invalid output metadata file path")
    return set(paths)


def persist_output_snapshot(
    output_file: str | Path, *, experiment_type: str, device_id: str,
    settings: Mapping[str, Any], instruments: Iterable[Mapping[str, Any]] = (),
    context: Optional[Mapping[str, Any]] = None, sample_id: Optional[str] = None,
    kind: str = "data", summary: Optional[Mapping[str, Any]] = None,
    reuse_legacy: bool = False,
) -> Path:
    """Own one current metadata record per exported output path.

    An unchanged export is a no-op. Overwriting the output refreshes its one
    record and journals the new content hash, rather than leaving multiple
    acquisition sidecars claiming to describe the same mutable file.
    """
    output = Path(output_file).expanduser().resolve()
    service = ExperimentMetadataService(output.parent)
    requested = _portable_settings(settings, output.parent)
    inventory = _jsonable(list(instruments))
    context = _jsonable(dict(context or {}))
    summary = _jsonable(dict(summary or {}))
    digest = _sha256(output)
    if digest is None:
        raise OSError(f"Cannot hash exported output: {output}")
    signature = hashlib.sha256(json.dumps(
        {"file_sha256": digest, "settings": requested, "instruments": inventory,
         "context": context, "sample_id": sample_id, "device_id": device_id,
         "kind": kind, "summary": summary},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    sidecar = output.with_name(f"{output.name}.experiment.metadata.json")
    if reuse_legacy and not sidecar.exists():
        # Reuse the most recent matching UUID sidecar from older versions.
        for candidate in sorted(output.parent.glob("*.experiment.metadata.json"),
                                key=lambda path: path.stat().st_mtime_ns, reverse=True):
            try:
                old = service.load_metadata(candidate)
                if old.get("experiment_type") != experiment_type:
                    continue
                if _raw_output_paths(old) == {output.name}:
                    sidecar = candidate
                    break
            except (OSError, ValueError, TypeError):
                continue
    if sidecar.exists():
        old = service.load_metadata(sidecar)
        if old.get("experiment_type") != experiment_type:
            raise ValueError("Output metadata belongs to another experiment type")
        raw_paths = _raw_output_paths(old)
        if raw_paths and raw_paths != {output.name}:
            raise ValueError("Output metadata belongs to another output file")
        if old.get("export_signature") == signature and old.get("status") == "completed":
            return sidecar
    run = service.begin(experiment_type, device_id, metadata_path=sidecar,
                        settings=requested, instruments=inventory, sample_id=sample_id,
                        allow_post_completion=True)
    with run._lock:
        run.metadata["metadata_schema"] = METADATA_SCHEMA_VERSION
        run.metadata["device_id"] = device_id
        run.metadata["device"] = {"device_id": device_id, **({"sample_id": sample_id} if sample_id else {})}
        run.metadata["settings"]["requested"] = requested
        run.metadata["settings"]["loadable"] = service.loadable_settings(requested)
        run.metadata["instruments"] = inventory
        run.metadata["context"] = context
        run.metadata["export_signature"] = signature
        run._register_file(output, role="raw", kind=kind, details={"sha256": digest, **summary})
        run._register_event_file(persist=False)
        recorded = run.record_export({"path": output.name, "kind": kind, "sha256": digest},
                                     output={"file": output.name})
        if recorded is None:
            raise RuntimeError("Export metadata could not be durably recorded")
        if run._terminal:
            if run.metadata.get("metadata_status") == "degraded":
                raise RuntimeError("Export metadata is degraded")
            run.metadata["status"] = "completed"
            run.metadata.pop("error", None)
            run.metadata.pop("cancellation_reason", None)
            run.metadata["summary"] = summary
            run.metadata["completed_utc"] = utc_now()
            run.metadata["completed_at"] = run.metadata["completed_utc"]
            run.metadata["result"] = {"status": "completed", "summary": summary,
                                      "error": None, "cancellation": None}
            run._write()
        else:
            result = run.complete(summary)
            if result["status"] != "completed":
                raise RuntimeError("Export metadata finalization failed")
    return run.path

def begin_output_metadata(
    output_file: str | Path,
    *,
    experiment_type: str,
    device_id: str,
    settings: Optional[Mapping[str, Any]] = None,
    instruments: Optional[Iterable[Mapping[str, Any]]] = None,
) -> ExperimentRun:
    output = Path(output_file)
    service = ExperimentMetadataService(output.parent)
    return service.begin(
        experiment_type,
        device_id,
        output_dir=output.parent,
        settings=settings,
        instruments=instruments,
    )


def finalize_output_metadata(
    output_file: str | Path,
    *,
    experiment_type: str,
    device_id: str,
    settings: Optional[Mapping[str, Any]] = None,
    status: str = "completed",
    summary: Optional[Mapping[str, Any]] = None,
    error: Any = None,
    cancellation_reason: Optional[str] = None,
    extra_files: Optional[Iterable[tuple[str | Path, str]]] = None,
    role: str = "raw",
) -> Optional[Path]:
    """Write one sidecar for a legacy output and return its path.

    Save failures are intentionally not swallowed: a successful acquisition
    must never claim metadata success when the portable artifact could not be
    written.  The helper returns ``None`` only for an absent output path.
    """
    output = Path(output_file)
    if not output.exists():
        return None
    # A late export may arrive after the acquisition sidecar is terminal. Reuse
    # the existing run when its file list already identifies this output so a
    # second format cannot create a competing experiment record.
    service = ExperimentMetadataService(output.parent)
    run = None
    try:
        for candidate in output.parent.glob("*.experiment.metadata.json"):
            try:
                metadata = service.load_metadata(candidate)
                paths = {str(item.get("path", "")) for item in metadata.get("files", [])}
                if output.name in paths or output.resolve().relative_to(output.parent.resolve()).as_posix() in paths:
                    run = service.open_run(candidate, allow_post_completion=True)
                    break
            except (OSError, ValueError, json.JSONDecodeError):
                continue
    except OSError:
        pass
    if run is None:
        run = begin_output_metadata(
            output,
            experiment_type=experiment_type,
            device_id=device_id,
            settings=settings,
        )
    run.register_file(output, role=role, kind=output.suffix.lstrip(".") or "data")
    discovered: list[tuple[Path, str]] = []
    for sibling, sibling_role in (
        (output.with_suffix(".meta.txt"), "intermediate"),
        (output.with_suffix(".log"), "intermediate"),
        (output.with_name(f"{output.stem}_summary.json"), "processed"),
    ):
        if sibling.exists():
            discovered.append((sibling, sibling_role))
    discovered.extend((Path(path), file_role) for path, file_role in (extra_files or ()))
    for path, role in discovered:
        if Path(path).exists():
            run.register_file(path, role=role)
    if run.metadata.get("status") in ("completed", "cancelled", "failed"):
        run.record_export({"path": output.name, "role": role, "late": True},
                          output={"file": output.name})
        return run.path
    if status == "completed":
        run.complete(summary)
    elif status == "cancelled":
        run.cancel(cancellation_reason)
    elif status == "failed":
        run.fail(error if error is not None else "experiment failed")
    else:
        raise ValueError(f"Unsupported metadata status: {status}")
    return run.path


__all__ = ["begin_output_metadata", "finalize_output_metadata", "persist_output_snapshot"]
