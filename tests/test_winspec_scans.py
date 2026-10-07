import csv
import threading
import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from app.devices.winspec_adapter import WinSpecSetup
from app.wavelength_calibration import fit_calibration
from controllers.lf6_controller import LF6Controller, LightFieldLifecycleState
from tests.test_winspec_adapter import Camera, Optics, performance_camera
from utils.config import cfg


@pytest.fixture(params=['legacy', 'managed'])
def scan(monkeypatch, request):
    app = QApplication.instance() or QApplication([])
    camera, optics = (performance_camera() if request.param == 'managed' else Camera()), Optics()
    optics.experiment = SimpleNamespace(ExperimentDevices=[
        SimpleNamespace(Type='Spectrometer', Model='SP', SerialNumber='123')])
    live = dict(grating='300', wavelength_nm=1050., output_port='SideExit')
    def set_center(nm, **kw):
        optics.calls.append(nm)
        live['wavelength_nm'] = nm
    optics.set_center_wavelength_when_ready = set_center
    monkeypatch.setattr('app.devices.winspec_adapter.read_optics', lambda lf: dict(live))
    monkeypatch.setattr('app.devices.winspec_adapter.ensure_output_route', lambda *a, **kw: dict(live))
    setup = WinSpecSetup(optics, client=camera)
    context = setup._calibration_context(live)
    record = fit_calibration([20, 200, 490], [1002, 1020, 1049], [300], [1030], context)
    monkeypatch.setattr(cfg.lf6, 'winspec_wavelength_calibrations', [record])
    controller = LF6Controller()
    controller._worker._backend = 'winspec_ingaas'
    controller._worker._setup = setup
    controller._worker._adapter = setup
    controller._worker._state = LightFieldLifecycleState.READY
    yield controller, setup, camera, live
    controller.shutdown()


def test_scan_prepare_and_acquire_use_matching_nm_calibration(scan):
    controller, setup, camera, live = scan
    readback = controller.configure_for_acquisition(center_nm=1050, exposure_ms=500, frames=2)
    assert readback['axis_unit'] == 'nm'
    camera.calls.clear()
    axis, counts = controller.adapter.acquire()
    assert axis[0] == pytest.approx(1002)
    assert axis[-1] == pytest.approx(1049)
    np.testing.assert_array_equal(counts, np.arange(512)[19:490]/2)
    np.testing.assert_allclose(controller.adapter.calibration_wavelengths(), axis)
    # Spectrum/calibration page still receives raw detector pixels.
    pixels, raw = setup.acquire()
    np.testing.assert_array_equal(pixels, np.arange(1, 513))
    assert len(raw) == 512
    if setup._acquisition_settings_version == 2:
        assert [command for command, _ in camera.calls] == ['ACQUIRE_GUARDED'] * 2


def test_missing_calibration_reports_requested_context_before_settings_writes(scan):
    controller, setup, camera, live = scan
    with pytest.raises(RuntimeError, match='1051.*300'):
        controller.configure_for_acquisition(center_nm=1051, exposure_ms=500, frames=2)
    assert not any(command == 'SET_SETTINGS' for command, _ in camera.calls)
    assert not setup.lightfield.calls


def test_changed_context_during_frame_never_exports_pixels_as_nm(scan):
    controller, setup, camera, live = scan
    controller.configure_for_acquisition(center_nm=1050, exposure_ms=500, frames=2)
    original = camera.request
    def request(command, *args, **kwargs):
        result = original(command, *args, **kwargs)
        if command == 'ACQUIRE_GUARDED':
            live['grating'] = '600'
        return result
    camera.request = request
    with pytest.raises(RuntimeError, match='calibration|context'):
        controller.adapter.acquire()


def test_all_planned_centers_checked_without_moving_hardware(scan):
    controller, setup, camera, live = scan
    with pytest.raises(RuntimeError, match='1051'):
        controller.validate_scan_centers([1050, 1051])
    assert not setup.lightfield.calls


def test_reversed_calibration_keeps_pixel_count_pairs(scan, monkeypatch):
    controller, setup, camera, live = scan
    record = fit_calibration([20, 200, 490], [1098, 1080, 1051], [300], [1070], setup._calibration_context(live))
    monkeypatch.setattr(cfg.lf6, 'winspec_wavelength_calibrations', [record])
    controller.configure_for_acquisition(center_nm=1050, exposure_ms=500, frames=2)
    axis, counts = controller.adapter.acquire()
    assert np.all(np.diff(axis) < 0)
    np.testing.assert_array_equal(counts, np.arange(512)[19:490]/2)


def test_megasweep_uses_nm_axis_and_propagates_context_failure(scan, monkeypatch):
    from ui.megasweep_panel import _get_wavelengths, _read_intensity
    controller, setup, camera, live = scan
    controller.configure_for_acquisition(center_nm=1050, exposure_ms=500, frames=2)
    monkeypatch.setattr('ui.megasweep_panel._wait_lambda', lambda *a: np.arange(1, 513))
    axis = _get_wavelengths(controller.adapter, setup, 1050, 1.)
    assert axis[0] == pytest.approx(1002)
    counts = _read_intensity(controller.adapter, len(axis))
    np.testing.assert_array_equal(counts, np.arange(512)[19:490]/2)
    live['grating'] = '600'
    with pytest.raises(RuntimeError, match='context'):
        _read_intensity(controller.adapter, len(axis))
    with pytest.raises(RuntimeError, match='context'):
        _get_wavelengths(controller.adapter, setup, 1050, 1.)


def test_scan_persists_frozen_calibration_metadata(scan, tmp_path):
    from app.experiment_metadata import ExperimentMetadataService
    from app.lightfield_metadata import bind_lightfield_metadata, set_lightfield_context
    controller, setup, camera, live = scan
    run = ExperimentMetadataService(tmp_path, tmp_path / 'history.sqlite').begin('scan', 'sample')
    bind_lightfield_metadata(controller, run)
    controller.configure_for_acquisition(center_nm=1050, exposure_ms=500, frames=2)
    set_lightfield_context(controller, point_index=1)
    controller.adapter.acquire()
    assert 'lightfield' in run.metadata['observed']
    snapshot = controller.adapter.read_metadata_snapshot()
    assert snapshot['calibration']['axis_unit'] == 'nm'
    assert snapshot['calibration']['context'] == setup.last_calibration_context
    assert snapshot['calibration']['record'] == cfg.lf6.winspec_wavelength_calibrations[0]
    assert 'fixed_position_calibration' in run.path.read_text(encoding='utf-8')
    import json
    events = [json.loads(line) for line in run.event_path.read_text(encoding='utf-8').splitlines()]
    completed = next(e for e in events if e['event'] == 'capture_completed')
    observation = completed.get('frame_observation', completed.get('details', {}).get('frame_observation'))
    assert observation['intensity_processing']['accumulations'] == 2
    assert observation['raw_accumulated_counts'][-1] == 511


def test_scan_records_recipe_drift_as_failed_capture(scan, tmp_path):
    import json
    from app.experiment_metadata import ExperimentMetadataService
    from app.lightfield_metadata import bind_lightfield_metadata

    controller, setup, camera, live = scan
    run = ExperimentMetadataService(tmp_path, tmp_path / 'history.sqlite').begin('scan', 'sample')
    bind_lightfield_metadata(controller, run)
    controller.configure_for_acquisition(center_nm=1050, exposure_ms=500, frames=2)
    camera.settings['exposure_ms'] = 800.
    with pytest.raises(RuntimeError, match='exposure'):
        controller.adapter.acquire()
    events = [json.loads(line) for line in run.event_path.read_text(encoding='utf-8').splitlines()]
    assert events[-1]['event'] == 'capture_failed'
    assert not any(e['event'] == 'capture_completed' for e in events)
    run.cancel()


@pytest.mark.parametrize('center,success', [(1050, True), (1051, False)])
def test_presets_run_writes_calibrated_csv_or_stops_before_voltage(scan, tmp_path, monkeypatch, center, success):
    import pandas as pd
    from ui.presets_panel import _RunWorker
    from tests.test_smu_resilience import _HealthyRunDevice, SMUResilienceTests
    controller, setup, camera, live = scan
    device = _HealthyRunDevice()
    commands = []
    original = device.set_gates
    def set_gates(*args, **kwargs):
        commands.append((args, kwargs))
        return original(*args, **kwargs)
    device.set_gates = set_gates
    meta = {**SMUResilienceTests._run_meta(), 'voltage_settle_s': 0, 'initial_voltage_settle_s': 0}
    worker = _RunWorker(
        [{'Center Wavelength (nm)': center, 'Exposure Time (ms)': 500, 'Accumulations (EPF)': 2}],
        pd.DataFrame([SMUResilienceTests._batch_row()]), lf6_ctrl=controller,
        smu_ctrl=SimpleNamespace(is_connected=True, device=device), out_dir=tmp_path,
        run_meta=meta, filename_parts=['device_id'], stop_event=threading.Event())
    finished = []
    worker.finished.connect(lambda *args: finished.append(args))
    monkeypatch.setattr(cfg.ramp, 'delay_s', 0.)
    worker.run()
    assert finished[-1][0] == success, finished
    if success:
        with next(tmp_path.rglob('*.csv')).open(encoding='utf-8', newline='') as stream:
            rows = list(csv.reader(stream))
        assert float(rows[0][-1]) == pytest.approx(1049)
        assert float(rows[1][-1]) == 244.5
    else:
        assert not commands
        assert not any(command == 'ACQUIRE_GUARDED' for command, _ in camera.calls)
