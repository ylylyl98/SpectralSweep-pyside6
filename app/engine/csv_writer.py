# app/engine/csv_writer.py
import csv
import math
from pathlib import Path
from typing import List, Dict, Optional, Any

try:
    import numpy as np
    _NUM_TYPES = (int, float, np.integer, np.floating)
except Exception:
    np = None
    _NUM_TYPES = (int, float)


class CSVWriter:
    def __init__(
        self,
        out_dir: str,
        file_base: str,
        wavelength_headers: List[float],
        extra_scalar_fields_order: Optional[List[str]] = None,
        scalar_fields_order: Optional[List[str]] = None,
        sample_dir: Optional[str] = None,
        sub_dir: Optional[str] = None,
    ):
        self.root_dir = Path(out_dir)
        parts = [p for p in (sample_dir, sub_dir) if p]
        self.out_dir = self.root_dir.joinpath(*parts) if parts else self.root_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self.file_base = file_base

        # keep reference; snapshot later when writing header
        self.wavelength_headers = wavelength_headers or []

        if scalar_fields_order is not None:
            self.scalar_fields = list(scalar_fields_order)
        else:
            base_scalars = ["Vbg", "Vtg"]
            extra = list(extra_scalar_fields_order or [])
            self.scalar_fields = base_scalars + extra

        self.fp = None
        self.writer = None
        self._data_rows_written = 0

    @property
    def path(self) -> Path:
        return self.out_dir / f"{self.file_base}.csv"

    def _fmt_cell(self, v: Any) -> str:
        """Write float-like values with ~float64 precision; keep strings as-is."""
        if v is None:
            return ""
        if isinstance(v, str):
            return v

        # numpy scalar -> python scalar
        if np is not None and hasattr(v, "item"):
            try:
                v = v.item()
            except Exception:
                pass

        if isinstance(v, _NUM_TYPES):
            try:
                x = float(v)
                if not math.isfinite(x):
                    return ""
                return format(x, ".15g")  # ~float64 round-trip precision
            except Exception:
                return ""

        return str(v)

    def _write_header(self) -> None:
        wl_cols = [
            (f"{float(w):.4f}" if isinstance(w, _NUM_TYPES) else str(w))
            for w in list(self.wavelength_headers or [])
        ]
        self.writer.writerow(self.scalar_fields + wl_cols)

    def _maybe_extend_scalar_fields(self, scalars: Dict[str, Any]) -> None:
        """
        If new scalar keys appear BEFORE the first data row is written, add them to header.
        """
        if not isinstance(scalars, dict):
            return

        new_keys = [k for k in scalars.keys() if k not in self.scalar_fields]
        if not new_keys:
            return

        # only safe to change header before any data rows
        if self._data_rows_written != 0:
            return

        self.scalar_fields.extend(new_keys)

        # if file already opened, rewrite header
        if self.writer is not None:
            self.fp.seek(0)
            self.fp.truncate(0)
            self._write_header()

    def _ensure_open(self) -> None:
        if self.writer is not None:
            return
        self.fp = self.path.open("w", newline="", encoding="utf-8")
        self.writer = csv.writer(self.fp)
        self._write_header()

    def set_wavelength_headers(self, wavelengths: List[float]) -> None:
        self.wavelength_headers = wavelengths or []

        # if file already opened but no data yet, rewrite header safely
        if self.writer is not None and self._data_rows_written == 0:
            self.fp.seek(0)
            self.fp.truncate(0)
            self._write_header()

    def write_row(self, scalars: Dict[str, Any], spectrum: Optional[List[float]] = None) -> None:
        # allow new scalar keys to become columns (only before first row)
        self._maybe_extend_scalar_fields(scalars)

        if spectrum is None:
            spectrum_values = [""] * len(self.wavelength_headers or [])
        else:
            spectrum_values = list(spectrum)

        expected_spectrum_len = len(self.wavelength_headers or [])
        if len(spectrum_values) != expected_spectrum_len:
            raise ValueError(
                "CSV spectrum length does not match its wavelength header: "
                f"expected {expected_spectrum_len}, received {len(spectrum_values)}."
            )

        self._ensure_open()

        row_scalars = [self._fmt_cell(scalars.get(k, "")) for k in self.scalar_fields]
        row_spec = [self._fmt_cell(x) for x in spectrum_values]

        self.writer.writerow(row_scalars + row_spec)
        self._data_rows_written += 1
        self.fp.flush()

    def add_row(self, scalars: Dict[str, Any], spectrum: Optional[List[float]] = None) -> None:
        self.write_row(scalars, spectrum)

    def write_matrix(
        self,
        scalars: Dict[str, Any],
        image: Any,
        point_index: int,
        y_pixels: Optional[List[Any]] = None,
    ) -> None:
        """Write one full-sensor acquisition as a contiguous group of rows."""
        if np is None:
            raise RuntimeError("Matrix CSV export requires NumPy.")
        frame = np.asarray(image, dtype=float)
        if frame.ndim != 2 or 0 in frame.shape:
            raise ValueError(f"CSV matrix export expects a non-empty 2D array; received {frame.shape}.")
        expected_width = len(self.wavelength_headers or [])
        if frame.shape[1] != expected_width:
            raise ValueError(
                "CSV matrix width does not match its wavelength header: "
                f"expected {expected_width}, received image shape {frame.shape}."
            )
        y_values = list(range(frame.shape[0])) if y_pixels is None else list(y_pixels)
        if len(y_values) != frame.shape[0]:
            raise ValueError(
                f"CSV matrix has {frame.shape[0]} rows but received {len(y_values)} Y-pixel labels."
            )

        matrix_fields = ["point_index", "y_pixel"]
        if self._data_rows_written and any(k not in self.scalar_fields for k in matrix_fields):
            raise ValueError("Cannot switch an existing 1D CSV to full-sensor matrix layout.")
        if self._data_rows_written == 0:
            self.scalar_fields = matrix_fields + [k for k in self.scalar_fields if k not in matrix_fields]
        self._maybe_extend_scalar_fields(scalars)
        self._ensure_open()

        rows = []
        for y_value, spectrum in zip(y_values, frame):
            row_values = dict(scalars)
            row_values["point_index"] = point_index
            row_values["y_pixel"] = y_value
            row_scalars = [self._fmt_cell(row_values.get(k, "")) for k in self.scalar_fields]
            rows.append(row_scalars + [self._fmt_cell(x) for x in spectrum])
        self.writer.writerows(rows)
        self._data_rows_written += len(rows)
        self.fp.flush()

    def close(self) -> None:
        if self.fp:
            self.fp.flush()
            self.fp.close()
            self.fp = None
            self.writer = None
