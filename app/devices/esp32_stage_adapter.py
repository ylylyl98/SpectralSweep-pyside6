"""Bounded serial protocol adapter for the ESP32 sample stage.

The adapter accepts stage-v1 only to identify it as legacy. A controller must
refuse all reference, motion, and device-setting commands until stage-v2 is
reported and its capabilities have been validated.
"""
from __future__ import annotations
import json
import time
from typing import Any, Callable, Optional


class ESP32StageAdapter:
    _MAX_LINE = 512
    _MAX_MESSAGE = 160
    SUPPORTED_PULSES = (200, 800, 1600, 3200)
    steps_per_mm = 200
    position_unit = "mm (estimated)"

    def __init__(self, port: str, serial_factory=None, clock: Callable[[], float] | None = None):
        self.port = str(port).strip()
        if not self.port:
            raise ValueError("A USB serial port is required")
        if serial_factory is None:
            import serial
            serial_factory = serial.Serial
        self._clock = clock or time.monotonic
        try:
            self.serial = serial_factory(self.port, baudrate=115200, timeout=0.05, write_timeout=0.2)
        except TypeError:
            self.serial = serial_factory(self.port, 115200, timeout=0.05)
        for attr in ("dtr", "rts"):
            try: setattr(self.serial, attr, False)
            except Exception: pass
        self._buffer = bytearray()
        self.last_rx_byte = self._clock()
        self.last_valid = 0.0

    def write(self, command: str) -> None:
        text = str(command)
        if not text or "\n" in text or "\r" in text or len(text) > 72:
            raise ValueError("Invalid stage command")
        self.serial.write((text + "\n").encode("ascii"))

    def stop_immediate(self) -> None: self.serial.write(b"!")

    def flush_input(self) -> None:
        reset = getattr(self.serial, "reset_input_buffer", None)
        if callable(reset): reset()
        self._buffer.clear()

    @staticmethod
    def _is_int(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    def _parse(self, raw: bytes) -> Optional[dict[str, Any]]:
        if len(raw) > self._MAX_LINE: return None
        try: value = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError): return None
        if not isinstance(value, dict) or value.get("protocol") not in ("stage-v1", "stage-v2"): return None
        required = {"homed", "moving", "steps", "limit", "stepsPerMm", "frequencyHz", "message"}
        if not required.issubset(value) or not isinstance(value["homed"], bool) or not isinstance(value["moving"], bool): return None
        if not all(self._is_int(value[k]) for k in ("steps", "limit", "stepsPerMm", "frequencyHz")): return None
        if value["stepsPerMm"] not in self.SUPPORTED_PULSES: return None
        max_steps = 100 * value["stepsPerMm"]
        if not (0 <= value["limit"] <= max_steps) or not (-max_steps <= value["steps"] <= max_steps): return None
        if not (100 <= value["frequencyHz"] <= 2000) or not isinstance(value["message"], str) or len(value["message"]) > self._MAX_MESSAGE: return None
        if value["protocol"] == "stage-v1":
            value["legacy"] = True; value["capabilities"] = []; return value
        caps = value.get("capabilities")
        if isinstance(caps, dict): caps = list(caps)
        if not isinstance(caps, list) or not all(isinstance(c, str) and len(c) <= 32 for c in caps) or not {"scale", "direction", "maximum"}.issubset(caps): return None
        ppr = value.get("pulsesPerRev", value["stepsPerMm"]); direction = value.get("directionAwayLevel")
        if not self._is_int(ppr) or ppr not in self.SUPPORTED_PULSES or ppr != value["stepsPerMm"] or not isinstance(direction, bool): return None
        value["pulsesPerRev"] = ppr; value["directionAwayLevel"] = direction; value["legacy"] = False
        return value

    def read_status(self) -> Optional[dict[str, Any]]:
        for _ in range(8):
            newline = self._buffer.find(b"\n")
            if newline >= 0: raw = b""
            else:
                read = getattr(self.serial, "read", None)
                if callable(read):
                    try: waiting = int(getattr(self.serial, "in_waiting", 0) or 0)
                    except (TypeError, ValueError, OSError): waiting = 0
                    raw = read(max(1, min(waiting, self._MAX_LINE)))
                else: raw = self.serial.readline()
            if raw is None: return None
            if isinstance(raw, str): raw = raw.encode("utf-8", errors="replace")
            if not isinstance(raw, (bytes, bytearray)): return None
            if raw: self.last_rx_byte = self._clock(); self._buffer.extend(raw)
            if len(self._buffer) > self._MAX_LINE:
                newline = self._buffer.find(b"\n")
                if newline < 0 or newline > self._MAX_LINE: self._buffer.clear()
                else: del self._buffer[:newline + 1]
                continue
            newline = self._buffer.find(b"\n")
            if newline < 0: return None
            line = bytes(self._buffer[:newline]).rstrip(b"\r"); del self._buffer[:newline + 1]
            state = self._parse(line)
            if state is not None: self.last_valid = self._clock(); return state
        return None

    def close(self) -> None:
        try: self.serial.close()
        except Exception: pass
