from __future__ import annotations
from typing import Tuple
import numpy as np
import lf6_automation

class SpectrometerLF6:
    """
    Thin adapter over lf6_automation.LF6Setup.

    Exposes:
      - calibration_wavelengths(force: bool = False) -> np.ndarray
      - acquire() -> (wavelengths: np.ndarray, intensities: np.ndarray)
      - change_spectra_center(center_nm)  # convenience; invalidates λ cache
    """
    def __init__(self, setup: lf6_automation.LF6Setup):
        self.setup = setup
        self._wls_cache: np.ndarray | None = None

    # ---- cache control ----
    def invalidate_wavelengths(self) -> None:
        self._wls_cache = None

    # ---- wavelength API ----
    def calibration_wavelengths(self, force: bool = False) -> np.ndarray:
        """Return wavelength vector; refresh if force=True or cache empty."""
        if force or self._wls_cache is None:
            try:
                # primary getter
                self._wls_cache = np.asarray(
                    self.setup.get_wavelength_calibration(), dtype=float
                ).ravel()
            except AttributeError:
                # fallback to older method names
                try:
                    self._wls_cache = np.asarray(
                        self.setup.calibrate_wavelength(), dtype=float
                    ).ravel()
                except Exception:
                    self._wls_cache = np.array([], dtype=float)
        return self._wls_cache

    # ---- data API ----
    def acquire(self) -> Tuple[np.ndarray, np.ndarray]:
        """
        Return (wavelengths, intensities).
        We force λ refresh to reflect any recent center change.
        """
        y = np.asarray(self.setup.acquire(), dtype=float).ravel()
        wl = self.calibration_wavelengths(force=True)
        return wl, y

    # ---- convenience setter that also clears λ cache ----
    def change_spectra_center(self, center_nm) -> None:
        """Accepts '730' or 730.0; forwards to LF6 and invalidates λ cache."""
        try:
            self.setup.change_spectra_center(f"{float(center_nm):.0f}")
        except Exception:
            self.setup.change_spectra_center(center_nm)
        self.invalidate_wavelengths()

    # ---- frames/accums convenience ----
    def set_accumulations(self, n: int) -> None:
        """
        Map UI 'Frames/Accums' to LightField Online Processing:
        OnlineProcesses -> Exposures per Frame.
        """
        if hasattr(self.setup, "change_frame_to_combine"):
            self.setup.change_frame_to_combine(int(n))
            return
        raise AttributeError("LF6Setup has no change_frame_to_combine()")

    def set_frames(self, n: int) -> None:
        """Alias for set_accumulations (common naming)."""
        self.set_accumulations(n)

    # OPTIONAL but handy: forward any other LF6Setup methods automatically
    def __getattr__(self, name):
        return getattr(self.setup, name)

