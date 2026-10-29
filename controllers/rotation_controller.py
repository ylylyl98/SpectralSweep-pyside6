# controllers/rotation_controller.py
# ──────────────────────────────────────────────────────────────────────────────
# Qt controller for up to 2 independent rotation mounts.
#
# Supported adapter types (selected at connect time):
#   "elliptec"  → app.devices.rotation_thorlabs_elliptec_adapter.ElliptecRotation
#   "esp300" / "esp301" → shared Newport controller session
#
# Slot names: "rot1", "rot2"  (arbitrary strings, must be unique)
#
# Signals:
#   connected(slot, adapter_type)   after successful open
#   disconnected(slot)
#   error(str)
#   position_ready(slot, float)     readback result
#   move_done(slot, float)          after move_to completes
#   axes_scanned(slot, list)        ESP300 axis scan result
#   operation_finished(slot, op)    queued connect/scan/disconnect completed
#
# Public methods:
#   connect_elliptec(slot, com_port)
#   connect_esp300(slot, visa_resource, axis=1)
#   disconnect(slot)
#   move_to(slot, angle_deg)
#   get_position(slot)
#   scan_axes(slot, visa_resource, model="esp300", baud=None)
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from app.devices.motion_verification import move_and_verify

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ── Worker ────────────────────────────────────────────────────────────────────

class _RotationWorker(QObject):

    connected      = Signal(str, str)    # slot, adapter_type
    disconnected   = Signal(str)         # slot
    error          = Signal(str)
    position_ready = Signal(str, float)  # slot, angle_deg
    move_done      = Signal(str, float)  # slot, angle_deg
    axes_scanned   = Signal(str, list)   # slot, [int]
    motion_profile_ready = Signal(str, object)  # slot, ESP300MotionProfile
    settings_saved = Signal(str, str)    # slot, resource
    operation_finished = Signal(str, str)  # slot, scan/connect/disconnect

    def __init__(self) -> None:
        super().__init__()
        # {slot: adapter_instance}
        self._adapters: Dict[str, object] = {}

    # ── connect ───────────────────────────────────────────────────────────────

    @Slot(str, str)
    def connect_elliptec(self, slot: str, com_port: str) -> None:
        self._close_slot(slot)
        try:
            from app.devices.rotation_thorlabs_elliptec_adapter import ElliptecRotation
            adapter = ElliptecRotation(com_port)
            self._adapters[slot] = adapter
            self.connected.emit(slot, "elliptec")
        except Exception as exc:
            self.error.emit(
                f"Rotation {slot} (Elliptec) connect failed: {exc}\n"
                f"{traceback.format_exc()}"
            )
        finally:
            self.operation_finished.emit(slot, "connect")

    @Slot(str, str, int, float, float, str, object)
    def connect_esp300(
        self,
        slot: str,
        visa_resource: str,
        axis: int,
        velocity_fraction: float = 1.0,
        acceleration_fraction: float = 0.5,
        model: str = "esp300",
        baud: int | None = None,
    ) -> None:
        try:
            from app.devices.rotation_esp300_shared_adapter import SharedESP300Rotation

            model = str(model).strip().lower()
            if model not in ("esp300", "esp301"):
                raise ValueError(f"Unsupported Newport model: {model}")
            # Check before closing the current mount or opening/enabling another
            # adapter. The same physical axis cannot serve both rotation roles.
            for other_slot, other in self._adapters.items():
                if other_slot == slot:
                    continue
                if (str(getattr(other, "address", "")).strip().upper()
                        == str(visa_resource).strip().upper()
                        and getattr(other, "axis", None) == int(axis)):
                    raise ValueError(
                        f"{visa_resource} axis {axis} is already assigned to {other_slot}"
                    )
            self._close_slot(slot)
            adapter = SharedESP300Rotation(
                visa_resource,
                axis=axis,
                role=slot,
                velocity_fraction=velocity_fraction,
                acceleration_fraction=acceleration_fraction,
                model=model,
                baud=baud,
            )
            self._adapters[slot] = adapter
            print(f"[Rotation] {slot} mapped to shared {model.upper()} {visa_resource} axis {axis}")
            self.connected.emit(slot, model)
            self.motion_profile_ready.emit(slot, adapter.motion_profile)
        except Exception as exc:
            self.error.emit(
                f"Rotation {slot} ({model.upper()}) connect failed: {exc}\n"
                f"{traceback.format_exc()}"
            )
        finally:
            self.operation_finished.emit(slot, "connect")

    # ── disconnect ────────────────────────────────────────────────────────────

    @Slot(str)
    def disconnect_slot(self, slot: str) -> None:
        self._close_slot(slot)
        self.disconnected.emit(slot)
        self.operation_finished.emit(slot, "disconnect")

    @Slot()
    def shutdown(self) -> None:
        # Queued after pending connections and scans, so no adapter can appear
        # after cleanup and no VISA call outlives its worker thread.
        for slot in list(self._adapters):
            self.disconnect_slot(slot)
        self.thread().quit()

    def _close_slot(self, slot: str) -> None:
        adapter = self._adapters.pop(slot, None)
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                pass

    # ── motion ────────────────────────────────────────────────────────────────

    @Slot(str, float)
    def move_to(self, slot: str, angle_deg: float) -> None:
        adapter = self._adapters.get(slot)
        if adapter is None:
            self.error.emit(f"Rotation {slot} not connected.")
            return
        try:
            pos = move_and_verify(adapter, float(angle_deg))
            self.move_done.emit(slot, pos)
        except Exception as exc:
            self.error.emit(f"Rotation {slot} move_to failed: {exc}")

    @Slot(str)
    def get_position(self, slot: str) -> None:
        adapter = self._adapters.get(slot)
        if adapter is None:
            self.error.emit(f"Rotation {slot} not connected.")
            return
        try:
            pos = float(adapter.get_position())
            self.position_ready.emit(slot, pos)
        except Exception as exc:
            self.error.emit(f"Rotation {slot} get_position failed: {exc}")

    @Slot(str)
    def inspect_motion_profile(self, slot: str) -> None:
        adapter = self._adapters.get(slot)
        if adapter is None or not hasattr(adapter, "refresh_motion_profile"):
            self.error.emit(f"Rotation {slot} is not connected to an ESP300.")
            return
        try:
            self.motion_profile_ready.emit(slot, adapter.refresh_motion_profile())
        except Exception as exc:
            self.error.emit(f"Rotation {slot} profile query failed: {exc}")

    @Slot(str, float, float)
    def apply_motion_profile(
        self, slot: str, velocity_fraction: float, acceleration_fraction: float
    ) -> None:
        adapter = self._adapters.get(slot)
        if adapter is None or not hasattr(adapter, "apply_fast_safe_motion_profile"):
            self.error.emit(f"Rotation {slot} is not connected to an ESP300.")
            return
        try:
            profile = adapter.apply_fast_safe_motion_profile(
                velocity_fraction=float(velocity_fraction),
                acceleration_fraction=float(acceleration_fraction),
            )
            self.motion_profile_ready.emit(slot, profile)
        except Exception as exc:
            self.error.emit(f"Rotation {slot} profile apply failed: {exc}")

    @Slot(str)
    def save_esp300_settings(self, slot: str) -> None:
        adapter = self._adapters.get(slot)
        if adapter is None or not hasattr(adapter, "save_settings"):
            self.error.emit(f"Rotation {slot} is not connected to an ESP300.")
            return
        try:
            adapter.refresh_motion_profile()
            adapter.save_settings()
            self.settings_saved.emit(slot, str(getattr(adapter, "address", "")))
        except Exception as exc:
            self.error.emit(f"Rotation {slot} settings save failed: {exc}")

    # ── axis scan (Newport, temporary open) ───────────────────────────────────

    @Slot(str, str, str, object)
    def scan_axes(
        self, slot: str, visa_resource: str,
        model: str = "esp300", baud: int | None = None,
    ) -> None:
        """
        Borrow a Newport connection, scan axes, release.
        Does NOT affect the persistent slot state.
        """
        try:
            from app.devices.esp300_shared import scan_shared_esp300_axes

            axes = scan_shared_esp300_axes(visa_resource, model=model, baud=baud)
            self.axes_scanned.emit(slot, axes)
        except Exception as exc:
            self.error.emit(f"Rotation {slot} axis scan failed: {exc}")
            self.axes_scanned.emit(slot, [])
        finally:
            self.operation_finished.emit(slot, "scan")

    # ── accessors ─────────────────────────────────────────────────────────────

    def is_connected(self, slot: str) -> bool:
        return slot in self._adapters

    def adapter(self, slot: str):
        return self._adapters.get(slot)


# ── Public controller ─────────────────────────────────────────────────────────

class RotationController(QObject):
    """
    Manages up to 2 rotation mounts (rot1, rot2).
    Each mount is independently connected/disconnected.

    Usage:
        ctrl = RotationController()
        ctrl.connected.connect(panel.on_rotation_connected)
        ctrl.connect_elliptec("rot1", "COM4")
        ctrl.connect_esp300("rot2", "ASRL6::INSTR", axis=1)
        ctrl.move_to("rot1", 45.0)
    """

    connected      = Signal(str, str)
    disconnected   = Signal(str)
    error          = Signal(str)
    position_ready = Signal(str, float)
    move_done      = Signal(str, float)
    axes_scanned   = Signal(str, list)
    motion_profile_ready = Signal(str, object)
    settings_saved = Signal(str, str)
    operation_finished = Signal(str, str)
    _connect_elliptec_requested = Signal(str, str)
    _connect_esp300_requested = Signal(str, str, int, float, float, str, object)
    _scan_requested = Signal(str, str, str, object)
    _disconnect_requested = Signal(str)
    _shutdown_requested = Signal()
    ROTATION_SLOTS = ("rot1", "rot2")
    # Compatibility spelling retained for older panels.
    LOGICAL_SLOTS = ROTATION_SLOTS

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._shutting_down = False

        self._thread = QThread(self)
        self._worker = _RotationWorker()
        self._worker.moveToThread(self._thread)

        self._worker.connected.connect(self.connected)
        self._worker.disconnected.connect(self.disconnected)
        self._worker.error.connect(self.error)
        self._worker.position_ready.connect(self.position_ready)
        self._worker.move_done.connect(self.move_done)
        self._worker.axes_scanned.connect(self.axes_scanned)
        self._worker.motion_profile_ready.connect(self.motion_profile_ready)
        self._worker.settings_saved.connect(self.settings_saved)
        self._worker.operation_finished.connect(self.operation_finished)
        self._connect_elliptec_requested.connect(
            self._worker.connect_elliptec, Qt.ConnectionType.QueuedConnection
        )
        self._connect_esp300_requested.connect(
            self._worker.connect_esp300, Qt.ConnectionType.QueuedConnection
        )
        self._scan_requested.connect(self._worker.scan_axes, Qt.ConnectionType.QueuedConnection)
        self._disconnect_requested.connect(
            self._worker.disconnect_slot, Qt.ConnectionType.QueuedConnection
        )
        self._shutdown_requested.connect(self._worker.shutdown, Qt.ConnectionType.QueuedConnection)
        self._thread.finished.connect(self._worker.deleteLater)

        self._thread.start()

    def logical_slots(self) -> tuple[str, ...]:
        """Stable logical mount names shared by all rotation UIs."""
        return tuple(self.ROTATION_SLOTS)

    def enumerate_logical_slots(self) -> tuple[str, ...]:
        """Compatibility method for panels that enumerate controller slots."""
        return self.logical_slots()

    def get_logical_slots(self) -> tuple[str, ...]:
        return self.logical_slots()

    # ── public API ────────────────────────────────────────────────────────────

    def connect_elliptec(self, slot: str, com_port: str) -> None:
        if not self._shutting_down:
            self._connect_elliptec_requested.emit(slot, com_port)

    def connect_esp300(
        self,
        slot: str,
        visa_resource: str,
        axis: int = 1,
        velocity_fraction: float = 1.0,
        acceleration_fraction: float = 0.5,
        *,
        model: str = "esp300",
        baud: int | None = None,
    ) -> None:
        if not self._shutting_down:
            self._connect_esp300_requested.emit(
                slot, visa_resource, axis, velocity_fraction, acceleration_fraction, model, baud
            )

    def disconnect(self, slot: str) -> None:
        if not self._shutting_down:
            self._disconnect_requested.emit(slot)

    def move_to(self, slot: str, angle_deg: float) -> None:
        self._worker.move_to.__func__(self._worker, slot, float(angle_deg))

    def get_position(self, slot: str) -> None:
        self._worker.get_position.__func__(self._worker, slot)

    def inspect_motion_profile(self, slot: str) -> None:
        self._worker.inspect_motion_profile.__func__(self._worker, slot)

    def apply_motion_profile(
        self, slot: str, velocity_fraction: float, acceleration_fraction: float
    ) -> None:
        self._worker.apply_motion_profile.__func__(
            self._worker, slot, velocity_fraction, acceleration_fraction
        )

    def save_esp300_settings(self, slot: str) -> None:
        self._worker.save_esp300_settings.__func__(self._worker, slot)

    def scan_axes(
        self, slot: str, visa_resource: str, *,
        model: str = "esp300", baud: int | None = None,
    ) -> None:
        if not self._shutting_down:
            self._scan_requested.emit(slot, visa_resource, model, baud)

    # ── state ─────────────────────────────────────────────────────────────────

    def is_connected(self, slot: str) -> bool:
        return self._worker.is_connected(slot)

    def adapter(self, slot: str):
        """Raw Elliptec/shared Newport adapter; None if not connected."""
        return self._worker.adapter(slot)

    # ── cleanup ───────────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        if not self._shutting_down:
            self._shutting_down = True
            self._shutdown_requested.emit()
        # Hardware operations have bounded I/O timeouts. A fixed short wait
        # could destroy a running QThread while a slow scan is still finishing.
        if QThread.currentThread() != self._thread:
            self._thread.wait()
