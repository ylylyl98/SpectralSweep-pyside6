from __future__ import annotations

import csv
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.engine.mcd2100_worker import MCD2100Cancelled, MCD2100Worker


class _Handle:
    def __init__(self, value=True): self.value = value
    def result(self, timeout=None): return self.value
    def wait_drained(self, timeout=None): return self.value


def _snapshot(field):
    return SimpleNamespace(
        field_t=field, temperature_k=4.0, setpoint_t=field,
        status=SimpleNamespace(
            quench=False, driven_mode=True, persistent_mode=False,
            backend_details={"field_control": True},
        ),
    )


class _Controller:
    def __init__(self, start, stop):
        self.start, self.stop = start, stop
        self.target = start
        self.ramp_seen = False
        self.events = []
        self.detach_calls = 0
        self.stop_calls = 0

    def set_h_setpoint_async(self, value):
        self.target = float(value); self.ramp_seen = False
        self.events.append(("set", self.target)); return _Handle(self.target)

    def start_field_control_async(self):
        self.events.append("start"); return _Handle()

    def read_snapshot_async(self):
        if self.target == self.start:
            field = self.start
        elif not self.ramp_seen:
            self.ramp_seen = True
            field = (self.target + self.start) / 2.0
        else:
            field = self.target
        self.events.append(("read", field))
        return _Handle(_snapshot(field))

    def read_field_async(self):
        if self.target == self.start:
            field = self.start
        elif not self.ramp_seen:
            self.ramp_seen = True
            field = (self.target + self.start) / 2.0
        else:
            field = self.target
        self.events.append(("field", field))
        return _Handle(field)

    def request_stop(self):
        self.stop_calls += 1; return _Handle()

    def detach_completed_run_async(self, target, gate, verified_snapshot=None):
        self.detach_calls += 1; return _Handle()


class _Optical:
    wavelengths = [700.0, 701.0]
    def __init__(self): self.events = []; self.gates = []
    def configure(self, **kwargs): self.events.append(("configure", kwargs))
    def apply_gates(self, **kwargs): self.gates.append(kwargs); self.events.append(("gate", kwargs["vtg_v"], kwargs["vbg_v"])); return {"ok": True}
    def move_to(self, angle): self.events.append(("move", angle))
    def get_position(self): self.events.append("position"); return 12.0
    def acquire(self, angle, label, stop_event):
        self.events.append(("acquire", angle)); return self.wavelengths, [1.0, 2.0], 12.0
    def cleanup(self): self.events.append("cleanup")


class _FailOnSecondGate(_Optical):
    def apply_gates(self, **kwargs):
        if len(self.gates) >= 1:
            raise RuntimeError("simulated gate fault")
        return super().apply_gates(**kwargs)


class MCD2100BatchTests(unittest.TestCase):
    def test_initial_telemetry_phases_and_gate_settling_are_logged_and_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            controller = _Controller(-0.01, 0.01)
            optical = _Optical()
            logs, phases = [], []
            worker = MCD2100Worker(
                controller, optical, -0.01, 0.01, [0.0], td,
                apply_voltages=True,
                conditions=[
                    {"enabled": True, "vtg_v": 1.0, "vbg_v": 2.0},
                    {"enabled": True, "vtg_v": 3.0, "vbg_v": 4.0},
                ],
                initial_voltage_settle_s=0.001,
                voltage_settle_s=0.001,
                filename_temperature_k=4.2,
                filename_temperature_source="live_sample_readback",
                poll_interval_s=0.001, gate_timeout_s=0.2,
                operation_timeout_s=1.0, cleanup_timeout_s=1.0,
            )
            worker.set_callbacks(log=logs.append, phase=phases.append)
            result = worker.run()
            self.assertEqual(result["status"], "COMPLETED")
            self.assertTrue(any("Initial magnet telemetry" in line for line in logs))
            self.assertTrue(any("Ramping gate 1/2" in line for line in phases))
            self.assertTrue(any("Gate settling after first gate ramp" in line for line in phases))
            self.assertTrue(any("Gate settling after later gate ramp" in line for line in phases))
            metadata = json.loads(Path(result["metadata_path"]).read_text(encoding="utf-8"))
            self.assertEqual(metadata["gate_settling_requested"]["initial_voltage_settle_s"], 0.001)
            self.assertEqual(metadata["gate_settling_requested"]["voltage_settle_s"], 0.001)
            self.assertEqual(
                [item["condition_index"] for item in metadata["gate_settling"]],
                [1, 2],
            )
            self.assertTrue(all(item["completed"] for item in metadata["gate_settling"]))
            self.assertEqual(
                metadata["filename_temperature"],
                {"sample_temperature_k": 4.2, "source": "live_sample_readback"},
            )
            self.assertTrue(all("_MCD_4p2K_G" in Path(path).name for path in result["csv_paths"]))

    def test_long_gate_settling_is_cancelable(self):
        with tempfile.TemporaryDirectory() as td:
            worker = MCD2100Worker(
                _Controller(-0.01, 0.01), _Optical(), -0.01, 0.01, [0.0], td,
                initial_voltage_settle_s=30.0,
                voltage_settle_s=30.0,
                poll_interval_s=0.001, gate_timeout_s=0.2,
                operation_timeout_s=1.0, cleanup_timeout_s=1.0,
            )
            timer = threading.Timer(0.02, worker.request_cancel)
            timer.start()
            started = time.monotonic()
            with self.assertRaises(MCD2100Cancelled):
                worker._wait_for_voltage_settle(
                    initial_ramp=True, condition_index=1, metadata={}
                )
            timer.join()
            self.assertLess(time.monotonic() - started, 1.0)

    def test_round_trip_multi_gate_batch_files_and_order(self):
        with tempfile.TemporaryDirectory() as td:
            controller = _Controller(-0.01, 0.01)
            optical = _Optical()
            observed = []
            worker = MCD2100Worker(
                controller, optical, -0.01, 0.01, [0.0, 90.0], td,
                bidirectional=False,
                apply_voltages=True,
                conditions=[
                    {"enabled": True, "vtg_v": 1.0, "vbg_v": 2.0},
                    {"enabled": True, "vtg_v": 3.0, "vbg_v": 4.0},
                    {"enabled": True, "vtg_v": 5.0, "vbg_v": 6.0},
                ],
                poll_interval_s=0.001, gate_timeout_s=0.2,
                operation_timeout_s=1.0, cleanup_timeout_s=1.0,
                metadata={"device_id": "YZ365", "point": "p5n2"},
            )
            durable_row_counts = []
            def observe(event):
                observed.append(event)
                with Path(event["file_path"]).open(newline="", encoding="utf-8") as stream:
                    durable_row_counts.append(len(list(csv.reader(stream))) - 1)
            worker.set_callbacks(spectrum_event=observe)
            result = worker.run()
            self.assertEqual(result["status"], "COMPLETED")
            self.assertEqual(controller.detach_calls, 1)
            self.assertEqual(controller.stop_calls, 0)
            self.assertEqual(len(result["csv_paths"]), 3)
            self.assertTrue(
                all(Path(path).name.startswith("YZ365_p5n2_MCD_") for path in result["csv_paths"])
            )
            self.assertTrue(all("_G0" in Path(path).name for path in result["csv_paths"]))
            self.assertTrue(all("B-0p01to+0p01T" in Path(path).name for path in result["csv_paths"]))
            self.assertTrue(all("_roundtrip.csv" in path for path in result["csv_paths"]))
            self.assertEqual(len(optical.gates), 3)
            self.assertTrue(observed)
            self.assertTrue(all(count >= 1 for count in durable_row_counts))
            self.assertIn("forward", [event["direction"] for event in observed])
            self.assertTrue(all(Path(event["file_path"]).exists() for event in observed))
            self.assertTrue(all(event["total_spectra"] >= 1 for event in observed))
            self.assertEqual(
                [(item["condition_index"], item["direction"]) for item in result["file_details"]],
                [(1, "roundtrip"), (2, "roundtrip"), (3, "roundtrip")],
            )
            for detail in result["file_details"]:
                self.assertIn("gate_parameters", detail)
                self.assertIn("requested_gate", detail)
                self.assertIn("derived_coordinates", detail)
                self.assertEqual(detail["file_status"], "complete")
                self.assertEqual(detail["directions"], ["forward", "backward"])
                self.assertGreaterEqual(detail["spectra_written"], 0)
            nonempty = 0
            for path in result["csv_paths"]:
                with Path(path).open(newline="", encoding="utf-8") as stream:
                    rows = list(csv.reader(stream))
                nonempty += len(rows) > 1
                directions = {row[2] for row in rows[1:]}
                self.assertTrue(directions.issubset({"forward", "backward"}))
            self.assertGreaterEqual(nonempty, 2)
            acquire_index = next(i for i, event in enumerate(optical.events) if isinstance(event, tuple) and event[0] == "acquire")
            self.assertLess(optical.events.index("position"), acquire_index)

    def test_abnormal_gate_failure_stops_once_and_skips_later_gates(self):
        with tempfile.TemporaryDirectory() as td:
            controller = _Controller(-0.01, 0.01)
            optical = _FailOnSecondGate()
            worker = MCD2100Worker(
                controller, optical, -0.01, 0.01, [0.0], td,
                bidirectional=True, apply_voltages=True,
                conditions=[{"enabled": True, "vtg_v": 1.0, "vbg_v": 2.0},
                            {"enabled": True, "vtg_v": 3.0, "vbg_v": 4.0},
                            {"enabled": True, "vtg_v": 5.0, "vbg_v": 6.0}],
                poll_interval_s=0.001, gate_timeout_s=0.2,
                operation_timeout_s=1.0, cleanup_timeout_s=1.0,
            )
            result = worker.run()
            self.assertEqual(result["status"], "FAILED")
            self.assertEqual(controller.stop_calls, 1)
            self.assertEqual(len(optical.gates), 1)
            self.assertTrue(result["file_details"])
            self.assertFalse(any(item["condition_index"] == 3 for item in result["file_details"]))


if __name__ == "__main__":
    unittest.main()
