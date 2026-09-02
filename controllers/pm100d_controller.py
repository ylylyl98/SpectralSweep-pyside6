# controllers/pm100d_controller.py
# ──────────────────────────────────────────────────────────────────────────────
# Qt controller for the Thorlabs PM100D optical power meter.
#
# Wraps app.devices.pm100d_adapter.ThorlabsPM100D_Wrapper.
# Device enumeration (scan_devices) uses PyVISA directly so it can run before
# connecting.
#
# Signals:
#   connected()
#   disconnected()
#   error(str)
#   power_ready(float)        measured power in Watts
#   devices_scanned(list)     list[str] of resource names from VISA scan
#
# Public methods:
#   scan_devices()            → devices_scanned signal
#   connect_instrument(resource_name)
#   disconnect_instrument()
#   read_power()              → power_ready signal
#   set_wavelength(nm)
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


class _PM100DWorker(QObject):

    connected      = Signal()
    disconnected   = Signal()
    error          = Signal(str)
    power_ready    = Signal(float)
    devices_scanned = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self._adapter = None   # ThorlabsPM100D_Wrapper

    # ── device scan (no connection required) ─────────────────────────────────

    @Slot()
    def scan_devices(self) -> None:
        try:
            from app.devices.pm100d_adapter import scan_pm100d_resources

            found = scan_pm100d_resources()
            self.devices_scanned.emit(found if found else [])
        except ImportError:
            self.error.emit("PyVISA is required to discover the PM100D.")
            self.devices_scanned.emit([])
        except Exception as exc:
            self.error.emit(f"PM100D scan failed: {exc}")
            self.devices_scanned.emit([])

    # ── connect ───────────────────────────────────────────────────────────────

    @Slot(str)
    def connect_instrument(self, resource_name: str) -> None:
        self._close()
        try:
            from app.devices.pm100d_adapter import ThorlabsPM100D_Wrapper
            self._adapter = ThorlabsPM100D_Wrapper(resource_name)
            self.connected.emit()
        except Exception as exc:
            self.error.emit(
                f"PM100D connect failed: {exc}\n{traceback.format_exc()}"
            )

    # ── disconnect ────────────────────────────────────────────────────────────

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

    # ── readback ─────────────────────────────────────────────────────────────

    @Slot()
    def read_power(self) -> None:
        if self._adapter is None:
            self.error.emit("PM100D not connected.")
            return
        try:
            pwr = float(self._adapter.get_power())
            self.power_ready.emit(pwr)
        except Exception as exc:
            self.error.emit(f"PM100D read_power failed: {exc}")

    # ── settings ─────────────────────────────────────────────────────────────

    @Slot(float)
    def set_wavelength(self, nm: float) -> None:
        if self._adapter is None:
            self.error.emit("PM100D not connected.")
            return
        try:
            self._adapter.configure_wavelength(float(nm))
        except Exception as exc:
            self.error.emit(f"PM100D set_wavelength failed: {exc}")

    @property
    def adapter(self):
        return self._adapter


class PM100DController(QObject):
    """
    Controls the Thorlabs PM100D power meter.

    Usage:
        ctrl = PM100DController()
        ctrl.devices_scanned.connect(panel.on_pm_devices)
        ctrl.scan_devices()
        ctrl.connect_instrument("USB0::0x1313::...")
        ctrl.read_power()
    """

    connected       = Signal()
    disconnected    = Signal()
    error           = Signal(str)
    power_ready     = Signal(float)
    devices_scanned = Signal(list)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)

        self._thread = QThread(self)
        self._worker = _PM100DWorker()
        self._worker.moveToThread(self._thread)

        self._worker.connected.connect(self.connected)
        self._worker.disconnected.connect(self.disconnected)
        self._worker.error.connect(self.error)
        self._worker.power_ready.connect(self.power_ready)
        self._worker.devices_scanned.connect(self.devices_scanned)

        self._thread.start()

    def scan_devices(self) -> None:
        self._worker.scan_devices.__func__(self._worker)

    def connect_instrument(self, resource_name: str) -> None:
        self._worker.connect_instrument.__func__(self._worker, resource_name)

    def disconnect_instrument(self) -> None:
        self._worker.disconnect_instrument.__func__(self._worker)

    def read_power(self) -> None:
        self._worker.read_power.__func__(self._worker)

    def set_wavelength(self, nm: float) -> None:
        self._worker.set_wavelength.__func__(self._worker, float(nm))

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
