import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from app.devices.attodry2100_adapter import (
    AttoDRY2100Adapter, AttoDRY2100ConnectionError,
    AttoDRY2100CommunicationError,
    AttoDRY2100LoadError, AttoDRY2100TelemetryError,
    AttoDRY2100SafetyError, AttoDRY2100StoppedError,
    AttoDRY2100VerificationError,
    HARD_MAX_FIELD_T, HARD_MAX_TEMPERATURE_K,
)


class FakeMagnet:
    def __init__(self):
        self.calls = []; self.field = 1.25; self.setpoint = 1.3
        self.temperature = 1.8; self.quench = False; self.field_control = False
        self.field_state = "stable"
        self.driven_mode = True; self.persistent_mode = False
    def _v(self, name, *args): self.calls.append((name, args)); return {"getH": self.field, "getHSetPoint": self.setpoint, "getHState": self.field_state, "getTemperature": self.temperature, "getDrivenMode": self.driven_mode, "getPersistentMode": self.persistent_mode, "getPersistentSwitchHeaterStatus": True, "getLeadsHot": False, "getFieldsInLeads": 0.01, "getFieldControl": self.field_control, "getIsInQuenchState": self.quench}[name]
    getH = lambda s, *a: s._v("getH", *a)
    getHSetPoint = lambda s, *a: s._v("getHSetPoint", *a)
    getHState = lambda s, *a: s._v("getHState", *a)
    getTemperature = lambda s, *a: s._v("getTemperature", *a)
    getDrivenMode = lambda s, *a: s._v("getDrivenMode", *a)
    getPersistentMode = lambda s, *a: s._v("getPersistentMode", *a)
    getPersistentSwitchHeaterStatus = lambda s, *a: s._v("getPersistentSwitchHeaterStatus", *a)
    getLeadsHot = lambda s, *a: s._v("getLeadsHot", *a)
    getFieldsInLeads = lambda s, *a: s._v("getFieldsInLeads", *a)
    getFieldControl = lambda s, *a: s._v("getFieldControl", *a)
    getIsInQuenchState = lambda s, *a: s._v("getIsInQuenchState", *a)
    def setHSetPoint(self, channel, target): self.calls.append(("setHSetPoint", (channel, target))); self.setpoint = target
    def startFieldControl(self, channel): self.calls.append(("startFieldControl", (channel,))); self.field_control = True
    def stopFieldControl(self, channel): self.calls.append(("stopFieldControl", (channel,))); self.field_control = False


class FakeTemperatureStage:
    def __init__(self, temperature=4.0):
        self.calls = []
        self.temperature = float(temperature)
        self.setpoint = float(temperature)
        self.control = False
        self.ramp = False
        self.ramp_rate = 1.0
    def _get(self, name, value): self.calls.append((name, ())); return value
    def getTemperature(self): return self._get("getTemperature", self.temperature)
    def getSetPoint(self): return self._get("getSetPoint", self.setpoint)
    def getTempControlStatus(self): return self._get("getTempControlStatus", self.control)
    def getRampControlStatus(self): return self._get("getRampControlStatus", self.ramp)
    def getRampRate(self): return self._get("getRampRate", self.ramp_rate)
    def setSetPoint(self, value): self.calls.append(("setSetPoint", (value,))); self.setpoint = float(value)
    def setRampRate(self, value): self.calls.append(("setRampRate", (value,))); self.ramp_rate = float(value)
    def startTempControl(self): self.calls.append(("startTempControl", ())); self.control = True
    def startRampControl(self): self.calls.append(("startRampControl", ())); self.ramp = True
    def stopTempControl(self): self.calls.append(("stopTempControl", ())); self.control = False
    def stopRampControl(self): self.calls.append(("stopRampControl", ())); self.ramp = False


class FakeDevice:
    def __init__(self, host):
        self.host = host; self.magnet = FakeMagnet(); self.closed = 0
        self.sample = FakeTemperatureStage(4.0)
        self.vti = FakeTemperatureStage(2.0)
        self.system = type("System", (), {"getDeviceType": lambda s: "2100"})()
        self.system_service = type("Service", (), {"getDeviceName": lambda s: "Cryo", "getSerialNumber": lambda s: "S1", "getFirmwareVersion": lambda s: "F1"})()
    def connect(self): pass
    def getNumberOfMagnetChannels(self): return 4
    def close(self): self.closed += 1


class AdapterTests(unittest.TestCase):
    def test_high_level_sample_temperature_control_never_mutates_vti(self):
        for target in (8.0, 20.0):
            with self.subTest(target=target):
                device = FakeDevice("host")
                adapter = AttoDRY2100Adapter(
                    "unused", maximum_field_t=6.0, maximum_temperature_k=7.0,
                    device_factory=lambda _: device,
                )
                adapter.connect()
                result = adapter.configure_sample_temperature(target, 2.5)
                self.assertEqual(result.sample_setpoint_k, target)
                self.assertTrue(result.sample_control_active)
                self.assertTrue(result.sample_ramp_active)
                sample_names = [name for name, _ in device.sample.calls]
                self.assertIn("setSetPoint", sample_names)
                self.assertIn("startTempControl", sample_names)
                self.assertIn("startRampControl", sample_names)
                self.assertFalse(any(name.startswith("set") or name.startswith("start")
                                     for name, _ in device.vti.calls))
                adapter.close()

    def test_temperature_request_validation_and_readback_are_fail_closed(self):
        device = FakeDevice("host")
        adapter = AttoDRY2100Adapter(
            "unused", maximum_field_t=6.0, maximum_temperature_k=7.0,
            device_factory=lambda _: device,
        )
        adapter.connect()
        for target, rate in ((1.7, 1.0), (301.0, 1.0), (4.0, 0.09), (4.0, 101.0)):
            with self.subTest(target=target, rate=rate), self.assertRaises(AttoDRY2100SafetyError):
                adapter.configure_sample_temperature(target, rate)
        original = device.sample.setSetPoint
        device.sample.setSetPoint = lambda value: device.sample.calls.append(("setSetPoint", (value,)))
        with self.assertRaises(AttoDRY2100VerificationError):
            adapter.configure_sample_temperature(10.0, 1.0)
        self.assertNotIn("startTempControl", [name for name, _ in device.sample.calls])
        device.sample.setSetPoint = original
        adapter.close()
    def test_named_hardware_safety_ceilings_are_fixed(self):
        self.assertEqual(HARD_MAX_FIELD_T, 6.0)
        self.assertEqual(HARD_MAX_TEMPERATURE_K, 7.0)

    def test_hard_ceilings_override_looser_configuration(self):
        device = FakeDevice("host")
        adapter = AttoDRY2100Adapter("unused", maximum_field_t=9.0, maximum_temperature_k=9.0,
                                     device_factory=lambda _: device)
        adapter.connect()
        with self.assertRaises(AttoDRY2100SafetyError):
            adapter.set_h_setpoint(6.0001)
        device.magnet.temperature = 7.0001
        with self.assertRaises(AttoDRY2100SafetyError):
            adapter.set_h_setpoint(1.0)
        adapter.close()

    def test_completion_requires_active_field_control(self):
        device = FakeDevice("host")
        adapter = AttoDRY2100Adapter("unused", maximum_field_t=6.0, maximum_temperature_k=7.0,
                                     device_factory=lambda _: device)
        adapter.connect()
        device.magnet.setpoint = device.magnet.field = 1.0
        with self.assertRaises(AttoDRY2100VerificationError):
            adapter.verify_continuous_completion(1.0, .001)
        device.magnet.field_control = True
        self.assertEqual(adapter.verify_continuous_completion(1.0, .001).field_t, 1.0)
        adapter.close()

    def test_completion_snapshot_is_reused_without_second_vendor_read(self):
        device = FakeDevice("host")
        adapter = AttoDRY2100Adapter(
            "unused", maximum_field_t=6.0, maximum_temperature_k=7.0,
            device_factory=lambda _: device,
        )
        adapter.connect()
        device.magnet.setpoint = device.magnet.field = 1.0
        device.magnet.field_control = True
        endpoint = adapter.read_snapshot()
        device.magnet.calls.clear()
        device.magnet.field = 1.002
        accepted = adapter.verify_continuous_completion_snapshot(endpoint, 1.0, .001)
        self.assertIs(accepted, endpoint)
        self.assertEqual(device.magnet.calls, [])
        with self.assertRaises(AttoDRY2100VerificationError):
            adapter.verify_continuous_completion_snapshot(
                replace(endpoint, monotonic_s=time.monotonic() - 31.0),
                1.0, .001,
            )
        adapter.close()

    def test_factory_lifecycle_and_read_only_telemetry(self):
        device = FakeDevice("host")
        adapter = AttoDRY2100Adapter("unused", host="host", channel=2, device_factory=lambda h: device)
        identity = adapter.connect(); self.assertEqual(identity.channel, 2)
        snap = adapter.read_snapshot(); self.assertEqual(snap.field_t, 1.25)
        self.assertEqual(device.magnet.calls[0], ("getH", (2,)))
        adapter.close(); adapter.close(); self.assertFalse(adapter.connected)

    def test_field_only_read_calls_only_get_h(self):
        device = FakeDevice("host")
        adapter = AttoDRY2100Adapter("unused", channel=2, device_factory=lambda _: device)
        adapter.connect()
        device.magnet.calls.clear()

        self.assertEqual(adapter.read_field(), 1.25)

        self.assertEqual(device.magnet.calls, [("getH", (2,))])

    def test_invalid_field_and_disconnected_errors(self):
        device = FakeDevice("host"); device.magnet.field = float("nan")
        with self.assertRaises(AttoDRY2100TelemetryError):
            AttoDRY2100Adapter("unused", device_factory=lambda _: device).connect()
        with self.assertRaises(AttoDRY2100ConnectionError):
            AttoDRY2100Adapter("unused", device_factory=lambda _: FakeDevice("x")).read_snapshot()

    def test_loader_failure_is_useful_and_does_not_change_sys_path(self):
        before = list(sys.path)
        with self.assertRaises(AttoDRY2100LoadError) as ctx:
            AttoDRY2100Adapter("missing-sdk").connect()
        self.assertIn("SDK directory", str(ctx.exception)); self.assertEqual(before, sys.path)

    def test_loader_relative_import_and_collision_paths(self):
        before = list(sys.path)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "one" / "atto_device"; root.mkdir(parents=True)
            (root / "helper.py").write_text("VALUE = 4\n")
            (root / "__init__.py").write_text("from .helper import VALUE\nclass Device:\n def __init__(self, host): self.magnet=type('M',(),{'getH':lambda s,c: VALUE})()\n def connect(self): pass\n def getNumberOfMagnetChannels(self): return 1\n def close(self): pass\n")
            adapter = AttoDRY2100Adapter(root.parent, device_factory=None); self.assertEqual(adapter.connect().channel, 0)
            other = Path(td) / "two" / "atto_device"; other.mkdir(parents=True)
            (other / "__init__.py").write_text("class Device:\n def __init__(self, host): self.magnet=type('M',(),{'getH':lambda s,c: 2})()\n def connect(self): pass\n def getNumberOfMagnetChannels(self): return 1\n def close(self): pass\n")
            second = AttoDRY2100Adapter(other.parent); second.connect()
            self.assertNotEqual(adapter.read_snapshot().field_t, second.read_snapshot().field_t)
            adapter.close(); second.close()
        self.assertEqual(before, sys.path)

    def test_loader_missing_init_device_noncallable_and_import_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(AttoDRY2100LoadError): AttoDRY2100Adapter(root).connect()
            package = root / "atto_device"; package.mkdir(); (package / "__init__.py").write_text("X=1")
            with self.assertRaises(AttoDRY2100LoadError): AttoDRY2100Adapter(root).connect()
            (package / "__init__.py").write_text("Device=3")
            with self.assertRaises(AttoDRY2100LoadError): AttoDRY2100Adapter(root).connect()
            (package / "helper.py").write_text("raise RuntimeError('boom')")
            (package / "__init__.py").write_text("from . import helper\nclass Device: pass")
            with self.assertRaises(AttoDRY2100LoadError): AttoDRY2100Adapter(root).connect()
            self.assertFalse(any(k.startswith("_attodry2100_sdk_") for k in sys.modules if "boom" in repr(sys.modules[k])))

    def test_validation_and_channel_cleanup(self):
        for kwargs in ({"host": ""}, {"channel": True}, {"channel": 1.5}, {"channel": -1}, {"timeout_s": 0}, {"timeout_s": float("nan")}):
            with self.assertRaises(ValueError): AttoDRY2100Adapter("unused", device_factory=lambda _: FakeDevice("x"), **kwargs)
        class Bad(FakeDevice):
            def getNumberOfMagnetChannels(self): return 1
        bad = Bad("x")
        with self.assertRaises(AttoDRY2100ConnectionError): AttoDRY2100Adapter("unused", channel=1, device_factory=lambda _: bad).connect()
        self.assertEqual(bad.closed, 1)

    def test_connect_failure_cleanup_and_idempotent_lifecycle(self):
        class Broken(FakeDevice):
            def connect(self): raise OSError("link")
        broken = Broken("x")
        adapter = AttoDRY2100Adapter("unused", device_factory=lambda _: broken)
        with self.assertRaises(AttoDRY2100ConnectionError): adapter.connect()
        self.assertFalse(adapter.connected); self.assertEqual(broken.closed, 1)
        device = FakeDevice("x"); adapter = AttoDRY2100Adapter("unused", device_factory=lambda _: device)
        first = adapter.connect(); self.assertIs(first, adapter.connect()); adapter.close(); adapter.close(); self.assertIsNone(adapter.identity)

    def test_close_failure_retains_device_for_owner_retry(self):
        class RetryClose(FakeDevice):
            def __init__(self, host): super().__init__(host); self.fail = True
            def close(self):
                self.closed += 1
                if self.fail: raise OSError("close failed")
        device = RetryClose("x")
        adapter = AttoDRY2100Adapter("unused", device_factory=lambda _: device)
        identity = adapter.connect()
        with self.assertRaises(AttoDRY2100TelemetryError): adapter.close()
        self.assertTrue(adapter.connected); self.assertIs(adapter.identity, identity)
        device.fail = False; adapter.close(); self.assertFalse(adapter.connected)

    def test_identity_preconnect_and_optional_metadata(self):
        device = FakeDevice("x")
        adapter = AttoDRY2100Adapter("unused", device_factory=lambda _: device)
        self.assertIsNone(adapter.identity); identity = adapter.connect(); self.assertEqual(identity.host, "192.168.1.1")
        self.assertEqual(identity.details["device_type"], "2100")
        adapter.close(); self.assertIsNone(adapter.identity)

    def test_telemetry_exact_calls_optional_none_and_quench(self):
        device = FakeDevice("x")
        device.magnet.getTemperature = lambda: None
        device.magnet.getIsInQuenchState = lambda: True
        adapter = AttoDRY2100Adapter("unused", channel=2, device_factory=lambda _: device); adapter.connect(); snap = adapter.read_snapshot()
        self.assertIsNone(snap.temperature_k); self.assertTrue(snap.status.quench)
        self.assertIn(("getHSetPoint", (2,)), device.magnet.calls); self.assertIn(("getLeadsHot", ()), device.magnet.calls)

    def test_optional_and_mandatory_transport_errors_are_normalized(self):
        device = FakeDevice("x"); device.magnet.getTemperature = lambda: (_ for _ in ()).throw(OSError("temp"))
        adapter = AttoDRY2100Adapter("unused", device_factory=lambda _: device); adapter.connect()
        with self.assertRaises(AttoDRY2100TelemetryError) as ctx: adapter.read_snapshot()
        self.assertIn("getTemperature", str(ctx.exception)); device.magnet.getH = lambda c: (_ for _ in ()).throw(TimeoutError("wait"))
        with self.assertRaises(AttoDRY2100TelemetryError): adapter.read_snapshot()

    def test_numeric_nan_inf_rejected_and_factory_bypasses_loader(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            device = FakeDevice("x"); device.magnet.field = value
            adapter = AttoDRY2100Adapter("does-not-exist", device_factory=lambda _: device)
            with self.assertRaises(AttoDRY2100TelemetryError): adapter.connect()
        device = FakeDevice("x"); adapter = AttoDRY2100Adapter("does-not-exist", device_factory=lambda _: device)
        self.assertTrue(adapter.connect()); adapter.close()

    def test_capabilities_are_read_only_and_timestamp_monotonic(self):
        adapter = AttoDRY2100Adapter("unused", device_factory=lambda _: FakeDevice("x")); adapter.connect()
        first = adapter.read_snapshot(); second = adapter.read_snapshot(); self.assertLessEqual(first.monotonic_s, second.monotonic_s)
        self.assertFalse(hasattr(adapter, "stop")); self.assertFalse(adapter.identity.details.get("mutating", False))
        self.assertTrue(first.capabilities.set_h_setpoint)
        self.assertTrue(first.capabilities.start_field_control)
        self.assertTrue(first.capabilities.stop_field_control)
        self.assertTrue(first.capabilities.continuous_ramp)
        self.assertFalse(first.capabilities.ramp_rate_edit)

    def test_invalid_constructor_values_do_not_call_factory(self):
        called = []
        for kwargs in ({"host": None}, {"host": 12}, {"channel": 1.0}, {"channel": True}):
            with self.assertRaises(ValueError): AttoDRY2100Adapter("unused", device_factory=lambda _: called.append(1), **kwargs)
        self.assertEqual(called, [])

    def _motion_adapter(self, device=None, **kwargs):
        device = device or FakeDevice("x")
        options = dict(maximum_field_t=9.0, minimum_temperature_k=1.0, maximum_temperature_k=6.0)
        options.update(kwargs)
        adapter = AttoDRY2100Adapter("unused", device_factory=lambda _: device, **options)
        adapter.connect()
        device.magnet.calls.clear()
        return adapter, device

    def test_motion_requires_field_and_max_temperature_limits_without_mutation(self):
        for missing in ("maximum_field_t", "maximum_temperature_k"):
            options = {"maximum_field_t": 9.0, "minimum_temperature_k": None, "maximum_temperature_k": 6.0}
            options[missing] = None
            adapter, device = self._motion_adapter(**options)
            with self.assertRaises(AttoDRY2100SafetyError): adapter.set_h_setpoint(1.0)
            self.assertFalse(any(name == "setHSetPoint" for name, _ in device.magnet.calls))

    def test_motion_allows_unset_minimum_temperature(self):
        adapter, device = self._motion_adapter(
            maximum_field_t=6.0, minimum_temperature_k=None, maximum_temperature_k=7.0
        )
        self.assertEqual(adapter.set_h_setpoint(1.0), 1.0)
        self.assertIn(("setHSetPoint", (0, 1.0)), device.magnet.calls)

    def test_motion_requires_exact_driven_mode_before_set_and_start(self):
        cases = ((True, False, True), (False, True, False), (None, False, False),
                 (True, True, False), (False, False, False), (1, 0, False))
        for driven_mode, persistent_mode, allowed in cases:
            with self.subTest(driven_mode=driven_mode, persistent_mode=persistent_mode):
                adapter, device = self._motion_adapter(
                    maximum_field_t=6.0, minimum_temperature_k=None, maximum_temperature_k=7.0
                )
                device.magnet.driven_mode = driven_mode
                device.magnet.persistent_mode = persistent_mode
                if allowed:
                    self.assertEqual(adapter.set_h_setpoint(1.0), 1.0)
                    adapter.start_field_control(1.0)
                    self.assertIn(("setHSetPoint", (0, 1.0)), device.magnet.calls)
                    self.assertIn(("startFieldControl", (0,)), device.magnet.calls)
                else:
                    with self.assertRaises(AttoDRY2100SafetyError): adapter.set_h_setpoint(1.0)
                    with self.assertRaises(AttoDRY2100SafetyError): adapter.start_field_control(1.0)
                    self.assertFalse(any(name in {"setHSetPoint", "startFieldControl"}
                                         for name, _ in device.magnet.calls))

    def test_setpoint_allows_real_idle_controlled_driven_state(self):
        adapter, device = self._motion_adapter(
            maximum_field_t=6.0, minimum_temperature_k=None, maximum_temperature_k=7.0
        )
        device.magnet.field_state = "IDLE"
        device.magnet.field_control = True
        self.assertEqual(adapter.set_h_setpoint(0.01), 0.01)
        self.assertIn(("setHSetPoint", (0, 0.01)), device.magnet.calls)

    def test_invalid_temperature_readback_fails_closed_without_mutation(self):
        for value in ("not-a-number", float("nan"), float("inf")):
            device = FakeDevice("x")
            device.magnet.temperature = value
            adapter, device = self._motion_adapter(
                device, maximum_field_t=6.0, minimum_temperature_k=None, maximum_temperature_k=7.0
            )
            with self.assertRaises(AttoDRY2100SafetyError): adapter.set_h_setpoint(1.0)
            self.assertFalse(any(name == "setHSetPoint" for name, _ in device.magnet.calls))

    def test_authorized_limits_reject_out_of_range_target_and_temperature(self):
        adapter, device = self._motion_adapter(
            maximum_field_t=6.0, minimum_temperature_k=None, maximum_temperature_k=7.0
        )
        for target in (-6.0, 6.0):
            self.assertEqual(adapter.set_h_setpoint(target), target)
        device.magnet.temperature = 7.0
        self.assertEqual(adapter.set_h_setpoint(0.01), 0.01)
        device.magnet.calls.clear()
        for target in (-6.0001, 6.0001):
            with self.assertRaises(AttoDRY2100SafetyError): adapter.set_h_setpoint(target)
        device.magnet.temperature = 7.0001
        with self.assertRaises(AttoDRY2100SafetyError): adapter.set_h_setpoint(0.01)
        self.assertFalse(any(name == "setHSetPoint" for name, _ in device.magnet.calls))

    def test_required_safety_telemetry_rejects_without_mutation(self):
        for field, value in (("quench", None), ("quench", True), ("temperature", None), ("temperature", 8.0)):
            device = FakeDevice("x"); setattr(device.magnet, field, value)
            adapter, device = self._motion_adapter(device)
            with self.assertRaises(AttoDRY2100SafetyError): adapter.set_h_setpoint(1.0)
            self.assertFalse(any(name == "setHSetPoint" for name, _ in device.magnet.calls))

    def test_set_start_stop_are_verified_and_use_exact_channel(self):
        adapter, device = self._motion_adapter()
        self.assertEqual(adapter.set_h_setpoint(2.0), 2.0)
        adapter.start_field_control(2.0)
        adapter.stop_field_control()
        self.assertIn(("setHSetPoint", (0, 2.0)), device.magnet.calls)
        self.assertIn(("startFieldControl", (0,)), device.magnet.calls)
        self.assertIn(("stopFieldControl", (0,)), device.magnet.calls)

    def test_stop_accepts_vendor_ack_with_idle_field_control_still_true(self):
        adapter, device = self._motion_adapter()
        device.magnet.field_state = "IDLE"
        device.magnet.field_control = True
        self.assertIsNone(adapter.stop_field_control())
        self.assertIn(("stopFieldControl", (0,)), device.magnet.calls)

    def test_stop_vendor_error_is_reported(self):
        adapter, device = self._motion_adapter()
        def fail_stop(channel):
            device.magnet.calls.append(("stopFieldControl", (channel,)))
            raise RuntimeError("vendor stop failed")
        device.magnet.stopFieldControl = fail_stop
        with self.assertRaises(AttoDRY2100CommunicationError) as ctx:
            adapter.stop_field_control()
        self.assertIn("stopFieldControl", str(ctx.exception))

    def test_start_requires_matching_target_and_verified_control(self):
        adapter, device = self._motion_adapter()
        with self.assertRaises(AttoDRY2100VerificationError): adapter.start_field_control(2.0)
        self.assertFalse(any(name == "startFieldControl" for name, _ in device.magnet.calls))
        device.magnet.setpoint = 2.0
        device.magnet.startFieldControl = lambda channel: device.magnet.calls.append(("startFieldControl", (channel,)))
        with self.assertRaises(AttoDRY2100VerificationError): adapter.start_field_control(2.0)

    def test_stop_during_real_adapter_preflight_prevents_mutation(self):
        adapter, device = self._motion_adapter()
        entered = threading.Event(); release = threading.Event(); stop = threading.Event()
        original = device.magnet.getTemperature
        def gated_temperature():
            entered.set(); release.wait(2.0); return original()
        device.magnet.getTemperature = gated_temperature
        errors = []
        thread = threading.Thread(target=lambda: self._capture(lambda: adapter.set_h_setpoint(1.0, stop_event=stop), errors))
        thread.start(); self.assertTrue(entered.wait(1.0)); stop.set(); release.set(); thread.join(2.0)
        self.assertTrue(errors); self.assertIsInstance(errors[0], AttoDRY2100StoppedError)
        self.assertFalse(any(name == "setHSetPoint" for name, _ in device.magnet.calls))

    @staticmethod
    def _capture(callback, errors):
        try: callback()
        except BaseException as exc: errors.append(exc)


if __name__ == "__main__": unittest.main()
