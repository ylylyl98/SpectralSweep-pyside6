# controllers/rotation_controller.py
# ──────────────────────────────────────────────────────────────────────────────
# Qt controller for up to 2 independent rotation mounts.
#
# Supported adapter types (selected at connect time):
#   "elliptec"  → app.devices.rotation_thorlabs_elliptec_adapter.ElliptecRotation
#   "esp300"    → app.devices.rotation_eps300_adapter.NewportEPS300
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
#
# Public methods:
#   connect_elliptec(slot, com_port)
#   connect_esp300(slot, visa_resource, axis=1)
#   disconnect(slot)
#   move_to(slot, angle_deg)
#   get_position(slot)
#   scan_axes(slot, visa_resource)      ← temporary open, no persistent state
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot

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

    @Slot(str, str, int)
    def connect_esp300(self, slot: str, visa_resource: str, axis: int) -> None:
        self._close_slot(slot)
        try:
            import pyvisa
            from app.devices.rotation_eps300_adapter import NewportEPS300
            rm = pyvisa.ResourceManager()
            adapter = NewportEPS300(visa_resource, rm=rm, axis=axis)
            # Motor ON for the selected axis
            try:
                adapter.motor_on(axis=axis)
            except Exception:
                pass
            self._adapters[slot] = adapter
            self.connected.emit(slot, "esp300")
        except Exception as exc:
            self.error.emit(
                f"Rotation {slot} (ESP300) connect failed: {exc}\n"
                f"{traceback.format_exc()}"
            )

    # ── disconnect ────────────────────────────────────────────────────────────

    @Slot(str)
    def disconnect_slot(self, slot: str) -> None:
        self._close_slot(slot)
        self.disconnected.emit(slot)

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
            adapter.move_to(float(angle_deg))
            # Readback after move
            pos = float(adapter.get_position())
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

    # ── axis scan (ESP300 only, temporary open) ───────────────────────────────

    @Slot(str, str)
    def scan_axes(self, slot: str, visa_resource: str) -> None:
        """
        Open a temporary ESP300 connection, scan axes, close.
        Does NOT affect the persistent slot state.
        """
        try:
            import pyvisa
            from app.devices.rotation_eps300_adapter import NewportEPS300
            rm = pyvisa.ResourceManager()
            tmp = NewportEPS300(visa_resource, rm=rm, connect_only=True)
            axes = tmp.get_axes(conservative=True) or [1]
            try:
                tmp.close()
            except Exception:
                pass
            self.axes_scanned.emit(slot, axes)
        except Exception as exc:
            self.error.emit(f"Rotation {slot} axis scan failed: {exc}")
            self.axes_scanned.emit(slot, [1])   # fallback

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

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)

        self._thread = QThread(self)
        self._worker = _RotationWorker()
        self._worker.moveToThread(self._thread)

        self._worker.connected.connect(self.connected)
        self._worker.disconnected.connect(self.disconnected)
        self._worker.error.connect(self.error)
        self._worker.position_ready.connect(self.position_ready)
        self._worker.move_done.connect(self.move_done)
        self._worker.axes_scanned.connect(self.axes_scanned)

        self._thread.start()

    # ── public API ────────────────────────────────────────────────────────────

    def connect_elliptec(self, slot: str, com_port: str) -> None:
        self._worker.connect_elliptec.__func__(self._worker, slot, com_port)

    def connect_esp300(self, slot: str, visa_resource: str, axis: int = 1) -> None:
        self._worker.connect_esp300.__func__(self._worker, slot, visa_resource, axis)

    def disconnect(self, slot: str) -> None:
        self._worker.disconnect_slot.__func__(self._worker, slot)

    def move_to(self, slot: str, angle_deg: float) -> None:
        self._worker.move_to.__func__(self._worker, slot, float(angle_deg))

    def get_position(self, slot: str) -> None:
        self._worker.get_position.__func__(self._worker, slot)

    def scan_axes(self, slot: str, visa_resource: str) -> None:
        self._worker.scan_axes.__func__(self._worker, slot, visa_resource)

    # ── state ─────────────────────────────────────────────────────────────────

    def is_connected(self, slot: str) -> bool:
        return self._worker.is_connected(slot)

    def adapter(self, slot: str):
        """Raw adapter (ElliptecRotation or NewportEPS300); None if not connected."""
        return self._worker.adapter(slot)

    # ── cleanup ───────────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        for slot in ("rot1", "rot2"):
            if self._worker.is_connected(slot):
                self._worker.disconnect_slot.__func__(self._worker, slot)
        self._thread.quit()
        self._thread.wait(3000)
