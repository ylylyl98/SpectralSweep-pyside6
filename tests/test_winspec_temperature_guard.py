import importlib.util
from pathlib import Path

import pytest


def guard_module():
    path = Path(__file__).parents[1] / 'tools/winspec/temperature_guard.py'
    spec = importlib.util.spec_from_file_location('temperature_guard', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('value', [-99.5, -80, 20, -100, -120])
def test_locked_valid_temperature_allowed(value):
    guard_module().validate_temperature({'actual_temperature_c': value, 'temperature_locked': True})


@pytest.mark.parametrize('value', [None, True, '-100', float('nan'), float('inf')])
def test_bad_temperature_fails_closed(value):
    with pytest.raises(RuntimeError, match='temperature interlock'):
        guard_module().validate_temperature({'actual_temperature_c': value, 'temperature_locked': True})


@pytest.mark.parametrize('locked', [False, None, 1, 'true'])
def test_lock_must_be_real_boolean(locked):
    with pytest.raises(RuntimeError, match='temperature interlock'):
        guard_module().validate_temperature({'actual_temperature_c': -99, 'temperature_locked': locked})


def test_failed_monitor_stops_and_cannot_return_success():
    guard = guard_module()
    stopped = []
    readings = iter([{'actual_temperature_c': -100, 'temperature_locked': True},
                     {'actual_temperature_c': -99, 'temperature_locked': False}])
    monitor = guard.TemperatureGuard(lambda: next(readings), lambda: stopped.append(True))
    monitor.check()
    with pytest.raises(RuntimeError, match='temperature interlock'):
        monitor.check()
    assert stopped == [True]
    with pytest.raises(RuntimeError):
        monitor.report()


def test_monitor_gap_rejected_even_when_temperature_recovers():
    guard = guard_module()
    now = [0.]
    monitor = guard.TemperatureGuard(lambda: {'actual_temperature_c': -100, 'temperature_locked': True},
                                     lambda: None, clock=lambda: now[0])
    monitor.check()
    now[0] = 4.
    with pytest.raises(RuntimeError, match='gap'):
        monitor.check()


@pytest.mark.parametrize('value', [-100., -100.5, -120.])
def test_cold_unlocked_temperature_allowed(value):
    assert guard_module().validate_temperature({'actual_temperature_c': value, 'temperature_locked': False}) == value


def test_boundary_checks_allow_long_exposure_but_reject_slow_read():
    module=guard_module();now=[0.]
    def read():
        return {'actual_temperature_c':-100.,'temperature_locked':True}
    guard=module.TemperatureGuard(read,lambda:None,clock=lambda:now[0],boundary_only=True)
    guard.check();now[0]=3600.;guard.check()
    assert guard.report()['monitoring_mode']=='before_after'
    assert guard.report()['sample_count']==2
    def stalled():
        now[0]+=4.
        return read()
    guard.read=stalled
    with pytest.raises(RuntimeError,match='gap'):guard.check()
