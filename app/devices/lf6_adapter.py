from __future__ import annotations
from typing import Tuple
import numpy as np
import lf6_automation
from app.lightfield_capture_guard import LightFieldAcquisitionStalled, capture_operation, get_capture_guard

from .spectrum_alignment import align_wavelengths_to_image, align_wavelengths_to_intensities
from .lightfield_sensor import verify_sensor_readout


def _align_custom_spectrum(wavelengths, frame, snapshot):
    """Map only verified one-row ROI data; never trim away a custom ROI's X offset.

    The installed Automation manual pp.64/68 defines calibration and ROI sensor
    coordinates. SPE 3.0 pp.25-26 defines sensor-column wavelength mapping and
    zero-based sensor regions. A full-sensor axis can therefore be sampled at
    each output bin's sensor-column center when both orientations are Normal.
    """
    region = snapshot["regions"][0]
    width, xbin = region["width"], region["x_binning"]
    output_width = width // xbin
    data = np.asarray(frame, dtype=float)
    if data.shape not in {(output_width,), (1, output_width)}:
        raise RuntimeError(
            f"Custom LightField ROI returned shape {data.shape}; expected one row with "
            f"{output_width} columns. Acquisition blocked; no silent flattening.")
    axis = np.asarray(wavelengths, dtype=float).ravel()
    if not axis.size or not np.all(np.isfinite(axis)):
        raise RuntimeError("Custom LightField wavelength calibration is unavailable/non-finite; acquisition blocked.")
    if axis.size == output_width:
        # Versions which supply ROI-adjusted calibration already label each
        # output sample. Applying X/binning again would corrupt those labels.
        return axis.copy(), data.reshape(output_width).copy()
    full_regions = snapshot.get("full_regions", [])
    sensor_width = snapshot.get("sensor_width")
    if (len(full_regions) != 1 or axis.size != sensor_width
            or full_regions[0].get("width") != sensor_width
            or full_regions[0].get("x_binning") != 1):
        raise RuntimeError(
            "Custom LightField wavelength axis has an unverified size/reference; "
            "check calibration and sensor regions. Acquisition blocked.")
    if (snapshot.get("acquisition_orientation") != "Normal"
            or snapshot.get("calibration_orientation") != "Normal"):
        raise RuntimeError(
            "Custom LightField wavelength coordinates cannot be verified for this orientation. "
            "Use matching Normal acquisition/calibration orientation or an ROI-sized SDK axis; acquisition blocked.")
    start = region["x"] - full_regions[0]["x"]
    if start < 0 or start + width > axis.size:
        raise RuntimeError("Custom LightField ROI lies outside the verified wavelength reference; acquisition blocked.")
    # A bin spans xbin sensor columns; its center is halfway between its first
    # and last column. Interpolate that coordinate rather than averaging a
    # potentially nonlinear wavelength calibration over the whole bin.
    centers = start + np.arange(output_width, dtype=float) * xbin + (xbin - 1) / 2.
    mapped = np.interp(centers, np.arange(axis.size, dtype=float), axis)
    return mapped, data.reshape(output_width).copy()

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
            guard = get_capture_guard(self.setup)
            guard.check_available()
            try:
                # primary getter
                self._wls_cache = np.asarray(
                    self.setup.get_wavelength_calibration(), dtype=float
                ).ravel()
            except AttributeError:
                # fallback to older method names
                guard.check_available()
                try:
                    self._wls_cache = np.asarray(
                        self.setup.calibrate_wavelength(), dtype=float
                    ).ravel()
                except LightFieldAcquisitionStalled:
                    raise
                except Exception:
                    guard.check_available()
                    self._wls_cache = np.array([], dtype=float)
            guard.check_available()
        return self._wls_cache

    # ---- data API ----
    def acquire(self) -> Tuple[np.ndarray, np.ndarray]:
        """
        Return (wavelengths, intensities).
        Refresh the SDK axis for every captured frame. LightField can also
        change calibration outside this adapter's setting methods.
        """
        with capture_operation(self.setup) as operation:
            bound_sensor = vars(self.setup).get("_sensor_intent")
            custom = isinstance(bound_sensor, dict) and bound_sensor.get("mode") == "custom"
            if custom:
                # Keep the exact reviewed intent through Capture and wavelength
                # readback; a later drift must never repair/relabel this frame.
                before = self.setup.read_sensor_snapshot()
                verify_sensor_readout(before, bound_sensor)
            # Keep the owner/deadline through the SDK wavelength readback too.
            if hasattr(self.setup, "acquire_2d"):
                y = np.asarray(self.setup.acquire_2d(), dtype=float)
            else:
                y = np.asarray(self.setup.acquire(), dtype=float)
            operation.stage("wavelength_calibration")
            wl = self.calibration_wavelengths(force=True)
            operation.stage("align_spectrum")
            if custom:
                observed = self.setup.read_sensor_snapshot()
                verify_sensor_readout(observed, bound_sensor)
                result = _align_custom_spectrum(wl, y, observed)
            elif y.ndim == 2 and y.shape[0] > 1 and y.shape[1] > 1:
                result = align_wavelengths_to_image(wl, y)
            else:
                result = align_wavelengths_to_intensities(wl, y.ravel())
            operation.check()
            return result

    def abort_acquisition(self) -> bool:
        """Forward a best-effort cancellation request to LightField."""
        method = getattr(self.setup, "abort_acquisition", None)
        return bool(method()) if callable(method) else False

    def acquire_2d(self):
        """Use the setup's validated capture and metadata path for 2D too."""
        with capture_operation(self.setup) as operation:
            result = self.setup.acquire_2d()
            operation.check()
            return result

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

