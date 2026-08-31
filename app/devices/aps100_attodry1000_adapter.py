"""Direct APS100 control for the attoDRY1000 magnet system.

The attoDRY2100 SDK exposes a high-level RPC magnet service.  The attoDRY1000
installation instead connects directly to the Attocube APS100 virtual COM port,
so the matching, sweep and persistent-switch sequencing live here.

Application-facing field values are tesla and sweep rates are tesla/minute.
The APS100 field protocol uses kG for limit writes and A/s for rate writes.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
import threading
import time
from typing import Callable, Optional


DEFAULT_RESOURCE = "ASRL5::INSTR"


class APS100Error(RuntimeError):
    """Base error for APS100 communication and command failures."""


class APS100IdentityError(APS100Error):
    """Raised when the connected instrument is not an Attocube APS100."""


class APS100SafetyError(APS100Error):
    """Raised when a requested operation violates a configured guard."""


class APS100CommandBlockedError(APS100Error):
    """Raised when the front panel or local state blocks a remote command."""


class APS100TimeoutError(APS100Error):
    """Raised when a field or state transition does not complete in time."""


@dataclass(frozen=True)
class APS100Status:
    raw: int
    sweep_active: bool
    standby: bool
    quench: bool
    power_module_failure: bool
    message_available: bool
    event_summary: bool
    master_summary: bool
    menu_locked: bool

    @property
    def faulted(self) -> bool:
        return self.quench or self.power_module_failure


@dataclass(frozen=True)
class APS100Identity:
    manufacturer: str
    model: str
    serial: str
    firmware: str
    build: str

    @property
    def display_name(self) -> str:
        return (
            f"{self.manufacturer} {self.model} "
            f"S/N {self.serial} FW {self.firmware}.{self.build}"
        )


@dataclass(frozen=True)
class MagnetSnapshot:
    monotonic_s: float
    field_t: float
    output_field_t: float
    output_current_a: float
    heater_on: bool
    sweep_state: str
    status: APS100Status
    lower_limit_t: float
    upper_limit_t: float
    voltage_limit_v: float
    magnet_voltage_v: float
    output_voltage_v: float
    units: str
    operating_mode: str

    @property
    def driven_mode(self) -> bool:
        return self.heater_on

    @property
    def persistent_mode(self) -> bool:
        return not self.heater_on


_VALUE_UNIT_RE = re.compile(
    r"^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*([A-Za-z]+)?\s*$"
)


def _parse_number_unit(value: str) -> tuple[float, str]:
    match = _VALUE_UNIT_RE.match(str(value))
    if not match:
        raise APS100Error(f"Could not parse APS100 value: {value!r}")
    return float(match.group(1)), (match.group(2) or "")


def field_response_to_tesla(value: str, coil_constant_t_per_a: float) -> float:
    number, unit = _parse_number_unit(value)
    normalized = unit.lower()
    if normalized == "t":
        return number
    if normalized == "kg":
        return number * 0.1
    if normalized == "g":
        return number / 10_000.0
    if normalized == "a":
        return number * coil_constant_t_per_a
    raise APS100Error(f"Unexpected APS100 field/current unit {unit!r} in {value!r}")


def voltage_response_to_volts(value: str) -> float:
    number, unit = _parse_number_unit(value)
    if unit and unit.lower() != "v":
        raise APS100Error(f"Unexpected APS100 voltage unit {unit!r} in {value!r}")
    return number


def decode_status_byte(raw: int | str) -> APS100Status:
    value = int(raw)
    if not 0 <= value <= 255:
        raise APS100Error(f"Invalid APS100 status byte: {value}")
    return APS100Status(
        raw=value,
        sweep_active=bool(value & 0x01),
        standby=bool(value & 0x02),
        quench=bool(value & 0x04),
        power_module_failure=bool(value & 0x08),
        message_available=bool(value & 0x10),
        event_summary=bool(value & 0x20),
        master_summary=bool(value & 0x40),
        menu_locked=bool(value & 0x80),
    )


class APS100AttoDry1000Adapter:
    """Thread-safe, direct serial adapter for one APS100 magnet channel."""

    def __init__(
        self,
        resource_name: str = DEFAULT_RESOURCE,
        *,
        baud_rate: int = 9600,
        timeout_ms: int = 1500,
        coil_constant_t_per_a: float = 0.20328,
        maximum_field_t: float = 9.0,
        maximum_current_a: float = 44.27,
        maximum_rate_a_per_s: float = 0.0343,
        heater_warm_s: float = 60.0,
        heater_cool_s: float = 120.0,
        current_match_tolerance_a: float = 0.01,
        expect_echo: bool = True,
        resource_manager=None,
        visa_resource=None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if coil_constant_t_per_a <= 0:
            raise ValueError("coil_constant_t_per_a must be positive")
        self.resource_name = str(resource_name).strip() or DEFAULT_RESOURCE
        self.baud_rate = int(baud_rate)
        self.timeout_ms = int(timeout_ms)
        self.coil_constant_t_per_a = float(coil_constant_t_per_a)
        self.maximum_field_t = float(maximum_field_t)
        self.maximum_current_a = float(maximum_current_a)
        self.maximum_rate_a_per_s = float(maximum_rate_a_per_s)
        self.heater_warm_s = float(heater_warm_s)
        self.heater_cool_s = float(heater_cool_s)
        self.current_match_tolerance_a = float(current_match_tolerance_a)
        self.expect_echo = bool(expect_echo)
        self._resource_manager = resource_manager
        self._resource = visa_resource
        self._owns_resource_manager = False
        self._lock = threading.RLock()
        self._sleep = sleep_fn
        self._identity: Optional[APS100Identity] = None
        self._remote = False
        self._last_target_t: Optional[float] = None
        self.command_observer = None

    @property
    def connected(self) -> bool:
        return self._resource is not None and self._identity is not None

    @property
    def identity(self) -> Optional[APS100Identity]:
        return self._identity

    def connect(self) -> APS100Identity:
        """Open and identify the supply without changing any APS100 setting."""
        with self._lock:
            if self.connected:
                assert self._identity is not None
                return self._identity
            if self._resource is None:
                if self._resource_manager is None:
                    import pyvisa

                    self._resource_manager = pyvisa.ResourceManager()
                    self._owns_resource_manager = True
                self._resource = self._resource_manager.open_resource(self.resource_name)
            resource = self._resource
            for attr, value in (
                ("baud_rate", self.baud_rate),
                ("timeout", self.timeout_ms),
                ("write_termination", "\r"),
                ("read_termination", "\n"),
            ):
                try:
                    setattr(resource, attr, value)
                except Exception:
                    pass
            identity = self._parse_identity(self._query("*IDN?"))
            if identity.manufacturer.lower() != "attocube" or identity.model.upper() != "APS100":
                self._identity = None
                raise APS100IdentityError(
                    f"Expected Attocube APS100, received {identity.manufacturer} {identity.model}"
                )
            self._identity = identity
            return identity

    @staticmethod
    def _parse_identity(response: str) -> APS100Identity:
        parts = [part.strip() for part in str(response).split(",")]
        if len(parts) < 5:
            raise APS100IdentityError(f"Unexpected *IDN? response: {response!r}")
        return APS100Identity(*parts[:5])

    @staticmethod
    def _normalized_line(value: str) -> str:
        return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())

    def _require_connected(self):
        if self._resource is None or self._identity is None:
            raise APS100Error("APS100 is not connected")
        return self._resource

    def _read_line(self) -> str:
        resource = self._require_connected()
        return self._normalized_line(resource.read())

    def _query(self, command: str) -> str:
        with self._lock:
            resource = self._resource
            if resource is None:
                raise APS100Error("APS100 resource is not open")
            resource.write(command)
            command_norm = self._normalized_line(command).lower()
            last = ""
            for _ in range(8):
                line = self._normalized_line(resource.read())
                if not line:
                    continue
                last = line
                if line.lower() == command_norm:
                    continue
                if "command blocked" in line.lower():
                    raise APS100CommandBlockedError(
                        f"APS100 blocked {command!r}; close the front-panel menu "
                        "and enable remote operation"
                    )
                observer = self.command_observer
                if callable(observer):
                    observer(
                        {
                            "utc_epoch_s": time.time(),
                            "kind": "query",
                            "command": command,
                            "response": line,
                        }
                    )
                return line
            raise APS100Error(f"APS100 returned no response to {command!r}; last line={last!r}")

    def _write(self, command: str, *, check_errors: bool = True) -> None:
        with self._lock:
            resource = self._require_connected()
            # *ESR? is cumulative and clears on read. Clear historical events
            # before a mutation so only errors caused by this command are
            # attributed to it. Live safety state is checked separately via
            # *STB?, SWEEP?, heater, current, and voltage telemetry.
            preexisting_event_status = 0
            if check_errors and not command.startswith(("*ESR", "*CLS")):
                preexisting_event_status = int(float(self._query("*ESR?")))
            observer = self.command_observer
            if callable(observer):
                observer(
                    {
                        "utc_epoch_s": time.time(),
                        "kind": "write",
                        "command": command,
                        "preexisting_esr": preexisting_event_status,
                    }
                )
            resource.write(command)
            if self.expect_echo:
                try:
                    echoed = self._read_line()
                except Exception as exc:
                    raise APS100Error(
                        f"Did not receive APS100 USB echo for {command!r}: {exc}"
                    ) from exc
                if echoed and echoed.lower() != self._normalized_line(command).lower():
                    if "command blocked" in echoed.lower():
                        raise APS100CommandBlockedError(
                            f"APS100 blocked {command!r}; close the front-panel "
                            "menu and enable remote operation"
                        )
                    raise APS100Error(
                        f"Unexpected APS100 response while writing {command!r}: {echoed!r}"
                    )
            if check_errors and not command.startswith("*ESR"):
                event_status = int(float(self._query("*ESR?")))
                if event_status & 0x3C:
                    raise APS100Error(
                        f"APS100 reported ESR={event_status} after {command!r}"
                    )

    def take_remote(self) -> None:
        self._write("REMOTE")
        self._remote = True

    def return_local(self) -> None:
        self._write("LOCAL", check_errors=False)
        self._remote = False

    def get_units(self) -> str:
        return self._query("UNITS?").strip()

    def get_operating_mode(self) -> str:
        """Return the APS100 operating mode (the Z magnet requires Manual)."""
        return self._query("MODE?").strip()

    def require_manual_mode(self) -> str:
        mode = self.get_operating_mode()
        if mode.lower() != "manual":
            raise APS100SafetyError(
                f"APS100 operating mode is {mode!r}; the Z magnet requires Manual mode"
            )
        return mode

    def select_field_units(self) -> str:
        self._write("UNITS G")
        units = self.get_units()
        if units.lower() not in {"g", "kg", "t"}:
            raise APS100Error(f"APS100 did not enter field units; UNITS? returned {units!r}")
        return units

    def get_status(self) -> APS100Status:
        return decode_status_byte(self._query("*STB?"))

    def get_sweep_state(self) -> str:
        return self._query("SWEEP?").strip().lower()

    def get_heater_status(self) -> bool:
        response = self._query("PSHTR?").strip()
        if response not in {"0", "1"}:
            raise APS100Error(f"Unexpected PSHTR? response: {response!r}")
        return response == "1"

    def get_field_t(self) -> float:
        return field_response_to_tesla(
            self._query("IMAG?"), self.coil_constant_t_per_a
        )

    def get_output_field_t(self) -> float:
        return field_response_to_tesla(
            self._query("IOUT?"), self.coil_constant_t_per_a
        )

    def get_limits_t(self) -> tuple[float, float]:
        low = field_response_to_tesla(
            self._query("LLIM?"), self.coil_constant_t_per_a
        )
        high = field_response_to_tesla(
            self._query("ULIM?"), self.coil_constant_t_per_a
        )
        return low, high

    def get_voltage_limit_v(self) -> float:
        return voltage_response_to_volts(self._query("VLIM?"))

    def get_magnet_voltage_v(self) -> float:
        return voltage_response_to_volts(self._query("VMAG?"))

    def get_output_voltage_v(self) -> float:
        return voltage_response_to_volts(self._query("VOUT?"))

    def get_rate_a_per_s(self, index: int) -> float:
        if index not in range(6):
            raise ValueError("APS100 rate index must be 0..5")
        value, unit = _parse_number_unit(self._query(f"RATE? {index}"))
        if unit and unit.lower() not in {"a", "a/s", "aps"}:
            raise APS100Error(f"Unexpected rate unit {unit!r}")
        return value

    def get_rate_t_per_min(self, index: int) -> float:
        return self.get_rate_a_per_s(index) * self.coil_constant_t_per_a * 60.0

    def get_range_a(self, index: int) -> float:
        if index not in range(5):
            raise ValueError("APS100 range index must be 0..4")
        value, _unit = _parse_number_unit(self._query(f"RANGE? {index}"))
        return value

    def get_rates(self) -> dict[int, tuple[float, float]]:
        return {
            index: (self.get_range_a(index), self.get_rate_t_per_min(index))
            for index in range(5)
        }

    def _ensure_no_fault(self) -> APS100Status:
        self.require_manual_mode()
        status = self.get_status()
        if status.quench:
            raise APS100SafetyError("APS100 reports a quench condition")
        if status.power_module_failure:
            raise APS100SafetyError("APS100 reports a power-module failure")
        if status.menu_locked:
            raise APS100SafetyError("APS100 front-panel menu is locking remote commands")
        return status

    def read_snapshot(self) -> MagnetSnapshot:
        with self._lock:
            units = self.get_units()
            operating_mode = self.get_operating_mode()
            field_t = self.get_field_t()
            output_field_t = self.get_output_field_t()
            low_t, high_t = self.get_limits_t()
            return MagnetSnapshot(
                monotonic_s=time.monotonic(),
                field_t=field_t,
                output_field_t=output_field_t,
                output_current_a=output_field_t / self.coil_constant_t_per_a,
                heater_on=self.get_heater_status(),
                sweep_state=self.get_sweep_state(),
                status=self.get_status(),
                lower_limit_t=low_t,
                upper_limit_t=high_t,
                voltage_limit_v=self.get_voltage_limit_v(),
                magnet_voltage_v=self.get_magnet_voltage_v(),
                output_voltage_v=self.get_output_voltage_v(),
                units=units,
                operating_mode=operating_mode,
            )

    def _validate_field(self, field_t: float) -> float:
        value = float(field_t)
        if not math.isfinite(value):
            raise APS100SafetyError("Magnetic field must be finite")
        if abs(value) > self.maximum_field_t + 1e-12:
            raise APS100SafetyError(
                f"Requested {value:.6g} T exceeds configured ±{self.maximum_field_t:g} T"
            )
        return value

    @staticmethod
    def _tesla_to_kg(field_t: float) -> float:
        return float(field_t) * 10.0

    def set_limits_t(self, low_t: float, high_t: float) -> tuple[float, float]:
        low = self._validate_field(low_t)
        high = self._validate_field(high_t)
        if low >= high:
            raise APS100SafetyError("Lower field limit must be less than upper limit")
        self._ensure_no_fault()
        self.select_field_units()
        old_low, old_high = self.get_limits_t()
        commands = (
            [("LLIM", low), ("ULIM", high)]
            if low <= old_high
            else [("ULIM", high), ("LLIM", low)]
        )
        for name, value in commands:
            self._write(f"{name} {self._tesla_to_kg(value):.6f}")
        actual_low, actual_high = self.get_limits_t()
        tolerance = 2e-4
        if abs(actual_low - low) > tolerance or abs(actual_high - high) > tolerance:
            raise APS100Error(
                "APS100 limit verification failed: "
                f"requested ({low:g}, {high:g}) T, "
                f"read ({actual_low:g}, {actual_high:g}) T"
            )
        return actual_low, actual_high

    def set_rate_t_per_min(
        self,
        rate_t_per_min: float,
        *,
        max_abs_field_t: Optional[float] = None,
    ) -> dict[int, float]:
        rate = float(rate_t_per_min)
        if not math.isfinite(rate) or rate <= 0:
            raise APS100SafetyError("Sweep rate must be positive and finite")
        rate_a_per_s = rate / (self.coil_constant_t_per_a * 60.0)
        if rate_a_per_s > self.maximum_rate_a_per_s + 1e-12:
            max_t_per_min = (
                self.maximum_rate_a_per_s * self.coil_constant_t_per_a * 60.0
            )
            raise APS100SafetyError(
                f"Requested {rate:g} T/min exceeds configured {max_t_per_min:.6g} T/min"
            )
        self._ensure_no_fault()
        max_current = self.maximum_current_a
        if max_abs_field_t is not None:
            max_current = min(
                max_current,
                abs(self._validate_field(max_abs_field_t)) / self.coil_constant_t_per_a,
            )
        boundaries = [self.get_range_a(index) for index in range(5)]
        changed: dict[int, float] = {}
        lower = 0.0
        for index, upper in enumerate(boundaries):
            if lower <= max_current + 1e-12:
                self._write(f"RATE {index} {rate_a_per_s:.7f}")
                actual = self.get_rate_a_per_s(index)
                if abs(actual - rate_a_per_s) > 1.5e-4:
                    raise APS100Error(
                        f"APS100 RATE {index} verification failed: "
                        f"requested {rate_a_per_s:g}, read {actual:g} A/s"
                    )
                changed[index] = actual * self.coil_constant_t_per_a * 60.0
            lower = upper
            if lower > max_current:
                break
        return changed

    def _set_directional_target(self, target_t: float, current_t: float) -> str:
        target = self._validate_field(target_t)
        self.select_field_units()
        if target >= current_t:
            self._write(f"ULIM {self._tesla_to_kg(target):.6f}")
            actual = field_response_to_tesla(
                self._query("ULIM?"), self.coil_constant_t_per_a
            )
            direction = "UP"
        else:
            self._write(f"LLIM {self._tesla_to_kg(target):.6f}")
            actual = field_response_to_tesla(
                self._query("LLIM?"), self.coil_constant_t_per_a
            )
            direction = "DOWN"
        if abs(actual - target) > 2e-4:
            raise APS100Error(
                f"APS100 target verification failed: requested {target:g} T, read {actual:g} T"
            )
        self._last_target_t = target
        return direction

    def start_sweep_to(self, target_t: float) -> str:
        self._ensure_no_fault()
        if not self.get_heater_status():
            raise APS100SafetyError("Field sweep requires driven mode (heater ON)")
        current = self.get_field_t()
        direction = self._set_directional_target(target_t, current)
        self._write(f"SWEEP {direction} SLOW")
        return direction.lower()

    def _start_output_sweep_to(self, target_t: float) -> str:
        self._ensure_no_fault()
        current = self.get_output_field_t()
        direction = self._set_directional_target(target_t, current)
        self._write(f"SWEEP {direction} SLOW")
        return direction.lower()

    def pause(self, *, confirm: bool = True, timeout_s: float = 3.0) -> None:
        self._write("SWEEP PAUSE")
        if not confirm:
            return
        deadline = time.monotonic() + max(0.1, float(timeout_s))
        last_state = ""
        last_status = None
        while time.monotonic() < deadline:
            state = self.get_sweep_state()
            status = self.get_status()
            last_state = state
            last_status = status
            # The APS100 manual documents "sweep paused". Firmware 1.67.323
            # also returns "standby" or the short form "pause" after
            # SWEEP PAUSE. In any case the sweep-active status bit must be
            # clear before the pause is safe.
            paused_state = "pause" in state or "standby" in state
            if paused_state and not status.sweep_active:
                return
            self._sleep(0.05)
        status_text = (
            f"STB={last_status.raw} (sweep_active={last_status.sweep_active}, "
            f"standby={last_status.standby})"
            if last_status is not None
            else "STB unavailable"
        )
        raise APS100TimeoutError(
            "APS100 did not confirm sweep paused: "
            f"SWEEP?={last_state!r}, {status_text}"
        )

    def wait_for_field(
        self,
        target_t: float,
        *,
        tolerance_t: float = 0.002,
        timeout_s: float = 900.0,
        read_output: bool = False,
        stop_event=None,
        progress: Optional[Callable[[float], None]] = None,
    ) -> float:
        target = self._validate_field(target_t)
        deadline = time.monotonic() + float(timeout_s)
        reader = self.get_output_field_t if read_output else self.get_field_t
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                self.pause(confirm=False)
                raise APS100SafetyError("Magnet operation stopped by user")
            self._ensure_no_fault()
            value = reader()
            if progress is not None:
                progress(value)
            if abs(value - target) <= abs(float(tolerance_t)):
                self.pause()
                return value
            state = self.get_sweep_state()
            if (
                "pause" in state or "standby" in state
            ) and not self.get_status().sweep_active:
                raise APS100SafetyError(
                    f"APS100 paused at {value:.6g} T before reaching {target:.6g} T"
                )
            self._sleep(0.1)
        self.pause(confirm=False)
        raise APS100TimeoutError(f"Timed out moving magnet to {target:g} T")

    def move_to_field(
        self,
        target_t: float,
        *,
        tolerance_t: float = 0.002,
        timeout_s: float = 900.0,
        stop_event=None,
        progress: Optional[Callable[[float], None]] = None,
    ) -> float:
        self.start_sweep_to(target_t)
        return self.wait_for_field(
            target_t,
            tolerance_t=tolerance_t,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )

    def zero_output(
        self,
        *,
        tolerance_t: float = 0.002,
        timeout_s: float = 900.0,
        verify_persistent_switch: bool = False,
        max_magnet_voltage_v: Optional[float] = None,
        stop_event=None,
        progress: Optional[Callable[[float], None]] = None,
    ) -> float:
        """Use the APS100 ZERO operation and require its automatic Standby state."""
        if abs(self.get_output_field_t()) <= abs(float(tolerance_t)):
            return self.get_output_field_t()
        if verify_persistent_switch:
            if max_magnet_voltage_v is None or not math.isfinite(
                float(max_magnet_voltage_v)
            ) or float(max_magnet_voltage_v) <= 0:
                raise APS100SafetyError(
                    "Persistent lead zeroing requires a commissioned positive "
                    "VMAG safety limit"
                )
        self._ensure_no_fault()
        self._write("SWEEP ZERO")
        deadline = time.monotonic() + float(timeout_s)
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                self.pause(confirm=False)
                raise APS100SafetyError("Magnet operation stopped by user")
            self._ensure_no_fault()
            output_t = self.get_output_field_t()
            if verify_persistent_switch:
                magnet_v = abs(self.get_magnet_voltage_v())
                if magnet_v > float(max_magnet_voltage_v):
                    self.pause(confirm=False)
                    raise APS100SafetyError(
                        f"VMAG {magnet_v:.6g} V exceeded the commissioned "
                        f"{float(max_magnet_voltage_v):.6g} V limit while zeroing leads"
                    )
            if progress is not None:
                progress(output_t)
            state = self.get_sweep_state()
            status = self.get_status()
            at_zero = abs(output_t) <= abs(float(tolerance_t))
            if at_zero and status.standby and not status.sweep_active:
                return output_t
            if not status.sweep_active and "zero" not in state:
                raise APS100SafetyError(
                    "APS100 stopped without confirming Standby after SWEEP ZERO: "
                    f"IOUT={output_t:.6g} T, SWEEP?={state!r}, STB={status.raw}"
                )
            self._sleep(0.1)
        self.pause(confirm=False)
        raise APS100TimeoutError("Timed out waiting for SWEEP ZERO and Standby")

    def _hold_transition(
        self,
        seconds: float,
        *,
        label: str,
        progress: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        duration = max(0.0, float(seconds))
        deadline = time.monotonic() + duration
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            if progress is not None:
                progress(label, remaining)
            if remaining <= 0:
                return
            self._ensure_no_fault()
            self._sleep(min(0.5, remaining))

    @staticmethod
    def _raise_if_stopped(stop_event) -> None:
        if stop_event is not None and stop_event.is_set():
            raise APS100SafetyError("Magnet operation stopped by user")

    def enter_driven_mode(
        self,
        *,
        tolerance_t: float = 0.002,
        timeout_s: float = 900.0,
        stop_event=None,
        progress: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        """Safely match the leads, turn the heater on, and wait for warm-up."""
        self._ensure_no_fault()
        if self.get_heater_status():
            return
        magnet_t = self.get_field_t()
        output_t = self.get_output_field_t()
        tolerance_a = self.current_match_tolerance_a
        if abs(output_t - magnet_t) / self.coil_constant_t_per_a > tolerance_a:
            self._start_output_sweep_to(magnet_t)
            self.wait_for_field(
                magnet_t,
                tolerance_t=tolerance_t,
                timeout_s=timeout_s,
                read_output=True,
                stop_event=stop_event,
                progress=(
                    (lambda value: progress("matching leads", value))
                    if progress is not None else None
                ),
            )
        self.pause()
        magnet_t = self.get_field_t()
        output_t = self.get_output_field_t()
        mismatch_a = abs(output_t - magnet_t) / self.coil_constant_t_per_a
        if mismatch_a > tolerance_a:
            raise APS100SafetyError(
                f"Cannot enable heater: current mismatch is {mismatch_a:.6g} A"
            )
        self._write("PSHTR ON")
        if not self.get_heater_status():
            raise APS100SafetyError("APS100 did not report heater ON")
        self._hold_transition(
            self.heater_warm_s,
            label="heater warming",
            progress=progress,
        )
        self._ensure_no_fault()
        # A stop requested while the heater was warming is honored only after
        # the full warm-up interval, leaving the switch in a known safe state.
        self._raise_if_stopped(stop_event)

    def enter_persistent_mode(
        self,
        *,
        zero_leads: bool = True,
        timeout_s: float = 900.0,
        max_magnet_voltage_v: Optional[float] = None,
        stop_event=None,
        progress: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        """Pause, turn the heater off, cool, and optionally zero the leads."""
        self._ensure_no_fault()
        status = self.get_status()
        if status.sweep_active:
            self.pause()
        if not self.get_heater_status():
            if zero_leads and abs(self.get_output_field_t()) > 0.002:
                self.zero_output(
                    tolerance_t=0.002,
                    timeout_s=timeout_s,
                    verify_persistent_switch=True,
                    max_magnet_voltage_v=max_magnet_voltage_v,
                    stop_event=stop_event,
                    progress=(
                        (lambda value: progress("zeroing leads", value))
                        if progress is not None else None
                    ),
                )
            return
        magnet_t = self.get_field_t()
        output_t = self.get_output_field_t()
        mismatch_a = abs(output_t - magnet_t) / self.coil_constant_t_per_a
        if mismatch_a > self.current_match_tolerance_a:
            raise APS100SafetyError(
                f"Cannot disable heater: current mismatch is {mismatch_a:.6g} A"
            )
        self._write("PSHTR OFF")
        if self.get_heater_status():
            raise APS100SafetyError("APS100 did not report heater OFF")
        self._hold_transition(
            self.heater_cool_s,
            label="heater cooling",
            progress=progress,
        )
        # Do not interrupt the mandatory cooling dwell. A stop at this point
        # leaves the magnet persistent with the leads still matched.
        self._raise_if_stopped(stop_event)
        if zero_leads:
            self.zero_output(
                tolerance_t=0.002,
                timeout_s=timeout_s,
                verify_persistent_switch=True,
                max_magnet_voltage_v=max_magnet_voltage_v,
                stop_event=stop_event,
                progress=(
                    (lambda value: progress("zeroing leads", value))
                    if progress is not None else None
                ),
            )

    def safe_move_to_field(
        self,
        target_t: float,
        *,
        final_mode: str = "driven",
        zero_leads: bool = True,
        tolerance_t: float = 0.002,
        settle_s: float = 2.0,
        timeout_s: float = 3600.0,
        persistent_field_confirmed: bool = False,
        max_magnet_voltage_v: Optional[float] = None,
        stop_event=None,
        progress: Optional[Callable[[str, float], None]] = None,
    ) -> MagnetSnapshot:
        """Move from either magnet mode to a target and verified final mode."""
        target = self._validate_field(target_t)
        mode = str(final_mode).strip().lower()
        if mode not in {"driven", "persistent"}:
            raise APS100SafetyError("Final magnet mode must be driven or persistent")
        self.take_remote()
        status = self._ensure_no_fault()
        if status.sweep_active:
            self.pause()
            self._ensure_no_fault()
        self._raise_if_stopped(stop_event)

        heater_on = self.get_heater_status()
        field_t = self.get_field_t()
        output_t = self.get_output_field_t()
        if not heater_on and mode == "persistent" and abs(field_t - target) <= abs(
            float(tolerance_t)
        ):
            if zero_leads and abs(output_t) > abs(float(tolerance_t)):
                self.zero_output(
                    tolerance_t=tolerance_t,
                    timeout_s=timeout_s,
                    verify_persistent_switch=True,
                    max_magnet_voltage_v=max_magnet_voltage_v,
                    stop_event=stop_event,
                    progress=(
                        (lambda value: progress("zeroing leads", value))
                        if progress is not None else None
                    ),
                )
            return self.read_snapshot()

        if not heater_on:
            if not persistent_field_confirmed:
                raise APS100SafetyError(
                    "Confirm the APS100 stored persistent field and polarity before "
                    "matching the power-supply leads"
                )
            if progress is not None:
                progress("entering driven mode", field_t)
            self.enter_driven_mode(
                tolerance_t=tolerance_t,
                timeout_s=timeout_s,
                stop_event=stop_event,
                progress=progress,
            )

        self._raise_if_stopped(stop_event)
        if abs(self.get_field_t() - target) > abs(float(tolerance_t)):
            field_progress = (
                (lambda value: progress("ramping field", value))
                if progress is not None else None
            )
            if abs(target) <= abs(float(tolerance_t)):
                self.zero_output(
                    tolerance_t=tolerance_t,
                    timeout_s=timeout_s,
                    stop_event=stop_event,
                    progress=field_progress,
                )
            else:
                self.move_to_field(
                    target,
                    tolerance_t=tolerance_t,
                    timeout_s=timeout_s,
                    stop_event=stop_event,
                    progress=field_progress,
                )
        else:
            self.pause()

        stable_deadline = time.monotonic() + max(0.0, float(settle_s))
        while time.monotonic() < stable_deadline:
            self._raise_if_stopped(stop_event)
            self._ensure_no_fault()
            field_t = self.get_field_t()
            if abs(field_t - target) > abs(float(tolerance_t)):
                raise APS100SafetyError(
                    f"Field drifted to {field_t:.6g} T while settling at {target:.6g} T"
                )
            if progress is not None:
                progress("field settling", max(0.0, stable_deadline - time.monotonic()))
            self._sleep(min(0.1, max(0.0, stable_deadline - time.monotonic())))

        if mode == "persistent":
            self.enter_persistent_mode(
                zero_leads=zero_leads,
                timeout_s=timeout_s,
                max_magnet_voltage_v=max_magnet_voltage_v,
                stop_event=stop_event,
                progress=progress,
            )
        self._ensure_no_fault()
        snapshot = self.read_snapshot()
        if abs(snapshot.field_t - target) > abs(float(tolerance_t)):
            raise APS100SafetyError(
                f"Final field {snapshot.field_t:.6g} T does not match target {target:.6g} T"
            )
        if mode == "driven" and not snapshot.heater_on:
            raise APS100SafetyError("Final APS100 state is not Driven mode")
        if mode == "persistent" and snapshot.heater_on:
            raise APS100SafetyError("Final APS100 state is not Persistent mode")
        if mode == "persistent" and zero_leads and abs(snapshot.output_field_t) > 0.002:
            raise APS100SafetyError("Persistent mode confirmed, but lead current is not zero")
        return snapshot

    def close(self, *, pause_if_sweeping: bool = True, return_local: bool = True) -> None:
        with self._lock:
            resource = self._resource
            if resource is None:
                return
            if self._identity is not None:
                if pause_if_sweeping:
                    try:
                        if self.get_status().sweep_active:
                            self.pause()
                    except Exception:
                        pass
                if return_local and self._remote:
                    try:
                        self.return_local()
                    except Exception:
                        pass
            try:
                resource.close()
            finally:
                self._resource = None
                self._identity = None
                self._remote = False
            if self._owns_resource_manager and self._resource_manager is not None:
                try:
                    self._resource_manager.close()
                except Exception:
                    pass
                self._resource_manager = None
                self._owns_resource_manager = False


class MockAPS100Adapter:
    """Deterministic APS100 substitute used by the app's mock mode and tests."""

    def __init__(
        self,
        resource_name: str = DEFAULT_RESOURCE,
        *,
        coil_constant_t_per_a: float = 0.20328,
        maximum_field_t: float = 9.0,
        maximum_rate_a_per_s: float = 0.0343,
        heater_warm_s: float = 0.01,
        heater_cool_s: float = 0.01,
        time_scale: float = 600.0,
        **_kwargs,
    ) -> None:
        self.resource_name = resource_name
        self.coil_constant_t_per_a = coil_constant_t_per_a
        self.maximum_field_t = maximum_field_t
        self.maximum_rate_a_per_s = maximum_rate_a_per_s
        self.heater_warm_s = heater_warm_s
        self.heater_cool_s = heater_cool_s
        self._time_scale = max(1.0, float(time_scale))
        self.current_match_tolerance_a = 0.01
        self._identity: Optional[APS100Identity] = None
        self._field_t = 0.0
        self._output_t = 0.0
        self._heater = True
        self._low_t = 0.0
        self._high_t = 0.0
        self._rate_t_per_min = 0.1
        self._target_t: Optional[float] = None
        self._standby = False
        self._magnet_voltage_v = 0.0
        self._zero_commands = 0
        self._last_update = time.monotonic()
        self._remote = False

    @property
    def identity(self):
        return self._identity

    @property
    def connected(self) -> bool:
        return self._identity is not None

    def connect(self):
        self._identity = APS100Identity("Attocube", "APS100", "MOCK", "1.67", "323")
        return self._identity

    def take_remote(self):
        self._remote = True

    def return_local(self):
        self._remote = False

    def _update(self):
        now = time.monotonic()
        elapsed = now - self._last_update
        self._last_update = now
        if self._target_t is None:
            return
        step = self._rate_t_per_min / 60.0 * elapsed * self._time_scale
        delta = self._target_t - self._output_t
        if abs(delta) <= step or step <= 0:
            self._output_t = self._target_t
            if self._heater:
                self._field_t = self._output_t
            self._target_t = None
        else:
            self._output_t += math.copysign(step, delta)
            if self._heater:
                self._field_t = self._output_t

    def get_status(self):
        self._update()
        raw = 1 if self._target_t is not None else (2 if self._standby else 0)
        return decode_status_byte(raw)

    def get_sweep_state(self):
        self._update()
        if self._target_t is None:
            return "standby" if self._standby else "pause"
        return "sweep up" if self._target_t > self._output_t else "sweep down"

    def get_heater_status(self):
        return self._heater

    def get_field_t(self):
        self._update()
        return self._field_t

    def get_output_field_t(self):
        self._update()
        return self._output_t

    def get_limits_t(self):
        return self._low_t, self._high_t

    def get_voltage_limit_v(self):
        return 3.0

    def get_magnet_voltage_v(self):
        return self._magnet_voltage_v

    def get_output_voltage_v(self):
        return 0.0

    def get_units(self):
        return "kG"

    def get_operating_mode(self):
        return "Manual"

    def require_manual_mode(self):
        return self.get_operating_mode()

    def get_range_a(self, index):
        return [40.0, 44.28, 45.0, 89.0, 100.0][index]

    def get_rate_a_per_s(self, _index):
        return self._rate_t_per_min / (self.coil_constant_t_per_a * 60.0)

    def get_rate_t_per_min(self, index):
        return self.get_rate_a_per_s(index) * self.coil_constant_t_per_a * 60.0

    def get_rates(self):
        return {i: (self.get_range_a(i), self.get_rate_t_per_min(i)) for i in range(5)}

    def read_snapshot(self):
        self._update()
        return MagnetSnapshot(
            monotonic_s=time.monotonic(),
            field_t=self._field_t,
            output_field_t=self._output_t,
            output_current_a=self._output_t / self.coil_constant_t_per_a,
            heater_on=self._heater,
            sweep_state=self.get_sweep_state(),
            status=self.get_status(),
            lower_limit_t=self._low_t,
            upper_limit_t=self._high_t,
            voltage_limit_v=3.0,
            magnet_voltage_v=self._magnet_voltage_v,
            output_voltage_v=0.0,
            units="kG",
            operating_mode=self.get_operating_mode(),
        )

    def set_limits_t(self, low_t, high_t):
        if low_t >= high_t or max(abs(low_t), abs(high_t)) > self.maximum_field_t:
            raise APS100SafetyError("Invalid mock APS100 limits")
        self._low_t, self._high_t = float(low_t), float(high_t)
        return self.get_limits_t()

    def set_rate_t_per_min(self, rate_t_per_min, *, max_abs_field_t=None):
        del max_abs_field_t
        max_rate = self.maximum_rate_a_per_s * self.coil_constant_t_per_a * 60.0
        if not 0 < rate_t_per_min <= max_rate:
            raise APS100SafetyError("Invalid mock APS100 rate")
        self._rate_t_per_min = float(rate_t_per_min)
        return {0: self._rate_t_per_min}

    def start_sweep_to(self, target_t):
        if not self._heater:
            raise APS100SafetyError("Field sweep requires driven mode")
        self._update()
        self._standby = False
        target_t = float(target_t)
        if abs(target_t) > self.maximum_field_t:
            raise APS100SafetyError("Target is outside mock field limits")
        direction = "up" if target_t >= self._field_t else "down"
        if direction == "up":
            self._high_t = target_t
        else:
            self._low_t = target_t
        self._target_t = target_t
        return direction

    def pause(self, **_kwargs):
        self._update()
        self._target_t = None
        self._standby = False

    def wait_for_field(
        self,
        target_t,
        *,
        tolerance_t=0.002,
        timeout_s=900.0,
        read_output=False,
        stop_event=None,
        progress=None,
    ):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                self.pause()
                raise APS100SafetyError("Magnet operation stopped by user")
            value = self.get_output_field_t() if read_output else self.get_field_t()
            if progress:
                progress(value)
            if abs(value - target_t) <= tolerance_t:
                self.pause()
                return value
            time.sleep(0.005)
        raise APS100TimeoutError("Mock field timeout")

    def move_to_field(self, target_t, **kwargs):
        self.start_sweep_to(target_t)
        return self.wait_for_field(target_t, **kwargs)

    def zero_output(
        self,
        *,
        tolerance_t=0.002,
        verify_persistent_switch=False,
        max_magnet_voltage_v=None,
        stop_event=None,
        progress=None,
        **_kwargs,
    ):
        if stop_event is not None and stop_event.is_set():
            raise APS100SafetyError("Magnet operation stopped by user")
        if verify_persistent_switch:
            if max_magnet_voltage_v is None or float(max_magnet_voltage_v) <= 0:
                raise APS100SafetyError(
                    "Persistent lead zeroing requires a commissioned positive VMAG safety limit"
                )
            if abs(self._magnet_voltage_v) > float(max_magnet_voltage_v):
                raise APS100SafetyError("VMAG exceeded the commissioned limit while zeroing leads")
        self._zero_commands += 1
        self._output_t = 0.0
        if self._heater:
            self._field_t = 0.0
        self._target_t = None
        self._standby = True
        if progress:
            progress(0.0)
        return self._output_t

    def enter_driven_mode(self, *, progress=None, **_kwargs):
        self._output_t = self._field_t
        self._heater = True
        self._standby = False
        if progress:
            progress("heater warming", 0.0)

    def enter_persistent_mode(
        self,
        *,
        zero_leads=True,
        max_magnet_voltage_v=None,
        progress=None,
        **_kwargs,
    ):
        if self._target_t is not None:
            self.pause()
        self._heater = False
        if progress:
            progress("heater cooling", 0.0)
        if zero_leads and abs(self._output_t) > 0.002:
            self.zero_output(
                verify_persistent_switch=True,
                max_magnet_voltage_v=max_magnet_voltage_v,
                progress=(
                    (lambda value: progress("zeroing leads", value))
                    if progress is not None else None
                ),
            )

    def safe_move_to_field(
        self,
        target_t,
        *,
        final_mode="driven",
        zero_leads=True,
        tolerance_t=0.002,
        settle_s=0.0,
        timeout_s=3600.0,
        persistent_field_confirmed=False,
        max_magnet_voltage_v=None,
        stop_event=None,
        progress=None,
    ):
        del settle_s
        self.take_remote()
        if stop_event is not None and stop_event.is_set():
            raise APS100SafetyError("Magnet operation stopped by user")
        if self.get_status().sweep_active:
            self.pause()
        mode = str(final_mode).strip().lower()
        if mode not in {"driven", "persistent"}:
            raise APS100SafetyError("Final magnet mode must be driven or persistent")
        target_t = float(target_t)
        if (
            not self._heater
            and mode == "persistent"
            and abs(self._field_t - target_t) <= tolerance_t
        ):
            if zero_leads and abs(self._output_t) > tolerance_t:
                self.zero_output(
                    verify_persistent_switch=True,
                    max_magnet_voltage_v=max_magnet_voltage_v,
                    progress=(
                        (lambda value: progress("zeroing leads", value))
                        if progress is not None else None
                    ),
                )
            return self.read_snapshot()
        if not self._heater:
            if not persistent_field_confirmed:
                raise APS100SafetyError(
                    "Confirm the APS100 stored persistent field and polarity before matching"
                )
            self.enter_driven_mode(progress=progress)
        if abs(self.get_field_t() - target_t) > abs(float(tolerance_t)):
            field_progress = (
                (lambda value: progress("ramping field", value))
                if progress is not None else None
            )
            if abs(target_t) <= tolerance_t:
                self.zero_output(stop_event=stop_event, progress=field_progress)
            else:
                self.move_to_field(
                    target_t,
                    tolerance_t=tolerance_t,
                    timeout_s=timeout_s,
                    stop_event=stop_event,
                    progress=field_progress,
                )
        if mode == "persistent":
            self.enter_persistent_mode(
                zero_leads=zero_leads,
                max_magnet_voltage_v=max_magnet_voltage_v,
                progress=progress,
            )
        return self.read_snapshot()

    def close(self, **_kwargs):
        self.pause()
        self._identity = None
        self._remote = False
