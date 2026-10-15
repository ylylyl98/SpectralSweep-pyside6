"""Exercise source replacement, state, Qt subscriptions and rollback without hardware."""
import copy
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Signal, QObject, Qt
from PySide6.QtWidgets import QApplication, QMainWindow, QStatusBar, QTabWidget, QWidget
from shiboken6 import isValid

from tests.test_spectrum_panel_controls import _FakeSpectrumController
from tests.test_winspec_scans import scan
from ui import page_reload
from ui.application_style import application_style
from ui.main_window import MainWindow
from ui.spectrum_panel import SpectrumPanel
from ui.signal_connections import SignalConnections
from utils.config import cfg


class Controller(_FakeSpectrumController):
    switching_lock_changed = Signal(bool)
    andor_status_ready = Signal(object)
    temperature_snapshot_ready = Signal(object)
    temperature_monitor_state = Signal(str)


@pytest.fixture
def shell(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    previous = copy.deepcopy(cfg.lf6)
    monkeypatch.setattr(cfg, 'save', Mock())
    monkeypatch.setattr('ui.detector_wavelength_advice.confirm_detector_wavelength', lambda *args: True)
    # Keep tests in this process independent of successful module replacement.
    modules = {name: sys.modules[name] for name in page_reload._MODULES}
    package = sys.modules['ui']
    window = MainWindow.__new__(MainWindow)
    QMainWindow.__init__(window)
    window._status = QStatusBar(window)
    window.setStatusBar(window._status)
    window._application_style = application_style(app)
    window._initialize_style_actions()
    window._lf6 = ctrl = Controller()
    ctrl.backend = 'lightfield'
    ctrl.identity = {'backend': 'lightfield', 'axis_unit': 'nm'}
    ctrl.shutdown = Mock()
    ctrl.disconnect_instrument = Mock()
    ctrl.set_cooler = Mock()
    window._spectrum = SpectrumPanel(ctrl)
    window._spectrum.set_sidebar_snapshot_provider(lambda: {'sample': 'test'})
    window._calibration = window._spectrum.create_calibration_page()
    window._tabs = QTabWidget(window)
    window.setCentralWidget(window._tabs)
    window._tabs.addTab(QWidget(), 'Other')
    window._tabs.addTab(window._spectrum, 'Spectrum')
    window._tabs.addTab(window._calibration, 'Calibration')
    window._tabs.setTabVisible(2, False)
    window._session_panels = {'spectrum': window._spectrum}
    window._tab_ids = {window._spectrum: 'spectrum', window._calibration: 'calibration'}
    window._tabs.setCurrentWidget(window._spectrum)
    window.resize(1200, 850)
    window.show()
    ctrl.connected.emit([])
    app.processEvents()
    ctrl.set_temperature_monitor_paused = Mock(wraps=ctrl.set_temperature_monitor_paused)
    yield window
    window._spectrum.unbind_controller_signals()
    window._spectrum.detach_calibration_page()
    window.hide()  # MainWindow.closeEvent would shut down the real shell services.
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    for name, module in modules.items():
        sys.modules[name] = module
        setattr(package, name.rsplit('.', 1)[1], module)
    cfg.lf6 = previous


def test_reloads_real_source_preserves_data_and_does_not_touch_hardware(shell, tmp_path, monkeypatch):
    old = shell._spectrum
    x, y = np.arange(10.) + 600, np.arange(10.) + 1
    snapshot = {'instrument_identity': {'backend': 'lightfield', 'axis_unit': 'nm'}, 'sample_id': 'saved sample'}
    old.push_spectrum(x, y, snapshot)
    old.push_frame(np.arange(24.).reshape(4, 6), snapshot)
    ref = old.add_reference(x, y, auto_save=False)
    ref['_row_name'].setText('unsaved draft name')
    ref['_row_checkbox'].setChecked(False)
    old._reference_list.setCurrentRow(0)
    old._exposure.setValue(345)
    old._comparison_mode.setCurrentText('Compare')
    old._normalize_references_chk.setChecked(True)
    old._spec_plot._autoscale_chk.setChecked(False)
    old._spec_plot._plot.setRange(xRange=(602, 607), yRange=(0, 2), padding=0)
    old._frame_plot._view.setLevels(2, 17)
    old._tabs.setCurrentIndex(1)
    old._side_panel.setSizes([320, 240])
    calibration = shell._calibration
    calibration.result.setText('Unfinished calibration work')
    calibration.record = {'draft': True}
    calibration.open_broad()
    old_plot_view = old._spec_plot.capture_view()
    original_path = page_reload._source_path
    path = tmp_path / 'spectrum_panel.py'
    path.write_bytes(original_path('ui.spectrum_panel').read_bytes().replace(
        b'QPushButton("Apply")', b'QPushButton("Apply new UI")'))
    monkeypatch.setattr(page_reload, '_source_path', lambda name: path if name == 'ui.spectrum_panel' else original_path(name))
    new = page_reload.reload_spectrum_page(shell)
    assert new is not old and new._ctrl is old._ctrl
    assert new._apply_btn.text() == 'Apply new UI'
    assert new._exposure.value() == 345
    assert shell._session_panels['spectrum'] is new
    assert shell._tab_ids[new] == 'spectrum' and old not in shell._tab_ids
    assert shell._tabs.currentWidget() is new
    assert shell._calibration is calibration and new._wavelength_dialog is calibration
    assert calibration.result.text() == 'Unfinished calibration work'
    assert calibration.record == {'draft': True}
    assert calibration.broad_dialog.save_callback.__self__ is new
    assert not shell._tabs.isTabVisible(shell._tabs.indexOf(calibration))
    np.testing.assert_array_equal(new._last_data, np.arange(24.).reshape(4, 6))
    np.testing.assert_array_equal(new._spectrum_data[1], y)
    assert new._last_acquisition_snapshot['sample_id'] == 'saved sample'
    assert new._tabs.currentIndex() == 1
    assert new._reference_panel.isVisible() and new._data_scroll.isVisible()
    assert new._frame_plot._view.getLevels() == (2, 17)
    assert new._spec_plot.capture_view() == old_plot_view
    reference = new.references[0]
    assert reference['id'] == ref['id']
    assert reference['name'] == 'unsaved draft name'
    assert not reference['visible'] and not reference['saved']
    assert not reference['_row_checkbox'].isChecked()
    assert new._retry_reference_btn.isEnabled()
    assert new._sidebar_snapshot_provider() == {'sample': 'test'}
    assert not shell._lf6.apply_calls
    shell._lf6.shutdown.assert_not_called()
    shell._lf6.disconnect_instrument.assert_not_called()
    shell._lf6.set_cooler.assert_not_called()
    shell._lf6.set_temperature_monitor_paused.assert_not_called()
    cfg.save.assert_not_called()
    calibration.capture_editors[1].setValue(456)
    assert new._exposure.value() == 456 and old._exposure.value() == 345
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(old)


def test_repeated_reload_receives_each_signal_once_and_keeps_other_subscribers(shell):
    ctrl = shell._lf6
    other = []
    ctrl.error.connect(other.append)
    for _ in range(4):
        new = page_reload.reload_spectrum_page(shell)
        ctrl.switching_lock_changed.emit(False)
        ctrl.error.emit('test error')
        assert new._error_details.toPlainText() == 'test error'
    assert other == ['test error'] * 4
    new._acquire_btn.click()
    assert len(ctrl.apply_calls) == 1
    ctrl.settings_applied.emit()
    assert ctrl.acquire_1d_calls == 1
    ctrl.spectrum_ready.emit(np.arange(3.) + 650, np.arange(3.))
    assert new._acquire_btn.isEnabled()
    assert ctrl.acquire_1d_calls == 1


def test_cold_andor_cached_controls_and_temperature_survive_without_refresh(shell):
    ctrl, old = shell._lf6, shell._spectrum
    ctrl.backend = 'andor_sdk2'
    ctrl.identity = {'backend': 'andor_sdk2', 'camera_role': 'ingaas', 'shamrock_connected': True}
    ctrl.refresh_andor_status = Mock()
    ctrl.connected.emit([])
    ctrl.andor_status_ready.emit({
        **ctrl.identity, 'temperature_c': -86.4, 'cooler_on': True,
        'temperature_setpoint_c': -90, 'temperature_range_c': [-100, 20],
        'grating': 1, 'wavelength_nm': 1050, 'detector_size': [512, 1],
    })
    old._andor_controls.temperature_target.setValue(-95)
    old._andor_toggle.setChecked(True)
    ctrl.shamrock_connection_changed.emit(False)
    ctrl.refresh_andor_status.reset_mock()
    ctrl.set_temperature_monitor_paused.reset_mock()
    new = page_reload.reload_spectrum_page(shell)
    assert new._andor_controls.cooler.isChecked()
    assert '-86.4' in new._andor_controls.cooling_status.text()
    assert new._andor_controls.temperature_target.value() == -95
    assert not new._andor_controls._shamrock_connected
    assert not new._andor_controls.apply_button.isEnabled()
    assert new._andor_toggle.isChecked()
    ctrl.refresh_andor_status.assert_not_called()
    ctrl.set_cooler.assert_not_called()
    ctrl.set_temperature_monitor_paused.assert_not_called()


def test_reload_keeps_real_controller_thread_and_mock_camera_session(shell, scan):
    from tests.test_spectrum_measurement_ownership import drain_until
    ctrl, setup, camera, live = scan
    old = shell._spectrum
    old.unbind_controller_signals()
    old._ctrl = ctrl
    old._andor_controls._ctrl = ctrl
    old.bind_controller_signals()
    ctrl.connected.emit([])
    drain_until(lambda: not ctrl.switching_locked)
    thread = ctrl._thread
    before = list(camera.calls), list(setup.lightfield.calls)
    new = page_reload.reload_spectrum_page(shell)
    assert ctrl._thread is thread and thread.isRunning()
    assert new._ctrl is ctrl and ctrl.setup is setup
    assert before == (list(camera.calls), list(setup.lightfield.calls))
    new._center.setValue(1050)
    new._acquire_btn.click()
    drain_until(lambda: new._last_data is not None and ctrl._acquisition_requests == 0)
    assert new._last_data.size == 512
    assert new._acquire_btn.isEnabled()


@pytest.mark.parametrize('failure', ['syntax', 'constructor', 'restore', 'bind'])
def test_failure_retains_original_page_modules_calibration_and_connections(shell, tmp_path, monkeypatch, failure):
    old, calibration = shell._spectrum, shell._calibration
    original_modules = {name: sys.modules[name] for name in page_reload._MODULES}
    original_path = page_reload._source_path
    if failure in {'syntax', 'constructor', 'bind'}:
        path = tmp_path / 'spectrum_panel.py'
        source = original_path('ui.spectrum_panel').read_text(encoding='utf-8')
        if failure == 'syntax':
            source += '\ninvalid syntax !'
        elif failure == 'constructor':
            source = source.replace('        self._wire()\n', '        raise RuntimeError("broken build")\n')
        else:
            source = source.replace('    def bind_controller_signals(self) -> None:\n',
                                    '    def bind_controller_signals(self) -> None:\n        raise RuntimeError("broken bind")\n')
        path.write_text(source, encoding='utf-8')
        monkeypatch.setattr(page_reload, '_source_path', lambda name: path if name == 'ui.spectrum_panel' else original_path(name))
    else:
        monkeypatch.setattr(page_reload, 'restore_spectrum_state', Mock(side_effect=ValueError('bad state')))
    shell._reload_spectrum_page()
    assert shell._spectrum is old and isValid(old)
    assert shell._tabs.currentWidget() is old
    assert old._wavelength_dialog is calibration
    assert all(sys.modules[name] is module for name, module in original_modules.items())
    assert 'reload failed' in shell._status.currentMessage()
    calibration.capture_editors[1].setValue(246)
    assert old._exposure.value() == 246
    old._acquire_btn.click()
    shell._lf6.settings_applied.emit()
    assert len(shell._lf6.apply_calls) == shell._lf6.acquire_1d_calls == 1


@pytest.mark.parametrize('busy', ['continuous', 'queued', 'settings', 'controller', 'calibration', 'analysis', 'other_workflow', 'saving'])
def test_busy_operation_blocks_before_loading_code(shell, monkeypatch, busy):
    old = shell._spectrum
    if busy == 'continuous': old._continuous_mode = '1d'
    if busy == 'queued': old._pending_acquisition = '1d'
    if busy == 'settings': old._action_controls_requested = False
    if busy == 'controller': shell._lf6.switching_locked = True
    if busy == 'calibration': old._calibration_capture_pending = True
    if busy == 'analysis': shell._calibration.broad_dialog = SimpleNamespace(analysis_running=True)
    if busy == 'other_workflow': shell._power_sweep_running = True
    if busy == 'saving': old._metadata_finalizing = True
    loader = Mock(side_effect=AssertionError('must not load source'))
    monkeypatch.setattr(page_reload, '_fresh_spectrum_modules', loader)
    shell._reload_spectrum_page()
    assert shell._spectrum is old
    loader.assert_not_called()
    assert 'finish' in shell._status.currentMessage()


def test_calibration_visibility_is_explicit_and_not_restored_by_session(shell):
    index = shell._tabs.indexOf(shell._calibration)
    assert not shell._tabs.isTabVisible(index)
    shell._restore_state({'active_tab': 'calibration'})
    assert shell._tabs.currentWidget() is shell._spectrum
    action = shell._show_calibration_action
    action.setChecked(True)
    assert shell._tabs.isTabVisible(index)
    assert shell._tabs.currentWidget() is shell._calibration
    action.setChecked(False)
    assert not shell._tabs.isTabVisible(index)
    assert shell._tabs.currentWidget() is shell._spectrum
    assert shell._calibration.record is None


def test_disconnected_subscriptions_drop_already_queued_callback():
    app = QApplication.instance() or QApplication([])
    class Emitter(QObject):
        signal = Signal()
    owner, emitter, calls = QObject(), Emitter(), []
    connections = SignalConnections(owner)
    # Emitting from a worker queues delivery to the subscribing GUI thread.
    import threading
    connections.connect(emitter.signal, lambda: calls.append(True))
    worker = threading.Thread(target=emitter.signal.emit)
    worker.start(); worker.join()
    connections.clear()
    app.processEvents()
    assert not calls
