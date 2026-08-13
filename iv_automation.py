import numpy as np, nidaqmx, time, pyvisa
import warnings
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Union
# connect to the instrument via pyvisa


class CustomError(Exception):
    def __init__(self, message="A custom error occurred"):
        self.message = message
        super().__init__(self.message)


def _classify_io_error(exc: BaseException) -> str:
    """Return TIMEOUT for VISA timeouts, otherwise FAILED."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if (
        "timeout" in text
        or "timed out" in text
        or "vi_error_tmo" in text
    ):
        return "TIMEOUT"
    return "FAILED"


class PyvisaInstrument:
    def __init__(
        self,
        address: str,
        name: str,
        termination: str,
        rm: pyvisa.ResourceManager,
        timeout_ms: int = 5000,
        trace_io: bool = False,
    ):
        # instrument address
        self.address = address
        self.name = name
        # r/w termination, '\r' or '\n'
        self.termination = termination
        # pyvisa resource manager
        self.rm = rm
        self.my_instr = None
        # PyVISA interprets None as an infinite timeout. A failed Keithley
        # would then trap the sweep thread forever and prevent Stop/cleanup.
        self.timeout = max(250, int(timeout_ms))
        self.x_indexes = {}
        self.y_indexes = {}
        self.x_values = np.array([])
        self.y_values = np.array([])
        # Structured, timestamped record of every VISA I/O operation.
        # Each entry: timestamp, address, op (WRITE/QUERY/READ/CLEAR),
        # command, elapsed_ms, status (ok/error), error.
        self.io_log: deque = deque(maxlen=300)
        # Named initialization stages: {stage, status, details, timestamp_utc}.
        self.stage_log: List[Dict[str, Any]] = []
        # Session parameters captured at connect time (timeout, terminations,
        # send_end, query_delay, chunk size, locking).
        self.session_params: Dict[str, Any] = {}
        # When True, every I/O operation (including VISA open and the
        # constructor-time recovery/*IDN? calls) is printed with a timestamp:
        #   [15:32:01.139] GPIB0::24::INSTR WRITE :SOUR:VOLT:RANG 20 -> OK 16 ms
        self.trace_io = bool(trace_io)
        self._closed = False

    # ------------------------------------------------------------------
    # I/O instrumentation
    # ------------------------------------------------------------------

    @staticmethod
    def _utc_now_ms() -> str:
        return datetime.now().strftime("%H:%M:%S.%f")[:-3]

    def log_stage(self, stage: str, status: str, details: Optional[dict] = None) -> dict:
        """Record one named initialization stage (tolerant of partial objects)."""
        entry = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "stage": str(stage),
            "status": str(status),
            "details": dict(details or {}),
        }
        log = getattr(self, "stage_log", None)
        if log is not None:
            try:
                log.append(entry)
            except Exception:
                pass
        return entry

    def _execute_io(self, op: str, command: str, fn: Callable[[], Any]):
        """Run one VISA operation with timestamp/elapsed/status instrumentation."""
        started = time.monotonic()
        started_at = self._utc_now_ms()
        entry = {
            "timestamp": started_at,
            "address": self.address,
            "op": op,
            "command": str(command),
            "status": "running",
        }
        try:
            result = fn()
        except Exception as exc:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            classification = _classify_io_error(exc)
            entry.update({
                "status": "error",
                "elapsed_ms": round(elapsed_ms, 1),
                "error": f"{type(exc).__name__}: {exc}",
                "classification": classification,
            })
            self.io_log.append(entry)
            if self.trace_io:
                print(
                    f"[{started_at}] {self.address} {op} {command} "
                    f"-> {classification} after {elapsed_ms:.0f} ms: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
            raise
        elapsed_ms = (time.monotonic() - started) * 1000.0
        entry.update({"status": "ok", "elapsed_ms": round(elapsed_ms, 1)})
        self.io_log.append(entry)
        if self.trace_io:
            print(
                f"[{started_at}] {self.address} {op} {command} "
                f"-> OK {elapsed_ms:.0f} ms",
                flush=True,
            )
        return result

    def recent_io(self, limit: int = 20) -> List[dict]:
        return list(self.io_log)[-max(0, int(limit)):]

    def last_io_entry(self, status: Optional[str] = None) -> Optional[dict]:
        """Last I/O entry, optionally filtered by status ('ok' or 'error')."""
        for entry in reversed(self.io_log):
            if status is None or entry.get("status") == status:
                return entry
        return None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def connect(self):
        if self.trace_io:
            print(
                f"[{self._utc_now_ms()}] {self.address} OPEN VISA "
                f"(timeout={self.timeout} ms) ...",
                flush=True,
            )
        try:
            self.my_instr = self.rm.open_resource(
                self.address, timeout=self.timeout
            )
        except Exception as exc:
            if self.trace_io:
                print(
                    f"[{self._utc_now_ms()}] {self.address} OPEN VISA FAILED: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
            raise
        if self.trace_io:
            print(
                f"[{self._utc_now_ms()}] {self.address} OPEN VISA OK",
                flush=True,
            )
        self.my_instr.read_termination = self.termination
        self.my_instr.write_termination = self.termination
        self._closed = False
        try:
            self.session_params = {
                "timeout_ms": getattr(self.my_instr, "timeout", None),
                "read_termination": getattr(self.my_instr, "read_termination", None),
                "write_termination": getattr(self.my_instr, "write_termination", None),
                "send_end": getattr(self.my_instr, "send_end", None),
                "query_delay": getattr(self.my_instr, "query_delay", None),
                "chunk_size": getattr(self.my_instr, "chunk_size", None),
                "locking": getattr(self.my_instr, "locking", None),
            }
        except Exception:
            self.session_params = {}

    def clear(self):
        """Issue a VISA-level clear (viClear) on the open session."""
        return self._execute_io("CLEAR", "VISA clear", lambda: self.my_instr.clear())

    def close(self):
        inst, self.my_instr = self.my_instr, None
        if inst is not None:
            try:
                inst.close()
            finally:
                self._closed = True

    def query(self, command: str, print_command=False, print_response=False):
        if print_command and not self.trace_io:
            print(command)
        response = self._execute_io(
            "QUERY", command, lambda: self.my_instr.query(command)
        )
        if print_response:
            print(response)
        return response

    def write(self, command: str, print_command=False):
        if print_command and not self.trace_io:
            print(command)
        return self._execute_io(
            "WRITE", command, lambda: self.my_instr.write(command)
        )

    def read(self, print_response=False):
        response = self._execute_io("READ", "<read>", lambda: self.my_instr.read())
        if print_response:
            print(response)
        return response

    def receive_x(self, variable: str, value: float):
        self.x_values[self.x_indexes[variable]] = value

    def send_y(self, variable: str):
        return self.y_values[self.y_indexes[variable]]


# ----------------------------------------------------------------------
# Central RSYN policy (shared by KeithControl and the diagnostic tool)
# ----------------------------------------------------------------------

# Models whose firmware wedges the GPIB interface for ~10 s when sent
# :SENS:CURR:PROT:RSYN (confirmed on the Keithley 2400 / A02, 2000-era).
# Kept for documentation / diagnostics; the automatic decision below is a
# denylist: only this model skips RSYN.
RSYN_SKIP_MODELS = frozenset({"2400"})


def _model_tokens(model: str) -> List[str]:
    """Normalize a model / *IDN? string into uppercase whitespace tokens."""
    return [
        token
        for token in str(model or "").upper().replace(",", " ").split()
        if token
    ]


def model_is_keithley_2400(model: str) -> bool:
    """
    True only for an exact MODEL 2400 token.

    Tolerates 'MODEL 2400', bare '2400', and raw *IDN? strings such as
    'KEITHLEY INSTRUMENTS INC.,MODEL 2400,...'.  Never matches '2401',
    '2400A', or unrelated models.
    """
    return "2400" in _model_tokens(model)


def should_send_rsyn(model: str, config_value: Optional[bool]) -> bool:
    """
    Central per-instrument RSYN policy.

      config_value True  -> force send (explicit opt-in, rsyn_enabled=True)
      config_value False -> force skip (explicit opt-out, rsyn_enabled=False)
      config_value None  -> automatic: skip only the Keithley MODEL 2400
                            (verified to wedge on RSYN); every other model,
                            including the MODEL 2401, keeps RSYN.
    """
    if config_value is True:
        return True
    if config_value is False:
        return False
    return not model_is_keithley_2400(model)


# monochromator control
class MonoControl(PyvisaInstrument):
    def __init__(self, address: str, name: str, rm: pyvisa.ResourceManager, initial_wl: float):
        super().__init__(address, name, '\r', rm)
        self.type = 'mono'
        self.connect()
        self.get_identity()
        self.setup_scan()
        self.wl_goto(initial_wl)
        wavelength = self.get_wl()
        self.x_indexes['wavelength'] = 0
        self.y_indexes['measured_wavelength'] = 0
        self.x_values = np.array([initial_wl])
        self.y_values = np.array([wavelength])

    def get_identity(self):
        self.query('MODEL', print_response=True)

    def get_wl(self):
        reading = self.query('?NM')
        wavelength = float(reading.split(' ')[1])
        return wavelength

    def setup_scan(self, speed=300):
        statement = '%.2f NM/MIN' % speed
        self.query(statement, print_command=True, print_response=True)

    def wl_goto(self, wavelength: float):
        statement = '%.2f GOTO' % wavelength
        # check the format of the data here
        self.query(statement)


    # write the x value to the physical instrument
    def write_x(self):
        self.wl_goto(self.x_values[0])

    # read the y value from the physical instrument
    def read_y(self):
        self.y_values[0] = self.get_wl()
        return


# Keithley 2400 control


class KeithControl(PyvisaInstrument):
    CURRENT_RANGES_A = (1e-6, 10e-6, 100e-6, 1e-3, 10e-3, 100e-3, 1.0)

    def __init__(
        self,
        address: str,
        name: str,
        variable_name: str,
        rm: pyvisa.ResourceManager,
        *,
        curr_compliance: float = 1e-6,
        volt_compliance: float = 20.0,
        timeout_ms: int = 5000,
        configure_on_connect: bool = True,
        recover_on_open: bool = True,
        rsyn_enabled: Optional[bool] = None,
        trace_io: bool = False,
    ):
        super().__init__(
            address,
            name,
            '\n',
            rm,
            timeout_ms=timeout_ms,
            trace_io=trace_io,
        )
        self.type = 'keithley'
        self.mode = 'unconfigured'
        self._curr_compliance_A = float(curr_compliance)
        self._volt_range_V = float(volt_compliance)
        self._curr_range_A = None
        # Parsed *IDN? fields and per-model capability state.
        self.identity_raw = ""
        self.identity: Dict[str, str] = {}
        self.model = ""
        self.firmware = ""
        self.capabilities: Dict[str, Optional[bool]] = {"rsyn_supported": None}
        self._rsyn_supported: Optional[bool] = None
        if rsyn_enabled is True:
            self.rsyn_policy = "forced_on"
        elif rsyn_enabled is False:
            self.rsyn_policy = "forced_off"
        else:
            self.rsyn_policy = "auto"
        self._last_system_errors: List[str] = []
        self.connect()
        try:
            if recover_on_open:
                # Establish a known communication state WITHOUT *RST (which
                # could change many instrument settings / output state
                # unexpectedly).
                self.recover_session()
            self.get_identity()
            # Configure exactly once. Previously construction used 1 µA and
            # the controller immediately configured the same SMU again.
            if configure_on_connect:
                self.set_volt_step(
                    curr_compliance=curr_compliance,
                    volt_compliance=volt_compliance,
                )
            self.x_indexes[variable_name] = 0
            self.y_indexes['measured_'+variable_name] = 0
            self.y_indexes[variable_name + '_leakage'] = 1
            volt, curr = self.read_curr() if configure_on_connect else (0.0, 0.0)
            self.x_values = np.array([volt])
            self.y_values = np.array([volt, curr])
        except Exception as exc:
            # Never leak a half-open VISA session (cleanup requirement).
            try:
                self.close()
            except Exception:
                pass
            try:
                exc._keithley_partial = self
            except Exception:
                pass
            raise

    def get_identity(self):
        raw = str(self.query('*IDN?')).strip()
        self.identity_raw = raw
        parts = [p.strip() for p in raw.split(',')]
        self.identity = {
            "manufacturer": parts[0] if len(parts) > 0 else "",
            "model": parts[1] if len(parts) > 1 else "",
            "serial": parts[2] if len(parts) > 2 else "",
            "firmware": parts[3] if len(parts) > 3 else "",
        }
        self.model = self.identity["model"]
        self.firmware = self.identity["firmware"]
        self.log_stage(
            "IDENTIFY",
            "ok",
            {"identity": raw, "model": self.model, "firmware": self.firmware},
        )
        print(raw)
        return raw

    def recover_session(self) -> None:
        """
        Conservative startup sequence: VISA clear + *CLS + :ABOR.

        Intentionally does NOT issue *RST; instrument settings that a previous
        experiment changed are only reset where this application needs them
        (source mode, trigger source, ranges), so a connected instrument is
        not silently reconfigured beyond the app's own initialization.
        """
        self.log_stage("CLEAR", "running")
        self.clear()
        self.write('*CLS')
        self.write(':ABOR')
        self.log_stage("CLEAR", "ok")

    def drain_system_errors(self, max_errors: int = 8) -> List[str]:
        """
        Drain :SYST:ERR? until '0,"No error"' (or the configured maximum).
        The complete result is kept on self._last_system_errors.
        """
        collected: List[str] = []
        for _ in range(max(1, int(max_errors))):
            raw = str(self.query(':SYST:ERR?')).strip()
            collected.append(raw)
            if self._is_no_error_response(raw):
                break
        self._last_system_errors = list(collected)
        return collected

    @staticmethod
    def _is_no_error_response(raw: str) -> bool:
        text = str(raw).strip().lower()
        return text.startswith("0") and "no error" in text

    @staticmethod
    def _looks_like_command_error(raw: str) -> bool:
        """Recognize SCPI 'invalid/undefined command' errors (e.g. 110/-113)."""
        text = str(raw).strip().lower()
        code = None
        try:
            code = int(float(text.split(",", 1)[0]))
        except (ValueError, IndexError, TypeError):
            pass
        if code in (110, -113, -110, -100):
            return True
        return any(
            token in text
            for token in ("command", "header", "undefined")
        )

    def _ensure_rsyn_capability(self) -> bool:
        """
        Decide whether to send :SENS:CURR:PROT:RSYN for this instrument.

        Never probe by sending on the Keithley 2400: its old firmware (e.g.
        A02) wedges the GPIB interface for ~10 seconds after receiving the
        command, so the probe itself would hang the connection.  The policy
        is centralized in should_send_rsyn() and evaluated independently per
        instrument instance:
          - "forced_on"  -> always send (cfg.smu.rsyn_enabled=True)
          - "forced_off" -> never send (cfg.smu.rsyn_enabled=False)
          - "auto"       -> skip only MODEL 2400; send for every other model.
        """
        if self._rsyn_supported is not None:
            return self._rsyn_supported
        policy = getattr(self, "rsyn_policy", "auto")
        config_value = {
            "forced_on": True,
            "forced_off": False,
            "auto": None,
        }.get(policy)
        model = getattr(self, "model", "") or ""
        supported = should_send_rsyn(model, config_value)
        self._rsyn_supported = bool(supported)
        capabilities = getattr(self, "capabilities", None)
        if capabilities is not None:
            try:
                capabilities["rsyn_supported"] = self._rsyn_supported
            except Exception:
                pass
        if not self._rsyn_supported:
            status = "skipped"
        else:
            # A stale error queue is drained first so a pre-existing error
            # is not misattributed to the command.
            self.drain_system_errors(max_errors=8)
            self.write(':SENS:CURR:PROT:RSYN ON')
            errors = self.drain_system_errors(max_errors=4)
            if any(self._looks_like_command_error(e) for e in errors):
                self._rsyn_supported = False
                if capabilities is not None:
                    try:
                        capabilities["rsyn_supported"] = False
                    except Exception:
                        pass
                status = "unsupported"
            else:
                status = "ok"
        self.log_stage(
            "CAPABILITY",
            status,
            {
                "policy": policy,
                "model": model or "?",
                "firmware": getattr(self, "firmware", "") or "?",
            },
        )
        result_label = {
            "ok": "enabled",
            "skipped": "skipped",
            "unsupported": "unsupported",
        }.get(status, status)
        print(
            f"[{getattr(self, 'address', '?')}] :SENS:CURR:PROT:RSYN "
            f"{result_label} "
            f"(policy={policy}, model={model or '?'}, "
            f"firmware={getattr(self, 'firmware', '') or '?'})",
            flush=True,
        )
        return self._rsyn_supported

    def ensure_trigger_immediate(self, *, verify: bool = True) -> str:
        """
        Explicitly set the trigger source to IMMEDIATE (never assume retained
        trigger state from a previous experiment) and optionally verify with
        :TRIG:SOUR?.
        """
        self.write(':TRIG:SOUR IMM')
        self.log_stage("TRIGGER", "running", {"source": "IMM"})
        if not verify:
            self.log_stage("TRIGGER", "ok", {"source": "IMM"})
            return "IMM"
        source = str(self.query(':TRIG:SOUR?')).strip()
        status = "ok" if source.upper().startswith("IMM") else "mismatch"
        self.log_stage("TRIGGER", status, {"source": source})
        if status == "mismatch":
            print(
                f"[{self.address}] WARNING :TRIG:SOUR? returned {source!r}, "
                "expected IMM",
                flush=True,
            )
        return source

    def read_esr(self) -> int:
        raw = str(self.query('*ESR?')).strip()
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            raise ValueError(f"Unexpected *ESR? response: {raw!r}") from None

    @staticmethod
    def esr_power_on(esr: int) -> bool:
        """ESR bit 7 (Power On) indicates the instrument restarted."""
        return bool(int(esr) & 0x80)

    def output_state(self) -> str:
        return str(self.query(':OUTP?')).strip()

    # for non-synchronized sweep only
    def set_volt_sweep(
        self,
        curr_compliance: Optional[float] = None,
        delay=0.01,
        volt_compliance: Optional[float] = None,
    ):

        curr_compliance = (
            self._curr_compliance_A
            if curr_compliance is None
            else float(curr_compliance)
        )
        volt_compliance = (
            self._volt_range_V
            if volt_compliance is None
            else float(volt_compliance)
        )
        self._curr_compliance_A = float(curr_compliance)
        self._volt_range_V = float(volt_compliance)

        self.write(':SOUR:FUNC VOLT', print_command=True)
        self.write(':SENS:FUNC \'CURR\'', print_command=True)
        self.write(':SENS:CURR:PROT %.2e' % curr_compliance, print_command=True)
        self.write(':SOUR:DEL %.3f' % delay, print_command=True)
        # Never rely on retained trigger state for the subsequent READ?.
        self.write(':TRIG:SOUR IMM', print_command=True)
        # turn on confield functions
        self.write(':SENS:FUNC:CONC ON', print_command=True)
        # set field reading
        self.write(':FORM:ELEM VOLT ,CURR', print_command=True)
        # turn on output
        self.write(':OUTP ON', print_command=True)
        # select sweep mode
        self.write(':SOUR:VOLT:MODE SWE', print_command=True)
        # select source ranging
        self.write(':SOUR:SWE:RANG %.0f' % volt_compliance, print_command=True)
        # select linear staircase sweep
        self.write(':SOUR:SWE:SPAC LIN', print_command=True)
        self.mode = 'volt_sweep'
        self.write(':OUTP ON', print_command=True)

    def set_volt_step(
        self,
        curr_compliance: Optional[float] = None,
        delay=0.1,
        volt_compliance: Optional[float] = None,
        initial_voltage: Optional[float] = None,
        *,
        output_on: bool = True,
    ):

        curr_compliance = (
            self._curr_compliance_A
            if curr_compliance is None
            else float(curr_compliance)
        )
        volt_compliance = (
            self._volt_range_V
            if volt_compliance is None
            else float(volt_compliance)
        )
        self._curr_compliance_A = float(curr_compliance)
        self._volt_range_V = float(volt_compliance)

        self.write(':SOUR:FUNC VOLT', print_command=True)
        self.write(':SENS:FUNC \'CURR\'', print_command=True)
        self.write(':SENS:CURR:PROT %.2e' % curr_compliance, print_command=True)
        self.write(':SOUR:DEL %.3f' % delay, print_command=True)
        self.write(':SENS:FUNC:CONC ON', print_command=True)
        self.write(':FORM:ELEM VOLT ,CURR', print_command=True)
        self.write(':SOUR:VOLT:MODE FIXED', print_command=True)
        self.write(':SOUR:VOLT:RANG %.0f' % volt_compliance, print_command=True)
        # Never rely on retained trigger state: READ? would otherwise wait on
        # a stale external/bus trigger and time out.
        self.write(':TRIG:SOUR IMM', print_command=True)
        self.write('TRIG:COUN 1', print_command=True)
        if initial_voltage is not None:
            self.write(
                ':SOUR:VOLT:LEV %.9g' % float(initial_voltage),
                print_command=True,
            )
        if output_on:
            self.mode = 'volt_step'
            self.write(':OUTP ON', print_command=True)
        else:
            # Configuration only: leave the output OFF and the mode
            # 'unconfigured' so the first explicit volt_step()/sweep call
            # re-runs set_volt_step() and enables the output.
            self.mode = 'unconfigured'

    def apply_compliance_settings(
        self,
        curr_compliance_A: float,
        current_range_A: Optional[float],
        voltage_range_V: float,
    ) -> dict:
        """Apply protection and range settings without enabling the output."""
        curr_compliance_A = float(curr_compliance_A)
        voltage_range_V = float(voltage_range_V)
        if not np.isfinite(curr_compliance_A) or curr_compliance_A <= 0:
            raise ValueError("Current compliance must be greater than zero.")
        if not np.isfinite(voltage_range_V) or voltage_range_V <= 0:
            raise ValueError("Voltage source range must be greater than zero.")

        # The 2400 family has a separate compliance range that limits the
        # highest selectable measurement range. Always derive both from the
        # requested compliance; accepting a wider manual measurement range can
        # produce Keithley error +824 ("Cannot exceed compliance range").
        resolved_range = self.recommended_current_range(curr_compliance_A)
        if not np.isfinite(resolved_range) or resolved_range <= 0:
            raise ValueError("Current measurement range must be greater than zero.")
        minimum = resolved_range * 0.001
        maximum = resolved_range * 1.05
        if curr_compliance_A < minimum or curr_compliance_A > maximum:
            raise ValueError(
                "Current compliance must be between 0.1% and 105% of "
                f"the selected current range ({minimum:g} A to {maximum:g} A)."
            )

        self._ensure_rsyn_capability()
        self.write(':SENS:CURR:RANG:AUTO OFF', print_command=True)
        if self._rsyn_supported:
            self.write(':SENS:CURR:PROT:RSYN ON', print_command=True)
        self.write(
            ':SENS:CURR:PROT %.9g' % curr_compliance_A,
            print_command=True,
        )
        # Select the measurement range explicitly so behavior is deterministic
        # even on firmware without RSYN support (avoids error +824).
        self.write(
            ':SENS:CURR:RANG %.9g' % resolved_range,
            print_command=True,
        )
        self.write(
            ':SOUR:VOLT:RANG %.9g' % voltage_range_V,
            print_command=True,
        )
        system_errors = self.drain_system_errors(max_errors=8)
        self.log_stage(
            "COMPLIANCE",
            "ok",
            {"system_errors": system_errors},
        )
        self._curr_compliance_A = curr_compliance_A
        self._curr_range_A = resolved_range
        self._volt_range_V = voltage_range_V
        return self.read_compliance_settings()

    @classmethod
    def recommended_current_range(cls, curr_compliance_A: float) -> float:
        """Choose the smallest supported range for the requested compliance."""
        curr_compliance_A = float(curr_compliance_A)
        for current_range_A in cls.CURRENT_RANGES_A:
            if (
                current_range_A * 0.001
                <= curr_compliance_A
                <= current_range_A * 1.05
            ):
                return current_range_A
        raise ValueError(
            f"No Keithley current range supports {curr_compliance_A:g} A compliance."
        )

    def read_compliance_settings(self) -> dict:
        """Return live protection and measurement/source range settings."""
        curr_compliance_A = float(self.query(':SENS:CURR:PROT?'))
        auto_range = bool(int(float(self.query(':SENS:CURR:RANG:AUTO?'))))
        current_range_A = float(self.query(':SENS:CURR:RANG?'))
        voltage_range_V = float(self.query(':SOUR:VOLT:RANG?'))
        self._curr_compliance_A = curr_compliance_A
        self._curr_range_A = None if auto_range else current_range_A
        self._volt_range_V = voltage_range_V
        return {
            'curr': curr_compliance_A,
            'curr_range': None if auto_range else current_range_A,
            'curr_range_actual': current_range_A,
            'curr_autorange': auto_range,
            'volt': voltage_range_V,
        }

    def volt_step(self, volt: float):
        if self.mode != 'volt_step':
            self.set_volt_step()
        self.write(':SOUR:VOLT:LEV %.3f' % volt)
        # self.query('READ?')

    def read_curr(self):
        if self.mode != 'volt_step':
            self.set_volt_step()
        volt, curr = self.read_float()
        return volt, curr

    def volt_sweep(self, start: float, stop: float, step: float):
        if self.mode != 'volt_sweep':
            self.set_volt_sweep()
        # select start
        self.write(':SOUR:VOLT:START %.3f' % start, print_command=True)
        # select stop
        self.write(':SOUR:VOLT:STOP %.3f' % stop, print_command=True)
        # select step
        if (stop - start) * step < 0:
            step = - step
        self.write(':SOUR:VOLT:STEP %.3f' % step, print_command=True)
        # set trigger count
        count = int((stop - start)/step + 1)
        self.write('TRIG:COUN %.0f' % count, print_command=True)
        # trigger sweep
        self.write('READ?', print_command=True)
        return count

    def read_float(self):
        raw_data = self.query('READ?')
        strings = raw_data.split(',')
        volt = float(strings[0])
        curr = float(strings[1])
        return volt, curr

    def read_numpy(self, dimension: int):
        raw_data = self.read()
        result = np.array(raw_data.split(',')).astype(np.float).reshape(dimension, -1)
        return result

    def write_x(self):
        self.volt_step(self.x_values[0])

    def read_y(self):
        volt, curr = self.read_float()
        self.y_values = np.array([volt, curr])


class DaqControl:

    def __init__(self, device_name: str):
        self.type = 'daq'
        self.device_name = device_name
        self.ai_task = nidaqmx.Task()
        self.ao_task = nidaqmx.Task()
        self.ai_index = 0
        self.ao_index = 0
        self.x_indexes = {}
        self.y_indexes = {}
        self.x_values = None
        self.y_values = None

    def add_ai_channel(self, address: str, variable: str):
        self.y_indexes[variable] = self.ai_index
        self.ai_task.ai_channels.add_ai_voltage_chan(self.device_name+'/'+address, max_val=10)
        self.read_y()
        self.ai_index += 1

    def add_ao_channel(self, address: str, variable: str):
        self.ao_task.ao_channels.add_ao_voltage_chan(self.device_name + '/' + address)
        self.x_indexes[variable] = self.ao_index
        self.ai_task.ai_channels.add_ai_voltage_chan(self.device_name+'/_'+address+'_vs_aognd', max_val=10)
        self.y_indexes['measured_'+variable] = self.ai_index
        self.read_y()
        if self.x_values is None:
            self.x_values = np.array(self.y_values[self.ai_index]).reshape(-1)
        else:
            self.x_values = np.append(self.x_values, self.y_values[self.ai_index])
        self.ai_index += 1
        self.ao_index += 1

    def receive_x(self, variable: str, value: float):
        self.x_values[self.x_indexes[variable]] = value

    def write_x(self):
        self.ao_task.write(self.x_values)

    def read_y(self):
        self.y_values = np.array(self.ai_task.read()).reshape(-1)

    def send_y(self, variable: float):
        return self.y_values[self.y_indexes[variable]]

    def check_status(self):
        for key, index in self.x_indexes.items():
            print(key, self.x_values[index])
        for key, index in self.y_indexes.items():
            print(key, self.y_values[index])


class YChannelCollection:

    def __init__(self):
        self.field_index = 0
        self.y_indexes = {}
        self.variable_name_list = []
        self.instrument_list = []
        self.value_list = []

    def add_y(self, variable: str, instrument: Union[PyvisaInstrument, DaqControl], value: float):
        self.y_indexes[variable] = self.field_index
        self.variable_name_list.append(variable)
        self.instrument_list.append(instrument)
        self.value_list.append(value)
        self.field_index += 1

    def add_y_from_instrument(self, instrument: Union[PyvisaInstrument, DaqControl]):
        for y_name, index in instrument.y_indexes.items():
            self.add_y(y_name, instrument, instrument.y_values[index])

    def receive_y(self, variable: str):
        y_index = self.y_indexes[variable]
        value = self.instrument_list[y_index].send_y(variable)
        self.value_list[y_index] = value

    def print_ys(self):
        for y_name, value in zip(self.variable_name_list, self.value_list):
            print('y_channel {}: value: {}'.format(y_name, value))

    def get_single_value(self, name: str):
        index = self.y_indexes[name]
        return self.value_list[index]

    def get_values(self):
        return np.array(self.value_list).reshape(-1)

    def get_names(self):
        return self.variable_name_list

    def get_instrument(self, name: str):
        index = self.y_indexes[name]
        return self.instrument_list[index]


class XChannelCollection:

    def __init__(self):
        self.x_index = 0
        self.x_indexes = {}
        self.variable_name_list = []
        self.instrument_list = []
        self.value_list = []

    def add_x(self, variable: str, instrument: Union[PyvisaInstrument, DaqControl], value: float):
        self.x_indexes[variable] = self.x_index
        self.variable_name_list.append(variable)
        self.instrument_list.append(instrument)
        self.value_list.append(value)
        self.x_index += 1

    def add_x_from_instrument(self, instrument: Union[PyvisaInstrument, DaqControl]):
        for x_name, index in instrument.x_indexes.items():
            self.add_x(x_name, instrument, instrument.x_values[index])

    def send_x(self, variable: str, value: float):
        x_index = self.x_indexes[variable]
        self.value_list[x_index] = value
        self.instrument_list[x_index].receive_x(variable, value)

    def print_xs(self):
        for x_name, value in zip(self.variable_name_list, self.value_list):
            print('x_channel {}: value: {}'.format(x_name, value))

    def get_single_value(self, name: str):
        index = self.x_indexes[name]
        return self.value_list[index]

    def get_values(self):
        return np.array(self.value_list).reshape(-1)

    def get_names(self):
        return self.variable_name_list

    def get_instrument(self, name: str) -> Union[PyvisaInstrument, DaqControl]:
        index = self.x_indexes[name]
        return self.instrument_list[index]


class IVSetup:

    def __init__(self, instrument_list: list):
        self.instrument_list = instrument_list

        self.y_channel_collection = YChannelCollection()
        for instrument in instrument_list:
            self.y_channel_collection.add_y_from_instrument(instrument)

        self.x_channel_collection = XChannelCollection()
        for instrument in instrument_list:
            self.x_channel_collection.add_x_from_instrument(instrument)

        self.report_status()

    def report_status(self):
        self.x_channel_collection.print_xs()
        self.y_channel_collection.print_ys()

    def update_xs(self, list_of_xs: list, list_of_values: list):
        for name, value in zip(list_of_xs, list_of_values):
            self.x_channel_collection.send_x(name, value)

    def update_ys(self, list_of_ys: list):
        for name in list_of_ys:
            self.y_channel_collection.receive_y(name)

    def get_x_values(self, list_of_x_names: list):
        lst = []
        for x_name in list_of_x_names:
            lst.append(self.get_single_x_value(x_name))
        return np.array(lst).reshape(-1)

    def get_single_x_value(self, x_name: str):
        return self.x_channel_collection.get_single_value(x_name)

    def get_x_names(self):
        return self.x_channel_collection.get_names()

    def get_y_values(self, list_of_y_names: list):
        lst = []
        for y_name in list_of_y_names:
            lst.append(self.get_single_y_value(y_name))
        return np.array(lst).reshape(-1)

    def get_single_y_value(self, y_name: str):
        return self.y_channel_collection.get_single_value(y_name)

    def get_y_names(self):
        return self.y_channel_collection.get_names()

    def create_1d_sweep(self, sample_name: str, exp_name: str, sweep_x_names: Union[str, list],
                        sweep_y_names: Union[str, list]):
        return OneDSweep(sample_name, exp_name, sweep_x_names, sweep_y_names, self)

    def x_goto(self, x_name: str, target: float, delta: float, delay: float, print_steps: bool=False):
        start = float(self.get_single_x_value(x_name))
        target = float(target)

        if delta is None or float(delta) == 0:
            steps = 2
        else:
            delta = float(delta)
            # keep delta sign consistent with direction
            if (target - start) * delta < 0:
                delta = -delta

            # number of intervals needed, then +1 points
            n_intervals = int(np.ceil(abs(target - start) / max(abs(delta), 1e-12)))
            steps = max(2, n_intervals + 1)


        y_names = ['measured_' + x_name]
        # Force-include leakage/current channel that matches the X name
        y_names.append(x_name + '_leakage')

        sweep = self.create_1d_sweep('test', 'test', x_name, y_names)
        # enable per-step printing if requested
        if hasattr(sweep, 'set_print'):
            sweep.set_print(print_steps)
        sweep.set_sweep(start, target, int(steps), delay, [1]*len(y_names))
        sweep.trigger_all()


class OneDSweep:

    def __init__(self, sample_name: str, exp_name: str, sweep_x_names: Union[str, list],
                 sweep_y_names: Union[str, list],  iv_setup: IVSetup):
        self.sample_name = sample_name
        self.exp_name = exp_name
        self.print_steps = False

        if isinstance(sweep_x_names, str):
            sweep_x_names = [sweep_x_names]
        if isinstance(sweep_y_names, str):
            sweep_y_names = [sweep_y_names]
        self.sweep_x_names = sweep_x_names
        self.sweep_y_names = sweep_y_names
        self.list_of_variable_names = self.sweep_x_names.copy()
        self.list_of_variable_names.extend(self.sweep_y_names)

        self.iv_setup = iv_setup

        self.instrument_set = set()
        for y_name in self.sweep_y_names:
            self.instrument_set.add(self.iv_setup.y_channel_collection.get_instrument(y_name))
        for x_name in self.sweep_x_names:
            self.instrument_set.add(self.iv_setup.x_channel_collection.get_instrument(x_name))

        self.delay = None
        self.y_factors = None
        self.sweep_x_values = None
        self.total_triggers = None
        self.current_trigger = None

    def set_print(self, print_steps: bool):
        self.print_steps = bool(print_steps)

    def set_sweep(self, x_starts: Union[list, np.ndarray, float], x_ends: Union[list, np.ndarray, float], x_steps: int,
                  delay: float, y_factors: Union[list, np.ndarray, float]):

        x_starts = np.array(x_starts).reshape(-1)
        x_ends = np.array(x_ends).reshape(-1)
        y_factors = np.array(y_factors).reshape(-1)
        if len(x_starts) != len(x_ends) or len(self.sweep_x_names) != len(x_ends):
            print('x dimensions do not match')
            return
        if len(y_factors) != len(self.sweep_y_names):
            print('y dimensions do not match')
            return

        sweep_x_values = np.linspace(x_starts, x_ends, x_steps)

        self.sweep_x_values = sweep_x_values
        self.total_triggers = x_steps

        self.delay = delay
        self.y_factors = y_factors
        self.current_trigger = 0

        '''self.storage = data_collection.OneDSweepData(self.sample_name, self.exp_name, x_steps,
                                                     self.list_of_variable_names, self.plot_x, self.plot_y, plot, save)'''

        print('variable: {}'.format(self.sweep_x_names))
        print('start: {}'.format(x_starts))
        print('end: {}'.format(x_ends))
        print('steps: {}'.format(x_steps))

    def trigger(self):
        if self.current_trigger >= self.total_triggers:
            return

        x_values = self.sweep_x_values[self.current_trigger]

        self.iv_setup.update_xs(self.sweep_x_names, x_values)
        for instrument in self.instrument_set:
            instrument.write_x()
        time.sleep(self.delay)
        for instrument in self.instrument_set:
            instrument.read_y()

        # Update Y channels and fetch values
        self.iv_setup.update_ys(self.sweep_y_names)
        factors = self.y_factors if self.y_factors is not None else 1.0
        y_values = self.iv_setup.get_y_values(self.sweep_y_names) * factors
        data = np.append(x_values, y_values)

        # Optional per-step printing of voltage and current (if available)
        if getattr(self, "print_steps", False):
            y_map = {name: val for name, val in zip(self.sweep_y_names, y_values)}
            measured_names = [n for n in self.sweep_y_names if n.startswith('measured_')]
            leakage_names  = [n for n in self.sweep_y_names if n.endswith('_leakage')]

            # pretty x value
            try:
                import numpy as _np
                x_disp = float(_np.array(x_values).reshape(-1)[0])
            except Exception:
                x_disp = x_values

            # choose values if present
            v_val = y_map.get(measured_names[0], None) if measured_names else None
            i_val = y_map.get(leakage_names[0], None) if leakage_names else None

            step_idx = self.current_trigger + 1
            total = self.total_triggers
            xlab = self.sweep_x_names[0] if self.sweep_x_names else "X"

            if v_val is not None and i_val is not None:
                print(f"[{step_idx}/{total}] {xlab}={x_disp:.6f} V | measured={v_val:.6f} V | current={(i_val*1e6):.3f} uA")
            elif v_val is not None:
                print(f"[{step_idx}/{total}] {xlab}={x_disp:.6f} V | measured={v_val:.6f} V")
            else:
                print(f"[{step_idx}/{total}] {xlab}={x_disp} | y={y_values}")

        self.current_trigger += 1
        return np.copy(data)


    def trigger_all(self):
        for t in range(self.total_triggers):
            self.trigger()

class MagnetPowerSupplyControl(PyvisaInstrument):
    def __init__(self, address: str, name: str, termination: str, rm: pyvisa.ResourceManager):
        super().__init__(address, name, termination, rm)
        self.type = 'attodry1000'
        self.delay = 0.4
        self.connect()
        self.get_identity()
        self.remote()
        self.set_unit()
        self.get_unit()
        self.get_magnetfield()
        self.get_magnetsweeprate() 
        self.heaterstatus = None
        self.target_high_limit = 0.0
        self.target_low_limit = 0.0
        self.current_field = None
        # 90 KGauss
        self.safe_magnetfields = 90
        self.KGausstoAmpera = 2.0328
        self.set_high_sweeplimit(self.target_high_limit)
        self.set_low_sweeplimit(self.target_low_limit)
    def connect(self):
        self.my_instr = self.rm.open_resource(self.address, timeout=self.timeout)
        # self.my_instr.read_termination = self.termination
        # self.my_instr.write_termination = self.termination
        # self.write('REMOTE',print_command= True)
        self.my_instr.read_termination = '\r\n'
        self.my_instr.write_termination = '\r\n'

    def get_identity(self):
        self.query('*IDN?',print_response=True)
        self.read(print_response=True)
# current A in the leads
    def get_magnetfield(self):
        self.query('IMAG?', print_response=True)
        read = self.read(print_response=True)
        field = read.split('kG',1)[0]
        return np.array(field).astype(float)

    
    def get_magnetsweeprate(self):
        query_list = [0,1,2]
        for query in query_list:
            self.query('RATE? {}'.format(query),print_response=True)
            read = self.read(print_response=True)
    
    def remote(self):
        self.write('REMOTE')
        self.read(print_response=True)
    
    def get_unit(self):
        self.write('UNITS?')
        time.sleep(0.1)
        # read = self.read(print_response=True)
        response = self.read()
        print(response)
        response = self.read()
        time.sleep(0.1)
        print(response)

    def set_unit(self):
        self.write('UNITS {}'.format('G'),print_command=True)
        self.read()
        print('x')

    def get_heaterstatus(self):
        self.query('PSHTR?',print_response=True)
        # query returns 1 if the switch heater is ON or 0 if the switch heater is OFF
        read = self.read(print_response=True) 
        return np.array(read).astype(int)
    
    def turnon_heater(self):
        heater = self.get_heaterstatus()
        if heater == 0:
            self.write('PSHTR ON',print_command=True)
            self.read()
            print('pausing 60s')
            time.sleep(60)
        else:
            raise KeyError('Heater already turned on!')
    
    def turnoff_heater(self):
        heater = self.get_heaterstatus()
        if heater == 1:
            self.write('PSHTR OFF',print_command=True)
            self.read()
            print('pausing 120s')
            time.sleep(120)
        else:
            raise KeyError('Heater already turned off!')

    
    def get_low_sweeplimit(self):
        self.query('LLIM?',print_command=True)
        read = self.read(print_response= True)
        return read

    def get_high_sweeplimit(self):
        self.query('ULIM?',print_command=True)
        read = self.read(print_response= True)     
        return read
    
    def set_low_sweeplimit(self,lowlimit):
        if (abs(lowlimit)-self.safe_magnetfields) < 0.001:
            self.target_low_limit = lowlimit
            self.write('LLIM {:.4f}'.format(lowlimit),print_command= True)
            self.read()
        else:
            raise ValueError('Exceeds maximum field!')


    def set_high_sweeplimit(self,highlimit):
        if (abs(highlimit)-self.safe_magnetfields) < 0.001:
            self.target_high_limit = highlimit
            self.write('ULIM {:.4f}'.format(highlimit),print_command= True)
            self.read()
        else:  
            raise ValueError('Exceeds maximum field!')            

    # def set_sweeplimit(self, targetlimit):
        

    def get_sweep_mode(self):
        self.query('SWEEP?',print_command=True)
        read = self.read(print_response= True)
        return read

    def start_sweep(self,mode):
        # Parameter Range: UP, DOWN, PAUSE, or ZERO
        # self.read()
        if self.get_heaterstatus() == 0:
            raise KeyError('Heater is off!')
        else:
            # self.get_sweep_mode()
            if mode == 'UP':
                target = self.target_high_limit   
            elif mode == 'ZERO':
                target = 0.0
            elif mode == 'DOWN':
                target = self.target_low_limit
            else:
                self.write('SWEEP {}'.format(mode),print_command=True)
                raise KeyError('Magnet pausing')
            self.write('SWEEP {}'.format(mode),print_command=True)
            self.read()
            self.get_sweep_mode()
            while True:
                time.sleep(1)
                epsilon = 0.0005
                actual_field = self.get_magnetfield()
                print(target, actual_field)
                if abs(target - actual_field) <= epsilon:
                    time.sleep(10)
                    break
            self.write('SWEEP PAUSE',print_command=True)
            print('Pause at target field:',self.get_magnetfield(),'kG')

                
    def sweep_to_target(self, mode, highlimit=0, lowlimit=0):
        if highlimit < lowlimit:
            raise KeyError('Highlimit < lowlimit!')
        else:
            self.set_high_sweeplimit(highlimit)
            self.set_low_sweeplimit(lowlimit)
            if mode == 'UP':
                self.start_sweep('UP')
            elif mode == "DOWN":
                self.start_sweep('DOWN')

    
    def unsync_start_sweep(self, mode):
        if self.get_heaterstatus() == 0:
            raise KeyError('Heater is off!')
        else:
            # self.get_sweep_mode()
            if mode == 'UP':
                target = self.target_high_limit   
            elif mode == 'ZERO':
                target = 0.0
            elif mode == 'DOWN':
                target = self.target_low_limit
            else:
                self.write('SWEEP {}'.format(mode),print_command=True)
                raise KeyError('Magnet pausing')
            self.write('SWEEP {}'.format(mode),print_command=True)
            self.read()

           
            

    
        
    

        



'''
class OneDSweep:

    def __init__(self, sweep_x_names, sweep_y_names, dict_all_x_channels, dict_all_y_channels, plot_x_name,
                 plot_y_name, sample_name):
        self.sweep_x_channels = []
        self.sweep_y_channels = []
        self.all_x_channels = dict_all_x_channels.values()
        self.sweep_instruments = []
        self.plot_x_name = plot_x_name
        self.plot_y_name = plot_y_name
        self.x_header = []
        for x_channel in self.all_x_channels:
            self.x_header.append(x_channel.name)
        self.y_header = sweep_y_names
        self.plot_x_index = self.x_header.index(plot_x_name)
        self.plot_y_index = self.y_header.index(plot_y_name)
        for x_name in sweep_x_names:
            self.sweep_x_channels.append(dict_all_x_channels[x_name])
        for y_name in sweep_y_names:
            self.sweep_y_channels.append(dict_all_y_channels[y_name])
        self.__get_sweep_instruments()
        self.sample_name = sample_name
        self.file_number = -1

    def __get_sweep_instruments(self):
        for xchannel in self.sweep_x_channels:
            if xchannel.x_instrument not in self.sweep_instruments:
                self.sweep_instruments.append(xchannel.x_instrument)
            if xchannel.y_instrument not in self.sweep_instruments:
                self.sweep_instruments.append(xchannel.y_instrument)
        for ychannel in self.sweep_y_channels:
            if ychannel.y_instrument not in self.sweep_instruments:
                self.sweep_instruments.append(ychannel.y_instrument)

    def one_d_sweep(self, list_of_x_values, ramp_steps, delay, y_amp_rates, experiment_name, save_data=True, plot=True, timer=True):
        # values for each channel should be passed as a row vector
        x_numbers = len(self.sweep_x_channels)
        x_array = np.array(list_of_x_values).reshape([x_numbers, -1]).T
        self.xs_goto(x_array[0, :], ramp_steps, delay)
        rows, columns = x_array.shape
        y_amp_rates = np.array(y_amp_rates)
        sweep_x_data = np.empty([rows, len(self.x_header)])
        sweep_y_data = np.empty([rows, len(self.y_header)])
        sweep_x_data[:, :] = np.nan
        sweep_y_data[:, :] = np.nan
        if plot:
            data_plot = plt.subplot(1, 1, 1)
        for i in range(rows):
            if timer:
                t1 = time.time()
            values_to_update = x_array[i]
            self.__update_x(values_to_update)
            self.__write_x()
            time.sleep(delay)
            self.__read_y()
            self.__update_y()
            x_data = self.__collect_x()
            y_data = self.__collect_y()/y_amp_rates
            sweep_x_data[i] = x_data
            sweep_y_data[i] = y_data
            if plot:
                plt.cla()
                data_plot.plot(sweep_x_data[:, self.plot_x_index], sweep_y_data[:, self.plot_y_index])
                plt.pause(0.001)
            if timer:
                t2 = time.time()
                print('{:.4f} s per frame'.format(t2-t1))
        data = np.append(sweep_x_data, sweep_y_data, axis=1)
        if save_data:
            self.__save_data(data, experiment_name)

    def xs_goto(self, list_of_targets, list_of_steps, delay):
        for (xchannel, target, step) in zip (self.sweep_x_channels, list_of_targets, list_of_steps):
            xchannel.x_goto(target, step, delay)
           

    def __save_data(self, data, experiment_name):
        header = self.x_header.copy()
        header.extend(self.y_header)
        df = pd.DataFrame(data, columns=header)
        while True:
            self.file_number += 1
            file_name = '{}_{}_{:0>3d}'.format(self.sample_name, experiment_name, self.file_number)
            csv_name = file_name + '.csv'
            if not os.path.exists(csv_name):
                df.to_csv(csv_name)
                plt.title(file_name)
                plt.xlabel(self.plot_x_name)
                plt.ylabel(self.plot_y_name)
                plt.savefig(file_name)
                plt.close()
                break

    def __update_x(self, values_to_update):
        for xchannel, x in zip(self.sweep_x_channels, values_to_update):
            xchannel.update_x(x)

    def __write_x(self):
        for instrument in self.sweep_instruments:
            instrument.write_x()

    def __read_y(self):
        for instrument in self.sweep_instruments:
            instrument.read_y()

    def __update_y(self):
        for ychannel in self.sweep_y_channels:
            ychannel.update_y()
        for xchannel in self.sweep_x_channels:
            xchannel.update_y()

    def __collect_x(self):
        data = []
        for xchannel in self.all_x_channels:
            data.append(xchannel.x_value)
        return np.array(data)

    def __collect_y(self):
        data = []
        for ychannel in self.sweep_y_channels:
            data.append(ychannel.y_value)
        return np.array(data)
'''

class TwoDSweep:
    def __init__(self, outer_x_names, inner_x_names, sweep_y_names,
                 dict_all_x_channels, dict_all_y_channels, plot_x_name,
                 plot_y_name, sample_name):

        self.outer_x_names = outer_x_names
        self.inner_x_names = inner_x_names
        self.outer_sweep = OneDSweep(outer_x_names, sweep_y_names, dict_all_x_channels, dict_all_y_channels,
                                     plot_x_name, plot_y_name, sample_name)

        self.inner_sweep = OneDSweep(inner_x_names, sweep_y_names, dict_all_x_channels, dict_all_y_channels,
                                     plot_x_name, plot_y_name, sample_name)

    def two_d_sweep(self, list_of_outer_values, outer_ramp_steps, list_of_inner_values, inner_ramp_steps,
                    delay, y_amp_rates, experiment_name):
        outer_x_numbers = len(self.outer_x_names)
        outer_x_array = np.array(list_of_outer_values).reshape(outer_x_numbers, -1).T
        rows, columns = outer_x_array.shape
        inner_x_numbers = len(self.inner_x_names)
        inner_x_array = np.array(list_of_inner_values).reshape(inner_x_numbers, -1).T
        for i in range(rows):
            self.outer_sweep.xs_goto(outer_x_array[i], outer_ramp_steps, delay)
            self.inner_sweep.xs_goto(inner_x_array[0], inner_ramp_steps, delay)
            self.inner_sweep.one_d_sweep(list_of_inner_values, inner_ramp_steps, delay, y_amp_rates, experiment_name)

    def inner_1d_sweep(self, list_of_x_values, ramp_steps, delay, y_amp_rates, experiment_name, save_data=True):
        self.inner_sweep.one_d_sweep(list_of_x_values, ramp_steps, delay, y_amp_rates, experiment_name,
                                     save_data=save_data)

    def outer_1d_sweep(self, list_of_x_values, ramp_steps, delay, y_amp_rates, experiment_name, save_data=True):
        self.outer_sweep.one_d_sweep(list_of_x_values, ramp_steps, delay, y_amp_rates, experiment_name,
                                     save_data=save_data)


if __name__ == '__main__':
    # keithley = KeithControl('GPIB1::6::INSTR', 'GPIB6', 'Vbg', pyvisa.ResourceManager())
    # keithley.volt_step(-0.1)
    MSP = MagnetPowerSupplyControl('ASRL4::INSTR','MSP','\r\n',pyvisa.ResourceManager())
 
    # MSP.get_heaterstatus()
    # MSP.get_high_sweeplimit()
    # MSP.get_low_sweeplimit()
    # MSP.set_high_sweeplimit(0.2)
    # print('---')
    # MSP.get_high_sweeplimit()
    # MSP.get_low_sweeplimit()

    MSP.get_unit()
    print('---')
    # time.sleep(1)
    # MSP.start_sweep('UP')
    
















