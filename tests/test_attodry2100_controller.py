import concurrent.futures
import threading
import time
import unittest
from dataclasses import asdict

from PySide6.QtWidgets import QApplication

from app.devices.attodry2100_adapter import (
    AttoDRY2100StateError,
    AttoDRY2100StoppedError,
    AttoDRY2100TimeoutError,
)
from controllers.attodry2100_controller import (
    AttoDRY2100Controller,
    ControllerState,
    RequestState,
)
from utils.config import AttoDRY2100Config


class EventGatedAdapter:
    def __init__(self):
        self.calls = []
        self.thread_ids = []
        self.active_calls = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.block_name = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail_stop = False
        self.fail_close = False
        self.connected = False
        self.identity = "fake-2100"
        self.setpoint = 0.0
        self.field_control = False

    def arm_gate(self, name):
        self.block_name = name
        self.entered.clear()
        self.release.clear()

    def unblock(self):
        self.release.set()

    def _call(self, name, value=None):
        with self.lock:
            self.active_calls += 1
            self.max_active = max(self.max_active, self.active_calls)
            self.calls.append((name, value, threading.get_ident()))
            self.thread_ids.append(threading.get_ident())
        try:
            if self.block_name == name:
                self.entered.set()
                if not self.release.wait(5.0):
                    raise RuntimeError(f"test gate {name} was not released")
            return value
        finally:
            with self.lock:
                self.active_calls -= 1

    def connect(self):
        self._call("connect")
        self.connected = True
        return self.identity

    def close(self):
        self._call("close")
        if self.fail_close:
            raise RuntimeError("close failed")
        self.connected = False

    def read_snapshot(self):
        return self._call("read", {"field": 0.0})

    def read_field(self):
        return self._call("read_field", 0.125)

    def read_sample_temperature(self):
        return self._call("read_sample_temperature", 12.5)

    def read_temperature_snapshot(self):
        return self._call("read_temperature", {"sample_temperature_k": 12.5})

    def configure_sample_temperature(self, target, ramp_rate, stop_event=None):
        self._call("configure_temperature", (target, ramp_rate))
        if stop_event is not None and stop_event.is_set():
            raise AttoDRY2100StoppedError("stop requested")
        return {"target": target, "ramp_rate": ramp_rate}

    def stop_sample_temperature_control(self):
        return self._call("stop_temperature", True)

    def set_h_setpoint(self, target, stop_event=None):
        self._call("set_preflight")
        if stop_event is not None and stop_event.is_set():
            raise AttoDRY2100StoppedError("stop requested")
        self._call("set_mutation", target)
        self.setpoint = float(target)
        return self.setpoint

    def start_field_control(self, expected_target, stop_event=None):
        self._call("start_preflight", expected_target)
        if stop_event is not None and stop_event.is_set():
            raise AttoDRY2100StoppedError("stop requested")
        self._call("start_mutation", expected_target)
        self.field_control = True
        return True

    def stop_field_control(self):
        self._call("stop")
        if self.fail_stop:
            raise RuntimeError("stop failed")
        self.field_control = False
        return True

    def verify_continuous_completion(self, target, gate):
        self._call("verify_completion", target)
        return True

    def verify_continuous_completion_snapshot(self, snapshot, target, gate):
        self._call("verify_completion_snapshot", target)
        return snapshot


class FactoryRecorder:
    def __init__(self, adapter=None, failure=None):
        self.adapter = adapter or EventGatedAdapter()
        self.failure = failure
        self.calls = []

    def __call__(self, config):
        self.calls.append((asdict(config), threading.get_ident()))
        if self.failure is not None:
            raise self.failure
        return self.adapter


class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.controllers = []

    def tearDown(self):
        for controller, adapter in reversed(self.controllers):
            adapter.unblock()
            adapter.fail_stop = False
            adapter.fail_close = False
            if controller._thread.isRunning():
                controller.shutdown(1.0)
                self.pump(0.05)
            self.assertFalse(
                controller._thread.isRunning(),
                "controller owner thread leaked from a test",
            )

    def pump(self, seconds=0.05):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.001)

    def config(self):
        return AttoDRY2100Config(
            sdk_directory="sdk-dir",
            host="test-host",
            channel=3,
            timeout_s=0.4,
            maximum_field_t=9.0,
            minimum_temperature_k=1.0,
            maximum_temperature_k=6.0,
            poll_interval_s=0.02,
        )

    def make(self, *, request_timeout=0.4, connect=True, recorder=None):
        recorder = recorder or FactoryRecorder()
        controller = AttoDRY2100Controller(
            config=self.config(),
            adapter_factory=recorder,
            request_timeout_s=request_timeout,
            shutdown_wait_s=0.1,
        )
        self.controllers.append((controller, recorder.adapter))
        if connect:
            controller.connect(timeout=0.5)
            self.pump()
        return controller, recorder.adapter, recorder

    def wait_terminal(self, handle, timeout=1.0):
        try:
            return handle.wait_drained(timeout)
        finally:
            self.pump()

    def test_factory_once_on_owner_thread_with_complete_config(self):
        controller, adapter, recorder = self.make()
        self.assertEqual(len(recorder.calls), 1)
        received, thread_id = recorder.calls[0]
        self.assertEqual(received, asdict(self.config()))
        self.assertNotEqual(thread_id, threading.get_ident())
        self.assertEqual({tid for _, _, tid in adapter.calls}, {thread_id})
        self.assertEqual(controller.state, ControllerState.IDLE)

    def test_internal_factory_typeerror_is_not_retried(self):
        recorder = FactoryRecorder(failure=TypeError("factory body failed"))
        controller, adapter, recorder = self.make(connect=False, recorder=recorder)
        with self.assertRaises(TypeError):
            controller.connect(timeout=0.5)
        self.pump()
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(controller.state, ControllerState.DISCONNECTED)

    def test_all_vendor_calls_are_serial_on_one_owner_thread(self):
        controller, adapter, recorder = self.make()
        adapter.arm_gate("read")
        first = controller.read_snapshot_async()
        self.assertTrue(adapter.entered.wait(1.0))
        second = controller.read_snapshot_async()
        adapter.unblock()
        self.wait_terminal(first)
        self.wait_terminal(second)
        self.assertEqual(adapter.max_active, 1)
        self.assertEqual(len(set(adapter.thread_ids)), 1)
        self.assertNotEqual(adapter.thread_ids[0], threading.get_ident())

    def test_field_only_read_runs_on_owner_and_returns_scalar(self):
        controller, adapter, recorder = self.make()

        value = controller.read_field_async().result(0.5)

        self.assertEqual(value, 0.125)
        calls = [item for item in adapter.calls if item[0] == "read_field"]
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0][2], threading.get_ident())

    def test_two_simultaneous_stops_coalesce_to_one_vendor_call(self):
        controller, adapter, recorder = self.make()
        adapter.arm_gate("read")
        read = controller.read_snapshot_async()
        self.assertTrue(adapter.entered.wait(1.0))
        results = []
        barrier = threading.Barrier(3)

        def submit_stop():
            barrier.wait()
            results.append(controller.request_stop())

        threads = [threading.Thread(target=submit_stop) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(1.0)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].request_id, results[1].request_id)
        self.assertTrue(controller.stop_event.is_set())
        adapter.unblock()
        self.wait_terminal(read)
        self.wait_terminal(results[0])
        self.assertEqual([name for name, _, _ in adapter.calls].count("stop"), 1)

    def test_stop_during_set_preflight_prevents_mutation(self):
        controller, adapter, recorder = self.make()
        adapter.arm_gate("set_preflight")
        setting = controller.set_h_setpoint_async(1.0)
        self.assertTrue(adapter.entered.wait(1.0))
        stopping = controller.request_stop()
        self.assertTrue(controller.stop_event.is_set())
        adapter.unblock()
        with self.assertRaises(AttoDRY2100StoppedError):
            setting.wait_drained(1.0)
        self.wait_terminal(stopping)
        names = [name for name, _, _ in adapter.calls]
        self.assertNotIn("set_mutation", names)
        self.assertEqual(names.count("stop"), 1)

    def test_queued_timeout_cancels_later_mutation(self):
        controller, adapter, recorder = self.make(request_timeout=0.05)
        adapter.arm_gate("read")
        reading = controller.read_snapshot_async()
        self.assertTrue(adapter.entered.wait(1.0))
        setting = controller.set_h_setpoint_async(2.0)
        with self.assertRaises(AttoDRY2100TimeoutError):
            setting.result(0.15)
        self.assertEqual(setting.state, RequestState.CANCELLED)
        adapter.unblock()
        self.wait_terminal(reading)
        self.pump()
        self.assertNotIn("set_mutation", [name for name, _, _ in adapter.calls])

    def test_running_timeout_remains_tracked_until_terminal(self):
        controller, adapter, recorder = self.make(request_timeout=0.05)
        adapter.arm_gate("read")
        reading = controller.read_snapshot_async()
        self.assertTrue(adapter.entered.wait(1.0))
        with self.assertRaises(AttoDRY2100TimeoutError):
            reading.result(0.15)
        self.assertEqual(reading.state, RequestState.TIMED_OUT_DRAINING)
        self.assertTrue(controller.has_pending_work)
        rejected = controller.set_h_setpoint_async(1.0)
        with self.assertRaises(AttoDRY2100StateError):
            rejected.result(0.1)
        adapter.unblock()
        self.wait_terminal(reading)
        self.pump()
        self.assertFalse(controller.has_pending_work)

    def test_running_mutation_timeout_cannot_mutate_after_preflight(self):
        controller, adapter, recorder = self.make(request_timeout=0.05)
        adapter.arm_gate("set_preflight")
        setting = controller.set_h_setpoint_async(2.5)
        self.assertTrue(adapter.entered.wait(1.0))
        with self.assertRaises(AttoDRY2100TimeoutError):
            setting.result(0.15)
        self.assertTrue(controller.stop_event.is_set())
        adapter.unblock()
        with self.assertRaises(AttoDRY2100StoppedError):
            setting.wait_drained(1.0)
        self.pump()
        self.assertNotIn("set_mutation", [name for name, _, _ in adapter.calls])

    def test_stale_timed_out_request_cannot_mutate_after_reconnect(self):
        controller, adapter, recorder = self.make(request_timeout=0.05)
        adapter.arm_gate("read")
        reading = controller.read_snapshot_async()
        self.assertTrue(adapter.entered.wait(1.0))
        setting = controller.set_h_setpoint_async(3.0)
        with self.assertRaises(AttoDRY2100TimeoutError):
            setting.result(0.15)
        disconnect = controller.disconnect_async()
        with self.assertRaises(AttoDRY2100StateError):
            disconnect.result(0.1)
        adapter.unblock()
        self.wait_terminal(reading)
        self.pump()
        controller.disconnect_async().result(0.5)
        self.pump()
        controller.connect_async().result(0.5)
        self.pump()
        self.assertNotIn("set_mutation", [name for name, _, _ in adapter.calls])
        self.assertEqual(len(recorder.calls), 2)

    def test_start_requires_verified_setpoint(self):
        controller, adapter, recorder = self.make()
        with self.assertRaises(AttoDRY2100StateError):
            controller.start_field_control_async().result(0.5)
        controller.set_h_setpoint_async(1.25).result(0.5)
        controller.start_field_control_async().result(0.5)
        self.pump()
        names = [name for name, _, _ in adapter.calls]
        self.assertLess(names.index("set_mutation"), names.index("start_mutation"))
        self.assertEqual(controller.state, ControllerState.ACTIVE)

    def test_successive_setpoint_and_start_are_allowed_while_active(self):
        controller, adapter, recorder = self.make()
        controller.set_h_setpoint_async(0.01).result(0.5)
        controller.start_field_control_async().result(0.5)
        controller.set_h_setpoint_async(0.02).result(0.5)
        controller.start_field_control_async().result(0.5)
        self.pump()
        names = [name for name, _, _ in adapter.calls]
        self.assertEqual(names.count("stop"), 0)
        self.assertEqual(names.count("set_mutation"), 2)
        self.assertEqual(names.count("start_mutation"), 2)
        self.assertTrue(controller._owner.field_may_be_active)
        self.assertEqual(controller.state, ControllerState.ACTIVE)

    def test_temperature_operations_use_the_existing_owner_and_connection(self):
        controller, adapter, recorder = self.make()
        configured = controller.configure_sample_temperature_async(20.0, 2.0)
        self.assertEqual(configured.result(.5)["target"], 20.0)
        self.assertEqual(controller.read_sample_temperature_async().result(.5), 12.5)
        self.assertEqual(
            controller.read_temperature_snapshot_async().result(.5)["sample_temperature_k"],
            12.5,
        )
        self.assertTrue(controller.stop_sample_temperature_control_async().result(.5))
        self.assertEqual(len(recorder.calls), 1)
        temperature_threads = {
            tid for name, _, tid in adapter.calls if "temperature" in name
        }
        self.assertEqual(len(temperature_threads), 1)

    def test_temperature_timeout_cancels_temperature_without_magnet_stop(self):
        controller, adapter, recorder = self.make(request_timeout=0.05)
        adapter.arm_gate("configure_temperature")
        configuring = controller.configure_sample_temperature_async(20.0, 2.0)
        self.assertTrue(adapter.entered.wait(1.0))
        with self.assertRaises(AttoDRY2100TimeoutError):
            configuring.result(0.15)
        self.assertFalse(controller.stop_event.is_set())
        adapter.unblock()
        with self.assertRaises(AttoDRY2100StoppedError):
            configuring.wait_drained(1.0)
        self.assertNotIn("stop", [name for name, _, _ in adapter.calls])

    def test_completed_detach_verifies_once_closes_without_stop_and_reconnects(self):
        controller, adapter, recorder = self.make()
        controller.set_h_setpoint_async(.01).result(.5)
        controller.start_field_control_async().result(.5)
        self.pump()
        handle = controller.detach_completed_run_async(.01, .001)
        self.assertTrue(handle.result(1.0))
        deadline = time.monotonic() + 1.0
        while not handle._request.drained_future.done() and time.monotonic() < deadline:
            self.pump(.01)
        handle.wait_drained(0.1)
        self.assertFalse(controller._thread.isRunning())
        names = [name for name, _, _ in adapter.calls]
        self.assertEqual(names.count("verify_completion"), 1)
        self.assertNotIn("stop", names)
        self.assertIn("close", names)
        self.assertEqual(controller.state, ControllerState.DISCONNECTED)
        controller.connect_async().result(1.0)
        self.assertEqual(len(recorder.calls), 2)
        controller.set_h_setpoint_async(.02).result(1.0)
        controller.start_field_control_async().result(1.0)
        self.pump()
        second = controller.detach_completed_run_async(.02, .001)
        self.assertTrue(second.result(1.0))
        deadline = time.monotonic() + 1.0
        while not second._request.drained_future.done() and time.monotonic() < deadline:
            self.pump(.01)
        second.wait_drained(.1)
        names = [name for name, _, _ in adapter.calls]
        self.assertEqual(names.count("verify_completion"), 2)
        self.assertEqual(names.count("close"), 2)
        self.assertNotIn("stop", names)

    def test_completed_detach_uses_endpoint_snapshot_without_second_field_verification(self):
        controller, adapter, _ = self.make()
        controller.set_h_setpoint_async(.01).result(.5)
        controller.start_field_control_async().result(.5)
        self.pump()
        endpoint_snapshot = object()
        handle = controller.detach_completed_run_async(.01, .001, endpoint_snapshot)
        self.assertTrue(handle.result(1.0))
        deadline = time.monotonic() + 1.0
        while not handle._request.drained_future.done() and time.monotonic() < deadline:
            self.pump(.01)
        handle.wait_drained(.1)
        names = [name for name, _, _ in adapter.calls]
        self.assertEqual(names.count("verify_completion_snapshot"), 1)
        self.assertEqual(names.count("verify_completion"), 0)
        self.assertNotIn("stop", names)
        self.assertIn("close", names)

    def test_detach_rejects_pending_work_and_close_failure_keeps_stop_recovery(self):
        controller, adapter, recorder = self.make()
        controller.set_h_setpoint_async(.01).result(.5)
        controller.start_field_control_async().result(.5)
        adapter.arm_gate("read")
        reading = controller.read_snapshot_async()
        self.assertTrue(adapter.entered.wait(1.0))
        with self.assertRaises(AttoDRY2100StateError):
            controller.detach_completed_run_async(.01, .001).result(.2)
        adapter.unblock()
        reading.result(1.0)
        adapter.fail_close = True
        failed = controller.detach_completed_run_async(.01, .001)
        with self.assertRaises(Exception):
            failed.result(1.0)
        adapter.fail_close = False
        stopped = controller.request_stop()
        self.assertTrue(stopped.result(1.0))
        self.assertEqual([name for name, _, _ in adapter.calls].count("stop"), 1)

    def test_cancel_before_detach_commit_prevents_close_and_drains_stop(self):
        controller, adapter, recorder = self.make()
        controller.set_h_setpoint_async(.01).result(.5)
        controller.start_field_control_async().result(.5)
        self.pump()
        adapter.arm_gate("verify_completion")
        detach = controller.detach_completed_run_async(.01, .001)
        self.assertTrue(adapter.entered.wait(1.0))
        stop = controller.request_stop()
        adapter.unblock()
        with self.assertRaises(Exception): detach.result(1.0)
        self.assertTrue(stop.result(1.0))
        self.assertNotIn("close", [name for name, _, _ in adapter.calls])
        self.assertEqual([name for name, _, _ in adapter.calls].count("stop"), 1)

    def test_active_shutdown_waits_for_running_vendor_call(self):
        controller, adapter, recorder = self.make(request_timeout=1.0)
        controller.set_h_setpoint_async(1.0).result(0.5)
        controller.start_field_control_async().result(0.5)
        adapter.arm_gate("read")
        reading = controller.read_snapshot_async()
        self.assertTrue(adapter.entered.wait(1.0))
        started = time.monotonic()
        self.assertFalse(controller.shutdown(0.05))
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertTrue(controller._thread.isRunning())
        self.assertNotIn("close", [name for name, _, _ in adapter.calls])
        adapter.unblock()
        self.wait_terminal(reading)
        deadline = time.monotonic() + 1.0
        while controller._thread.isRunning() and time.monotonic() < deadline:
            self.pump(0.01)
        self.assertFalse(controller._thread.isRunning())
        names = [name for name, _, _ in adapter.calls]
        self.assertLess(names.index("stop"), names.index("close"))

    def test_polling_uses_owner_thread_and_never_overlaps(self):
        controller, adapter, recorder = self.make()
        controller.set_polling_enabled(True)
        deadline = time.monotonic() + 0.5
        while not any(name == "read" for name, _, _ in adapter.calls) and time.monotonic() < deadline:
            self.pump(0.01)
        controller.set_polling_enabled(False)
        self.pump()
        reads = [tid for name, _, tid in adapter.calls if name == "read"]
        self.assertTrue(reads)
        self.assertEqual(len(set(reads)), 1)
        self.assertEqual(adapter.max_active, 1)

    def test_stop_and_close_failures_keep_owner_retryable(self):
        controller, adapter, recorder = self.make()
        controller.set_h_setpoint_async(1.0).result(0.5)
        controller.start_field_control_async().result(0.5)
        adapter.fail_stop = True
        self.assertFalse(controller.shutdown(0.2))
        self.assertTrue(controller._thread.isRunning())
        self.assertNotIn("close", [name for name, _, _ in adapter.calls])
        adapter.fail_stop = False
        self.assertTrue(controller.shutdown(0.5))

    def test_close_failure_keeps_same_owner_and_retries(self):
        controller, adapter, recorder = self.make()
        adapter.fail_close = True
        self.assertFalse(controller.shutdown(0.2))
        self.assertTrue(controller._thread.isRunning())
        owner_threads = set(adapter.thread_ids)
        adapter.fail_close = False
        self.assertTrue(controller.shutdown(0.5))
        self.assertEqual(owner_threads, set(adapter.thread_ids))


if __name__ == "__main__":
    unittest.main()
