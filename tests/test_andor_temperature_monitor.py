import os
import threading
import time
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from controllers.lf6_controller import LF6Controller, LightFieldLifecycleState
from ui.instrument_panel import _LF6Section


class TemperatureMonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.release = threading.Event()
        self.release.set()
        self.reads = 0
        self.fail_read = False
        self.controller = LF6Controller()
        self.controller._worker._adapter = SimpleNamespace(acquire=lambda: ([1], [2]))
        self.controller._worker._setup = SimpleNamespace(
            get_temperature_snapshot=self.read, is_busy=False)
        self.controller._worker._identity = {"backend": "andor_sdk2"}
        self.controller._worker._state = LightFieldLifecycleState.READY
        self.results = []
        signal = getattr(self.controller, "temperature_snapshot_ready", None)
        if signal is not None:
            signal.connect(self.results.append)

    def read(self):
        self.reads += 1
        self.release.wait(2)
        if self.fail_read:
            raise RuntimeError("sensor unavailable")
        return dict(temperature_c=-65.0, temperature_setpoint_c=-70.0,
                    temperature_status="not_stabilized", cooler_on=True)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail("Timed out waiting for monitor")

    def tearDown(self):
        self.release.set()
        self.controller._worker._setup = None
        self.controller._worker._adapter = None
        self.controller.shutdown()

    def test_idle_monitor_refreshes_without_manual_click(self):
        self.assertTrue(hasattr(self.controller, "_temperature_timer"))
        self.controller._temperature_timer.setInterval(10)
        self.wait_for(lambda: len(self.results) >= 2)
        self.assertEqual(self.results[-1]["temperature_c"], -65)

    def test_slow_reads_do_not_accumulate_and_pause_sources_are_independent(self):
        self.assertTrue(hasattr(self.controller, "poll_temperature"))
        self.release.clear()
        self.controller.poll_temperature()
        self.wait_for(lambda: self.reads == 1)
        for _ in range(10):
            self.controller.poll_temperature()
        self.controller.set_temperature_monitor_paused("sweep", True)
        self.controller.set_temperature_monitor_paused("spectrum", True)
        self.release.set()
        self.wait_for(lambda: not self.controller._temperature_pending)
        self.controller.set_temperature_monitor_paused("spectrum", False)
        self.controller.poll_temperature()
        self.assertEqual(self.reads, 1)
        self.controller.set_temperature_monitor_paused("sweep", False)
        self.controller.poll_temperature()
        self.wait_for(lambda: self.reads == 2)

    def test_busy_and_disconnected_do_not_read(self):
        self.assertTrue(hasattr(self.controller, "poll_temperature"))
        self.controller._worker._setup.is_busy = True
        self.controller.poll_temperature()
        self.controller._worker._setup.is_busy = False
        self.controller._worker._adapter = None
        self.controller.poll_temperature()
        self.app.processEvents()
        self.assertEqual(self.reads, 0)

    def test_monitor_error_is_separate_from_acquisition_error_and_recovers(self):
        self.assertTrue(hasattr(self.controller, "poll_temperature"))
        errors = []
        self.controller.error.connect(errors.append)
        self.fail_read = True
        self.controller.poll_temperature()
        self.wait_for(lambda: bool(self.results))
        self.assertIn("sensor unavailable", self.results[-1]["error"])
        self.assertEqual(errors, [])
        self.fail_read = False
        self.controller.poll_temperature()
        self.wait_for(lambda: len(self.results) == 2)
        self.assertEqual(self.results[-1]["temperature_c"], -65)

    def test_unstable_readback_updates_display_without_overwriting_target_or_blocking_capture(self):
        section = _LF6Section(self.controller)
        try:
            self.assertTrue(hasattr(section, "_on_temperature_snapshot"))
            section._real_andor = True
            section._andor_target_temperature.setValue(-80)
            section._on_temperature_snapshot(self.read())
            self.assertIn("-65.0", section._temperature.text())
            self.assertIn("-70.0", section._temperature_detail.text())
            self.assertIn("5.0", section._temperature_detail.text())
            self.assertEqual(section._andor_target_temperature.value(), -80)
            spectra = []
            self.controller.spectrum_ready.connect(lambda *args: spectra.append(args))
            self.controller.acquire_single()
            self.wait_for(lambda: bool(spectra))
        finally:
            section.deleteLater()

    def test_queued_acquisition_pauses_monitor_before_worker_starts(self):
        self.controller.acquire_single()
        self.controller.poll_temperature()
        self.assertTrue(self.controller._worker.temperature_monitor_paused.is_set())
        self.assertEqual(self.reads, 0)
        self.wait_for(lambda: not self.controller._worker.temperature_monitor_paused.is_set())

    def test_queued_monitor_is_cancelled_when_measurement_takes_ownership(self):
        blocker = threading.Event()
        started = threading.Event()
        self.controller._worker._adapter.acquire = lambda: (started.set(), blocker.wait(2))
        # Hold the worker with an acquisition, then queue a monitor directly
        # to model a poll already queued just before a workflow starts.
        self.controller.acquire_single()
        self.wait_for(started.is_set)
        self.controller._temperature_pending = True
        self.controller._temperature_snapshot_requested.emit(self.controller._temperature_generation)
        self.controller.set_temperature_monitor_paused("sweep", True)
        blocker.set()
        self.wait_for(lambda: not self.controller._temperature_pending)
        self.assertEqual(self.reads, 0)

    def test_readback_from_previous_connection_is_discarded(self):
        self.assertTrue(hasattr(self.controller, "_temperature_generation"))
        old = self.controller._temperature_generation
        self.controller._temperature_generation += 1
        self.controller._on_temperature_snapshot((old, self.read()))
        self.assertEqual(self.results, [])
