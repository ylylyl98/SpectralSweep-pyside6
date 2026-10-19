"""Real retry adapter on the production Qt worker, with a fake USB driver."""
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from app.devices import mff101_adapter as module
from controllers.lf6_controller import LF6Controller, LightFieldLifecycleState
from controllers.mff101_controller import MFF101Controller
from tests.test_mff101_adapter import _Flipper
from tests.test_mff101_control import app, wait_for  # noqa: F401
from utils.config import MFF101Config, cfg


@pytest.fixture
def hardware(app, monkeypatch):
    monkeypatch.setattr(cfg, 'mff101', MFF101Config(auto_route=True))
    driver = _Flipper()
    driver.commands = []
    driver.started = threading.Event()
    driver.fail_all = False
    driver.state = 0
    driver.get_state = lambda: driver.state
    def move(value):
        driver.commands.append((value, threading.get_ident()))
        driver.started.set()
        if len(driver.commands) == 2 and not driver.fail_all:
            driver.state = value
    driver.move_to_state = move
    monkeypatch.setattr(module, '_load_thorlabs', lambda: SimpleNamespace(MFF=lambda _: driver))
    class FastAdapter(module.MFF101Adapter):
        def move_to(self, position, *, stop_event=None):
            return super().move_to(position, timeout_s=.15, stop_event=stop_event)
    ctrl = MFF101Controller(adapter_factory=FastAdapter, scan_func=lambda: ['37000123'])
    yield ctrl, driver
    assert ctrl.shutdown()
    app.processEvents()


@pytest.mark.parametrize('operation', ['manual', 'route'])
def test_manual_and_automatic_motion_share_bounded_retry(app, hardware, operation):
    ctrl, driver = hardware
    assert ctrl.connect_instrument('37000123')
    wait_for(app, lambda: not ctrl.is_busy)
    assert (ctrl.move_to(2) if operation == 'manual' else ctrl.route_for_setup('lightfield'))
    wait_for(app, lambda: not ctrl.is_busy)
    assert not ctrl.state['error']
    assert ctrl.state['position'] == 2
    assert len(driver.commands) == 2
    owners = {owner for _, owner in driver.commands}
    assert len(owners) == 1 and threading.get_ident() not in owners


@pytest.mark.parametrize('in_motion', [False, True])
def test_measurement_stop_cancels_pending_retry(app, hardware, in_motion):
    ctrl, driver = hardware
    if in_motion:
        driver.get_state = lambda: None if driver.commands else 0
    stop, errors = threading.Event(), []
    def preflight():
        try:
            ctrl.ensure_setup_position('lightfield', stop_event=stop)
        except Exception as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=preflight)
    thread.start()
    try:
        assert driver.started.wait(1)
        stop.set()
        thread.join(2)
        assert not thread.is_alive()
        assert len(driver.commands) == 1
        assert errors and 'stopped' in errors[0]
    finally:
        stop.set()
        thread.join(2)


@pytest.mark.parametrize('in_motion', [False, True])
def test_shutdown_cancels_retry_without_needing_gui_dispatch(app, hardware, in_motion):
    ctrl, driver = hardware
    if in_motion:
        driver.get_state = lambda: None if driver.commands else 0
    assert ctrl.route_for_setup('lightfield')
    assert driver.started.wait(1)
    assert ctrl.shutdown(timeout_ms=200)
    assert len(driver.commands) == 1
    assert driver.closed


@pytest.mark.parametrize('fail_all', [False, True])
def test_detector_acquires_only_after_successful_retry(app, hardware, fail_all):
    ctrl, driver = hardware
    driver.fail_all = fail_all
    spec = LF6Controller()
    spec.set_flipper_controller(ctrl)
    captures, errors = [], []
    def acquire():
        captures.append(driver.state)
        return np.array([700.]), np.array([1.])
    worker = spec._worker
    worker._setup = SimpleNamespace(close=lambda: None)
    worker._adapter = SimpleNamespace(acquire=acquire)
    worker._identity = {'backend': 'lightfield'}
    worker._state = LightFieldLifecycleState.READY
    spec.error.connect(errors.append)
    try:
        spec.acquire_single()
        wait_for(app, lambda: spec._acquisition_requests == 0)
        assert len(driver.commands) == 2
        if fail_all:
            assert not captures
            assert errors and '2 command(s)' in errors[0]
            assert ctrl.state['position'] is None
        else:
            assert captures == [1]
            assert not errors
    finally:
        spec.shutdown()
