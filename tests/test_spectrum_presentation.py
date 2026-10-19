"""Presentation state transitions without devices or user data."""
import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPointF, QPoint
from PySide6.QtGui import QFocusEvent
from PySide6.QtWidgets import QApplication

from tests.test_spectrum_panel_controls import _FakeSpectrumController
from ui.spectrum_panel import SpectrumPanel


@pytest.fixture
def panel():
    app = QApplication.instance() or QApplication([])
    controller = _FakeSpectrumController()
    controller.backend = 'lightfield'
    controller.identity = {'backend': 'lightfield', 'axis_unit': 'nm'}
    widget = SpectrumPanel(controller)
    widget.resize(1100, 750)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    app.processEvents()


def test_empty_plot_transitions_follow_visible_finite_data(panel):
    plot = panel._spec_plot
    assert plot._display_stack.currentWidget() is plot._empty_label
    assert not plot._fit_visible_btn.isEnabled()
    ref = panel.add_reference(np.array([650., 651.]), np.array([2., 3.]), auto_save=False)
    assert plot._display_stack.currentWidget() is plot._plot_container
    assert not plot._fit_live_btn.isEnabled()
    assert plot._fit_visible_btn.isEnabled()
    panel._set_reference_visible(ref['id'], False)
    assert plot._display_stack.currentWidget() is plot._empty_label
    panel.push_spectrum(np.array([650., 651.]), np.array([2., 3.]))
    assert plot._display_stack.currentWidget() is plot._plot_container
    assert plot._fit_live_btn.isEnabled()
    plot.update_spectrum([650.], [np.nan])
    assert plot._display_stack.currentWidget() is plot._empty_label


def test_reference_actions_follow_selection_and_saved_state(panel):
    assert not panel._details_reference_btn.isEnabled()
    assert panel._retry_reference_btn.isHidden()
    ref = panel.add_reference(np.array([650., 651.]), np.array([2., 3.]), auto_save=False)
    panel._reference_list.setCurrentRow(0)
    assert panel._details_reference_btn.isEnabled()
    assert panel._retry_reference_btn.isEnabled()
    assert not panel._retry_reference_btn.isHidden()
    ref['saved'] = True
    panel._refresh_reference_item(ref)
    assert not panel._retry_reference_btn.isEnabled()
    assert panel._retry_reference_btn.isHidden()
    panel._reference_list.setCurrentRow(-1)
    assert not panel._details_reference_btn.isEnabled()
    assert not panel._reference_menu_actions[0][0].isEnabled()
    hide = next(a for a, button in panel._reference_menu_actions if button is panel._hide_all_btn)
    hide.trigger()
    assert not ref['visible']


def test_focusing_reference_name_selects_its_actions(panel):
    first = panel.add_reference(np.array([650., 651.]), np.array([2., 3.]), auto_save=False)
    second = panel.add_reference(np.array([650., 651.]), np.array([3., 4.]), auto_save=False)
    panel._reference_list.setCurrentRow(0)
    QApplication.sendEvent(second['_row_name'], QFocusEvent(QEvent.Type.FocusIn))
    assert panel._selected_reference() is second
    assert panel._details_reference_btn.isEnabled()
    assert first['visible']


def test_disconnected_status_and_temperature_notice_stay_truthful(panel):
    assert panel._status_lbl.text() == 'Disconnected'
    assert panel._winspec_temperature_label.isHidden()
    panel._ctrl.connected.emit([])
    assert panel._status_lbl.text() == 'Ready'
    assert panel._winspec_temperature_label.isHidden()
    panel._ctrl.identity = {'backend': 'winspec_ingaas', 'camera_role': 'ingaas'}
    panel._update_device_status()
    assert not panel._winspec_temperature_label.isHidden()
    assert panel._acquisition_details.isHidden()
    panel._ctrl.disconnected.emit()
    assert panel._winspec_temperature_label.isHidden()
    assert panel._status_lbl.text() == 'Disconnected'


def test_collapsed_panels_keep_curves_and_session_preferences(panel):
    panel.push_spectrum(np.array([650., 651.]), np.array([2., 3.]))
    panel._details_toggle.setChecked(True)
    panel._side_panel.setSizes([300, 250])
    panel._references_toggle.setChecked(False)
    state = panel.capture_session_state()
    np.testing.assert_array_equal(panel._spec_plot._curve.yData, [2., 3.])
    panel._details_toggle.setChecked(False)
    panel._references_toggle.setChecked(True)
    panel._side_panel.setSizes([450, 100])
    panel.restore_session_state(state)
    assert not panel._acquisition_details.isHidden()
    assert panel._side_panel.isHidden()


def test_instrument_controls_remain_above_plot_when_side_panel_is_hidden(panel):
    panel._references_toggle.setChecked(False)
    QApplication.processEvents()
    assert panel._side_panel.isHidden()
    assert panel._apply_optics_btn.isVisible()
    assert panel._center.isVisible()
    assert panel._apply_optics_btn.mapTo(panel, QPoint(0, panel._apply_optics_btn.height())).y() < panel._content_splitter.y()
    assert panel._acquire_btn.isVisible()
    assert panel._stop_btn.isVisible()
    panel._references_toggle.setChecked(True)
    assert panel._reference_panel.isVisible()
    assert panel._source_label.isVisible()
    assert panel._save_root_lbl.isVisible()
    assert panel._save_btn.isVisible()
    assert panel._acquire_btn.isVisible()


def test_previous_side_tab_indices_migrate_without_hiding_instrument(panel):
    panel.restore_session_state({'side_tab': 2})
    assert panel._data_scroll.isVisible()
    panel.restore_session_state({'side_tab': 1})
    assert panel._reference_panel.isVisible()
    assert panel._apply_optics_btn.isVisible()


def test_frame_clear_restores_empty_state(panel):
    frame = panel._frame_plot
    assert frame._display_stack.currentWidget() is frame._empty_label
    panel.push_frame(np.ones((4, 5)))
    assert frame._display_stack.currentWidget() is frame._view
    frame.clear_frame()
    assert frame._view.image is None
    assert frame._display_stack.currentWidget() is frame._empty_label


def test_plot_right_edge_and_optics_apply_fit_with_wide_reference_pane(panel):
    panel.resize(1000, 800)
    x = np.linspace(600., 800., 101)
    panel.push_spectrum(x, np.linspace(1., 3., 101))
    panel.add_reference(x, np.ones(101), auto_save=False)
    panel._content_splitter.setSizes([400, 580])
    QApplication.processEvents()
    panel.grab()  # Settle the graphics layout and axis label metrics.
    plot = panel._spec_plot._plot
    view = plot.getViewBox()
    assert view.sceneBoundingRect().right() < plot.viewport().width() - 10
    assert view.mapViewToScene(QPointF(800., 3.)).x() < plot.viewport().width() - 10
    apply_button = panel._apply_optics_btn
    assert not apply_button.isHidden()
    right = apply_button.mapTo(panel, QPoint(apply_button.width(), 0)).x()
    assert right <= panel.width()
    assert panel._spec_plot.width() < panel._content_splitter.width() * .65
