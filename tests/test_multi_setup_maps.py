import os
import threading
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from ui.megasweep_panel import MegaSweepPanel, OpticalCondition
from tests.test_winspec_scans import scan


def test_recipe_roundtrip_preserves_setup_and_average():
    app = QApplication.instance() or QApplication([])
    panel = MegaSweepPanel()
    restored = MegaSweepPanel()
    try:
        panel._optical_widget.set_conditions([
            dict(enabled=True, name='PIXIS', backend='lightfield', center_nm=650, exposure_ms=80, frames=4, reduction='average'),
            dict(enabled=True, name='WinSpec', backend='winspec_ingaas', center_nm=1050, exposure_ms=250, frames=4, reduction='average')])
        restored.restore_session_state(panel.capture_session_state())
        rows = [item.as_dict() for item in restored._optical_widget.conditions()]
        assert [r.get('backend') for r in rows] == ['lightfield', 'winspec_ingaas']
        assert [r.get('reduction') for r in rows] == ['average', 'average']
        restored._optical_widget._duplicate_selected()
        assert restored._optical_widget.conditions()[1].backend == 'lightfield'
    finally:
        panel.close(); restored.close()


def test_four_independent_frames_are_averaged_not_summed():
    from app.devices.scan_average import AveragedScanAdapter
    calls = []
    def acquire():
        calls.append(True)
        return np.array([650., 651.]), np.array([len(calls), 2 * len(calls)])
    adapter = AveragedScanAdapter(SimpleNamespace(acquire=acquire), 4, threading.Event())
    x, y = adapter.acquire()
    np.testing.assert_array_equal(x, [650., 651.])
    np.testing.assert_allclose(y, [2.5, 5.])
    assert len(calls) == 4


def test_average_stops_between_frames_and_rejects_changed_axis():
    from app.devices.scan_average import AveragedScanAdapter
    stop = threading.Event()
    def acquire():
        stop.set()
        return np.array([650.]), np.array([1.])
    with pytest.raises(RuntimeError, match='stopped'):
        AveragedScanAdapter(SimpleNamespace(acquire=acquire), 4, stop).acquire()
    stop.clear()
    axes = iter([650., 651.])
    def drift():
        return np.array([next(axes)]), np.array([1.])
    with pytest.raises(RuntimeError, match='axis'):
        AveragedScanAdapter(SimpleNamespace(acquire=drift), 2, stop).acquire()


def test_scan_session_switches_only_connected_backends_without_unlocking():
    from controllers.lf6_controller import LF6Controller, LightFieldLifecycleState
    from unittest.mock import Mock
    app = QApplication.instance() or QApplication([])
    ctrl = LF6Controller()
    a = SimpleNamespace(is_busy=False, is_ready=True, configure_for_acquisition=Mock(return_value={}),
                        readback_online_process=lambda: {'exposures_per_frame': 1})
    adapter = SimpleNamespace(invalidate_wavelengths=lambda: None)
    ctrl._worker._setup = a
    ctrl._worker._adapter = adapter
    ctrl._worker._backend = 'lightfield'
    ctrl._worker._identity = {'backend': 'mock_lightfield'}
    ctrl._worker._state = LightFieldLifecycleState.READY
    try:
        with pytest.raises(RuntimeError, match='measurement'):
            ctrl.prepare_scan_condition('lightfield', 650, 80, 4, 'average', threading.Event())
        ctrl.set_temperature_monitor_paused('megasweep', True)
        result = ctrl.prepare_scan_condition('lightfield', 650, 80, 4, 'average', threading.Event())
        a.configure_for_acquisition.assert_called_once_with(center_nm=650, exposure_ms=80, frames=1)
        assert result.frames == 4
        assert ctrl.switching_locked
        assert ctrl._worker.temperature_monitor_paused.is_set()
        with pytest.raises(RuntimeError, match='connect'):
            ctrl.prepare_scan_condition('winspec_ingaas', 1050, 250, 4, 'average', threading.Event())
        assert ctrl.backend == 'lightfield'
    finally:
        ctrl.shutdown()


def test_two_setups_repeat_same_grid_with_four_exposures_and_separate_files(tmp_path):
    from tests.test_megasweep_sequence import _params, _FakeSMUController
    from ui.megasweep_panel import _MegaSweepWorker
    from app.devices.scan_average import AveragedScanAdapter
    smu = _FakeSMUController()
    calls = []
    class Controller:
        is_connected = True
        backend = 'lightfield'
        connected_backends = ('lightfield', 'winspec_ingaas')
        adapter = setup = None
        def prepare_scan_condition(self, backend, center, exposure, frames, reduction, stop):
            assert smu.device.gates == (0., 0.)
            self.backend = backend
            self.setup = SimpleNamespace()
            def acquire():
                calls.append((backend, smu.device.gates, exposure))
                return np.array([center-1, center, center+1]), np.ones(3) * len(calls)
            raw = SimpleNamespace(acquire=acquire, calibration_wavelengths=lambda: np.array([center-1, center, center+1]))
            self.adapter = AveragedScanAdapter(raw, frames, stop)
            return self.adapter
    ctrl = Controller()
    p = _params(tmp_path)
    p['optical_conditions'] = [
        OpticalCondition(True, 'PIXIS', 650, 80, 4, 'lightfield', 'average').as_dict(),
        OpticalCondition(True, 'WinSpec', 1050, 250, 4, 'winspec_ingaas', 'average').as_dict()]
    worker = _MegaSweepWorker(p, smu, ctrl)
    worker._run_sweep(p)
    assert [x[0] for x in calls] == ['lightfield', 'winspec_ingaas'] + ['lightfield'] * 8 + ['winspec_ingaas'] * 8
    assert [x[1] for x in calls[2:10]] == [x[1] for x in calls[10:]]
    assert smu.device.zero_ramps == 2
    files = sorted(tmp_path.glob('*.csv'))
    assert len(files) == 2
    assert 'Avg4' in files[0].name
    first = np.loadtxt(files[0], delimiter=',', skiprows=1)
    np.testing.assert_allclose(first[:, -1], [4.5, 8.5])


def test_validation_uses_actual_trimmed_axis_and_rejects_image():
    from app.devices.scan_average import AveragedScanAdapter
    raw = SimpleNamespace(calibration_wavelengths=lambda **kw: np.arange(6.),
                          acquire=lambda: (np.array([650., 651., 652.]), np.ones(3)))
    averaged = AveragedScanAdapter(raw, 4, threading.Event())
    averaged.validate_spectrum()
    np.testing.assert_array_equal(averaged.calibration_wavelengths(), [650, 651, 652])
    raw.acquire = lambda: (np.array([650., 651., 652.]), np.ones((2, 3)))
    with pytest.raises(RuntimeError, match='one-dimensional'):
        averaged.validate_spectrum()


def test_real_controller_switches_pixis_winspec_and_keeps_temperature_paused(scan, tmp_path):
    from tests.test_megasweep_sequence import _params, _FakeSMUController
    from ui.megasweep_panel import _MegaSweepWorker
    ctrl, winspec, camera, live = scan
    optics = winspec.lightfield
    pixis_calls = []
    def configure(**kwargs):
        live['wavelength_nm'] = kwargs['center_nm']
        pixis_calls.append(kwargs)
        return kwargs
    optics.configure_for_acquisition = configure
    optics.readback_online_process = lambda: {'exposures_per_frame': 1}
    pixis = SimpleNamespace(
        invalidate_wavelengths=lambda: None,
        calibration_wavelengths=lambda **kw: np.array([649., 650., 651.]),
        acquire=lambda: (np.array([649., 650., 651.]), np.ones(3)))
    ctrl._worker._parked['lightfield'] = (optics, pixis, {'backend': 'mock_lightfield'}, [])
    routes = []
    ctrl._worker._route_selected_detector = lambda setup, *_args: routes.append(setup is optics)
    p = _params(tmp_path)
    panel = MegaSweepPanel()
    try:
        panel._optical_widget._load_dual_setup_recipe()
        p['optical_conditions'] = [condition.as_dict() for condition in panel._optical_widget.conditions()]
    finally:
        panel.close()
    ctrl.set_temperature_monitor_paused('megasweep', True)
    try:
        smu = _FakeSMUController()
        _MegaSweepWorker(p, smu, ctrl)._run_sweep(p)
        assert ctrl.backend == 'winspec_ingaas'
        assert ctrl.temperature_monitor_paused
        assert all(c['frames'] == 1 and c['exposure_ms'] == 80 for c in pixis_calls)
        assert camera.settings['accumulations'] == 4
        assert camera.settings['exposure_ms'] == 250
        assert sum(c == 'ACQUIRE_GUARDED' for c, _ in camera.calls) == 2
        winspec_file = next(path for path in tmp_path.glob('*.csv') if 'WinSpec' in path.name)
        np.testing.assert_allclose(np.loadtxt(winspec_file, delimiter=',', skiprows=1)[:, -1], [122.25, 122.25])
        assert len(list(tmp_path.glob('*.csv'))) == 2
        assert routes == [True, False, True, False]
    finally:
        ctrl.set_temperature_monitor_paused('megasweep', False)


def test_invalid_image_preflight_never_commands_gates(tmp_path):
    from tests.test_megasweep_sequence import _params, _FakeSMUController
    from ui.megasweep_panel import _MegaSweepWorker
    from app.devices.scan_average import AveragedScanAdapter
    from unittest.mock import Mock
    smu = _FakeSMUController()
    smu.device.set_gates = Mock()
    raw = SimpleNamespace(acquire=lambda: (np.array([649., 650., 651.]), np.ones((2, 3))))
    ctrl = SimpleNamespace(is_connected=True, backend='lightfield', connected_backends=('lightfield',),
                           adapter=raw, setup=None,
                           prepare_scan_condition=lambda *args: AveragedScanAdapter(raw, 4, args[-1]))
    p = _params(tmp_path)
    p['optical_conditions'] = [OpticalCondition(True, 'PIXIS', 650, 80, 4, 'lightfield', 'average').as_dict()]
    with pytest.raises(RuntimeError, match='one-dimensional'):
        _MegaSweepWorker(p, smu, ctrl)._run_sweep(p)
    smu.device.set_gates.assert_not_called()
    assert not list(tmp_path.glob('*.csv'))


def test_stop_during_average_discards_partial_point_and_skips_next_map(tmp_path):
    from tests.test_megasweep_sequence import _params, _FakeSMUController
    from ui.megasweep_panel import _MegaSweepWorker
    from app.devices.scan_average import AveragedScanAdapter
    smu = _FakeSMUController()
    calls = []
    ctrl = SimpleNamespace(is_connected=True, backend='lightfield', connected_backends=('lightfield', 'winspec_ingaas'),
                           adapter=None, setup=None)
    def prepare(backend, center, exposure, frames, reduction, stop):
        def acquire():
            calls.append(backend)
            if len(calls) == 3:  # Two validation frames, then partial first point.
                stop.set()
            return np.array([center-1, center, center+1]), np.ones(3)
        return AveragedScanAdapter(SimpleNamespace(acquire=acquire), frames, stop)
    ctrl.prepare_scan_condition = prepare
    p = _params(tmp_path)
    p['optical_conditions'] = [
        OpticalCondition(True, 'PIXIS', 650, 80, 4, 'lightfield', 'average').as_dict(),
        OpticalCondition(True, 'WinSpec', 1050, 250, 4, 'winspec_ingaas', 'average').as_dict()]
    _MegaSweepWorker(p, smu, ctrl)._run_sweep(p)
    assert calls == ['lightfield', 'winspec_ingaas', 'lightfield']
    assert smu.device.zero_ramps == 1
    files = list(tmp_path.glob('*.csv'))
    assert len(files) == 1 and len(files[0].read_text().splitlines()) == 1
    assert '# Status: Stopped' in next(tmp_path.glob('*.meta.txt')).read_text()
