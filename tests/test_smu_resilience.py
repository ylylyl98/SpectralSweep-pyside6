from __future__ import annotations

import unittest
import importlib.util
import importlib.machinery
import io
import json
import os
import sys
import tempfile
import threading
import types
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

if "pylablib" not in sys.modules and importlib.util.find_spec("pylablib") is None:
    pylablib_stub = types.ModuleType("pylablib")
    devices_stub = types.ModuleType("pylablib.devices")
    pylablib_stub.__spec__ = importlib.machinery.ModuleSpec("pylablib", loader=None)
    devices_stub.__spec__ = importlib.machinery.ModuleSpec("pylablib.devices", loader=None)
    devices_stub.Thorlabs = types.SimpleNamespace(ElliptecMotor=object)
    pylablib_stub.devices = devices_stub
    sys.modules.setdefault("pylablib", pylablib_stub)
    sys.modules.setdefault("pylablib.devices", devices_stub)
if "nidaqmx" not in sys.modules:
    try:
        import nidaqmx  # noqa: F401
    except ModuleNotFoundError:
        sys.modules["nidaqmx"] = types.SimpleNamespace(Task=object)
if "pyvisa" not in sys.modules:
    try:
        import pyvisa  # noqa: F401
    except ModuleNotFoundError:
        sys.modules["pyvisa"] = types.SimpleNamespace(ResourceManager=object)

from app.devices.iv_adapter import IVDevice, SMUCommunicationError
from controllers.smu_controller import _SMUWorker
from iv_automation import IVSetup, KeithControl, PyvisaInstrument, should_send_rsyn
from ui.presets_panel import (
    _RunWorker,
    _format_current_readback,
    _required_smu_roles,
    _smu_readiness_issues,
)
from utils.config import cfg
from utils.hardware_incidents import (
    HardwareIncidentRecorder,
    build_hardware_incident,
)


_DEFAULT_FAKE_RESPONSES = {
    "*IDN?": "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1058315,C27",
    ":SYST:ERR?": '0,"No error"',
    ":SENS:CURR:PROT?": "6e-07",
    ":SENS:CURR:RANG:AUTO?": "0",
    ":SENS:CURR:RANG?": "1e-06",
    ":SOUR:VOLT:RANG?": "20",
    ":TRIG:SOUR?": "IMM",
    ":SOUR:CLE:AUTO?": "0",
    "*ESR?": "0",
    ":OUTP?": "0",
    "READ?": "0.0,0.0",
}

# Realistic *IDN? responses captured from the physical instruments.
REAL_2400_IDN = (
    "KEITHLEY INSTRUMENTS INC.,MODEL 2400,0824046,"
    "C21   Nov  7 2000 12:51:38/A02  /H/H"
)
REAL_2401_IDN = (
    "KEITHLEY INSTRUMENTS INC.,MODEL 2401,4612957,"
    "B02 Jan 20 2021 10:19:49/B01  /W/N"
)


class _FakeResource:
    read_termination = None
    write_termination = None
    timeout = 5000

    def __init__(self, fail_on_query=None, fail_on_write=None, responses=None):
        self.fail_on_query = set(fail_on_query or [])
        self.fail_on_write = set(fail_on_write or [])
        self.responses = dict(_DEFAULT_FAKE_RESPONSES)
        if responses:
            self.responses.update(responses)
        self.writes = []
        self.queries = []
        self.clears = 0
        self.closed = False

    def write(self, command, *args, **kwargs):
        if command in self.fail_on_write:
            raise TimeoutError("VI_ERROR_TMO: simulated write timeout")
        self.writes.append(command)
        return (len(command), 0)

    def query(self, command, *args, **kwargs):
        self.queries.append(command)
        if command in self.fail_on_query:
            raise TimeoutError("VI_ERROR_TMO: simulated query timeout")
        if command not in self.responses:
            raise TimeoutError(f"VI_ERROR_TMO: no fake response for {command}")
        return self.responses[command]

    def read(self, *args, **kwargs):
        return "0.0,0.0"

    def clear(self):
        self.clears += 1

    def close(self):
        self.closed = True


class _FakeResourceManager:
    def __init__(self, resources=None, fail_open=None):
        self.open_calls = []
        self.resources = dict(resources or {})
        self.fail_open = set(fail_open or [])

    def open_resource(self, address, **kwargs):
        if address in self.fail_open:
            raise TimeoutError(
                f"VI_ERROR_TMO: simulated open failure for {address}"
            )
        self.open_calls.append((address, kwargs))
        if address in self.resources:
            return self.resources[address]
        return _FakeResource()

    def list_resources(self):
        return sorted(self.resources.keys())

    def close(self):
        return None


class _RecordingRampResource(_FakeResource):
    """Keep physical voltage separate from the adapter's cached setpoint."""

    def __init__(self, role, voltage, events):
        super().__init__()
        self.role = role
        self.voltage = voltage
        self.events = events

    def query(self, command, *args, **kwargs):
        self.events.append(("read", self.role, command))
        if command == "READ?":
            self.responses[command] = f"{self.voltage},0"
        return super().query(command, *args, **kwargs)

    def write(self, command, *args, **kwargs):
        if command.startswith(":SOUR:VOLT:LEV "):
            voltage = float(command.rsplit(" ", 1)[-1])
            self.events.append(("write", self.role, voltage))
            result = super().write(command, *args, **kwargs)
            self.voltage = voltage
            return result
        self.events.append(("other", self.role, command))
        return super().write(command, *args, **kwargs)


def _recording_ramp_device(starts):
    events = []
    addresses = {role: f"GPIB1::{index}::INSTR" for index, role in enumerate(starts, start=2)}
    resources = {role: _RecordingRampResource(role, start, events) for role, start in starts.items()}
    manager = _FakeResourceManager({address: resources[role] for role, address in addresses.items()})
    instruments = []
    for role, address in addresses.items():
        instrument = KeithControl(address, role, role, manager, configure_on_connect=False, recover_on_open=False)
        instrument.mode = "volt_step"
        instrument.x_values[0] = 9.0  # Deliberately stale cached voltage.
        instruments.append(instrument)
    setup = IVSetup(instruments)
    events.clear()
    return IVDevice(setup, role_map=addresses), resources, events


class _DeadInstrument:
    address = "GPIB1::2::INSTR"

    def read_y(self):
        raise TimeoutError("simulated VISA timeout")

    def write_x(self):
        raise TimeoutError("simulated VISA timeout")


class _PowerCycledInstrument:
    address = "GPIB1::2::INSTR"
    timeout = 5000

    def __init__(self):
        self.write_calls = 0
        self.queries = []

    def write_x(self):
        self.write_calls += 1
        raise TimeoutError("VI_ERROR_TMO")

    def query(self, command):
        self.queries.append(command)
        responses = {
            "*IDN?": "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1058315,C27",
            "*ESR?": "128",
            ":OUTP?": "0",
            ":SYST:ERR?": '0,"No error"',
        }
        return responses[command]


class _FailsDuringPostAcquisitionRead(_PowerCycledInstrument):
    def __init__(self, stop_event):
        super().__init__()
        self.stop_event = stop_event
        self.read_calls = 0

    def write_x(self):
        self.write_calls += 1
        return None

    def read_y(self):
        self.read_calls += 1
        if self.read_calls >= 3:
            # Models the operator clicking Stop while the VISA read is stuck.
            self.stop_event.set()
            raise TimeoutError("VI_ERROR_TMO")
        return None


class _FakeLFAdapter:
    def __init__(self):
        self.acquire_calls = 0

    def acquire(self):
        self.acquire_calls += 1
        return np.array([700.0, 701.0]), np.array([10.0, 11.0])


class _FakeLFController:
    def __init__(self):
        self.is_connected = True
        self.adapter = _FakeLFAdapter()

    def configure_for_acquisition(self, *, center_nm, exposure_ms, frames):
        self.apply_settings(center_nm, exposure_ms, frames)

    def apply_settings(self, *_args):
        return None


class _RecordingStopEvent:
    def __init__(self, *, stop_during_wait=False):
        self._is_set = False
        self.stop_during_wait = bool(stop_during_wait)
        self.wait_calls = []

    def is_set(self):
        return self._is_set

    def wait(self, timeout):
        self.wait_calls.append(float(timeout))
        if self.stop_during_wait:
            self._is_set = True
        return self._is_set

    def set(self):
        self._is_set = True


class _FakeYChannels:
    def __init__(self, instrument):
        self.instrument = instrument

    def get_instrument(self, _key):
        return self.instrument

    def receive_y(self, _key):
        return None


class _FakeXChannels:
    def __init__(self, instrument):
        self.instrument = instrument

    def send_x(self, _key, _value):
        return None

    def get_instrument(self, _key):
        return self.instrument


class _HealthyRunDevice:
    health_states = {"Vbg": "ready", "Vtg": "ready", "Vbias": "ready"}
    requires_reconnect = False

    def __init__(self, roles=("Vbg", "Vtg")):
        self.roles = set(roles)
        self.current_reads = 0

    def role_is_available(self, role):
        return role in self.roles

    def set_operation_context(self, **_context):
        return None

    def clear_operation_context(self):
        return None

    def read_current_gates(self, *, strict=False):
        return 0.01, 0.02

    def read_current_bias(self, *, strict=False):
        return 0.03

    def set_gates(self, **_kwargs):
        return None

    def set_bias(self, **_kwargs):
        return None

    def read_currents(self, *, strict=False):
        self.current_reads += 1
        return 1.25e-9, -2.5e-9, 3.75e-9 if "Vbias" in self.roles else None

    def ramp_all_to_zero_report(self, **_kwargs):
        return {
            role: {"status": "reached_zero"}
            for role in self.roles
        }


class SMUResilienceTests(unittest.TestCase):
    @staticmethod
    def _batch_row(**overrides):
        row = {
            "Run": True,
            "When": "",
            "MeasurePower": False,
            "condition_label": "dual-gate",
            "repeat": 1,
            "frames": 1,
            "Vbg_start": 0.0,
            "Vbg_stop": 0.0,
            "Vtg_start": 0.0,
            "Vtg_stop": 0.0,
            "Vbias_start": "",
            "Vbias_stop": "",
        }
        row.update(overrides)
        return row

    @staticmethod
    def _run_meta():
        return {
            "device_id": "sample",
            "point": "p1",
            "tag": "",
            "temperature": "4K",
            "measurement_mode": "PL",
            "laser_nm": "633",
            "power_uw": "1",
            "power_coefficient": 1.0,
            "subfolder": "Initial Data",
        }

    def test_required_smu_roles_include_vbias_only_when_an_applicable_row_uses_it(self):
        sequence = [{"Center Wavelength (nm)": 700.0}]
        batch = pd.DataFrame([
            self._batch_row(),
            self._batch_row(
                When="Center_Wavelength == 700",
                Vbias_start=-0.1,
                Vbias_stop=0.2,
            ),
        ])

        self.assertEqual(
            _required_smu_roles(sequence, batch),
            ("Vbg", "Vtg", "Vbias"),
        )

        batch.loc[1, "When"] = "Center_Wavelength == 800"
        self.assertEqual(
            _required_smu_roles(sequence, batch),
            ("Vbg", "Vtg"),
        )

    def test_smu_readiness_reports_disconnected_and_partial_keithley_roles(self):
        self.assertEqual(
            _smu_readiness_issues(None, ("Vbg", "Vtg")),
            ["Required Keithley channels are not connected: Vbg, Vtg."],
        )

        device = _HealthyRunDevice(("Vbg",))
        smu = SimpleNamespace(is_connected=True, device=device)
        self.assertEqual(
            _smu_readiness_issues(smu, ("Vbg", "Vtg")),
            ["Required Keithley channels are missing: Vtg."],
        )

        gates_only = _HealthyRunDevice(("Vbg", "Vtg"))
        smu = SimpleNamespace(is_connected=True, device=gates_only)
        self.assertEqual(
            _smu_readiness_issues(smu, ("Vbg", "Vtg")),
            [],
        )
        self.assertEqual(
            _smu_readiness_issues(smu, ("Vbg", "Vtg", "Vbias")),
            ["Required Keithley channels are missing: Vbias."],
        )

        smu = SimpleNamespace(
            is_connected=True,
            device=gates_only,
            limits_are_applied_for_roles=lambda _roles: False,
        )
        self.assertEqual(
            _smu_readiness_issues(smu, ("Vbg", "Vtg")),
            ["Apply and verify the compliance settings for: Vbg, Vtg."],
        )
        smu.limits_are_applied_for_roles = lambda _roles: True
        self.assertEqual(_smu_readiness_issues(smu, ("Vbg", "Vtg")), [])

    def test_worker_never_acquires_without_required_keithleys(self):
        sequence = [{
            "Center Wavelength (nm)": 700.0,
            "Exposure Time (ms)": 1.0,
            "Accumulations (EPF)": 1,
        }]
        batch = pd.DataFrame([self._batch_row()])
        lf6 = _FakeLFController()

        with tempfile.TemporaryDirectory() as tmp:
            worker = _RunWorker(
                sequence,
                batch,
                lf6_ctrl=lf6,
                smu_ctrl=None,
                out_dir=Path(tmp),
                run_meta=self._run_meta(),
                filename_parts=["device_id"],
                stop_event=threading.Event(),
            )
            finished = []
            worker.finished.connect(lambda success, message: finished.append((success, message)))
            worker.run()

        self.assertEqual(lf6.adapter.acquire_calls, 0)
        self.assertFalse(finished[0][0])
        self.assertIn("Keithley channels are not connected", finished[0][1])

    def test_worker_logs_saved_current_readback_without_an_extra_current_read(self):
        sequence = [{
            "Center Wavelength (nm)": 700.0,
            "Exposure Time (ms)": 1.0,
            "Accumulations (EPF)": 1,
        }]
        batch = pd.DataFrame([self._batch_row()])
        device = _HealthyRunDevice()
        smu = SimpleNamespace(is_connected=True, device=device)
        logs = []

        with tempfile.TemporaryDirectory() as tmp:
            worker = _RunWorker(
                sequence,
                batch,
                lf6_ctrl=_FakeLFController(),
                smu_ctrl=smu,
                out_dir=Path(tmp),
                run_meta=self._run_meta(),
                filename_parts=["device_id"],
                stop_event=threading.Event(),
            )
            worker.log.connect(logs.append)
            with (
                patch.object(cfg.ramp, "delay_s", 0.0),
                patch.object(cfg.ramp, "settle_s", 0.0),
            ):
                worker.run()

        self.assertEqual(device.current_reads, 1)
        self.assertTrue(
            any(
                "Keithley current readback: Ibg=1.2500e-09 A, "
                "Itg=-2.5000e-09 A" in line
                for line in logs
            ),
            logs,
        )
        self.assertEqual(
            _format_current_readback(1e-9, 2e-9, None, include_bias=True),
            "Keithley current readback: Ibg=1.0000e-09 A, "
            "Itg=2.0000e-09 A, Ibias=unavailable",
        )

    def test_worker_uses_run_snapshot_settle_before_every_acquisition(self):
        sequence = [{
            "Center Wavelength (nm)": 700.0,
            "Exposure Time (ms)": 1.0,
            "Accumulations (EPF)": 1,
        }]
        batch = pd.DataFrame([self._batch_row(frames=2, Vbg_stop=0.1)])
        lf6 = _FakeLFController()
        stop_event = _RecordingStopEvent()
        run_meta = self._run_meta()
        run_meta["initial_voltage_settle_s"] = 600.0
        run_meta["voltage_settle_s"] = 120.0

        with tempfile.TemporaryDirectory() as tmp:
            worker = _RunWorker(
                sequence,
                batch,
                lf6_ctrl=lf6,
                smu_ctrl=SimpleNamespace(is_connected=True, device=_HealthyRunDevice()),
                out_dir=Path(tmp),
                run_meta=run_meta,
                filename_parts=["device_id"],
                stop_event=stop_event,
            )
            worker.run()

        self.assertEqual(stop_event.wait_calls, [600.0, 120.0])
        self.assertEqual(lf6.adapter.acquire_calls, 2)

    def test_stop_interrupts_post_voltage_settle_before_acquisition(self):
        sequence = [{
            "Center Wavelength (nm)": 700.0,
            "Exposure Time (ms)": 1.0,
            "Accumulations (EPF)": 1,
        }]
        lf6 = _FakeLFController()
        stop_event = _RecordingStopEvent(stop_during_wait=True)
        run_meta = self._run_meta()
        run_meta["initial_voltage_settle_s"] = 3600.0
        run_meta["voltage_settle_s"] = 1.0
        finished = []

        with tempfile.TemporaryDirectory() as tmp:
            worker = _RunWorker(
                sequence,
                pd.DataFrame([self._batch_row()]),
                lf6_ctrl=lf6,
                smu_ctrl=SimpleNamespace(is_connected=True, device=_HealthyRunDevice()),
                out_dir=Path(tmp),
                run_meta=run_meta,
                filename_parts=["device_id"],
                stop_event=stop_event,
            )
            worker.finished.connect(
                lambda success, message: finished.append((success, message))
            )
            worker.run()

        self.assertEqual(stop_event.wait_calls, [3600.0])
        self.assertEqual(lf6.adapter.acquire_calls, 0)
        self.assertEqual(finished, [(False, "Run stopped by user.")])

    def test_visa_timeout_is_finite(self):
        rm = _FakeResourceManager()
        inst = PyvisaInstrument(
            "GPIB1::2::INSTR",
            "Vbg",
            "\n",
            rm,
        )
        inst.connect()

        self.assertEqual(inst.timeout, 5000)
        self.assertEqual(
            rm.open_calls,
            [("GPIB1::2::INSTR", {"timeout": 5000})],
        )

    def test_keithley_is_configured_once_with_requested_compliance(self):
        rm = _FakeResourceManager()
        with (
            patch.object(KeithControl, "connect"),
            patch.object(KeithControl, "recover_session"),
            patch.object(KeithControl, "get_identity"),
            patch.object(KeithControl, "set_volt_step") as configure,
            patch.object(KeithControl, "read_curr", return_value=(0.0, 0.0)),
        ):
            KeithControl(
                "GPIB1::2::INSTR",
                "Vbg_SMU",
                "Vbg",
                rm,
                curr_compliance=6e-7,
                volt_compliance=20.0,
            )

        configure.assert_called_once_with(
            curr_compliance=6e-7,
            volt_compliance=20.0,
        )

    def test_keithley_connection_can_skip_configuration_without_forcing_output_off(self):
        rm = _FakeResourceManager()
        with (
            patch.object(KeithControl, "connect"),
            patch.object(KeithControl, "get_identity"),
            patch.object(KeithControl, "set_volt_step") as configure,
            patch.object(KeithControl, "read_curr") as read_curr,
            patch.object(KeithControl, "write") as write,
        ):
            instrument = KeithControl(
                "GPIB1::2::INSTR",
                "Vbg_SMU",
                "Vbg",
                rm,
                configure_on_connect=False,
                recover_on_open=False,
            )

        configure.assert_not_called()
        read_curr.assert_not_called()
        write.assert_not_called()
        self.assertEqual(instrument.mode, "unconfigured")

    def test_recovery_sequence_issued_on_open(self):
        resource = _FakeResource()
        rm = _FakeResourceManager(resources={"GPIB1::2::INSTR": resource})
        instrument = KeithControl(
            "GPIB1::2::INSTR",
            "Vbg_SMU",
            "Vbg",
            rm,
            curr_compliance=6e-7,
            volt_compliance=20.0,
            configure_on_connect=False,
            recover_on_open=True,
        )
        self.assertGreaterEqual(resource.clears, 1)
        self.assertIn("*CLS", resource.writes)
        self.assertIn(":ABOR", resource.writes)
        self.assertIn("*IDN?", resource.queries)
        self.assertEqual(instrument.model, "MODEL 2400")
        self.assertEqual(instrument.firmware, "C27")

    def test_io_log_records_timestamp_op_command_and_elapsed(self):
        resource = _FakeResource()
        rm = _FakeResourceManager(resources={"GPIB1::2::INSTR": resource})
        instrument = KeithControl(
            "GPIB1::2::INSTR",
            "Vbg_SMU",
            "Vbg",
            rm,
            configure_on_connect=False,
        )
        entries = list(instrument.io_log)
        self.assertTrue(entries)
        for key in ("timestamp", "address", "op", "command", "elapsed_ms", "status"):
            self.assertIn(key, entries[0])
        self.assertEqual(entries[0]["status"], "ok")

    def test_rsyn_is_skipped_on_2400_old_firmware(self):
        instrument = object.__new__(KeithControl)
        instrument._rsyn_supported = None
        instrument.model = "MODEL 2400"
        instrument.rsyn_policy = "auto"
        writes = []
        responses = dict(_DEFAULT_FAKE_RESPONSES)
        instrument.write = lambda command, print_command=False: writes.append(command)
        instrument.query = lambda command: responses[command]

        instrument.apply_compliance_settings(6e-7, 1e-6, 20.0)

        self.assertFalse(instrument._rsyn_supported)
        self.assertNotIn(":SENS:CURR:PROT:RSYN ON", writes)
        self.assertIn(":SENS:CURR:RANG 1e-06", writes)

    def test_real_2400_idn_parses_and_skips_rsyn(self):
        resource = _FakeResource(responses={"*IDN?": REAL_2400_IDN})
        rm = _FakeResourceManager(resources={"GPIB1::2::INSTR": resource})
        instrument = KeithControl(
            "GPIB1::2::INSTR",
            "Vtg_SMU",
            "Vtg",
            rm,
            curr_compliance=6e-7,
            volt_compliance=20.0,
            configure_on_connect=False,
        )

        self.assertEqual(instrument.identity["manufacturer"], "KEITHLEY INSTRUMENTS INC.")
        self.assertEqual(instrument.identity["model"], "MODEL 2400")
        self.assertEqual(instrument.model, "MODEL 2400")
        self.assertEqual(instrument.identity["serial"], "0824046")
        self.assertEqual(
            instrument.identity["firmware"],
            "C21   Nov  7 2000 12:51:38/A02  /H/H",
        )
        self.assertEqual(instrument.rsyn_policy, "auto")

        instrument.apply_compliance_settings(6e-7, 1e-6, 20.0)

        self.assertFalse(instrument._rsyn_supported)
        self.assertNotIn(":SENS:CURR:PROT:RSYN ON", resource.writes)

    def test_real_2400_connect_completes_full_sequence_and_keeps_verification(self):
        resource = _FakeResource(responses={"*IDN?": REAL_2400_IDN})
        rm = _FakeResourceManager(resources={"GPIB1::2::INSTR": resource})
        worker = _SMUWorker()
        with (
            patch("pyvisa.ResourceManager", return_value=rm),
            patch.object(cfg.smu, "rsyn_enabled", None),
        ):
            worker.connect_instrument(
                ["GPIB1::2::INSTR"],
                {"Vbg": None, "Vtg": "GPIB1::2::INSTR", "Vbias": None},
                "\n",
                {"GPIB1::2::INSTR": {"curr": 6e-7, "volt": 20.0}},
            )

        # RSYN must be skipped and the output must stay OFF.
        self.assertNotIn(":SENS:CURR:PROT:RSYN ON", resource.writes)
        self.assertNotIn(":OUTP ON", resource.writes)
        # The rest of the setup sequence continues unchanged.
        for command in (
            ":SENS:CURR:RANG:AUTO OFF",
            ":SENS:CURR:PROT 6e-07",
            ":SENS:CURR:RANG 1e-06",
            ":SOUR:VOLT:RANG 20",
            ":SOUR:FUNC VOLT",
            ":SENS:FUNC 'CURR'",
            ":SOUR:VOLT:MODE FIXED",
            ":TRIG:SOUR IMM",
            "TRIG:COUN 1",
            ":SOUR:CLE:AUTO OFF",
        ):
            self.assertIn(command, resource.writes)
        # All verification/readback queries remain intact.
        for query in (
            ":SYST:ERR?",
            ":SENS:CURR:PROT?",
            ":SENS:CURR:RANG:AUTO?",
            ":SENS:CURR:RANG?",
            ":SOUR:VOLT:RANG?",
            ":TRIG:SOUR?",
            ":SOUR:CLE:AUTO?",
            "*ESR?",
            "READ?",
        ):
            self.assertIn(query, resource.queries)
        # The connected Vtg role must be visible to the readiness system.
        self.assertIsNotNone(worker.device)
        self.assertTrue(worker.device.role_is_available("Vtg"))
        self.assertFalse(worker.device.role_is_available("Vbg"))
        readiness_smu = SimpleNamespace(
            is_connected=True,
            device=worker.device,
            limits_are_applied_for_roles=lambda roles: True,
        )
        self.assertEqual(_smu_readiness_issues(readiness_smu, ("Vtg",)), [])

    def test_rsyn_auto_sends_for_model_2401(self):
        instrument = object.__new__(KeithControl)
        instrument._rsyn_supported = None
        instrument.model = "MODEL 2401"
        instrument.rsyn_policy = "auto"
        writes = []
        responses = dict(_DEFAULT_FAKE_RESPONSES)
        instrument.write = lambda command, print_command=False: writes.append(command)
        instrument.query = lambda command: responses[command]

        instrument.apply_compliance_settings(6e-7, 1e-6, 20.0)

        self.assertTrue(instrument._rsyn_supported)
        self.assertIn(":SENS:CURR:PROT:RSYN ON", writes)
        self.assertIn(":SENS:CURR:RANG 1e-06", writes)

    def test_rsyn_forced_off_skips_for_model_2401(self):
        instrument = object.__new__(KeithControl)
        instrument._rsyn_supported = None
        instrument.model = "MODEL 2401"
        instrument.rsyn_policy = "forced_off"
        writes = []
        responses = dict(_DEFAULT_FAKE_RESPONSES)
        instrument.write = lambda command, print_command=False: writes.append(command)
        instrument.query = lambda command: responses[command]

        instrument.apply_compliance_settings(6e-7, 1e-6, 20.0)

        self.assertFalse(instrument._rsyn_supported)
        self.assertNotIn(":SENS:CURR:PROT:RSYN ON", writes)

    def test_should_send_rsyn_policy_matrix_and_model_parsing(self):
        cases = [
            # (model string, config_value, expected)
            ("MODEL 2400", None, False),
            ("2400", None, False),
            ("KEITHLEY INSTRUMENTS INC.,MODEL 2400,0824046,C21", None, False),
            (REAL_2400_IDN, None, False),
            ("MODEL 2401", None, True),
            ("KEITHLEY INSTRUMENTS INC.,MODEL 2401,4612957,B02", None, True),
            (REAL_2401_IDN, None, True),
            ("2400A", None, True),
            ("MODEL 2400A", None, True),
            ("MODEL 2410", None, True),
            ("", None, True),
            ("MODEL 2400", True, True),
            ("MODEL 2400", False, False),
            ("MODEL 2401", True, True),
            ("MODEL 2401", False, False),
            (REAL_2400_IDN, True, True),
            (REAL_2401_IDN, False, False),
        ]
        for model, config_value, expected in cases:
            with self.subTest(model=model, config_value=config_value):
                self.assertEqual(should_send_rsyn(model, config_value), expected)

    def test_rsyn_forced_on_sends_for_model_2400(self):
        resource = _FakeResource()
        rm = _FakeResourceManager(resources={"GPIB1::2::INSTR": resource})
        instrument = KeithControl(
            "GPIB1::2::INSTR",
            "Vbg_SMU",
            "Vbg",
            rm,
            curr_compliance=6e-7,
            volt_compliance=20.0,
            configure_on_connect=False,
            rsyn_enabled=True,
        )
        instrument.apply_compliance_settings(6e-7, 1e-6, 20.0)

        self.assertTrue(instrument._rsyn_supported)
        self.assertEqual(instrument.rsyn_policy, "forced_on")
        self.assertIn(":SENS:CURR:PROT:RSYN ON", resource.writes)

    def test_rsyn_forced_off_skips_for_model_2401(self):
        resource = _FakeResource(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS INC.,MODEL 2401,4612957,B02"
            }
        )
        rm = _FakeResourceManager(resources={"GPIB1::2::INSTR": resource})
        instrument = KeithControl(
            "GPIB1::2::INSTR",
            "Vbg_SMU",
            "Vbg",
            rm,
            curr_compliance=6e-7,
            volt_compliance=20.0,
            configure_on_connect=False,
            rsyn_enabled=False,
        )
        instrument.apply_compliance_settings(6e-7, 1e-6, 20.0)

        self.assertFalse(instrument._rsyn_supported)
        self.assertEqual(instrument.rsyn_policy, "forced_off")
        self.assertNotIn(":SENS:CURR:PROT:RSYN ON", resource.writes)

    def test_mixed_2400_and_2401_connect_with_independent_rsyn_policy(self):
        res_2400 = _FakeResource(
            responses={"*IDN?": REAL_2400_IDN}
        )
        res_2401 = _FakeResource(
            responses={"*IDN?": REAL_2401_IDN}
        )
        rm = _FakeResourceManager(
            resources={
                "GPIB1::2::INSTR": res_2400,
                "GPIB1::3::INSTR": res_2401,
            }
        )
        worker = _SMUWorker()
        with (
            patch("pyvisa.ResourceManager", return_value=rm),
            patch.object(cfg.smu, "rsyn_enabled", None),
        ):
            worker.connect_instrument(
                ["GPIB1::2::INSTR", "GPIB1::3::INSTR"],
                {
                    "Vbg": "GPIB1::2::INSTR",
                    "Vtg": "GPIB1::3::INSTR",
                    "Vbias": None,
                },
                "\n",
                {},
            )
        # Auto policy skips RSYN only for the MODEL 2400; the MODEL 2401
        # keeps sending it.  Each instrument resolves its own policy without
        # shared state.
        self.assertNotIn(":SENS:CURR:PROT:RSYN ON", res_2400.writes)
        self.assertIn(":SENS:CURR:PROT:RSYN ON", res_2401.writes)
        # Connect must never energize outputs, for either instrument.
        self.assertNotIn(":OUTP ON", res_2400.writes)
        self.assertNotIn(":OUTP ON", res_2401.writes)

    def test_failed_connect_stops_issuing_commands_and_allows_reconnect(self):
        failing = _FakeResource(fail_on_query={":SENS:CURR:PROT?"})
        rm = _FakeResourceManager(resources={"GPIB1::2::INSTR": failing})
        worker = _SMUWorker()
        errors = []
        worker.error.connect(errors.append)
        with patch("pyvisa.ResourceManager", return_value=rm):
            worker.connect_instrument(
                ["GPIB1::2::INSTR"],
                {"Vbg": "GPIB1::2::INSTR", "Vtg": None, "Vbias": None},
                "\n",
                {"GPIB1::2::INSTR": {"curr": 6e-7, "volt": 20.0}},
            )
        # No commands after the failing query are issued.
        self.assertNotIn(":SOUR:FUNC VOLT", failing.writes)
        self.assertNotIn("TRIG:COUN 1", failing.writes)
        self.assertNotIn("READ?", failing.queries)
        # Resource cleaned up and software connection state reset.
        self.assertTrue(failing.closed)
        self.assertIsNone(worker.device)
        self.assertFalse(worker._connecting)
        self.assertTrue(any("PRIMARY FAILURE" in error for error in errors))

        # A fresh reconnect attempt succeeds.
        healthy = _FakeResource()
        rm2 = _FakeResourceManager(resources={"GPIB1::2::INSTR": healthy})
        connected = []
        worker.connected.connect(connected.append)
        with patch("pyvisa.ResourceManager", return_value=rm2):
            worker.connect_instrument(
                ["GPIB1::2::INSTR"],
                {"Vbg": "GPIB1::2::INSTR", "Vtg": None, "Vbias": None},
                "\n",
                {"GPIB1::2::INSTR": {"curr": 6e-7, "volt": 20.0}},
            )
        self.assertEqual(connected, [["GPIB1::2::INSTR"]])
        self.assertIsNotNone(worker.device)
        self.assertTrue(worker.device.role_is_available("Vbg"))

    def test_connect_instrumentation_reports_each_configured_role(self):
        res_vtg = _FakeResource(responses={"*IDN?": REAL_2400_IDN})
        res_vbg = _FakeResource(responses={"*IDN?": REAL_2401_IDN})
        rm = _FakeResourceManager(
            resources={
                "GPIB1::2::INSTR": res_vtg,
                "GPIB1::3::INSTR": res_vbg,
            }
        )
        worker = _SMUWorker()
        output = io.StringIO()
        with (
            patch("pyvisa.ResourceManager", return_value=rm),
            redirect_stdout(output),
        ):
            worker.connect_instrument(
                ["GPIB1::2::INSTR", "GPIB1::3::INSTR"],
                {"Vbg": "GPIB1::3::INSTR", "Vtg": "GPIB1::2::INSTR", "Vbias": None},
                "\n",
                {},
            )
        text = output.getvalue()
        self.assertIn("SMU connection start", text)
        self.assertIn("configured roles:", text)
        self.assertIn("Vbg -> GPIB1::3::INSTR", text)
        self.assertIn("Vtg -> GPIB1::2::INSTR", text)
        self.assertIn("VISA resources visible", text)
        self.assertIn("CONNECT ATTEMPT role=Vbg address=GPIB1::3::INSTR", text)
        self.assertIn("CONNECT ATTEMPT role=Vtg address=GPIB1::2::INSTR", text)
        self.assertIn("CONNECT SUCCESS role=Vbg address=GPIB1::3::INSTR", text)
        self.assertIn("CONNECT SUCCESS role=Vtg address=GPIB1::2::INSTR", text)
        self.assertIn("SMU CONNECTION SUMMARY", text)

    def test_connect_failure_reports_stage_and_address(self):
        res_good = _FakeResource(responses={"*IDN?": REAL_2400_IDN})
        rm = _FakeResourceManager(
            resources={"GPIB1::2::INSTR": res_good},
            fail_open={"GPIB1::3::INSTR"},
        )
        worker = _SMUWorker()
        output = io.StringIO()
        with (
            patch("pyvisa.ResourceManager", return_value=rm),
            redirect_stdout(output),
        ):
            worker.connect_instrument(
                ["GPIB1::3::INSTR", "GPIB1::2::INSTR"],
                {"Vbg": "GPIB1::3::INSTR", "Vtg": "GPIB1::2::INSTR", "Vbias": None},
                "\n",
                {},
            )
        text = output.getvalue()
        self.assertIn(
            "CONNECT ATTEMPT role=Vbg address=GPIB1::3::INSTR",
            text,
        )
        self.assertIn(
            "CONNECT FAILED role=Vbg address=GPIB1::3::INSTR stage=OPEN",
            text,
        )
        self.assertIn(
            "CONNECT SUCCESS role=Vtg address=GPIB1::2::INSTR model=MODEL 2400",
            text,
        )
        self.assertIn(
            "Vbg address=GPIB1::3::INSTR model=unknown status=FAILED",
            text,
        )
        self.assertIn(
            "Vtg address=GPIB1::2::INSTR model=MODEL 2400 status=CONNECTED",
            text,
        )

    def test_constructor_trace_io_reports_open_and_idn(self):
        resource = _FakeResource(responses={"*IDN?": REAL_2400_IDN})
        rm = _FakeResourceManager(resources={"GPIB1::2::INSTR": resource})
        output = io.StringIO()
        with redirect_stdout(output):
            KeithControl(
                "GPIB1::2::INSTR",
                "Vbg_SMU",
                "Vbg",
                rm,
                curr_compliance=6e-7,
                volt_compliance=20.0,
                configure_on_connect=False,
                trace_io=True,
            )
        text = output.getvalue()
        self.assertIn("OPEN VISA", text)
        self.assertIn("OPEN VISA OK", text)
        self.assertIn("QUERY *IDN?", text)

    def test_connect_reports_exact_timed_out_query_and_preserves_primary_error(self):
        failing = _FakeResource(fail_on_query={":SENS:CURR:PROT?"})
        rm = _FakeResourceManager(resources={"GPIB1::2::INSTR": failing})
        worker = _SMUWorker()
        errors = []
        worker.error.connect(errors.append)
        with patch("pyvisa.ResourceManager", return_value=rm):
            worker.connect_instrument(
                ["GPIB1::2::INSTR"],
                {"Vbg": "GPIB1::2::INSTR", "Vtg": None, "Vbias": None},
                "\n",
                {"GPIB1::2::INSTR": {"curr": 6e-7, "volt": 20.0}},
            )
        self.assertIsNone(worker.device)
        combined = "\n".join(errors)
        self.assertIn("PRIMARY FAILURE", combined)
        self.assertIn("QUERY :SENS:CURR:PROT?", combined)
        self.assertIn("LAST SUCCESSFUL OPERATION", combined)
        self.assertIn("POST-FAILURE DIAGNOSTICS", combined)
        self.assertIn("post_failure_io: suppressed", combined)
        self.assertNotIn("VISA clear: success", combined)
        self.assertEqual(failing.queries[-1], ":SENS:CURR:PROT?")
        self.assertEqual(failing.clears, 1)
        self.assertTrue(failing.closed)

    def test_partial_connection_keeps_successful_instrument(self):
        bad = _FakeResource(fail_on_query={":SENS:CURR:PROT?"})
        good = _FakeResource()
        rm = _FakeResourceManager(
            resources={
                "GPIB1::2::INSTR": bad,
                "GPIB1::3::INSTR": good,
            }
        )
        worker = _SMUWorker()
        connected = []
        errors = []
        worker.connected.connect(connected.append)
        worker.error.connect(errors.append)
        with patch("pyvisa.ResourceManager", return_value=rm):
            worker.connect_instrument(
                ["GPIB1::2::INSTR", "GPIB1::3::INSTR"],
                {
                    "Vbg": "GPIB1::2::INSTR",
                    "Vtg": "GPIB1::3::INSTR",
                    "Vbias": None,
                },
                "\n",
                {
                    "GPIB1::2::INSTR": {"curr": 6e-7, "volt": 20.0},
                    "GPIB1::3::INSTR": {"curr": 6e-7, "volt": 20.0},
                },
            )
        self.assertEqual(connected, [["GPIB1::3::INSTR"]])
        self.assertIsNotNone(worker.device)
        self.assertTrue(worker.device.role_is_available("Vtg"))
        self.assertFalse(worker.device.role_is_available("Vbg"))
        self.assertTrue(bad.closed)
        combined = "\n".join(errors)
        self.assertIn("GPIB1::2::INSTR", combined)

    def test_connect_keeps_output_off_and_sets_trigger_immediate(self):
        resource = _FakeResource()
        rm = _FakeResourceManager(resources={"GPIB1::2::INSTR": resource})
        worker = _SMUWorker()
        with patch("pyvisa.ResourceManager", return_value=rm):
            worker.connect_instrument(
                ["GPIB1::2::INSTR"],
                {"Vbg": "GPIB1::2::INSTR", "Vtg": None, "Vbias": None},
                "\n",
                {},
            )
        self.assertNotIn(":OUTP ON", resource.writes)
        self.assertIn(":TRIG:SOUR IMM", resource.writes)
        self.assertIn("TRIG:COUN 1", resource.writes)
        self.assertIn(":SOUR:CLE:AUTO OFF", resource.writes)
        self.assertIn("*CLS", resource.writes)
        self.assertIn(":ABOR", resource.writes)
        self.assertGreaterEqual(resource.clears, 1)

    def test_voltage_step_preserves_existing_level_and_ensures_output_on(self):
        instrument = object.__new__(KeithControl)
        instrument._curr_compliance_A = 500e-9
        instrument._volt_range_V = 20.0
        writes = []
        instrument.write = lambda command, print_command=False: writes.append(command)
        instrument.query = lambda command, **_kwargs: "0"

        instrument.set_volt_step(
            curr_compliance=500e-9,
            volt_compliance=20.0,
        )

        self.assertFalse(any(command.startswith(":SOUR:VOLT:LEV") for command in writes))
        self.assertIn(":SOUR:CLE:AUTO OFF", writes)
        self.assertEqual(writes[-1], ":OUTP ON")

    def test_voltage_sweep_disables_source_auto_clear_before_output_on(self):
        instrument = object.__new__(KeithControl)
        instrument._curr_compliance_A = 500e-9
        instrument._volt_range_V = 20.0
        writes = []
        instrument.write = lambda command, print_command=False: writes.append(command)
        instrument.query = lambda command, **_kwargs: "0"

        instrument.set_volt_sweep(
            curr_compliance=500e-9,
            volt_compliance=20.0,
        )

        auto_clear_index = writes.index(":SOUR:CLE:AUTO OFF")
        first_output_on_index = writes.index(":OUTP ON")
        self.assertLess(auto_clear_index, first_output_on_index)
        self.assertNotIn(":OUTP OFF", writes)

    def test_voltage_step_rejects_auto_clear_that_remains_enabled(self):
        instrument = object.__new__(KeithControl)
        instrument._curr_compliance_A = 500e-9
        instrument._volt_range_V = 20.0
        instrument.write = lambda _command, print_command=False: None
        instrument.query = lambda _command, **_kwargs: "1"

        with self.assertRaisesRegex(RuntimeError, "persistent-output"):
            instrument.set_volt_step(
                curr_compliance=500e-9,
                volt_compliance=20.0,
            )

    def test_500_na_compliance_uses_one_microamp_range(self):
        self.assertAlmostEqual(
            KeithControl.recommended_current_range(500e-9),
            1e-6,
        )

    def test_live_limits_force_auto_range_for_500_na_without_output(self):
        instrument = object.__new__(KeithControl)
        instrument._rsyn_supported = True
        writes = []
        responses = {
            ":SENS:CURR:PROT?": "5e-7",
            ":SENS:CURR:RANG:AUTO?": "0",
            ":SENS:CURR:RANG?": "1e-6",
            ":SOUR:VOLT:RANG?": "20",
            ":SYST:ERR?": '0,"No error"',
        }
        instrument.write = lambda command, print_command=False: writes.append(command)
        instrument.query = lambda command: responses[command]

        result = instrument.apply_compliance_settings(500e-9, 100e-6, 20.0)

        self.assertEqual(
            writes,
            [
                ":SENS:CURR:RANG:AUTO OFF",
                ":SENS:CURR:PROT:RSYN ON",
                ":SENS:CURR:PROT 5e-07",
                ":SENS:CURR:RANG 1e-06",
                ":SOUR:VOLT:RANG 20",
            ],
        )
        self.assertNotIn(":OUTP ON", writes)
        self.assertAlmostEqual(result["curr"], 500e-9)
        self.assertAlmostEqual(result["curr_range"], 1e-6)

    def test_range_sync_handles_lower_compliance_without_manual_range_write(self):
        instrument = object.__new__(KeithControl)
        instrument._rsyn_supported = True
        instrument._curr_compliance_A = 50e-6
        instrument._curr_range_A = 100e-6
        instrument._volt_range_V = 21.0
        writes = []
        responses = {
            ":SENS:CURR:PROT?": "1e-5",
            ":SENS:CURR:RANG:AUTO?": "0",
            ":SENS:CURR:RANG?": "1e-5",
            ":SOUR:VOLT:RANG?": "21",
            ":SYST:ERR?": '0,"No error"',
        }
        instrument.write = lambda command, print_command=False: writes.append(command)
        instrument.query = lambda command: responses[command]

        instrument.apply_compliance_settings(10e-6, 10e-6, 21.0)

        self.assertEqual(
            writes,
            [
                ":SENS:CURR:RANG:AUTO OFF",
                ":SENS:CURR:PROT:RSYN ON",
                ":SENS:CURR:PROT 1e-05",
                ":SENS:CURR:RANG 1e-05",
                ":SOUR:VOLT:RANG 21",
            ],
        )

    def test_range_sync_avoids_824_when_raising_500_na_to_10_ua(self):
        instrument = object.__new__(KeithControl)
        instrument._rsyn_supported = True
        instrument._curr_compliance_A = 500e-9
        instrument._curr_range_A = 1e-6
        instrument._volt_range_V = 21.0
        writes = []
        responses = {
            ":SENS:CURR:PROT?": "1e-5",
            ":SENS:CURR:RANG:AUTO?": "0",
            ":SENS:CURR:RANG?": "1e-5",
            ":SOUR:VOLT:RANG?": "21",
            ":SYST:ERR?": '0,"No error"',
        }
        instrument.write = lambda command, print_command=False: writes.append(command)
        instrument.query = lambda command: responses[command]

        instrument.apply_compliance_settings(10e-6, 10e-6, 21.0)

        self.assertEqual(
            writes,
            [
                ":SENS:CURR:RANG:AUTO OFF",
                ":SENS:CURR:PROT:RSYN ON",
                ":SENS:CURR:PROT 1e-05",
                ":SENS:CURR:RANG 1e-05",
                ":SOUR:VOLT:RANG 21",
            ],
        )

    def test_dead_vbg_read_reports_role_and_address(self):
        dead = _DeadInstrument()
        setup = SimpleNamespace(
            y_channel_collection=_FakeYChannels(dead),
            x_channel_collection=_FakeXChannels(dead),
            get_single_y_value=lambda _key: 0.0,
        )
        device = IVDevice(
            setup,
            role_map={"Vbg": "GPIB1::2::INSTR", "Vtg": None, "Vbias": None},
        )

        with self.assertRaisesRegex(
            SMUCommunicationError,
            r"Vbg read_current failed on GPIB1::2::INSTR",
        ):
            device.read_currents(strict=True)

    def test_dead_vbg_write_is_not_silently_ignored(self):
        dead = _DeadInstrument()
        setup = SimpleNamespace(
            x_channel_collection=_FakeXChannels(dead),
        )
        device = IVDevice(
            setup,
            role_map={"Vbg": "GPIB1::2::INSTR", "Vtg": None, "Vbias": None},
        )

        with self.assertRaisesRegex(
            SMUCommunicationError,
            r"Vbg set_voltage failed on GPIB1::2::INSTR",
        ):
            device.set_gates(Vbg=1.0, ramp_step=0.0, delay_s=0.0)

    def test_zero_ramp_continues_after_one_role_fails(self):
        device = object.__new__(IVDevice)
        calls = []
        device.has_role = lambda _role: True

        def ramp(role, *_args):
            calls.append(role)
            if role == "Vbg":
                raise TimeoutError("dead Vbg")
            return True

        device.ramp_to = ramp
        errors = device.ramp_all_to_zero(ramp_step=0.1, delay_s=0.0)

        self.assertEqual(calls, ["Vbias", "Vbias", "Vbg", "Vtg", "Vtg"])
        self.assertEqual(len(errors), 1)
        self.assertIn("Vbg", errors[0])

    def test_coordinated_zero_pairs_measured_gate_steps_before_each_delay(self):
        for bg, tg, ticks in ((0.20, -0.08, 4), (-0.075, 0.15, 3)):
            with self.subTest(bg=bg, tg=tg):
                device, resources, events = _recording_ramp_device({"Vbg": bg, "Vtg": tg})
                with patch("app.devices.iv_adapter.time.sleep", side_effect=lambda delay: events.append(("wait", delay))):
                    report = device.ramp_all_to_zero_report(
                        ramp_step=0.05, delay_s=0.04, coordinated_gates=True)

                self.assertEqual(
                    {role: result["status"] for role, result in report.items()},
                    {"Vbg": "reached_zero", "Vtg": "reached_zero"},
                )
                self.assertCountEqual(events[:2], [("read", "Vbg", "READ?"), ("read", "Vtg", "READ?")])
                self.assertEqual(len(events), 2 + 3 * ticks)
                previous = (bg, tg)
                for tick in range(1, ticks + 1):
                    offset = 2 + 3 * (tick - 1)
                    bg_write, tg_write, wait = events[offset:offset + 3]
                    self.assertEqual(bg_write[:2], ("write", "Vbg"))
                    self.assertEqual(tg_write[:2], ("write", "Vtg"))
                    self.assertEqual(wait, ("wait", 0.04))
                    current = (bg_write[2], tg_write[2])
                    for start, before, after in zip((bg, tg), previous, current):
                        self.assertAlmostEqual(after, start * (1 - tick / ticks), places=3)
                        self.assertLessEqual(abs(after - before), 0.05 + 1e-12)
                    previous = current
                self.assertEqual(previous, (0.0, 0.0))
                self.assertEqual([resource.voltage for resource in resources.values()], [0.0, 0.0])

    def test_coordinated_zero_keeps_bias_axes_independent_and_bias2_optional(self):
        for include_bias2 in (False, True):
            with self.subTest(include_bias2=include_bias2):
                device, resources, events = _recording_ramp_device(
                    {"Vbg": 0.10, "Vtg": -0.08, "Vbias": 0.10, "Vbias2": -0.10})
                report = device.ramp_all_to_zero_report(
                    ramp_step=0.05, delay_s=0, include_bias2=include_bias2, coordinated_gates=True)
                roles = [event[1] for event in events if event[0] == "write"]
                paired = ["Vbg", "Vtg"] * 2
                expected = (["Vbias2"] * 3 if include_bias2 else []) + ["Vbias"] * 3 + paired
                self.assertEqual(roles, expected)
                self.assertTrue(all(result["status"] == "reached_zero" for result in report.values()))
                self.assertEqual(resources["Vbias"].voltage, 0.0)
                self.assertEqual(resources["Vbias2"].voltage, 0.0 if include_bias2 else -0.10)

    def test_coordinated_zero_requires_finite_readback_from_both_gates(self):
        for invalid in (float("nan"), float("inf"), -float("inf")):
            for role in ("Vbg", "Vtg"):
                with self.subTest(invalid=invalid, role=role):
                    starts = {"Vbg": 0.15, "Vtg": -0.10}
                    starts[role] = invalid
                    device, _resources, events = _recording_ramp_device(starts)
                    with patch("app.devices.iv_adapter.time.sleep") as wait:
                        report = device.ramp_all_to_zero_report(
                            ramp_step=0.05, delay_s=0.04, coordinated_gates=True)
                    self.assertEqual(set(report), {"Vbg", "Vtg"})
                    self.assertTrue(all(result["status"] != "reached_zero" for result in report.values()))
                    self.assertTrue(events)
                    self.assertTrue(all(event[0] == "read" for event in events))
                    wait.assert_not_called()

    def test_coordinated_zero_read_timeout_freezes_without_cached_ramp(self):
        device, resources, events = _recording_ramp_device({"Vbg": 0.15, "Vtg": -0.10})
        resources["Vbg"].fail_on_query.add("READ?")
        report = device.ramp_all_to_zero_report(ramp_step=0.05, delay_s=0, coordinated_gates=True)
        self.assertTrue(device.io_frozen)
        self.assertEqual(events, [("read", "Vbg", "READ?")])
        self.assertEqual(set(report), {"Vbg", "Vtg"})
        self.assertTrue(all(result["status"] != "reached_zero" for result in report.values()))

    def test_coordinated_zero_refuses_gate_without_a_read_instrument(self):
        device, _resources, events = _recording_ramp_device({"Vbg": 0.15, "Vtg": -0.10})
        channels = device.setup.y_channel_collection
        lookup = channels.get_instrument
        # Keep the cached Y entry and the write instrument available.
        with patch.object(channels, "get_instrument",
                          side_effect=lambda name: None if name == "measured_Vtg" else lookup(name)):
            report = device.ramp_all_to_zero_report(ramp_step=0.05, delay_s=0, coordinated_gates=True)
        self.assertEqual(events, [("read", "Vbg", "READ?")])
        self.assertEqual(set(report), {"Vbg", "Vtg"})
        self.assertTrue(all(result["status"] != "reached_zero" for result in report.values()))

    def test_coordinated_zero_partial_tick_failure_sends_no_retry_or_later_writes(self):
        device, resources, events = _recording_ramp_device({"Vbg": 0.20, "Vtg": -0.08})
        resources["Vtg"].fail_on_write.add(":SOUR:VOLT:LEV -0.040")
        with patch("app.devices.iv_adapter.time.sleep", side_effect=lambda delay: events.append(("wait", delay))):
            report = device.ramp_all_to_zero_report(
                ramp_step=0.05, delay_s=0.04, coordinated_gates=True)
        self.assertEqual(events, [
            ("read", "Vbg", "READ?"), ("read", "Vtg", "READ?"),
            ("write", "Vbg", 0.15), ("write", "Vtg", -0.06), ("wait", 0.04),
            ("write", "Vbg", 0.10), ("write", "Vtg", -0.04),
        ])
        self.assertTrue(device.io_frozen)
        self.assertEqual(device.last_communication_error.role, "Vtg")
        self.assertEqual((resources["Vbg"].voltage, resources["Vtg"].voltage), (0.10, -0.06))
        self.assertEqual(set(report), {"Vbg", "Vtg"})
        self.assertTrue(all(result["status"] != "reached_zero" for result in report.values()))
        events.clear()
        frozen = device.ramp_all_to_zero_report(ramp_step=0.05, delay_s=0, coordinated_gates=True)
        self.assertEqual(events, [])
        self.assertTrue(all(result["status"] == "skipped_frozen_after_error" for result in frozen.values()))

    def test_coordinated_zero_final_tick_failure_reports_only_confirmed_command(self):
        device, resources, events = _recording_ramp_device({"Vbg": 0.10, "Vtg": -0.08})
        resources["Vtg"].fail_on_write.add(":SOUR:VOLT:LEV 0.000")
        with patch("app.devices.iv_adapter.time.sleep") as wait:
            report = device.ramp_all_to_zero_report(
                ramp_step=0.05, delay_s=0.04, coordinated_gates=True)
        self.assertEqual(report["Vbg"]["status"], "reached_zero")
        self.assertNotEqual(report["Vtg"]["status"], "reached_zero")
        self.assertEqual(events[-2:], [("write", "Vbg", 0.0), ("write", "Vtg", 0.0)])
        self.assertEqual((resources["Vbg"].voltage, resources["Vtg"].voltage), (0.0, -0.04))
        self.assertTrue(device.io_frozen)
        wait.assert_called_once_with(0.04)

    def test_coordinated_zero_invalid_parameters_never_write_bias_or_gates(self):
        for step, delay in ((0, 0.04), (-0.05, 0.04), (float("nan"), 0.04),
                            (0.05, -0.04), (0.05, float("inf"))):
            with self.subTest(step=step, delay=delay):
                device, _resources, events = _recording_ramp_device(
                    {"Vbg": 0.1, "Vtg": -0.1, "Vbias": 0.08})
                report = device.ramp_all_to_zero_report(
                    ramp_step=step, delay_s=delay, coordinated_gates=True)
                self.assertEqual(set(report), {"Vbg", "Vtg", "Vbias"})
                self.assertTrue(all(result["status"] == "failed" for result in report.values()))
                self.assertEqual(events, [])

    def test_coordinated_zero_wrapper_forwards_options_and_reports_failures(self):
        device, _resources, events = _recording_ramp_device({"Vbg": float("nan"), "Vtg": -0.08})
        errors = device.ramp_all_to_zero(ramp_step=0.05, delay_s=0, coordinated_gates=True)
        self.assertEqual({error.split(":", 1)[0] for error in errors}, {"Vbg", "Vtg"})
        self.assertTrue(events)
        self.assertFalse(any(event[0] == "write" for event in events))

    def test_default_zero_is_serial_and_coordinated_mode_can_zero_one_gate(self):
        device, _resources, events = _recording_ramp_device({"Vbg": 0.10, "Vtg": -0.10})
        self.assertEqual(device.ramp_all_to_zero(ramp_step=0.05, delay_s=0), [])
        self.assertEqual([event[1] for event in events if event[0] == "write"], ["Vbg"] * 3 + ["Vtg"] * 3)

        device, resources, events = _recording_ramp_device({"Vtg": -0.10})
        report = device.ramp_all_to_zero_report(ramp_step=0.05, delay_s=0, coordinated_gates=True)
        self.assertEqual(set(report), {"Vtg"})
        self.assertEqual(report["Vtg"]["status"], "reached_zero")
        self.assertEqual(resources["Vtg"].voltage, 0.0)

    def test_duplicate_connection_is_rejected_before_opening_visa(self):
        worker = _SMUWorker()
        worker._device = object()
        errors = []
        worker.error.connect(errors.append)

        worker.connect_instrument([], {}, "\n", {})

        self.assertEqual(
            errors,
            ["SMU is already connected. Disconnect before reconnecting."],
        )

    def test_failed_io_freezes_without_post_failure_queries(self):
        instrument = _PowerCycledInstrument()
        setup = SimpleNamespace(
            x_channel_collection=_FakeXChannels(instrument),
        )
        device = IVDevice(
            setup,
            role_map={"Vbg": "GPIB1::2::INSTR", "Vtg": None, "Vbias": None},
        )
        device.set_operation_context(frame=28, frame_total=161)

        with self.assertRaises(SMUCommunicationError) as raised:
            device.set_gates(Vbg=-5.3, ramp_step=0.0, delay_s=0.0)

        error = raised.exception
        self.assertEqual(error.role, "Vbg")
        self.assertEqual(error.operation, "set_voltage")
        self.assertIsNone(error.diagnosis["power_on_bit_set"])
        self.assertIsNone(error.diagnosis["output_on"])
        self.assertEqual(
            error.diagnosis["classification"],
            "communication_fault_frozen",
        )
        self.assertEqual(
            device.health_states["Vbg"],
            "frozen_reconnect_required",
        )
        self.assertTrue(device.io_frozen)
        self.assertTrue(error.diagnosis["post_failure_io_suppressed"])
        self.assertEqual(error.diagnosis["destructive_queries"], [])
        self.assertEqual(error.context["frame"], 28)
        self.assertEqual(instrument.queries, [])

        with self.assertRaises(SMUCommunicationError):
            device.set_gates(Vbg=-5.2, ramp_step=0.0, delay_s=0.0)
        self.assertEqual(instrument.write_calls, 1)

    def test_incident_report_contains_context_diagnosis_and_cleanup(self):
        error = SMUCommunicationError(
            "Vbg read timed out",
            role="Vbg",
            address="GPIB1::2::INSTR",
            operation="read_current",
            command=":READ?",
            timeout_ms=5000,
            diagnosis={
                "classification": "power_cycle_detected",
                "power_on_bit_set": True,
                "output_on": False,
            },
            recent_operations=[{"operation": "read_current", "status": "failed"}],
            context={"frame": 28, "frame_total": 161},
        )
        incident = build_hardware_incident(
            error,
            stage="smu_io",
            run_context={"condition": "BG2only"},
            cleanup={
                "roles": {
                    "Vbg": {"status": "skipped_reconnect_required"},
                    "Vtg": {"status": "reached_zero"},
                }
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = HardwareIncidentRecorder(Path(tmp)).write(incident)
            record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])

        self.assertEqual(record["hardware"]["role"], "Vbg")
        self.assertEqual(record["run_context"]["frame"], 28)
        self.assertEqual(record["run_context"]["condition"], "BG2only")
        self.assertTrue(
            record["hardware"]["diagnosis"]["power_on_bit_set"]
        )
        self.assertEqual(
            record["cleanup"]["roles"]["Vtg"]["status"],
            "reached_zero",
        )

    def test_stop_during_failed_read_is_reported_as_hardware_incident(self):
        stop_event = threading.Event()
        instrument = _FailsDuringPostAcquisitionRead(stop_event)

        def _get_x(key):
            if key in ("Vbg", "Vtg"):
                return 0.0
            raise KeyError(key)

        setup = SimpleNamespace(
            x_channel_collection=_FakeXChannels(instrument),
            y_channel_collection=_FakeYChannels(instrument),
            get_single_x_value=_get_x,
            get_single_y_value=lambda _key: 0.0,
        )
        device = IVDevice(
            setup,
            role_map={
                "Vbg": "GPIB1::2::INSTR",
                "Vtg": "GPIB1::2::INSTR",
                "Vbias": None,
            },
        )
        smu = SimpleNamespace(is_connected=True, device=device)
        batch = pd.DataFrame([{
            "Run": True,
            "When": "",
            "MeasurePower": False,
            "condition_label": "BG2only",
            "repeat": 1,
            "frames": 2,
            "Vbg_start": 0.0,
            "Vbg_stop": 0.1,
            "Vtg_start": 0.0,
            "Vtg_stop": 0.0,
            "Vbias_start": "",
            "Vbias_stop": "",
        }])
        sequence = [{
            "Center Wavelength (nm)": 700.0,
            "Exposure Time (ms)": 1.0,
            "Accumulations (EPF)": 1,
        }]
        run_meta = {
            "device_id": "sample",
            "point": "p1",
            "tag": "",
            "temperature": "4K",
            "measurement_mode": "PL",
            "laser_nm": "633",
            "power_uw": "1",
            "power_coefficient": 1.0,
            "subfolder": "Initial Data",
        }

        with tempfile.TemporaryDirectory() as tmp:
            worker = _RunWorker(
                sequence,
                batch,
                lf6_ctrl=_FakeLFController(),
                smu_ctrl=smu,
                out_dir=Path(tmp),
                run_meta=run_meta,
                filename_parts=["device_id"],
                stop_event=stop_event,
            )
            incidents = []
            finished = []
            worker.incident.connect(incidents.append)
            worker.finished.connect(lambda success, message: finished.append((success, message)))
            with (
                patch.object(cfg.ramp, "delay_s", 0.0),
                patch.object(cfg.ramp, "settle_s", 0.0),
            ):
                worker.run()

            report_path = Path(tmp) / "hardware_incidents.jsonl"
            records = [
                json.loads(line)
                for line in report_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(len(incidents), 1)
        self.assertFalse(finished[0][0])
        self.assertIn("Hardware incident", finished[0][1])
        self.assertIn(records[0]["hardware"]["role"], ("Vbg", "Vtg"))
        self.assertEqual(records[0]["run_context"]["frame"], 1)
        self.assertEqual(
            records[0]["hardware"]["diagnosis"]["classification"],
            "communication_fault_frozen",
        )
        self.assertTrue(device.io_frozen)
        self.assertEqual(instrument.queries, [])
        for result in records[0]["cleanup"].get("roles", {}).values():
            self.assertEqual(result["status"], "skipped_frozen_after_error")


if __name__ == "__main__":
    unittest.main()
