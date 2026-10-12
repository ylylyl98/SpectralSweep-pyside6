"""Zero-order spectrograph controls with simulated hardware only."""
import csv
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from app.devices.andor_adapter import AndorConnectionOptions, AndorSDK2Setup, SpectrometerAndor
from app.devices.winspec_adapter import WinSpecSetup
from tests.test_andor_adapter import _FakeAndorModule, _FakeSpectrograph
from tests.test_spectrum_panel_controls import _FakeSpectrumController
from tests.test_winspec_adapter import Camera, Optics
from ui.andor_controls_widget import AndorControlsWidget
from ui.detector_wavelength_advice import wavelength_advice
from ui.settings_panel import SettingsPanel
from ui.spectrum_panel import SpectrumPanel
from utils.config import cfg


@pytest.fixture(autouse=True)
def app(monkeypatch):
    application = QApplication.instance() or QApplication([])
    monkeypatch.setattr(cfg.lf6, 'center_nm', 860.)
    yield application


def test_zero_center_applies_without_energy_conversion_and_recovers_after_locks():
    controller = _FakeSpectrumController()
    controller.identity = {'backend': 'lightfield'}
    requests = []
    controller.lightfield_optics = lambda requested=None: requests.append(requested)
    panel = SpectrumPanel(controller)
    try:
        controller.connected.emit([])
        panel._center.setValue(0.)
        assert panel._center.value() == 0.
        assert not panel._energy.isEnabled()
        assert 'zero order' in panel._energy.text().lower()
        panel._refresh_action_controls()
        assert not panel._energy.isEnabled()
        panel._apply_optics_btn.click()
        assert requests[-1]['wavelength_nm'] == 0.
        panel._on_optics_status({'wavelength_nm': 0.})
        assert '0 nm' in panel._optics_readback.text()
        assert 'zero order' in panel._optics_readback.text().lower()
        panel._center.setValue(1000.)
        assert panel._energy.isEnabled()
        assert panel._energy.value() == pytest.approx(1.239842, abs=1e-6)
        panel._set_action_controls_enabled(False)
        panel._center.setValue(0.)
        panel._center.setValue(800.)
        assert not panel._energy.isEnabled()
        panel._set_action_controls_enabled(True)
        assert panel._energy.isEnabled()
    finally:
        panel.close()


def test_zero_center_survives_startup_and_session_restore(monkeypatch):
    monkeypatch.setattr(cfg.lf6, 'center_nm', 0.)
    panel = SpectrumPanel(_FakeSpectrumController())
    settings = SettingsPanel()
    restored = SpectrumPanel(_FakeSpectrumController())
    try:
        assert panel._center.value() == settings._center.value() == 0.
        restored._center.setValue(750.)
        restored.restore_session_state(panel.capture_session_state())
        assert restored._center.value() == 0.
        assert not restored._energy.isEnabled()
    finally:
        panel.close()
        settings.close()
        restored.close()


def test_andor_limits_keep_zero_order_available():
    widget = AndorControlsWidget(_FakeSpectrumController())
    try:
        widget._populate_spectrograph({'wavelength_limits_nm': (200., 2500.), 'wavelength_nm': 0.})
        assert widget.center.value() == 0.
        widget.center.setValue(1210.)
        assert widget.center.value() == 1210.
    finally:
        widget.close()


@pytest.mark.parametrize('backend', ['andor_ingaas', 'andor_si', 'lightfield'])
def test_zero_order_does_not_trigger_detector_wavelength_advice(backend):
    assert wavelength_advice({'backend': backend}, [0.]) == ''


@pytest.mark.parametrize('backend', ['lightfield', 'andor_sdk2'])
def test_zero_order_capture_uses_pixel_axis_and_exports_pixels(tmp_path, backend):
    controller = _FakeSpectrumController()
    controller.identity = {'backend': backend}
    panel = SpectrumPanel(controller)
    try:
        panel._pending_acquisition_snapshot = {'center_nm': 0., 'instrument_identity': controller.identity.copy()}
        controller.spectrum_ready.emit(np.array([-1., 0., 1.]), np.array([10., 20., 30.]))
        assert panel._spec_plot.axis_unit == 'pixel'
        assert not panel._spec_plot._plot.getAxis('top').isVisible()
        np.testing.assert_array_equal(panel._last_wavelength, [0., 1., 2.])
        assert panel._last_acquisition_snapshot['instrument_identity']['spectrograph_mode'] == 'zero_order'
        output = tmp_path / 'zero-order.csv'
        panel._save_spectrum_data(panel._last_wavelength, panel._last_data, output,
                                  settings_snapshot=panel._last_acquisition_snapshot, source='zero_order_test')
        with output.open(newline='', encoding='utf-8') as stream:
            rows = list(csv.reader(stream))
        assert rows[0] == ['pixel', 'intensity_counts']
        assert [float(row[0]) for row in rows[1:]] == [0., 1., 2.]
        panel._pending_acquisition_snapshot = {'center_nm': 750., 'instrument_identity': controller.identity.copy()}
        controller.spectrum_ready.emit(np.array([749., 750., 751.]), np.ones(3))
        assert panel._spec_plot.axis_unit == 'nm'
        np.testing.assert_array_equal(panel._last_wavelength, [749., 750., 751.])
    finally:
        panel.close()


@pytest.mark.parametrize('backend', ['lightfield', 'andor_sdk2', 'winspec_ingaas'])
def test_positive_center_clears_stale_zero_order_identity(tmp_path, backend):
    controller = _FakeSpectrumController()
    controller.identity = {'backend': backend, 'axis_unit': 'pixel',
                           'spectrograph_mode': 'zero_order', 'calibration_status': 'zero_order'}
    panel = SpectrumPanel(controller)
    try:
        panel._pending_acquisition_snapshot = {'center_nm': 750., 'instrument_identity': controller.identity.copy()}
        controller.spectrum_ready.emit(np.array([749., 750., 751.]), np.ones(3))
        identity = panel._last_acquisition_snapshot['instrument_identity']
        assert identity['spectrograph_mode'] == 'wavelength'
        assert identity.get('calibration_status') != 'zero_order'
        expected_unit = 'pixel' if backend == 'winspec_ingaas' else 'nm'
        assert identity['axis_unit'] == panel._spec_plot.axis_unit == expected_unit
        np.testing.assert_array_equal(panel._last_wavelength, [749., 750., 751.])
        output = tmp_path / 'positive-center.csv'
        panel._save_spectrum_data(panel._last_wavelength, panel._last_data, output,
                                  settings_snapshot=panel._last_acquisition_snapshot, source='positive_center_test')
        with output.open(newline='', encoding='utf-8') as stream:
            rows = list(csv.reader(stream))
        assert rows[0][0] == ('pixel' if expected_unit == 'pixel' else 'wavelength_nm')
        panel._pending_acquisition_snapshot = {'center_nm': 750., 'instrument_identity': controller.identity.copy()}
        controller.frame_ready.emit(np.ones((2, 3)))
        assert panel._last_acquisition_snapshot['instrument_identity']['spectrograph_mode'] == 'wavelength'
    finally:
        panel.close()


def test_winspec_zero_order_retains_full_pixel_frame_despite_saved_calibration(monkeypatch):
    from app.wavelength_calibration import fit_calibration

    context = {'profile': 'bench1', 'grating': '300', 'center_nm': 0.,
               'output_port': 'SideExit', 'detector': 'camera1', 'geometry': [512, 1], 'spectrometer': 'sp1'}
    record = fit_calibration([20, 200, 490], [1202, 1220, 1249], [300], [1230], context, 1, .1)
    monkeypatch.setattr(cfg.lf6, 'winspec_wavelength_calibrations', [record])
    controller = _FakeSpectrumController()
    controller.identity = {'backend': 'winspec_ingaas'}
    panel = SpectrumPanel(controller)
    try:
        panel._use_winspec_nm.setChecked(True)
        panel._on_acquisition_readback({'winspec_frame_context': context})
        panel._pending_acquisition_snapshot = {'center_nm': 0., 'instrument_identity': controller.identity.copy()}
        controller.spectrum_ready.emit(np.arange(1, 513), np.arange(512))
        np.testing.assert_array_equal(panel._last_data, np.arange(512))
        np.testing.assert_array_equal(panel._last_wavelength, np.arange(1, 513))
        assert panel._spec_plot.axis_unit == 'pixel'
        assert 'wavelength_calibration' not in panel._last_acquisition_snapshot
    finally:
        panel.close()


class _ZeroOrderSpectrograph(_FakeSpectrograph):
    def set_wavelength(self, value):
        if value == 0.:
            raise AssertionError('Use the dedicated zero-order command')
        return super().set_wavelength(value)

    def goto_zero_order(self):
        self.wavelength = 0.
        return True

    def is_at_zero_order(self):
        return self.get_wavelength() == 0.

    def get_calibration(self):
        if self.is_at_zero_order():
            raise AssertionError('Zero order has no wavelength calibration')
        return super().get_calibration()


@pytest.fixture
def andor():
    module = _FakeAndorModule()
    module.ShamrockSpectrograph = lambda idx: _ZeroOrderSpectrograph(module, idx)
    setup = AndorSDK2Setup(AndorConnectionOptions(
        camera_role='si', camera_serial='SI-002', invert_wavelength_axis=False,
        operation_timeout_s=2.), andor_module=module)
    yield setup
    setup.close()


@pytest.mark.parametrize('operation', ['configure', 'controls'])
def test_andor_zero_order_command_readback_and_pixel_calibration(andor, operation):
    if operation == 'configure':
        result = andor.configure_for_acquisition(center_nm=0., exposure_ms=100., frames=1)
        assert result['center_wavelength']['readback'] == 0.
    else:
        result = andor.apply_controls({'wavelength_nm': 0.})
        assert result['wavelength_nm'] == 0.
    assert andor.identity['axis_unit'] == 'pixel'
    snapshot = andor.get_control_snapshot()
    assert 'stored_calibration_nm' not in snapshot
    assert snapshot['spectrograph_mode'] == 'zero_order'
    assert isinstance(snapshot['pixel_axis'], list)
    assert snapshot['pixel_axis'] == [0., 1., 2., 3.]
    np.testing.assert_array_equal(andor.get_wavelength_calibration(), [0., 1., 2., 3.])
    axis, counts = SpectrometerAndor(andor).acquire()
    np.testing.assert_array_equal(axis, [0., 1., 2., 3.])
    assert counts.size == 4
    andor.configure_for_acquisition(center_nm=1210., exposure_ms=100., frames=1)
    assert andor.identity['axis_unit'] == 'nm'
    np.testing.assert_allclose(andor.get_wavelength_calibration(), [1., 2., 3., 4.])


def test_andor_zero_order_requires_hardware_confirmation(andor):
    andor._spectrograph.is_at_zero_order = lambda: False
    with pytest.raises(RuntimeError, match='[Zz]ero.order'):
        andor.apply_controls({'wavelength_nm': 0.})


def test_winspec_zero_order_apply_and_acquisition(monkeypatch):
    monkeypatch.setattr('app.devices.winspec_adapter.ensure_output_route', lambda *args, **kwargs: {})
    camera, optics = Camera(), Optics()
    setup = WinSpecSetup(optics, client=camera)
    try:
        readback = setup.configure_for_acquisition(center_nm=0., exposure_ms=20., frames=2)
        assert readback['center_nm'] == 0.
        assert optics.calls == [0.]
        axis, counts = setup.acquire()
        np.testing.assert_array_equal(axis, np.arange(1, 513))
        np.testing.assert_array_equal(counts, np.arange(512) / 2)
        for invalid in [-1., float('nan')]:
            with pytest.raises(ValueError, match='[Cc]enter'):
                setup.configure_for_acquisition(center_nm=invalid, exposure_ms=20., frames=2)
    finally:
        setup.close()
