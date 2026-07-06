from __future__ import annotations

import unittest
import json
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

from app.devices.iv_adapter import IVDevice, SMUCommunicationError
from controllers.smu_controller import _SMUWorker
from iv_automation import KeithControl, PyvisaInstrument
from ui.presets_panel import _RunWorker
from utils.config import cfg
from utils.hardware_incidents import (
    HardwareIncidentRecorder,
    build_hardware_incident,
)


class _FakeResource:
    read_termination = None
    write_termination = None


class _FakeResourceManager:
    def __init__(self):
        self.open_calls = []

    def open_resource(self, address, **kwargs):
        self.open_calls.append((address, kwargs))
        return _FakeResource()


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
    def acquire(self):
        return np.array([700.0, 701.0]), np.array([10.0, 11.0])


class _FakeLFController:
    is_connected = True
    adapter = _FakeLFAdapter()

    def apply_settings(self, *_args):
        return None


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


class SMUResilienceTests(unittest.TestCase):
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

    def test_power_cycle_is_diagnosed_and_role_is_quarantined(self):
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
        self.assertTrue(error.diagnosis["power_on_bit_set"])
        self.assertFalse(error.diagnosis["output_on"])
        self.assertEqual(
            error.diagnosis["classification"],
            "power_cycle_detected",
        )
        self.assertEqual(
            device.health_states["Vbg"],
            "recovered_reinit_required",
        )
        self.assertEqual(error.context["frame"], 28)
        self.assertEqual(
            instrument.queries,
            ["*IDN?", "*ESR?", ":OUTP?", ":SYST:ERR?"],
        )

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
            if key == "Vbg":
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
            role_map={"Vbg": "GPIB1::2::INSTR", "Vtg": None, "Vbias": None},
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
        self.assertEqual(records[0]["hardware"]["role"], "Vbg")
        self.assertEqual(records[0]["run_context"]["frame"], 1)
        self.assertEqual(
            records[0]["hardware"]["diagnosis"]["classification"],
            "power_cycle_detected",
        )


if __name__ == "__main__":
    unittest.main()
