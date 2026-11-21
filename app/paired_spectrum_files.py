"""Append two independently calibrated spectra and journal their shared scan point."""
import csv
import json
import os
import uuid
from pathlib import Path

import numpy as np
from app.experiment_metadata import _jsonable


_NAMES = {"lightfield": "PIXIS", "andor_ingaas": "Andor_InGaAs", "andor_si": "Andor_Si"}
_COLUMNS = ("run_id", "point_id", "backend", "started_at", "finished_at",
            "center_nm", "exposure_ms", "frames")


class PairedSpectrumSaveError(RuntimeError):
    def __init__(self, failures, paths):
        super().__init__("Could not save paired point: " + ", ".join(failures))
        self.paths = dict(paths)


class PairedSpectrumFiles:
    def __init__(self, directory, stem, andor_backend, *, andor_stem=None, pixis_stem=None):
        if andor_backend not in ("andor_ingaas", "andor_si"):
            raise ValueError("Choose an Andor detector for the pair")
        if not stem or Path(stem).name != stem:
            raise ValueError("Provide a filename stem without a directory")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.backends = ("lightfield", andor_backend)
        self.run_id = str(uuid.uuid4())
        self._segments = {}
        self._points = set()
        index = 0
        while True:
            self.stem = stem if index == 0 else f"{stem}_{index:03d}"
            self.manifest_path = self.directory / f"{self.stem}_pairs.jsonl"
            try:
                self.manifest_path.touch(exist_ok=False)
                break
            except FileExistsError:
                index += 1
        # Each detector's file may carry its own recipe in the name; both keep
        # the collision suffix of the shared manifest.
        suffix = "" if index == 0 else f"_{index:03d}"
        self._stems = {"lightfield": (pixis_stem or stem) + suffix,
                       andor_backend: (andor_stem or stem) + suffix}

    def _journal(self, entry):
        with self.manifest_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _write_spectrum(self, backend, point_id, record, scalars):
        axis = np.asarray(record["wavelengths"], dtype=float)
        counts = np.asarray(record["counts"], dtype=float)
        if (axis.ndim != 1 or not axis.size or counts.shape != axis.shape
                or not np.isfinite(axis).all() or not np.isfinite(counts).all()):
            raise ValueError("Expected equal, finite 1D axis and counts")
        settings = record.get("settings") or {}
        # Plain numeric wavelength columns, as in single-detector files; the
        # unit is recorded in the pair journal (axis_unit), not in each name.
        header = [*_COLUMNS, *scalars, *(f"{value:.4f}" for value in axis)]
        segment = self._segments.get(backend)
        if segment is None or segment[1] != header:
            number = segment[0] + 1 if segment else 1
            while True:
                suffix = "" if number == 1 else f"_{number:03d}"
                path = self.directory / f"{self._stems[backend]}_{_NAMES[backend]}{suffix}.csv"
                try:
                    with path.open("x", newline="", encoding="utf-8") as stream:
                        csv.writer(stream).writerow(header)
                    break
                except FileExistsError:
                    number += 1
            self._segments[backend] = number, header, path
        else:
            path = segment[2]
        with path.open("a", newline="", encoding="utf-8") as stream:
            csv.writer(stream).writerow([self.run_id, point_id, backend,
                record.get("started_at"), record.get("finished_at"),
                settings.get("center_nm"), settings.get("exposure_ms"), settings.get("frames"),
                *scalars.values(), *counts])
            stream.flush()
            os.fsync(stream.fileno())
        return path

    def write_point(self, point_id, records, *, scalars=None, incomplete_reason=""):
        point_id = str(point_id)
        if not point_id or point_id in self._points:
            raise ValueError("Every paired point needs a unique point_id")
        scalars = dict(scalars or {})
        if set(scalars).intersection(_COLUMNS):
            raise ValueError("Point metadata duplicates a reserved CSV column")
        entry = dict(run_id=self.run_id, point_id=point_id, station_id="attodry2100")
        self._journal(dict(entry, status="writing"))
        self._points.add(point_id)
        paths, outcomes, failures = {}, {}, []
        for backend in self.backends:
            record = records.get(backend) or {"error": "No capture result received"}
            outcome = {key: _jsonable(record[key]) for key in (
                "started_at", "finished_at", "settings", "error", "axis_unit",
                "readback", "recovery_required") if key in record}
            if not record.get("error"):
                try:
                    path = self._write_spectrum(backend, point_id, record, scalars)
                    paths[backend] = path
                    outcome["file"] = path.name
                except (OSError, KeyError, ValueError, TypeError) as exc:
                    outcome["error"] = str(exc)
                    failures.append(f"{backend}: {exc}")
            outcomes[backend] = outcome
        if incomplete_reason:
            entry["reason"] = str(incomplete_reason)
        self._journal(dict(entry, status="complete" if len(paths) == 2 and not incomplete_reason else "incomplete",
                           detectors=outcomes))
        if failures:
            raise PairedSpectrumSaveError(failures, paths)
        return paths
