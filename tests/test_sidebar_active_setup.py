"""The sidebar follows scan-owned setup changes without issuing commands."""
import threading
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from tests.test_winspec_scans import scan
from ui.instrument_panel import _LF6Section


@pytest.fixture
def sidebar(scan, monkeypatch):
    from app.devices import lightfield_optics

    controller, winspec, camera, live = scan
    optics = winspec.lightfield
    def configure(**values):
        lightfield_optics.ensure_output_route(optics, 'front')
        live['wavelength_nm'] = values['center_nm']
        return values
    optics.configure_for_acquisition = configure
    pixis = SimpleNamespace(invalidate_wavelengths=lambda: None)
    controller._worker._identity = {**winspec.identity, 'output_route': 'side'}
    controller._worker._parked['lightfield'] = (
        optics, pixis, {'backend': 'lightfield', 'output_route': 'front'}, [])
    routes = []
    def route(setup, requested, *, apply=True, **_):
        target = 'FrontExit' if requested == 'front' else 'SideExit'
        if apply:
            if live['output_port'] != target:
                routes.append(requested)
            live['output_port'] = target
        else:
            assert live['output_port'] == target
        return dict(live)
    monkeypatch.setattr('app.devices.lightfield_optics.ensure_output_route', route)
    monkeypatch.setattr('app.devices.winspec_adapter.ensure_output_route', route)
    section = _LF6Section(controller)
    section._on_connected([])
    commands = []
    controller._connect_requested.connect(lambda *_: commands.append('connect'))
    controller._lightfield_optics_requested.connect(lambda *_: commands.append('optics'))
    controller._temperature_requested.connect(lambda *_: commands.append('temperature'))
    notifications = []
    controller.connected.connect(lambda *_: notifications.append('connected'))
    controller.set_temperature_monitor_paused('presets', True)
    yield controller, section, camera, routes, commands, notifications
    section.close()
    # The controller is closed by the scan fixture. Keep measurements paused
    # during teardown so closing widgets does not schedule a connection refresh.


def test_sidebar_tracks_pixis_and_winspec_while_measurement_lock_stays_held(sidebar):
    controller, section, camera, routes, commands, notifications = sidebar
    assert section._backend.currentData() == 'winspec_ingaas'
    controller.prepare_scan_condition('lightfield', 730., 250., 3, 'device', threading.Event())
    QApplication.processEvents()
    assert section._backend.currentData() == 'lightfield'
    assert 'Active setup: LightField + PIXIS' in section._connections_status.text()
    assert 'PIXIS' in section._status.text()
    assert 'WinSpec' not in section._status.text()
    assert not section._backend.isEnabled()
    assert not section._connect_btn.isEnabled()
    assert not section._disconnect_btn.isEnabled()
    assert controller.switching_locked
    controller.prepare_scan_condition('winspec_ingaas', 1050., 500., 2, 'device', threading.Event())
    QApplication.processEvents()
    assert section._backend.currentData() == 'winspec_ingaas'
    assert 'Active setup: WinSpec' in section._connections_status.text()
    assert 'WinSpec' in section._status.text()
    assert not section._backend.isEnabled()
    assert not section._connect_btn.isEnabled()
    assert controller.switching_locked
    assert routes == ['front', 'side']
    assert commands == []
    assert notifications == []


def test_failed_preparation_shows_active_setup_and_keeps_measurement_locked(sidebar, monkeypatch):
    controller, section, camera, routes, commands, notifications = sidebar
    def failed(*_args, **_kwargs):
        raise RuntimeError('exit failed')
    monkeypatch.setattr('app.devices.lightfield_optics.ensure_output_route', failed)
    with pytest.raises(RuntimeError, match='exit failed'):
        controller.prepare_scan_condition('lightfield', 730., 250., 3, 'device', threading.Event())
    QApplication.processEvents()
    # Activation succeeds before preparation checks the physical exit. The
    # sidebar must reflect that active connection even though it cannot acquire.
    assert controller.backend == 'lightfield'
    assert controller._worker._scan_adapter is None
    assert section._backend.currentData() == 'lightfield'
    assert 'Active setup: LightField + PIXIS' in section._connections_status.text()
    assert not section._backend.isEnabled()
    assert controller.switching_locked
    assert not any(command.startswith('ACQUIRE') for command, _ in camera.calls)
    assert commands == []
    assert notifications == []


def test_sidebar_unlocks_on_last_used_setup_after_measurement(sidebar):
    controller, section, camera, routes, commands, notifications = sidebar
    controller.prepare_scan_condition('lightfield', 730., 250., 3, 'device', threading.Event())
    QApplication.processEvents()
    assert section._backend.currentData() == 'lightfield'
    controller.set_temperature_monitor_paused('presets', False)
    QApplication.processEvents()
    assert section._backend.currentData() == 'lightfield'
    assert section._backend.isEnabled()
    assert section._disconnect_btn.isEnabled()
    assert not controller.switching_locked
