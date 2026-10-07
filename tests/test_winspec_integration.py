import csv
import json
import os
from unittest.mock import Mock, patch

import numpy as np
import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from controllers.lf6_controller import _LF6Worker
from ui.spectrum_panel import SpectrumPanel
from ui.instrument_panel import _LF6Section
from controllers.lf6_controller import LF6Controller
from utils.config import AppConfig, cfg
from app.wavelength_calibration import fit_calibration
from tests.test_spectrum_panel_controls import _FakeSpectrumController


@pytest.fixture(scope='session', autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_hybrid_borrows_lightfield_and_can_switch_back_without_closing_it():
    w = _LF6Worker(); w.connect_instrument(True, 'lightfield')
    w._identity = {'backend': 'lightfield'}  # Fake a real session without creating Automation.
    original = w.setup; original.close = Mock()
    hybrid = Mock(is_ready=True, is_busy=False)
    hybrid.readiness_snapshot = {'ready': True, 'busy': False}
    hybrid.identity = {'backend': 'winspec_ingaas', 'axis_unit': 'pixel'}
    hybrid.get_saved_experiments.return_value = []
    with patch('app.devices.winspec_adapter.WinSpecSetup', return_value=hybrid) as factory, \
         patch('app.devices.lightfield_optics.ensure_output_route', return_value={'output_port': 'SideExit'}) as route:
        w.connect_instrument(False, 'winspec_ingaas')
    route.assert_called_once_with(hybrid.lightfield, 'side')
    assert w.setup is hybrid
    assert factory.call_args.args[0] is original
    original.close.assert_not_called()
    w.connect_instrument(True, 'lightfield')
    assert w.setup is original
    w.disconnect_instrument()
    hybrid.close.assert_called_once()
    assert 'winspec_ingaas' not in w.connected_backends


def test_hybrid_requires_lightfield_without_creating_new_instance():
    w = _LF6Worker(); errors = []; w.error.connect(errors.append)
    w.connect_instrument(False, 'winspec_ingaas')
    assert any('Connect LightField first' in e for e in errors)


def test_worker_delivers_acceleration_and_timing_with_the_matching_spectrum():
    worker=_LF6Worker();controller=_FakeSpectrumController()
    controller.identity={'backend':'winspec_ingaas','axis_unit':'pixel'}
    panel=SpectrumPanel(controller)
    frame={'start_acceleration':{'mode':'optimized','optimized':True},
           'bridge_timing_s':{'start_experiment':.4},'client_timing_s':{'request_total':1.6},
           'host_timing_s':{'total':1.7},'raw_accumulated_counts':np.arange(512).tolist()}
    setup=Mock(last_calibration_context=None)
    setup.acquire.return_value=(np.arange(1,513),np.arange(512)/2)
    setup.read_metadata_snapshot.return_value={'observed':{'last_frame':frame}}
    worker._backend='winspec_ingaas';worker._setup=worker._adapter=setup
    worker.acquisition_settings_readback.connect(panel._on_acquisition_readback)
    worker.spectrum_ready.connect(panel._on_spectrum_ready)
    try:
        worker.acquire_single()
        snapshot=panel._last_acquisition_snapshot
        assert snapshot['start_acceleration']==frame['start_acceleration']
        assert snapshot['capture_timing']['host']==frame['host_timing_s']
        assert snapshot['capture_timing']['signal_emitted_unix']>0
        np.testing.assert_array_equal(panel._latest_winspec_frame['counts'],np.arange(512)/2)
    finally:panel.close()


def test_start_acceleration_is_default_off_and_disabled_for_pvcam(tmp_path,monkeypatch):
    config=AppConfig();monkeypatch.setattr(cfg,'lf6',config.lf6)
    controller=LF6Controller();panel=_LF6Section(controller)
    try:
        assert config.lf6.winspec_start_acceleration is False
        assert not panel._winspec_start_acceleration.isChecked()
        panel._winspec_start_acceleration.setChecked(True)
        with patch.object(cfg,'save'),patch.object(controller,'connect_instrument'):
            panel._on_connect()
        assert cfg.lf6.winspec_start_acceleration is True
        config.save(tmp_path/'config.json');restored=AppConfig();restored.load(tmp_path/'config.json')
        assert restored.lf6.winspec_start_acceleration is True
        panel._winspec_acquisition_backend.setCurrentIndex(panel._winspec_acquisition_backend.findData('pvcam'))
        assert not panel._winspec_start_acceleration.isEnabled()
    finally:panel.close();controller.shutdown()


def test_pvcam_choice_persists_and_is_passed_to_detector_without_new_lightfield(tmp_path, monkeypatch):
    config = AppConfig(); monkeypatch.setattr(cfg, 'lf6', config.lf6)
    controller = LF6Controller(); panel = _LF6Section(controller)
    try:
        assert panel._winspec_acquisition_backend.currentData() == 'winspec'
        panel._winspec_acquisition_backend.setCurrentIndex(panel._winspec_acquisition_backend.findData('pvcam'))
        with patch.object(cfg,'save'), patch.object(controller,'connect_instrument'):
            panel._on_connect()
        path=tmp_path/'config.json';config.save(path)
        restored=AppConfig();restored.load(path)
        assert restored.lf6.winspec_acquisition_backend=='pvcam'
        w=_LF6Worker();w.connect_instrument(True,'lightfield')
        w._identity={'backend':'lightfield'}
        original=w.setup
        hybrid=Mock(is_ready=True,is_busy=False)
        hybrid.identity={'backend':'winspec_ingaas','acquisition_backend':'pvcam'}
        hybrid.readiness_snapshot={'ready':True,'busy':False}
        hybrid.get_saved_experiments.return_value=[]
        with patch('app.devices.winspec_adapter.WinSpecSetup',return_value=hybrid) as factory, \
             patch('app.devices.lightfield_optics.ensure_output_route',return_value={'output_port':'SideExit'}):
            w.connect_instrument(False,'winspec_ingaas')
        assert factory.call_args.args[0] is original
        assert factory.call_args.kwargs['acquisition_backend']=='pvcam'
        w.disconnect_all()
    finally:
        panel.close();controller.shutdown()


def test_setup_profile_ui_defaults_and_fixed_exit(tmp_path, monkeypatch):
    config = AppConfig()
    monkeypatch.setattr(cfg, 'lf6', config.lf6)
    controller = LF6Controller()
    panel = None
    try:
        panel = _LF6Section(controller)
        assert panel._winspec_host.text() == '192.168.170.128'
        assert panel._winspec_port.value() == 5000
        assert panel._lf_route.currentData() == 'front'
        assert panel._winspec_route.currentData() == 'side'
        panel._optical_profile.setCurrentText('Fixed front exit')
        assert panel._lf_route.currentData() == 'fixed_front'
        assert panel._winspec_route.currentData() == 'disabled'
        config.lf6.optical_profile = 'Fixed front exit'
        path = tmp_path/'config.json'
        config.save(path)
        restored = AppConfig(); restored.load(path)
        assert restored.lf6.optical_profile == 'Fixed front exit'
        assert restored.lf6.optical_profiles['Fixed front exit']['lightfield'] == 'fixed_front'
    finally:
        if panel is not None: panel.close()
        controller.shutdown()


def test_shutdown_with_lightfield_and_hybrid_parked():
    w = _LF6Worker(); w.connect_instrument(True, 'lightfield')
    w._identity = {'backend': 'lightfield'}
    hybrid = Mock(is_ready=True, is_busy=False)
    hybrid.identity = {'backend': 'winspec_ingaas', 'axis_unit': 'pixel'}
    hybrid.readiness_snapshot = {'ready': True, 'busy': False}
    hybrid.get_saved_experiments.return_value = []
    with patch('app.devices.winspec_adapter.WinSpecSetup', return_value=hybrid), \
         patch('app.devices.lightfield_optics.ensure_output_route', return_value={'output_port': 'SideExit'}):
        w.connect_instrument(False, 'winspec_ingaas')
    assert w.setup is hybrid
    w.connect_instrument(True, 'andor_si')
    w.disconnect_all()
    hybrid.close.assert_called_once()
    assert not w.connected_backends


def test_pixel_spectrum_has_no_energy_axis_and_exports_pixels(tmp_path):
    controller = _FakeSpectrumController()
    controller.identity = {'backend': 'winspec_ingaas', 'camera_role': 'ingaas', 'axis_unit': 'pixel'}
    controller.backend = 'winspec_ingaas'
    panel = SpectrumPanel(controller)
    panel._on_lf6_connected([])
    assert not panel._supports_2d
    panel._on_spectrum_ready(np.arange(1, 513), np.arange(512))
    assert panel._spec_plot._plot.getAxis('bottom').labelText.startswith('Pixel')
    assert not panel._spec_plot._plot.getAxis('top').isVisible()
    assert 'eV' not in panel._spec_plot._peak_lbl.text()
    assert 'pixel' in panel._spec_plot._range_lbl.text().lower()
    output = tmp_path/'pixel.csv'
    meta = panel._save_spectrum_data(np.arange(1, 513), np.arange(512), output,
                                    settings_snapshot=panel._last_acquisition_snapshot, source='acquired')
    with output.open() as f: assert next(csv.reader(f))[0] == 'pixel'
    document = json.loads(meta.read_text())
    assert '"pixel_axis"' in json.dumps(document)
    assert '"wavelength_axis_nm": [1.0' not in json.dumps(document)
    # Frozen pixel provenance must survive switching the connected device.
    controller.identity = {'backend': 'lightfield'}
    output2 = tmp_path/'pixel-after-switch.csv'
    panel._save_spectrum_data(np.arange(1, 513), np.arange(512), output2,
                             settings_snapshot=panel._last_acquisition_snapshot, source='acquired')
    assert output2.read_text().startswith('pixel,')
    ref = panel.add_reference(np.arange(1, 513), np.arange(512), auto_save=False,
                              settings_snapshot=panel._last_acquisition_snapshot)
    details = panel.reference_details(ref['id'])
    assert details['pixel_range'] == [1., 512.]
    assert 'wavelength_range_nm' not in details
    panel.push_spectrum(np.arange(500, 600), np.ones(100))
    assert panel._spec_plot.axis_unit == 'nm'
    assert panel._spec_plot._plot.getAxis('top').isVisible()
    panel.close()


def test_nm_calibration_is_winspec_only_and_invalidates_on_center_change(monkeypatch, tmp_path):
    controller = _FakeSpectrumController()
    controller.identity = {'backend': 'winspec_ingaas', 'axis_unit': 'pixel'}
    controller.backend = 'winspec_ingaas'
    context = {'profile':'bench', 'grating':'300', 'center_nm':1320., 'output_port':'SideExit',
               'detector':'camera', 'geometry':[512,1], 'spectrometer':'sp'}
    record = fit_calibration([20,200,490],[1202,1220,1249],[300],[1230],context,1,.1)
    monkeypatch.setattr(cfg.lf6,'winspec_wavelength_calibrations',[record])
    panel = SpectrumPanel(controller)
    panel._on_lf6_connected([])
    assert panel._wavelength_calibration_btn.isEnabled()
    panel._use_winspec_nm.setChecked(True)
    panel._on_acquisition_readback({'winspec_frame_context':context})
    panel._on_spectrum_ready(np.arange(1,513),np.arange(512))
    assert panel._spec_plot.axis_unit == 'nm'
    assert panel._last_wavelength[0] == pytest.approx(1202)
    assert len(panel._last_data) == 471
    assert len(panel._last_acquisition_snapshot['raw_detector']['counts']) == 512
    output=tmp_path/'calibrated.csv'
    meta=panel._save_spectrum_data(panel._last_wavelength,panel._last_data,output,
                                   settings_snapshot=panel._last_acquisition_snapshot,source='acquired')
    assert record['id'] in meta.read_text()
    panel._on_acquisition_readback({'winspec_frame_context':{**context,'center_nm':1321.}})
    panel._on_spectrum_ready(np.arange(1,513),np.arange(512))
    assert panel._spec_plot.axis_unit == 'pixel'
    controller.identity={'backend':'lightfield'}; controller.backend='lightfield'
    panel._on_lf6_connected([])
    assert not panel._wavelength_calibration_btn.isEnabled()
    panel._on_spectrum_ready(np.arange(600,700),np.ones(100))
    assert panel._last_wavelength[0] == 600
    assert panel._spec_plot.axis_unit == 'nm'
    panel.close()


def test_calibration_dialog_requires_explicit_lines_and_validation():
    from ui.wavelength_calibration_dialog import WavelengthCalibrationDialog
    dialog=WavelengthCalibrationDialog()
    context = {'profile':'bench', 'grating':'300', 'center_nm':1320., 'output_port':'SideExit',
               'detector':'camera', 'geometry':[512,1], 'spectrometer':'sp'}
    dialog.set_frame({'counts':np.ones(512).tolist(),'context':context})
    dialog.fit()
    assert not dialog.save.isEnabled()
    dialog.source_confirm.setChecked(True)
    for pixel,nm,role in [(20,1202,'Fit'),(200,1220,'Fit'),(490,1249,'Fit'),(300,1230,'Check')]:
        dialog.add_peak(pixel)
        row=dialog.table.rowCount()-1
        dialog.table.cellWidget(row,0).setCurrentText(role)
        dialog.table.cellWidget(row,2).setEditText(str(nm))
    dialog.fit()
    assert dialog.save.isEnabled()
    dialog.table.cellWidget(0,2).setEditText('1500')
    assert not dialog.save.isEnabled()
    dialog.close()


def test_winspec_reverse_display_preserves_raw_pairs_and_resets_for_nm(monkeypatch):
    monkeypatch.setattr(cfg.lf6, 'winspec_reverse_pixel_display', True, raising=False)
    monkeypatch.setattr(cfg, 'save', Mock())
    panel = SpectrumPanel(_FakeSpectrumController())
    pixels=np.arange(1,513); counts=np.arange(512)**2
    snapshot={'instrument_identity':{'backend':'winspec_ingaas','axis_unit':'pixel'}}
    panel.push_spectrum(pixels,counts,settings_snapshot=snapshot)
    assert panel._spec_plot._plot.getViewBox().state['xInverted']
    assert 'reversed' in panel._spec_plot._plot.getAxis('bottom').labelText
    np.testing.assert_array_equal(panel._last_wavelength,pixels)
    np.testing.assert_array_equal(panel._last_data,counts)
    np.testing.assert_array_equal(panel._spec_plot._curve.xData,pixels)
    np.testing.assert_array_equal(panel._spec_plot._curve.yData,counts)
    panel._reverse_winspec_pixels.setChecked(False)
    assert not panel._spec_plot._plot.getViewBox().state['xInverted']
    assert cfg.lf6.winspec_reverse_pixel_display is False
    panel._reverse_winspec_pixels.setChecked(True)
    panel.push_spectrum(np.array([1002,1001,1000]),np.array([1,2,3]),
        settings_snapshot={'instrument_identity':{'backend':'winspec_ingaas','axis_unit':'nm'}})
    assert not panel._spec_plot._plot.getViewBox().state['xInverted']
    panel.push_spectrum(pixels,counts,settings_snapshot={'instrument_identity':{'backend':'lightfield','axis_unit':'pixel'}})
    assert not panel._spec_plot._plot.getViewBox().state['xInverted']
    panel.close()


def test_winspec_temperature_report_attached_to_exact_frame():
    controller = _FakeSpectrumController()
    controller.identity = {'backend': 'winspec_ingaas', 'axis_unit': 'pixel'}
    panel = SpectrumPanel(controller)
    report = {'passed': True, 'maximum_c': -100., 'limit_c': -100.}
    panel._on_acquisition_readback({'winspec_frame_context': None, 'winspec_temperature_guard': report})
    panel._on_spectrum_ready(np.arange(1, 513), np.ones(512))
    assert panel._last_acquisition_snapshot['temperature_guard'] == report
    assert '-100' in panel._winspec_temperature_label.text()
    assert 'passed' in panel._winspec_temperature_label.text()
    panel._on_spectrum_ready(np.arange(1, 513), np.ones(512))
    assert 'temperature_guard' not in panel._last_acquisition_snapshot
    panel.close()


def test_connection_panel_configuration_and_advanced_hierarchy(monkeypatch):
    monkeypatch.setattr(cfg, 'save', Mock())
    controller = LF6Controller()
    panel = _LF6Section(controller)
    try:
        panel._backend.setCurrentIndex(panel._backend.findData('winspec_ingaas'))
        panel._update_connection_actions()
        assert panel._advanced_content.isHidden()
        assert 'LightField spectrograph' in panel._connections_status.text()
        assert 'XP WinSpec' in panel._connections_status.text()
        assert 'Connect required' in panel._connect_btn.text()
        panel._mock_chk.setChecked(False)
        controller.connect_instrument = Mock()
        panel._on_connect()
        controller.connect_instrument.assert_called_once_with(use_mock=False, backend='lightfield')
        assert panel._pending_configuration == 'winspec_ingaas'
        controller._settings_pending = True
        panel._continue_configuration()
        assert controller.connect_instrument.call_count == 1
        controller._settings_pending = False
        panel._continue_configuration()
        assert controller.connect_instrument.call_args.kwargs == {'use_mock': False, 'backend': 'winspec_ingaas'}
    finally:
        panel.close(); controller.shutdown()


def test_winspec_temperature_display_expires_and_real_session_not_mock():
    from controllers.lf6_controller import LightFieldLifecycleState
    from datetime import timedelta
    controller = LF6Controller()
    panel = _LF6Section(controller)
    try:
        controller._worker._backend = 'winspec_ingaas'
        controller._worker._identity = {'backend': 'winspec_ingaas'}
        controller._worker._adapter = Mock()
        controller._worker._state = LightFieldLifecycleState.READY
        panel._backend.setCurrentIndex(panel._backend.findData('winspec_ingaas'))
        panel._mock_chk.setChecked(True)
        panel._on_temperature_snapshot({'temperature_c': -100., 'temperature_status': 'Locked', 'temperature_setpoint_c': -100.})
        assert 'Simulation mode' not in panel._setup_help.text()
        assert 'Temperature satisfied' in panel._acquisition_condition.text()
        panel._winspec_readback_time -= timedelta(seconds=11)
        panel._update_connection_actions()
        assert 'Temperature satisfied' in panel._acquisition_condition.text()
        panel._winspec_readback_time -= timedelta(seconds=5)
        panel._update_connection_actions()
        assert 'Fresh temperature required' in panel._acquisition_condition.text()
        assert 'no fresh readback' in panel._temperature.text()
    finally:
        panel.close(); controller.shutdown()


def test_calibration_page_owns_settings_separately_from_spectrum():
    panel = SpectrumPanel(_FakeSpectrumController())
    page = panel.create_calibration_page()
    assert page.steps.count() == 3
    assert page.isAncestorOf(panel._use_winspec_nm)
    assert page.isAncestorOf(panel._reverse_winspec_pixels)
    assert not panel.isAncestorOf(page)
    page.fit()
    assert not page.save.isEnabled()
    assert page.steps.currentIndex() == 2
    page.close(); panel.close()


def test_guided_calibration_requires_source_and_starts_selected_mode():
    from ui.wavelength_calibration_dialog import WavelengthCalibrationDialog
    page = WavelengthCalibrationDialog()
    captures = []
    page.capture_requested.connect(lambda: captures.append(True))
    page.start_calibration()
    assert not captures
    page.source_confirm.setChecked(True)
    page.mode.setCurrentIndex(0)
    page.start_calibration()
    assert captures == [True]
    page.mode.setCurrentIndex(1)
    page.open_broad = Mock()
    page.broad_dialog = Mock(batch_active=False, sweep_active=False, analysis_running=False)
    page.start_calibration()
    page.broad_dialog.confirm.setChecked.assert_called_once_with(True)
    page.broad_dialog.start_all_gratings.assert_called_once()
    page.broad_dialog = None
    page.close()


def test_embedded_escape_and_hidden_capture_completion():
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    controller = _FakeSpectrumController()
    controller.identity = {'backend': 'winspec_ingaas', 'axis_unit': 'pixel'}
    panel = SpectrumPanel(controller)
    page = panel.create_calibration_page()
    page.show(); QApplication.processEvents()
    QTest.keyClick(page, Qt.Key.Key_Escape)
    assert not page.isHidden()
    page.hide()
    page.save_raw_frame = Mock()
    panel._calibration_capture_pending = True
    panel._on_spectrum_ready(np.arange(1, 513), np.ones(512))
    page.save_raw_frame.assert_called_once()
    assert page.frame is not None
    assert not panel._calibration_capture_pending
    page.close(); panel.close()


def test_batch_switch_waits_for_verified_optics(monkeypatch):
    ctrl=_FakeSpectrumController();ctrl.identity={'backend':'winspec_ingaas'}
    ctrl.lightfield_optics=Mock()
    panel=SpectrumPanel(ctrl);page=panel.create_calibration_page();page.open_broad()
    d=page.broad_dialog;d.confirm.setChecked(True)
    panel._optics_snapshot={'grating_infos':[{'index':'g1'},{'index':'g2'}]}
    assert panel._calibration_gratings()==['g1','g2']
    d.start_all_gratings(panel._calibration_gratings())
    ctrl.lightfield_optics.assert_called_once_with({'grating':'g1'})
    assert not d.sweep_active
    panel._on_optics_status({'grating':'wrong'})
    assert not d.batch_active and not d.sweep_active
    d.close();page.close();panel.close()


def test_partial_batch_success_enables_saved_models_but_disconnect_does_not():
    panel=SpectrumPanel(_FakeSpectrumController())
    panel._automatic_calibration_started()
    panel._use_winspec_nm.setChecked(False)
    panel._automatic_calibration_finished(True)
    assert panel._use_winspec_nm.isChecked()
    panel._calibration_previous_nm=None
    panel._use_winspec_nm.setChecked(False)
    panel._automatic_calibration_finished(True)
    assert not panel._use_winspec_nm.isChecked()
    panel.close()
