# utils/mock_lf6.py
# ──────────────────────────────────────────────────────────────────────────────
# Drop-in mock for lf6_automation.LF6Setup.
#
# Purpose: develop and test UI panels without a live LightField installation.
# The mock mirrors every method called by:
#   - lf6_automation.LF6Setup          (real instrument)
#   - app/devices/lf6_adapter.py       (SpectrometerLF6 wrapper)
#   - controllers/lf6_controller.py    (Qt controller)
#
# Spectrum model:
#   Gaussian emission peak centred at `center_nm` + shot noise scaled by
#   sqrt(intensity * exposure_ms).  A second weaker satellite peak sits
#   +15 nm away to look like real PL data.
#
# Usage:
#   from utils.mock_lf6 import MockLF6Setup
#   lf6 = MockLF6Setup()                   # no .NET, no hardware
#   wl, cts = lf6.get_wavelength_calibration(), lf6.acquire()
#
# Reloadability:
#   importlib.reload(utils.mock_lf6) is safe — no module-level side effects.
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import time
import numpy as np
from typing import List


# ── Constants matching a typical Princeton Instruments PIXIS / PyLoN CCD ──────
_DEFAULT_N_PIXELS = 1340          # detector width in pixels
_DEFAULT_DISPERSION_NM_PX = 0.05  # nm per pixel at the detector
_SIMULATE_ACQUIRE_DELAY = True    # set False to skip exposure sleep in tests


class MockLF6Setup:
    """
    Drop-in replacement for lf6_automation.LF6Setup.

    Maintains internal state (center_nm, exposure_ms, accumulations) and
    generates synthetic Gaussian spectra.  No .NET / COM dependency.
    """

    # -- fake experiment names returned by get_saved_experiments() ------------
    _FAKE_EXPERIMENTS: List[str] = [
        "Mock_Experiment_Default",
        "Mock_Experiment_PL",
        "Mock_Experiment_Raman",
    ]

    def __init__(
        self,
        n_pixels: int = _DEFAULT_N_PIXELS,
        dispersion_nm_px: float = _DEFAULT_DISPERSION_NM_PX,
        center_nm: float = 860.0,
        exposure_ms: float = 2000.0,
        simulate_delay: bool = _SIMULATE_ACQUIRE_DELAY,
    ) -> None:
        self._n_pixels = n_pixels
        self._dispersion = dispersion_nm_px
        self._center_nm = float(center_nm)
        self._exposure_ms = float(exposure_ms)
        self._accumulations = 1
        self._simulate_delay = simulate_delay
        self._roi = "FullSensor"
        self._exit_port = "Front"
        self._frames_to_save = 1
        self._loaded_experiment: str | None = None
        self._rng = np.random.default_rng(seed=42)

    # ── internal helpers ──────────────────────────────────────────────────────

    def _wavelength_axis(self) -> np.ndarray:
        """Return the wavelength axis for the current center wavelength."""
        half = self._n_pixels / 2.0
        pixels = np.arange(self._n_pixels, dtype=float)
        return self._center_nm + (pixels - half) * self._dispersion

    def _make_spectrum(self, wl: np.ndarray) -> np.ndarray:
        """
        Generate a realistic synthetic PL spectrum:
          - Main peak at center_nm (FWHM ~ 20 nm)
          - Satellite peak at center_nm + 15 nm (1/5 amplitude)
          - Poisson shot noise scaled by sqrt(signal * exposure_ms / 1000)
          - Flat background ~ 200 counts
        """
        bg = 200.0
        peak_amp = 30_000.0 * (self._exposure_ms / 1000.0) * self._accumulations

        sigma_main = 8.5          # nm  (FWHM ≈ 20 nm)
        sigma_sat  = 6.0          # nm
        c0 = self._center_nm
        c1 = c0 + 15.0

        signal = (
            bg
            + peak_amp * np.exp(-0.5 * ((wl - c0) / sigma_main) ** 2)
            + (peak_amp / 5.0) * np.exp(-0.5 * ((wl - c1) / sigma_sat) ** 2)
        )

        # Shot noise: σ = sqrt(counts)
        noise = self._rng.normal(0.0, np.sqrt(np.maximum(signal, 1.0)))
        return np.clip(signal + noise, 0.0, None).astype(np.float32)

    def _maybe_sleep(self) -> None:
        if self._simulate_delay:
            time.sleep(self._exposure_ms / 1000.0 * 0.05)   # 5 % of exposure

    # ── LF6Setup public API ───────────────────────────────────────────────────

    # --- experiment management -----------------------------------------------

    def get_saved_experiments(self) -> List[str]:
        return list(self._FAKE_EXPERIMENTS)

    def print_saved_experiments(self) -> None:
        print("Mock saved experiments:")
        for name in self._FAKE_EXPERIMENTS:
            print(f"\t{name}")

    def load_experiment(self, exp_name: str) -> None:
        self._loaded_experiment = exp_name
        print(f"[MockLF6] load_experiment: {exp_name!r}")

    # --- acquisition ---------------------------------------------------------

    def acquire(self) -> np.ndarray:
        """Return 1-D spectrum array (n_pixels,)."""
        self._maybe_sleep()
        wl = self._wavelength_axis()
        return self._make_spectrum(wl)

    def acquire_2d(self) -> np.ndarray:
        """
        Return 2-D array (n_rows, n_pixels) simulating a CCD image.
        n_rows = 100 (typical binned rows for full-sensor mode).
        """
        self._maybe_sleep()
        wl = self._wavelength_axis()
        spectrum_1d = self._make_spectrum(wl)
        n_rows = 100
        # Spatial variation: Gaussian profile across rows (peak at centre row)
        rows = np.arange(n_rows, dtype=float)
        row_profile = np.exp(-0.5 * ((rows - n_rows / 2.0) / 15.0) ** 2)
        image = spectrum_1d[np.newaxis, :] * row_profile[:, np.newaxis]
        noise = self._rng.normal(0.0, 5.0, image.shape).astype(np.float32)
        return np.clip(image + noise, 0.0, None).astype(np.float32)

    def get_wavelength_calibration(self) -> np.ndarray:
        """Return wavelength axis as float64 array, matching real LF6 output."""
        return self._wavelength_axis().astype(np.float64)

    # --- settings: exposure --------------------------------------------------

    def change_expose_time(self, value) -> None:
        self._exposure_ms = float(value)
        print(f"[MockLF6] Exposure time set to {self._exposure_ms:.1f} ms")

    # --- settings: grating centre wavelength ---------------------------------

    def change_spectra_center(self, value) -> None:
        self._center_nm = float(value)
        print(f"[MockLF6] Center wavelength set to {self._center_nm:.1f} nm")

    def change_center_wavelength(self, wavelength) -> None:
        """Alias used by older calling code."""
        self.change_spectra_center(wavelength)

    # --- settings: online processing (frame combine) -------------------------

    def change_frame_to_combine(self, frames: int) -> None:
        self._accumulations = max(1, int(frames))
        print(f"[MockLF6] Frames to combine set to {self._accumulations}")

    def readback_online_process(self) -> dict:
        return {
            "exposures_per_frame": self._accumulations,
            "combine_mode": "Mean",
        }

    def set_frames_to_save(self, frames: int) -> None:
        self._frames_to_save = int(frames)
        print(f"[MockLF6] Frames to save set to {self._frames_to_save}")

    # --- settings: ROI -------------------------------------------------------

    def change_roi_FullSensor(self) -> None:
        self._roi = "FullSensor"
        print("[MockLF6] ROI → FullSensor")

    def change_roi_LineSensor(self) -> None:
        self._roi = "LineSensor"
        print("[MockLF6] ROI → LineSensor")

    # --- settings: exit port -------------------------------------------------

    def change_to_side_exit_port(self) -> None:
        self._exit_port = "Side"
        print("[MockLF6] Exit port → Side")

    def change_to_front_exit_port(self) -> None:
        self._exit_port = "Front"
        print("[MockLF6] Exit port → Front")

    # --- multi-frame ---------------------------------------------------------

    def multi_frame_acquire(
        self,
        image_mode: bool = False,
        x_pixels_num: int = 512,
        y_pixels_num: int = 512,
    ) -> np.ndarray:
        frames = self._frames_to_save
        wl = self._wavelength_axis()
        if image_mode:
            buf = np.zeros((frames, x_pixels_num * y_pixels_num), dtype=np.float32)
            for i in range(frames):
                buf[i, :x_pixels_num] = self._make_spectrum(wl[:x_pixels_num])
        else:
            buf = np.zeros((frames, x_pixels_num), dtype=np.float32)
            for i in range(frames):
                buf[i, :] = self._make_spectrum(wl[:x_pixels_num])
        return buf

    # --- legacy helper -------------------------------------------------------

    def create_spectra_sweep(self, sample_name: str, exp_name: str):
        """Return a minimal mock SpectraSweep-compatible object."""
        return _MockSpectraSweep(sample_name, exp_name, self)

    # ── state inspection (for tests / debugging) ──────────────────────────────

    @property
    def center_nm(self) -> float:
        return self._center_nm

    @property
    def exposure_ms(self) -> float:
        return self._exposure_ms

    @property
    def accumulations(self) -> int:
        return self._accumulations


# ── Adapter shim: same interface as SpectrometerLF6, no lf6_automation import ─

class MockAdapter:
    """
    Wraps MockLF6Setup with the same interface as
    app.devices.lf6_adapter.SpectrometerLF6 — used by LF6Controller when
    running in mock mode so that lf6_adapter.py (which imports clr) is never
    touched.
    """

    def __init__(self, setup: MockLF6Setup) -> None:
        self.setup = setup
        self._wls_cache: np.ndarray | None = None

    def invalidate_wavelengths(self) -> None:
        self._wls_cache = None

    def calibration_wavelengths(self, force: bool = False) -> np.ndarray:
        if force or self._wls_cache is None:
            self._wls_cache = self.setup.get_wavelength_calibration()
        return self._wls_cache

    def acquire(self):
        cts = self.setup.acquire()
        wl  = self.calibration_wavelengths(force=True)
        return wl, cts

    def acquire_2d(self) -> np.ndarray:
        return self.setup.acquire_2d()

    def change_spectra_center(self, center_nm) -> None:
        self.setup.change_spectra_center(center_nm)
        self.invalidate_wavelengths()

    def set_accumulations(self, n: int) -> None:
        self.setup.change_frame_to_combine(n)

    def set_frames(self, n: int) -> None:
        self.set_accumulations(n)

    def __getattr__(self, name):
        return getattr(self.setup, name)


# ── Minimal SpectraSweep mock ─────────────────────────────────────────────────

class _MockSpectraSweep:
    def __init__(self, sample_name: str, exp_name: str, lf6: MockLF6Setup) -> None:
        self.sample_name = sample_name
        self.exp_name = exp_name
        self._lf6 = lf6
        self.wavelengths: list = []
        self.total_triggers: int = 0
        self.current_trigger: int = 0
        self.plot = False

    def calibrate_wavelength(self) -> list:
        self.wavelengths = list(self._lf6.get_wavelength_calibration())
        return self.wavelengths

    def set_sweep(self, frames: int, plot: bool = False) -> None:
        self.plot = plot
        self.calibrate_wavelength()
        self.total_triggers = frames
        self.current_trigger = 0

    def trigger(self) -> np.ndarray | None:
        if self.current_trigger >= self.total_triggers:
            return None
        data = self._lf6.acquire()
        self.current_trigger += 1
        return data
