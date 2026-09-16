from __future__ import annotations

import concurrent.futures
import threading
import time
import unittest

from PySide6.QtWidgets import QApplication

from app.devices.attodry2100_adapter import AttoDRY2100TimeoutError
from controllers.attodry2100_controller import AttoDRY2100Controller
from controllers.attodry2100_controller import DisplayTelemetryUpdate
from utils.config import AttoDRY2100Config


class FakeClock:
    def __init__(self, value=0.0):
        self.value = float(value)
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def set(self, value):
        with self.lock:
            self.value = float(value)


class DisplayAdapter:
    def __init__(self, clock=None):
        self.clock = clock
        self.connected = False
        self.calls = []
        self.lock = threading.Lock()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.gate = None
        self.fail_read = False
        self.identity = "telemetry-fake"

    def connect(self):
        self.connected = True
        return self.identity

    def close(self):
        self.calls.append("close")
        self.connected = False

    def arm(self, name):
        self.gate = name
        self.entered.clear()
        self.release.clear()

    def unblock(self):
        self.release.set()

    def _wait(self, name):
        with self.lock:
            self.calls.append(name)
        if self.gate == name:
            self.entered.set()
            if not self.release.wait(2.0):
                raise RuntimeError("test gate was not released")

    def read_snapshot(self):
        self._wait("magnet")
        if self.fail_read:
            raise RuntimeError("magnet read failed")
        if self.clock is not None:
            self.clock.set(self.clock() + 3.0)
        return {"field": len([name for name in self.calls if name == "magnet"])}

    def read_temperature_snapshot(self):
        self._wait("temperature")
        return {"sample_temperature_k": 4.0}


class TelemetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.controllers = []

    def tearDown(self):
        for controller, adapter in reversed(self.controllers):
            adapter.unblock()
            if controller._thread.isRunning():
                controller.shutdown(1.0)
                self.app.processEvents()
            self.assertFalse(controller._thread.isRunning())

    def pump(self, seconds=.05):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.001)

    def make(self, *, clock=None, timeout=.5):
        adapter = DisplayAdapter(clock)
        controller = AttoDRY2100Controller(
            config=AttoDRY2100Config(
                sdk_directory="sdk", host="telemetry", channel=2,
                timeout_s=.5, poll_interval_s=.02,
            ),
            adapter_factory=lambda _config: adapter,
            request_timeout_s=timeout,
            shutdown_wait_s=.2,
            clock=clock,
        )
        self.controllers.append((controller, adapter))
        controller.connect(timeout=1.0)
        self.pump()
        return controller, adapter

    def test_request_diagnostics_separate_queue_and_sdk_execution_time(self):
        clock = FakeClock(10.0)
        controller, adapter = self.make(clock=clock)
        adapter.arm("temperature")
        prior = controller.read_temperature_snapshot_async()
        self.assertTrue(adapter.entered.wait(1.0))
        clock.set(12.0)
        current = controller.read_snapshot_async()
        clock.set(15.0)
        adapter.unblock()
        self.assertEqual(current.result(1.0)["field"], 1)
        self.pump()
        diagnostics = controller.telemetry_diagnostics()
        record = [item for item in diagnostics if item.group == "magnet"][-1]
        self.assertEqual(record.started_at - record.queued_at, 3.0)
        self.assertEqual(record.finished_at - record.started_at, 3.0)
        self.assertEqual(record.outcome, "succeeded")
        prior.result(1.0)

    def test_failed_read_keeps_exception_and_records_failed_diagnostic(self):
        controller, adapter = self.make()
        adapter.fail_read = True
        handle = controller.read_snapshot_async()
        with self.assertRaises(RuntimeError):
            handle.result(1.0)
        self.pump()
        record = [item for item in controller.telemetry_diagnostics() if item.group == "magnet"][-1]
        self.assertEqual(record.outcome, "failed")
        self.assertIsNotNone(record.started_at)
        self.assertIsNotNone(record.finished_at)

    def test_diagnostics_are_bounded_and_read_only(self):
        controller, adapter = self.make()
        for _ in range(257):
            controller.read_snapshot(timeout=1.0)
        self.pump()
        before = len(adapter.calls)
        diagnostics = controller.telemetry_diagnostics()
        self.assertEqual(len(diagnostics), 256)
        self.assertEqual(len(adapter.calls), before)

    def test_display_requests_coalesce_but_fresh_safety_read_is_separate(self):
        controller, adapter = self.make()
        adapter.arm("magnet")
        handles = [controller.read_display_snapshot_async() for _ in range(10)]
        self.assertTrue(adapter.entered.wait(1.0))
        fresh = controller.read_snapshot_async()
        adapter.unblock()
        results = [handle.result(1.0) for handle in handles]
        self.assertEqual(len([name for name in adapter.calls if name == "magnet"]), 2)
        self.assertEqual(results, [results[0]] * 10)
        fresh.result(1.0)

    def test_display_cache_freshness_boundaries_and_zero_age_bypass(self):
        clock = FakeClock(0.0)
        controller, adapter = self.make(clock=clock)
        first = controller.read_display_snapshot_async()
        first.result(1.0)
        self.pump()
        clock.set(first.completed_at + .49)
        controller.read_display_snapshot_async(max_age_s=.5).result(1.0)
        self.assertEqual(adapter.calls.count("magnet"), 1)
        clock.set(first.completed_at + .51)
        second = controller.read_display_snapshot_async(max_age_s=.5)
        second.result(1.0)
        self.assertEqual(adapter.calls.count("magnet"), 2)
        self.pump()
        clock.set(first.completed_at + .52)
        controller.read_display_snapshot_async(max_age_s=0).result(1.0)
        self.assertEqual(adapter.calls.count("magnet"), 3)
        records = [item for item in controller.telemetry_diagnostics() if item.group == "magnet"]
        self.assertIn("cache_hit", [item.disposition for item in records])
        self.assertIn("new", [item.disposition for item in records])

    def test_display_subscriber_timeout_does_not_cancel_shared_owner_request(self):
        controller, adapter = self.make(timeout=1.0)
        adapter.arm("magnet")
        first = controller.read_display_snapshot_async()
        self.assertTrue(adapter.entered.wait(1.0))
        second = controller.read_display_snapshot_async()
        with self.assertRaises(AttoDRY2100TimeoutError):
            second.result(.01)
        self.assertTrue(controller.has_pending_work)
        adapter.unblock()
        self.assertEqual(first.result(1.0)["field"], 1)
        self.assertEqual(second.wait_drained(1.0)["field"], 1)

    def test_display_subscriber_future_cancel_is_local(self):
        controller, adapter = self.make(timeout=1.0)
        adapter.arm("magnet")
        first = controller.read_display_snapshot_async()
        self.assertTrue(adapter.entered.wait(1.0))
        second = controller.read_display_snapshot_async()
        self.assertIsNot(first.future, second.future)
        self.assertTrue(second.future.cancel())
        self.assertTrue(controller.has_pending_work)
        adapter.unblock()
        self.assertEqual(first.result(1.0)["field"], 1)
        with self.assertRaises(concurrent.futures.CancelledError):
            second.result(0)
        self.assertEqual(second.wait_drained(1.0)["field"], 1)

    def test_drained_callback_is_safe_while_mailbox_lock_is_held(self):
        controller, adapter = self.make()
        handle = controller.read_display_snapshot_async(max_age_s=0)
        handle.result(1.0)
        request = handle._request
        # Future callbacks may run synchronously from set_result while a
        # producer owns this lock.  The diagnostics callback must not acquire
        # the controller lock in that context.
        with controller._mailbox.lock:
            controller._record_drained_callback(request)
        self.assertTrue(any(item.request_id == request.request_id
                            for item in controller.telemetry_diagnostics()))

    def test_display_cache_is_cleared_across_disconnect_and_reconnect(self):
        controller, adapter = self.make()
        controller.read_display_snapshot_async().result(1.0)
        self.pump()
        controller.disconnect_async().result(timeout=1.0)
        self.pump()
        controller.connect(timeout=1.0)
        self.pump()
        controller.read_display_snapshot_async().result(1.0)
        self.assertEqual(adapter.calls.count("magnet"), 2)

    def test_background_cycle_uses_display_broker_once_for_magnet_then_temperature(self):
        controller, adapter = self.make()
        adapter.arm("magnet")
        updates = []
        controller.display_snapshot_updated.connect(lambda value: updates.append(("magnet", value)))
        controller.display_temperature_updated.connect(lambda value: updates.append(("temperature", value)))
        controller.set_polling_enabled(True)
        controller._start_display_cycle()
        self.assertTrue(adapter.entered.wait(1.0))
        adapter.unblock()
        self.pump(.2)
        self.assertEqual(adapter.calls.count("magnet"), 1)
        self.assertEqual(adapter.calls.count("temperature"), 1)
        self.assertEqual([name for name, _ in updates], ["magnet", "temperature"])
        self.assertIsNone(controller._display_poll_cycle)
        controller.set_polling_enabled(False)
        sources = [item.source for item in controller.telemetry_diagnostics()
                   if item.group in {"magnet", "temperature"}]
        self.assertIn("background", sources)

    def test_background_timeout_drains_then_advances_to_temperature(self):
        controller, adapter = self.make(timeout=.05)
        adapter.arm("magnet")
        controller.set_polling_enabled(True)
        self.pump(.10)
        cycle = controller._display_poll_cycle
        self.assertIsNotNone(cycle)
        magnet = cycle["magnet"]
        self.assertEqual(magnet.state.name, "TIMED_OUT_DRAINING")
        adapter.unblock()
        self.assertEqual(magnet.wait_drained(1.0)["field"], 1)
        self.pump(.20)
        self.assertEqual(adapter.calls.count("temperature"), 1)
        self.assertIsNone(controller._display_poll_cycle)

    def test_display_update_preserves_owner_completed_timestamp_and_generation(self):
        controller, adapter = self.make()
        updates = []
        controller.display_snapshot_updated.connect(updates.append)
        controller.set_polling_enabled(True)
        self.pump(.20)
        controller.set_polling_enabled(False)
        self.assertTrue(updates)
        update = updates[-1]
        self.assertIsInstance(update, DisplayTelemetryUpdate)
        self.assertIsNotNone(update.completed_at)
        self.assertEqual(update.generation, controller.generation)

    def test_disconnect_invalidates_cache_advances_generation_and_stops_scheduler(self):
        controller, adapter = self.make()
        controller.read_display_snapshot_async(max_age_s=0).result(1.0)
        self.pump(.05)
        generation = controller.generation
        controller.set_polling_enabled(True)
        controller.disconnect_async().result(1.0)
        self.pump(.05)
        self.assertGreater(controller.generation, generation)
        self.assertFalse(controller._display_polling_enabled)
        self.assertFalse(controller._display_poll_timer.isActive())
        with self.assertRaises(Exception):
            controller.read_display_snapshot_async().result(0)


if __name__ == "__main__":
    unittest.main()
