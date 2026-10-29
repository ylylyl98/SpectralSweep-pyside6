# app/devices/rotation_eps300_adapter.py
from __future__ import annotations

from dataclasses import dataclass
from collections import deque
import math
import re
from typing import Iterable, List, Optional, Union
from threading import RLock
import time

from app.devices.motion_verification import MotionHardwareFault, MotionVerificationError

try:
    import pyvisa
except Exception as e:  # pragma: no cover
    pyvisa = None


@dataclass(frozen=True)
class ESP300MotionProfile:
    axis: int
    stage_id: str
    velocity: float
    max_velocity: float
    acceleration: float
    max_acceleration: float
    deceleration: float


@dataclass(frozen=True)
class ESPControllerError:
    code: int
    timestamp: str
    message: str
    raw: str

    @property
    def axis(self) -> int | None:
        return self.code // 100 if self.code >= 100 else None


class NewportEPS300:
    """
    Newport ESP300/ESP301 native ASCII adapter (VISA GPIB/serial/USB COM).
    """

    #: Default VISA timeouts (ms)
    DEFAULT_TIMEOUT_MS = 3000
    #: Default serial baud for EPS/ESP-300
    DEFAULT_BAUD = 19200
    ESP301_USB_BAUD = 921600

    @classmethod
    def connection_settings(cls, model: str = "esp300", baud: Optional[int] = None) -> tuple[str, int]:
        """Resolve host communication settings without contacting a controller."""
        model = str(model).strip().lower()
        if model not in ("esp300", "esp301"):
            raise ValueError(f"Unsupported Newport controller model: {model!r}")
        resolved_baud = int(baud) if baud is not None else (
            cls.ESP301_USB_BAUD if model == "esp301" else cls.DEFAULT_BAUD
        )
        if resolved_baud <= 0:
            raise ValueError("Newport serial baud must be positive")
        return model, resolved_baud
    
    def __init__(
        self,
        resource: str,
        *,
        rm: "pyvisa.ResourceManager" = None,
        model: str = "esp300",
        baud: Optional[int] = None,
        axis: Optional[int] = 1,
        connect_only: bool = False,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ):
        self.model, self.baud = self.connection_settings(model, baud)
        if pyvisa is None and rm is None:  # pragma: no cover
            raise RuntimeError("pyvisa not installed")

        self.resource_name = resource
        self.rm = rm or pyvisa.ResourceManager()
        self._inst = None
        self._io_lock = RLock()
        self._timeout_ms = int(timeout_ms)
        self._axis = int(axis) if axis is not None else 1
        self._identity: str | None = None
        self._controller_error_history: deque[ESPControllerError] = deque(maxlen=100)

        self._open(baud=self.baud)
        time.sleep(0.05)

    # ---------------------------
    # Property Fix for main_ui.py
    # ---------------------------
    @property
    def active_axis(self) -> int:
        """Alias for the internal _axis, required by main_ui.py logic."""
        return self._axis

    @active_axis.setter
    def active_axis(self, val: int):
        """Allows main_ui.py to update the axis (e.g. from 1 to 2)."""
        self._axis = int(val)

    # ---------------------------
    # Basic VISA open/close
    # ---------------------------
    def _open(self, *, baud: Optional[int]):
        inst = self.rm.open_resource(self.resource_name)
        try:
            inst.timeout = self._timeout_ms
            if self.resource_name.upper().startswith("ASRL"):
                # ESP301 native USB COM uses a different baud from RS-232.
                inst.baud_rate = int(baud or self.DEFAULT_BAUD)
            # Both native serial and GPIB receive CR and send CRLF.
            inst.read_termination = "\r\n"
            inst.write_termination = "\r"
        except Exception:
            try:
                inst.close()
            except Exception:
                pass
            raise

        self._inst = inst

    def close(self):
        if self._inst is not None:
            try:
                self._inst.close()
            except Exception:
                pass
            self._inst = None

    # ---------------------------
    # Low-level I/O helpers
    # ---------------------------
    def _write(self, s: str):
        if self._inst is None:
            raise RuntimeError("Controller not open")
        with self._io_lock:
            return self._inst.write(s)

    def _read(self) -> str:
        if self._inst is None:
            raise RuntimeError("Controller not open")
        with self._io_lock:
            return self._inst.read()

    def _query(self, s: str) -> str:
        # Keep a query's write/read pair together.  Rotation and stage
        # adapters can share one ESP300 port from different worker threads;
        # locking only each individual I/O would allow response interleaving.
        with self._io_lock:
            self._write(s)
            time.sleep(0.02)
            return self._read()

    # ---------------------------
    # Public helpers
    # ---------------------------
    @property
    def axis(self) -> int:
        return self._axis

    def set_axis(self, axis: int):
        self._axis = int(axis)

    # ---------------------------
    # Axis / Motor power
    # ---------------------------
    def motor_on(self, axis: Optional[int] = None):
        ax = int(self._axis if axis is None else axis)
        # Enable once, then verify. Never repeat MO as fault recovery.
        self._write(f"{ax}MO")
        state = None
        for attempt in range(3):
            state = self.motor_power(axis=ax)
            if state is True:
                return
            if attempt < 2:
                time.sleep(0.05)
        self._raise_motor_power_error(ax, state, "after motor enable")

    def motor_power(self, *, axis: Optional[int] = None) -> Optional[bool]:
        """Read selected-axis power, preserving unknown communication state."""
        ax = int(self._axis if axis is None else axis)
        command = f"{ax}MO?"
        try:
            response = self._discovery_payload(self._query(command), command)
        except Exception:
            return None
        return {"0": False, "1": True}.get(response)

    @property
    def error_history(self) -> tuple[ESPControllerError, ...]:
        """Errors consumed for fault diagnostics, retained for all shared axes."""
        with self._io_lock:
            return tuple(self._controller_error_history)

    def _capture_fault_errors(self) -> str:
        """Consume at most the documented ten-entry FIFO, retaining evidence.

        Called only after an observed motor-off fault. No TE query or blanket
        clear is sent, and errors for other axes remain in local history.
        """
        with self._io_lock:
            for _ in range(10):
                try:
                    raw = self._discovery_payload(self._query("TB?"), "TB?")
                    fields = raw.split(",", 2)
                    if len(fields) != 3 or not re.fullmatch(r"\d{1,3}", fields[0].strip()):
                        return f"error diagnostics unreadable: {raw!r}"
                    code = int(fields[0].strip())
                    if code == 0:
                        break
                    self._controller_error_history.append(ESPControllerError(
                        code=code, timestamp=fields[1].strip(),
                        message=fields[2].strip(), raw=raw,
                    ))
                except Exception as exc:
                    return f"error diagnostics unavailable: {exc}"
        return ""

    def _raise_motor_power_error(self, axis: int, state: Optional[bool], context: str) -> None:
        label = f"{self.model.upper()} axis {axis} on {self.resource_name}"
        if state is None:
            raise MotionVerificationError(f"{label}: motor power could not be verified {context}; no further move issued")
        diagnostic_error = self._capture_fault_errors()
        selected = [entry for entry in self.error_history if entry.axis == axis]
        global_errors = [entry for entry in self.error_history if entry.axis is None]
        details = []
        if selected:
            details.append("recorded axis errors: " + "; ".join(
                f"{entry.code} {entry.message} (time {entry.timestamp})" for entry in selected
            ))
        if global_errors:
            details.append("controller errors (not axis-specific): " + "; ".join(
                f"{entry.code} {entry.message} (time {entry.timestamp})" for entry in global_errors
            ))
        if diagnostic_error:
            details.append(diagnostic_error)
        if not details:
            details.append("no recorded error for this axis; inspect the controller")
        summary = f"{label}: motor power is OFF {context}"
        if selected:
            # Prefer the originating fault over follow-up MOTOR NOT ENABLED
            # entries, without presenting historical evidence as newly raised.
            primary = next((entry for entry in reversed(selected) if entry.code % 100 != 13), selected[-1])
            summary += f"; recorded {primary.code} {primary.message}"
        elif global_errors:
            primary = global_errors[-1]
            summary += f"; recorded controller error {primary.code} {primary.message}"
        if len(summary) > 220:
            summary = summary[:217] + "..."
        raise MotionHardwareFault(summary + "\n" + "\n".join(details))

    def require_motor_enabled(self, *, axis: Optional[int] = None) -> None:
        ax = int(self._axis if axis is None else axis)
        state = self.motor_power(axis=ax)
        if state is not True:
            self._raise_motor_power_error(ax, state, "during motion verification")

    def motor_off(self, axis: Optional[int] = None):
        ax = int(self._axis if axis is None else axis)
        self._write(f"{ax}MF")

    # ---------------------------
    # Motion
    # ---------------------------
    def motion_status(self, axis: Optional[int] = None) -> Optional[bool]:
        """Return stopped/moving/unknown without conflating I/O failure."""
        ax = int(self._axis if axis is None else axis)
        try:
            # 'MD?' returns 1 if motion is done, 0 if moving
            resp = self._query(f"{ax}MD?")
            s = str(resp).strip()
            # Handle possible echo like "1MD?1" or just "1"
            if "MD?" in s:
                 s = s.split("?")[-1].strip()
            if s == "1":
                return True
            if s == "0":
                return False
            return None
        except Exception:
            return None

    def is_motion_done(self, axis: Optional[int] = None) -> bool:
        """Compatibility boolean; callers needing trust use ``motion_status``."""
        return self.motion_status(axis=axis) is True

    def stop_motion(self, axis: Optional[int] = None) -> None:
        ax = int(self._axis if axis is None else axis)
        self._write(f"{ax}ST")

    def move_to(
        self,
        position_deg: Union[int, float],
        *,
        axis: Optional[int] = None,
        wait: bool = True,
        timeout_s: float = 60.0,
        stop_event=None,
    ):
        """
        Move to position. 
        If wait=True (default), blocks execution until the controller confirms 
        motion is complete (MD=1).
        """
        ax = int(self._axis if axis is None else axis)
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("motion cancelled before command")
        if not math.isfinite(float(position_deg)) or not math.isfinite(float(timeout_s)) or timeout_s <= 0:
            raise ValueError("motion target and positive timeout must be finite")
        self.require_motor_enabled(axis=ax)
        self._write(f"{ax}PA{float(position_deg)}")
        
        if wait:
            deadline = time.monotonic() + float(timeout_s)
            while True:
                if stop_event is not None and stop_event.is_set():
                    try:
                        self.stop_motion(axis=ax)
                    finally:
                        raise RuntimeError("motion cancelled")
                # Poll device status
                status = self.motion_status(axis=ax)
                # MD=1 also occurs after a following-error shutdown. A stopped
                # but unpowered axis must not permit corrective PA retries.
                self.require_motor_enabled(axis=ax)
                # A late status reply cannot turn an expired deadline into success.
                # Timeout safety
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Move on axis {ax} did not report completion within {timeout_s:g}s"
                    )

                if status is True:
                    return True
                time.sleep(min(0.1, remaining))
        return True

    def get_position(self, *, axis: Optional[int] = None) -> float:
        ax = int(self._axis if axis is None else axis)
        attempts = []
        for cmd in (f"{ax}TP", f"{ax}TP?"):
            try:
                resp = self._query(cmd)
            except Exception as exc:
                attempts.append(f"{cmd} -> {exc}")
                continue

            raw = str(resp).strip()
            cleaned = self._clean_resp(raw)
            for candidate in (cleaned, raw):
                if not candidate:
                    continue
                compact = candidate.replace(",", " ").strip()
                try:
                    return float(compact)
                except Exception:
                    pass
                for token in compact.split():
                    try:
                        return float(token)
                    except Exception:
                        continue
            attempts.append(f"{cmd} -> {raw!r}")

        raise RuntimeError(
            f"Unexpected TP response for axis {ax}: {'; '.join(attempts)}"
        )

    # ---------------------------
    # Per-axis motion profile
    # ---------------------------
    def _query_required_float(self, command: str) -> float:
        raw = self._query(command)
        cleaned = self._clean_resp(str(raw)).replace(",", " ")
        for candidate in (cleaned, str(raw).strip()):
            for token in candidate.split():
                try:
                    value = float(token)
                except ValueError:
                    continue
                if math.isfinite(value):
                    return value
        raise RuntimeError(f"ESP300 returned no numeric value for {command}: {raw!r}")

    def get_stage_id(self, *, axis: Optional[int] = None) -> str:
        ax = int(self._axis if axis is None else axis)
        raw = str(self._query(f"{ax}ID?")).strip()
        cleaned = self._clean_resp(raw)
        return cleaned or raw or "UNKNOWN"

    def get_motion_profile(self, *, axis: Optional[int] = None) -> ESP300MotionProfile:
        ax = int(self._axis if axis is None else axis)
        return ESP300MotionProfile(
            axis=ax,
            stage_id=self.get_stage_id(axis=ax),
            velocity=self._query_required_float(f"{ax}VA?"),
            max_velocity=self._query_required_float(f"{ax}VU?"),
            acceleration=self._query_required_float(f"{ax}AC?"),
            max_acceleration=self._query_required_float(f"{ax}AU?"),
            deceleration=self._query_required_float(f"{ax}AG?"),
        )

    def apply_fast_safe_motion_profile(
        self,
        *,
        axis: Optional[int] = None,
        velocity_fraction: float = 1.0,
        acceleration_fraction: float = 0.5,
    ) -> ESP300MotionProfile:
        """Derive an operating profile from this axis's configured maxima.

        Maximum values are discovered, never copied from another axis.  The
        profile is changed only while the selected axis reports motion done.
        Successful readback is authoritative.
        """
        ax = int(self._axis if axis is None else axis)
        velocity_fraction = float(velocity_fraction)
        acceleration_fraction = float(acceleration_fraction)
        if not 0.0 < velocity_fraction <= 1.0:
            raise ValueError("velocity_fraction must be greater than 0 and no more than 1")
        if not 0.0 < acceleration_fraction <= 1.0:
            raise ValueError("acceleration_fraction must be greater than 0 and no more than 1")
        if not self.is_motion_done(axis=ax):
            raise RuntimeError(f"ESP300 axis {ax} must be stopped before changing its motion profile")

        discovered = self.get_motion_profile(axis=ax)
        if discovered.max_velocity <= 0 or discovered.max_acceleration <= 0:
            raise RuntimeError(
                f"ESP300 axis {ax} reported invalid motion limits: "
                f"VU={discovered.max_velocity:g}, AU={discovered.max_acceleration:g}"
            )
        velocity = discovered.max_velocity * velocity_fraction
        acceleration = discovered.max_acceleration * acceleration_fraction
        deceleration = discovered.max_acceleration * acceleration_fraction

        # Keep the controller's discovered limits, then set operating values.
        # This order prevents a requested operating value from exceeding its
        # corresponding programmed maximum.
        for command, value in (
            ("VU", discovered.max_velocity),
            ("AU", discovered.max_acceleration),
            ("VA", velocity),
            ("AC", acceleration),
            ("AG", deceleration),
        ):
            self._write(f"{ax}{command}{value:g}")

        verified = self.get_motion_profile(axis=ax)
        expected = {
            "max_velocity": discovered.max_velocity,
            "max_acceleration": discovered.max_acceleration,
            "velocity": velocity,
            "acceleration": acceleration,
            "deceleration": deceleration,
        }
        mismatches = [
            f"{name} expected {value:g}, read {getattr(verified, name):g}"
            for name, value in expected.items()
            if not math.isclose(getattr(verified, name), value, rel_tol=1e-9, abs_tol=1e-9)
        ]
        if mismatches:
            raise RuntimeError(
                f"ESP300 axis {ax} motion-profile verification failed: " + "; ".join(mismatches)
            )
        return verified

    def save_settings(self) -> None:
        """Persist the complete ESP300 controller configuration to flash."""
        self._write("SM")

    # ---------------------------
    # Discovery Helpers
    # ---------------------------
    def _clean_resp(self, s: str) -> str:
        """Trim, strip echoes (like '1TP0.000') and controller prompts."""
        if s is None:
            return ""
        s = str(s).strip()
        # Drop obvious echoes of the command 
        for tag in ("TP", "PR", "ID", "TS", "MD", "QM", "?"):
            # keep numeric payload to the right of the last tag, if any
            if tag in s:
                s = s.split(tag)[-1].strip()
        return s

    def _try_query_float(self, cmd: str) -> tuple[bool, float]:
        """Query and parse a float; tolerate junk around the number."""
        try:
            raw = self._query(cmd)
            s = self._clean_resp(raw).replace(",", " ")
            # pick first token that parses as float
            for tok in s.split():
                try:
                    return True, float(tok)
                except Exception:
                    continue
            return False, 0.0
        except Exception:
            return False, 0.0

    def _try_query_text(self, cmd: str) -> tuple[bool, str]:
        """Query that should return a non-empty, non-echo text."""
        try:
            raw = self._query(cmd)
            s = self._clean_resp(raw)
            if not s:
                return False, ""
            # Filter obvious error/echo markers or single digits
            if s in ("0", "1", "?") or "ERR" in s.upper():
                return False, ""
            return True, s
        except Exception:
            return False, ""

    # ---------------------------
    # Public Discovery
    # ---------------------------
    def get_axes(self, conservative: bool = True, *, max_axes: int = 3) -> list[int]:
        """Read installed drivers and ESP stages, without changing motor state.

        ESP300/301 ``ZU`` reports driver bits 0..2 and stage bits 8..10.
        Some ESP300 and ESP301 controllers return 0H despite attached stages.
        An all-zero bitmap therefore uses a bounded QM?/ID? scan only after
        native VE? identity confirms the configured model. Unavailable ZU
        responses also use the bounded scan, requiring motor and stage proof.
        Neither a configured axis nor a timeout establishes stage presence.
        """
        limit = min(3, int(max_axes))
        if limit < 1:
            raise ValueError("max_axes must be positive")
        try:
            payload = self._discovery_payload(self._query("ZU"), "ZU")
            if not re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]+[hH]?", payload):
                raise ValueError("Invalid ZU bitmap")
            bitmap = int(payload.rstrip("hH"), 16)
            if not 0 <= bitmap <= 0xFFFFFFFF:
                raise ValueError("Invalid ZU bitmap range")
        except Exception:
            return self._legacy_axes(limit)

        if bitmap == 0 and self._has_verified_native_identity():
            # Observed on ESP300 3.09 and ESP301 1.1.1: a version whitelist
            # cannot establish reliable ZU support. Require controller identity
            # plus per-axis corroboration instead of interpreting zero as proof
            # of absence or assuming an axis exists.
            # Even for conservative=False, require actual stage corroboration.
            return self._legacy_axes(limit)

        return [
            ax for ax in range(1, limit + 1)
            if bitmap & (1 << (ax - 1))
            and (not conservative or bitmap & (1 << (ax + 7)))
        ]

    def _has_verified_native_identity(self) -> bool:
        return bool(re.fullmatch(
            rf"{re.escape(self.model)}\s+Version\s+\d+(?:\.\d+)+"
            r"(?:\s+\d{1,2}/\d{1,2}/\d{2,4})?",
            self.idn(), re.IGNORECASE,
        ))

    @staticmethod
    def _discovery_payload(response: str, command: str) -> str:
        """Remove only this command's echo, never pieces of a stage name."""
        payload = str(response).strip()
        if payload.upper().startswith(command.upper()):
            payload = payload[len(command):].strip()
        return payload

    def _legacy_axes(self, limit: int) -> list[int]:
        found = []
        absent = (
            "UNKNOWN", "UNCONFIGURED", "NO STAGE", "NO MOTOR", "NOT CONNECTED",
            "DISCONNECTED", "NOT DETECTED", "UNAVAILABLE", "UNDEFINED", "NO DEVICE",
            "NOT PRESENT", "NO AXIS", "OUT OF RANGE", "NOT AVAILABLE", "MISSING",
            "ERROR", "ERR",
        )
        for ax in range(1, limit + 1):
            try:
                qm = f"{ax}QM?"
                motor = self._discovery_payload(self._query(qm), qm)
                # Motor type is an integer enum, never a float or error number.
                if motor not in ("1", "2", "3", "4"):
                    continue
                command = f"{ax}ID?"
                stage = self._discovery_payload(self._query(command), command)
                upper = stage.upper()
                if (
                    not stage or upper in (
                        "NONE", "NULL", "N/A", "?", "ID", "ID?", "NAN", "INF",
                        "+INF", "-INF", "INFINITY", "+INFINITY", "-INFINITY",
                    )
                    or any(marker in upper for marker in absent)
                    or not re.search(r"[a-zA-Z]", stage)
                    or "?" in stage or "\n" in stage or "\r" in stage
                ):
                    continue
                found.append(ax)
            except Exception:
                continue
        return found

    def motor_on_all(self, axes=None):
        axes = axes or self.get_axes(conservative=True)
        for a in axes:
            try:
                self.motor_on(axis=a)
            except Exception:
                pass

    def idn(self) -> str:
        if self._identity is not None:
            return self._identity
        try:
            identity = self._discovery_payload(self._query("VE?"), "VE?")
            if identity and identity.upper() != "UNKNOWN":
                self._identity = identity
            return identity or "UNKNOWN"
        except Exception:
            return "UNKNOWN"
