"""Exercise SDK update states without connecting to instruments or opening LF."""
import threading
from types import SimpleNamespace

import pytest

from app.devices.lf6_adapter import SpectrometerLF6
from app.devices.lightfield_optics import ensure_output_route
from controllers.lf6_controller import _LF6Worker, LightFieldLifecycleState
from lf6_automation import LF6Setup, SpectrometerSettings
from tests.test_lightfield_acquisition_validation import CENTER, EXIT, Experiment


class Clock:
    now = 0.

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class UpdatingExperiment(Experiment):
    """Model the documented IsUpdating window following an optical change."""
    IsRunning = False
    IsBusy = False  # An earlier false flag must not hide a later true flag.
    IsReadyToRun = False  # A side-port external camera need not run inside LF.

    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.updating_until = 0.
        self.writes_during_update = []
        self.values[SpectrometerSettings.GratingSelected] = '300 grooves/mm'

    @property
    def IsUpdating(self):
        return self.clock.now < self.updating_until

    def SetValue(self, key, value):
        if self.IsUpdating or self.IsRunning:
            self.writes_during_update.append((key, value))
            return  # SDK calls during updates cannot be relied on to apply.
        super().SetValue(key, value)
        if key == EXIT:
            self.updating_until = self.clock.now + .2


@pytest.fixture
def transition(monkeypatch):
    clock = Clock()
    # Replace only the module's clock, not global time used by Qt/pytest.
    monkeypatch.setattr('lf6_automation.time', clock)
    setup = object.__new__(LF6Setup)
    setup.application = SimpleNamespace(IsReady=True, IsBusy=False)
    setup.experiment = UpdatingExperiment(clock)
    setup.spectrometer_settings = SpectrometerSettings
    setup.detector_output_route = 'front'
    return setup, clock


@pytest.mark.parametrize('flag', ['IsRunning', 'IsUpdating'])
def test_sdk_busy_state_prevents_center_write_despite_legacy_idle_flags(transition, flag):
    setup, clock = transition
    if flag == 'IsUpdating':
        setup.experiment.updating_until = 100.
    else:
        setup.experiment.IsRunning = True
    with pytest.raises(TimeoutError):
        setup.set_center_wavelength_when_ready(730., timeout_s=.5, poll_interval_s=.01)
    assert setup.experiment.writes_during_update == []
    assert setup.center_wavelength_write_stats['attempts'] == 0
    assert setup.center_wavelength_write_stats['state'][flag] is True


def test_output_switch_returns_only_after_sdk_finishes_updating(transition):
    setup, clock = transition
    result = ensure_output_route(setup, 'front')
    assert not setup.experiment.IsUpdating
    assert result['output_port'] == 'FrontExit'
    assert result['wavelength_nm'] == 1097.3969261940897


def test_automatic_winspec_to_pixis_switch_waits_before_center_write(transition):
    setup, clock = transition
    worker = _LF6Worker()
    worker._setup = SimpleNamespace(is_busy=False)
    worker._backend = 'winspec_ingaas'
    worker._identity = {'backend': 'winspec_ingaas', 'output_route': 'side'}
    pixis = SpectrometerLF6(setup)
    worker._parked['lightfield'] = (
        setup, pixis, {'backend': 'lightfield', 'output_route': 'front'}, [])
    worker._state = LightFieldLifecycleState.READY
    worker.temperature_monitor_paused.set()
    request = dict(backend='lightfield', center=730., exposure=250., frames=3,
                   reduction='device', stop=threading.Event(), done=threading.Event())
    worker.prepare_scan_condition(request)
    assert request.get('error') is None
    assert request['done'].is_set()
    assert request['result'] is pixis
    assert worker.backend == 'lightfield'
    assert setup.experiment.writes_during_update == []
    assert setup.experiment.values[CENTER] == 730.
    assert setup.center_wavelength_write_stats['attempts'] == 1
    assert setup.experiment.captures == 0


def test_center_write_can_configure_external_camera_when_lf_cannot_acquire(transition):
    setup, clock = transition
    setup.set_center_wavelength_when_ready(1100., timeout_s=.5, poll_interval_s=.01,
                                           update_acquisition_recipe=False)
    assert setup.experiment.values[CENTER] == 1100.
    assert setup.center_wavelength_write_stats['result'] == 'succeeded'


def test_failed_scan_preparation_identifies_requested_and_active_setup(transition):
    setup, clock = transition
    setup.experiment.ignored.add(CENTER)
    worker = _LF6Worker()
    worker._setup = setup
    worker._adapter = SpectrometerLF6(setup)
    worker._backend = 'lightfield'
    worker._identity = {'backend': 'lightfield', 'output_route': 'front'}
    worker._state = LightFieldLifecycleState.READY
    worker.temperature_monitor_paused.set()
    request = dict(backend='lightfield', center=730., exposure=250., frames=3,
                   reduction='device', stop=threading.Event(), done=threading.Event())
    worker.prepare_scan_condition(request)
    error = str(request['error'])
    assert 'requested_setup=lightfield' in error
    assert 'active_setup=lightfield' in error
    assert 'configured_exit=front' in error
    assert '1097.3969261940897' in error
    assert request['done'].is_set()
    assert 'result' not in request
    assert worker._scan_adapter is None
