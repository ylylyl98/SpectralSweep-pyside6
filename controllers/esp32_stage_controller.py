"""Thread-owned Qt controller for the USB ESP32 open-loop imaging stage."""
from __future__ import annotations

import threading
import time
import math
from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot

from app.devices.esp32_stage_adapter import ESP32StageAdapter


@dataclass
class _Pending:
    command: str
    kind: str
    generation: int
    sent_at: float
    target_steps: Optional[int] = None
    acknowledged: bool = False


class _ESP32Worker(QObject):
    connected = Signal(str)
    disconnected = Signal()
    error = Signal(str)
    state_changed = Signal(dict)
    _wake = Signal()

    def __init__(self, adapter_factory=None, clock: Callable[[], float] | None = None):
        super().__init__()
        self._adapter_factory = adapter_factory or ESP32StageAdapter
        self._clock = clock or time.monotonic
        self._adapter = None
        self._connected = False
        self._state: dict = {}
        self._last_valid: Optional[float] = None
        self._status_started: Optional[float] = None
        self._next_status = 0.0
        self._cooldown_until = 0.0
        self._pending: Optional[_Pending] = None
        self._generation = 0
        self._mailbox = None
        self._mailbox_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._stop_transmitted = False
        self._await_unhomed_handshake = False
        self._recovery_flush_done = False
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self._wake.connect(self._service_mailbox, Qt.QueuedConnection)

    # ---------- thread-safe request side (called by the GUI thread) ----------
    def _post(self, kind: str, value=None) -> bool:
        if self._stop_event.is_set():
            return False
        now = self._clock()
        if (not self._connected or self._adapter is None or self._pending is not None
                or self._state.get("moving") or now < self._cooldown_until
                or not self._fresh(now)):
            self.error.emit("Stage is not ready; command discarded.")
            return False
        with self._mailbox_lock:
            if self._mailbox is not None:
                self.error.emit("Previous stage command is pending; command discarded.")
                return False
            self._mailbox = (kind, value, self._generation)
        self._wake.emit()
        return True

    def request_stop(self) -> None:
        """Cancel the mailbox and invalidate the estimate before transmitting ``!``."""
        with self._mailbox_lock:
            self._mailbox = None
            self._generation += 1
            self._stop_event.set()
            self._stop_transmitted = False
            self._pending = None
            self._state = {}
            self._last_valid = None
            self._cooldown_until = self._clock() + 0.35
            self._await_unhomed_handshake = True
            self._recovery_flush_done = False
        self.state_changed.emit({"valid": False, "homed": False, "moving": False,
                                 "message": "Stopped - position unknown", "pending": False,
                                 "cooldown": True})
        self._wake.emit()

    def post_command(self, command: str) -> bool:
        command = str(command).strip()
        if command == "status":
            self._wake.emit()
            return True
        if command not in {"zero", "calibrate", "setmax", "home", "invalidate", "stop"}:
            self.error.emit("Unknown imaging-stage command.")
            return False
        if command == "stop":
            self.request_stop()
            return True
        return self._post(command)

    def post_distance(self, kind: str, value: float) -> bool:
        try:
            value = float(value)
        except (TypeError, ValueError):
            self.error.emit("Stage distance is invalid.")
            return False
        if kind not in {"jog", "move", "goto"}:
            self.error.emit("Unknown stage move command.")
            return False
        return self._post(kind, value)

    def post_frequency(self, value: int) -> bool:
        try:
            value = int(value)
        except (TypeError, ValueError):
            self.error.emit("Frequency must be an integer from 100 to 2000 Hz.")
            return False
        return self._post("frequency", value)

    def post_maximum(self, value: float | None = None) -> bool:
        if value is None:
            return self._post("setmax")
        try:
            value = float(value)
        except (TypeError, ValueError):
            self.error.emit("Maximum distance must be between 0 and 100 mm.")
            return False
        return self._post("setmax_value", value)

    def post_scale(self, pulses_per_rev: int) -> bool:
        try: value = int(pulses_per_rev)
        except (TypeError, ValueError):
            self.error.emit("Unsupported driver pulse setting."); return False
        return self._post("scale", value)

    def post_direction(self, away_level: bool) -> bool:
        return self._post("direction", bool(away_level))

    # ---------- serial owner (runs in the worker thread) ----------
    @Slot(str)
    def connect_port(self, port: str):
        self._disconnect_internal(send_stop=False)
        self._stop_event.clear()
        try:
            self._adapter = self._adapter_factory(str(port).strip())
            # Remove bytes produced before this session.  The stop response
            # itself is still consumed below but never treated as a reference.
            flush = getattr(self._adapter, "flush_input", None)
            if callable(flush):
                flush()
            self._adapter.stop_immediate()
            now = self._clock()
            self._connected = True
            self._state = {}
            self._pending = None
            self._last_valid = None
            self._status_started = None
            self._cooldown_until = now + 0.35
            self._next_status = self._cooldown_until
            self._stop_transmitted = True
            self._await_unhomed_handshake = True
            self._recovery_flush_done = False
            self.connected.emit(str(port))
            self.state_changed.emit({"valid": False, "homed": False, "moving": False,
                                     "message": "Connected - manual zero required",
                                     "pending": False, "cooldown": True})
            self._timer.start()
        except Exception as exc:
            self._disconnect_internal(send_stop=False)
            self.error.emit(f"ESP32 stage connection failed: {exc}")

    @Slot()
    def disconnect_port(self):
        self._disconnect_internal(send_stop=True)

    def _disconnect_internal(self, send_stop: bool):
        self._timer.stop()
        if self._adapter is not None and send_stop:
            try:
                self._adapter.stop_immediate()
            except Exception as exc:
                self.error.emit(f"Stage stop during disconnect failed: {exc}")
        if self._adapter is not None:
            try:
                self._adapter.close()
            except Exception as exc:
                self.error.emit(f"Stage serial close failed: {exc}")
        was_connected = self._connected
        self._adapter = None
        self._connected = False
        self._pending = None
        self._state = {}
        self._last_valid = None
        self._status_started = None
        with self._mailbox_lock:
            self._mailbox = None
            self._generation += 1
        self._stop_event.clear()
        self._stop_transmitted = False
        if was_connected:
            self.state_changed.emit({"valid": False, "homed": False, "moving": False,
                                     "message": "Disconnected - position unknown",
                                     "pending": False, "cooldown": False})
            self.disconnected.emit()

    def _transmit_line(self, command: str) -> bool:
        # This is the one gate before every ordinary serial write.
        with self._mailbox_lock:
            if self._stop_event.is_set() or self._adapter is None:
                return False
            self._adapter.write(command)
            return True

    def _service_mailbox(self):
        if self._adapter is None:
            return
        if self._stop_event.is_set():
            self._transmit_stop()
            return
        with self._mailbox_lock:
            request = self._mailbox
            self._mailbox = None
        if request is None:
            return
        kind, value, generation = request
        if generation != self._generation or self._stop_event.is_set():
            return
        if not self._can_accept(kind, value):
            return
        if kind == "frequency":
            command = f"frequency {int(value)}"
        elif kind == "setmax_value":
            command = f"setmax {float(value):g}"
        elif kind == "scale":
            command = f"scale {int(value)}"
        elif kind == "direction":
            command = f"direction {1 if value else 0}"
        elif kind in {"jog", "move", "goto"}:
            command = f"{kind} {float(value):g}"
        else:
            command = kind
        try:
            if not self._transmit_line(command):
                return
            now = self._clock()
            target = None
            if kind in {"home", "jog", "move", "goto"}:
                scale = int(self._state.get("stepsPerMm", 200) or 200)
                if kind == "home":
                    target = 0
                elif kind == "goto":
                    target = self._round_steps(float(value), scale)
                elif kind == "jog" and not self._state.get("homed"):
                    target = self._round_steps(float(value), scale)
                else:
                    target = int(self._state.get("steps", 0)) + self._round_steps(float(value), scale)
            elif kind == "setmax_value":
                target = self._round_steps(float(value), int(self._state.get("stepsPerMm", 200)))
            self._pending = _Pending(command, kind, generation, now, target)
        except Exception as exc:
            self.error.emit(f"Stage command failed: {exc}")
            # A write may have reached the firmware before raising. Fence the
            # parser and position reference instead of trying the command
            # again or allowing a later command to concatenate with it.
            self.request_stop()

    def _fresh(self, now: float) -> bool:
        return self._last_valid is not None and now - self._last_valid <= 1.5

    def _can_accept(self, kind: str, value=None) -> bool:
        now = self._clock()
        if not self._connected or self._adapter is None:
            self.error.emit("Imaging stage is not connected.")
            return False
        if not self._fresh(now):
            self.error.emit("Stage status is not fresh; command discarded.")
            return False
        if self._pending is not None:
            self.error.emit("Previous stage command is pending; command discarded.")
            return False
        if now < self._cooldown_until:
            self.error.emit("Stage is settling; try again shortly.")
            return False
        if self._state.get("legacy") or self._state.get("protocol") != "stage-v2":
            self.error.emit("Firmware update required (stage-v2).")
            return False
        moving = bool(self._state.get("moving"))
        if moving:
            self.error.emit("Stage is moving; command discarded.")
            return False
        if kind == "frequency":
            if not isinstance(value, int) or not 100 <= value <= 2000:
                self.error.emit("Frequency must be an integer from 100 to 2000 Hz.")
                return False
        elif kind == "jog":
            if not math.isfinite(float(value)) or not (-10.0 <= float(value) <= 10.0) or float(value) == 0:
                self.error.emit("Jog must be nonzero and at most 10 mm.")
                return False
            if self._state.get("homed"):
                limit = int(self._state.get("limit", 0) or 0)
                target = int(self._state.get("steps", 0)) + self._round_steps(float(value), int(self._state.get("stepsPerMm", 200)))
                if limit <= 0:
                    self.error.emit("Set maximum distance in Advanced before moving away.")
                    return False
                if target < 0 or target > limit:
                    self.error.emit("Jog would exceed the saved maximum distance.")
                    return False
        elif kind in {"move", "goto"}:
            if not math.isfinite(float(value)):
                self.error.emit("Stage distance is invalid.")
                return False
            if kind == "move" and (float(value) == 0 or abs(float(value)) > 100):
                self.error.emit("Relative move must be between 0.005 and 100 mm.")
                return False
            if kind == "goto" and not 0 <= float(value) <= 100:
                self.error.emit("Absolute position must be between 0 and 100 mm.")
                return False
            if not self._state.get("homed") or int(self._state.get("limit", 0)) <= 0:
                self.error.emit("Set zero and upper limit before normal moves.")
                return False
            if kind == "goto":
                scale = int(self._state.get("stepsPerMm", 200))
                if float(value) > int(self._state["limit"]) / scale:
                    self.error.emit("Target exceeds the saved maximum distance.")
                    return False
        elif kind in {"home", "setmax", "setmax_value", "scale", "direction", "invalidate", "zero", "calibrate"}:
            if not self._state.get("homed") or (kind == "home" and int(self._state.get("limit", 0)) <= 0):
                if kind not in {"zero", "invalidate", "calibrate", "scale", "direction"}:
                    self.error.emit("Set zero before this command.")
                    return False
            if kind == "setmax" and not (int(self._state.get("steps", 0)) > 0):
                self.error.emit("Move to the upper reference before setting the limit.")
                return False
            if kind == "setmax_value":
                if not math.isfinite(float(value)) or not 0 < float(value) <= 100:
                    self.error.emit("Maximum distance must be between 0 and 100 mm."); return False
                if self._round_steps(float(value), int(self._state.get("stepsPerMm", 200))) <= 0:
                    self.error.emit("Maximum is smaller than one motor step."); return False
                current = int(self._state.get("steps", 0)) / int(self._state.get("stepsPerMm", 200))
                if self._state.get("homed") and float(value) < current:
                    self.error.emit("Maximum distance must be at least the current position."); return False
            if kind == "scale" and value not in ESP32StageAdapter.SUPPORTED_PULSES:
                self.error.emit("Unsupported driver pulse setting."); return False
        return True

    def _transmit_stop(self):
        if self._adapter is None or self._stop_transmitted:
            return
        try:
            with self._mailbox_lock:
                self._adapter.stop_immediate()
            self._stop_transmitted = True
            self._cooldown_until = self._clock() + 0.35
            self._await_unhomed_handshake = True
            self._recovery_flush_done = False
            self._next_status = self._cooldown_until
        except Exception as exc:
            self.error.emit(f"Stage stop failed: {exc}")
            self._disconnect_internal(send_stop=False)

    def _send_status(self, now: float):
        if self._adapter is None or self._stop_event.is_set() or now < self._cooldown_until:
            return
        try:
            if not self._transmit_line("status"):
                return
            self._next_status = now + 0.5
            if self._status_started is None:
                self._status_started = now
        except Exception as exc:
            self.error.emit(f"Stage polling failed: {exc}")
            self._disconnect_internal(send_stop=True)

    @staticmethod
    def _round_steps(mm: float, scale: int = 200) -> int:
        return int(math.floor(mm * scale + 0.5) if mm >= 0 else math.ceil(mm * scale - 0.5))

    @staticmethod
    def _message_matches(pending: _Pending, message: str, state: dict) -> bool:
        if pending.kind == "zero":
            return message == "Manual zero set"
        if pending.kind == "calibrate":
            return message.startswith("Calibration:")
        if pending.kind == "setmax":
            return message in {"Upper limit saved", "Maximum saved"}
        if pending.kind == "frequency":
            return message == "Frequency updated"
        if pending.kind == "setmax_value":
            if pending.target_steps is not None and message in {"Maximum saved", "Upper limit saved"}:
                return state.get("limit") == pending.target_steps
            return message in {"Maximum saved", "Upper limit saved"}
        if pending.kind == "scale":
            return (message == "Scale updated" and state.get("stepsPerMm") == int(pending.command.split()[-1])
                    and not state.get("homed") and int(state.get("limit", 0) or 0) == 0)
        if pending.kind == "direction":
            try: expected = bool(int(pending.command.split()[-1]))
            except (ValueError, IndexError): return False
            return (message == "Direction updated" and state.get("directionAwayLevel") is expected
                    and not state.get("homed") and int(state.get("limit", 0) or 0) == 0)
        if pending.kind == "invalidate":
            return message in {"Reference cleared", "Set zero again before normal moves"}
        if pending.kind == "zero":
            return message == "Manual zero set"
        if pending.kind in {"home", "jog", "move", "goto"}:
            if message in {"Move complete", "Already at target"} and pending.target_steps is not None:
                if state.get("steps") != pending.target_steps:
                    return False
            return message in {"Moving", "Move complete", "Already at target"}
        return False

    @staticmethod
    def _is_rejection(message: str) -> bool:
        return (message.startswith(("Busy -", "Unknown command", "Invalid distance", "Jog must",
                                     "Frequency must", "Outside software", "Set zero", "Could not",
                                     "Calibration:", "Connection timeout", "Stopped", "Maximum", "Scale", "Direction", "Invalid setting", "Set maximum distance"))
                and message != "Calibration: jog to reference then set zero")

    def _handle_state(self, state: dict):
        now = self._clock()
        message = state.get("message", "")
        if self._stop_event.is_set() or now < self._cooldown_until:
            return
        if self._await_unhomed_handshake:
            # The first post-STOP status must prove that the firmware is idle
            # and unhomed.  This prevents a delayed pre-stop homed frame from
            # restoring trust after the 350 ms silence interval.
            if state.get("homed") or state.get("moving"):
                return
            self._await_unhomed_handshake = False
        self._last_valid = now
        pending = self._pending
        if pending is not None and pending.kind in {"home", "jog", "move", "goto"} and state.get("moving"):
            pending.acknowledged = True
        if (pending is not None and pending.acknowledged
                and pending.kind in {"home", "jog", "move", "goto"}
                and not state.get("moving") and message not in {"Move complete", "Already at target"}):
            if pending.target_steps is not None and state.get("steps") == pending.target_steps:
                self._pending = None
                self._cooldown_until = now + 0.35
                self._next_status = self._cooldown_until
            else:
                self.error.emit(f"Stage completed {pending.command} at an unexpected position.")
                self.request_stop()
                return
        if (pending is not None and pending.kind in {"home", "jog", "move", "goto"}
                and message in {"Move complete", "Already at target"}
                and pending.target_steps is not None and state.get("steps") != pending.target_steps):
            self.error.emit(f"Stage completed {pending.command} at an unexpected position.")
            self.request_stop()
            return
        if pending is not None and self._message_matches(pending, message, state):
            if pending.kind in {"home", "jog", "move", "goto"} and message == "Moving":
                pending.acknowledged = True
            elif message in {"Move complete", "Already at target"} or pending.kind not in {"home", "jog", "move", "goto"}:
                self._pending = None
                if message in {"Move complete", "Already at target"}:
                    self._cooldown_until = now + 0.35
                    self._next_status = self._cooldown_until
        elif pending is not None and self._is_rejection(message):
            self._pending = None
            self.error.emit(f"Stage rejected {pending.command}: {message}")
        out = dict(state)
        out.update(valid=True, pending=self._pending is not None, cooldown=now < self._cooldown_until)
        self._state = out
        self.state_changed.emit(out)

    def _tick(self):
        if self._adapter is None:
            return
        if self._stop_event.is_set():
            self._transmit_stop()
            if self._adapter is None:
                return
            if self._clock() < self._cooldown_until:
                return
            if not self._recovery_flush_done:
                try:
                    flush = getattr(self._adapter, "flush_input", None)
                    if callable(flush):
                        flush()
                except Exception as exc:
                    self.error.emit(f"Stage input flush failed: {exc}")
                    self._disconnect_internal(send_stop=False)
                    return
                self._recovery_flush_done = True
            self._stop_event.clear()
            self._last_valid = None
            self._status_started = None
            self._stop_transmitted = False
            return
        now = self._clock()
        # Drain a bounded number of frames.  Only valid frames update freshness.
        for _ in range(2):
            if self._stop_event.is_set():
                return
            try:
                state = self._adapter.read_status()
            except Exception as exc:
                self.error.emit(f"Stage read failed: {exc}")
                self._disconnect_internal(send_stop=True)
                return
            if state is None:
                break
            self._handle_state(state)
        pending = self._pending
        if pending is not None and not pending.acknowledged and now - pending.sent_at > 3.0:
            self.error.emit(f"No acknowledgement for stage command: {pending.command}")
            # The write may have reached the firmware even though its reply
            # was lost. Fence the stream and require a new unhomed handshake;
            # the command is never replayed.
            self.request_stop()
            return
        if ((self._last_valid is not None and now - self._last_valid > 1.5)
                or (self._last_valid is None and self._status_started is not None
                    and now - self._status_started > 1.5)):
            self.error.emit("Stage status stale; position is unknown.")
            self._disconnect_internal(send_stop=True)
            return
        if now >= self._next_status:
            self._send_status(now)
        self._service_mailbox()

    # Name retained for small integration tests written against the original
    # scaffold.
    def _poll(self):
        self._tick()

    # Compatibility slots for tests and older callers; normal GUI requests use
    # the thread-safe post_* methods above.
    @Slot(str)
    def command(self, command: str):
        self.post_command(command)

    @Slot(str, float)
    def distance_command(self, kind: str, value: float):
        self.post_distance(kind, value)

    @Slot(int)
    def frequency(self, value: int):
        self.post_frequency(value)

    @Slot()
    def shutdown(self):
        self._disconnect_internal(send_stop=True)
        QThread.currentThread().quit()


class ESP32StageController(QObject):
    connected = Signal(str)
    disconnected = Signal()
    error = Signal(str)
    state_changed = Signal(dict)

    def __init__(self, parent: Optional[QObject] = None, adapter_factory=None):
        super().__init__(parent)
        self._thread = QThread(self)
        self._worker = _ESP32Worker(adapter_factory)
        self._worker.moveToThread(self._thread)
        self._worker.connected.connect(self.connected)
        self._worker.disconnected.connect(self.disconnected)
        self._worker.error.connect(self.error)
        self._worker.state_changed.connect(self.state_changed)
        self._thread.started.connect(lambda: None)
        self._thread.start()

    def connect_instrument(self, port):
        # Queued invocation keeps serial creation inside the owner thread.
        from PySide6.QtCore import QMetaObject, Q_ARG
        QMetaObject.invokeMethod(self._worker, "connect_port", Qt.QueuedConnection, Q_ARG(str, str(port)))

    def disconnect_instrument(self):
        from PySide6.QtCore import QMetaObject
        QMetaObject.invokeMethod(self._worker, "disconnect_port", Qt.QueuedConnection)

    def send(self, command):
        self._worker.post_command(str(command))

    def jog(self, mm):
        self._worker.post_distance("jog", float(mm))

    def move_relative(self, mm):
        self._worker.post_distance("move", float(mm))

    def goto(self, mm):
        self._worker.post_distance("goto", float(mm))

    def set_frequency(self, hz):
        self._worker.post_frequency(int(hz))

    def set_maximum(self, mm):
        self._worker.post_maximum(float(mm))

    def save_current_as_maximum(self):
        self._worker.post_maximum()

    def set_scale(self, pulses_per_rev):
        self._worker.post_scale(int(pulses_per_rev))

    def set_direction(self, away_level):
        self._worker.post_direction(bool(away_level))

    def stop(self):
        self._worker.request_stop()

    def shutdown(self):
        if not self._thread.isRunning():
            return True
        from PySide6.QtCore import QMetaObject
        self._worker.request_stop()
        QMetaObject.invokeMethod(self._worker, "shutdown", Qt.QueuedConnection)
        ok = self._thread.wait(3000)
        if not ok:
            self.error.emit("ESP32 stage worker did not close its serial port before shutdown.")
        return ok
