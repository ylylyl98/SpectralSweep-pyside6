"""Real LightField acquisition paths with a simulated SDK; never opens hardware."""
from types import SimpleNamespace
import csv
import json

import numpy as np
import pytest

from app.devices.lf6_adapter import SpectrometerLF6
from lf6_automation import LF6Setup, CameraSettings, ExperimentSettings, SpectrometerSettings
from System import Array, Double


CENTER = SpectrometerSettings.GratingCenterWavelength
EXPOSURE = CameraSettings.ShutterTimingExposureTime
EPF = ExperimentSettings.OnlineProcessingFrameCombinationFramesCombined
EXIT = SpectrometerSettings.OpticalPortExitSelected


class Experiment:
    def __init__(self):
        self.values = {CENTER: 1097.3969261940897, EXPOSURE: 100., EPF: 10,
                       EXIT: 'SideExit'}
        self.writes = []
        self.ignored = set()
        self.center_reads = None
        self.captures = 0
        self.after_capture = lambda: None

    def Exists(self, key):
        return key in self.values

    def IsWritable(self, key):
        return True

    def GetValue(self, key):
        if key == CENTER and self.center_reads:
            value = self.center_reads.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        return self.values[key]

    def SetValue(self, key, value):
        self.writes.append((key, value))
        if key not in self.ignored:
            self.values[key] = value

    def GetCurrentCapabilities(self, key):
        return ['FrontExit', 'SideExit'] if key == EXIT else []

    @property
    def SystemColumnCalibration(self):
        center = float(self.values[CENTER])
        return Array[Double]([center - 1., center, center + 1.])

    def Capture(self, frames):
        self.captures += 1
        self.after_capture()
        frame = SimpleNamespace(Width=3, Height=1, Format=None,
                                GetData=lambda: np.array([10., 20., 30.]))
        return SimpleNamespace(GetFrame=lambda *_: frame)


@pytest.fixture
def setup():
    result = object.__new__(LF6Setup)
    result.experiment = Experiment()
    result.application = SimpleNamespace(IsReady=True)
    result.spectrometer_settings = SpectrometerSettings
    result.detector_output_route = 'front'
    result.convert_buffer = lambda data, _format: np.asarray(data)
    # Keep production logic; allow scheduling headroom for the three stable
    # polls while shortening the 15-second hardware wait for offline failures.
    def set_center(value, **kwargs):
        timeout_s = min(.25, kwargs.pop('timeout_s', .25))
        return LF6Setup.set_center_wavelength_when_ready(
            result, value, timeout_s=timeout_s, poll_interval_s=.001, **kwargs)
    result.set_center_wavelength_when_ready = set_center
    return result


@pytest.mark.parametrize('actual', [1097.3969261940897, None, float('nan'), float('inf')])
def test_wrong_or_unavailable_center_cannot_report_success(setup, actual):
    setup.experiment.values[CENTER] = actual
    setup.experiment.ignored.add(CENTER)
    with pytest.raises(TimeoutError, match='720'):
        setup.set_center_wavelength_when_ready(720.)
    assert setup.center_wavelength_write_stats['result'] == 'timeout'


def test_delayed_center_readback_is_waited_without_repeated_motor_writes(setup):
    setup.experiment.center_reads = [1097.3969261940897, 1097.3969261940897, 730., 730., 730.]
    setup.set_center_wavelength_when_ready(730.)
    assert setup.center_wavelength_write_stats['readback'] == 730.
    assert [float(value) for key, value in setup.experiment.writes if key == CENTER] == [730.]


def test_a_single_matching_read_does_not_accept_a_center_that_reverts(setup):
    setup.experiment.center_reads = [720.]
    setup.experiment.ignored.add(CENTER)
    with pytest.raises(TimeoutError):
        setup.set_center_wavelength_when_ready(720.)


def test_readback_error_can_recover_while_waiting(setup):
    setup.experiment.center_reads = [RuntimeError('updating'), 730., 730., 730.]
    setup.set_center_wavelength_when_ready(730.)
    assert setup.center_wavelength_write_stats['readback'] == 730.


@pytest.mark.parametrize('field, target', [(EXPOSURE, 250.), (EPF, 3)])
def test_ignored_exposure_or_accumulation_write_blocks_configuration(setup, field, target):
    setup.experiment.ignored.add(field)
    with pytest.raises(RuntimeError, match='readback'):
        setup.configure_for_acquisition(center_nm=730., exposure_ms=250., frames=3)
    assert setup.experiment.captures == 0


def test_switching_exit_precedes_center_and_preserves_arbitrary_recipe(setup):
    adapter = SpectrometerLF6(setup)
    adapter.configure_for_acquisition(center_nm=730.25, exposure_ms=250., frames=3)
    wavelengths, counts = adapter.acquire()
    assert setup.experiment.writes[0] == (EXIT, 'FrontExit')
    assert setup.experiment.writes[1][0] == CENTER
    np.testing.assert_array_equal(wavelengths, [729.25, 730.25, 731.25])
    np.testing.assert_array_equal(counts, [10., 20., 30.])
    assert float(setup.experiment.values[EXPOSURE]) == 250.
    assert int(setup.experiment.values[EPF]) == 3


@pytest.mark.parametrize('entry', ['controller', 'legacy_adapter'])
def test_all_explicit_center_setters_refresh_previously_cached_wavelengths(setup, entry):
    from controllers.lf6_controller import _LF6Worker, LightFieldLifecycleState

    adapter = SpectrometerLF6(setup)
    adapter.configure_for_acquisition(center_nm=720., exposure_ms=100., frames=10)
    adapter.acquire()
    if entry == 'controller':
        worker = _LF6Worker()
        worker._setup, worker._adapter = setup, adapter
        worker._state = LightFieldLifecycleState.READY
        worker.set_center_wavelength_when_ready(730.)
    else:
        adapter.change_center_wavelength(730.)
    np.testing.assert_array_equal(adapter.calibration_wavelengths(), [729., 730., 731.])
    axis, counts = adapter.acquire()
    np.testing.assert_array_equal(axis, [729., 730., 731.])


def test_acquisition_returns_current_sdk_axis_instead_of_previous_cache(setup):
    adapter = SpectrometerLF6(setup)
    adapter.configure_for_acquisition(center_nm=720., exposure_ms=100., frames=10)
    adapter.acquire()
    setup.get_wavelength_calibration = lambda: np.array([719.5, 720., 720.5])
    axis, counts = adapter.acquire()
    np.testing.assert_array_equal(axis, [719.5, 720., 720.5])


@pytest.mark.parametrize('phase', ['before', 'during'])
def test_grating_drift_blocks_frame_until_recipe_is_reapplied(setup, phase):
    grating = SpectrometerSettings.GratingSelected
    setup.experiment.values[grating] = '300'
    adapter = SpectrometerLF6(setup)
    adapter.configure_for_acquisition(center_nm=720., exposure_ms=100., frames=10)
    change = lambda: setup.experiment.values.update({grating: '600'})
    if phase == 'before':
        change()
    else:
        setup.experiment.after_capture = change
    with pytest.raises(RuntimeError, match='grating'):
        adapter.acquire()
    assert setup.experiment.captures == (1 if phase == 'during' else 0)
    setup.experiment.after_capture = lambda: None
    setup.experiment.values[grating] = '300'
    with pytest.raises(RuntimeError, match='apply'):
        adapter.acquire()
    assert setup.experiment.captures == (1 if phase == 'during' else 0)
    adapter.configure_for_acquisition(center_nm=730., exposure_ms=250., frames=3)
    axis, counts = adapter.acquire()
    np.testing.assert_array_equal(axis, [729., 730., 731.])


@pytest.mark.parametrize('field, actual', [(CENTER, 1097.3969261940897), (EXPOSURE, 800.), (EPF, 4), (EXIT, 'SideExit')])
def test_settings_changed_after_prepare_block_capture_without_silent_repair(setup, field, actual):
    setup.configure_for_acquisition(center_nm=720., exposure_ms=100., frames=10)
    setup.experiment.values[field] = actual
    writes_before = list(setup.experiment.writes)
    with pytest.raises(RuntimeError):
        setup.acquire_2d()
    assert setup.experiment.captures == 0
    assert setup.experiment.writes == writes_before


def test_center_changed_during_capture_discards_the_frame(setup):
    adapter = SpectrometerLF6(setup)
    adapter.configure_for_acquisition(center_nm=720., exposure_ms=100., frames=10)
    setup.experiment.after_capture = lambda: setup.experiment.values.update({CENTER: 1100.})
    with pytest.raises(RuntimeError, match='720'):
        adapter.acquire()
    assert setup.experiment.captures == 1


def test_failed_reconfiguration_cannot_acquire_using_previous_recipe(setup):
    setup.configure_for_acquisition(center_nm=720., exposure_ms=100., frames=10)
    setup.experiment.ignored.add(CENTER)
    with pytest.raises(TimeoutError):
        setup.configure_for_acquisition(center_nm=730., exposure_ms=250., frames=3)
    with pytest.raises(RuntimeError):
        setup.acquire()
    assert setup.experiment.captures == 0


@pytest.mark.parametrize('failure', ['prepare', 'during', 'between', 'grating_between', 'axis_between', None])
def test_dual_gate_rejects_bad_pixis_frames_and_preserves_completed_rows(setup, tmp_path, monkeypatch, failure):
    from tests.test_dual_gate_multi_setup import Detector, run_worker

    prepare = Detector.prepare_scan_condition
    acquire = Detector.acquire
    adapter = SpectrometerLF6(setup)
    setup.experiment.values[SpectrometerSettings.GratingSelected] = '300'
    if failure == 'prepare':
        setup.experiment.ignored.add(CENTER)
    if failure == 'during':
        setup.experiment.after_capture = lambda: setup.experiment.values.update({CENTER: 1097.3969261940897})

    def prepare_with_sdk(self, backend, center, exposure, frames, reduction, stop):
        prepare(self, backend, center, exposure, frames, reduction, stop)
        if backend == 'lightfield':
            adapter.configure_for_acquisition(center_nm=center, exposure_ms=exposure, frames=frames)
        else:
            # WinSpec and PIXIS use the same spectrograph. Exercise the preflight
            # that leaves it at WinSpec's exit/center before the PIXIS sweep.
            setup.experiment.values.update({EXIT: 'SideExit', CENTER: center})

    def acquire_with_sdk(self):
        if self.backend != 'lightfield':
            return acquire(self)
        data = adapter.acquire()
        if failure == 'between':
            setup.experiment.values[CENTER] = 1097.3969261940897
        elif failure == 'grating_between':
            setup.experiment.values[SpectrometerSettings.GratingSelected] = '600'
        elif failure == 'axis_between':
            setup.get_wavelength_calibration = lambda: np.array([729.5, 730., 730.5])
        return data

    monkeypatch.setattr(Detector, 'prepare_scan_condition', prepare_with_sdk)
    monkeypatch.setattr(Detector, 'acquire', acquire_with_sdk)
    events, result = run_worker(tmp_path, monkeypatch)
    if failure is None:
        assert result[-1][0], result
        paths = sorted((tmp_path / 'PL').glob('*.csv'))
        assert len(paths) == 2
        for path in paths:
            with path.open(encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.reader(stream))
            assert len(rows) == 3
            assert list(map(float, rows[0][9:])) == ([729., 730., 731.] if 'PIXIS' in path.name else [1149., 1150., 1151.])
    else:
        assert not result[-1][0]
        if failure == 'grating_between':
            assert 'grating' in result[-1][1]
        elif failure == 'axis_between':
            assert 'Wavelength calibration changed' in result[-1][1]
        else:
            assert '730' in result[-1][1] and '1097.396' in result[-1][1]
        assert not any(e[:2] == ('acquire', 'winspec_ingaas') for e in events)
        paths = list((tmp_path / 'PL').glob('*.csv'))
        if failure in {'between', 'grating_between', 'axis_between'}:
            assert len(paths) == 1
            with paths[0].open(encoding='utf-8-sig', newline='') as stream:
                assert len(list(csv.reader(stream))) == 2  # Header + one verified row.
        else:
            assert not paths
        if failure == 'prepare':
            assert not any(e[0] == 'gates' for e in events)


@pytest.mark.parametrize('when', ['before', 'during'])
def test_validation_failure_is_recorded_as_failed_capture(setup, tmp_path, when):
    from app.experiment_metadata import ExperimentMetadataService

    run = ExperimentMetadataService(tmp_path, tmp_path / 'history.sqlite').begin('dual_gate_sweep', 'sample')
    setup.bind_metadata_run(run)
    setup.configure_for_acquisition(center_nm=720., exposure_ms=100., frames=10)
    if when == 'before':
        setup.experiment.values[CENTER] = 1097.3969261940897
    else:
        setup.experiment.after_capture = lambda: setup.experiment.values.update({CENTER: 1097.3969261940897})
    with pytest.raises(RuntimeError):
        setup.acquire_2d()
    events = [json.loads(line) for line in run.event_path.read_text().splitlines()]
    assert events[-1]['event'] == 'capture_failed'
    assert '720' in events[-1]['error'] and '1097.396' in events[-1]['error']
    assert not any(e['event'] == 'capture_completed' for e in events)
    run.cancel()


@pytest.mark.parametrize('name, value, key', [('change_spectra_center', 730., CENTER),
                                           ('change_expose_time', 250., EXPOSURE),
                                           ('set_accumulations', 3, EPF)])
def test_explicit_direct_setting_updates_the_recipe_used_for_capture(setup, name, value, key):
    adapter = SpectrometerLF6(setup)
    adapter.configure_for_acquisition(center_nm=720., exposure_ms=100., frames=10)
    getattr(adapter, name)(value)
    axis, _ = adapter.acquire()
    assert setup.experiment.captures == 1
    assert float(setup.experiment.values[key]) == value
    if key == CENTER:
        np.testing.assert_array_equal(axis, [729., 730., 731.])


def test_borrowed_winspec_center_write_does_not_change_pixis_recipe(setup):
    from app.devices.winspec_adapter import WinSpecSetup

    setup.configure_for_acquisition(center_nm=720., exposure_ms=100., frames=10)
    winspec = object.__new__(WinSpecSetup)
    winspec.lightfield = setup
    winspec.set_center_wavelength_when_ready(1100.)
    with pytest.raises(RuntimeError, match='720'):
        setup.acquire()
    assert setup.experiment.captures == 0


def test_public_adapter_2d_cannot_bypass_readback_checks(setup):
    adapter = SpectrometerLF6(setup)
    adapter.configure_for_acquisition(center_nm=720., exposure_ms=100., frames=10)
    setup.experiment.values[CENTER] = 1097.3969261940897
    with pytest.raises(RuntimeError, match='720'):
        adapter.acquire_2d()
    assert setup.experiment.captures == 0


@pytest.mark.parametrize('name, value, key', [('change_spectra_center', 730., CENTER),
                                           ('change_expose_time', 250., EXPOSURE),
                                           ('set_accumulations', 3, EPF)])
def test_explicit_retry_verifies_again_and_can_recover_after_failed_setting(setup, name, value, key):
    adapter = SpectrometerLF6(setup)
    adapter.configure_for_acquisition(center_nm=720., exposure_ms=100., frames=10)
    setup.experiment.ignored.add(key)
    for _ in range(2):
        with pytest.raises((RuntimeError, TimeoutError)):
            getattr(adapter, name)(value)
    setup.experiment.ignored.clear()
    getattr(adapter, name)(value)
    adapter.acquire()
    assert setup.experiment.captures == 1
