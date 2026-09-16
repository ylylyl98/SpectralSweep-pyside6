import unittest
import threading
from types import SimpleNamespace

from app.engine.magnet_preparation import MagnetPreparation, MagnetPreparationWorker, format_preparation_progress


def snap(field, *, active=False, setpoint=0.0):
    return SimpleNamespace(field_t=field, setpoint_t=setpoint, temperature_k=4.0,
        status=SimpleNamespace(quench=False, driven_mode=True, persistent_mode=False,
            backend_details={"field_control": active}))


class Handle:
    def __init__(self, events, name, value): self.events, self.name, self.value = events, name, value
    def result(self, timeout=None): self.events.append(self.name + ".result"); return self.value
    def wait_drained(self, timeout=None): self.events.append(self.name + ".drain"); return self.value


class Controller:
    mode_recovery_required = False
    def __init__(self):
        self.events = []; self.reads = [snap(0, active=False), snap(0, active=False),
                                        snap(.01, active=True, setpoint=.01)] + [snap(.01, active=True, setpoint=.01)] * 5
    def _h(self, name, value=True): self.events.append(name); return Handle(self.events, name, value)
    def preflight_magnet_async(self, targets=()): return self._h("preflight", self.reads.pop(0))
    def prepare_driven_mode_async(self): return self._h("prepare_driven", True)
    def set_h_setpoint_async(self, target): return self._h("set:.01", .01)
    def start_field_control_async(self): return self._h("start")
    def read_snapshot_async(self): return self._h("read", self.reads.pop(0))
    def request_stop(self): return self._h("stop")
    def cancel_magnet_preparation(self): self.events.append("cancel_prepare")


class MagnetPreparationTests(unittest.TestCase):
    def test_mode_progress_reports_observed_mode_without_position_confirmation_count(self):
        progress = []
        MagnetPreparation(Controller(), .01, preparation_progress=progress.append,
                          clock=lambda: 0., sleep=lambda _: None).prepare()
        mode = next(event for event in progress if event["stage"] == "mode readiness")
        rendered = format_preparation_progress(mode, 20.)
        self.assertNotIn("stable=", rendered)
        self.assertIn("Driven=yes", rendered)
        self.assertIn("Persistent=no", rendered)
        self.assertIn("Heater=unknown", rendered)
        self.assertIn("Leads hot=unknown", rendered)

    def test_slow_ramp_and_confirmation_have_independent_position_budget(self):
        clock = VirtualClock()
        controller = SlowController(clock)
        events, logs = [], []
        result = MagnetPreparation(controller, -2, timeout_s=300,
            position_timeout_s=1800, clock=clock, sleep=clock.sleep,
            preparation_progress=events.append, log=logs.append).prepare()
        self.assertEqual(result.field_t, -2)
        self.assertGreater(clock.now - 120, 300)
        self.assertEqual(events[-1]["stable_count"], 5)
        self.assertEqual(events[-1]["stage"], "ready")
        baseline_clock = VirtualClock()
        baseline = SlowController(baseline_clock)
        MagnetPreparation(baseline, -2, timeout_s=300, position_timeout_s=1800,
                          clock=baseline_clock, sleep=baseline_clock.sleep).prepare()
        self.assertEqual(controller.events, baseline.events)
        self.assertTrue(any("stable=5/5" in line for line in logs))
        self.assertLess(len(logs), len(events))

    def test_late_fifth_sample_cannot_succeed_and_timeout_reports_evidence(self):
        clock = VirtualClock()
        controller = SlowController(clock, ramp_s=0)
        result = MagnetPreparationWorker(controller, -2, timeout_s=300,
            position_timeout_s=55, clock=clock, sleep=clock.sleep).run()
        self.assertEqual(result["status"], "FAILED")
        for evidence in ("reach Start", "field=-2", "target=-2", "age=3.0", "stable=4/5"):
            self.assertIn(evidence, result["error"])
        self.assertIn("stop", controller.events)
        self.assertLess(controller.events.index("preflight.drain", len(controller.events)-5),
                        controller.events.index("stop"))

    def test_position_budget_rejects_non_finite_or_non_positive_before_work(self):
        for value in (0, -1, float("inf"), float("nan")):
            controller = Controller()
            with self.subTest(value=value), self.assertRaises(ValueError):
                MagnetPreparation(controller, .01, position_timeout_s=value)
            self.assertEqual(controller.events, [])

    def test_expired_fresh_preflight_does_not_submit_a_field_command(self):
        clock = VirtualClock()
        controller = SlowController(clock)
        result = MagnetPreparationWorker(controller, -2, position_timeout_s=5,
            clock=clock, sleep=clock.sleep).run()
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("reach Start", result["error"])
        self.assertNotIn("set:.01", controller.events)
        self.assertNotIn("start", controller.events)
        self.assertNotIn("stop", controller.events)

    def test_expiry_during_phase_callback_prevents_late_target_submission(self):
        clock = VirtualClock()
        controller = SlowController(clock)
        def phase(message):
            if message.startswith("Moving to Start"):
                clock.sleep(1800)
        result = MagnetPreparationWorker(controller, -2, position_timeout_s=1800,
            clock=clock, sleep=clock.sleep, phase=phase).run()
        self.assertEqual(result["status"], "FAILED")
        self.assertNotIn("set:.01", controller.events)

    def test_cancel_during_position_read_drains_then_uses_existing_stop_cleanup(self):
        clock, stopped = VirtualClock(), threading.Event()
        class CancelRead(SlowController):
            def preflight_magnet_async(self, targets=()):
                handle = super().preflight_magnet_async(targets)
                original = handle.result
                def result(timeout=None):
                    value = original(timeout)
                    if self.moving_at is not None: stopped.set()
                    return value
                handle.result = result
                return handle
        controller = CancelRead(clock)
        progress = []
        result = MagnetPreparationWorker(controller, -2, position_timeout_s=1800,
            clock=clock, sleep=clock.sleep, stop_event=stopped,
            preparation_progress=progress.append).run()
        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(controller.events[-4:], ["preflight.drain", "stop", "stop.result", "stop.drain"])
        self.assertNotIn("ready", [event["stage"] for event in progress])

    def test_sequence_drains_each_owner_operation_and_requires_stable_start(self):
        controller = Controller()
        prep = MagnetPreparation(controller, .01, timeout_s=1, poll_interval_s=.001,
                                  sleep=lambda _: None, clock=lambda: 0.0)
        result = prep.prepare()
        self.assertEqual(result.field_t, .01)
        self.assertLess(controller.events.index("preflight.drain"), controller.events.index("prepare_driven"))
        self.assertLess(controller.events.index("prepare_driven.drain"), controller.events.index("preflight.drain", controller.events.index("prepare_driven.drain")))
        self.assertGreaterEqual(controller.events.count("preflight"), 7)

    def test_standalone_result_is_operation_envelope(self):
        controller = Controller()
        result = MagnetPreparationWorker(controller, .01, timeout_s=1, poll_interval_s=.001,
                                         sleep=lambda _: None, clock=lambda: 0.0).run()
        self.assertEqual(result["operation"], "magnet_preparation")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertNotIn("csv_path", result)

    def test_cancelled_before_prepare_does_not_submit_preflight(self):
        controller = Controller()
        prep = MagnetPreparation(controller, .01, stop_event=threading.Event())
        prep.request_cancel()
        with self.assertRaises(Exception):
            prep.prepare()
        self.assertEqual(controller.events, ["cancel_prepare"])

    def test_inactive_field_is_positioned_then_requires_five_safe_samples(self):
        class InactiveController(Controller):
            def __init__(self):
                super().__init__()
                self.preflight_count = 0
            def preflight_magnet_async(self, targets=()):
                self.preflight_count += 1
                if self.preflight_count <= 2:
                    value = snap(0, active=False, setpoint=0.0)
                else:
                    value = snap(.01, active=True, setpoint=.01)
                return self._h("preflight", value)
            def read_snapshot_async(self):
                return self._h("read", self.reads.pop(0) if self.reads else snap(.01, active=True, setpoint=.01))

        controller = InactiveController()
        result = MagnetPreparation(controller, .01, timeout_s=1,
                                   poll_interval_s=.001, sleep=lambda _: None,
                                   clock=lambda: 0.0).prepare()
        self.assertEqual(result.field_t, .01)
        self.assertIn("set:.01", controller.events)
        self.assertIn("start", controller.events)
        self.assertGreaterEqual(controller.preflight_count, 7)

    def test_position_timeout_is_distinct_from_mode_operation_budget(self):
        class Clock:
            def __init__(self): self.now = 0.0
            def __call__(self): return self.now
            def sleep(self, seconds): self.now += seconds
        class NeverReady(Controller):
            def read_snapshot_async(self):
                return self._h("read", snap(0.0, active=True, setpoint=.01))
        clock = Clock()
        with self.assertRaises(Exception) as ctx:
            MagnetPreparation(NeverReady(), .01, timeout_s=.01,
                               operation_timeout_s=10.0, poll_interval_s=.01,
                               sleep=clock.sleep, clock=clock).prepare()
        self.assertIn("reach Start", str(ctx.exception))

    def test_cancelled_cleanup_failure_is_reported_as_failed(self):
        class StopFailure(Controller):
            def request_stop(self):
                raise RuntimeError("Stop failed")
        controller = StopFailure()
        worker = MagnetPreparationWorker(controller, .01, timeout_s=1,
                                         poll_interval_s=.001,
                                         sleep=lambda _: None, clock=lambda: 0.0)
        worker.preparation.field_command_issued = True
        worker.preparation.request_cancel()
        result = worker.run()
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("Stop failed", result["cleanup_error"])


class VirtualClock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now
    def sleep(self, seconds): self.now += seconds


class SlowController(Controller):
    def __init__(self, clock, ramp_s=284):
        super().__init__()
        self.clock, self.ramp_s = clock, ramp_s
        self.moving_at = None

    def preflight_magnet_async(self, targets=()):
        owner = self
        class Read(Handle):
            def result(self, timeout=None):
                owner.clock.sleep(10)
                field = 0 if owner.moving_at is None else -2 * min(
                    1, (owner.clock.now - owner.moving_at) / max(.001, owner.ramp_s))
                self.value = snap(field, active=owner.moving_at is not None, setpoint=-2)
                self.value.monotonic_s = owner.clock.now - 3
                return super().result(timeout)
        self.events.append("preflight")
        return Read(self.events, "preflight", None)

    def prepare_driven_mode_async(self):
        self.clock.sleep(110)
        return self._h("prepare_driven", True)

    def start_field_control_async(self):
        self.moving_at = self.clock.now
        return self._h("start")


if __name__ == "__main__": unittest.main()
