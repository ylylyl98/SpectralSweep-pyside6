"""Finalize MCD output associations in one worker-owned manifest transaction."""
from pathlib import Path
from typing import Any, Mapping


def condition_metadata_summary(metadata: Mapping[str, Any], compatibility_key: str) -> dict[str, Any]:
    """Link the durable condition state instead of embedding its whole history again."""
    fields = ("status", "condition_index", "condition_count", "condition", "spectra_written",
              "cycles", "completed_legs", "error", "cleanup_error", "completed_utc", "ended_utc")
    return {"compatibility_key": str(compatibility_key),
            **{field: metadata[field] for field in fields if field in metadata}}


def _measurement_status(result: Mapping[str, Any]) -> str:
    return str(result.get("measurement_status") or result.get("status") or (
        "FAILED" if result.get("error") else "CANCELLED" if result.get("stopped") else "COMPLETED"
    )).upper()


def finalize_mcd_metadata(run: Any, result: dict[str, Any]) -> None:
    """Keep metadata failure distinct from the already completed hardware work."""
    if run is None:
        return
    result.setdefault("measurement_status", _measurement_status(result))
    try:
        _finalize_mcd_metadata(run, result)
    except Exception as exc:
        result["status"] = "FAILED"
        result["metadata_status"] = "failed"
        result["metadata_finalized"] = False
        result.setdefault("metadata_error", str(exc))
        # A history failure can happen after the completed sidecar was written,
        # but before _terminal is committed. Persist a failure marker and failed
        # terminal state if storage still permits it. Neither repair failure
        # replaces the original exception nor changes the hardware outcome.
        try:
            run.mark_metadata_failure(exc, event="finalization")
        except Exception as repair_error:
            result.setdefault("metadata_failure_persistence_error", str(repair_error))
        if not getattr(run, "_terminal", False):
            try:
                run.fail(result.get("error") or exc)
            except Exception as repair_error:
                result.setdefault("metadata_failure_persistence_error", str(repair_error))
        raise
    result["metadata_status"] = "saved"


def _finalize_mcd_metadata(run: Any, result: dict[str, Any]) -> None:
    """Run after hardware cleanup and before the worker publishes completion.

    Legacy projections and raw CSVs keep their existing formats. Passing all
    associations to the terminal operation avoids a full manifest/history
    rewrite for each output. This function performs no hardware operations.
    """
    if run is None:
        return
    entries = []
    seen = set()
    details = {str(item.get("path")): item for item in result.get("file_details", [])
               if isinstance(item, Mapping)}

    def add(path, role, **kwargs):
        path = Path(path)
        key = str(path.resolve())
        if key not in seen and path.is_file():
            entries.append({"path": path, "role": role, **kwargs})
            seen.add(key)

    projection_details = {"compatibility_projection": True,
                          "derived_from_experiment_id": run.experiment_id}
    csv_paths = result.get("csv_paths") or ([result["csv_path"]] if result.get("csv_path") else [])
    for csv_name in csv_paths:
        path = Path(csv_name)
        detail = details.get(str(path), {})
        add(path, detail.get("role", "raw"),
            kind=detail.get("kind", "continuous_mcd_spectrum"), details=detail)
        add(path.with_suffix(".meta.json"), "intermediate", details=projection_details)
    if result.get("metadata_path"):
        add(result["metadata_path"], "intermediate", details=projection_details)

    status = _measurement_status(result)
    error = result.get("error") or result.get("cleanup_error")
    if status == "COMPLETED":
        saved = run.complete({"spectra_written": int(result.get("spectra_written", 0))}, files=entries)
    elif status == "CANCELLED":
        saved = run.cancel(error or "MCD stop requested", files=entries)
    else:
        saved = run.fail(error or "MCD failed", files=entries)
    if isinstance(saved, Mapping) and str(saved.get("status", "")).upper() != status:
        raise RuntimeError("MCD metadata was marked failed because records could not be persisted")
    result["metadata_finalized"] = True
