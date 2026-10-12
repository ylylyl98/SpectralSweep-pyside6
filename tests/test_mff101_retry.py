"""Retry real adapter commands against a timed USB boundary, never live hardware."""
import threading
from types import SimpleNamespace

import pytest

from app.devices import mff101_adapter as module
from tests.test_mff101_adapter import _Clock, _Flipper


@pytest.fixture
def rig(monkeypatch):
    clock, driver = _Clock(), _Flipper()
    reads, commands = [], []
    state = lambda: 0
    def read():
        value = state()
        reads.append((clock.now, value))
        return value
    def command(value):
        commands.append((clock.now, value))
    driver.get_state, driver.move_to_state = read, command
    monkeypatch.setattr(module, '_load_thorlabs', lambda: SimpleNamespace(MFF=lambda _: driver))
    monkeypatch.setattr(module.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(module.time, 'sleep', clock.sleep)
    adapter = module.MFF101Adapter('37000123')
    def set_state(callback):
        nonlocal state
        state = callback
    yield SimpleNamespace(adapter=adapter, driver=driver, clock=clock, reads=reads,
                          commands=commands, state=set_state)
    adapter.close()


def test_retry_once_after_first_command_is_ignored(rig):
    rig.state(lambda: 1 if len(rig.commands) == 2 else 0)
    assert rig.adapter.move_to(2, timeout_s=.2) == 2
    assert [value for _, value in rig.commands] == [1, 1]
    assert rig.commands[1][0] >= .2
    assert rig.clock.now <= .4


def test_wrong_endpoint_after_both_attempts_fails_with_no_third_command(rig):
    with pytest.raises(TimeoutError, match='2.*command'):
        rig.adapter.move_to(2, timeout_s=.2)
    assert len(rig.commands) == 2
    assert rig.clock.now == pytest.approx(.4)


def test_target_read_at_retry_boundary_avoids_resending(rig):
    rig.state(lambda: 1 if rig.clock.now >= .2 else 0)
    assert rig.adapter.move_to(2, timeout_s=.2) == 2
    assert len(rig.commands) == 1


def test_still_moving_after_first_window_only_waits_for_arrival(rig):
    rig.state(lambda: (1 if rig.clock.now >= .3 else None) if rig.commands else 0)
    assert rig.adapter.move_to(2, timeout_s=.2) == 2
    assert len(rig.commands) == 1


def test_unresolved_motion_has_bounded_wait_and_no_retry(rig):
    rig.state(lambda: None if rig.commands else 0)
    with pytest.raises(TimeoutError):
        rig.adapter.move_to(2, timeout_s=.2)
    assert len(rig.commands) == 1
    assert rig.clock.now == pytest.approx(.4)


def test_wait_for_existing_motion_before_sending_any_command(rig):
    rig.state(lambda: 1 if rig.clock.now >= .1 else None)
    assert rig.adapter.move_to(2, timeout_s=.2) == 2
    assert rig.commands == []


def test_already_at_target_needs_no_command(rig):
    rig.state(lambda: 1)
    assert rig.adapter.move_to(2, timeout_s=.2) == 2
    assert rig.commands == []


@pytest.mark.parametrize('error', [OSError('USB disconnected'), TimeoutError('USB read timeout')])
def test_communication_error_is_not_a_motion_retry(rig, error):
    def state():
        if rig.commands:
            raise error
        return 0
    rig.state(state)
    with pytest.raises(type(error), match='USB'):
        rig.adapter.move_to(2, timeout_s=.2)
    assert len(rig.commands) == 1


def test_write_timeout_is_never_retried(rig):
    attempts = []
    def write(value):
        attempts.append(value)
        raise TimeoutError('USB write timeout')
    rig.driver.move_to_state = write
    with pytest.raises(TimeoutError, match='USB'):
        rig.adapter.move_to(2, timeout_s=.2)
    assert attempts == [1]


def test_stop_during_first_attempt_prevents_retry(rig):
    stop = threading.Event()
    def state():
        if rig.clock.now >= .1:
            stop.set()
        return 0
    rig.state(state)
    with pytest.raises(RuntimeError, match='stopped'):
        rig.adapter.move_to(2, timeout_s=.2, stop_event=stop)
    assert len(rig.commands) == 1
    assert rig.clock.now == pytest.approx(.1)


def test_stop_while_motion_is_unknown_ends_polling_without_timeout(rig):
    stop = threading.Event()
    def state():
        if rig.clock.now >= .1:
            stop.set()
        return None if rig.commands else 0
    rig.state(state)
    with pytest.raises(RuntimeError, match='stopped'):
        rig.adapter.move_to(2, timeout_s=.2, stop_event=stop)
    assert len(rig.commands) == 1
    assert rig.clock.now == pytest.approx(.1)


def test_stop_after_second_command_ends_waiting(rig):
    stop = threading.Event()
    def state():
        if len(rig.commands) == 2:
            stop.set()
        return 0
    rig.state(state)
    with pytest.raises(RuntimeError, match='stopped'):
        rig.adapter.move_to(2, timeout_s=.2, stop_event=stop)
    assert len(rig.commands) == 2
    assert rig.clock.now < .4


def test_stop_before_start_never_sends_a_command(rig):
    stop = threading.Event()
    stop.set()
    with pytest.raises(RuntimeError, match='stopped'):
        rig.adapter.move_to(2, timeout_s=.2, stop_event=stop)
    assert not rig.commands
