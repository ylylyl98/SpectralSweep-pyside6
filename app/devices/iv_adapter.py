from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any, Tuple, Optional, Dict, Callable, Type
import math
import threading
import time
import iv_automation


class SMUCommunicationError(RuntimeError):
    """A structured, role-specific SMU communication failure."""

    def __init__(
        self,
        message: str,
        *,
        role: Optional[str] = None,
        address: Optional[str] = None,
        operation: Optional[str] = None,
        command: Optional[str] = None,
        timeout_ms: Optional[int] = None,
        diagnosis: Optional[Dict[str, Any]] = None,
        recent_operations: Optional[list] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.role = role
        self.address = address
        self.operation = operation
        self.command = command
        self.timeout_ms = timeout_ms
        self.diagnosis = dict(diagnosis or {})
        self.recent_operations = list(recent_operations or [])
        self.context = dict(context or {})

    def to_incident_dict(self) -> Dict[str, Any]:
        return {
            "error_type": type(self).__name__,
            "error": str(self),
            "role": self.role,
            "address": self.address,
            "operation": self.operation,
            "command": self.command,
            "timeout_ms": self.timeout_ms,
            "diagnosis": dict(self.diagnosis),
            "recent_operations": list(self.recent_operations),
            "context": dict(self.context),
        }


class IVDevice:
    """
    Thin adapter over iv_automation.IVSetup with role awareness.
    Roles may be missing: Vbg, Vtg, Vbias. We only operate on roles that exist.
    """

    def __init__(
        self,
        setup: iv_automation.IVSetup,
        role_map: Optional[Dict[str, Optional[str]]] = None,  # e.g. {"Vbg": "...", "Vtg": "...", "Vbias": "..."}
    ):
        self.setup = setup
        self.role_map = role_map or {"Vbg": None, "Vtg": None, "Vbias": None}
        self._io_lock = threading.RLock()
        self._operation_context: Dict[str, Any] = {}
        self._operation_history = deque(maxlen=30)
        self._role_health: Dict[str, str] = {
            role: "ready" for role in ("Vbg", "Vtg", "Vbias")
        }
        self._health_transitions: Dict[str, list] = {
            role: [] for role in ("Vbg", "Vtg", "Vbias")
        }
        self._baseline_esr: Dict[str, Optional[int]] = {}
        self._last_communication_error: Optional[SMUCommunicationError] = None

    # ---------------- internal helpers ----------------

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    @staticmethod
    def _is_timeout_error(exc: BaseException) -> bool:
        current: Optional[BaseException] = exc
        while current is not None:
            text = f"{type(current).__name__}: {current}".lower()
            code = str(getattr(current, "error_code", "")).lower()
            if "timeout" in text or "timed out" in text or "vi_error_tmo" in code:
                return True
            current = current.__cause__ or current.__context__
        return False

    @staticmethod
    def _instrument_timeout_ms(inst) -> Optional[int]:
        candidates = (
            getattr(inst, "timeout", None),
            getattr(getattr(inst, "my_instr", None), "timeout", None),
        )
        for value in candidates:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    def set_operation_context(self, **context: Any) -> None:
        """Attach run/frame metadata to subsequent SMU operations."""
        with self._io_lock:
            self._operation_context = {
                str(key): value for key, value in context.items() if value is not None
            }

    def clear_operation_context(self) -> None:
        with self._io_lock:
            self._operation_context = {}

    @property
    def requires_reconnect(self) -> bool:
        return any(
            state != "ready"
            for role, state in self._role_health.items()
            if self.role_map.get(role)
        )

    @property
    def health_states(self) -> Dict[str, str]:
        return dict(self._role_health)

    @property
    def last_communication_error(self) -> Optional[SMUCommunicationError]:
        return self._last_communication_error

    def _set_health(self, role: str, state: str, reason: str) -> None:
        role = str(role)
        old_state = self._role_health.get(role, "ready")
        if old_state == state and self._health_transitions.get(role):
            return
        self._role_health[role] = state
        self._health_transitions.setdefault(role, []).append({
            "timestamp_utc": self._utc_now(),
            "from": old_state,
            "to": state,
            "reason": str(reason),
        })

    def _instrument_for_role(self, role: str):
        key = f"measured_{role}"
        try:
            ycc = getattr(self.setup, "y_channel_collection", None)
            if ycc is not None:
                inst = ycc.get_instrument(key)
                if inst is not None:
                    return inst
        except Exception:
            pass
        try:
            xcc = getattr(self.setup, "x_channel_collection", None)
            if xcc is not None:
                return xcc.get_instrument(role)
        except Exception:
            pass
        return None

    def _assert_role_ready(self, role: str, inst=None) -> None:
        state = self._role_health.get(role, "ready")
        if state == "ready":
            return
        if (
            self._last_communication_error is not None
            and self._last_communication_error.role == role
        ):
            raise self._last_communication_error
        address = getattr(inst, "address", None) or self.role_map.get(role)
        diagnosis = {
            "classification": state,
            "summary": (
                "This SMU is quarantined after a communication failure. "
                "Disconnect and reconnect the SMUs before another run."
            ),
            "state_transitions": list(self._health_transitions.get(role, [])),
        }
        raise SMUCommunicationError(
            f"{role} on {address or 'unknown address'} requires reconnect/reinitialization",
            role=role,
            address=address,
            operation="blocked_after_failure",
            diagnosis=diagnosis,
            recent_operations=self.recent_operations(role=role),
            context=self._operation_context,
        )

    def _record_operation(
        self,
        *,
        role: str,
        address: str,
        operation: str,
        command: str,
        status: str,
        started: float,
        started_at_utc: str,
        error: str = "",
        setpoint: Optional[float] = None,
    ) -> Dict[str, Any]:
        finished_at_utc = self._utc_now()
        item = {
            "timestamp_utc": finished_at_utc,
            "started_at_utc": started_at_utc,
            "finished_at_utc": finished_at_utc,
            "role": role,
            "address": address,
            "operation": operation,
            "command": command,
            "status": status,
            "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
            "context": dict(self._operation_context),
        }
        if error:
            item["error"] = error
        if setpoint is not None:
            item["setpoint_V"] = float(setpoint)
        self._operation_history.append(item)
        return item

    def recent_operations(self, *, role: Optional[str] = None) -> list:
        items = list(self._operation_history)
        if role is not None:
            items = [item for item in items if item.get("role") == role]
        return items

    def establish_health_baseline(
        self, *, strict: bool = True
    ) -> Dict[str, str]:
        """
        Destructively read ESR once after connection.

        This clears a pre-existing Power-On bit so a later bit-7 report is
        evidence that the instrument restarted after the connection baseline.

        Returns {role: error_message} for every role whose baseline query
        failed (empty when all succeeded).  When strict=True the first
        failure raises instead of being collected.
        """
        seen = set()
        failures: Dict[str, str] = {}
        for role in ("Vbg", "Vtg", "Vbias"):
            if not self.has_role(role):
                continue
            inst = self._instrument_for_role(role)
            if inst is None or id(inst) in seen:
                continue
            seen.add(id(inst))
            try:
                raw = inst.query("*ESR?")
                self._baseline_esr[role] = int(float(str(raw).strip()))
                self._set_health(role, "ready", "Connection health baseline established.")
            except Exception as exc:
                self._baseline_esr[role] = None
                failures[role] = f"{type(exc).__name__}: {exc}"
                if strict:
                    address = getattr(inst, "address", self.role_map.get(role))
                    raise SMUCommunicationError(
                        f"{role} baseline query failed on {address}: {exc}",
                        role=role,
                        address=address,
                        operation="connection_baseline",
                        command="*ESR?",
                        timeout_ms=self._instrument_timeout_ms(inst),
                        context=self._operation_context,
                    ) from exc
        return failures

    def _diagnose_after_failure(
        self,
        role: str,
        inst,
        failure: BaseException,
    ) -> Dict[str, Any]:
        """
        Read-only diagnosis after the failed VISA call has unwound.

        The diagnostic timeout is shortened and the output is never enabled.
        *ESR? and :SYST:ERR? are destructive reads, which is recorded here.
        """
        address = getattr(inst, "address", self.role_map.get(role))
        resource = getattr(inst, "my_instr", None)
        old_timeout = getattr(resource, "timeout", None) if resource is not None else None
        timeout_changed = False
        if resource is not None and old_timeout is not None:
            try:
                resource.timeout = min(max(int(old_timeout), 250), 1000)
                timeout_changed = True
            except Exception:
                pass

        responses: Dict[str, Any] = {}
        errors: Dict[str, str] = {}
        try:
            query = getattr(inst, "query", None)
            if not callable(query):
                errors["*IDN?"] = "Instrument wrapper does not provide query()."
            else:
                try:
                    responses["*IDN?"] = str(query("*IDN?")).strip()
                except Exception as exc:
                    errors["*IDN?"] = f"{type(exc).__name__}: {exc}"

                if "*IDN?" in responses:
                    for command in ("*ESR?", ":OUTP?", ":SYST:ERR?"):
                        try:
                            responses[command] = str(query(command)).strip()
                        except Exception as exc:
                            errors[command] = f"{type(exc).__name__}: {exc}"
        finally:
            if timeout_changed:
                try:
                    resource.timeout = old_timeout
                except Exception:
                    pass

        esr = None
        try:
            esr = int(float(responses.get("*ESR?", "")))
        except (TypeError, ValueError):
            pass
        power_on_bit = bool(esr is not None and esr & 0x80)

        output_on = None
        output_raw = str(responses.get(":OUTP?", "")).strip().upper()
        if output_raw in {"0", "OFF"}:
            output_on = False
        elif output_raw in {"1", "ON"}:
            output_on = True

        responded = "*IDN?" in responses
        timed_out = self._is_timeout_error(failure)
        if not responded:
            classification = "unreachable"
            summary = (
                f"{role} did not answer the post-failure identity query; it may "
                "still be unpowered, rebooting, disconnected, or unreachable on GPIB."
            )
            state = "unreachable"
        elif power_on_bit:
            classification = "power_cycle_detected"
            summary = (
                f"{role} responded again and ESR bit 7 (Power On) is set. "
                "The instrument lost/restarted power after connection and must be reinitialized."
            )
            state = "recovered_reinit_required"
        elif output_on is False:
            classification = "output_off_after_failure"
            summary = (
                f"{role} responded again but its source output is OFF. A power "
                "restart or another output-off event is suspected; reconnect is required."
            )
            state = "recovered_reinit_required"
        else:
            classification = "communication_fault"
            summary = (
                f"{role} responded to diagnostics after the failed operation, "
                "but a power cycle was not proven. Reconnect is required before continuing."
            )
            state = "reinit_required"

        self._set_health(
            role,
            "timeout" if timed_out else "communication_error",
            f"{type(failure).__name__}: {failure}",
        )
        if power_on_bit:
            self._set_health(
                role,
                "power_lost",
                "ESR bit 7 indicates the instrument restarted after the connection baseline.",
            )
        self._set_health(role, state, summary)
        return {
            "classification": classification,
            "summary": summary,
            "responded_after_failure": responded,
            "timed_out": timed_out,
            "identity": responses.get("*IDN?"),
            "connection_baseline_esr": self._baseline_esr.get(role),
            "esr": esr,
            "power_on_bit_set": power_on_bit,
            "output_on": output_on,
            "system_error": responses.get(":SYST:ERR?"),
            "query_responses": responses,
            "query_errors": errors,
            "destructive_queries": ["*ESR?", ":SYST:ERR?"],
            "state_transitions": list(self._health_transitions.get(role, [])),
            "address": address,
        }

    def _execute_io(
        self,
        *,
        role: str,
        inst,
        operation: str,
        command: str,
        action: Callable[[], Any],
        setpoint: Optional[float] = None,
    ) -> Any:
        address = str(getattr(inst, "address", self.role_map.get(role) or "unknown address"))
        with self._io_lock:
            self._assert_role_ready(role, inst)
            started = time.monotonic()
            started_at_utc = self._utc_now()
            try:
                result = action()
            except SMUCommunicationError:
                raise
            except Exception as exc:
                self._record_operation(
                    role=role,
                    address=address,
                    operation=operation,
                    command=command,
                    status="failed",
                    started=started,
                    started_at_utc=started_at_utc,
                    error=f"{type(exc).__name__}: {exc}",
                    setpoint=setpoint,
                )
                diagnosis = self._diagnose_after_failure(role, inst, exc)
                timeout_ms = self._instrument_timeout_ms(inst)
                timeout_text = (
                    f" after {timeout_ms} ms" if timeout_ms is not None else ""
                )
                communication_error = SMUCommunicationError(
                    f"{role} {operation} failed on {address}{timeout_text}: {exc}",
                    role=role,
                    address=address,
                    operation=operation,
                    command=command,
                    timeout_ms=timeout_ms,
                    diagnosis=diagnosis,
                    recent_operations=self.recent_operations(role=role),
                    context=self._operation_context,
                )
                self._last_communication_error = communication_error
                raise communication_error from exc
            self._record_operation(
                role=role,
                address=address,
                operation=operation,
                command=command,
                status="ok",
                started=started,
                started_at_utc=started_at_utc,
                setpoint=setpoint,
            )
            return result

    def has_role(self, name: str) -> bool:
        """
        Return True if this role is mapped OR can be auto-detected from the setup.
        This avoids "no Vbg role configured" when the channel exists but role_map wasn't passed.
        """
        if bool(self.role_map.get(name)):
            return True

        # Auto-detect if the setup can read this x channel name.
        try:
            fn = getattr(self.setup, "get_single_x_value", None)
            if callable(fn):
                _ = fn(name)  # raises if missing
                return True
        except Exception:
            pass

        return False

    def role_is_available(self, name: str) -> bool:
        """Return whether a role has a real instrument-backed channel."""
        return self.has_role(name) and self._instrument_for_role(name) is not None

    def _write_x_only(self, values: Dict[str, float]) -> bool:
        """
        Fast path: update X setpoints and call each owning instrument.write_x()
        WITHOUT creating sweeps or reading Y channels.
        """
        setup = getattr(self, "setup", None)
        xcc = getattr(setup, "x_channel_collection", None) if setup else None
        if xcc is None:
            return False
        if not (hasattr(xcc, "send_x") and hasattr(xcc, "get_instrument")):
            return False

        insts = []
        seen = set()

        for name, val in (values or {}).items():
            if val is None:
                continue
            try:
                xcc.send_x(str(name), float(val))  # updates cache + instrument.receive_x()
                inst = xcc.get_instrument(str(name))
                if inst is not None:
                    k = id(inst)
                    if k not in seen:
                        seen.add(k)
                        insts.append((str(name), float(val), inst))
            except Exception as exc:
                raise SMUCommunicationError(
                    f"{name} setpoint preparation failed: {exc}",
                    role=str(name),
                    address=self.role_map.get(str(name)),
                    operation="prepare_set_voltage",
                    command=f":SOUR:VOLT:LEV {float(val):.9g}",
                    context=self._operation_context,
                ) from exc

        if not insts:
            return False

        for role, value, inst in insts:
            self._execute_io(
                role=role,
                inst=inst,
                operation="set_voltage",
                command=f":SOUR:VOLT:LEV {value:.9g}",
                action=inst.write_x,
                setpoint=value,
            )

        return True

    def _set_x_fast(self, name: str, value: float) -> bool:
        """
        Fast set (no ramp, no readback). Falls back to setup.x_goto if needed.
        """
        if not self.has_role(name):
            return False

        ok = self._write_x_only({name: float(value)})
        if ok:
            return True

        try:
            self.setup.x_goto(name, float(value), delta=0, delay=0.0, print_steps=False)
            return True
        except SMUCommunicationError:
            raise
        except Exception:
            return False

    def _safe_x_goto(self, name: str, value: float, delay_s: float = 0.05) -> bool:
        """
        Jump to value (no ramp). Uses fast write_x_only when possible.
        """
        ok = self._set_x_fast(name, float(value))
        if not ok:
            return False
        if delay_s and float(delay_s) > 0:
            time.sleep(float(delay_s))
        return True

    def _check_stop(
        self,
        stop_cb: Optional[Callable[[], bool]],
        stop_exc: Optional[Type[BaseException]],
    ) -> None:
        """
        If stop_cb requests stop, raise stop_exc() (or RuntimeError) to unwind safely.
        This is designed to be called frequently inside ramp loops.
        """
        if not callable(stop_cb):
            return
        try:
            requested = bool(stop_cb())
        except Exception:
            requested = False

        if requested:
            if stop_exc is not None:
                raise stop_exc()
            raise RuntimeError("StopRequested")

    def _safe_read_x(self, name: str) -> float:
        """Best-effort read of current x (setpoint)."""
        nan = float("nan")
        try:
            fn = getattr(self.setup, "get_single_x_value", None)
            if callable(fn):
                return float(fn(name))
        except Exception:
            pass
        return nan

    def _update_x_cache(self, name: str, value: Optional[float]) -> None:
        """Keep cached setpoints aligned with a confirmed live hardware read."""
        if value is None:
            return
        try:
            value_f = float(value)
        except Exception:
            return
        if not math.isfinite(value_f):
            return
        try:
            xcc = getattr(self.setup, "x_channel_collection", None)
            if xcc is not None and hasattr(xcc, "send_x"):
                xcc.send_x(str(name), value_f)
        except Exception:
            pass

    def _safe_read_live_x(self, name: str) -> float:
        """Best-effort live hardware read for one axis, falling back to nan."""
        nan = float("nan")
        try:
            if name in ("Vbg", "Vtg", "Vbias"):
                value = self._read_measured_role(name)
                return float(value) if value is not None else nan
        except Exception:
            pass
        return nan

    def _ramp_axis_stopaware(
        self,
        name: str,
        target: float,
        step: float,
        delay_s: float = 0.05,
        *,
        start_value: Optional[float] = None,
        stop_cb: Optional[Callable[[], bool]] = None,
        stop_exc: Optional[Type[BaseException]] = None,
    ) -> bool:
        """
        Manual stepped ramp so we can poll stop_cb each step.
        Fast per-step write (no sweep construction / no per-step READ?).
        """
        if not self.has_role(name):
            return False

        target = float(target)
        step = float(abs(step)) if step and step > 0 else 0.0

        # jump mode
        if step <= 0:
            self._check_stop(stop_cb, stop_exc)
            if not self._set_x_fast(name, target):
                raise SMUCommunicationError(
                    f"{name} set failed because no mapped instrument accepted the command",
                    role=name,
                    address=self.role_map.get(name),
                    operation="set_voltage",
                    command=f":SOUR:VOLT:LEV {target:.9g}",
                    context=self._operation_context,
                )
            if delay_s and float(delay_s) > 0:
                time.sleep(float(delay_s))
            return True

        try:
            x0 = float(start_value) if start_value is not None else float("nan")
        except (TypeError, ValueError):
            x0 = float("nan")
        if not math.isfinite(x0):
            x0 = self._safe_read_live_x(name)
        if not math.isfinite(x0):
            x0 = self._safe_read_x(name)
        if not math.isfinite(x0):
            x0 = 0.0

        dx = target - float(x0)
        ratio = abs(dx) / max(step, 1e-12)
        n = max(1, int(math.ceil(ratio - 1e-12)))

        for i in range(1, n + 1):
            self._check_stop(stop_cb, stop_exc)
            f = i / n
            xi = target if i == n else float(x0) + dx * f
            if not self._set_x_fast(name, float(xi)):
                raise SMUCommunicationError(
                    f"{name} ramp failed because no mapped instrument accepted the command",
                    role=name,
                    address=self.role_map.get(name),
                    operation="set_voltage",
                    command=f":SOUR:VOLT:LEV {float(xi):.9g}",
                    context=self._operation_context,
                )
            if delay_s and float(delay_s) > 0:
                time.sleep(float(delay_s))

        # The final loop iteration writes the exact target; do not send the
        # same command and wait a second time.
        return True

    def ramp_to(
        self,
        name: str,
        value: float,
        step: float,
        delay_s: float = 0.05,
        *,
        start_value: Optional[float] = None,
        stop_cb: Optional[Callable[[], bool]] = None,
        stop_exc: Optional[Type[BaseException]] = None,
    ) -> bool:
        """
        Safely ramp an axis to 'value' using manual stepped ramp (stop-aware).
        Returns False if that role does not exist.
        """
        if not self.has_role(name):
            return False
        step = abs(step) if step and step > 0 else 0.0
        return self._ramp_axis_stopaware(
            name,
            float(value),
            float(step),
            float(delay_s),
            start_value=start_value,
            stop_cb=stop_cb,
            stop_exc=stop_exc,
        )

    def set_role_voltage_fast(
        self, role: str, target: float, delay_s: float = 0.0
    ) -> bool:
        """Set one role once, using a caller-maintained confirmed setpoint."""
        if role not in ("Vbg", "Vtg", "Vbias"):
            raise ValueError(f"Unknown Keithley role: {role}")
        if not self.has_role(role):
            return False
        target = float(target)
        if not math.isfinite(target):
            raise ValueError(f"Invalid {role} target: {target}")
        if not self._safe_x_goto(role, target, delay_s):
            raise SMUCommunicationError(
                f"{role} set failed because no mapped instrument accepted the command",
                role=role,
                address=self.role_map.get(role),
                operation="set_voltage",
                command=f":SOUR:VOLT:LEV {target:.9g}",
                context=self._operation_context,
            )
        return True

    def _ramp_gates_together(
        self,
        Vbg: float,
        Vtg: float,
        ramp_step: float,
        delay_s: float,
        *,
        stop_cb: Optional[Callable[[], bool]] = None,
        stop_exc: Optional[Type[BaseException]] = None,
    ) -> None:
        """
        Interleaved ramp: at each step, set BOTH Vbg and Vtg, then sleep once.
        Fast per-step write (no sweep construction / no per-step READ?).
        """
        # start points: prefer measured, fall back to x setpoint
        bg0, tg0 = self.read_current_gates()

        if not math.isfinite(bg0):
            bg0 = self._safe_read_x("Vbg")
        if not math.isfinite(tg0):
            tg0 = self._safe_read_x("Vtg")

        # If we still can't read both starts reliably, fall back to per-axis ramps
        if not (math.isfinite(bg0) and math.isfinite(tg0)):
            self.ramp_to("Vbg", Vbg, ramp_step, delay_s, stop_cb=stop_cb, stop_exc=stop_exc)
            self.ramp_to("Vtg", Vtg, ramp_step, delay_s, stop_cb=stop_cb, stop_exc=stop_exc)
            return

        bg1 = float(Vbg)
        tg1 = float(Vtg)

        d_bg = bg1 - bg0
        d_tg = tg1 - tg0

        step = float(abs(ramp_step)) if ramp_step and ramp_step > 0 else 0.0
        if step <= 0:
            self._check_stop(stop_cb, stop_exc)
            self._write_x_only({"Vbg": bg1, "Vtg": tg1})
            if delay_s and float(delay_s) > 0:
                time.sleep(float(delay_s))
            return

        max_d = max(abs(d_bg), abs(d_tg))
        n = max(1, int(math.ceil(max_d / step)))

        for i in range(1, n + 1):
            self._check_stop(stop_cb, stop_exc)
            f = i / n
            bg_i = bg0 + d_bg * f
            tg_i = tg0 + d_tg * f
            self._write_x_only({"Vbg": float(bg_i), "Vtg": float(tg_i)})
            if delay_s and float(delay_s) > 0:
                time.sleep(float(delay_s))

        self._check_stop(stop_cb, stop_exc)
        self._write_x_only({"Vbg": bg1, "Vtg": tg1})
        if delay_s and float(delay_s) > 0:
            time.sleep(float(delay_s))

    # ---------------- public API used by UI/steps ----------------

    def set_gates(
        self,
        Vbg: Optional[float] = None,
        Vtg: Optional[float] = None,
        delay_s: float = 0.05,
        ramp_step: Optional[float] = 0.1,
        *,
        stop_cb: Optional[Callable[[], bool]] = None,
        stop_exc: Optional[Type[BaseException]] = None,
    ):
        """
        Set gate voltages. If ramp_step>0, move in steps; otherwise jump.

        If BOTH Vbg and Vtg are provided AND both roles exist AND ramp_step>0,
        ramp together (interleaved). Stop-aware via stop_cb.
        """
        if (
            Vbg is not None
            and Vtg is not None
            and self.has_role("Vbg")
            and self.has_role("Vtg")
            and ramp_step is not None
            and ramp_step > 0
        ):
            self._ramp_gates_together(
                float(Vbg),
                float(Vtg),
                float(ramp_step),
                float(delay_s),
                stop_cb=stop_cb,
                stop_exc=stop_exc,
            )
            return

        # Single gate / missing role / no ramp
        if Vbg is not None:
            if ramp_step and ramp_step > 0:
                self.ramp_to("Vbg", Vbg, ramp_step, delay_s, stop_cb=stop_cb, stop_exc=stop_exc)
            elif not self._safe_x_goto("Vbg", Vbg, delay_s):
                raise SMUCommunicationError(
                    "Vbg set failed because no mapped instrument accepted the command",
                    role="Vbg",
                    address=self.role_map.get("Vbg"),
                    operation="set_voltage",
                    command=f":SOUR:VOLT:LEV {float(Vbg):.9g}",
                    context=self._operation_context,
                )

        if Vtg is not None:
            if ramp_step and ramp_step > 0:
                self.ramp_to("Vtg", Vtg, ramp_step, delay_s, stop_cb=stop_cb, stop_exc=stop_exc)
            elif not self._safe_x_goto("Vtg", Vtg, delay_s):
                raise SMUCommunicationError(
                    "Vtg set failed because no mapped instrument accepted the command",
                    role="Vtg",
                    address=self.role_map.get("Vtg"),
                    operation="set_voltage",
                    command=f":SOUR:VOLT:LEV {float(Vtg):.9g}",
                    context=self._operation_context,
                )

    def set_bias(
        self,
        Vbias: Optional[float] = None,
        delay_s: float = 0.05,
        ramp_step: Optional[float] = 0.1,
        *,
        stop_cb: Optional[Callable[[], bool]] = None,
        stop_exc: Optional[Type[BaseException]] = None,
    ):
        """
        Set source-drain bias. If ramp_step>0, move in steps; otherwise jump.
        Stop-aware via stop_cb.
        """
        if Vbias is not None:
            if ramp_step and ramp_step > 0:
                self.ramp_to("Vbias", Vbias, ramp_step, delay_s, stop_cb=stop_cb, stop_exc=stop_exc)
            elif not self._safe_x_goto("Vbias", Vbias, delay_s):
                raise SMUCommunicationError(
                    "Vbias set failed because no mapped instrument accepted the command",
                    role="Vbias",
                    address=self.role_map.get("Vbias"),
                    operation="set_voltage",
                    command=f":SOUR:VOLT:LEV {float(Vbias):.9g}",
                    context=self._operation_context,
                )

    def read_leakages(self) -> Tuple[float, float]:
        """Return (Vbg_leakage, Vtg_leakage); if missing, return 0.0."""
        def _get(name: str) -> float:
            try:
                return float(self.setup.get_single_y_value(name))
            except Exception:
                return 0.0
        return _get("Vbg_leakage"), _get("Vtg_leakage")

    def _safe_read_y(self, key: str, *, strict: bool = False) -> Optional[float]:
        """
        Best-effort: force a hardware read for the instrument that owns this y-channel,
        then return the latest y value for that key.
        """
        try:
            ycc = getattr(self.setup, "y_channel_collection", None)
            if ycc is None:
                return None

            try:
                inst = ycc.get_instrument(key)
            except Exception:
                return None

            if inst is not None and hasattr(inst, "read_y"):
                try:
                    if key.startswith("Vbg"):
                        role = "Vbg"
                    elif key.startswith("Vtg"):
                        role = "Vtg"
                    elif key.startswith("Vbias"):
                        role = "Vbias"
                    else:
                        role = key
                    self._execute_io(
                        role=role,
                        inst=inst,
                        operation="read_current",
                        command=":READ?",
                        action=inst.read_y,
                    )
                except SMUCommunicationError:
                    raise
                except Exception as exc:
                    if strict:
                        address = getattr(inst, "address", "unknown address")
                        raise SMUCommunicationError(
                            f"{key} read failed on {address}: {exc}",
                            role=role,
                            address=address,
                            operation="read_current",
                            command=":READ?",
                            context=self._operation_context,
                        ) from exc
                    return None

            try:
                ycc.receive_y(key)
            except Exception:
                pass

            try:
                return float(self.setup.get_single_y_value(key))
            except Exception:
                return None
        except SMUCommunicationError:
            if strict:
                raise
            return None
        except Exception as exc:
            if strict:
                raise SMUCommunicationError(
                    f"{key} read failed: {exc}",
                    operation="read_current",
                    command=":READ?",
                    context=self._operation_context,
                ) from exc
            return None

    def read_currents(
        self, *, strict: bool = False
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """
        Return (Ibg, Itg, Ibias) in Amps.
        Keys come directly from iv_automation.py: <x_name>_leakage.
        """
        Ibg = self._safe_read_y("Vbg_leakage", strict=strict) if self.has_role("Vbg") else None
        Itg = self._safe_read_y("Vtg_leakage", strict=strict) if self.has_role("Vtg") else None
        Ib  = self._safe_read_y("Vbias_leakage", strict=strict) if self.has_role("Vbias") else None
        return Ibg, Itg, Ib

    def read_role_snapshot(
        self, role: str, *, strict: bool = False
    ) -> Tuple[Optional[float], Optional[float]]:
        """Read one Keithley once and return its (voltage, current)."""
        if not self.has_role(role):
            return None, None
        meas_key = f"measured_{role}"
        current_key = f"{role}_leakage"
        try:
            inst = self.setup.y_channel_collection.get_instrument(meas_key)
            if inst:
                self._execute_io(
                    role=role,
                    inst=inst,
                    operation="read_voltage",
                    command=":READ?",
                    action=inst.read_y,
                )
            self.setup.y_channel_collection.receive_y(meas_key)
            self.setup.y_channel_collection.receive_y(current_key)
            voltage = float(self.setup.get_single_y_value(meas_key))
            current = float(self.setup.get_single_y_value(current_key))
            self._update_x_cache(role, voltage)
            return voltage, current
        except SMUCommunicationError:
            if strict:
                raise
            return None, None
        except Exception as exc:
            if strict:
                address = getattr(locals().get("inst"), "address", "unknown address")
                raise SMUCommunicationError(
                    f"{role} read failed on {address}: {exc}",
                    role=role,
                    address=address,
                    operation="read_voltage",
                    command=":READ?",
                    context=self._operation_context,
                ) from exc
            return None, None

    def _read_measured_role(
        self, role: str, *, strict: bool = False
    ) -> Optional[float]:
        voltage, _current = self.read_role_snapshot(role, strict=strict)
        return voltage

    def read_current_bias(self, *, strict: bool = False) -> Optional[float]:
        if not self.has_role("Vbias"):
            return None
        return self._read_measured_role("Vbias", strict=strict)

    def read_role_voltage(
        self, role: str, *, strict: bool = False
    ) -> Optional[float]:
        """Read the live output voltage for one mapped Keithley role."""
        if role not in ("Vbg", "Vtg", "Vbias"):
            raise ValueError(f"Unknown Keithley role: {role}")
        return self._read_measured_role(role, strict=strict)

    def read_current_gates(self, *, strict: bool = False):
        """
        Forces a hardware read of 'measured_Vbg' and 'measured_Vtg'.
        Returns nan for any role that is not mapped / not readable.
        """
        nan = float("nan")
        bg_val = nan
        tg_val = nan

        if self.has_role("Vbg"):
            value = self._read_measured_role("Vbg", strict=strict)
            if value is not None:
                bg_val = float(value)
        if self.has_role("Vtg"):
            value = self._read_measured_role("Vtg", strict=strict)
            if value is not None:
                tg_val = float(value)

        return bg_val, tg_val

    def ramp_all_to_zero_report(
        self, ramp_step: float = 0.1, delay_s: float = 0.05
    ) -> Dict[str, Dict[str, Any]]:
        """Return a per-role cleanup result while isolating failed instruments."""
        report: Dict[str, Dict[str, Any]] = {}
        role_map = getattr(self, "role_map", {})
        role_health = getattr(self, "_role_health", {})
        for role in ("Vbias", "Vbg", "Vtg"):
            if not self.has_role(role):
                continue
            try:
                self.ramp_to(role, 0.0, ramp_step, delay_s)
                self.ramp_to(role, 0.0, 0.0, 0.0)
                report[role] = {
                    "status": "reached_zero",
                    "address": role_map.get(role),
                }
            except Exception as exc:
                status = (
                    "skipped_reconnect_required"
                    if role_health.get(role, "ready") != "ready"
                    else "failed"
                )
                report[role] = {
                    "status": status,
                    "address": role_map.get(role),
                    "error": str(exc),
                }
        return report

    def ramp_all_to_zero(self, ramp_step: float = 0.1, delay_s: float = 0.05):
        """Best-effort isolated ramps; one dead role cannot block the others."""
        report = self.ramp_all_to_zero_report(
            ramp_step=ramp_step,
            delay_s=delay_s,
        )
        errors = [
            f"{role}: {result.get('error', result.get('status', 'failed'))}"
            for role, result in report.items()
            if result.get("status") != "reached_zero"
        ]
        return errors
