"""Delayed optical updates using production setters and a deterministic clock."""
import json
import threading
from types import SimpleNamespace

import pytest

from app.devices.lightfield_optics import apply_optics, ensure_output_route
from lf6_automation import LF6Setup, SpectrometerSettings
from tests.test_lightfield_acquisition_validation import CENTER, EXIT, Experiment
from tests.test_lightfield_setup_transition import Clock


GRATING = SpectrometerSettings.GratingSelected
OLD_CENTER = 1097.3969261940897


class DelayedExperiment(Experiment):
    IsRunning = False
    IsReadyToRun = True

    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.values.update({CENTER: 1100., GRATING: 'G1'})
        self.events = []
        self.center_write_times = []
        self.reverts_remaining = 0
        self.busy_until = 0.
        self.revert_extra = {}

    @property
    def IsUpdating(self):
        return self.clock.now < self.busy_until

    def _advance(self):
        due = [event for event in self.events if event[0] <= self.clock.now]
        self.events = [event for event in self.events if event[0] > self.clock.now]
        for _, values in sorted(due, key=lambda event: event[0]):
            self.values.update(values)

    def GetValue(self, key):
        self._advance()
        return super().GetValue(key)

    def SetValue(self, key, value):
        self._advance()
        super().SetValue(key, value)
        if key == EXIT:
            # The selected exit updates immediately, before its wavelength.
            self.events.append((self.clock.now + .04, {CENTER: OLD_CENTER}))
        if key == CENTER:
            self.center_write_times.append(self.clock.now)
            if self.reverts_remaining:
                self.reverts_remaining -= 1
                self.events.append((self.clock.now + .04,
                                    {CENTER: OLD_CENTER, **self.revert_extra}))

    def GetCurrentCapabilities(self, key):
        return ['G1', 'G2'] if key == GRATING else super().GetCurrentCapabilities(key)


@pytest.fixture
def optics(monkeypatch):
    clock = Clock()
    monkeypatch.setattr('lf6_automation.time', clock)
    monkeypatch.setattr('app.devices.lightfield_optics.time', clock, raising=False)
    monkeypatch.setattr('app.lightfield_diagnostics.time', clock, raising=False)
    setup = object.__new__(LF6Setup)
    setup.application = SimpleNamespace(IsReady=True, IsBusy=False)
    setup.experiment = DelayedExperiment(clock)
    setup.spectrometer_settings = SpectrometerSettings
    return setup, clock


def test_exit_waits_for_delayed_center_even_when_sdk_reports_idle(optics):
    setup, clock = optics
    actual = ensure_output_route(setup, 'front')
    assert actual['output_port'] == 'FrontExit'
    assert actual['wavelength_nm'] == OLD_CENTER
    assert clock.now >= .35  # .05 observation + .30 continuously stable.
    assert not setup.experiment.center_write_times


def test_port_and_center_apply_waits_before_the_first_motor_write(optics):
    setup, clock = optics
    actual = apply_optics(setup, {'output_port': 'FrontExit', 'wavelength_nm': 720.})
    assert actual['wavelength_nm'] == 720.
    assert setup.experiment.center_write_times[0] >= .35
    assert setup.center_wavelength_write_stats['attempts'] == 1


def test_stability_window_restarts_when_grating_changes(optics):
    setup, clock = optics
    setup.experiment.events.append((.25, {GRATING: 'G2'}))
    ensure_output_route(setup, 'front')
    assert clock.now >= .55 - 1e-12


def test_matching_center_that_reverts_recovers_with_one_rewrite(optics):
    setup, clock = optics
    setup.experiment.reverts_remaining = 1
    setup.set_center_wavelength_when_ready(720., timeout_s=1.)
    assert setup.center_wavelength_write_stats['result'] == 'succeeded'
    assert setup.center_wavelength_write_stats['attempts'] == 2
    assert setup.experiment.center_write_times[1] >= .35
    assert setup.experiment.values[CENTER] == 720.


def test_rewrite_waits_for_busy_state_to_end(optics):
    setup, clock = optics
    setup.experiment.reverts_remaining = 1
    original = setup.experiment.SetValue

    def write(key, value):
        original(key, value)
        if key == CENTER and len(setup.experiment.center_write_times) == 1:
            setup.experiment.busy_until = .25

    setup.experiment.SetValue = write
    setup.set_center_wavelength_when_ready(720., timeout_s=1.)
    assert setup.experiment.center_write_times[1] >= .55 - 1e-12


def test_permanent_reversion_is_limited_to_two_rewrites_and_one_deadline(optics):
    setup, clock = optics
    setup.experiment.reverts_remaining = 100
    with pytest.raises(TimeoutError):
        setup.set_center_wavelength_when_ready(720., timeout_s=1.5)
    assert len(setup.experiment.center_write_times) == 3
    assert clock.now == pytest.approx(1.5)
    assert setup.center_wavelength_write_stats['result'] == 'timeout'
    assert setup._acquisition_prepared is False


def test_recovery_does_not_restart_a_nearly_expired_timeout(optics):
    setup, clock = optics
    setup.experiment.reverts_remaining = 1
    with pytest.raises(TimeoutError):
        setup.set_center_wavelength_when_ready(720., timeout_s=.2)
    assert setup.experiment.center_write_times == [0.]
    assert clock.now == pytest.approx(.2)


@pytest.mark.parametrize('field, value', [(EXIT, 'FrontExit'), (GRATING, 'G2')])
def test_changed_optical_configuration_blocks_rewrite(optics, field, value):
    setup, clock = optics
    setup.experiment.reverts_remaining = 1
    setup.experiment.revert_extra = {field: value}
    with pytest.raises(RuntimeError, match='configuration changed'):
        setup.set_center_wavelength_when_ready(720., timeout_s=1.)
    assert setup.experiment.center_write_times == [0.]
    assert setup._acquisition_prepared is False


def test_wrong_center_without_any_matching_read_is_never_rewritten(optics):
    setup, clock = optics
    setup.experiment.ignored.add(CENTER)
    with pytest.raises(TimeoutError):
        setup.set_center_wavelength_when_ready(720., timeout_s=1.)
    assert setup.experiment.center_write_times == [0.]


def test_missing_center_after_transient_match_is_never_rewritten(optics):
    setup, clock = optics
    setup.experiment.reverts_remaining = 1
    setup.experiment.revert_extra = {CENTER: None}
    with pytest.raises(TimeoutError):
        setup.set_center_wavelength_when_ready(720., timeout_s=1.)
    assert setup.experiment.center_write_times == [0.]


def test_recovery_diagnostics_record_each_write_and_reason(optics, tmp_path):
    setup, clock = optics
    setup._center_diagnostics_path = tmp_path / 'center.jsonl'
    setup.experiment.reverts_remaining = 1
    setup.set_center_wavelength_when_ready(720., timeout_s=1.)
    record = json.loads(setup._center_diagnostics_path.read_text(encoding='utf-8'))
    assert record['stats']['attempts'] == 2
    assert len(record['stats']['write_attempts']) == 2
    assert record['stats']['before_write']['center_nm'] == 1100.
    rewrite = record['stats']['rewrites'][0]
    assert rewrite['reason'] == 'matching_center_reverted'
    assert rewrite['readback_nm'] == OLD_CENTER
    assert rewrite['next_attempt'] == 2


def test_port_wait_and_center_recovery_share_apply_timeout(optics):
    setup, clock = optics
    setup.experiment.reverts_remaining = 100
    with pytest.raises(TimeoutError):
        apply_optics(setup, {'output_port': 'FrontExit', 'wavelength_nm': 720.},
                     timeout_s=.6)
    assert clock.now == pytest.approx(.6)
    assert len(setup.experiment.center_write_times) == 1


def test_capture_preparation_shares_exit_and_recovery_budget(optics):
    setup, clock = optics
    setup.detector_output_route = 'front'
    setup.experiment.reverts_remaining = 100
    with pytest.raises(TimeoutError):
        setup.configure_for_acquisition(center_nm=720., exposure_ms=100., frames=1,
                                        timeout_s=.6)
    assert clock.now == pytest.approx(.6)
    assert setup.experiment.captures == 0
    assert setup._acquisition_prepared is False


def test_winspec_preparation_shares_exit_and_recovery_budget(optics, monkeypatch):
    from app.devices.winspec_adapter import WinSpecSetup
    from tests.test_winspec_adapter import Camera

    setup, clock = optics
    monkeypatch.setattr('app.devices.winspec_adapter.time', clock)
    setup.experiment.values[EXIT] = 'FrontExit'
    setup.experiment.reverts_remaining = 100
    winspec = WinSpecSetup(setup, client=Camera(), output_route='side')
    with pytest.raises(TimeoutError):
        winspec.configure_for_acquisition(center_nm=720., exposure_ms=100., frames=1,
                                          timeout_s=.6)
    assert clock.now == pytest.approx(.6)
    assert winspec._acquisition_prepared is False


def test_automatic_detector_activation_does_not_restart_optics_budget(optics):
    from app.devices.lf6_adapter import SpectrometerLF6
    from controllers.lf6_controller import _LF6Worker, LightFieldLifecycleState

    setup, clock = optics
    setup.detector_output_route = 'front'
    setup.experiment.reverts_remaining = 100
    worker = _LF6Worker()
    worker._setup = SimpleNamespace(is_busy=False)
    worker._backend = 'winspec_ingaas'
    worker._identity = {'backend': 'winspec_ingaas', 'output_route': 'side'}
    worker._parked['lightfield'] = (
        setup, SpectrometerLF6(setup), {'backend': 'lightfield', 'output_route': 'front'}, [])
    worker._state = LightFieldLifecycleState.READY
    worker.temperature_monitor_paused.set()
    request = dict(backend='lightfield', center=720., exposure=100., frames=1,
                   reduction='device', stop=threading.Event(), done=threading.Event())
    worker.prepare_scan_condition(request)
    assert 'error' in request
    assert clock.now == pytest.approx(15.)
    assert setup.experiment.captures == 0


def test_slow_writability_probe_cannot_send_a_center_command_after_deadline(optics):
    setup, clock = optics

    def writable(key):
        clock.sleep(.2)
        return True

    setup.experiment.IsWritable = writable
    with pytest.raises(TimeoutError):
        setup.set_center_wavelength_when_ready(720., timeout_s=.1)
    assert not setup.experiment.center_write_times


def test_slow_writability_probe_cannot_send_an_exit_command_after_deadline(optics):
    setup, clock = optics

    def writable(key):
        clock.sleep(.2)
        return True

    setup.experiment.IsWritable = writable
    with pytest.raises(TimeoutError):
        apply_optics(setup, {'output_port': 'FrontExit'}, timeout_s=.1)
    assert not setup.experiment.writes


def test_route_capability_read_uses_the_original_timeout(optics):
    setup, clock = optics
    original = setup.experiment.GetCurrentCapabilities
    delayed = False

    def capabilities(key):
        nonlocal delayed
        if key == EXIT and not delayed:
            delayed = True
            clock.sleep(.2)
        return original(key)

    setup.experiment.GetCurrentCapabilities = capabilities
    with pytest.raises(TimeoutError):
        ensure_output_route(setup, 'front', timeout_s=.1)
    assert not setup.experiment.writes


def test_matching_read_arriving_after_deadline_cannot_report_success(optics):
    setup, clock = optics
    original = setup.experiment.GetValue
    reads = 0

    def read(key):
        nonlocal reads
        if key == CENTER:
            reads += 1
            if reads == 3:
                clock.sleep(.2)
        return original(key)

    setup.experiment.GetValue = read
    with pytest.raises(TimeoutError):
        setup.set_center_wavelength_when_ready(720., timeout_s=.25)
    assert setup.center_wavelength_write_stats['result'] == 'timeout'


@pytest.mark.parametrize('missing', [EXIT, GRATING])
def test_missing_initial_optical_identity_disables_automatic_rewrite(optics, missing):
    setup, clock = optics
    del setup.experiment.values[missing]
    setup.experiment.reverts_remaining = 1
    setup.experiment.revert_extra = {missing: 'FrontExit' if missing == EXIT else 'G2'}
    with pytest.raises(TimeoutError):
        setup.set_center_wavelength_when_ready(720., timeout_s=1.)
    assert setup.experiment.center_write_times == [0.]


@pytest.fixture
def side_scan(optics, monkeypatch):
    from app.devices.winspec_adapter import WinSpecSetup
    from app.devices.winspec_scan_adapter import WinSpecScanAdapter
    from app.wavelength_calibration import fit_calibration
    from tests.test_winspec_adapter import Camera
    from utils.config import cfg

    setup, clock = optics
    setup.experiment.values[EXIT] = 'FrontExit'
    setup.experiment.ExperimentDevices = [SimpleNamespace(
        Type='Spectrometer', Model='SP', SerialNumber='123')]
    monkeypatch.setattr('app.devices.winspec_adapter.time', clock)
    monkeypatch.setattr('app.devices.winspec_scan_adapter.time', clock, raising=False)
    camera = Camera()
    winspec = WinSpecSetup(setup, client=camera, output_route='side')
    context = winspec._calibration_context(
        {'grating': 'G1', 'wavelength_nm': 720., 'output_port': 'SideExit'})
    record = fit_calibration([20, 200, 490], [702, 720, 749], [300], [730], context)
    monkeypatch.setattr(cfg.lf6, 'winspec_wavelength_calibrations', [record])
    scan = WinSpecScanAdapter(winspec)
    return scan, setup, camera, clock


def test_winspec_side_calibration_is_checked_after_shared_budget_routing(side_scan):
    scan, setup, camera, clock = side_scan
    actual = scan.configure_for_acquisition(center_nm=720., exposure_ms=100., frames=1)
    assert actual['axis_unit'] == 'nm'
    assert setup.experiment.values[EXIT] == 'SideExit'
    assert setup.experiment.center_write_times[0] >= .35


def test_identity_changed_by_final_writability_probe_blocks_rewrite(optics):
    setup, clock = optics
    setup.experiment.reverts_remaining = 1

    def writable(key):
        if clock.now >= .35 - 1e-12:
            setup.experiment.values[GRATING] = 'G2'
        return True

    setup.experiment.IsWritable = writable
    with pytest.raises(RuntimeError, match='configuration changed'):
        setup.set_center_wavelength_when_ready(720., timeout_s=1.)
    assert setup.experiment.center_write_times == [0.]


def test_slow_initial_diagnostics_cannot_restart_the_write_budget(optics, tmp_path, monkeypatch):
    import app.lightfield_diagnostics as diagnostics

    setup, clock = optics
    setup._center_diagnostics_path = tmp_path / 'center.jsonl'
    original = diagnostics.center_context

    def context(*args):
        clock.sleep(.2)
        return original(*args)

    monkeypatch.setattr(diagnostics, 'center_context', context)
    with pytest.raises(TimeoutError):
        setup.set_center_wavelength_when_ready(720., timeout_s=.15)
    assert not setup.experiment.center_write_times
    record = json.loads(setup._center_diagnostics_path.read_text(encoding='utf-8'))
    assert record['stats']['attempts'] == 0
    assert record['stats']['result'] == 'timeout'


def test_slow_final_exists_check_cannot_send_a_late_motor_command(optics):
    setup, clock = optics
    original = setup.experiment.Exists
    original_write = setup._set_center_wavelength_raw
    in_raw_write = False

    def exists(key):
        if key == CENTER and in_raw_write:
            clock.sleep(.2)
        return original(key)

    def write(value, **kwargs):
        nonlocal in_raw_write
        in_raw_write = True
        try:
            return original_write(value, **kwargs)
        finally:
            in_raw_write = False

    setup.experiment.Exists = exists
    setup._set_center_wavelength_raw = write
    with pytest.raises(TimeoutError):
        setup.set_center_wavelength_when_ready(720., timeout_s=.1)
    assert not setup.experiment.center_write_times
    assert setup.center_wavelength_write_stats['attempts'] == 0


def test_winspec_status_read_cannot_restart_scan_budget(side_scan):
    scan, setup, camera, clock = side_scan
    original = camera.request

    def request(command, *args, **kwargs):
        if command == 'GET_SETTINGS':
            clock.sleep(.3)
        return original(command, *args, **kwargs)

    camera.request = request
    with pytest.raises(TimeoutError):
        scan.configure_for_acquisition(center_nm=720., exposure_ms=100., frames=1,
                                       timeout_s=.6)
    assert not setup.experiment.center_write_times
    assert scan._prepared is None
    assert scan.setup._acquisition_prepared is False


def test_final_scan_calibration_read_cannot_report_prepared_after_deadline(side_scan):
    scan, setup, camera, clock = side_scan
    original = scan._context
    reads = 0

    def context():
        nonlocal reads
        reads += 1
        if reads == 2:
            clock.sleep(.3)
        return original()

    scan._context = context
    with pytest.raises(TimeoutError):
        scan.configure_for_acquisition(center_nm=720., exposure_ms=100., frames=1,
                                       timeout_s=.6)
    assert scan._prepared is None


def test_center_unavailable_during_final_readiness_probe_blocks_rewrite(optics):
    setup, clock = optics
    setup.experiment.reverts_remaining = 1
    invalidated = False

    def writable(key):
        nonlocal invalidated
        if clock.now >= .35 - 1e-12 and not invalidated:
            invalidated = True
            setup.experiment.values[CENTER] = None
        return True

    setup.experiment.IsWritable = writable
    with pytest.raises((RuntimeError, TimeoutError)):
        setup.set_center_wavelength_when_ready(720., timeout_s=1.)
    assert setup.experiment.center_write_times == [0.]


def test_slow_final_exit_confirmation_cannot_report_apply_success(optics):
    setup, clock = optics
    original = setup.experiment.GetValue
    original_wait = setup.wait_until_optics_stable
    delayed = False
    settled = False

    def wait(**kwargs):
        nonlocal settled
        result = original_wait(**kwargs)
        settled = True
        return result

    def read(key):
        nonlocal delayed
        if key == EXIT and settled and not delayed:
            delayed = True
            clock.sleep(.4)
        return original(key)

    setup.experiment.GetValue = read
    setup.wait_until_optics_stable = wait
    with pytest.raises(TimeoutError):
        apply_optics(setup, {'output_port': 'FrontExit'}, timeout_s=.6)
