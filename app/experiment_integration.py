"""Small adapter used by existing save paths.

Keeping this bridge separate means legacy CSV writers stay unchanged while all
new output receives the same schema-v1 sidecar and history entry.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .experiment_metadata import ExperimentMetadataService, ExperimentRun


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
    if status == "completed":
        run.complete(summary)
    elif status == "cancelled":
        run.cancel(cancellation_reason)
    elif status == "failed":
        run.fail(error if error is not None else "experiment failed")
    else:
        raise ValueError(f"Unsupported metadata status: {status}")
    return run.path


__all__ = ["begin_output_metadata", "finalize_output_metadata"]
