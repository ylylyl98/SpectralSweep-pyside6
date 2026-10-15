"""Exercise real sweep engines and routing controllers with simulated USB/SDK IO."""
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from controllers.lf6_controller import LF6Controller, LightFieldLifecycleState
from controllers.mff101_controller import MFF101Controller
from tests.test_mff101_control import Device, app  # noqa: F401
from utils.config import MFF101Config, cfg


class Detector:
    """SDK boundary recording the physical endpoint at each captured frame."""
    is_ready = True
    is_busy = False

    def __init__(self, backend, flipper, captures):
        self.backend, self.flipper, self.captures = backend, flipper, captures
        self.center = 730.
        self.setup = self
        self.fail_next_flip = False

    def configure_for_acquisition(self, *, center_nm, exposure_ms, frames):
        self.center = center_nm
        return dict(center_nm=center_nm, exposure_ms=exposure_ms, frames=frames)

    def calibration_wavelengths(self, **_):
        return np.array([self.center - 1, self.center, self.center + 1])

    get_wavelength_calibration = calibration_wavelengths

    def acquire(self):
        self.captures.append((self.backend, self.flipper.position))
        if self.fail_next_flip:
            self.flipper.fail_move = True
        return self.calibration_wavelengths(), np.ones(3)

    def invalidate_wavelengths(self):
        pass

    def close(self):
        pass


@pytest.fixture
def measurement(app, monkeypatch):
    monkeypatch.setattr(cfg, 'mff101', MFF101Config(auto_route=True))
    monkeypatch.setattr(cfg.ramp, 'delay_s', 0.)
    usb = Device()
    flipper = MFF101Controller(adapter_factory=usb.open, scan_func=lambda: ['37862922'])
    controller = LF6Controller()
    controller.set_flipper_controller(flipper)
    captures = []
    def install(andor_backend):
        lightfield = Detector('lightfield', usb, captures)
        andor = Detector(andor_backend, usb, captures)
        worker = controller._worker
        worker._setup = worker._adapter = lightfield
        worker._identity = {'backend': 'lightfield'}
        worker._state = LightFieldLifecycleState.READY
        worker._parked[andor_backend] = (andor, andor, {'backend': 'andor_sdk2'}, [])
        return lightfield
    yield SimpleNamespace(ctrl=controller, usb=usb, flipper=flipper, captures=captures, install=install)
    controller.shutdown()
    assert flipper.shutdown()
    app.processEvents()


def run_dual_gate(measurement, backend, out_path, *, inner=False):
    from tests.test_dual_gate_multi_setup import loops, batch, Device as SMU
    from tests.test_smu_resilience import SMUResilienceTests
    from ui import presets_panel as panel
    definition = loops()
    definition.loc[0, 'Values'] = f'PIXIS, {backend}'
    sequence, rows, _ = panel._build_plan(definition, batch(), 'Customized')
    order = ['conditions', 'points', 'group:1'] if inner else ['group:1', 'conditions', 'points']
    schedule = panel._build_nested_execution_schedule(definition, batch(), mode='Customized', execution_order=order)
    metadata = SMUResilienceTests._run_meta()
    metadata.update(initial_voltage_settle_s=0., voltage_settle_s=0.)
    worker = panel._RunWorker(sequence, rows, lf6_ctrl=measurement.ctrl,
        smu_ctrl=SimpleNamespace(is_connected=True, device=SMU([])), out_dir=out_path,
        run_meta=metadata, filename_parts=['center'], stop_event=threading.Event(),
        acquisition_schedule=schedule)
    finished = []
    worker.finished.connect(lambda *args: finished.append(args))
    measurement.ctrl.set_temperature_monitor_paused('presets', True)
    worker.run()
    assert finished
    return finished[-1]


def run_maps(measurement, backend, out_path):
    from tests.test_megasweep_sequence import _params, _FakeSMUController
    from ui.megasweep_panel import _MegaSweepWorker, OpticalCondition
    params = _params(out_path)
    params['optical_conditions'] = [
        OpticalCondition(True, 'PIXIS', 730, 100, 2, 'lightfield', 'device').as_dict(),
        OpticalCondition(True, 'Andor', 1150, 500, 3, backend, 'device').as_dict(),
    ]
    measurement.ctrl.set_temperature_monitor_paused('megasweep', True)
    _MegaSweepWorker(params, _FakeSMUController(), measurement.ctrl)._run_sweep(params)


@pytest.mark.parametrize('backend', ['andor_si', 'andor_ingaas'])
@pytest.mark.parametrize('flow', ['dual_gate', 'inner_setup', 'maps'])
@pytest.mark.parametrize('reversed_mapping', [False, True])
def test_measurements_route_each_setup_before_capturing(measurement, tmp_path, backend, flow, reversed_mapping):
    measurement.install(backend)
    if reversed_mapping:
        cfg.mff101.lightfield_position, cfg.mff101.andor_position = 1, 2
    if flow == 'maps':
        run_maps(measurement, backend, tmp_path)
    else:
        result = run_dual_gate(measurement, backend, tmp_path, inner=flow == 'inner_setup')
        assert result[0], result
    lf_position, andor_position = (1, 2) if reversed_mapping else (2, 1)
    expected = ([('lightfield', lf_position), (backend, andor_position)] * 2
                if flow == 'inner_setup' else
                [('lightfield', lf_position)] * 2 + [(backend, andor_position)] * 2)
    assert measurement.captures == expected
    assert not measurement.flipper.move_to(lf_position)
    files = list(tmp_path.rglob('*.csv'))
    assert len(files) == 2


@pytest.mark.parametrize('flow', ['dual_gate', 'maps'])
def test_route_failure_during_measurement_blocks_next_setup(measurement, tmp_path, flow):
    lightfield = measurement.install('andor_si')
    lightfield.fail_next_flip = True
    if flow == 'maps':
        with pytest.raises(RuntimeError, match='target'):
            run_maps(measurement, 'andor_si', tmp_path)
    else:
        result = run_dual_gate(measurement, 'andor_si', tmp_path)
        assert not result[0]
        assert 'target' in str(result)
    assert measurement.captures == [('lightfield', 2)] * 2
    assert measurement.flipper.state['position'] is None


@pytest.mark.parametrize('flow', ['dual_gate', 'inner_setup', 'maps'])
def test_measurements_without_flipper_capture_all_setups(measurement, tmp_path, monkeypatch, flow):
    monkeypatch.setattr(cfg, 'mff101', MFF101Config())
    measurement.install('andor_si')

    def unavailable():
        raise ImportError('No flipper driver installed on this PC')

    measurement.flipper._worker._scan = unavailable
    if flow == 'maps':
        run_maps(measurement, 'andor_si', tmp_path)
    else:
        result = run_dual_gate(measurement, 'andor_si', tmp_path, inner=flow == 'inner_setup')
        assert result[0], result
    expected = (['lightfield', 'andor_si'] * 2 if flow == 'inner_setup' else
                ['lightfield'] * 2 + ['andor_si'] * 2)
    assert [backend for backend, _ in measurement.captures] == expected
    assert measurement.usb.calls == []
    assert not measurement.flipper.state['error']
