"""Portable reference-list preferences and recovery of pre-list autosaves."""
from pathlib import Path
import re

from app.experiment_metadata import ExperimentMetadataService, _jsonable


REFERENCE_FIELDS = (
    "name", "color", "visible", "source", "path", "metadata_path", "axis_unit",
    "setup_key", "sample_id", "save_root", "saved", "save_error",
)


def capture_reference(ref):
    record = {key: ref.get(key) for key in REFERENCE_FIELDS}
    # Capture a name still being edited when the application closes.
    record["name"] = ref["_row_name"].text().strip() or ref["name"]
    if not ref["saved"]:
        # A failed CSV/sidecar save must remain retryable after sample switching
        # or restart. Successful saves keep their arrays in the CSV only.
        record["recovery"] = _jsonable({key: ref.get(key) for key in (
            "wavelength", "counts", "settings_snapshot", "metadata", "observed_readback",
        )})
    return record


def sample_spectrum_folder(root, sample_id):
    sample = re.sub(r'[<>:"/\\|?*]+', "_", str(sample_id).strip()).strip(" .")
    return (Path(root) / sample if sample else Path(root)) / "Spectrum"


def discover_sample_references(root, sample_id):
    """Recover only Add spectrum exports with matching recorded sample identity.

    Folder names alone are insufficient: Windows sanitization can map two IDs
    to the same directory. Manual exports and other experiment types are excluded.
    """
    records, errors, seen = [], [], set()
    folder = sample_spectrum_folder(root, sample_id)
    for sidecar in sorted(folder.glob("*/*.experiment.metadata.json")):
        try:
            metadata = ExperimentMetadataService.load_metadata(sidecar)
            settings = metadata.get("settings", {}).get("requested", {})
            recorded_sample = str(metadata.get("device", {}).get("sample_id") or "").strip()
            if (recorded_sample != sample_id or metadata.get("experiment_type") != "spectrum_preview"
                    or settings.get("source") != "acquired" or not settings.get("reference_name")):
                continue
            for output in metadata.get("files", []):
                if output.get("kind") != "spectrum_1d":
                    continue
                path = sidecar.parent / Path(output["path"]).name
                if str(path) in seen:
                    continue
                seen.add(str(path))
                records.append({"path": str(path), "metadata_path": str(sidecar),
                                "name": settings["reference_name"], "source": "acquired",
                                "saved": True, "sample_id": sample_id})
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            errors.append(f"{sidecar.name}: {exc}")
    return records, errors
