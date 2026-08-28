import csv
import inspect
import json
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from app.engine.mcd2100_worker import DiscreteMCD2100Worker as MCD2100Worker, MCD2100Worker as ContinuousWorker


class _ContinuousHandle:
    def __init__(self, events, name, value=True): self.events, self.name, self.value = events, name, value
    def result(self, timeout=None): self.events.append(self.name + ".result"); return self.value
    def wait_drained(self, timeout=None): self.events.append(self.name + ".drain"); return self.value


class _ContinuousClock:
    def __init__(self): self.value = 0.0
    def __call__(self): return self.value
    def sleep(self, seconds): self.value += seconds


def _safe_snapshot(field, *, temperature=4.0, quench=False, driven=True, persistent=False, field_control=True):
    return SimpleNamespace(
        field_t=field, temperature_k=temperature, setpoint_t=field,
        status=SimpleNamespace(
            quench=quench, driven_mode=driven, persistent_mode=persistent,
            backend_details={"field_control": field_control, "h_state": "RAMPING"},
        ),
    )


def _temperature_snapshot(sample, *, target=None, sample_control=True,
                          ramp=True, vti_control=True, vti=None):
    return SimpleNamespace(
        sample_temperature_k=float(sample),
        sample_setpoint_k=float(sample if target is None else target),
        sample_control_active=sample_control,
        sample_ramp_active=ramp,
        sample_ramp_rate_k_per_min=1.0,
        vti_temperature_k=float(vti if vti is not None else max(1.8, sample - 10.0)),
        vti_setpoint_k=float(max(1.8, (sample if target is None else target) - 10.0)),
        vti_control_active=vti_control,
    )


class _ContinuousFakeController:
    def __init__(self, fields, *, detach=True):
        self.events, self.fields, self.detach_calls = [], list(fields), 0
        self.target = float(self.fields[0].field_t) if self.fields else 0.0
        self.detach_enabled = detach
        self.detach_verified_snapshot = None
        self.temperature_snapshots = []
        self.sample_temperatures = []
    def _handle(self, name, value=True):
        self.events.append(name); return _ContinuousHandle(self.events, name, value)
    def set_h_setpoint_async(self, value):
        self.target = float(value)
        return self._handle(f"set:{float(value):g}")
    def start_field_control_async(self): return self._handle("start")
    def read_snapshot_async(self):
        value = self.fields.pop(0) if self.fields else _safe_snapshot(self.target)
        return self._handle("read", value)
    def read_field_async(self):
        value = self.fields.pop(0) if self.fields else _safe_snapshot(self.target)
        return self._handle("field", value.field_t)
    def request_stop(self): return self._handle("stop")
    def configure_sample_temperature_async(self, target, ramp_rate):
        self.events.append(f"temperature.configure:{float(target):g}:{float(ramp_rate):g}")
        value = (self.temperature_snapshots[0] if self.temperature_snapshots
                 else _temperature_snapshot(target, target=target))
        return _ContinuousHandle(self.events, "temperature.configure", value)
    def read_temperature_snapshot_async(self):
        value = (self.temperature_snapshots.pop(0) if self.temperature_snapshots
                 else _temperature_snapshot(self.target, target=self.target))
        self.events.append("temperature.read")
        return _ContinuousHandle(self.events, "temperature.read", value)
    def read_sample_temperature_async(self):
        value = self.sample_temperatures.pop(0) if self.sample_temperatures else 4.0
        self.events.append("temperature.sample")
        return _ContinuousHandle(self.events, "temperature.sample", value)
    def detach_completed_run_async(self, target, gate, verified_snapshot=None):
        self.detach_calls += 1
        self.detach_verified_snapshot = verified_snapshot
        return self._handle("detach") if self.detach_enabled else None


class _ContinuousFakeOptical:
    wavelengths = [700.0, 701.0]
    def __init__(self, *, row_wavelengths=None, cancel_on=None):
        self.events, self.acquire_count, self.cancel_on = [], 0, cancel_on
        self.row_wavelengths = row_wavelengths or self.wavelengths
    def prepare(self, stop_event): return self.wavelengths
    def move_to(self, angle): self.events.append(("move", angle))
    def get_position(self): self.events.append(("position",)); return 33.0
    def acquire(self, angle, label, stop_event):
        self.events.append(("acquire", angle)); self.acquire_count += 1
        if self.cancel_on == self.acquire_count: stop_event.set()
        return list(self.row_wavelengths), [1.0, 3.0, 5.0] if len(self.row_wavelengths) == 3 else [1.0, 2.0], 33.0
    def cleanup(self): self.events.append(("cleanup",))


class ContinuousContractTests(unittest.TestCase):
    def _run_scripted(self, fields, *, start=0.0, stop=.01, angles=(0.0,), bidirectional=False,
                      optical=None, fsync=None):
        controller = _ContinuousFakeController(fields)
        optical = optical or _ContinuousFakeOptical()
        clock = _ContinuousClock()
        tempdir = tempfile.TemporaryDirectory()
        td = tempdir.name
        context = mock.patch("app.engine.mcd2100_worker.os.fsync", fsync) if fsync else __import__("contextlib").nullcontext()
        with context:
                result = ContinuousWorker(
                    controller, optical, start, stop, angles, td,
                    bidirectional=bidirectional, poll_interval_s=.01,
                    gate_timeout_s=.3, operation_timeout_s=2.0, cleanup_timeout_s=1.0,
                    sleep=clock.sleep, clock=clock,
                ).run()
        return result, controller, optical, tempdir

    def test_continuous_reverse_executes_two_legs_without_stop(self):
        fields = ([_safe_snapshot(0.0)] * 5 +
                  [_safe_snapshot(v) for v in (
                      .004, .006, .006, .006, .01, .01, .01, .01,
                      .006, .006, .004, .004, 0.0, 0.0, 0.0, 0.0,
                  )])
        result, controller, _, tempdir = self._run_scripted(fields, angles=(0.0, 90.0), bidirectional=True)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual([event for event in controller.events if event.startswith("set:") and not event.endswith((".result", ".drain"))],
                         ["set:0.01", "set:0"])
        csv_text = Path(result["csv_path"]).read_text(encoding="utf-8")
        self.assertIn(",forward,", csv_text); self.assertIn(",backward,", csv_text)
        self.assertNotIn("stop", controller.events)
        self.assertEqual(controller.detach_calls, 1)
        tempdir.cleanup()

    def test_round_trip_progress_uses_forward_half_and_backward_half(self):
        worker = object.__new__(ContinuousWorker)
        worker.conditions = []
        worker.bidirectional = True
        self.assertEqual(worker._overall_progress_percent(leg=1, leg_fraction=0.0), 0.0)
        self.assertEqual(worker._overall_progress_percent(leg=1, leg_fraction=1.0), 50.0)
        self.assertEqual(worker._overall_progress_percent(leg=2, leg_fraction=0.0), 50.0)
        self.assertEqual(worker._overall_progress_percent(leg=2, leg_fraction=1.0), 100.0)

    def test_multi_condition_progress_is_global(self):
        worker = object.__new__(ContinuousWorker)
        worker.conditions = [{"enabled": True}, {"enabled": True}]
        worker.bidirectional = True
        self.assertEqual(worker._overall_progress_percent(
            leg=1, leg_fraction=1.0, condition_index=1, condition_count=2
        ), 25.0)
        self.assertEqual(worker._overall_progress_percent(
            leg=2, leg_fraction=1.0, condition_index=1, condition_count=2
        ), 50.0)
        self.assertEqual(worker._overall_progress_percent(
            leg=2, leg_fraction=1.0, condition_index=2, condition_count=2
        ), 100.0)

    def test_reverse_leg_reuses_forward_endpoint_without_second_five_sample_gate(self):
        fields = ([_safe_snapshot(0.0)] * 5 +
                  [_safe_snapshot(v) for v in (
                      .004, .004, .006, .006, .01, .01,
                      .006, .006, .004, .004, 0.0, 0.0,
                  )])
        controller = _ContinuousFakeController(fields)
        optical = _ContinuousFakeOptical()
        clock = _ContinuousClock()
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        worker = ContinuousWorker(
            controller, optical, 0.0, .01, [0.0], tempdir.name,
            bidirectional=True, poll_interval_s=.01, gate_timeout_s=.3,
            operation_timeout_s=2.0, cleanup_timeout_s=1.0,
            sleep=clock.sleep, clock=clock,
        )
        with mock.patch.object(worker, "_wait_gate", wraps=worker._wait_gate) as wait_gate:
            result = worker.run()
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(wait_gate.call_count, 1)

    def test_crossing_cycle_retained_and_no_endpoint_rows(self):
        fields = [_safe_snapshot(0.0)] * 5 + [_safe_snapshot(0.0)] + [
            _safe_snapshot(.005), _safe_snapshot(.02), _safe_snapshot(.006), _safe_snapshot(.02), _safe_snapshot(.02)
        ]
        result, controller, _, tempdir = self._run_scripted(fields, angles=(0.0, 90.0))
        with open(result["csv_path"], newline="", encoding="utf-8") as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(len(rows) - 1, 2)
        self.assertEqual(controller.detach_calls, 1)
        tempdir.cleanup()

    def test_cycle_boundary_endpoint_snapshot_is_reused_for_detach(self):
        fields = ([_safe_snapshot(0.0)] * 5 + [
            _safe_snapshot(.002), _safe_snapshot(.003), _safe_snapshot(.008),
            _safe_snapshot(.01), _safe_snapshot(.012),
        ])
        result, controller, _, tempdir = self._run_scripted(fields)
        self.assertEqual(result["status"], "COMPLETED", result)
        # The cycle-boundary field-only read is followed by the authoritative
        # full snapshot when the endpoint is reached.  The latter is the
        # snapshot reused for detach validation.
        self.assertEqual(controller.detach_verified_snapshot.field_t, .012)
        self.assertEqual(controller.fields, [])
        self.assertEqual(controller.detach_calls, 1)
        self.assertNotIn("stop", controller.events)
        tempdir.cleanup()

    def test_temperature_stabilizes_before_field_command_and_is_saved_per_spectrum(self):
        fields = ([_safe_snapshot(0.0)] * 5 + [
            _safe_snapshot(.002), _safe_snapshot(.003), _safe_snapshot(.008),
            _safe_snapshot(.01),
        ])
        controller = _ContinuousFakeController(fields)
        controller.temperature_snapshots = [
            _temperature_snapshot(19.7, target=20.0, vti_control=True),
            _temperature_snapshot(20.0, target=20.0, vti_control=True),
        ]
        controller.sample_temperatures = [19.9, 20.1]
        clock = _ContinuousClock()
        with tempfile.TemporaryDirectory() as td:
            result = ContinuousWorker(
                controller, _ContinuousFakeOptical(), 0.0, .01, [0.0], td,
                temperature_control_enabled=True, sample_target_k=20.0,
                sample_ramp_rate_k_per_min=2.0, temperature_tolerance_k=.05,
                temperature_stable_s=0.0, temperature_timeout_s=1.0,
                poll_interval_s=.01, gate_timeout_s=.3,
                operation_timeout_s=2.0, cleanup_timeout_s=1.0,
                sleep=clock.sleep, clock=clock,
            ).run()
            with Path(result["csv_path"]).open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            metadata = json.loads(Path(result["metadata_path"]).read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertLess(
            controller.events.index("temperature.configure:20:2"),
            controller.events.index("set:0.01"),
        )
        self.assertAlmostEqual(float(rows[0]["sample_T0_K"]), 19.9)
        self.assertAlmostEqual(float(rows[0]["sample_T1_K"]), 20.1)
        self.assertAlmostEqual(float(rows[0]["sample_Tmid_K"]), 20.0)
        self.assertEqual(metadata["temperature_stabilization"]["status"], "stable")
        self.assertEqual(metadata["temperature_requested"]["vti_coordination"], "cryostat automatic")
        self.assertIsNone(metadata["temperature_requested"]["ramp_rate_k_per_min"])
        self.assertEqual(metadata["temperature_requested"]["ramp_control"], "unchanged")

    def test_temperature_timeout_is_bounded_and_prevents_field_and_optical_start(self):
        controller = _ContinuousFakeController([_safe_snapshot(0.0)] * 20)
        controller.temperature_snapshots = [
            _temperature_snapshot(5.0, target=20.0, vti_control=True)
            for _ in range(10)
        ]
        optical = _ContinuousFakeOptical()
        clock = _ContinuousClock()
        with tempfile.TemporaryDirectory() as td:
            result = ContinuousWorker(
                controller, optical, 0.0, .01, [0.0], td,
                temperature_control_enabled=True, sample_target_k=20.0,
                sample_ramp_rate_k_per_min=1.0, temperature_tolerance_k=.05,
                temperature_stable_s=.1, temperature_timeout_s=.025,
                poll_interval_s=.01, gate_timeout_s=.3,
                operation_timeout_s=2.0, cleanup_timeout_s=1.0,
                sleep=clock.sleep, clock=clock,
            ).run()
            metadata = json.loads(Path(result["metadata_path"]).read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("did not stabilize", result["error"])
        self.assertEqual(optical.acquire_count, 0)
        self.assertFalse(any(event.startswith("set:") for event in controller.events))
        self.assertNotIn("stop", controller.events)
        self.assertFalse(metadata["magnet_stop_requested"])
        self.assertEqual(metadata["magnet_stop_reason"], "no magnet command was issued")

    def test_continuous_preposition_timeout_leaves_healthy_field_control_active(self):
        fields = [_safe_snapshot(.1)] * 100
        result, controller, optical, tempdir = self._run_scripted(fields)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual([event for event in controller.events if event == "stop"], [])
        self.assertNotIn("set:0.01", controller.events)
        self.assertEqual(optical.acquire_count, 0); self.assertEqual(controller.detach_calls, 0)
        metadata = json.loads(Path(result["metadata_path"]).read_text(encoding="utf-8"))
        self.assertFalse(metadata["magnet_stop_requested"])
        self.assertIn("field control left active", metadata["magnet_stop_reason"])
        tempdir.cleanup()

    def test_endpoint_reached_is_accepted_before_expired_leg_watchdog(self):
        fields = ([_safe_snapshot(0.0)] * 5 +
                  [_safe_snapshot(.002), _safe_snapshot(.003), _safe_snapshot(.01),
                   _safe_snapshot(.01), _safe_snapshot(.01)])
        controller = _ContinuousFakeController(fields)
        clock = _ContinuousClock()

        class SlowOptical(_ContinuousFakeOptical):
            def acquire(self, angle, label, stop_event):
                clock.sleep(3.0)
                return super().acquire(angle, label, stop_event)

        optical = SlowOptical()
        with tempfile.TemporaryDirectory() as td:
            result = ContinuousWorker(
                controller, optical, 0.0, .01, [0.0], td,
                poll_interval_s=.01, gate_timeout_s=.3,
                operation_timeout_s=2.0, cleanup_timeout_s=1.0,
                sleep=clock.sleep, clock=clock,
            ).run()
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertGreater(clock.value, 2.0)
        self.assertNotIn("stop", controller.events)

    def test_valid_progress_refreshes_leg_watchdog(self):
        fields = ([_safe_snapshot(0.0)] * 5 +
                  [_safe_snapshot(.001), _safe_snapshot(.002), _safe_snapshot(.003),
                   _safe_snapshot(.004), _safe_snapshot(.005), _safe_snapshot(.006),
                   _safe_snapshot(.007), _safe_snapshot(.008), _safe_snapshot(.01),
                   _safe_snapshot(.01), _safe_snapshot(.01)])
        controller = _ContinuousFakeController(fields)
        clock = _ContinuousClock()

        class SlowOptical(_ContinuousFakeOptical):
            def acquire(self, angle, label, stop_event):
                clock.sleep(.75)
                return super().acquire(angle, label, stop_event)

        with tempfile.TemporaryDirectory() as td:
            result = ContinuousWorker(
                controller, SlowOptical(), 0.0, .01, [0.0], td,
                poll_interval_s=.01, gate_timeout_s=.3,
                operation_timeout_s=1.0, cleanup_timeout_s=1.0,
                sleep=clock.sleep, clock=clock,
            ).run()
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertGreater(clock.value, 1.0)
        self.assertNotIn("stop", controller.events)

    def test_leg_inactivity_timeout_is_bounded_and_remains_fail_safe(self):
        fields = [_safe_snapshot(0.0)] * 5 + [_safe_snapshot(.001)] * 30
        controller = _ContinuousFakeController(fields)
        clock = _ContinuousClock()

        class StalledOptical(_ContinuousFakeOptical):
            def acquire(self, angle, label, stop_event):
                clock.sleep(.1)
                return super().acquire(angle, label, stop_event)

        with tempfile.TemporaryDirectory() as td:
            result = ContinuousWorker(
                controller, StalledOptical(), 0.0, .01, [0.0], td,
                poll_interval_s=.01, gate_timeout_s=.3,
                operation_timeout_s=.05, cleanup_timeout_s=1.0,
                sleep=clock.sleep, clock=clock,
            ).run()
            metadata = json.loads(Path(result["metadata_path"]).read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("made no progress", result["error"])
        self.assertTrue(metadata["magnet_stop_requested"])
        self.assertIn("stop", controller.events)

    def test_preposition_inside_start_gate_skips_initial_set_start(self):
        fields = ([_safe_snapshot(0.0)] * 5 +
                  [_safe_snapshot(0.0), _safe_snapshot(.004), _safe_snapshot(.006),
                   _safe_snapshot(.01)])
        result, controller, _, tempdir = self._run_scripted(fields, start=0.0, stop=.01)
        self.assertEqual(result["status"], "COMPLETED", result)
        mutations = [event for event in controller.events
                     if event.startswith("set:") and not event.endswith((".result", ".drain"))]
        self.assertEqual(mutations, ["set:0.01"])
        self.assertEqual(controller.events.count("start"), 1)
        metadata = json.loads(Path(result["metadata_path"]).read_text(encoding="utf-8"))
        self.assertTrue(metadata["preposition_skipped"])
        tempdir.cleanup()

    def test_preposition_outside_start_gate_keeps_set_start(self):
        fields = ([_safe_snapshot(.1)] + [_safe_snapshot(0.0)] * 5 +
                  [_safe_snapshot(0.0), _safe_snapshot(.004), _safe_snapshot(.006),
                   _safe_snapshot(.01)])
        result, controller, _, tempdir = self._run_scripted(fields, start=0.0, stop=.01)
        self.assertEqual(result["status"], "COMPLETED", result)
        mutations = [event for event in controller.events
                     if event.startswith("set:") and not event.endswith((".result", ".drain"))]
        self.assertEqual(mutations, ["set:0", "set:0.01"])
        self.assertEqual(controller.events.count("start"), 2)
        metadata = json.loads(Path(result["metadata_path"]).read_text(encoding="utf-8"))
        self.assertFalse(metadata["preposition_skipped"])
        tempdir.cleanup()

    def test_preposition_skip_out_of_gate_read_resets_until_five_consecutive(self):
        fields = ([_safe_snapshot(0.0), _safe_snapshot(0.0), _safe_snapshot(.1)] +
                  [_safe_snapshot(0.0)] * 5 +
                  [_safe_snapshot(0.0), _safe_snapshot(.004), _safe_snapshot(.006),
                   _safe_snapshot(.01)])
        result, controller, _, tempdir = self._run_scripted(fields, start=0.0, stop=.01)
        self.assertEqual(result["status"], "COMPLETED", result)
        mutations = [event for event in controller.events
                     if event.startswith("set:") and not event.endswith((".result", ".drain"))]
        self.assertEqual(mutations, ["set:0.01"])
        self.assertEqual(controller.events.count("start"), 1)
        self.assertGreaterEqual(controller.events.count("read"), 9)
        self.assertGreaterEqual(controller.events.count("field"), 3)
        tempdir.cleanup()

    def test_continuous_wrong_way_increasing_stops_before_next_acquisition(self):
        fields = ([_safe_snapshot(0.0)] * 5 +
                  [_safe_snapshot(.005), _safe_snapshot(.005), _safe_snapshot(.002)])
        result, controller, optical, tempdir = self._run_scripted(
            fields, angles=(0.0, 90.0), start=0.0, stop=.01
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("wrong direction", result["error"])
        self.assertEqual(optical.acquire_count, 1)
        self.assertEqual(controller.detach_calls, 0)
        self.assertEqual(controller.events.count("stop"), 1)
        tempdir.cleanup()

    def test_continuous_wrong_way_decreasing_stops_before_next_acquisition(self):
        fields = ([_safe_snapshot(.01)] * 5 +
                  [_safe_snapshot(.005), _safe_snapshot(.005), _safe_snapshot(.008)])
        result, controller, optical, tempdir = self._run_scripted(
            fields, angles=(0.0, 90.0), start=.01, stop=0.0
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("wrong direction", result["error"])
        self.assertEqual(optical.acquire_count, 1)
        self.assertEqual(controller.detach_calls, 0)
        self.assertEqual(controller.events.count("stop"), 1)
        tempdir.cleanup()

    def test_continuous_cancel_preserves_partial_csv_and_metadata_and_stops(self):
        fields = [_safe_snapshot(0.0)] * 5 + [_safe_snapshot(0.0)] + [
            _safe_snapshot(.004), _safe_snapshot(.006), _safe_snapshot(.005)
        ]
        result, controller, optical, tempdir = self._run_scripted(fields, angles=(0.0, 90.0), optical=_ContinuousFakeOptical(cancel_on=2))
        self.assertIn(result["status"], {"CANCELLED", "FAILED"})
        self.assertEqual(optical.acquire_count, 2); self.assertEqual(controller.detach_calls, 0)
        self.assertEqual([event for event in controller.events if event == "stop"], ["stop"])
        self.assertEqual(result["spectra_written"], 1)
        metadata = json.loads(Path(result["metadata_path"]).read_text(encoding="utf-8"))
        self.assertEqual(metadata["spectra_written"], 1)
        tempdir.cleanup()

    def test_continuous_wavelength_interpolation_and_durable_sync(self):
        fields = [_safe_snapshot(0.0)] * 5 + [_safe_snapshot(0.0), _safe_snapshot(.004), _safe_snapshot(.006), _safe_snapshot(.01)]
        optical = _ContinuousFakeOptical(row_wavelengths=[700.0, 700.5, 701.0])
        calls = []
        result, _, _, tempdir = self._run_scripted(fields, optical=optical, fsync=lambda fd: calls.append(fd))
        with open(result["csv_path"], newline="", encoding="utf-8") as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(rows[0][-2:], ["700.0", "701.0"])
        self.assertEqual([float(value) for value in rows[1][-2:]], [1.0, 5.0])
        self.assertGreaterEqual(len(calls), 4)
        tempdir.cleanup()

    def test_no_fallible_work_after_success_detach(self):
        fields = [_safe_snapshot(0.0)] * 5 + [_safe_snapshot(0.0), _safe_snapshot(.004), _safe_snapshot(.006), _safe_snapshot(.01)]
        controller = _ContinuousFakeController(fields)
        committed = {"value": False}
        original = ContinuousWorker._write_metadata
        def guarded(path, data):
            if committed["value"]: raise AssertionError("metadata write after detach commit")
            return original(path, data)
        old_detach = controller.detach_completed_run_async
        def detach(target, gate, verified_snapshot=None):
            committed["value"] = True
            return old_detach(target, gate, verified_snapshot)
        controller.detach_completed_run_async = detach
        optical = _ContinuousFakeOptical(); clock = _ContinuousClock()
        with tempfile.TemporaryDirectory() as td, mock.patch.object(ContinuousWorker, "_write_metadata", staticmethod(guarded)):
            result = ContinuousWorker(controller, optical, 0.0, .01, [0.0], td, poll_interval_s=.01,
                                      gate_timeout_s=.3, operation_timeout_s=2, cleanup_timeout_s=1,
                                      sleep=clock.sleep, clock=clock).run()
        self.assertEqual(result["status"], "COMPLETED")

    def test_continuous_shared_optical_path_orders_b0_acquire_b1(self):
        events = []

        class SharedController(_ContinuousFakeController):
            def __init__(self, fields):
                super().__init__(fields)
                self.events = events

            def read_snapshot_async(self):
                value = self.fields.pop(0) if self.fields else _safe_snapshot(.01)
                events.append(f"snapshot:{value.field_t:g}")
                return _ContinuousHandle(events, "read", value)

            def read_field_async(self):
                value = self.fields.pop(0) if self.fields else _safe_snapshot(.01)
                events.append(f"field:{value.field_t:g}")
                return _ContinuousHandle(events, "field", value.field_t)

            def _handle(self, name, value=True):
                events.append(name)
                return _ContinuousHandle(events, name, value)

        class SharedOptical:
            wavelengths = [700.0, 701.0]

            def prepare(self, _stop_event):
                return self.wavelengths

            def move_to(self, _angle):
                events.append("move")

            def get_position(self):
                events.append("position")
                return 33.0

            def acquire(self, _angle, _label, _stop_event):
                events.append("acquire")
                return self.wavelengths, [1.0, 2.0], 33.0

            def cleanup(self):
                events.append("cleanup")

        fields = ([_safe_snapshot(0.0)] * 5 +
                  [_safe_snapshot(0.0), _safe_snapshot(.004), _safe_snapshot(.006),
                   _safe_snapshot(.01)])
        controller = SharedController(fields)
        optical = SharedOptical()
        clock = _ContinuousClock()
        with tempfile.TemporaryDirectory() as td:
            result = ContinuousWorker(
                controller, optical, 0.0, .01, [33.0], td,
                poll_interval_s=.01, gate_timeout_s=.3, operation_timeout_s=2.0,
                cleanup_timeout_s=1.0, sleep=clock.sleep, clock=clock,
            ).run()
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertLess(events.index("field:0.004"), events.index("acquire"))
        self.assertLess(events.index("acquire"), events.index("field:0.006"))
        b0_index = events.index("field:0.004")
        b1_index = events.index("field:0.006")
        self.assertFalse(any(event.startswith("snapshot:") for event in events[b0_index + 1:b1_index]))

    def test_executable_forward_run_preserves_command_and_acquisition_order(self):
        class Handle:
            def __init__(self, events, name, value=True): self.events, self.name, self.value = events, name, value
            def result(self, timeout=None): self.events.append(self.name + ".result"); return self.value
            def wait_drained(self, timeout=None): self.events.append(self.name + ".drain"); return self.value
        class Clock:
            def __init__(self): self.t = 0.0
            def __call__(self): return self.t
            def sleep(self, value): self.t += value
        class Controller:
            def __init__(self):
                self.events, self.fields = [], [0.0] * 5 + [0.0, 0.005, 0.01]
            def _h(self, name, value=True): self.events.append(name); return Handle(self.events, name, value)
            def set_h_setpoint_async(self, value): return self._h("set:" + str(value))
            def start_field_control_async(self): return self._h("start")
            def read_snapshot_async(self):
                field = self.fields.pop(0) if self.fields else .01
                status = SimpleNamespace(quench=False, driven_mode=True, persistent_mode=False,
                                         backend_details={"field_control": True, "h_state": "RAMPING"})
                return self._h("read", SimpleNamespace(field_t=field, temperature_k=4.0, setpoint_t=field, status=status))
            def read_field_async(self):
                field = self.fields.pop(0) if self.fields else .01
                return self._h("field", field)
            def detach_completed_run_async(self, target, gate, verified_snapshot=None): return self._h("detach")
            def request_stop(self): return self._h("stop")
        class Optical:
            wavelengths = [700.0, 701.0]
            def __init__(self): self.events = []
            def prepare(self, stop): return self.wavelengths
            def move_to(self, angle): self.events.append("move")
            def get_position(self): self.events.append("position"); return 33.0
            def acquire(self, angle, label, stop): self.events.append("acquire"); return self.wavelengths, [1.0, 2.0], 33.0
            def cleanup(self): self.events.append("cleanup")
        controller, optical, clock = Controller(), Optical(), Clock()
        with tempfile.TemporaryDirectory() as td:
            result = ContinuousWorker(controller, optical, 0.0, .01, [45.0], td,
                                      poll_interval_s=.01, gate_timeout_s=1, operation_timeout_s=2,
                                      cleanup_timeout_s=1, sleep=clock.sleep, clock=clock).run()
            self.assertEqual(result["status"], "COMPLETED", result)
            self.assertEqual(controller.events.count("detach"), 1)
            self.assertNotIn("stop", controller.events)
            self.assertEqual(sum(event in {"set:0.0", "set:0.01"} for event in controller.events), 1)
            self.assertEqual(controller.events.count("start"), 1)
            self.assertGreaterEqual(controller.events.count("read"), 6)
            self.assertGreaterEqual(controller.events.count("field"), 3)
            for index, event in enumerate(controller.events[:-1]):
                if event.endswith(".result"):
                    self.assertTrue(controller.events[index + 1].endswith(".drain"))
            self.assertLess(optical.events.index("move"), optical.events.index("acquire"))
            self.assertLess(controller.events.index("set:0.01"), controller.events.index("start"))
            with open(result["csv_path"], newline="", encoding="utf-8") as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(rows[0][:13], [
                "timestamp_start_utc", "timestamp_end_utc", "leg", "direction",
                "rotation_angle_deg", "B0_T", "B1_T", "Bmid_T", "Vtg_V", "Vbg_V",
                "Vbias_V", "Doping_V", "Efield_V",
            ])
            self.assertEqual(rows[1][2], "forward")
            self.assertEqual(float(rows[1][4]), 33.0)
            self.assertEqual(float(rows[1][5]), 0.005)
            self.assertEqual(float(rows[1][6]), 0.01)
            self.assertEqual(float(rows[1][7]), 0.0075)

    def test_equal_endpoints_and_axis_contract_fail_closed(self):
        with self.assertRaises(ValueError):
            ContinuousWorker(object(), object(), 1.0, 1.0, [0.0], tempfile.gettempdir())
        self.assertEqual(ContinuousWorker._align_counts([700.0, 701.0], [700.0, 700.5, 701.0], [1.0, 2.0, 3.0]), [1.0, 3.0])
        self.assertEqual(ContinuousWorker._align_counts([700.0], [700.0], [1.0]), [1.0])
        with self.assertRaises(RuntimeError):
            ContinuousWorker._align_counts([700.0, 701.0], [701.0, 700.0], [1.0, 2.0])

    def test_target_specific_gates_and_leg_labels_are_continuous_contract(self):
        worker = object.__new__(ContinuousWorker)
        worker.gate_t = None
        worker.start_field_t, worker.stop_field_t = 0.01, 5.0
        self.assertEqual(worker._target_gate(0.01), .001)
        self.assertEqual(worker._target_gate(5.0), .001)
        self.assertIn("forward", inspect.getsource(ContinuousWorker._leg))

    def test_paths_reserve_csv_meta_and_log_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            worker = object.__new__(ContinuousWorker)
            worker.output_dir, worker.stem = Path(td), "run"
            Path(td, "run.log").write_text("existing")
            csv_path, meta_path = worker._paths()
            self.assertEqual(csv_path.name, "run_1.csv")
            self.assertEqual(meta_path.name, "run_1.meta.json")

    def test_forbidden_mode_and_ramp_apis_are_absent_from_continuous_source(self):
        source = inspect.getsource(ContinuousWorker)
        for forbidden in ("setDrivenMode", "setPersistentMode3D", "setRampRate", "stopFieldControl"):
            self.assertNotIn(forbidden, source)

    def test_absolute_preposition_contract_is_explicit(self):
        source = inspect.getsource(ContinuousWorker._wait_gate)
        self.assertIn("abs(field - target)", source)
        self.assertIn("consecutive >= 5", source)

    def test_continuous_rows_capture_midpoint_and_utc_boundaries(self):
        source = inspect.getsource(ContinuousWorker._leg)
        self.assertIn("timestamp_start", source)
        self.assertIn("timestamp_end", source)
        self.assertIn("(b0 + b1) / 2.0", source)

    def test_executable_five_sample_gate_resets_and_times_out(self):
        class Clock:
            def __init__(self): self.t = 0.0
            def __call__(self): return self.t
            def sleep(self, value): self.t += value
        def snapshot(field):
            return SimpleNamespace(field_t=field, temperature_k=4.0,
                status=SimpleNamespace(quench=False, driven_mode=True, persistent_mode=False,
                    backend_details={"field_control": True}))
        clock = Clock(); worker = object.__new__(ContinuousWorker)
        worker.gate_t = .001; worker.start_field_t = worker.stop_field_t = 0.0
        worker.poll_interval_s = .1; worker.gate_timeout_s = 2.0
        worker.clock, worker.sleep, worker.stop_event = clock, clock.sleep, threading.Event()
        values = iter([snapshot(0), snapshot(0), snapshot(.1), snapshot(0), snapshot(0), snapshot(0), snapshot(0), snapshot(0)])
        worker._read_snapshot = lambda: next(values)
        worker._wait_gate(0.0, increasing=True)
        timeout_worker = object.__new__(ContinuousWorker)
        timeout_worker.gate_t = .001; timeout_worker.start_field_t = timeout_worker.stop_field_t = 0.0
        timeout_worker.poll_interval_s = .1; timeout_worker.gate_timeout_s = .25
        timeout_worker.clock = Clock(); timeout_worker.sleep = timeout_worker.clock.sleep; timeout_worker.stop_event = threading.Event()
        timeout_worker._read_snapshot = lambda: snapshot(.1)
        with self.assertRaises(Exception):
            timeout_worker._wait_gate(0.0, increasing=True)


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.on_sleep = None

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds
        if self.on_sleep:
            self.on_sleep()


class FakeHandle:
    def __init__(self, events, name, value=None, error=None):
        self.events, self.name, self.value, self.error = events, name, value, error

    def result(self, timeout=None):
        self.events.append(f"{self.name}.result")
        if self.error:
            raise self.error
        return self.value

    def wait_drained(self, timeout=None):
        self.events.append(f"{self.name}.drain")
        return self.value


def snapshot(field, *, temperature=1.8, quench=False, field_control=True,
             driven_mode=True, persistent_mode=False):
    status = SimpleNamespace(
        quench=quench, driven_mode=driven_mode, persistent_mode=persistent_mode,
        backend_details={"field_control": field_control}
    )
    return SimpleNamespace(field_t=field, temperature_k=temperature, status=status)


class FakeController:
    """Public controller surface only; there is deliberately no adapter/SDK API."""
    def __init__(self, events):
        self.events = events
        self.target = 0.0
        self.snapshot_factory = lambda: snapshot(self.target)
        self.set_error = None
        self.start_error = None
        self._stop_handle = None
        self.stop_vendor_calls = 0

    def set_h_setpoint_async(self, target):
        self.events.append(f"set.submit:{target}")
        self.target = float(target)
        self._stop_handle = None
        return FakeHandle(self.events, "set", self.target, self.set_error)

    def start_field_control_async(self):
        self.events.append("start.submit")
        return FakeHandle(self.events, "start", True, self.start_error)

    def read_snapshot_async(self):
        self.events.append("read.submit")
        return FakeHandle(self.events, "read", self.snapshot_factory())

    def request_stop(self):
        self.events.append("stop.request")
        if self._stop_handle is None:
            self.stop_vendor_calls += 1
            self.events.append("stop.vendor")
            self._stop_handle = FakeHandle(self.events, "stop", True)
        return self._stop_handle

    def __getattr__(self, name):
        if "adapter" in name.lower() or "vendor" in name.lower() or "sdk" in name.lower():
            raise AssertionError(f"forbidden controller access: {name}")
        raise AttributeError(name)


class FakeOptical:
    wavelengths = [700.0, 701.0]

    def __init__(self, events):
        self.events = events
        self.acquire_count = 0
        self.on_acquire = None
        self.fail_on = None

    def prepare(self, stop_event):
        self.events.append("optical.prepare")
        return self.wavelengths

    def acquire(self, angle, label, stop_event):
        self.acquire_count += 1
        self.events.append(f"optical.acquire:{angle}")
        if self.on_acquire:
            self.on_acquire(self.acquire_count)
        if self.fail_on == self.acquire_count:
            raise IOError("camera failure")
        return self.wavelengths, [10.0 + self.acquire_count, 20.0], angle

    def cleanup(self):
        self.events.append("optical.cleanup")


class MCD2100WorkflowTests(unittest.TestCase):
    def make_worker(self, *, targets=(1.0,), angles=(45.0,), settling=None):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        events = []
        clock = FakeClock()
        controller = FakeController(events)
        optical = FakeOptical(events)
        settings = {
            "field_tolerance_t": 0.01,
            "relative_tolerance": 0.0,
            "db_dt_t_per_s": 1.0,
            "slope_window_s": 0.0,
            "stable_hold_s": 0.0,
            "polling_interval_s": 0.1,
            "settle_timeout_s": 0.5,
            "operation_timeout_s": 1.0,
            "cleanup_timeout_s": 1.0,
        }
        settings.update(settling or {})
        worker = MCD2100Worker(
            controller, optical, targets, angles, self.temp.name,
            settling=settings, clock=clock, sleep=clock.sleep,
        )
        return worker, controller, optical, clock, events

    @staticmethod
    def csv_rows(result):
        with Path(result["csv_path"]).open(newline="", encoding="utf-8") as stream:
            return list(csv.reader(stream))

    @staticmethod
    def metadata(result):
        return json.loads(Path(result["metadata_path"]).read_text(encoding="utf-8"))

    def test_optical_preflight_failure_precedes_gate_mutation(self):
        events = []

        class Optical:
            def configure(self, **_kwargs):
                events.append("configure")
                raise RuntimeError("LightField frozen")

            def apply_gates(self, **_kwargs):
                events.append("apply_gates")

        with tempfile.TemporaryDirectory() as td:
            worker = ContinuousWorker(
                FakeController(events), Optical(), 0.0, 1.0, [45.0], td,
                apply_voltages=True, lf_center_nm=800.0, lf_exposure_ms=10.0,
                lf_frames=1, operation_timeout_s=1.0, cleanup_timeout_s=1.0,
            )
            with self.assertRaises(RuntimeError):
                worker._apply_setup(configure=True, apply_gate=True)
        self.assertEqual(events, ["configure"])

    def test_single_point_orders_stabilizes_acquires_saves_and_completes(self):
        worker, controller, optical, _, events = self.make_worker()
        result = worker.run()
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["spectra_written"], 1)
        self.assertEqual(len(self.csv_rows(result)), 2)
        self.assertEqual(self.metadata(result)["status"], "COMPLETED")
        order = [events.index(item) for item in (
            "set.submit:1.0", "set.result", "set.drain", "start.submit",
            "start.result", "start.drain", "optical.acquire:45.0",
            "optical.cleanup",
        )]
        self.assertEqual(order, sorted(order))
        self.assertEqual(controller.stop_vendor_calls, 0)

    def test_multiple_field_points_execute_strictly_in_order(self):
        worker, controller, _, _, events = self.make_worker(targets=(-1.0, 0.0, 1.0))
        result = worker.run()
        self.assertEqual(result["status"], "COMPLETED")
        set_indices = [events.index(f"set.submit:{target}") for target in (-1.0, 0.0, 1.0)]
        acquire_indices = [i for i, value in enumerate(events) if value == "optical.acquire:45.0"]
        self.assertEqual(len(acquire_indices), 3)
        self.assertEqual([value for value in events if value == "stop.vendor"], [])
        self.assertTrue(set_indices[0] < acquire_indices[0] < set_indices[1])
        self.assertTrue(set_indices[1] < acquire_indices[1] < set_indices[2])
        self.assertTrue(set_indices[2] < acquire_indices[2])
        targets = [float(row[7]) for row in self.csv_rows(result)[1:]]
        self.assertEqual(targets, [-1.0, 0.0, 1.0])

    def test_optical_acquisition_begins_only_after_stability_window_and_hold(self):
        worker, controller, _, clock, events = self.make_worker(settling={
            "slope_window_s": 0.2, "stable_hold_s": 0.2,
            "polling_interval_s": 0.1, "settle_timeout_s": 1.0,
        })
        samples = iter([0.5, 0.99, 1.0, 1.0, 1.0, 1.0, 1.0])
        controller.snapshot_factory = lambda: snapshot(next(samples, 1.0))
        result = worker.run()
        self.assertEqual(result["status"], "COMPLETED")
        self.assertGreaterEqual(clock.value, 0.4)
        acquire_index = events.index("optical.acquire:45.0")
        self.assertGreaterEqual(events[:acquire_index].count("read.submit"), 5)

    def test_unstable_field_times_out_without_acquisition(self):
        worker, controller, optical, _, _ = self.make_worker()
        controller.snapshot_factory = lambda: snapshot(0.0)
        result = worker.run()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(optical.acquire_count, 0)
        self.assertIn("stabilize", result["error"])

    def test_missing_or_unsafe_required_telemetry_prevents_acquisition(self):
        cases = [
            lambda: snapshot(None),
            lambda: snapshot(1.0, temperature=None),
            lambda: snapshot(1.0, quench=True),
            lambda: snapshot(1.0, field_control=False),
            lambda: snapshot(1.0, field_control=None),
            lambda: snapshot(1.0, driven_mode=None),
            lambda: snapshot(1.0, persistent_mode=True),
        ]
        for factory in cases:
            with self.subTest(factory=factory):
                worker, controller, optical, _, _ = self.make_worker()
                controller.snapshot_factory = factory
                result = worker.run()
                self.assertEqual(result["status"], "FAILED")
                self.assertEqual(optical.acquire_count, 0)

    def test_mode_loss_fails_stops_and_does_not_schedule_next_target(self):
        worker, controller, optical, _, events = self.make_worker(
            targets=(1.0, 2.0), settling={"stable_hold_s": 0.2}
        )
        reads = {"count": 0}

        def mode_loss_snapshot():
            reads["count"] += 1
            driven = reads["count"] < 3
            return snapshot(controller.target, driven_mode=driven, persistent_mode=not driven)

        controller.snapshot_factory = mode_loss_snapshot
        result = worker.run()
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("Driven mode", result["error"])
        self.assertEqual(optical.acquire_count, 0)
        self.assertNotIn("set.submit:2.0", events)
        self.assertIn("stop.vendor", events)

    def test_cancel_during_stabilization_stops_drains_and_skips_later_points(self):
        worker, controller, optical, clock, events = self.make_worker(targets=(1.0, 2.0))
        controller.snapshot_factory = lambda: snapshot(0.0)
        clock.on_sleep = lambda: worker.request_cancel()
        result = worker.run()
        self.assertEqual(result["status"], "CANCELLED")
        self.assertNotIn("set.submit:2.0", events)
        self.assertEqual(optical.acquire_count, 0)
        self.assertIn("stop.result", events)
        self.assertIn("stop.drain", events)

    def test_cancel_during_acquisition_preserves_partial_data_and_cleans_up(self):
        worker, _, optical, _, events = self.make_worker(targets=(1.0, 2.0), angles=(0.0, 90.0))
        optical.on_acquire = lambda count: worker.request_cancel() if count == 2 else None
        result = worker.run()
        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(result["spectra_written"], 1)
        self.assertNotIn("set.submit:2.0", events)
        self.assertIn("stop.drain", events)
        self.assertIn("optical.cleanup", events)
        self.assertEqual(len(self.csv_rows(result)), 2)

    def test_field_command_failure_drains_stops_and_fails_without_acquisition(self):
        worker, controller, optical, _, events = self.make_worker(targets=(1.0, 2.0))
        controller.set_error = RuntimeError("set failed")
        result = worker.run()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(optical.acquire_count, 0)
        self.assertNotIn("set.submit:2.0", events)
        self.assertIn("set.drain", events)
        self.assertIn("stop.drain", events)
        self.assertIn("optical.cleanup", events)

    def test_optical_failure_preserves_partial_csv_stops_and_fails(self):
        worker, _, optical, _, events = self.make_worker(targets=(1.0, 2.0), angles=(0.0, 90.0))
        optical.fail_on = 2
        result = worker.run()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["spectra_written"], 1)
        self.assertNotIn("set.submit:2.0", events)
        self.assertIn("stop.drain", events)
        self.assertIn("optical.cleanup", events)
        self.assertEqual(self.metadata(result)["status"], "FAILED")

    def test_optical_cleanup_failure_stops_after_successful_points(self):
        worker, controller, optical, _, events = self.make_worker()

        def fail_cleanup():
            events.append("optical.cleanup")
            raise IOError("optical cleanup failed")

        optical.cleanup = fail_cleanup
        result = worker.run()
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("optical cleanup", result["cleanup_error"])
        self.assertIn("stop.vendor", events)

    def test_worker_uses_only_public_controller_api_and_never_adapter_or_vendor(self):
        worker, controller, _, _, events = self.make_worker()
        result = worker.run()
        self.assertEqual(result["status"], "COMPLETED")
        allowed_prefixes = ("set.", "start.", "read.", "stop.", "set.submit", "start.submit", "read.submit", "stop.request", "stop.vendor")
        self.assertTrue(all(event.startswith(allowed_prefixes) for event in events if not event.startswith("optical.")))
        with self.assertRaises(AssertionError):
            getattr(controller, "adapter")


if __name__ == "__main__":
    unittest.main()
