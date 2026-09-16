import concurrent.futures
import threading
import time
import unittest
from types import SimpleNamespace
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
        self.prepare_observe_only = []
        self.commit_setpoint_before_release = False

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

    def preflight_magnet(self, targets=(), stop_event=None):
        self._call("preflight", tuple(targets))
        if stop_event is not None and stop_event.is_set():
            raise AttoDRY2100StoppedError("stop requested")
        return SimpleNamespace(field_t=0.0)

    def read_ramp_tables(self, cancel_event=None):
        self._call("ramp_read")
        return SimpleNamespace(
            interrupted=bool(cancel_event is not None and cancel_event.is_set()),
            current=SimpleNamespace(rows=()), default=SimpleNamespace(rows=()),
        )

    def prepare_driven_mode(self, **kwargs):
        self.prepare_observe_only.append(bool(kwargs.get("observe_only")))
        callback = kwargs.get("on_mode_requested")
        allow_request = kwargs.get("allow_mode_request", True)
        if not kwargs.get("observe_only") and allow_request and callable(callback):
            callback()
        self._call("prepare_mode")
        if kwargs.get("stop_event") is not None and kwargs["stop_event"].is_set():
            raise AttoDRY2100StoppedError("stop requested")
        return SimpleNamespace(
            mode_requested=not kwargs.get("observe_only", False) and allow_request
        )

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
        if self.commit_setpoint_before_release:
            return self.setpoint
        if stop_event is not None and stop_event.is_set():
            raise AttoDRY2100StoppedError("stop requested")
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

    def test_prepare_runs_on_owner_with_extended_watchdog_and_no_stop_on_cancel(self):
        controller, adapter, recorder = self.make(request_timeout=.05)
        controller.config.mode_prepare_timeout_s = 1.0
        adapter.arm_gate("prepare_mode")
        handle = controller.prepare_driven_mode_async()
        self.assertTrue(adapter.entered.wait(1.0))
        self.pump()
        self.assertEqual(controller.state, ControllerState.RECOVERY_REQUIRED)
        controller.cancel_magnet_preparation()
        adapter.unblock()
        with self.assertRaises(Exception):
            handle.wait_drained(1.0)
        self.pump()
        self.assertNotIn("stop", [name for name, _, _ in adapter.calls])
        self.assertTrue(controller.mode_recovery_required)
        controller._lifecycle["mode_recovery_required"] = False

    def test_queued_preparation_cancel_clears_identity_and_allows_retry(self):
        controller, adapter, recorder = self.make(request_timeout=.2)
        adapter.arm_gate("read")
        reading = controller.read_snapshot_async()
        self.assertTrue(adapter.entered.wait(1.0))
        first = controller.prepare_driven_mode_async()
        controller.cancel_magnet_preparation()
        self.assertEqual(first.state, RequestState.CANCELLED)
        second = controller.prepare_driven_mode_async()
        self.assertNotEqual(second.request_id, first.request_id)
        adapter.unblock()
        self.wait_terminal(reading)
        controller.cancel_magnet_preparation()
        self.pump()
        self.assertNotIn("stop", [name for name, _, _ in adapter.calls])
        controller._lifecycle["mode_recovery_required"] = False

    def test_recovery_retry_is_observe_only(self):
        controller, adapter, recorder = self.make()
        controller._lifecycle["mode_recovery_required"] = True
        retry = controller.prepare_driven_mode_async()
        self.assertTrue(retry.result(.5).mode_requested is False)
        self.assertEqual(adapter.prepare_observe_only, [True])

    def test_preparation_rejects_pending_setpoint_and_timeout_still_stops_field(self):
        controller, adapter, recorder = self.make(request_timeout=.05)
        adapter.commit_setpoint_before_release = True
        adapter.arm_gate("set_mutation")
        setting = controller.set_h_setpoint_async(1.0)
        self.assertTrue(adapter.entered.wait(1.0))
        prep = controller.prepare_driven_mode_async()
        with self.assertRaises(AttoDRY2100StateError):
            prep.result(.2)
        with self.assertRaises(AttoDRY2100TimeoutError):
            setting.result(.15)
        adapter.unblock()
        self.assertEqual(setting.wait_drained(1.0), 1.0)
        stopping = controller.request_stop()
        self.wait_terminal(stopping)
        self.pump()
        names = [name for name, _, _ in adapter.calls]
        self.assertIn("set_mutation", names)
        self.assertIn("stop", names)

    def test_prepare_active_field_is_observe_only_and_preserves_active_state(self):
        controller, adapter, recorder = self.make()
        controller._owner.field_may_be_active = True
        controller._owner.state = ControllerState.ACTIVE
        controller._state = ControllerState.ACTIVE
        handle = controller.prepare_driven_mode_async()
        result = handle.result(.5)
        self.assertFalse(result.mode_requested)
        self.assertEqual(adapter.prepare_observe_only, [False])
        self.assertEqual(controller.state, ControllerState.ACTIVE)

    def test_immediate_post_prepare_preflight_uses_request_ownership_not_stale_state_signal(self):
        controller, adapter, recorder = self.make()
        prep = controller.prepare_driven_mode_async()
        self.assertTrue(prep.result(.5).mode_requested)
        # Deliberately do not pump queued state/request-terminal signals.
        self.assertEqual(controller.preflight_magnet_async(()).result(.5).field_t, 0.0)

    def test_stale_preparation_watchdog_cannot_cancel_new_preparation(self):
        controller, adapter, recorder = self.make(request_timeout=.05)
        adapter.arm_gate("read")
        reading = controller.read_snapshot_async()
        self.assertTrue(adapter.entered.wait(1.0))
        first = controller.prepare_driven_mode_async()
        controller.cancel_magnet_preparation()
        second = controller.prepare_driven_mode_async()
        controller._caller_timeout(first._request)
        self.assertFalse(controller._preparation_stop_event.is_set())
        adapter.unblock()
        self.wait_terminal(reading)
        controller.cancel_magnet_preparation()
        self.pump()
        self.assertNotEqual(first.request_id, second.request_id)

    def test_pending_preparation_blocks_mutation_even_if_cached_state_is_idle(self):
        controller, adapter, recorder = self.make()
        adapter.arm_gate("prepare_mode")
        prep = controller.prepare_driven_mode_async()
        self.assertTrue(adapter.entered.wait(1.0))
        controller._state = ControllerState.IDLE
        setting = controller.set_h_setpoint_async(.01)
        with self.assertRaises(AttoDRY2100StateError): setting.result(.2)
        controller.cancel_magnet_preparation()
        adapter.unblock()
        with self.assertRaises(Exception): prep.wait_drained(1.0)
        controller._lifecycle["mode_recovery_required"] = False

    def test_preparation_cannot_slip_between_mutation_admission_and_enqueue(self):
        controller, adapter, recorder = self.make()
        adapter.arm_gate("set_preflight")
        admitted = threading.Event()
        release = threading.Event()
        original_allowed = controller._ordinary_allowed

        def gated_allowed():
            allowed = original_allowed()
            admitted.set()
            self.assertTrue(release.wait(1.0))
            return allowed

        controller._ordinary_allowed = gated_allowed
        setting_box = []
        prep_box = []
        set_thread = threading.Thread(
            target=lambda: setting_box.append(controller.set_h_setpoint_async(.01))
        )
        prep_thread = threading.Thread(
            target=lambda: prep_box.append(controller.prepare_driven_mode_async())
        )
        set_thread.start()
        self.assertTrue(admitted.wait(1.0))
        prep_thread.start()
        self.assertTrue(prep_thread.is_alive())
        release.set()
        set_thread.join(1.0)
        prep_thread.join(1.0)
        controller._ordinary_allowed = original_allowed
        self.assertEqual(len(setting_box), 1)
        self.assertEqual(len(prep_box), 1)
        with self.assertRaises(AttoDRY2100StateError):
            prep_box[0].result(.5)
        self.assertTrue(adapter.entered.wait(1.0))
        adapter.unblock()
        self.assertEqual(setting_box[0].result(1.0), .01)

    def test_work_status_signal_follows_request_registry_drain(self):
        controller, adapter, recorder = self.make()
        observed = []
        controller.work_status_changed.connect(
            lambda: observed.append(controller.has_pending_work)
        )
        adapter.arm_gate("read")
        reading = controller.read_snapshot_async()
        self.assertTrue(adapter.entered.wait(1.0))
        adapter.unblock()
        self.assertEqual(reading.result(1.0)["field"], 0.0)
        deadline = time.monotonic() + 1.0
        while not observed and time.monotonic() < deadline:
            self.pump(.01)
        self.assertTrue(observed)
        self.assertFalse(observed[-1])

    def test_ramp_table_read_is_single_owner_and_blocks_mutations_and_duplicates(self):
        controller, adapter, recorder = self.make()
        adapter.arm_gate("ramp_read")
        reading = controller.read_ramp_tables_async()
        self.assertTrue(adapter.entered.wait(1.0))
        with self.assertRaises(AttoDRY2100StateError):
            controller.set_h_setpoint_async(.01).result(.5)
        with self.assertRaises(AttoDRY2100StateError):
            controller.read_ramp_tables_async().result(.5)
        adapter.unblock()
        result = reading.result(1.0)
        self.assertFalse(result.interrupted)
        reading.wait_drained(1.0)
        self.assertEqual(len({tid for name, _, tid in adapter.calls if name == "ramp_read"}), 1)
        self.assertNotIn("stop", [name for name, _, _ in adapter.calls])

    def test_ramp_table_timeout_cancels_read_without_stop_and_drains_partial_report(self):
        controller, adapter, recorder = self.make(request_timeout=.05)
        adapter.arm_gate("ramp_read")
        reading = controller.read_ramp_tables_async()
        self.assertTrue(adapter.entered.wait(1.0))
        with self.assertRaises(AttoDRY2100TimeoutError):
            reading.result(.15)
        self.assertTrue(controller.has_pending_work)
        with self.assertRaises(AttoDRY2100StateError):
            controller.set_h_setpoint_async(.01).result(.2)
        self.assertNotIn("stop", [name for name, _, _ in adapter.calls])
        adapter.unblock()
        drained = reading.wait_drained(1.0)
        self.assertTrue(drained.interrupted)
        self.pump()
        self.assertFalse(controller.has_pending_work)
        self.assertEqual(controller.state, ControllerState.IDLE)

    def test_ramp_table_read_rejects_recovery_and_shutdown_without_mutation(self):
        controller, adapter, recorder = self.make()
        controller._lifecycle["mode_recovery_required"] = True
        with self.assertRaises(AttoDRY2100StateError):
            controller.read_ramp_tables_async().result(.2)
        controller._lifecycle["mode_recovery_required"] = False
        adapter.arm_gate("ramp_read")
        reading = controller.read_ramp_tables_async()
        self.assertTrue(adapter.entered.wait(1.0))
        with self.assertRaises(AttoDRY2100StateError):
            controller.request_shutdown().result(.2)
        adapter.unblock()
        reading.result(1.0)

    def test_ramp_read_requires_connected_idle_or_active_owner_without_pending_work(self):
        controller, adapter, recorder = self.make(connect=False)
        with self.assertRaises(AttoDRY2100StateError):
            controller.read_ramp_tables_async().result(.2)
        controller.connect(timeout=.5)
        self.pump()
        adapter.arm_gate("set_preflight")
        setting = controller.set_h_setpoint_async(.01)
        self.assertTrue(adapter.entered.wait(1.0))
        with self.assertRaises(AttoDRY2100StateError):
            controller.read_ramp_tables_async().result(.2)
        adapter.unblock()
        self.assertEqual(setting.result(1.0), .01)

    def test_ramp_read_blocks_temperature_telemetry_submission_until_drain(self):
        controller, adapter, recorder = self.make()
        adapter.arm_gate("ramp_read")
        reading = controller.read_ramp_tables_async()
        self.assertTrue(adapter.entered.wait(1.0))
        with self.assertRaises(AttoDRY2100StateError):
            controller.read_temperature_snapshot_async().result(.2)
        adapter.unblock()
        reading.result(1.0)

    def test_recovery_blocks_mutations_but_allows_observation(self):
        controller, adapter, recorder = self.make()
        controller._lifecycle["mode_recovery_required"] = True
        rejected = controller.set_h_setpoint_async(.01)
        with self.assertRaises(AttoDRY2100StateError): rejected.result(.2)
        observed = controller.preflight_magnet_async(())
        self.assertEqual(observed.result(.5).field_t, 0.0)
        controller._lifecycle["mode_recovery_required"] = False

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

    def test_retargeted_active_field_allows_display_and_ramp_diagnostics(self):
        controller, adapter, _ = self.make()
        controller.set_polling_enabled(False)
        controller.set_h_setpoint_async(0.01).wait_drained(0.5)
        controller.start_field_control_async().wait_drained(0.5)
        controller.set_h_setpoint_async(0.02).wait_drained(0.5)
        self.pump()
        self.assertTrue(adapter.field_control)
        self.assertEqual(controller.read_display_snapshot_async(max_age_s=0).result(0.5), {"field": 0.0})
        controller.read_ramp_tables_async().result(0.5)

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

    def test_pending_work_snapshot_identifies_undrained_display_owner(self):
        controller, adapter, _recorder = self.make()
        adapter.arm_gate("read")
        controller.set_polling_enabled(True)
        deadline = time.monotonic() + 1.0
        while not adapter.entered.is_set() and time.monotonic() < deadline:
            self.pump(0.01)
        self.assertTrue(adapter.entered.is_set())
        snapshot = controller.pending_work_snapshot()
        self.assertTrue(snapshot.pending)
        self.assertTrue(snapshot.display_pending)
        self.assertFalse(snapshot.control_pending)
        self.assertEqual(len(snapshot.pending_request_ids), 1)
        self.assertEqual(snapshot.display_request_ids, snapshot.pending_request_ids)
        adapter.unblock()

    def test_pending_work_snapshot_marks_mixed_control_work_incompatible(self):
        controller, adapter, _recorder = self.make()
        adapter.arm_gate("read")
        controller.set_polling_enabled(True)
        deadline = time.monotonic() + 1.0
        while not adapter.entered.is_set() and time.monotonic() < deadline:
            self.pump(0.01)
        self.assertTrue(adapter.entered.is_set())
        control = controller.read_field_async()
        snapshot = controller.pending_work_snapshot()
        self.assertTrue(snapshot.display_pending)
        self.assertTrue(snapshot.control_pending)
        adapter.unblock()
        self.wait_terminal(control)

    def test_pending_snapshot_and_watchdog_complete_without_lock_inversion(self):
        controller, adapter, _recorder = self.make(request_timeout=5.0)
        adapter.arm_gate("read")
        handle = controller.read_display_snapshot_async(max_age_s=0, source="manual")
        self.assertTrue(adapter.entered.wait(1.0))
        request = handle._request
        snapshot_done = threading.Event()
        timeout_done = threading.Event()

        def snapshot_loop():
            for _ in range(100):
                controller.pending_work_snapshot()
            snapshot_done.set()

        def timeout_once():
            controller._caller_timeout(request)
            timeout_done.set()

        snapshot_thread = threading.Thread(target=snapshot_loop)
        timeout_thread = threading.Thread(target=timeout_once)
        snapshot_thread.start(); timeout_thread.start()
        snapshot_thread.join(1.0); timeout_thread.join(1.0)
        self.assertTrue(snapshot_done.is_set())
        self.assertTrue(timeout_done.is_set())
        adapter.unblock()
        handle.wait_drained(1.0)

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
