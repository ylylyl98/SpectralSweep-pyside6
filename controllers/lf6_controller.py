# controllers/lf6_controller.py
# ──────────────────────────────────────────────────────────────────────────────
# Qt controller for the LightField 6 spectrometer.
#
# Design rules:
#   - All LF6 state lives HERE.  UI panels never hold a reference to LF6Setup
#     or SpectrometerLF6 directly.
#   - Every blocking operation (connect, acquire) runs in a QThread worker so
#     the GUI event loop never stalls.
#   - UI panels connect to signals; they never call instrument methods directly.
#   - importlib.reload() on any ui/ module is safe: signals are reconnected on
#     panel construction; this controller is never reloaded.
#
# Signals emitted (all on the main thread via queued connections):
#   connected(list[str])         after successful connect; payload = saved experiments
#   disconnected()
#   error(str)                   any instrument-side exception
#   spectrum_ready(np.ndarray, np.ndarray)   wl, intensity arrays (1-D)
#   frame_ready(np.ndarray)      2-D array from acquire_2d()
#   settings_applied()           after exposure/center/accum applied OK
#   wavelengths_updated(np.ndarray)   fresh calibration vector
#
# Public slots (call from UI via direct call or Qt slot):
#   connect_instrument(use_mock=False)
#   disconnect_instrument()
#   apply_settings(exposure_ms, center_nm, accumulations)
#   acquire_single()
#   acquire_2d()
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal, Slot

# ── project root on sys.path so app/ and utils/ are importable ────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from utils.config import cfg


# ── Worker: runs blocking LF6 calls off the GUI thread ────────────────────────

class _LF6Worker(QObject):
    """
    Lives inside a QThread.  All methods that talk to hardware run here.
    Never construct directly — LF6Controller owns it.
    """

    # outbound signals → main thread
    connected      = Signal(list)       # list of saved experiment names
    disconnected   = Signal()
    error          = Signal(str)
    spectrum_ready = Signal(object, object)   # wl ndarray, cts ndarray
    frame_ready    = Signal(object)           # 2-D ndarray
    settings_applied   = Signal()
    wavelengths_updated = Signal(object)      # wl ndarray

    def __init__(self) -> None:
        super().__init__()
        self._setup  = None   # lf6_automation.LF6Setup or MockLF6Setup
        self._adapter = None  # app.devices.lf6_adapter.SpectrometerLF6

    # ── connect / disconnect ──────────────────────────────────────────────────

    @Slot(bool)
    def connect_instrument(self, use_mock: bool) -> None:
        try:
            if use_mock:
                # MockAdapter never imports lf6_automation / clr
                from utils.mock_lf6 import MockLF6Setup, MockAdapter
                self._setup = MockLF6Setup(
                    center_nm=cfg.lf6.center_nm,
                    exposure_ms=cfg.lf6.exposure_ms,
                    simulate_delay=False,
                )
                self._adapter = MockAdapter(self._setup)
            else:
                # Real path: lazy-import so clr is only touched on real hardware
                import lf6_automation
                self._setup = lf6_automation.LF6Setup()
                from app.devices.lf6_adapter import SpectrometerLF6
                self._adapter = SpectrometerLF6(self._setup)

            # Apply saved config values
            self._setup.change_expose_time(cfg.lf6.exposure_ms)
            self._setup.change_spectra_center(cfg.lf6.center_nm)
            self._setup.change_frame_to_combine(cfg.lf6.accumulations)

            experiments: list = []
            try:
                experiments = list(self._setup.get_saved_experiments())
            except Exception:
                pass

            self.connected.emit(experiments)

        except Exception as exc:
            self._setup  = None
            self._adapter = None
            self.error.emit(f"LF6 connect failed: {exc}\n{traceback.format_exc()}")

    @Slot()
    def disconnect_instrument(self) -> None:
        self._setup   = None
        self._adapter = None
        self.disconnected.emit()

    # ── settings ─────────────────────────────────────────────────────────────

    @Slot(float, float, int)
    def apply_settings(self, exposure_ms: float, center_nm: float, accumulations: int) -> None:
        if self._setup is None:
            self.error.emit("LF6 not connected.")
            return
        try:
            self._setup.change_expose_time(exposure_ms)
            self._setup.change_spectra_center(center_nm)
            self._setup.change_frame_to_combine(accumulations)
            if self._adapter is not None:
                self._adapter.invalidate_wavelengths()
            # emit fresh calibration after centre change
            wl = self._get_wavelengths()
            self.wavelengths_updated.emit(wl)
            self.settings_applied.emit()
        except Exception as exc:
            self.error.emit(f"LF6 apply_settings failed: {exc}")

    # ── acquisition ──────────────────────────────────────────────────────────

    @Slot()
    def acquire_single(self) -> None:
        if self._adapter is None:
            self.error.emit("LF6 not connected.")
            return
        try:
            wl, cts = self._adapter.acquire()
            self.spectrum_ready.emit(wl, cts)
        except Exception as exc:
            self.error.emit(f"LF6 acquire failed: {exc}")

    @Slot()
    def acquire_2d(self) -> None:
        if self._setup is None:
            self.error.emit("LF6 not connected.")
            return
        try:
            img = self._setup.acquire_2d()
            self.frame_ready.emit(img)
        except Exception as exc:
            self.error.emit(f"LF6 acquire_2d failed: {exc}")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _get_wavelengths(self) -> np.ndarray:
        if self._adapter is not None:
            return self._adapter.calibration_wavelengths(force=True)
        if self._setup is not None:
            return np.asarray(self._setup.get_wavelength_calibration(), dtype=float)
        return np.array([], dtype=float)

    # ── direct-call helpers (called by app.steps via lf6 property) ────────────

    @property
    def adapter(self):
        """Return the SpectrometerLF6 adapter for use by sweep steps."""
        return self._adapter

    @property
    def setup(self):
        """Return the raw LF6Setup (or mock) for methods not on the adapter."""
        return self._setup


# ── Public controller ─────────────────────────────────────────────────────────

class LF6Controller(QObject):
    """
    Owned by main.py; shared across all UI panels via dependency injection.

    Usage:
        ctrl = LF6Controller()
        ctrl.connected.connect(my_panel.on_lf6_connected)
        ctrl.connect_instrument(use_mock=True)
    """

    # Re-export worker signals so callers only need a reference to the controller
    connected           = Signal(list)
    disconnected        = Signal()
    error               = Signal(str)
    spectrum_ready      = Signal(object, object)
    frame_ready         = Signal(object)
    settings_applied    = Signal()
    wavelengths_updated = Signal(object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)

        self._thread = QThread(self)
        self._worker = _LF6Worker()
        self._worker.moveToThread(self._thread)

        # wire worker → controller signals (queued across thread boundary)
        self._worker.connected.connect(self.connected)
        self._worker.disconnected.connect(self.disconnected)
        self._worker.error.connect(self.error)
        self._worker.spectrum_ready.connect(self.spectrum_ready)
        self._worker.frame_ready.connect(self.frame_ready)
        self._worker.settings_applied.connect(self.settings_applied)
        self._worker.wavelengths_updated.connect(self.wavelengths_updated)

        self._thread.start()

    # ── public API (called from main thread) ──────────────────────────────────

    def connect_instrument(self, use_mock: bool = False) -> None:
        """Start LF6 connection in background thread."""
        self._worker.connect_instrument.__func__(self._worker, use_mock)

    def disconnect_instrument(self) -> None:
        self._worker.disconnect_instrument.__func__(self._worker)

    def apply_settings(
        self,
        exposure_ms: float,
        center_nm: float,
        accumulations: int,
    ) -> None:
        self._worker.apply_settings.__func__(
            self._worker, exposure_ms, center_nm, accumulations
        )

    def acquire_single(self) -> None:
        self._worker.acquire_single.__func__(self._worker)

    def acquire_2d(self) -> None:
        self._worker.acquire_2d.__func__(self._worker)

    # ── state accessors (read from main thread — be aware of races) ───────────

    @property
    def is_connected(self) -> bool:
        return self._worker.adapter is not None

    @property
    def adapter(self):
        """SpectrometerLF6 instance; None if not connected."""
        return self._worker.adapter

    @property
    def setup(self):
        """Raw LF6Setup / MockLF6Setup; None if not connected."""
        return self._worker.setup

    # ── cleanup ───────────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Call from main.py on application exit."""
        self._worker.disconnect_instrument.__func__(self._worker)
        self._thread.quit()
        self._thread.wait(3000)
