# controllers/lf6_controller.py
# ──────────────────────────────────────────────────────────────────────────────
# Qt controller for the shared spectrum backend.
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
import threading
from enum import Enum
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, QMetaObject, QThread, QTimer, Qt, Signal, Slot

# ── project root on sys.path so app/ and utils/ are importable ────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from utils.config import cfg

_LOG = logging.getLogger(__name__)
_SPECTRUM_OWNERSHIP_ERROR = "Spectrum controls are unavailable while another measurement owns the spectrometer."


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
    acquisition_settings_readback = Signal(object)
    spectrograph_status_ready = Signal(object)
    wavelengths_updated = Signal(object)      # wl ndarray
    state_changed      = Signal(object)
    temperature_ready  = Signal(object)
    temperature_snapshot_ready = Signal(object)
    acquisition_finished = Signal()
    cooler_changed     = Signal(bool)
    andor_status_ready = Signal(object)
    andor_controls_applied = Signal(object)
    shamrock_connection_changed = Signal(bool)
    backend_temperature_snapshot = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._setup  = None   # lf6_automation.LF6Setup or MockLF6Setup
        self._adapter = None  # app.devices.lf6_adapter.SpectrometerLF6
        self._state = LightFieldLifecycleState.DISCONNECTED
        self._backend = "lightfield"
        self._identity = {}
        self.temperature_monitor_paused = threading.Event()
        self.manual_temperature_paused = threading.Event()
        self.external_measurement_active = threading.Event()
        self._scan_adapter = None
        self._scan_selected = False
        self._parked = {}
        self._experiments = []

    @property
    def connected_backends(self):
        return tuple(self._parked) + ((self._backend,) if self._setup is not None else ())

    def _park_active(self):
        if self._setup is not None:
            self._parked[self._backend] = (self._setup, self._adapter, self._identity, self._experiments)
        self._setup = self._adapter = None
        self._identity = {}

    @staticmethod
    def _route_selected_detector(setup, adapter, identity):
        route = identity.get('output_route')
        backend = identity.get('backend')
        if not route or backend not in {'lightfield', 'winspec_ingaas'}:
            return None
        from app.devices.lightfield_optics import ensure_output_route
        optics = setup.lightfield if backend == 'winspec_ingaas' else setup
        # Invalidate even on readback failure: the physical mirror may have moved.
        if adapter is not None:
            adapter.invalidate_wavelengths()
        return ensure_output_route(optics, route)

    def _activate(self, backend, *, route_output=True, notify=True):
        setup, adapter, identity, _ = self._parked[backend]
        snapshot = self._route_selected_detector(setup, adapter, identity) if route_output else None
        self._setup, self._adapter, self._identity, self._experiments = self._parked.pop(backend)
        self._backend = backend
        self._transition(LightFieldLifecycleState.READY)
        if notify:
            self.connected.emit(self._experiments)
        if snapshot is not None and notify:
            self.spectrograph_status_ready.emit(snapshot)

    @Slot(object)
    def prepare_scan_condition(self, request):
        """Scan-owned, serialized setup change; measurement locks stay held."""
        try:
            if request['stop'].is_set():
                raise RuntimeError('Optical sequence stopped')
            if not self.temperature_monitor_paused.is_set() or self.is_busy:
                raise RuntimeError('Setup selection requires an idle detector under measurement ownership')
            backend = request['backend']
            if backend not in self.connected_backends:
                raise RuntimeError(f'Please connect {backend} before starting the optical sequence')
            self._scan_adapter = None
            if backend != self._backend:
                previous = self._backend
                self._park_active()
                try:
                    self._activate(backend, notify=False)
                except Exception:
                    self._activate(previous, route_output=False, notify=False)
                    raise
            self._scan_selected = True
            adapter = self._adapter
            if backend == 'winspec_ingaas':
                from app.devices.winspec_scan_adapter import WinSpecScanAdapter
                adapter = WinSpecScanAdapter(self._setup)
            frames = 1 if request['reduction'] == 'average' else request['frames']
            from app.lightfield_diagnostics import center_write_context
            with center_write_context(self._setup, source='scan recipe', backend=backend):
                if backend == 'winspec_ingaas':
                    adapter.configure_for_acquisition(center_nm=request['center'], exposure_ms=request['exposure'], frames=frames)
                else:
                    self.configure_for_acquisition(center_nm=request['center'], exposure_ms=request['exposure'], frames=frames)
            if request['reduction'] == 'average':
                if backend == 'lightfield':
                    readback = self._setup.readback_online_process()
                    if int(readback.get('exposures_per_frame') or 0) != 1:
                        raise RuntimeError('PIXIS averaging requires verified single-exposure frames; check LightField online processing')
                from app.devices.scan_average import AveragedScanAdapter
                adapter = AveragedScanAdapter(adapter, request['frames'], request['stop'])
            self._scan_adapter = adapter
            request['result'] = adapter
        except Exception as exc:
            error = RuntimeError(
                f"Setup preparation failed (requested_setup={request.get('backend')}, "
                f"active_setup={self._backend}, "
                f"configured_exit={self._identity.get('output_route', 'unreported')}): {exc}")
            error.__cause__ = exc
            request['error'] = error
        finally:
            request['done'].set()

    def _transition(self, state: LightFieldLifecycleState) -> None:
        self._state = state
        _LOG.info("LightField lifecycle -> %s", state.value)
        self.state_changed.emit(state)

    # ── connect / disconnect ──────────────────────────────────────────────────

    @Slot(bool, str)
    def connect_instrument(self, use_mock: bool, backend: str = "lightfield") -> None:
        backend = str(backend or "lightfield").strip().lower()
        if backend not in {"lightfield", "andor_si", "andor_ingaas", "winspec_ingaas"}:
            self.error.emit(f"Unknown spectrum backend: {backend}")
            return
        if self.is_busy or self.temperature_monitor_paused.is_set():
            self.error.emit("Cannot switch spectrum devices during a measurement")
            return
        if backend == "winspec_ingaas":
            if use_mock:
                self.error.emit("WinSpec uses the XP bridge; uncheck Use mock and connect LightField first")
                return
            if "lightfield" not in self.connected_backends:
                self.error.emit("Connect LightField first, load its spectrograph experiment, then select WinSpec InGaAs")
                return
            lf_identity = self._identity if self._backend == 'lightfield' else self._parked['lightfield'][2]
            if lf_identity.get('backend', '').startswith('mock_'):
                self.error.emit('WinSpec requires a real LightField connection; reconnect LightField with Use mock unchecked')
                return
        if backend.startswith("andor_") and any(
            key.startswith("andor_") and key != backend for key in self.connected_backends
        ):
            self.error.emit("The Si and InGaAs roles share Shamrock. Disconnect the existing Andor camera before changing its role; LightField can remain online.")
            return
        previous = self._backend
        self._park_active()
        if backend in self._parked:
            try:
                self._activate(backend)
            except Exception as exc:
                if previous in self._parked:
                    self._activate(previous, route_output=False)
                self.error.emit(f'Device switch failed: {exc}. Verify the physical output before acquiring.')
            return
        self._backend = backend
        self._identity = {}
        self._transition(LightFieldLifecycleState.STARTING)
        try:
            if backend == "winspec_ingaas":
                from app.devices.winspec_adapter import WinSpecSetup
                lightfield = self._parked["lightfield"][0]
                self._setup = WinSpecSetup(
                    lightfield, host=cfg.lf6.winspec_host, port=cfg.lf6.winspec_port,
                    pixel_pitch_um=cfg.lf6.winspec_pixel_pitch_um,
                    acquisition_backend=cfg.lf6.winspec_acquisition_backend,
                    start_acceleration=(cfg.lf6.winspec_start_acceleration
                                        and cfg.lf6.winspec_acquisition_backend == 'winspec'),
                    output_route=cfg.lf6.optical_profiles[cfg.lf6.optical_profile]['winspec_ingaas'],
                )
                self._adapter = self._setup
                self._identity = self._setup.identity
            elif use_mock:
                # MockAdapter never imports lf6_automation / clr
                from utils.mock_lf6 import MockLF6Setup, MockAdapter
                self._setup = MockLF6Setup(
                    center_nm=cfg.lf6.center_nm,
                    exposure_ms=cfg.lf6.exposure_ms,
                    simulate_delay=False,
                )
                self._adapter = MockAdapter(self._setup)
                self._identity = {
                    "backend": f"mock_{backend}",
                    "camera_role": backend.removeprefix("andor_"),
                }
            elif backend == "lightfield":
                # Real path: lazy-import so clr is only touched on real hardware
                import lf6_automation
                self._setup = lf6_automation.LF6Setup()
                self._setup.detector_output_route = cfg.lf6.optical_profiles[cfg.lf6.optical_profile]['lightfield']
                from app.devices.lf6_adapter import SpectrometerLF6
                self._adapter = SpectrometerLF6(self._setup)
                self._identity = {"backend": "lightfield"}
            if backend in {'lightfield', 'winspec_ingaas'} and not use_mock:
                self._identity.update({'optical_profile': cfg.lf6.optical_profile,
                                       'output_route': cfg.lf6.optical_profiles[cfg.lf6.optical_profile][backend]})
            elif not use_mock and backend.startswith('andor_'):
                from app.devices.andor_adapter import (
                    AndorConnectionOptions,
                    AndorSDK2Setup,
                    SpectrometerAndor,
                )

                role = backend.removeprefix("andor_")
                index = (
                    cfg.lf6.andor_si_camera_index
                    if role == "si" else cfg.lf6.andor_ingaas_camera_index
                )
                serial = (
                    cfg.lf6.andor_si_serial
                    if role == "si" else cfg.lf6.andor_ingaas_serial
                )
                temperature_c = getattr(
                    cfg.lf6,
                    f"andor_{role}_temperature_c",
                    cfg.lf6.andor_temperature_c,
                )
                cooler_on_connect = getattr(
                    cfg.lf6,
                    f"andor_{role}_cooler_on_connect",
                    cfg.lf6.andor_cooler_on_connect,
                )
                fan_mode = getattr(
                    cfg.lf6,
                    f"andor_{role}_fan_mode",
                    cfg.lf6.andor_fan_mode,
                )
                output_port = getattr(
                    cfg.lf6,
                    f"andor_{role}_output_port",
                    "unchanged",
                )
                if role == "ingaas" and output_port == "unchanged":
                    output_port = "direct"
                options = AndorConnectionOptions(
                    camera_role=role,
                    camera_index=int(index),
                    camera_serial=str(serial),
                    spectrograph_index=int(cfg.lf6.andor_spectrograph_index),
                    sdk2_dll_dir=str(cfg.lf6.andor_sdk2_dll_dir),
                    shamrock_dll_dir=str(cfg.lf6.andor_shamrock_dll_dir),
                    temperature_c=float(temperature_c),
                    cooler_on_connect=bool(cooler_on_connect),
                    fan_mode=str(fan_mode),
                    output_port=str(output_port),
                    shutter_mode=str(cfg.lf6.andor_shutter_mode),
                    grating=int(cfg.lf6.andor_grating),
                    slit_width_um=float(cfg.lf6.andor_slit_width_um),
                    invert_wavelength_axis=bool(cfg.lf6.andor_invert_wavelength_axis),
                    discard_first=bool(cfg.lf6.andor_discard_first),
                    timeout_margin_s=float(cfg.lf6.andor_timeout_margin_s),
                )
                self._setup = AndorSDK2Setup(options)
                self._adapter = SpectrometerAndor(self._setup)
                self._identity = self._setup.identity

            self._transition(LightFieldLifecycleState.INITIALIZING)
            # Startup is connection-only. Mutable experiment settings are deferred
            # to the shared acquisition preflight immediately before a run.
            # An empty LightField experiment is a valid automation connection:
            # the user loads the instrument experiment in that same window.
            # Acquisition readiness is checked by explicit acquisition preflight.
            # Treating it as a connection failure used to orphan the window and
            # spawn another Automation instance on each Connect retry.
            if backend != 'lightfield' or use_mock:
                self.wait_until_ready()
            route_snapshot = None
            if backend == 'winspec_ingaas':
                route_snapshot = self._route_selected_detector(self._setup, self._adapter, self._identity)

            experiments: list = []
            try:
                experiments = list(self._setup.get_saved_experiments())
            except Exception:
                pass

            self._transition(LightFieldLifecycleState.READY)
            self._experiments = experiments
            self.connected.emit(experiments)
            if route_snapshot is not None:
                self.spectrograph_status_ready.emit(route_snapshot)

        except Exception as exc:
            failed_setup = self._setup
            self._setup  = None
            self._adapter = None
            close = getattr(failed_setup, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    _LOG.exception("Failed to close spectrum backend after connect error")
            self._transition(LightFieldLifecycleState.DISCONNECTED)
            if previous in self._parked:
                self._activate(previous, route_output=False)
            self.error.emit(
                f"{backend.replace('_', ' ').title()} connect failed: {exc}\n"
                f"{traceback.format_exc()}"
            )

    @Slot()
    def disconnect_instrument(self) -> None:
        if self._backend == "lightfield":
            dependent = self._parked.pop("winspec_ingaas", None)
            if dependent is not None:
                dependent[0].close()
        setup = self._setup
        self._setup = None
        self._adapter = None
        self._identity = {}
        close = getattr(setup, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                self.error.emit(f"Spectrometer disconnect warning: {exc}")
        self._transition(LightFieldLifecycleState.DISCONNECTED)
        self.disconnected.emit()

    @Slot()
    def disconnect_all(self):
        self.disconnect_instrument()
        for backend in list(self._parked):
            parked = self._parked.pop(backend, None)
            if parked is None:  # Closing LightField also closes its dependent WinSpec wrapper.
                continue
            self._setup, self._adapter, self._identity, self._experiments = parked
            self._backend = backend
            self.disconnect_instrument()

    def andor_setups(self):
        result = {key: value[0] for key, value in self._parked.items() if key.startswith("andor_")}
        if self._backend.startswith("andor_") and self._setup is not None:
            result[self._backend] = self._setup
        return result

    # ── settings ─────────────────────────────────────────────────────────────

    def _require_spectrum_access(self):
        # Recheck queued requests: a scan may have acquired ownership after dispatch.
        if self.external_measurement_active.is_set():
            raise RuntimeError(_SPECTRUM_OWNERSHIP_ERROR)

    @Slot(float, float, int)
    def apply_settings(self, exposure_ms: float, center_nm: float, accumulations: int) -> None:
        if self._setup is None:
            self.error.emit("Spectrometer not connected.")
            return
        try:
            self._require_spectrum_access()
            from app.lightfield_diagnostics import center_write_context
            with center_write_context(self._setup, source='Spectrum Apply', backend=self._backend):
                readback = self.configure_for_acquisition(
                    center_nm=center_nm, exposure_ms=exposure_ms, frames=accumulations
                )
            self.acquisition_settings_readback.emit(readback or {})
            # emit fresh calibration after centre change
            wl = self._get_wavelengths()
            self.wavelengths_updated.emit(wl)
            self.settings_applied.emit()
        except Exception as exc:
            self.error.emit(f"Spectrometer apply_settings failed: {exc}")

    def set_center_wavelength_when_ready(self, center_nm: float, **kwargs) -> None:
        """Use LF6's bounded readiness/writeability wait for center changes."""
        if self._setup is None:
            raise RuntimeError("LF6 not connected")
        if self._state is not LightFieldLifecycleState.READY:
            raise RuntimeError(f"LightField is not ready for settings (state={self._state.value})")
        method = getattr(self._setup, "set_center_wavelength_when_ready", None)
        if callable(method):
            if self._adapter is not None:
                self._adapter.invalidate_wavelengths()
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
    def backend(self) -> str:
        return self._backend

    @property
    def identity(self) -> dict:
        return dict(self._identity)

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
            self.error.emit("Spectrometer not connected.")
            self.acquisition_finished.emit()
            return
        try:
            self._require_spectrum_access()
            wl, cts = self._adapter.acquire()
            if self._backend == 'winspec_ingaas':
                frame = self._setup.read_metadata_snapshot()['observed']['last_frame']
                self.acquisition_settings_readback.emit({
                    'winspec_frame_context': self._setup.last_calibration_context,
                    'winspec_intensity_processing': frame.get('intensity_processing'),
                    'winspec_raw_accumulated_counts': frame.get('raw_accumulated_counts'),
                    'winspec_frame_datatype': frame.get('winspec_datatype'),
                    'winspec_temperature_guard': frame.get('temperature_guard'),
                    'winspec_start_acceleration': frame.get('start_acceleration'),
                    'winspec_capture_timing': {
                        'bridge': frame.get('bridge_timing_s'), 'client': frame.get('client_timing_s'),
                        'host': frame.get('host_timing_s'),
                        'signal_emitted_unix': time.time()}})
            self.spectrum_ready.emit(wl, cts)
        except Exception as exc:
            self.error.emit(f"Spectrometer acquire failed: {exc}")
        finally:
            self.acquisition_finished.emit()

    @Slot()
    def acquire_2d(self) -> None:
        if self._setup is None:
            self.error.emit("Spectrometer not connected.")
            self.acquisition_finished.emit()
            return
        try:
            self._require_spectrum_access()
            if self._backend in {"andor_ingaas", "winspec_ingaas"}:
                raise RuntimeError(
                    "The connected InGaAs detector is a one-dimensional array; "
                    "use Acquire 1D"
                )
            if self._backend == "andor_si":
                change_mode = getattr(self._setup, "change_roi_FullSensor", None)
                if callable(change_mode):
                    change_mode()
            img = self._setup.acquire_2d()
            self.frame_ready.emit(img)
        except Exception as exc:
            self.error.emit(f"Spectrometer acquire_2d failed: {exc}")
        finally:
            self.acquisition_finished.emit()

    @Slot()
    def read_temperature(self) -> None:
        if self.manual_temperature_paused.is_set() or self.is_busy:
            return
        method = getattr(self._setup, "get_temperature", None)
        if not callable(method):
            self.error.emit(
                "The connected spectrum backend has no detector temperature readback"
            )
            return
        try:
            self.temperature_ready.emit(method())
        except Exception as exc:
            self.error.emit(f"Detector temperature read failed: {exc}")

    @Slot(int)
    def read_temperature_snapshot(self, generation: int) -> None:
        # Recheck after queueing: a measurement may have started meanwhile.
        result = None
        try:
            if not self.temperature_monitor_paused.is_set() and not self.is_busy:
                method = getattr(self._setup, "get_temperature_snapshot", None)
                if callable(method):
                    result = method()
                for backend, session in self._parked.items():
                    if self.temperature_monitor_paused.is_set() or self.is_busy:
                        break
                    if not backend.startswith("andor_"):
                        continue
                    read = getattr(session[0], "get_temperature_snapshot", None)
                    if callable(read):
                        try:
                            self.backend_temperature_snapshot.emit((backend, read()))
                        except Exception as exc:
                            self.backend_temperature_snapshot.emit((backend, {"error": str(exc)}))
        except Exception as exc:
            # A monitor failure must never abort a measurement via error.
            result = {"error": str(exc)}
        finally:
            self.temperature_snapshot_ready.emit((generation, result))

    @Slot(bool)
    def set_cooler(self, on: bool) -> None:
        method = getattr(self._setup, "set_cooler", None)
        if not callable(method):
            self.error.emit("The connected spectrum backend has no cooler control")
            return
        try:
            method(bool(on))
            self.cooler_changed.emit(bool(on))
        except Exception as exc:
            self.error.emit(f"Detector cooler update failed: {exc}")

    @Slot()
    def refresh_andor_status(self) -> None:
        method = getattr(self._setup, "get_control_snapshot", None)
        if not callable(method):
            self.error.emit("Andor controls are unavailable for this spectrum backend")
            return
        try:
            self.andor_status_ready.emit(method(include_calibration=True))
        except Exception as exc:
            self.error.emit(f"Andor status refresh failed: {exc}")

    @Slot(object)
    def lightfield_optics(self, requested):
        if self._backend not in {"lightfield", "winspec_ingaas"} or self._setup is None:
            self.error.emit("LightField is not the active device")
            return
        try:
            from app.devices.lightfield_optics import read_optics, apply_optics
            optics_setup = self._setup.lightfield if self._backend == "winspec_ingaas" else self._setup
            snapshot = read_optics(optics_setup) if requested is None else apply_optics(optics_setup, dict(requested))
            if requested is not None and self._adapter is not None:
                self._adapter.invalidate_wavelengths()
            self.spectrograph_status_ready.emit(snapshot)
        except Exception as exc:
            self.error.emit(f"LightField spectrograph control failed: {exc}")

    @Slot(bool)
    def set_shamrock_connected(self, connected: bool) -> None:
        if self.is_busy or self.temperature_monitor_paused.is_set():
            self.error.emit("Cannot change Shamrock connection during a measurement")
            return
        method = getattr(self._setup, "reconnect_spectrograph" if connected else "disconnect_spectrograph", None)
        if not callable(method):
            self.error.emit("Separate Shamrock connection controls are unavailable")
            return
        try:
            method()
            self._identity["shamrock_connected"] = connected
            invalidate = getattr(self._adapter, "invalidate_wavelengths", None)
            if callable(invalidate):
                invalidate()
            self.shamrock_connection_changed.emit(connected)
            if connected:
                self.refresh_andor_status()
        except Exception as exc:
            self.error.emit(f"Shamrock connection change failed: {exc}")

    @Slot(object)
    def apply_andor_controls(self, settings: object) -> None:
        method = getattr(self._setup, "apply_controls", None)
        if not callable(method):
            self.error.emit("Andor controls are unavailable for this spectrum backend")
            return
        try:
            snapshot = method(dict(settings or {}))
            invalidate = getattr(self._adapter, "invalidate_wavelengths", None)
            if callable(invalidate):
                invalidate()
            self._identity.update(
                {
                    "spectrograph_serial": snapshot.get("spectrograph_serial", ""),
                    "output_port": snapshot.get("output_port"),
                    "grating": snapshot.get("grating"),
                    "wavelength_nm": snapshot.get("wavelength_nm"),
                    "connection_warnings": self._identity.get("connection_warnings", []),
                }
            )
            self.andor_controls_applied.emit(snapshot)
            self.andor_status_ready.emit(snapshot)
        except Exception as exc:
            self.error.emit(f"Andor apply-and-verify failed: {exc}")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _get_wavelengths(self) -> np.ndarray:
        if self._adapter is not None:
            return self._adapter.calibration_wavelengths(force=False)
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
    acquisition_settings_readback = Signal(object)
    spectrograph_status_ready = Signal(object)
    wavelengths_updated = Signal(object)
    state_changed       = Signal(object)
    temperature_ready   = Signal(object)
    temperature_snapshot_ready = Signal(object)
    temperature_monitor_state = Signal(str)
    cooler_changed      = Signal(bool)
    andor_status_ready  = Signal(object)
    andor_controls_applied = Signal(object)
    shamrock_connection_changed = Signal(bool)
    backend_temperature_snapshot = Signal(object)
    switching_lock_changed = Signal(bool)

    _connect_requested = Signal(bool, str)
    _disconnect_requested = Signal()
    _apply_requested = Signal(float, float, int)
    _acquire_requested = Signal()
    _acquire_2d_requested = Signal()
    _temperature_requested = Signal()
    _temperature_snapshot_requested = Signal(int)
    _scan_prepare_requested = Signal(object)
    _cooler_requested = Signal(bool)
    _andor_status_requested = Signal()
    _andor_controls_requested = Signal(object)
    _shamrock_connection_requested = Signal(bool)
    _lightfield_optics_requested = Signal(object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)

        self._thread = QThread(self)
        self._worker = _LF6Worker()
        self._worker.moveToThread(self._thread)
        self._temperature_pending = False
        self._temperature_generation = 0
        self._connection_pending = False
        self._settings_pending = False
        self._acquisition_requests = 0
        self._temperature_pause_sources: set[str] = set()
        self._last_temperature_monitor_state = None
        self._temperature_timer = QTimer(self)
        self._temperature_timer.setInterval(2000)
        self._temperature_timer.timeout.connect(self.poll_temperature)
        self._temperature_timer.start()

        # wire worker → controller signals (queued across thread boundary)
        self._worker.connected.connect(self._on_backend_connected)
        self._worker.disconnected.connect(self.disconnected)
        self._worker.error.connect(self.error)
        self._worker.error.connect(self._clear_pending_operations)
        self._worker.settings_applied.connect(self._clear_pending_operations)
        self._worker.spectrum_ready.connect(self.spectrum_ready)
        self._worker.frame_ready.connect(self.frame_ready)
        self._worker.settings_applied.connect(self.settings_applied)
        self._worker.acquisition_settings_readback.connect(self.acquisition_settings_readback)
        self._worker.spectrograph_status_ready.connect(self._clear_pending_operations)
        self._worker.spectrograph_status_ready.connect(self.spectrograph_status_ready)
        self._worker.wavelengths_updated.connect(self.wavelengths_updated)
        self._worker.state_changed.connect(self.state_changed)
        self._worker.temperature_ready.connect(self.temperature_ready)
        self._worker.temperature_snapshot_ready.connect(self._on_temperature_snapshot)
        self._worker.acquisition_finished.connect(self._on_acquisition_finished)
        self._worker.cooler_changed.connect(self.cooler_changed)
        self._worker.andor_status_ready.connect(self.andor_status_ready)
        self._worker.andor_controls_applied.connect(self.andor_controls_applied)
        self._worker.andor_controls_applied.connect(self._clear_pending_operations)
        self._worker.shamrock_connection_changed.connect(self.shamrock_connection_changed)
        self._worker.backend_temperature_snapshot.connect(self.backend_temperature_snapshot)

        self._connect_requested.connect(self._worker.connect_instrument)
        self._disconnect_requested.connect(self._worker.disconnect_instrument)
        self._apply_requested.connect(self._worker.apply_settings)
        self._acquire_requested.connect(self._worker.acquire_single)
        self._acquire_2d_requested.connect(self._worker.acquire_2d)
        self._temperature_requested.connect(self._worker.read_temperature)
        self._temperature_snapshot_requested.connect(self._worker.read_temperature_snapshot)
        self._scan_prepare_requested.connect(self._worker.prepare_scan_condition)
        self._cooler_requested.connect(self._worker.set_cooler)
        self._andor_status_requested.connect(self._worker.refresh_andor_status)
        self._andor_controls_requested.connect(self._worker.apply_andor_controls)
        self._shamrock_connection_requested.connect(self._worker.set_shamrock_connected)
        self._lightfield_optics_requested.connect(self._worker.lightfield_optics)

        self._thread.start()

    # ── public API (called from main thread) ──────────────────────────────────

    @Slot()
    def _clear_pending_operations(self, *args):
        self._connection_pending = self._settings_pending = False
        self.switching_lock_changed.emit(self.switching_locked)

    @Slot(list)
    def _on_backend_connected(self, experiments):
        self._connection_pending = False
        cfg.lf6.backend = self.backend
        self.connected.emit(experiments)
        self.switching_lock_changed.emit(self.switching_locked)

    def connect_instrument(
        self, use_mock: bool = False, backend: Optional[str] = None
    ) -> None:
        """Connect the selected LightField or Andor spectrum backend."""
        if self.switching_locked:
            self.error.emit("Cannot switch spectrum devices during a measurement or connection change")
            return
        selected = str(backend or cfg.lf6.backend or "lightfield")
        cfg.lf6.backend = selected
        self._temperature_generation += 1
        self._temperature_timer.start()
        self.set_temperature_monitor_paused("disconnect", False)
        self._connection_pending = True
        self.switching_lock_changed.emit(True)
        self._connect_requested.emit(bool(use_mock), selected)

    def lightfield_optics(self, requested=None):
        if self.switching_locked:
            self.error.emit("Cannot change optics during a measurement or connection change")
            return
        self._settings_pending = True
        self.switching_lock_changed.emit(True)
        self._lightfield_optics_requested.emit(requested)

    def set_shamrock_connected(self, connected: bool) -> None:
        if self.switching_locked:
            self.error.emit("Cannot change Shamrock connection during a measurement or connection change")
            return
        self._shamrock_connection_requested.emit(bool(connected))

    def disconnect_instrument(self) -> None:
        if self.switching_locked:
            self.error.emit("Cannot disconnect during a measurement or connection change")
            return
        self._temperature_generation += 1
        self._disconnect_requested.emit()

    def apply_settings(
        self,
        exposure_ms: float,
        center_nm: float,
        accumulations: int,
    ) -> None:
        if self.spectrum_actions_blocked:
            self.error.emit(_SPECTRUM_OWNERSHIP_ERROR)
            return
        if self._connection_pending:
            self.error.emit("Wait for the spectrum device connection change to finish")
            return
        self._settings_pending = True
        self.switching_lock_changed.emit(True)
        self._apply_requested.emit(
            float(exposure_ms), float(center_nm), int(accumulations)
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
        if self.backend == "winspec_ingaas":
            return self.adapter.configure_for_acquisition(
                center_nm=center_nm, exposure_ms=exposure_ms, frames=frames)
        return self._worker.configure_for_acquisition.__func__(
            self._worker, center_nm=center_nm, exposure_ms=exposure_ms, frames=frames
        )

    prepare_acquisition = configure_for_acquisition

    def prepare_scan_condition(self, backend, center_nm, exposure_ms, frames, reduction, stop_event):
        if not self._temperature_pause_sources.intersection({'megasweep', 'presets'}):
            raise RuntimeError('Setup selection requires 2D or Dual Gate measurement ownership')
        if reduction not in {'average', 'device'}:
            raise ValueError('Unknown scan combination mode')
        request = dict(backend=backend, center=float(center_nm), exposure=float(exposure_ms),
                       frames=int(frames), reduction=reduction, stop=stop_event, done=threading.Event())
        self._scan_prepare_requested.emit(request)
        # Do not abandon a queued hardware operation: its setters have their own
        # bounded waits. The worker checks Stop before starting the operation.
        request['done'].wait()
        if 'error' in request:
            raise request['error']
        if stop_event.is_set():
            raise RuntimeError('Optical sequence stopped')
        return request['result']

    def validate_scan_centers(self, centers):
        """Check WinSpec calibration coverage before a scan commands the SMUs."""
        if self.backend == 'winspec_ingaas':
            self.adapter.validate_scan_centers(centers)

    def acquire_single(self) -> None:
        if self.spectrum_actions_blocked:
            self.error.emit(_SPECTRUM_OWNERSHIP_ERROR)
            return
        self._acquisition_requests += 1
        self.set_temperature_monitor_paused("acquisition", True)
        self._acquire_requested.emit()

    def acquire_2d(self) -> None:
        if self.spectrum_actions_blocked:
            self.error.emit(_SPECTRUM_OWNERSHIP_ERROR)
            return
        self._acquisition_requests += 1
        self.set_temperature_monitor_paused("acquisition", True)
        self._acquire_2d_requested.emit()

    @Slot()
    def _on_acquisition_finished(self) -> None:
        self._acquisition_requests = max(0, self._acquisition_requests - 1)
        self.set_temperature_monitor_paused("acquisition", self._acquisition_requests > 0)

    def read_temperature(self) -> None:
        if self._worker.manual_temperature_paused.is_set() or self.is_busy:
            return
        self._temperature_requested.emit()

    @property
    def temperature_monitor_paused(self) -> bool:
        return bool(self._temperature_pause_sources) or self.is_busy

    def _emit_temperature_monitor_state(self) -> None:
        state = ('Paused during measurement' if self.temperature_monitor_paused else
                 'Auto refresh: 10 s' if self.backend == 'winspec_ingaas' else 'Auto refresh: 2 s')
        if state != self._last_temperature_monitor_state:
            self._last_temperature_monitor_state = state
            self.temperature_monitor_state.emit(state)

    def set_temperature_monitor_paused(self, source: str, paused: bool) -> None:
        was_paused = bool(self._temperature_pause_sources)
        if paused:
            self._temperature_pause_sources.add(source)
        else:
            self._temperature_pause_sources.discard(source)
        if self._temperature_pause_sources - {'spectrum', 'acquisition', 'disconnect'}:
            self._worker.external_measurement_active.set()
        else:
            self._worker.external_measurement_active.clear()
        if self._temperature_pause_sources:
            self._worker.temperature_monitor_paused.set()
            if not was_paused:
                # A pre-measurement read must not appear fresh after resuming.
                self._temperature_generation += 1
        else:
            self._worker.temperature_monitor_paused.clear()
        # Warm-up needs explicit readings to determine when disconnect is safe.
        if self._temperature_pause_sources - {'warmup', 'disconnect'}:
            self._worker.manual_temperature_paused.set()
        else:
            self._worker.manual_temperature_paused.clear()
        self._emit_temperature_monitor_state()
        if not self._temperature_pause_sources and self._worker._scan_selected:
            self._worker._scan_adapter = None
            self._worker._scan_selected = False
            self._worker.connected.emit(self._worker._experiments)
        self.switching_lock_changed.emit(self.switching_locked)

    @Slot()
    def poll_temperature(self) -> None:
        active_detector = self.is_connected and self.identity.get("backend") in {"andor_sdk2", "winspec_ingaas"}
        parked_andor = any(key != self.backend for key in self._worker.andor_setups())
        if not active_detector and not parked_andor:
            return
        self._emit_temperature_monitor_state()
        if self.temperature_monitor_paused:
            return
        if self._temperature_pending:
            return
        self._temperature_pending = True
        self._temperature_snapshot_requested.emit(self._temperature_generation)

    @Slot(object)
    def _on_temperature_snapshot(self, result) -> None:
        generation, snapshot = result
        self._temperature_pending = False
        if (generation == self._temperature_generation and snapshot is not None
                and self.is_connected and not self._temperature_pause_sources):
            self.temperature_snapshot_ready.emit(snapshot)

    def set_cooler(self, on: bool) -> None:
        self._cooler_requested.emit(bool(on))

    def refresh_andor_status(self) -> None:
        self._andor_status_requested.emit()

    def apply_andor_controls(self, settings: dict) -> None:
        if self.switching_locked:
            self.error.emit("Cannot apply Andor controls during a measurement or connection change")
            return
        self._settings_pending = True
        self.switching_lock_changed.emit(True)
        self._andor_controls_requested.emit(dict(settings or {}))

    def abort_acquisition(self) -> bool:
        adapter = self._worker.adapter
        method = getattr(adapter, "abort_acquisition", None)
        return bool(method()) if callable(method) else False

    def andor_disconnect_safety_snapshot(self) -> Optional[dict]:
        for setup in self._worker.andor_setups().values():
            method = getattr(setup, "get_disconnect_safety_snapshot", None)
            if callable(method):
                return dict(method())
        return None

    @property
    def connected_backends(self):
        return self._worker.connected_backends

    @property
    def spectrum_actions_blocked(self):
        """Other measurement owners block Spectrum, including queued requests."""
        return self._worker.external_measurement_active.is_set()

    @property
    def switching_locked(self):
        return (self._connection_pending or self._settings_pending or self.is_busy or self._acquisition_requests > 0
                or bool(self._temperature_pause_sources - {"disconnect"})
                or self.state in {LightFieldLifecycleState.STARTING, LightFieldLifecycleState.INITIALIZING})

    # ── state accessors (read from main thread — be aware of races) ───────────

    @property
    def is_connected(self) -> bool:
        return (not self._connection_pending and self._worker.adapter is not None
                and self.state is LightFieldLifecycleState.READY)

    @property
    def state(self):
        return self._worker.state

    @property
    def backend(self) -> str:
        return self._worker.backend

    @property
    def identity(self) -> dict:
        return self._worker.identity

    @property
    def is_ready(self) -> bool:
        return self.is_connected and bool(self._worker.is_ready)

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
        if self._worker._scan_adapter is not None:
            return self._worker._scan_adapter
        if self.backend == 'winspec_ingaas' and self._worker.adapter is not None:
            from app.devices.winspec_scan_adapter import WinSpecScanAdapter
            cached = getattr(self, '_winspec_scan_adapter', None)
            if cached is None or cached.setup is not self._worker.setup:
                cached = self._winspec_scan_adapter = WinSpecScanAdapter(self._worker.setup)
            return cached
        return self._worker.adapter

    @property
    def setup(self):
        """Raw LF6Setup / MockLF6Setup; None if not connected."""
        return self._worker.setup

    # ── cleanup ───────────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Call from main.py on application exit."""
        self._temperature_timer.stop()
        self._worker.temperature_monitor_paused.set()
        self._worker.manual_temperature_paused.set()
        if self._thread.isRunning():
            QMetaObject.invokeMethod(
                self._worker,
                "disconnect_all",
                Qt.ConnectionType.BlockingQueuedConnection,
            )
        self._thread.quit()
        self._thread.wait(3000)
