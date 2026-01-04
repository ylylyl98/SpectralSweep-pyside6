from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Callable

@dataclass
class RunContext:
    run_id: str
    devices: Dict[str, Any]
    csv_writer: Any
    progress_cb: Optional[Callable[[str], None]] = None
    state: Dict[str, Any] = field(default_factory=dict)
    axes: Dict[str, Any] = field(default_factory=dict)
    extra_scalar_fields_order: List[str] = field(default_factory=list)

    def log(self, msg: str):
        if self.progress_cb:
            self.progress_cb(msg)

    def emit_row(self, scalars: Dict[str, float], spectrum: Optional[list] = None):
        """
        Write only Vbg, Vtg, and any fields listed in extra_scalar_fields_order (e.g., 'Vbias').
        No leakage columns are written.
        """
        # locked order: Vbg, Vtg
        out = {
            "Vbg": float(scalars.get("Vbg", 0.0)),
            "Vtg": float(scalars.get("Vtg", 0.0)),
        }
        # append extras in the exact order requested by the UI
        for k in self.extra_scalar_fields_order:
            if k in scalars:
                out[k] = scalars[k]
        # delegate to CSVWriter (new API: write_row(scalars, spectrum))
        self.csv_writer.write_row(out, spectrum)
