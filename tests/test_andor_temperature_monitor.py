import os
import threading
import time
import unittest
from types import SimpleNamespace
from datetime import timedelta
from unittest.mock import Mock

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

    def test_winspec_idle_monitor_refreshes_and_respects_pause(self):
        self.controller._worker._identity = {"backend": "winspec_ingaas"}
        self.controller._temperature_timer.setInterval(10)
        self.wait_for(lambda: len(self.results) >= 2)
        self.controller.set_temperature_monitor_paused("acquisition", True)
        self.wait_for(lambda: not self.controller._temperature_pending)
        before = self.reads
        self.controller.poll_temperature()
        self.app.processEvents()
        self.assertEqual(self.reads, before)

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

    def test_manual_and_queued_manual_reads_blocked_during_sweep(self):
        read = Mock(return_value=-100.)
        self.controller._worker._setup.get_temperature = read
        self.controller.set_temperature_monitor_paused('sweep', True)
        requested = []
        self.controller._temperature_requested.connect(lambda: requested.append(True))
        self.controller.read_temperature()
        self.controller._worker.read_temperature()  # Already queued before pause.
        self.assertFalse(requested)
        read.assert_not_called()

    def test_manual_warmup_read_remains_available_but_not_during_sweep(self):
        read = Mock(return_value=-10.)
        self.controller._worker._setup.get_temperature = read
        self.controller.set_temperature_monitor_paused('warmup', True)
        self.controller._worker.read_temperature()
        read.assert_called_once()
        self.controller.set_temperature_monitor_paused('sweep', True)
        self.controller._worker.read_temperature()
        self.assertEqual(read.call_count, 1)

    def test_unchanged_monitor_state_emitted_only_once(self):
        states = []
        self.controller.temperature_monitor_state.connect(states.append)
        self.controller.set_temperature_monitor_paused('sweep', True)
        for _ in range(5):
            self.controller.poll_temperature()
        self.assertEqual(states, ['Paused during measurement'])

    def test_read_started_before_sweep_not_accepted_as_fresh_after_resume(self):
        generation = self.controller._temperature_generation
        self.controller.set_temperature_monitor_paused('sweep', True)
        self.controller.set_temperature_monitor_paused('sweep', False)
        self.controller._on_temperature_snapshot((generation, self.read()))
        self.assertEqual(self.results, [])

    def test_pause_during_read_prevents_followup_query_to_parked_camera(self):
        worker = self.controller._worker
        parked_read = Mock()
        worker._parked['andor_si'] = (SimpleNamespace(get_temperature_snapshot=parked_read), None, {}, [])
        def read_and_pause():
            worker.temperature_monitor_paused.set()
            return self.read()
        worker._setup.get_temperature_snapshot = read_and_pause
        worker.read_temperature_snapshot(0)
        parked_read.assert_not_called()
        worker._parked.clear()

    def test_winspec_display_stays_paused_between_frames_and_after_readback_expires(self):
        self.controller._worker._backend = 'winspec_ingaas'
        self.controller._worker._identity = {'backend': 'winspec_ingaas'}
        section = _LF6Section(self.controller)
        try:
            section._backend.setCurrentIndex(section._backend.findData('winspec_ingaas'))
            section._on_temperature_snapshot(dict(temperature_c=-100., temperature_status='Locked', temperature_setpoint_c=-100.))
            before = section._temperature.text()
            self.controller.set_temperature_monitor_paused('sweep', True)
            self.assertEqual(section._temperature.text(), before)
            self.assertIn('paused', section._temperature_monitor.text().lower())
            self.assertFalse(section._temperature_refresh.isEnabled())
            section._winspec_readback_time -= timedelta(seconds=60)
            paused = (section._temperature.text(), section._temperature_detail.text(), section._temperature_monitor.text(), section._acquisition_condition.text())
            for busy in (True, False, True, False):
                self.controller._worker._setup.is_busy = busy
                section._status_refresh_timer.timeout.emit()
                self.assertEqual(paused, (section._temperature.text(), section._temperature_detail.text(), section._temperature_monitor.text(), section._acquisition_condition.text()))
            self.controller.set_temperature_monitor_paused('sweep', False)
            self.assertIn('no fresh readback', section._temperature.text())
            section._on_temperature_snapshot(dict(temperature_c=-100., temperature_status='Locked', temperature_setpoint_c=-100.))
            self.assertEqual(section._temperature.text(), before)
            self.assertTrue(section._temperature_refresh.isEnabled())
        finally:
            section.close()

    def test_sidebar_timer_does_not_rewrite_connection_or_unchanged_temperature(self):
        self.controller._worker._backend = 'winspec_ingaas'
        self.controller._worker._identity = {'backend': 'winspec_ingaas'}
        section = _LF6Section(self.controller)
        try:
            section._backend.setCurrentIndex(section._backend.findData('winspec_ingaas'))
            section._on_temperature_snapshot(dict(temperature_c=-100., temperature_status='Locked', temperature_setpoint_c=-100.))
            section._connections_status.setText = Mock(wraps=section._connections_status.setText)
            section._temperature.setText = Mock(wraps=section._temperature.setText)
            for _ in range(4):
                section._status_refresh_timer.timeout.emit()
            section._connections_status.setText.assert_not_called()
            section._temperature.setText.assert_not_called()
        finally:
            section.close()
