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

        self._ensure_open()

        row_scalars = [self._fmt_cell(scalars.get(k, "")) for k in self.scalar_fields]

        if spectrum is None:
            spectrum = [""] * len(self.wavelength_headers or [])
        row_spec = [self._fmt_cell(x) for x in list(spectrum)]

        self.writer.writerow(row_scalars + row_spec)
        self._data_rows_written += 1

    def add_row(self, scalars: Dict[str, Any], spectrum: Optional[List[float]] = None) -> None:
        self.write_row(scalars, spectrum)

    def close(self) -> None:
        if self.fp:
            self.fp.flush()
            self.fp.close()
            self.fp = None
            self.writer = None
