"""Read-only attoDRY2100 adapter.

The vendor package is deliberately loaded into a private, hash-named module
namespace.  Importing this module never changes ``sys.path`` and tests can
inject a Device factory without loading vendor code.
"""
from __future__ import annotations

import hashlib
import importlib.util
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

HARD_MAX_FIELD_T = 6.0
HARD_MAX_TEMPERATURE_K = 7.0


class AttoDRY2100Error(RuntimeError): pass
class AttoDRY2100LoadError(AttoDRY2100Error): pass
class AttoDRY2100ConnectionError(AttoDRY2100Error): pass
class AttoDRY2100TelemetryError(AttoDRY2100Error): pass
class AttoDRY2100CommunicationError(AttoDRY2100TelemetryError): pass
class AttoDRY2100TimeoutError(AttoDRY2100CommunicationError): pass
class AttoDRY2100SafetyError(AttoDRY2100Error): pass
class AttoDRY2100StoppedError(AttoDRY2100Error): pass
class AttoDRY2100StateError(AttoDRY2100Error): pass
class AttoDRY2100VerificationError(AttoDRY2100Error): pass


@dataclass(frozen=True)
class AttoDRY2100Identity:
    backend: str = "attodry2100"
    host: str = ""
    channel: int = 0
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AttoDRY2100Capabilities:
    temperature_readback: bool = True
    quench_reporting: bool = True
    stop_pause: bool = True
    discrete_field_points: bool = False
    continuous_ramp: bool = True
    set_h_setpoint: bool = True
    start_field_control: bool = True
    stop_field_control: bool = True
    ramp_rate_edit: bool = False
    remote_mode: bool = False
    programmable_field_limits: bool = False
    programmable_ramp_rate: bool = False
    persistent_driven_mode: bool = True
    lead_field_readback: bool = True
    lead_current_readback: bool = False
    sample_temperature_readback: bool = False
    sample_temperature_control: bool = False
    vti_temperature_readback: bool = False


@dataclass(frozen=True)
class AttoDRY2100Status:
    field_control_state: Optional[Any] = None
    driven_mode: Optional[Any] = None
    persistent_mode: Optional[Any] = None
    heater_on: Optional[Any] = None
    leads_hot: Optional[Any] = None
    quench: Optional[bool] = None
    backend_details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AttoDRY2100Snapshot:
    field_t: float
    monotonic_s: float = field(default_factory=time.monotonic)
    setpoint_t: Optional[float] = None
    temperature_k: Optional[float] = None
    status: AttoDRY2100Status = field(default_factory=AttoDRY2100Status)
    lead_field_t: Optional[float] = None
    capabilities: AttoDRY2100Capabilities = field(default_factory=AttoDRY2100Capabilities)


@dataclass(frozen=True)
class AttoDRY2100TemperatureSnapshot:
    monotonic_s: float = field(default_factory=time.monotonic)
    sample_temperature_k: Optional[float] = None
    sample_setpoint_k: Optional[float] = None
    sample_control_active: Optional[bool] = None
    sample_ramp_active: Optional[bool] = None
    sample_ramp_rate_k_per_min: Optional[float] = None
    vti_temperature_k: Optional[float] = None
    vti_setpoint_k: Optional[float] = None
    vti_control_active: Optional[bool] = None


def _load_sdk(sdk_directory: str | Path):
    root = Path(sdk_directory).expanduser().resolve()
    if not root.is_dir():
        raise AttoDRY2100LoadError(f"SDK directory does not exist: {root}")
    candidates = [root / "atto_device" / "__init__.py", root / "__init__.py"]
    init = next((p for p in candidates if p.is_file()), None)
    if init is None:
        raise AttoDRY2100LoadError(f"No Python SDK package found in {root}")
    digest = hashlib.sha256(str(init).encode("utf-8")).hexdigest()[:16]
    name = f"_attodry2100_sdk_{digest}"
    kwargs = {"submodule_search_locations": [str(init.parent)]} if init.name == "__init__.py" else {}
    spec = importlib.util.spec_from_file_location(name, str(init), **kwargs)
    if spec is None or spec.loader is None:
        raise AttoDRY2100LoadError(f"Could not create SDK loader for {init}")
    module = importlib.util.module_from_spec(spec)
    # Register only the private, hash-named package so relative SDK imports
    # work; never expose the vendor directory through sys.path.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)
        raise AttoDRY2100LoadError(f"Unable to load attoDRY2100 SDK from {init}: {exc}") from exc
    if not hasattr(module, "Device"):
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)
        raise AttoDRY2100LoadError(f"SDK module {init} does not export Device")
    if not callable(module.Device):
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)
        raise AttoDRY2100LoadError(f"SDK module {init} exports a non-callable Device")
    return module.Device


class AttoDRY2100Adapter:
    def __init__(self, sdk_directory, host="192.168.1.1", channel=0, timeout_s=10.0, device_factory=None, maximum_field_t=None, minimum_temperature_k=None, maximum_temperature_k=None, setpoint_tolerance_t=1e-6):
        if not isinstance(host, str) or not host.strip(): raise ValueError("host must be a non-blank string")
        if isinstance(channel, bool) or not isinstance(channel, int) or channel < 0:
            raise ValueError("channel must be a non-negative integer")
        self.sdk_directory = str(sdk_directory)
        self.host = host
        self.channel = int(channel)
        self.timeout_s = float(timeout_s)
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0: raise ValueError("timeout_s must be positive and finite")
        self.maximum_field_t = maximum_field_t
        self.minimum_temperature_k = minimum_temperature_k
        self.maximum_temperature_k = maximum_temperature_k
        self.setpoint_tolerance_t = float(setpoint_tolerance_t)
        self._device_factory = device_factory
        self._device = None
        self._magnet = None
        self._sample = None
        self._vti = None
        self._identity = None

    @property
    def connected(self): return self._device is not None
    @property
    def identity(self): return self._identity

    def connect(self):
        if self.connected: return self._identity
        if self.channel < 0: raise AttoDRY2100ConnectionError("channel must be non-negative")
        device = None
        try:
            factory = self._device_factory or _load_sdk(self.sdk_directory)
            device = factory(self.host)
            device.connect()
            magnet = getattr(device, "magnet", None)
            if magnet is None or not callable(getattr(magnet, "getH", None)):
                raise AttoDRY2100ConnectionError("SDK device has no magnet.getH(channel) API")
            count_fn = getattr(device, "getNumberOfMagnetChannels", None) or getattr(magnet, "getNumberOfMagnetChannels", None)
            if not callable(count_fn): raise AttoDRY2100ConnectionError("SDK device has no magnet channel-count API")
            count = count_fn()
            if isinstance(count, bool) or int(count) != count or int(count) <= 0 or self.channel >= int(count):
                raise AttoDRY2100ConnectionError(f"channel {self.channel} is outside SDK channel count {count}")
            # Validate channel/readback without changing hardware state.
            try:
                field_t = magnet.getH(self.channel)
            except TimeoutError as exc: raise AttoDRY2100TimeoutError(f"getH channel={self.channel}: {exc}") from exc
            except OSError as exc: raise AttoDRY2100CommunicationError(f"getH channel={self.channel}: {exc}") from exc
            except Exception as exc: raise AttoDRY2100CommunicationError(f"getH channel={self.channel}: {exc}") from exc
            try: field_numeric = float(field_t)
            except Exception as exc: raise AttoDRY2100TelemetryError(f"getH returned non-numeric field: {field_t!r}") from exc
            if not math.isfinite(field_numeric):
                raise AttoDRY2100TelemetryError("getH returned a non-finite field")
            details = {}
            system = getattr(device, "system", None)
            service = getattr(device, "system_service", None)
            for key, obj, names in (("device_type", system, ("getDeviceType",)), ("device_name", service, ("getDeviceName",)), ("serial", service, ("getSerialNumber",)), ("firmware", service, ("getFirmwareVersion",)), ("channel_name", magnet, ("getMagnetChannelName",))):
                for name in names:
                    fn = getattr(obj, name, None) if obj is not None else None
                    if callable(fn):
                        try: details[key] = fn(self.channel) if key == "channel_name" else fn()
                        except Exception: pass
                        break
            self._identity = AttoDRY2100Identity(host=self.host, channel=self.channel, details=details)
            self._device, self._magnet = device, magnet
            self._sample = getattr(device, "sample", None)
            self._vti = getattr(device, "vti", None)
            return self._identity
        except AttoDRY2100Error:
            self._identity = None
            try: device.close()
            except Exception: pass
            raise
        except TimeoutError as exc:
            self._identity = None
            try: device.close()
            except Exception: pass
            raise AttoDRY2100TimeoutError(f"connect host={self.host}: {exc}") from exc
        except Exception as exc:
            try: device.close()
            except Exception: pass
            self._identity = None
            raise AttoDRY2100ConnectionError(f"attoDRY2100 connection failed: {exc}") from exc

    def close(self):
        device = self._device
        try:
            if device is not None: device.close()
        except TimeoutError as exc: raise AttoDRY2100TimeoutError(f"close: {exc}") from exc
        except Exception as exc: raise AttoDRY2100CommunicationError(f"close: {exc}") from exc
        self._device = None
        self._magnet = None
        self._sample = None
        self._vti = None
        self._identity = None

    def _optional(self, name, *args):
        fn = getattr(self._magnet, name, None)
        if not callable(fn): return None
        try: return fn(*args)
        except (TimeoutError, TimeoutError) as exc: raise AttoDRY2100TimeoutError(f"{name} channel={self.channel}: {exc}") from exc
        except Exception as exc: raise AttoDRY2100CommunicationError(f"{name} channel={self.channel}: {exc}") from exc

    def _temperature_call(self, component, name, *args, required=True):
        if component is None:
            if required:
                raise AttoDRY2100StateError("sample temperature control is unavailable")
            return None
        fn = getattr(component, name, None)
        if not callable(fn):
            if required:
                raise AttoDRY2100StateError(f"temperature API {name} is unavailable")
            return None
        try:
            return fn(*args)
        except TimeoutError as exc:
            raise AttoDRY2100TimeoutError(f"temperature {name}: {exc}") from exc
        except Exception as exc:
            raise AttoDRY2100CommunicationError(f"temperature {name}: {exc}") from exc

    @staticmethod
    def _finite_optional(value, name):
        if value is None: return None
        try: value = float(value)
        except Exception as exc: raise AttoDRY2100TelemetryError(f"{name} is not numeric") from exc
        if not math.isfinite(value): raise AttoDRY2100TelemetryError(f"{name} is non-finite")
        return value

    def read_snapshot(self):
        if not self.connected: raise AttoDRY2100ConnectionError("attoDRY2100 is not connected")
        field_t = self.read_field()
        setpoint = self._finite_optional(self._optional("getHSetPoint", self.channel), "setpoint")
        temperature = self._finite_optional(self._optional("getTemperature"), "temperature")
        status = AttoDRY2100Status(
            field_control_state=self._optional("getHState", self.channel),
            driven_mode=self._optional("getDrivenMode", self.channel),
            persistent_mode=self._optional("getPersistentMode", self.channel),
            heater_on=self._optional("getPersistentSwitchHeaterStatus", self.channel),
            leads_hot=self._optional("getLeadsHot"),
            quench=self._optional("getIsInQuenchState"),
            backend_details={"field_control": self._optional("getFieldControl", self.channel)},
        )
        capabilities = AttoDRY2100Capabilities(
            sample_temperature_readback=callable(getattr(self._sample, "getTemperature", None)),
            sample_temperature_control=all(callable(getattr(self._sample, name, None)) for name in (
                "setSetPoint", "setRampRate", "startTempControl", "startRampControl",
            )),
            vti_temperature_readback=callable(getattr(self._vti, "getTemperature", None)),
        )
        return AttoDRY2100Snapshot(field_t=field_t, monotonic_s=time.monotonic(), setpoint_t=setpoint,
                                   temperature_k=temperature, status=status,
                                   lead_field_t=self._finite_optional(self._optional("getFieldsInLeads", self.channel), "lead field"),
                                   capabilities=capabilities)

    def read_field(self) -> float:
        """Read only magnet field for timing-critical continuous acquisition."""
        if not self.connected: raise AttoDRY2100ConnectionError("attoDRY2100 is not connected")
        try: raw_field = self._magnet.getH(self.channel)
        except TimeoutError as exc: raise AttoDRY2100TimeoutError(f"getH channel={self.channel}: {exc}") from exc
        except Exception as exc: raise AttoDRY2100CommunicationError(f"getH channel={self.channel}: {exc}") from exc
        try: field_t = float(raw_field)
        except Exception as exc: raise AttoDRY2100TelemetryError(f"getH returned non-numeric field: {raw_field!r}") from exc
        if not math.isfinite(field_t): raise AttoDRY2100TelemetryError("getH returned a non-finite field")
        return field_t

    def read_sample_temperature(self) -> float:
        if not self.connected:
            raise AttoDRY2100ConnectionError("attoDRY2100 is not connected")
        value = self._finite_optional(
            self._temperature_call(self._sample, "getTemperature"),
            "sample temperature",
        )
        if value is None:
            raise AttoDRY2100TelemetryError("sample temperature is unavailable")
        return value

    def read_temperature_snapshot(self) -> AttoDRY2100TemperatureSnapshot:
        if not self.connected:
            raise AttoDRY2100ConnectionError("attoDRY2100 is not connected")
        sample_temperature = self.read_sample_temperature()
        sample_setpoint = self._finite_optional(
            self._temperature_call(self._sample, "getSetPoint"), "sample setpoint"
        )
        sample_control = self._temperature_call(
            self._sample, "getTempControlStatus"
        )
        sample_ramp = self._temperature_call(
            self._sample, "getRampControlStatus", required=False
        )
        sample_rate = self._finite_optional(
            self._temperature_call(self._sample, "getRampRate", required=False),
            "sample ramp rate",
        )
        vti_temperature = self._finite_optional(
            self._temperature_call(self._vti, "getTemperature", required=False),
            "VTI temperature",
        )
        vti_setpoint = self._finite_optional(
            self._temperature_call(self._vti, "getSetPoint", required=False),
            "VTI setpoint",
        )
        vti_control = self._temperature_call(
            self._vti, "getTempControlStatus", required=False
        )
        return AttoDRY2100TemperatureSnapshot(
            sample_temperature_k=sample_temperature,
            sample_setpoint_k=sample_setpoint,
            sample_control_active=(sample_control if type(sample_control) is bool else None),
            sample_ramp_active=(sample_ramp if type(sample_ramp) is bool else None),
            sample_ramp_rate_k_per_min=sample_rate,
            vti_temperature_k=vti_temperature,
            vti_setpoint_k=vti_setpoint,
            vti_control_active=(vti_control if type(vti_control) is bool else None),
        )

    @staticmethod
    def _validate_temperature_request(target_k, ramp_rate_k_per_min):
        try:
            target = float(target_k)
            ramp_rate = float(ramp_rate_k_per_min)
        except (TypeError, ValueError) as exc:
            raise AttoDRY2100SafetyError("sample target and ramp rate must be numeric") from exc
        if not math.isfinite(target) or not 1.8 <= target <= 300.0:
            raise AttoDRY2100SafetyError("sample target must be within 1.8 to 300 K")
        if not math.isfinite(ramp_rate) or not 0.1 <= ramp_rate <= 100.0:
            raise AttoDRY2100SafetyError("sample ramp rate must be within 0.1 to 100 K/min")
        return target, ramp_rate

    def configure_sample_temperature(self, target_k, ramp_rate_k_per_min, stop_event=None):
        if not self.connected:
            raise AttoDRY2100StateError("attoDRY2100 is not connected")
        target, ramp_rate = self._validate_temperature_request(target_k, ramp_rate_k_per_min)
        if stop_event is not None and stop_event.is_set():
            raise AttoDRY2100StoppedError("stop requested")
        self._temperature_call(self._sample, "setSetPoint", target)
        actual_target = self._finite_optional(
            self._temperature_call(self._sample, "getSetPoint"), "sample setpoint"
        )
        if actual_target is None or abs(actual_target - target) > 0.01:
            raise AttoDRY2100VerificationError("sample temperature setpoint readback mismatch")
        if stop_event is not None and stop_event.is_set():
            raise AttoDRY2100StoppedError("stop requested")
        self._temperature_call(self._sample, "setRampRate", ramp_rate)
        actual_rate = self._finite_optional(
            self._temperature_call(self._sample, "getRampRate"), "sample ramp rate"
        )
        if actual_rate is None or abs(actual_rate - ramp_rate) > 0.01:
            raise AttoDRY2100VerificationError("sample ramp-rate readback mismatch")
        if stop_event is not None and stop_event.is_set():
            raise AttoDRY2100StoppedError("stop requested")
        self._temperature_call(self._sample, "startTempControl")
        self._temperature_call(self._sample, "startRampControl")
        snapshot = self.read_temperature_snapshot()
        if snapshot.sample_control_active is not True:
            raise AttoDRY2100VerificationError("sample temperature control did not become active")
        if snapshot.sample_ramp_active is not True:
            raise AttoDRY2100VerificationError("sample temperature ramp control did not become active")
        return snapshot

    def stop_sample_temperature_control(self):
        if not self.connected:
            raise AttoDRY2100StateError("attoDRY2100 is not connected")
        self._temperature_call(self._sample, "stopRampControl")
        self._temperature_call(self._sample, "stopTempControl")
        snapshot = self.read_temperature_snapshot()
        if snapshot.sample_control_active is not False:
            raise AttoDRY2100VerificationError("sample temperature control did not stop")
        return snapshot

    def _motion_limits(self):
        try:
            maximum = float(self.maximum_field_t)
            maximum_temperature = float(self.maximum_temperature_k)
        except (TypeError, ValueError) as exc:
            raise AttoDRY2100SafetyError(
                "field and maximum temperature safety limits must be explicitly configured"
            ) from exc
        if not math.isfinite(maximum) or maximum <= 0:
            raise AttoDRY2100SafetyError("maximum field must be positive and finite")
        if not math.isfinite(maximum_temperature):
            raise AttoDRY2100SafetyError("maximum temperature must be finite")
        return min(maximum, HARD_MAX_FIELD_T), min(maximum_temperature, HARD_MAX_TEMPERATURE_K)

    def _preflight(self, target=None, stop_event=None):
        if stop_event is not None and stop_event.is_set(): raise AttoDRY2100StoppedError("stop requested")
        if not self.connected: raise AttoDRY2100StateError("2100 is not connected")
        maximum, maximum_temperature = self._motion_limits()
        try:
            snap = self.read_snapshot()
        except AttoDRY2100TelemetryError as exc:
            raise AttoDRY2100SafetyError(
                "required safety telemetry is unavailable or invalid"
            ) from exc
        if snap.status.quench is not False: raise AttoDRY2100SafetyError("quench telemetry is not explicitly safe")
        if abs(snap.field_t) > HARD_MAX_FIELD_T or abs(snap.field_t) > maximum:
            raise AttoDRY2100SafetyError("current field exceeds configured safety range")
        if snap.temperature_k is None:
            raise AttoDRY2100SafetyError("temperature telemetry is unavailable")
        if snap.temperature_k > maximum_temperature: raise AttoDRY2100SafetyError("temperature above configured maximum")
        driven_mode = snap.status.driven_mode
        persistent_mode = snap.status.persistent_mode
        if type(driven_mode) is not bool or type(persistent_mode) is not bool:
            raise AttoDRY2100SafetyError(
                "attoDRY2100 operating mode telemetry is unavailable or invalid; "
                "set the magnet to Driven mode manually before starting MCD"
            )
        if driven_mode is not True or persistent_mode is not False:
            raise AttoDRY2100SafetyError(
                "attoDRY2100 is not in Driven mode. Set the magnet to Driven mode "
                "manually before starting MCD."
            )
        if target is not None:
            try: target_value = float(target)
            except (TypeError, ValueError) as exc: raise AttoDRY2100SafetyError("target field must be numeric") from exc
            if not math.isfinite(target_value): raise AttoDRY2100SafetyError("target field must be finite")
            if abs(target_value) > maximum: raise AttoDRY2100SafetyError("target exceeds configured field limit")
        if stop_event is not None and stop_event.is_set(): raise AttoDRY2100StoppedError("stop requested")
        return snap

    def set_h_setpoint(self, target, stop_event=None):
        self._preflight(target, stop_event)
        if stop_event is not None and stop_event.is_set(): raise AttoDRY2100StoppedError("stop requested")
        try: self._magnet.setHSetPoint(self.channel, float(target))
        except Exception as exc: raise AttoDRY2100CommunicationError(f"setHSetPoint channel={self.channel}: {exc}") from exc
        actual = self._finite_optional(self._optional("getHSetPoint", self.channel), "setpoint")
        if actual is None or abs(actual - float(target)) > self.setpoint_tolerance_t: raise AttoDRY2100VerificationError("setpoint readback mismatch")
        return actual

    def start_field_control(self, expected_target=None, stop_event=None):
        if expected_target is None: raise AttoDRY2100StateError("a verified target is required before field control starts")
        snap = self._preflight(expected_target, stop_event)
        if snap.setpoint_t is None or abs(snap.setpoint_t-float(expected_target)) > self.setpoint_tolerance_t: raise AttoDRY2100VerificationError("start target mismatch")
        if stop_event is not None and stop_event.is_set(): raise AttoDRY2100StoppedError("stop requested")
        fn = getattr(self._magnet, "startFieldControl", None)
        if not callable(fn): raise AttoDRY2100StateError("startFieldControl is unavailable")
        try: result = fn(self.channel)
        except Exception as exc: raise AttoDRY2100CommunicationError(f"startFieldControl channel={self.channel}: {exc}") from exc
        state = self._optional("getFieldControl", self.channel)
        if state is not True: raise AttoDRY2100VerificationError("field control did not become active")
        return result

    def verify_continuous_completion(self, target, gate_t):
        """Fresh, read-only completion verification for a continuous leg."""
        try:
            gate = float(gate_t)
            target_value = float(target)
        except (TypeError, ValueError) as exc:
            raise AttoDRY2100SafetyError("continuous completion gate and target must be numeric") from exc
        if not math.isfinite(gate) or gate <= 0 or not math.isfinite(target_value):
            raise AttoDRY2100SafetyError("continuous completion gate and target must be finite")
        snap = self._preflight(target_value)
        if snap.status.backend_details.get("field_control") is not True:
            raise AttoDRY2100VerificationError("field control is not active at completion")
        if snap.setpoint_t is None or abs(snap.setpoint_t - target_value) > self.setpoint_tolerance_t:
            raise AttoDRY2100VerificationError("continuous completion setpoint mismatch")
        if abs(snap.field_t - target_value) > gate:
            raise AttoDRY2100VerificationError("continuous completion field is outside the target gate")
        return snap

    def verify_continuous_completion_snapshot(self, snapshot, target, gate_t, *, max_age_s=30.0):
        """Validate the exact recent full snapshot accepted by the sweep endpoint."""
        if not isinstance(snapshot, AttoDRY2100Snapshot):
            raise AttoDRY2100VerificationError("completion snapshot is missing or invalid")
        try:
            gate = float(gate_t)
            target_value = float(target)
            max_age = float(max_age_s)
        except (TypeError, ValueError) as exc:
            raise AttoDRY2100SafetyError(
                "completion gate, target, and snapshot age must be numeric"
            ) from exc
        if (not math.isfinite(gate) or gate <= 0 or not math.isfinite(target_value)
                or not math.isfinite(max_age) or max_age <= 0):
            raise AttoDRY2100SafetyError(
                "completion gate, target, and snapshot age must be finite and positive"
            )
        age_s = time.monotonic() - snapshot.monotonic_s
        if age_s < 0 or age_s > max_age:
            raise AttoDRY2100VerificationError(
                f"completion snapshot is stale ({age_s:.1f} s old)"
            )
        maximum, maximum_temperature = self._motion_limits()
        if snapshot.status.quench is not False:
            raise AttoDRY2100SafetyError("quench telemetry is not explicitly safe at completion")
        if abs(snapshot.field_t) > min(maximum, HARD_MAX_FIELD_T):
            raise AttoDRY2100SafetyError("completion field exceeds configured safety range")
        if snapshot.temperature_k is None or snapshot.temperature_k > maximum_temperature:
            raise AttoDRY2100SafetyError("completion temperature is unavailable or unsafe")
        if snapshot.status.driven_mode is not True or snapshot.status.persistent_mode is not False:
            raise AttoDRY2100SafetyError("completion snapshot is not in Driven mode")
        if snapshot.status.backend_details.get("field_control") is not True:
            raise AttoDRY2100VerificationError("field control is not active at completion")
        if snapshot.setpoint_t is None or abs(snapshot.setpoint_t - target_value) > self.setpoint_tolerance_t:
            raise AttoDRY2100VerificationError("continuous completion setpoint mismatch")
        if abs(snapshot.field_t - target_value) > gate:
            raise AttoDRY2100VerificationError("continuous completion field is outside the target gate")
        return snapshot

    def stop_field_control(self, stop_event=None):
        if not self.connected: raise AttoDRY2100StateError("2100 is not connected")
        fn = getattr(self._magnet, "stopFieldControl", None)
        if not callable(fn): raise AttoDRY2100StateError("stopFieldControl is unavailable")
        try: result = fn(self.channel)
        except Exception as exc: raise AttoDRY2100CommunicationError(f"stopFieldControl channel={self.channel}: {exc}") from exc
        # The SDK documents the synchronous vendor acknowledgement, but does
        # not define a post-stop getFieldControl=False state transition. Keep
        # any later snapshot as an observation for commissioning evidence.
        return result


Attodry2100Adapter = AttoDRY2100Adapter
