"""The local backend must never turn incomplete native output into a spectrum."""
import importlib
import hashlib
import json
import threading
from pathlib import Path

import numpy as np
import pytest


def backend():
    # Keep the first red run an assertion about the missing feature.
    import importlib.util
    assert importlib.util.find_spec('app.devices.winusb_ingaas') is not None, 'Local WinUSB backend missing'
    return importlib.import_module('app.devices.winusb_ingaas')


class Optics:
    is_ready = True
    is_busy = False
    def __init__(self): self.centers = []
    def set_center_wavelength_when_ready(self, value, **kwargs): self.centers.append(value)


class Bridge:
    is_busy = False
    def __init__(self): self.calls = 0
    def preflight(self): return {'runtime_verified': True}
    def close(self): self.closed = True
    def capture(self, cancelled, *, request_id=None, context=None):
        self.calls += 1
        payload = np.arange(512, dtype='<u2').tobytes()
        return {'acquisition_backend': 'winusb_local', 'exposure_ms': 100.0,
                'request_id': request_id, 'request_context': context,
                'payload_sha256': hashlib.sha256(payload).hexdigest(),
                'evidence_directory': 'fixture', 'temperature_verified': False}, payload


@pytest.fixture
def setup(monkeypatch):
    module = backend()
    monkeypatch.setattr(module, 'ensure_output_route', lambda *a, **k: {'output_port': 'SideExit'})
    monkeypatch.setattr(module, 'read_optics', lambda *a: {'wavelength_nm': 1300, 'output_port': 'SideExit'})
    return module.LocalWinUSBSetup(Optics(), bridge=Bridge())


def test_manual_apply_arms_exactly_one_raw_pixel_frame(setup):
    with pytest.raises(RuntimeError, match='Apply'):
        setup.acquire()
    setup.configure_for_acquisition(center_nm=1300, exposure_ms=100, frames=1)
    x, y = setup.acquire()
    np.testing.assert_array_equal(x, np.arange(1, 513))
    np.testing.assert_array_equal(y, np.arange(512))
    assert setup.identity['acquisition_backend'] == 'winusb_local'
    assert setup.last_calibration_context is None
    frame = setup.read_metadata_snapshot()['observed']['last_frame']
    assert frame['temperature_verification']['verified'] is False
    assert 'temperature_guard' not in frame
    assert frame['native_capture']['evidence_directory'] == 'fixture'
    with pytest.raises(RuntimeError, match='Apply'):
        setup.acquire()
    assert setup.bridge.calls == 1


def test_disconnect_closes_owned_native_session(setup):
    setup.close()
    assert getattr(setup.bridge, 'closed', False), 'Persistent helper was not closed'
    assert setup.lightfield.is_ready


@pytest.mark.parametrize('dependent,all_backends', [(False, False), (True, False), (False, True)])
def test_failed_disconnect_retains_session_for_cleanup_retry(dependent, all_backends):
    from controllers.lf6_controller import _LF6Worker
    from types import SimpleNamespace
    worker = _LF6Worker()
    attempts, events = [], []
    def close():
        attempts.append(True)
        if len(attempts) == 1: raise RuntimeError('Native ownership retained')
    owner = SimpleNamespace(close=close)
    optics = SimpleNamespace(close=lambda: events.append('optics closed'))
    worker.disconnected.connect(lambda: events.append('disconnected'))
    worker._backend = 'lightfield' if dependent else 'winspec_ingaas'
    worker._setup = optics if dependent else owner
    if dependent: worker._parked['winspec_ingaas'] = (owner, None, {}, [])
    if all_backends: worker._parked['lightfield'] = (optics, None, {}, [])
    disconnect = worker.disconnect_all if all_backends else worker.disconnect_instrument
    disconnect()
    assert events == [], 'Failed close reported disconnected or closed dependent optics'
    retained = worker._parked['winspec_ingaas'][0] if dependent else worker._setup
    assert retained is owner, 'Cleanup owner was lost'
    disconnect()
    assert len(attempts) == 2 and 'disconnected' in events
    assert worker._setup is None and 'winspec_ingaas' not in worker._parked


@pytest.mark.parametrize('exposure,frames', [(99, 1), (200, 1), (100, 2), (100, True), (float('nan'), 1)])
def test_unsupported_recipe_is_rejected_before_optics_or_capture(setup, exposure, frames):
    with pytest.raises(ValueError, match='100 ms'):
        setup.configure_for_acquisition(center_nm=1300, exposure_ms=exposure, frames=frames)
    assert setup.lightfield.centers == []
    assert setup.bridge.calls == 0


def test_cancelled_frame_is_never_published_and_requires_new_apply(setup):
    setup.configure_for_acquisition(center_nm=1300, exposure_ms=100, frames=1)
    original = setup.bridge.capture
    def cancelled(event, **kwargs):
        result = original(event, **kwargs)
        setup.abort_acquisition()
        return result
    setup.bridge.capture = cancelled
    with pytest.raises(RuntimeError, match='cancelled'):
        setup.acquire()
    assert not setup.read_metadata_snapshot()['observed']['last_frame']
    assert not setup.is_busy
    with pytest.raises(RuntimeError, match='Apply'):
        setup.acquire()


def test_scan_wrapper_refuses_local_backend_before_optics(setup, monkeypatch):
    from app.devices.winspec_scan_adapter import WinSpecScanAdapter
    monkeypatch.setattr('app.devices.winspec_adapter.read_optics', lambda *a: pytest.fail('scan touched optics'))
    adapter = WinSpecScanAdapter(setup)
    with pytest.raises(RuntimeError, match='single.frame'):
        adapter.validate_scan_centers([1300])
    with pytest.raises(RuntimeError, match='single.frame'):
        adapter.configure_for_acquisition(center_nm=1300, exposure_ms=100, frames=1)


def test_ui_offers_local_backend_and_disables_xp_address(monkeypatch):
    from PySide6.QtWidgets import QApplication
    from controllers.lf6_controller import LF6Controller
    from ui.instrument_panel import _LF6Section
    from utils.config import AppConfig, cfg
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(cfg, 'lf6', AppConfig().lf6)
    controller = LF6Controller(); panel = _LF6Section(controller)
    try:
        index = panel._winspec_acquisition_backend.findData('winusb_local')
        assert index >= 0, 'Local backend missing from InGaAs selection'
        panel._winspec_acquisition_backend.setCurrentIndex(index)
        assert not panel._winspec_host.isEnabled()
        assert not panel._winspec_port.isEnabled()
        assert not panel._winspec_start_acceleration.isEnabled()
    finally:
        panel.close(); controller.shutdown()


def test_local_ui_locks_recipe_continuous_and_calibration_but_keeps_single_capture(monkeypatch):
    from PySide6.QtWidgets import QApplication
    from ui.spectrum_panel import SpectrumPanel
    from tests.test_spectrum_panel_controls import _FakeSpectrumController
    app = QApplication.instance() or QApplication([])
    controller = _FakeSpectrumController()
    controller.identity = {'backend': 'winspec_ingaas', 'acquisition_backend': 'winusb_local', 'camera_role': 'ingaas'}
    panel = SpectrumPanel(controller)
    try:
        controller.connected.emit([])
        assert panel._exposure.value() == 100
        assert panel._accumulations.value() == 1
        assert not panel._exposure.isEnabled()
        assert not panel._accumulations.isEnabled()
        assert not panel._run_1d_btn.isEnabled()
        assert not panel._wavelength_calibration_btn.isEnabled()
        assert not panel._use_winspec_nm.isChecked()
        assert panel._acquire_btn.isEnabled()
        panel._start_continuous('1d')
        assert controller.apply_calls == []
        assert 'unverified' in panel._winspec_temperature_label.text().lower()
    finally:
        panel.close()


def test_local_capture_factory_and_csv_keep_native_provenance(setup, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from controllers.lf6_controller import _LF6Worker
    from ui.spectrum_panel import SpectrumPanel
    from tests.test_spectrum_panel_controls import _FakeSpectrumController
    from utils.config import AppConfig, cfg
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(cfg, 'lf6', AppConfig().lf6)
    cfg.lf6.winspec_acquisition_backend = 'winusb_local'
    worker = _LF6Worker()
    worker.connect_instrument(True, 'lightfield')
    worker._identity = {'backend': 'lightfield'}
    borrowed = worker.setup
    def factory(lightfield, **kwargs):
        assert lightfield is borrowed
        return setup
    monkeypatch.setattr(backend(), 'LocalWinUSBSetup', factory)
    monkeypatch.setattr('app.devices.lightfield_optics.ensure_output_route', lambda *a, **k: {'output_port': 'SideExit'})
    worker.connect_instrument(False, 'winspec_ingaas')
    assert worker.identity['acquisition_backend'] == 'winusb_local'
    controller = _FakeSpectrumController(); controller.identity = worker.identity
    panel = SpectrumPanel(controller)
    worker.acquisition_settings_readback.connect(panel._on_acquisition_readback)
    worker.spectrum_ready.connect(panel._on_spectrum_ready)
    errors = []; worker.error.connect(errors.append)
    try:
        controller.connected.emit([])
        applied = []
        worker.settings_applied.connect(lambda: applied.append(True))
        worker.apply_settings(100.0, 1300.0, 1)
        assert not errors
        assert applied == [True]
        worker.acquire_single()
        assert not errors
        panel.save_current(tmp_path/'local.csv')
        saved = list(tmp_path.glob('*.json'))
        assert saved, list(tmp_path.iterdir())
        content = saved[0].read_text(encoding='utf-8')
        assert 'winusb_local' in content
        assert 'experimental_unverified' in content
        assert 'evidence_directory' in content
        assert 'request_id' in content
        assert 'requested_center_nm' in content
        assert 'cold_or_locked' not in content
        assert panel._spec_plot.axis_unit == 'pixel'
        assert len(panel._last_data) == 512
    finally:
        panel.close(); worker.disconnect_all()


@pytest.mark.parametrize('damage', ['previous_request', 'wrong_payload'])
def test_frame_from_wrong_request_or_changed_payload_is_not_published(setup, damage):
    original = setup.bridge.capture
    def corrupt(event, **kwargs):
        metadata, payload = original(event, **kwargs)
        if damage == 'previous_request': metadata['request_id'] = 'previous-request'
        else: payload = b'\0'*1024
        return metadata, payload
    setup.bridge.capture = corrupt
    setup.configure_for_acquisition(center_nm=1300, exposure_ms=100, frames=1)
    with pytest.raises(RuntimeError, match='request|payload'):
        setup.acquire()
    assert not setup.read_metadata_snapshot()['observed']['last_frame']
    assert not setup.is_busy
    with pytest.raises(RuntimeError, match='Apply'): setup.acquire()


def test_leaving_local_preserves_each_other_detectors_exposure(monkeypatch):
    from PySide6.QtWidgets import QApplication
    from ui.spectrum_panel import SpectrumPanel
    from tests.test_spectrum_panel_controls import _FakeSpectrumController
    from utils.config import AppConfig, cfg
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(cfg, 'lf6', AppConfig().lf6)
    controller = _FakeSpectrumController()
    controller.backend = 'lightfield'; controller.identity = {'backend': 'lightfield'}
    panel = SpectrumPanel(controller)
    try:
        controller.connected.emit([])
        panel._exposure.setValue(200); panel._accumulations.setValue(2)
        panel._backend_profiles['winspec_ingaas'] = {'exposure_ms': 5000, 'accumulations': 4}
        controller.backend = 'winspec_ingaas'
        controller.identity = {'backend': 'winspec_ingaas', 'acquisition_backend': 'winusb_local', 'camera_role': 'ingaas'}
        controller.connected.emit([])
        assert (panel._exposure.value(), panel._accumulations.value()) == (100, 1)
        controller.backend = 'lightfield'; controller.identity = {'backend': 'lightfield'}
        controller.connected.emit([])
        assert (panel._exposure.value(), panel._accumulations.value()) == (200, 2)
        controller.backend = 'winspec_ingaas'
        controller.identity = {'backend': 'winspec_ingaas', 'acquisition_backend': 'winspec', 'camera_role': 'ingaas'}
        controller.connected.emit([])
        assert (panel._exposure.value(), panel._accumulations.value()) == (5000, 4)
        assert panel._exposure.isEnabled()
        assert panel._run_1d_btn.isEnabled()
    finally:
        panel.close()


def test_existing_calibration_page_cannot_start_local_capture_or_change_recipe(monkeypatch):
    from PySide6.QtWidgets import QApplication
    from ui.spectrum_panel import SpectrumPanel
    from tests.test_spectrum_panel_controls import _FakeSpectrumController
    app = QApplication.instance() or QApplication([])
    controller = _FakeSpectrumController(); controller.backend = 'winspec_ingaas'
    controller.identity = {'backend': 'winspec_ingaas', 'acquisition_backend': 'winspec', 'camera_role': 'ingaas'}
    panel = SpectrumPanel(controller)
    try:
        page = panel.create_calibration_page()
        controller.connected.emit([])
        controller.identity['acquisition_backend'] = 'winusb_local'
        controller.connected.emit([])
        assert not page.isEnabled()
        before = (panel._center.value(), panel._exposure.value(), panel._accumulations.value())
        panel._capture_calibration_lamp()
        panel._capture_broad_settings(1500, 4000)
        panel._capture_broad_center(1600)
        assert controller.apply_calls == []
        assert (panel._center.value(), panel._exposure.value(), panel._accumulations.value()) == before
        assert 'unavailable' in page.result.text().lower()
    finally:
        panel.close()
