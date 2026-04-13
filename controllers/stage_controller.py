# controllers/stage_controller.py
# ──────────────────────────────────────────────────────────────────────────────
# Qt controller for the linear stage.
#
# Supports Thorlabs Elliptec and Newport ESP300 backends.
#
# Signals:
#   connected()
#   disconnected()
#   error(str)
#   position_ready(float)   readback result (mm or stage units)
#   move_done(float)        after move_to completes
#
# Public methods:
#   connect_elliptec(com_port)
#   connect_esp300(visa_resource, axis=3)
#   disconnect_instrument()
#   move_to(position)
#   get_position()
#   home()
#   scan_axes(visa_resource)
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


class _StageWorker(QObject):

    connected      = Signal(str)
    disconnected   = Signal()
    error          = Signal(str)
    position_ready = Signal(float)
    move_done      = Signal(float)
    homed          = Signal(float)
    axes_scanned   = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self._adapter = None
        self._backend_key = ""

    @Slot(str)
    def connect_elliptec(self, com_port: str) -> None:
        self._close()
        try:
            from app.devices.stage_adapter import create_linear_stage

            self._adapter = create_linear_stage("elliptec", com_port)
            self._backend_key = "elliptec"
            print(f"[LinearStage] Connected {self._adapter.display_name} on {com_port}")
            self.connected.emit("elliptec")
        except Exception as exc:
            self.error.emit(
                f"Stage connect failed (Elliptec): {exc}\n{traceback.format_exc()}"
            )

    @Slot(str, int)
    def connect_esp300(self, visa_resource: str, axis: int) -> None:
        self._close()
        try:
            from app.devices.stage_adapter import create_linear_stage

            self._adapter = create_linear_stage("esp300", visa_resource, axis=axis)
            self._backend_key = "esp300"
            print(
                f"[LinearStage] Connected {self._adapter.display_name} "
                f"on {visa_resource} axis {self._adapter.axis}"
            )
            self.connected.emit("esp300")
        except Exception as exc:
            self.error.emit(
                f"Stage connect failed (ESP300): {exc}\n{traceback.format_exc()}"
            )

    @Slot()
    def disconnect_instrument(self) -> None:
        self._close()
        self.disconnected.emit()

    def _close(self) -> None:
        if self._adapter is not None:
            try:
                print(
                    f"[LinearStage] Closing {self._adapter.display_name} "
                    f"at {self._adapter.address}"
                    + (
                        f" axis {self._adapter.axis}"
                        if getattr(self._adapter, "axis", None) is not None
                        else ""
                    )
                )
                self._adapter.close()
            except Exception:
                pass
            self._adapter = None
            self._backend_key = ""

    @Slot(float)
    def move_to(self, position: float) -> None:
        if self._adapter is None:
            self.error.emit("Stage not connected.")
            return
        try:
            target = float(position)
            print(
                f"[LinearStage] {self._adapter.display_name} move_to {target:g} "
                f"({self._adapter.minimum_position:g} to {self._adapter.maximum_position:g} "
                f"{self._adapter.position_unit})"
            )
            self._adapter.move_to(target)
            pos = float(self._adapter.get_position())
            self.move_done.emit(pos)
        except Exception as exc:
            self.error.emit(f"Stage move_to failed: {exc}")

    @Slot()
    def get_position(self) -> None:
        if self._adapter is None:
            self.error.emit("Stage not connected.")
            return
        try:
            pos = float(self._adapter.get_position())
            print(f"[LinearStage] {self._adapter.display_name} readback -> {pos:g}")
            self.position_ready.emit(pos)
        except Exception as exc:
            self.error.emit(f"Stage get_position failed: {exc}")

    @Slot()
    def home(self) -> None:
        if self._adapter is None:
            self.error.emit("Stage not connected.")
            return
        try:
            print(f"[LinearStage] {self._adapter.display_name} home")
            self._adapter.home()
            pos = float(self._adapter.get_position())
            self.homed.emit(pos)
        except Exception as exc:
            self.error.emit(f"Stage home failed: {exc}")

    @Slot(str)
    def scan_axes(self, visa_resource: str) -> None:
        try:
            from app.devices.esp300_shared import scan_shared_esp300_axes

            axes = scan_shared_esp300_axes(visa_resource, default_axis=3) or [3]
            print(f"[LinearStage] ESP300 axis scan on {visa_resource} -> {axes}")
            self.axes_scanned.emit(axes)
        except Exception as exc:
            self.error.emit(f"Stage axis scan failed: {exc}")
            self.axes_scanned.emit([3])

    @property
    def adapter(self):
        return self._adapter


class StageController(QObject):
    """
    Controls a single linear stage backend.

    Usage:
        ctrl = StageController()
        ctrl.connected.connect(panel.on_stage_connected)
        ctrl.connect_elliptec("COM5")
        ctrl.connect_esp300("ASRL3::INSTR", axis=3)
        ctrl.move_to(10.5)
    """

    connected      = Signal(str)
    disconnected   = Signal()
    error          = Signal(str)
    position_ready = Signal(float)
    move_done      = Signal(float)
    homed          = Signal(float)
    axes_scanned   = Signal(list)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)

        self._thread = QThread(self)
        self._worker = _StageWorker()
        self._worker.moveToThread(self._thread)

        self._worker.connected.connect(self.connected)
        self._worker.disconnected.connect(self.disconnected)
        self._worker.error.connect(self.error)
        self._worker.position_ready.connect(self.position_ready)
        self._worker.move_done.connect(self.move_done)
        self._worker.homed.connect(self.homed)
        self._worker.axes_scanned.connect(self.axes_scanned)

        self._thread.start()

    def connect_elliptec(self, com_port: str) -> None:
        self._worker.connect_elliptec.__func__(self._worker, com_port)

    def connect_esp300(self, visa_resource: str, axis: int = 3) -> None:
        self._worker.connect_esp300.__func__(self._worker, visa_resource, int(axis))

    def connect_instrument(self, com_port: str) -> None:
        self.connect_elliptec(com_port)

    def disconnect_instrument(self) -> None:
        self._worker.disconnect_instrument.__func__(self._worker)

    def move_to(self, position: float) -> None:
        self._worker.move_to.__func__(self._worker, float(position))

    def get_position(self) -> None:
        self._worker.get_position.__func__(self._worker)

    def home(self) -> None:
        self._worker.home.__func__(self._worker)

    def scan_axes(self, visa_resource: str) -> None:
        self._worker.scan_axes.__func__(self._worker, visa_resource)

    @property
    def is_connected(self) -> bool:
        return self._worker.adapter is not None

    @property
    def adapter(self):
        return self._worker.adapter

    @property
    def backend_key(self) -> str:
        return getattr(self._worker, "_backend_key", "")

    def shutdown(self) -> None:
        self._worker.disconnect_instrument.__func__(self._worker)
        self._thread.quit()
        self._thread.wait(3000)
