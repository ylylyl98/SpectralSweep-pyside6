"""Setup selection must reach the configured optical path before acquisition."""
import threading

import pytest

from tests.test_mff101_control import Device, wait_for
from tests.test_mff101_control import app  # noqa: F401
from utils.config import AppConfig, MFF101Config, cfg


@pytest.fixture
def routing(app, monkeypatch):
    from controllers.mff101_controller import MFF101Controller
    monkeypatch.setattr(cfg, 'mff101', MFF101Config(auto_route=True))
    device = Device()
    ctrl = MFF101Controller(adapter_factory=device.open, scan_func=lambda: ['37862922'])
    yield ctrl, device
    device.release.set()
    device.fail_close = False
    assert ctrl.shutdown()
    app.processEvents()


def moves(device):
    return [action for action, _ in device.calls if isinstance(action, tuple) and action[0] == 'move']


@pytest.mark.parametrize('auto_route', [False, True])
def test_mapping_defaults_and_round_trip(tmp_path, auto_route):
    config = AppConfig()
    assert not config.mff101.auto_route
    assert config.mff101.andor_position == 1
    assert config.mff101.lightfield_position == 2
    config.mff101.auto_route = auto_route
    config.mff101.andor_position, config.mff101.lightfield_position = 2, 1
    path = tmp_path / 'config.json'
    config.save(path)
    loaded = AppConfig()
    loaded.load(path)
    assert loaded.mff101.auto_route is auto_route
    assert (loaded.mff101.andor_position, loaded.mff101.lightfield_position) == (2, 1)


@pytest.mark.parametrize('source', ['missing', 'project', 'legacy'])
def test_new_pc_and_legacy_configs_do_not_require_flipper(tmp_path, monkeypatch, source):
    import utils.config as config_module

    path = tmp_path / 'user' / 'config.json'
    monkeypatch.setattr(config_module, '_CONFIG_FILE', path)
    if source != 'project':
        monkeypatch.setattr(config_module, '_PROJECT_CONFIG_FILE', tmp_path / 'missing.json')
    if source == 'legacy':
        path.parent.mkdir()
        path.write_text('{"mff101": {"serial_number": "37862922"}}', encoding='utf-8')
    config = AppConfig()
    config.load()
    assert not config.mff101.auto_route


@pytest.fixture
def no_flipper(app, monkeypatch):
    from controllers.mff101_controller import MFF101Controller

    monkeypatch.setattr(cfg, 'mff101', MFF101Config())
    calls = []

    def unavailable(*args):
        calls.append(args)
        raise ImportError('No flipper driver installed on this PC')

    ctrl = MFF101Controller(adapter_factory=unavailable, scan_func=unavailable)
    try:
        yield ctrl, calls
    finally:
        assert ctrl.shutdown()
        app.processEvents()


@pytest.mark.parametrize('backend', ['lightfield', 'winspec_ingaas', 'andor_si', 'andor_ingaas'])
def test_default_setup_selection_and_preflight_never_access_flipper(app, no_flipper, backend):
    ctrl, calls = no_flipper
    assert ctrl.route_for_setup(backend, use_mock=False)
    wait_for(app, lambda: not ctrl.is_busy)
    ctrl.ensure_setup_position(backend, use_mock=False)
    assert calls == []
    assert not ctrl.state['error']
    assert not ctrl.is_connected


def test_real_spectrometer_connection_and_capture_without_flipper(app, no_flipper, monkeypatch):
    import sys
    from types import SimpleNamespace
    import numpy as np
    from controllers.lf6_controller import LF6Controller

    flipper, calls = no_flipper
    sensor_checks = []
    def prepare_sensor_readout(intent, *, apply=True, **kwargs):
        sensor_checks.append((intent['mode'], apply))
        return {'fingerprint': 'fake-lightfield-bin-all', 'changed': False}
    setup = SimpleNamespace(close=lambda: None, prepare_sensor_readout=prepare_sensor_readout)
    adapter = SimpleNamespace(acquire=lambda: (np.array([730.]), np.array([1.])))
    monkeypatch.setitem(sys.modules, 'lf6_automation', SimpleNamespace(LF6Setup=lambda: setup))
    # Install the SDK double before importing its adapter on machines that do
    # not have LightField/.NET vendor assemblies installed (including CI).
    from app.devices import lf6_adapter
    monkeypatch.setattr(lf6_adapter, 'SpectrometerLF6', lambda _: adapter)
    spec = LF6Controller()
    spec.set_flipper_controller(flipper)
    errors, spectra = [], []
    spec.error.connect(errors.append)
    spec.spectrum_ready.connect(lambda *data: spectra.append(data))
    try:
        spec.connect_instrument(use_mock=False, backend='lightfield')
        wait_for(app, lambda: spec.is_connected or bool(errors))
        assert not errors
        assert spec.is_connected
        spec.acquire_single()
        wait_for(app, lambda: bool(spectra) or bool(errors))
        assert not errors
        assert len(spectra) == 1
        assert calls == []
        assert sensor_checks == [('bin_all', True), ('bin_all', False)]
    finally:
        spec.shutdown()


def test_selection_autoconnects_routes_and_verifies_without_redundant_move(app, routing):
    ctrl, device = routing
    assert ctrl.route_for_setup('lightfield')
    wait_for(app, lambda: not ctrl.is_busy)
    assert ctrl.state['position'] == 2
    assert ctrl.state['serial_number'] == '37862922'
    assert ctrl.route_for_setup('winspec_ingaas')
    wait_for(app, lambda: not ctrl.is_busy)
    assert moves(device) == [('move', 2)]
    assert ctrl.route_for_setup('andor_ingaas')
    wait_for(app, lambda: not ctrl.is_busy)
    assert ctrl.state['position'] == 1


def test_disabled_and_mock_selection_never_open_hardware(app, routing):
    ctrl, device = routing
    assert ctrl.route_for_setup('lightfield', use_mock=True)
    cfg.mff101.auto_route = False
    assert ctrl.route_for_setup('andor_si')
    assert device.calls == []


def test_reversed_mapping_does_not_parse_descriptive_names(app, routing):
    ctrl, device = routing
    cfg.mff101.andor_position, cfg.mff101.lightfield_position = 2, 1
    cfg.mff101.position_1_path = 'Camera A'
    assert ctrl.route_for_setup('andor_si')
    wait_for(app, lambda: not ctrl.is_busy)
    assert device.position == 2


def test_blocking_preflight_completes_without_gui_pumping(app, routing):
    ctrl, device = routing
    # The spectrometer worker must not require GUI delivery to finish a route.
    errors = []
    def run():
        try:
            ctrl.ensure_setup_position('lightfield')
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    thread.join(3)
    assert not thread.is_alive()
    assert not errors
    assert device.position == 2
    assert len({owner for _, owner in device.calls}) == 1


def test_failed_motion_rejects_preflight(routing):
    ctrl, device = routing
    device.fail_move = True
    with pytest.raises(RuntimeError, match='target'):
        ctrl.ensure_setup_position('lightfield')
    assert ctrl.state['position'] is None


def test_ambiguous_device_requires_explicit_serial(app, routing):
    ctrl, device = routing
    ctrl._worker._scan = lambda: ['37862922', '37000001']
    assert ctrl.route_for_setup('lightfield')
    wait_for(app, lambda: not ctrl.is_busy)
    assert 'serial' in ctrl.state['error'].lower()
    assert device.calls == []


def test_setup_combo_activation_routes_but_restore_and_measurement_do_not(app, routing):
    from controllers.lf6_controller import LF6Controller
    from ui.instrument_panel import _LF6Section
    flipper, device = routing
    spectrometer = LF6Controller()
    spectrometer.set_flipper_controller(flipper)
    section = _LF6Section(spectrometer)
    try:
        section._mock_chk.setChecked(False)
        section._backend.setCurrentIndex(section._backend.findData('lightfield'))
        assert device.calls == []
        section._backend.activated.emit(section._backend.currentIndex())
        wait_for(app, lambda: not flipper.is_busy and not spectrometer.switching_locked)
        assert device.position == 2
        spectrometer.set_temperature_monitor_paused('presets', True)
        assert not flipper.move_to(1)
        assert not spectrometer.select_setup_path('andor_si')
        assert not section._backend.isEnabled()
        assert device.position == 2
        spectrometer.set_temperature_monitor_paused('presets', False)
        section._backend.setCurrentIndex(section._backend.findData('andor_si'))
        section._backend.activated.emit(section._backend.currentIndex())
        wait_for(app, lambda: not flipper.is_busy)
        assert device.position == 1
    finally:
        spectrometer.shutdown()
        section.close()


def test_preflight_failure_prevents_detector_acquisition(app, routing):
    from types import SimpleNamespace
    from controllers.lf6_controller import LF6Controller, LightFieldLifecycleState
    flipper, device = routing
    spectrometer = LF6Controller()
    spectrometer.set_flipper_controller(flipper)
    captures = []
    errors = []
    worker = spectrometer._worker
    worker._setup = SimpleNamespace(close=lambda: None)
    worker._adapter = SimpleNamespace(acquire=lambda: captures.append('frame'))
    worker._state = LightFieldLifecycleState.READY
    worker._identity = {'backend': 'lightfield'}
    spectrometer.error.connect(errors.append)
    device.fail_move = True
    try:
        spectrometer.acquire_single()
        wait_for(app, lambda: bool(errors) and spectrometer._acquisition_requests == 0)
        assert not captures
        assert 'target' in errors[0]
    finally:
        spectrometer.shutdown()


def test_existing_mock_identity_overrides_unchecked_simulation_checkbox(app, routing):
    from controllers.lf6_controller import LF6Controller
    flipper, device = routing
    spectrometer = LF6Controller()
    spectrometer.set_flipper_controller(flipper)
    try:
        spectrometer.connect_instrument(use_mock=True, backend='lightfield')
        wait_for(app, lambda: spectrometer.is_connected)
        assert spectrometer.select_setup_path('lightfield', use_mock=False)
        spectrometer.ensure_light_path()
        assert device.calls == []
    finally:
        spectrometer.shutdown()


def test_settings_mapping_edits_are_passive_and_persist(app, routing, tmp_path, monkeypatch):
    import utils.config as config_module
    from ui.settings_panel import SettingsPanel
    flipper, device = routing
    path = tmp_path / 'config.json'
    monkeypatch.setattr(config_module, '_CONFIG_FILE', path)
    settings = SettingsPanel()
    try:
        settings._mff101_andor_position.setCurrentIndex(1)
        settings._mff101_lightfield_position.setCurrentIndex(0)
        settings._save_btn.click()
        assert device.calls == []
        loaded = AppConfig()
        loaded.load(path)
        assert (loaded.mff101.andor_position, loaded.mff101.lightfield_position) == (2, 1)
        assert flipper.route_for_setup('andor_si')
        wait_for(app, lambda: not flipper.is_busy)
        assert device.position == 2
    finally:
        settings.close()


def test_shutdown_rejects_late_preflight_and_finishes_accepted_route(app, routing):
    ctrl, device = routing
    device.release.clear()
    assert ctrl.route_for_setup('lightfield')
    release = threading.Timer(.05, device.release.set)
    release.start()
    try:
        assert ctrl.shutdown()
        with pytest.raises(RuntimeError, match='closing'):
            ctrl.ensure_setup_position('andor_si')
        assert moves(device) == [('move', 2)]
    finally:
        release.join()


def test_stop_during_an_earlier_move_cancels_queued_setup_route(app, routing):
    ctrl, device = routing
    device.release.clear()
    assert ctrl.route_for_setup('lightfield')
    stop = threading.Event()
    errors = []
    def preflight():
        try:
            ctrl.ensure_setup_position('andor_si', stop_event=stop)
        except Exception as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=preflight)
    thread.start()
    stop.set()
    device.release.set()
    thread.join(3)
    assert not thread.is_alive()
    assert errors and 'stopped' in errors[0]
    assert moves(device) == [('move', 2)]


def test_configured_serial_never_falls_back_to_another_device(app, routing):
    ctrl, device = routing
    cfg.mff101.serial_number = '37000009'
    def open_failed(serial):
        assert serial == '37000009'
        raise OSError('Configured flipper not found')
    ctrl._worker._factory = open_failed
    assert ctrl.route_for_setup('lightfield')
    wait_for(app, lambda: not ctrl.is_busy)
    assert 'Configured flipper not found' in ctrl.state['error']
    assert device.calls == []


def test_readback_must_match_target_even_if_driver_returns_success(routing):
    ctrl, device = routing
    device.move_to = lambda position, **kwargs: position
    with pytest.raises(RuntimeError, match='did not reach position 2'):
        ctrl.ensure_setup_position('lightfield')
    assert ctrl.state['position'] is None


def test_failed_connection_preflight_preserves_active_backend(app, routing):
    from controllers.lf6_controller import LF6Controller
    ctrl, device = routing
    spec = LF6Controller()
    spec.set_flipper_controller(ctrl)
    errors = []
    spec.error.connect(errors.append)
    try:
        spec.connect_instrument(use_mock=True, backend='andor_si')
        wait_for(app, lambda: spec.is_connected)
        original = spec.adapter
        device.fail_move = True
        spec.connect_instrument(use_mock=False, backend='lightfield')
        wait_for(app, lambda: bool(errors))
        assert spec.backend == 'andor_si'
        assert spec.adapter is original
        assert 'routing failed' in errors[0]
    finally:
        spec.shutdown()


def test_scan_routes_before_configuration_and_keeps_previous_setup_on_failure(app, routing):
    from types import SimpleNamespace
    from controllers.lf6_controller import LF6Controller, LightFieldLifecycleState
    flipper, device = routing
    spec = LF6Controller()
    spec.set_flipper_controller(flipper)
    observed = []
    def session(backend):
        setup = SimpleNamespace(close=lambda: None, configure_for_acquisition=lambda **kw: observed.append((backend, device.position)))
        adapter = SimpleNamespace(invalidate_wavelengths=lambda: None)
        return setup, adapter, {'backend': backend}, []
    worker = spec._worker
    worker._setup, worker._adapter, worker._identity, worker._experiments = session('lightfield')
    worker._state = LightFieldLifecycleState.READY
    worker._parked['andor_si'] = session('andor_sdk2')
    spec.set_temperature_monitor_paused('presets', True)
    try:
        spec.prepare_scan_condition('lightfield', 700, 1, 1, 'device', threading.Event())
        assert observed == [('lightfield', 2)]
        device.fail_move = True
        with pytest.raises(RuntimeError, match='target'):
            spec.prepare_scan_condition('andor_si', 700, 1, 1, 'device', threading.Event())
        assert spec.backend == 'lightfield'
        assert len(observed) == 1
        device.fail_move = False
        spec.prepare_scan_condition('andor_si', 700, 1, 1, 'device', threading.Event())
        assert observed[-1] == ('andor_sdk2', 1)
        assert moves(device) == [('move', 2), ('move', 1), ('move', 1)]
    finally:
        spec.shutdown()


def test_legacy_power_sweep_does_not_swallow_route_failure(app, tmp_path):
    from controllers.mff101_controller import MFF101RoutingError
    from tests.test_motion_sweep import _worker_params, _FakeStageController, _FakeLF6Controller
    from ui.power_sweep_panel import _PowerSweepWorker
    stage, spec = _FakeStageController(), _FakeLF6Controller()
    spec.setup = object()
    captures = []
    original = spec.adapter.acquire
    def acquire():
        captures.append('frame')
        return original()
    spec.adapter.acquire = acquire
    def fail(**_):
        raise MFF101RoutingError('flipper did not reach target')
    spec.configure_for_acquisition = fail
    params = _worker_params(str(tmp_path), 'stage')
    params['plan_schema'] = 'legacy'
    worker = _PowerSweepWorker(params, stage, None, None, spec, None)
    with pytest.raises(MFF101RoutingError, match='target'):
        worker._run_sweep(params)
    assert captures == []
    assert stage.adapter.moves == []
