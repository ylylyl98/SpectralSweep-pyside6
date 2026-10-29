"""Real queued controller + shared GUI policy, with in-memory detector only."""
import threading
from types import SimpleNamespace

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget, QVBoxLayout

from controllers.lf6_controller import LF6Controller, LightFieldLifecycleState
from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.sensor_readout_preflight import ensure_sensor_readout
from utils.mock_lf6 import MockLF6Setup


def wait_for(predicate):
    for _ in range(300):
        if predicate():
            return
        QTest.qWait(5)
    assert predicate()


@pytest.fixture
def sensor(monkeypatch):
    quick_application()
    controller = LF6Controller()
    setup = MockLF6Setup(n_pixels=16)
    setup._sensor_height = 12
    setup._sensor_row_binning = 4
    worker = controller._worker
    worker._setup, worker._adapter = setup, SimpleNamespace(invalidate_wavelengths=lambda: None)
    worker._identity = {'backend': 'lightfield', 'serial': 'fake-ccd'}
    worker._state = LightFieldLifecycleState.READY
    host = QWidget()
    QVBoxLayout(host)
    failures = []
    monkeypatch.setattr('ui.sensor_readout_preflight._show_error', lambda parent, message: failures.append(message))
    yield controller, setup, host, failures
    controller.shutdown()
    host.close()
    host.deleteLater()
    controller.deleteLater()
    drain_qt_deletions()


def custom(setup, height=4):
    setup._roi = 'CustomRegions'
    setup._custom_sensor_regions = [dict(x=2, y=1, width=8, height=height, x_binning=1, y_binning=height)]


def test_default_bin_all_uses_full_height_and_runs_sdk_reads_off_gui(sensor, monkeypatch):
    controller, setup, host, failures = sensor
    setup._roi = 'LineSensor'
    called_threads = []
    read = setup.read_sensor_snapshot
    def record():
        called_threads.append(threading.get_ident())
        return read()
    monkeypatch.setattr(setup, 'read_sensor_snapshot', record)
    assert ensure_sensor_readout(host, controller)
    assert not failures
    assert setup._sensor_row_binning == 12
    assert setup.read_sensor_snapshot()['regions'][0]['y_binning'] == 12
    assert called_threads[0] != threading.get_ident()
    assert controller.sensor_readout_intents['lightfield']['mode'] == 'bin_all'
    assert 'Bin All' in host._sensor_readout_summary.text()


def test_preflight_accepts_verified_bin_all_without_editable_settings(sensor, monkeypatch):
    controller, setup, host, failures = sensor
    setup._roi = 'LineSensor'
    setup._sensor_row_binning = setup._sensor_height
    sensor_setup = setup._sensor_setup
    def fixed_sensor():
        adapter = sensor_setup()
        adapter.setting_is_available = lambda _key: False
        adapter.setting_is_writable = lambda _key: False
        return adapter
    monkeypatch.setattr(setup, '_sensor_setup', fixed_sensor)
    assert ensure_sensor_readout(host, controller)
    assert not failures and not controller.sensor_preflight_pending
    assert controller.sensor_readout_intents['lightfield']['mode'] == 'bin_all'


def test_detailed_preflight_failure_is_logged_and_retry_releases_request(sensor, monkeypatch, caplog):
    controller, setup, host, failures = sensor
    prepare = setup.prepare_sensor_readout
    message = 'RowBinning: requested 12; read back 4; acquisition blocked.'
    def rejected(*args, **kwargs):
        raise TimeoutError(message)
    monkeypatch.setattr(setup, 'prepare_sensor_readout', rejected)
    assert not ensure_sensor_readout(host, controller)
    assert failures == [message] and message in caplog.text
    assert not controller.sensor_preflight_pending and not controller.sensor_readout_intents
    monkeypatch.setattr(setup, 'prepare_sensor_readout', prepare)
    assert ensure_sensor_readout(host, controller)
    assert not controller.sensor_preflight_pending


def test_custom_keep_preserves_roi_and_reuses_only_unchanged_confirmation(sensor, monkeypatch):
    controller, setup, host, failures = sensor
    custom(setup)
    choices = []
    monkeypatch.setattr('ui.sensor_readout_preflight._confirm_custom',
                        lambda parent, snapshot: choices.append(snapshot['fingerprint']) or 'custom')
    before = setup.read_sensor_snapshot()
    assert ensure_sensor_readout(host, controller)
    assert ensure_sensor_readout(host, controller)
    assert len(choices) == 1 and setup.read_sensor_snapshot() == before
    custom(setup, height=6)
    assert ensure_sensor_readout(host, controller)
    assert len(choices) == 2 and not failures


def test_custom_cancel_never_mutates_and_switch_consumes_overwrite_permission(sensor, monkeypatch):
    controller, setup, host, failures = sensor
    custom(setup)
    before = setup.read_sensor_snapshot()
    monkeypatch.setattr('ui.sensor_readout_preflight._confirm_custom', lambda *_: None)
    assert not ensure_sensor_readout(host, controller)
    assert setup.read_sensor_snapshot() == before
    monkeypatch.setattr('ui.sensor_readout_preflight._confirm_custom', lambda *_: 'bin_all')
    assert ensure_sensor_readout(host, controller)
    assert setup._roi == 'LineSensor' and setup._sensor_row_binning == 12
    assert 'expected_fingerprint' not in controller.sensor_readout_intents['lightfield']
    custom(setup, height=6)
    with pytest.raises(RuntimeError, match='Custom'):
        controller.prepare_sensor_for_acquisition(mode='1d')
    assert not failures and setup._roi == 'CustomRegions'


def test_cancelled_slow_query_keeps_device_reserved_until_worker_returns(sensor, monkeypatch):
    controller, setup, _, _ = sensor
    entered, release = threading.Event(), threading.Event()
    read = setup.read_sensor_snapshot
    def slow():
        entered.set()
        assert release.wait(3)
        return read()
    monkeypatch.setattr(setup, 'read_sensor_snapshot', slow)
    request = dict(action='read', mode='1d', cancel_event=threading.Event())
    try:
        controller.request_sensor_preflight(request)
        wait_for(entered.is_set)
        request['cancel_event'].set()
        assert controller.sensor_preflight_pending and controller.spectrum_actions_blocked
        with pytest.raises(RuntimeError):
            controller.shutdown()
    finally:
        release.set()
        wait_for(lambda: not controller.sensor_preflight_pending)
    assert 'cancelled' in request['error']
    assert not controller.sensor_readout_intents


def test_queued_external_owner_blocks_read_and_mutation(sensor, monkeypatch):
    controller, setup, _, _ = sensor
    calls = []
    monkeypatch.setattr(setup, 'read_sensor_snapshot', lambda: calls.append('read'))
    controller._worker.external_measurement_active.set()
    request = dict(action='read', mode='1d', cancel_event=threading.Event())
    responses = []
    controller.sensor_preflight_finished.connect(responses.append)
    controller.request_sensor_preflight(request)
    wait_for(lambda: bool(responses))
    controller._worker.external_measurement_active.clear()
    assert not calls and request.get('error') and not controller.sensor_readout_intents


def test_read_only_parked_lightfield_preflight_does_not_route_or_activate(sensor):
    controller, setup, host, failures = sensor
    worker = controller._worker
    worker._parked['lightfield'] = (setup, worker._adapter, worker._identity, [])
    worker._backend, worker._setup = 'andor_si', None
    assert ensure_sensor_readout(host, controller, backends=['lightfield', 'andor_si'])
    assert worker._backend == 'andor_si' and not failures
    assert setup._sensor_row_binning == 12


def test_stop_during_spectrum_sensor_query_never_starts_or_calls_sdk_abort(sensor, monkeypatch):
    from PySide6.QtCore import QTimer
    from ui.spectrum_panel import SpectrumPanel
    controller, setup, _, failures = sensor
    entered, release = threading.Event(), threading.Event()
    read = setup.read_sensor_snapshot
    def slow():
        entered.set()
        assert release.wait(3)
        return read()
    monkeypatch.setattr(setup, 'read_sensor_snapshot', slow)
    monkeypatch.setattr(controller, 'lightfield_optics', lambda *args: None)
    monkeypatch.setattr('ui.detector_wavelength_advice.confirm_detector_wavelength', lambda *args: True)
    calls = []
    controller._worker._adapter.abort_acquisition = lambda: calls.append('sdk_abort')
    monkeypatch.setattr(controller, 'apply_settings', lambda **kwargs: calls.append('apply'))
    panel = SpectrumPanel(controller)
    panel._on_lf6_connected([])
    timer = QTimer(panel)
    def stop_when_query_starts():
        if entered.is_set():
            timer.stop()
            panel._stop_continuous()
    timer.timeout.connect(stop_when_query_starts)
    timer.start(5)
    try:
        panel._start_continuous('1d')
        assert panel._continuous_mode is None
        assert not calls and not failures
        assert controller.sensor_preflight_pending
    finally:
        timer.stop()
        release.set()
        wait_for(lambda: not controller.sensor_preflight_pending)
        panel.close()
        panel.deleteLater()


def test_close_is_rejected_before_shutdown_while_sensor_sdk_call_drains(sensor):
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QMainWindow, QStatusBar
    from ui.main_window import MainWindow
    from unittest.mock import Mock
    window = MainWindow.__new__(MainWindow)
    QMainWindow.__init__(window)
    window._lf6 = SimpleNamespace(capture_status={}, acquisition_in_progress=False, sensor_preflight_pending=True)
    window._status = QStatusBar(window)
    window._power_sweep = SimpleNamespace(shutdown=Mock())
    try:
        event = QCloseEvent()
        window.closeEvent(event)
        assert not event.isAccepted()
        window._power_sweep.shutdown.assert_not_called()
    finally:
        window.deleteLater()


def test_gui_status_and_temperature_do_not_probe_sdk_during_sensor_preflight(sensor):
    controller, setup, _, _ = sensor
    def forbidden(*args):
        raise AssertionError('GUI status must not enter SDK while sensor query owns it')
    controller._worker._setup = SimpleNamespace(is_ready=forbidden, is_busy=forbidden,
        setting_is_available=forbidden, setting_is_writable=forbidden)
    pending = threading.Event()
    controller._sensor_preflight_requests.add(pending)
    try:
        assert controller.is_busy and not controller.is_ready and controller.switching_locked
        assert not controller.setting_is_available('roi')
        assert controller.setting_is_writable('roi') is False
        assert controller.temperature_monitor_paused
        controller.poll_temperature()
        controller.read_temperature()
    finally:
        controller._sensor_preflight_requests.discard(pending)
        controller._worker._setup = setup
