# app/engine/csv_writer.py
import csv
from pathlib import Path
from typing import List, Dict, Optional, Any

class CSVWriter:
    def __init__(self, out_dir: str, file_base: str,
                 wavelength_headers: List[float],
                 extra_scalar_fields_order: Optional[List[str]] = None,
                 sample_dir: Optional[str] = None,
                 sub_dir: Optional[str] = None):
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

    def _ensure_open(self) -> None:
        if self.writer is not None:
            return
        self.fp = self.path.open("w", newline="", encoding="utf-8")
        self.writer = csv.writer(self.fp)

        wl_cols = [
            f"{w:.4f}" if isinstance(w, (int, float)) else str(w)
            for w in list(self.wavelength_headers or [])
        ]
        self.writer.writerow(self.scalar_fields + wl_cols)

    def set_wavelength_headers(self, wavelengths: List[float]) -> None:
        self.wavelength_headers = wavelengths or []

        # if file already opened but no data yet, rewrite header safely
        if self.writer is not None and self._data_rows_written == 0:
            self.fp.seek(0)
            self.fp.truncate(0)
            wl_cols = [
                f"{w:.4f}" if isinstance(w, (int, float)) else str(w)
                for w in list(self.wavelength_headers or [])
            ]
            self.writer.writerow(self.scalar_fields + wl_cols)

    def write_row(self, scalars: Dict[str, Any], spectrum: Optional[List[float]] = None) -> None:
        self._ensure_open()
        row_scalars = [scalars.get(k, "") for k in self.scalar_fields]
        if spectrum is None:
            spectrum = [""] * len(self.wavelength_headers or [])
        self.writer.writerow(row_scalars + list(spectrum))
        self._data_rows_written += 1

    def add_row(self, scalars: Dict[str, Any], spectrum: Optional[List[float]] = None) -> None:
        self.write_row(scalars, spectrum)

    def close(self) -> None:
        if self.fp:
            self.fp.flush()
            self.fp.close()
            self.fp = None
            self.writer = None
