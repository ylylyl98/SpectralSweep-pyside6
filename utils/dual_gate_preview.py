from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Optional

import numpy as np


def _numeric_wavelength_columns(header: list[str]) -> tuple[list[int], np.ndarray]:
    indices: list[int] = []
    values: list[float] = []
    for index, name in enumerate(header):
        try:
            value = float(name)
        except (TypeError, ValueError):
            continue
        indices.append(index)
        values.append(value)
    if not indices:
        raise ValueError("CSV contains no numeric wavelength columns.")
    return indices, np.asarray(values, dtype=float)


def load_last_dual_gate_acquisition(path: str | Path) -> dict[str, Any]:
    """Read only the last acquisition group from a Dual Gate CSV."""
    csv_path = Path(path)
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ValueError("CSV is empty.") from exc
        wl_indices, wavelengths = _numeric_wavelength_columns(header)
        point_col = header.index("point_index") if "point_index" in header else None
        y_col = header.index("y_pixel") if "y_pixel" in header else None

        if point_col is None or y_col is None:
            last_row: Optional[list[str]] = None
            for row in reader:
                if row:
                    last_row = row
            if last_row is None:
                raise ValueError("CSV contains no completed acquisitions.")
            values = np.asarray([float(last_row[i]) for i in wl_indices], dtype=float)
            return {"mode": "spectrum", "wavelengths": wavelengths, "data": values}

        last_point: Optional[int] = None
        group: list[tuple[float, np.ndarray]] = []
        for row in reader:
            if not row:
                continue
            point = int(float(row[point_col]))
            values = np.asarray([float(row[i]) for i in wl_indices], dtype=float)
            y_value = float(row[y_col])
            if last_point is None or point != last_point:
                last_point = point
                group = []
            group.append((y_value, values))
        if last_point is None or not group:
            raise ValueError("CSV contains no completed full-sensor acquisitions.")
        group.sort(key=lambda item: item[0])
        return {
            "mode": "full_sensor",
            "point_index": last_point,
            "wavelengths": wavelengths,
            "y_pixels": np.asarray([item[0] for item in group], dtype=float),
            "data": np.vstack([item[1] for item in group]),
        }
