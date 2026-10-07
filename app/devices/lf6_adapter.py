from __future__ import annotations
from typing import Tuple
import numpy as np
import lf6_automation

from .spectrum_alignment import align_wavelengths_to_image, align_wavelengths_to_intensities

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
        Refresh the SDK axis for every captured frame. LightField can also
        change calibration outside this adapter's setting methods.
        """
        # acquire_2d preserves full-sensor geometry while still returning a
        # flat buffer when the LightField frame does not expose dimensions.
        if hasattr(self.setup, "acquire_2d"):
            y = np.asarray(self.setup.acquire_2d(), dtype=float)
        else:
            y = np.asarray(self.setup.acquire(), dtype=float)
        wl = self.calibration_wavelengths(force=True)
        if y.ndim == 2 and y.shape[0] > 1 and y.shape[1] > 1:
            return align_wavelengths_to_image(wl, y)
        y = y.ravel()
        return align_wavelengths_to_intensities(wl, y)

    def abort_acquisition(self) -> bool:
        """Forward a best-effort cancellation request to LightField."""
        method = getattr(self.setup, "abort_acquisition", None)
        return bool(method()) if callable(method) else False

    def acquire_2d(self):
        """Use the setup's validated capture and metadata path for 2D too."""
        return self.setup.acquire_2d()

    # ---- convenience setter that also clears λ cache ----
    def change_spectra_center(self, center_nm) -> None:
        """Accepts '730' or 730.0; forwards to LF6 and invalidates λ cache."""
        self.set_center_wavelength_when_ready(center_nm)

    change_center_wavelength = change_spectra_center

    def set_center_wavelength_when_ready(self, center_nm, **kwargs) -> None:
        """Guarded shared center setter used by all production callers."""
        method = getattr(self.setup, "set_center_wavelength_when_ready", None)
        if not callable(method):
            raise AttributeError("LF6Setup has no guarded center-wavelength setter")
        self.invalidate_wavelengths()
        method(float(center_nm), **kwargs)

    def configure_for_acquisition(self, *, center_nm, exposure_ms, frames):
        method = getattr(self.setup, "configure_for_acquisition", None)
        if not callable(method):
            raise AttributeError("LF6Setup has no acquisition preparation surface")
        self.invalidate_wavelengths()
        return method(center_nm=center_nm, exposure_ms=exposure_ms, frames=frames)

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

