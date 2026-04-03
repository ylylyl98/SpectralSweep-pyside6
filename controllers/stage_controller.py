# controllers/stage_controller.py
# ──────────────────────────────────────────────────────────────────────────────
# Qt controller for the Thorlabs Elliptec linear stage.
#
# Wraps app.devices.stage_adapter.LinearStage.
#
# Signals:
#   connected()
#   disconnected()
#   error(str)
#   position_ready(float)   readback result (mm or stage units)
#   move_done(float)        after move_to completes
#
# Public methods:
#   connect_instrument(com_port)
#   disconnect_instrument()
#   move_to(position)
#   get_position()
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

    connected      = Signal()
    disconnected   = Signal()
    error          = Signal(str)
    position_ready = Signal(float)
    move_done      = Signal(float)

    def __init__(self) -> None:
        super().__init__()
        self._adapter = None   # app.devices.stage_adapter.LinearStage

    @Slot(str)
    def connect_instrument(self, com_port: str) -> None:
        self._close()
        try:
            from app.devices.stage_adapter import LinearStage
            self._adapter = LinearStage(com_port)
            self.connected.emit()
        except Exception as exc:
            self.error.emit(
                f"Stage connect failed: {exc}\n{traceback.format_exc()}"
            )

    @Slot()
    def disconnect_instrument(self) -> None:
        self._close()
        self.disconnected.emit()

    def _close(self) -> None:
        if self._adapter is not None:
            try:
                self._adapter.close()
            except Exception:
                pass
            self._adapter = None

    @Slot(float)
    def move_to(self, position: float) -> None:
        if self._adapter is None:
            self.error.emit("Stage not connected.")
            return
        try:
            self._adapter.move_to(float(position))
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
            self.position_ready.emit(pos)
        except Exception as exc:
            self.error.emit(f"Stage get_position failed: {exc}")

    @property
    def adapter(self):
        return self._adapter


class StageController(QObject):
    """
    Controls a single Thorlabs Elliptec linear stage.

    Usage:
        ctrl = StageController()
        ctrl.connected.connect(panel.on_stage_connected)
        ctrl.connect_instrument("COM5")
        ctrl.move_to(10.5)
    """

    connected      = Signal()
    disconnected   = Signal()
    error          = Signal(str)
    position_ready = Signal(float)
    move_done      = Signal(float)

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

        self._thread.start()

    def connect_instrument(self, com_port: str) -> None:
        self._worker.connect_instrument.__func__(self._worker, com_port)

    def disconnect_instrument(self) -> None:
        self._worker.disconnect_instrument.__func__(self._worker)

    def move_to(self, position: float) -> None:
        self._worker.move_to.__func__(self._worker, float(position))

    def get_position(self) -> None:
        self._worker.get_position.__func__(self._worker)

    @property
    def is_connected(self) -> bool:
        return self._worker.adapter is not None

    @property
    def adapter(self):
        return self._worker.adapter

    def shutdown(self) -> None:
        self._worker.disconnect_instrument.__func__(self._worker)
        self._thread.quit()
        self._thread.wait(3000)
