"""Serialize manual control, setup routing, and polling on one MFF101 thread."""
from __future__ import annotations

from datetime import datetime, timezone
import threading
import time

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot

from utils.config import cfg


class MFF101RoutingError(RuntimeError):
    """A required physical light path could not be verified."""


class _MoveStop:
    """Combine measurement Stop and controller shutdown for one movement."""
    def __init__(self, shutdown, measurement=None):
        self.shutdown, self.measurement = shutdown, measurement

    def is_set(self):
        return self.shutdown.is_set() or (self.measurement is not None and self.measurement.is_set())


def _open_adapter(serial_number):
    from app.devices.mff101_adapter import MFF101Adapter
    return MFF101Adapter(serial_number)


def _scan_devices():
    from app.devices.mff101_adapter import scan_mff101_devices
    return scan_mff101_devices()


class _MFF101Worker(QObject):
    completed = Signal(dict)

    def __init__(self, adapter_factory, scan_func):
        super().__init__()
        self._factory = adapter_factory
        self._scan = scan_func
        self._adapter = None
        self._serial = ""
        self._position = None
        self._error = ""
        self._observed_utc = None
        self.shutdown_done = threading.Event()
        self.stop_motion = threading.Event()
        self.shutdown_result = {}
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._poll)

    def _close(self):
        self._timer.stop()
        self._position = None
        self._observed_utc = None
        if self._adapter is not None:
            self._adapter.close()
            self._adapter = None

    @Slot()
    def _poll(self):
        if self._adapter is not None:
            self.execute("poll", None)

    @Slot(str, object)
    def execute(self, operation, value):
        devices = None
        if operation != "poll":
            self._error = ""
        try:
            if operation == "scan":
                devices = self._scan()
            elif operation == "connect":
                self._serial = value
                self._adapter = self._factory(value)
                self._position = self._adapter.get_position()
                self._observed_utc = datetime.now(timezone.utc).isoformat()
                self._timer.start()
            elif operation in ("disconnect", "shutdown"):
                self._close()
            elif operation == "route":
                stop = value.get('stop')
                if stop is not None and stop.is_set():
                    raise RuntimeError("Setup routing stopped.")
                if self._adapter is None:
                    serial = value['serial']
                    if not serial:
                        devices = self._scan()
                        if len(devices) != 1:
                            raise RuntimeError("Select an MFF101 serial number; expected exactly one USB device.")
                        serial = devices[0]
                    self._serial = serial
                    self._adapter = self._factory(serial)
                    self._timer.start()
                elif value['serial'] and value['serial'] != self._serial:
                    raise RuntimeError("Connected MFF101 does not match the configured serial number.")
                target = value['position']
                self._position = self._adapter.get_position()
                if self._position != target:
                    if stop is not None and stop.is_set():
                        raise RuntimeError("Setup routing stopped.")
                    self._adapter.move_to(target, stop_event=_MoveStop(self.stop_motion, stop))
                    self._position = self._adapter.get_position()
                if self._position != target:
                    raise RuntimeError(f"MFF101 did not reach position {target}.")
                self._observed_utc = datetime.now(timezone.utc).isoformat()
            elif operation in ("move", "read", "poll"):
                if self._adapter is None:
                    raise RuntimeError("MFF101 is disconnected.")
                self._position = (self._adapter.move_to(value, stop_event=self.stop_motion) if operation == "move"
                                  else self._adapter.get_position())
                self._observed_utc = datetime.now(timezone.utc).isoformat()
            else:
                raise ValueError(f"Unknown MFF101 operation: {operation}")
        except Exception as exc:
            self._position = None
            self._observed_utc = None
            self._error = f"MFF101 {operation} failed: {exc}"
            if operation in ("connect", "read", "poll"):
                try:
                    self._close()
                except Exception as close_exc:
                    self._error += f"; close failed: {close_exc}"
        finally:
            result = {
                "operation": operation, "connected": self._adapter is not None,
                "serial_number": self._serial, "position": self._position,
                "error": self._error, "devices": devices,
                "observed_utc": self._observed_utc,
            }
            if operation == "shutdown":
                self.shutdown_result = result
                if not self._error:
                    QThread.currentThread().quit()
                self.shutdown_done.set()
            else:
                self.completed.emit(result)
                if operation == "route" and 'done' in value:
                    value['result'] = result
                    value['done'].set()


class MFF101Controller(QObject):
    state_changed = Signal(dict)
    devices_scanned = Signal(list)
    error = Signal(str)
    _request = Signal(str, object)

    def __init__(self, parent=None, *, adapter_factory=None, scan_func=None):
        super().__init__(parent)
        self._state = {"connected": False, "serial_number": "", "position": None,
                       "busy": False, "operation": "", "error": "",
                       "observed_utc": None, "closing": False}
        self._closing = False
        self._lock = threading.RLock()
        self._idle = threading.Event()
        self._idle.set()
        self._manual_locked = False
        self._thread = QThread(self)
        self._worker = _MFF101Worker(adapter_factory or _open_adapter, scan_func or _scan_devices)
        self._worker.moveToThread(self._thread)
        self._request.connect(self._worker.execute, Qt.ConnectionType.QueuedConnection)
        # Completion bookkeeping must also run when the GUI is waiting for
        # shutdown. This slot only changes locked state and emits Qt signals.
        self._worker.completed.connect(self._complete, Qt.ConnectionType.DirectConnection)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    @property
    def state(self):
        with self._lock:
            return dict(self._state, manual_locked=self._manual_locked)

    @property
    def is_connected(self):
        return self.state["connected"]

    @property
    def is_busy(self):
        return self.state["busy"]

    def _post(self, operation, value=None, *, preflight=False):
        with self._lock:
            if self._closing or self.is_busy or (self._manual_locked and not preflight):
                return False
            self._idle.clear()
            self._state.update(busy=True, operation=operation, error="")
            if operation in ("move", "connect", "disconnect", "route"):
                self._state["position"] = None
            self.state_changed.emit(self.state)
            self._request.emit(operation, value)
            return True

    @Slot(bool)
    def set_manual_locked(self, locked):
        with self._lock:
            if self._manual_locked != bool(locked):
                self._manual_locked = bool(locked)
                self.state_changed.emit(self.state)

    @staticmethod
    def _route_request(backend, use_mock):
        if use_mock or not cfg.mff101.auto_route:
            return None
        if backend in {'lightfield', 'winspec_ingaas'}:
            position = cfg.mff101.lightfield_position
        elif backend in {'andor_si', 'andor_ingaas'}:
            position = cfg.mff101.andor_position
        else:
            raise ValueError(f"Unknown measurement setup: {backend}")
        if type(position) is not int or position not in (1, 2):
            raise ValueError("Choose position 1 or 2 for this setup in Settings → MFF101 light paths.")
        return {'position': position, 'serial': cfg.mff101.serial_number.strip()}

    def route_for_setup(self, backend, use_mock=False):
        """Nonblocking route for an explicit user selection; never from restore."""
        try:
            request = self._route_request(backend, use_mock)
            if request is None:
                return True
            if not self._post('route', request):
                raise RuntimeError("Wait for the measurement or MFF101 operation to finish.")
            return True
        except Exception as exc:
            self.error.emit(str(exc))
            return False

    def ensure_setup_position(self, backend, use_mock=False, stop_event=None):
        """Blocking preflight for acquisition workers; no GUI dispatch needed.

        Wait for any accepted manual operation, then verify the actual position.
        All USB calls still run on the MFF101 thread, including readback.
        """
        try:
            request = self._route_request(backend, use_mock)
        except Exception as exc:
            raise MFF101RoutingError(str(exc)) from exc
        if request is None:
            return
        request['done'] = threading.Event()
        request['stop'] = stop_event
        deadline = time.monotonic() + 10
        while True:
            if stop_event is not None and stop_event.is_set():
                raise MFF101RoutingError("Setup routing stopped.")
            if self._post('route', request, preflight=True):
                break
            if self._closing:
                raise MFF101RoutingError("MFF101 is closing; setup routing is unavailable.")
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._idle.wait(remaining):
                raise MFF101RoutingError("MFF101 is busy; setup routing could not start.")
        # Do not abandon an accepted move: adapter IO and motion waits are bounded.
        request['done'].wait()
        if request['result']['error']:
            raise MFF101RoutingError(request['result']['error'])
        if stop_event is not None and stop_event.is_set():
            raise MFF101RoutingError("Setup routing stopped.")

    def scan_devices(self):
        return not self.is_connected and self._post("scan")

    def connect_instrument(self, serial_number):
        serial = str(serial_number).strip()
        if not serial or self.is_connected:
            return False
        return self._post("connect", serial)

    def disconnect_instrument(self):
        return self.is_connected and self._post("disconnect")

    def read_position(self):
        return self.is_connected and self._post("read")

    def move_to(self, position):
        if type(position) is not int or position not in (1, 2) or not self.is_connected:
            return False
        return self._post("move", position)

    @Slot(dict)
    def _complete(self, result):
        with self._lock:
            if self._closing or (result["operation"] == "poll" and self.is_busy):
                return
            previous_error = self._state["error"]
            self._state.update({k: v for k, v in result.items() if k != "devices"})
            self._state["busy"] = False
            self._idle.set()
            self.state_changed.emit(self.state)
            if result.get("devices") is not None:
                self.devices_scanned.emit(result["devices"])
            if result["error"] and result["error"] != previous_error:
                self.error.emit(result["error"])

    def shutdown(self, timeout_ms=10000):
        """Cancel further commands, finish active USB IO, then close on its owner thread."""
        if not self._thread.isRunning():
            return True
        deadline = time.monotonic() + timeout_ms / 1000
        with self._lock:
            if not self._closing or (self._worker.shutdown_done.is_set()
                                     and self._worker.shutdown_result.get("error")):
                self._closing = True
                self._worker.stop_motion.set()
                self._idle.set()
                self._worker.shutdown_done.clear()
                self._state.update(busy=True, position=None, operation="shutdown", closing=True)
                self.state_changed.emit(self.state)
                self._request.emit("shutdown", None)
        if not self._worker.shutdown_done.wait(max(0, deadline - time.monotonic())):
            return False
        result = self._worker.shutdown_result
        if result["error"]:
            self._state.update({k: v for k, v in result.items() if k != "devices"})
            self._state["busy"] = False
            self.state_changed.emit(self.state)
            self.error.emit(result["error"])
            return False
        stopped = self._thread.wait(max(0, int((deadline - time.monotonic()) * 1000)))
        if stopped:
            self._state.update(connected=False, busy=False, position=None, observed_utc=None, error="")
            self.state_changed.emit(self.state)
        return stopped
