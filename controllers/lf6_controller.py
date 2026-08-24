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
import time
import logging
from enum import Enum
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal, Slot

# ── project root on sys.path so app/ and utils/ are importable ────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from utils.config import cfg

_LOG = logging.getLogger(__name__)


class LightFieldLifecycleState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    STARTING = "STARTING"
    INITIALIZING = "INITIALIZING"
    READY = "READY"


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
    state_changed      = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._setup  = None   # lf6_automation.LF6Setup or MockLF6Setup
        self._adapter = None  # app.devices.lf6_adapter.SpectrometerLF6
        self._state = LightFieldLifecycleState.DISCONNECTED

    def _transition(self, state: LightFieldLifecycleState) -> None:
        self._state = state
        _LOG.info("LightField lifecycle -> %s", state.value)
        self.state_changed.emit(state)

    # ── connect / disconnect ──────────────────────────────────────────────────

    @Slot(bool)
    def connect_instrument(self, use_mock: bool) -> None:
        self._transition(LightFieldLifecycleState.STARTING)
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

            self._transition(LightFieldLifecycleState.INITIALIZING)
            # Startup is connection-only. Mutable experiment settings are deferred
            # to the shared acquisition preflight immediately before a run.
            self.wait_until_ready()

            experiments: list = []
            try:
                experiments = list(self._setup.get_saved_experiments())
            except Exception:
                pass

            self._transition(LightFieldLifecycleState.READY)
            self.connected.emit(experiments)

        except Exception as exc:
            self._setup  = None
            self._adapter = None
            self._transition(LightFieldLifecycleState.DISCONNECTED)
            self.error.emit(f"LF6 connect failed: {exc}\n{traceback.format_exc()}")

    @Slot()
    def disconnect_instrument(self) -> None:
        self._setup   = None
        self._adapter = None
        self._transition(LightFieldLifecycleState.DISCONNECTED)
        self.disconnected.emit()

    # ── settings ─────────────────────────────────────────────────────────────

    @Slot(float, float, int)
    def apply_settings(self, exposure_ms: float, center_nm: float, accumulations: int) -> None:
        if self._setup is None:
            self.error.emit("LF6 not connected.")
            return
        try:
            self.configure_for_acquisition(
                center_nm=center_nm, exposure_ms=exposure_ms, frames=accumulations
            )
            # emit fresh calibration after centre change
            wl = self._get_wavelengths()
            self.wavelengths_updated.emit(wl)
            self.settings_applied.emit()
        except Exception as exc:
            self.error.emit(f"LF6 apply_settings failed: {exc}")

    def set_center_wavelength_when_ready(self, center_nm: float, **kwargs) -> None:
        """Use LF6's bounded readiness/writeability wait for center changes."""
        if self._setup is None:
            raise RuntimeError("LF6 not connected")
        if self._state is not LightFieldLifecycleState.READY:
            raise RuntimeError(f"LightField is not ready for settings (state={self._state.value})")
        method = getattr(self._setup, "set_center_wavelength_when_ready", None)
        if callable(method):
            method(float(center_nm), **kwargs)
            return
        raise RuntimeError("LF6 guarded center-wavelength setter is unavailable")

    def wait_until_ready(
        self,
        *,
        timeout_s: float = 15.0,
        poll_interval_s: float = 0.05,
        stable_polls: int = 3,
    ) -> None:
        if self._setup is None:
            raise RuntimeError("LF6 not connected")
        timeout_s = float(timeout_s)
        poll_interval_s = float(poll_interval_s)
        stable_polls = int(stable_polls)
        if timeout_s <= 0 or poll_interval_s <= 0 or stable_polls <= 0:
            raise ValueError("LightField readiness timings must be positive")
        deadline = time.monotonic() + timeout_s
        consecutive_ready = 0
        last_snapshot = None
        while True:
            snapshot_value = getattr(self._setup, "readiness_snapshot", None)
            snapshot = snapshot_value() if callable(snapshot_value) else snapshot_value
            if isinstance(snapshot, dict):
                ready = bool(snapshot.get("ready", False))
                busy = bool(snapshot.get("busy", False))
            else:
                ready_value = getattr(self._setup, "is_ready", False)
                busy_value = getattr(self._setup, "is_busy", False)
                ready = bool(ready_value() if callable(ready_value) else ready_value)
                busy = bool(busy_value() if callable(busy_value) else busy_value)
                snapshot = {"ready": ready, "busy": busy}
            if snapshot != last_snapshot:
                _LOG.info("LightField readiness evidence: %s", snapshot)
                last_snapshot = snapshot
            if ready and not busy:
                consecutive_ready += 1
                if consecutive_ready >= stable_polls:
                    return
            else:
                consecutive_ready = 0
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"LightField did not reach a stable ready/non-busy state within {timeout_s:g}s; "
                    f"last evidence={last_snapshot}"
                )
            time.sleep(min(poll_interval_s, remaining))

    def ensure_ready(self, **kwargs) -> None:
        """Re-evaluate the existing shared LightField connection without respawning it."""
        if self._setup is None:
            raise RuntimeError("LightField is not connected")
        if self._state is LightFieldLifecycleState.READY and self.is_ready:
            return
        if self._state not in (
            LightFieldLifecycleState.STARTING,
            LightFieldLifecycleState.INITIALIZING,
            LightFieldLifecycleState.READY,
        ):
            raise RuntimeError(f"LightField cannot become ready from state={self._state.value}")
        self.wait_until_ready(**kwargs)
        if self._state is not LightFieldLifecycleState.READY:
            self._transition(LightFieldLifecycleState.READY)

    def configure_for_acquisition(self, *, center_nm, exposure_ms, frames):
        if self._setup is None or self._state is not LightFieldLifecycleState.READY:
            raise RuntimeError(f"LightField is not ready for acquisition (state={self._state.value})")
        method = getattr(self._setup, "configure_for_acquisition", None)
        if not callable(method):
            raise RuntimeError("LightField acquisition preparation is unavailable")
        result = method(center_nm=center_nm, exposure_ms=exposure_ms, frames=frames)
        if self._adapter is not None:
            self._adapter.invalidate_wavelengths()
        return result

    @property
    def state(self):
        return self._state

    @property
    def is_ready(self) -> bool:
        if self._setup is None:
            return False
        value = getattr(self._setup, "is_ready", True)
        return bool(value() if callable(value) else value)

    @property
    def is_busy(self) -> bool:
        if self._setup is None:
            return False
        value = getattr(self._setup, "is_busy", False)
        return bool(value() if callable(value) else value)

    def setting_is_available(self, setting) -> bool:
        method = getattr(self._setup, "setting_is_available", None)
        return bool(method(setting)) if callable(method) else self._setup is not None

    def setting_is_writable(self, setting):
        method = getattr(self._setup, "setting_is_writable", None)
        return method(setting) if callable(method) else None

    def wait_until_setting_writable(self, setting, **kwargs) -> None:
        method = getattr(self._setup, "wait_until_setting_writable", None)
        if callable(method):
            method(setting, **kwargs)

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

    # ── direct-call helpers used by sweep workers ──────────────────

    @property
    def adapter(self):
        """Return the SpectrometerLF6 adapter for use by sweep workers."""
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
    state_changed       = Signal(object)

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
        self._worker.state_changed.connect(self.state_changed)

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

    def set_center_wavelength_when_ready(self, center_nm: float, **kwargs) -> None:
        """Set center wavelength through the shared LF6 setup after readiness."""
        self._worker.set_center_wavelength_when_ready.__func__(
            self._worker, center_nm, **kwargs
        )

    def wait_until_setting_writable(self, setting, **kwargs) -> None:
        self._worker.wait_until_setting_writable.__func__(
            self._worker, setting, **kwargs
        )

    def wait_until_ready(self, **kwargs) -> None:
        self._worker.wait_until_ready.__func__(self._worker, **kwargs)

    def ensure_ready(self, **kwargs) -> None:
        """Wait for the existing shared LightField connection to become usable."""
        self._worker.ensure_ready.__func__(self._worker, **kwargs)

    def configure_for_acquisition(self, *, center_nm, exposure_ms, frames):
        """Shared LightField preflight used by every acquisition tab."""
        return self._worker.configure_for_acquisition.__func__(
            self._worker, center_nm=center_nm, exposure_ms=exposure_ms, frames=frames
        )

    prepare_acquisition = configure_for_acquisition

    def acquire_single(self) -> None:
        self._worker.acquire_single.__func__(self._worker)

    def acquire_2d(self) -> None:
        self._worker.acquire_2d.__func__(self._worker)

    # ── state accessors (read from main thread — be aware of races) ───────────

    @property
    def is_connected(self) -> bool:
        return self._worker.adapter is not None and self.state is LightFieldLifecycleState.READY

    @property
    def state(self):
        return self._worker.state

    @property
    def is_ready(self) -> bool:
        return self.state is LightFieldLifecycleState.READY and bool(self._worker.is_ready)

    @property
    def is_busy(self) -> bool:
        return bool(self._worker.is_busy)

    def setting_is_available(self, setting) -> bool:
        return self._worker.setting_is_available(setting)

    def setting_is_writable(self, setting):
        return self._worker.setting_is_writable(setting)

    @property
    def center_wavelength_write_stats(self):
        setup = self._worker.setup
        return dict(getattr(setup, "center_wavelength_write_stats", {}) or {})

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
