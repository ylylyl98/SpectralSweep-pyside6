
from __future__ import annotations
from typing import List, Tuple
import lf6_automation

class SpectrometerLF6:
    """
    Thin adapter over your lf6_automation.LF6Setup

    Exposes:
      - calibration_wavelengths() -> List[float]
      - acquire() -> (wavelengths, intensities)
    """
    def __init__(self, setup: lf6_automation.LF6Setup):
        self.setup = setup
        # Prefer explicit calibration list if available
        # Fallback to older method names.
        if hasattr(self.setup, "get_wavelength_calibration"):
            self._wavelengths = list(self.setup.get_wavelength_calibration())
        elif hasattr(self.setup, "calibrate_wavelength"):
            self._wavelengths = list(self.setup.calibrate_wavelength())
        else:
            # Last resort: call 'trigger' once to obtain a frame with metadata (not ideal).
            self._wavelengths = []

    def calibration_wavelengths(self) -> List[float]:
        if not self._wavelengths and hasattr(self.setup, "get_wavelength_calibration"):
            self._wavelengths = list(self.setup.get_wavelength_calibration())
        return self._wavelengths

    def acquire(self) -> Tuple[list, list]:
        # Newer API
        if hasattr(self.setup, "acquire"):
            vals = self.setup.acquire()
            wl = self.calibration_wavelengths()
            return wl, list(vals)
        # Legacy API: trigger returns spectrum in a property
        if hasattr(self.setup, "trigger"):
            # Expect trigger to return (wavelengths, intensities) or just intensities
            out = self.setup.trigger()
            if isinstance(out, tuple) and len(out) == 2:
                wl, vals = out
                self._wavelengths = list(wl)
                return list(wl), list(vals)
            else:
                wl = self.calibration_wavelengths()
                return wl, list(out)
        raise RuntimeError("LF6Setup is missing acquire/trigger methods")
