"""Setup ownership and real plot geometry regressions (no hardware)."""
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from tests.test_spectrum_panel_controls import _FakeSpectrumController
from ui.spectrum_panel import SpectrumPanel


@pytest.fixture
def panel():
    app = QApplication.instance() or QApplication([])
    ctrl = _FakeSpectrumController()
    ctrl.backend = 'lightfield'
    ctrl.identity = {'backend': 'lightfield', 'axis_unit': 'nm'}
    widget = SpectrumPanel(ctrl)
    ctrl.connected.emit([])
    widget.resize(1200, 800)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    app.processEvents()


def switch(panel, backend):
    panel._ctrl.backend = backend
    panel._ctrl.identity = {'backend': 'andor_sdk2' if backend.startswith('andor') else backend,
                            'camera_role': 'ingaas' if backend.endswith('ingaas') else 'si',
                            'axis_unit': 'nm'}
    panel._ctrl.connected.emit([])
    QApplication.processEvents()


def spectrum(panel, start=650, stop=750, peak=10):
    wl = np.linspace(start, stop, 101)
    cts = np.linspace(0, peak, 101)
    panel.push_spectrum(wl, cts, {'instrument_identity': dict(panel._ctrl.identity)})
    QApplication.processEvents()
    return wl, cts


def reference(panel, start=650, stop=750, peak=10):
    wl, cts = spectrum(panel, start, stop, peak)
    return panel.add_reference(wl, cts, auto_save=False)


def test_switch_filters_references_without_changing_user_visibility(panel):
    a = reference(panel)
    switch(panel, 'andor_ingaas')
    assert a['visible']
    assert not panel._spec_plot._reference_curves[a['id']].isVisible()
    b = reference(panel, 1100, 1300)
    panel._hide_all_btn.click()
    assert a['visible']  # Batch actions affect the current setup only.
    assert not b['visible']
    switch(panel, 'lightfield')
    assert panel._spec_plot._reference_curves[a['id']].isVisible()
    assert not panel._spec_plot._reference_curves[b['id']].isVisible()


def test_switch_restores_both_frames_and_manual_view(panel):
    wl, cts = spectrum(panel)
    frame = np.arange(12).reshape(3, 4)
    panel.push_frame(frame, {'instrument_identity': dict(panel._ctrl.identity)})
    panel._spec_plot._plot.setRange(xRange=(675, 705), yRange=(1, 7), padding=0)
    before = panel._spec_plot._plot.viewRange()
    switch(panel, 'andor_ingaas')
    assert panel._last_data is None
    assert not panel._save_btn.isEnabled()
    assert panel._spec_plot._curve.xData is None or panel._spec_plot._curve.xData.size == 0
    spectrum(panel, 1100, 1300)
    switch(panel, 'lightfield')
    np.testing.assert_array_equal(panel._last_data, frame)
    np.testing.assert_array_equal(panel._spec_plot._curve.xData, wl)
    np.testing.assert_array_equal(panel._spec_plot._curve.yData, cts)
    np.testing.assert_array_equal(panel._frame_plot._view.image, frame.T)
    np.testing.assert_allclose(panel._spec_plot._plot.viewRange(), before)


def test_reference_ownership_uses_frozen_acquisition(panel):
    wl, cts = spectrum(panel)
    snapshot = panel._last_acquisition_snapshot.copy()
    switch(panel, 'andor_ingaas')
    ref = panel.add_reference(wl, cts, settings_snapshot=snapshot, auto_save=False)
    assert not panel._spec_plot._reference_curves[ref['id']].isVisible()
    switch(panel, 'lightfield')
    assert panel._spec_plot._reference_curves[ref['id']].isVisible()


def test_run_range_follows_live_not_distant_or_stronger_reference(panel):
    reference(panel, 1100, 1300, 100000)
    spectrum(panel)
    xmin, xmax = panel._spec_plot._plot.viewRange()[0]
    ymin, ymax = panel._spec_plot._plot.viewRange()[1]
    assert xmin < 650 < 750 < xmax < 800
    assert ymin <= 0 < 10 <= ymax < 20
    panel._spec_plot._plot.setRange(xRange=(680, 700), yRange=(2, 5), padding=0)
    before = panel._spec_plot._plot.viewRange()
    spectrum(panel, peak=20)
    np.testing.assert_allclose(panel._spec_plot._plot.viewRange(), before)


def test_compare_splits_gaps_merges_overlaps_and_preserves_real_coordinates(panel):
    a = reference(panel)
    switch(panel, 'andor_ingaas')
    b = reference(panel, 1100, 1300)
    panel._comparison_mode.setCurrentText('Compare')
    panel._hide_gaps_chk.setChecked(True)
    QApplication.processEvents()
    plots = panel._spec_plot.segment_plots
    assert len(plots) == 2
    assert plots[0].viewRange()[0][1] < 800
    assert plots[1].viewRange()[0][0] > 1000
    np.testing.assert_allclose(plots[0].viewRange()[1], plots[1].viewRange()[1])
    np.testing.assert_array_equal(panel._spec_plot._reference_curves[a['id']].xData, a['wavelength'])
    np.testing.assert_array_equal(b['counts'], np.linspace(0, 10, 101))
    original_plots = tuple(plots)
    spectrum(panel, 1100, 1300, 11)
    assert tuple(panel._spec_plot.segment_plots) == original_plots
    # A bridging spectrum covers the gap, so the comparison becomes continuous.
    panel.add_reference(np.linspace(700, 1150, 100), np.zeros(100), auto_save=False)
    assert len(panel._spec_plot.segment_plots) == 1


def test_compare_can_restore_continuous_axis_and_fit_live(panel):
    reference(panel)
    switch(panel, 'andor_ingaas')
    reference(panel, 1100, 1300)
    panel._comparison_mode.setCurrentText('Compare')
    panel._hide_gaps_chk.setChecked(True)
    panel._hide_gaps_chk.setChecked(False)
    xmin, xmax = panel._spec_plot._plot.viewRange()[0]
    assert xmin < 650 < 1300 < xmax
    panel._spec_plot._fit_live_btn.click()
    xmin, xmax = panel._spec_plot._plot.viewRange()[0]
    assert 1000 < xmin < 1100 < 1300 < xmax
    panel._spec_plot._fit_visible_btn.click()
    assert panel._spec_plot._plot.viewRange()[0][0] < 650


def test_pixel_references_are_never_mixed_with_nm_or_gap_compressed(panel):
    ref = panel.add_reference([1, 2, 3], [2, 3, 4], axis_unit='pixel', auto_save=False)
    spectrum(panel)
    panel._comparison_mode.setCurrentText('Compare')
    panel._hide_gaps_chk.setChecked(True)
    assert not panel._spec_plot._reference_curves[ref['id']].isVisible()
    panel.push_spectrum([1, 2, 3], [3, 2, 1], {'instrument_identity': {'axis_unit': 'pixel'}})
    assert len(panel._spec_plot.segment_plots) == 1
    assert not panel._hide_gaps_chk.isEnabled()


def test_normalization_changes_axis_label_and_preserves_saved_arrays(panel):
    ref = reference(panel)
    panel._normalize_references_chk.setChecked(True)
    assert panel._spec_plot._plot.getAxis('left').labelText == 'Normalized intensity'
    np.testing.assert_array_equal(ref['counts'], np.linspace(0, 10, 101))


def test_loaded_spectrum_recovers_its_setup_from_saved_metadata(panel, tmp_path):
    spectrum(panel)
    output = tmp_path / 'reference.csv'
    panel.save_current(output)
    switch(panel, 'andor_ingaas')
    loaded = panel.load_spectra([output])[0]
    assert not panel._spec_plot._reference_curves[loaded['id']].isVisible()
    assert loaded['setup_key'] == 'lightfield'
    panel._comparison_mode.setCurrentText('Compare')
    assert panel._spec_plot._reference_curves[loaded['id']].isVisible()
    assert 'LightField' in loaded['_row_source'].text()


def test_compare_removal_and_changed_wavelengths_update_segments(panel):
    ref = reference(panel)
    spectrum(panel, 1100, 1300)
    panel._comparison_mode.setCurrentText('Compare')
    assert '350 nm' in panel._spec_plot._break_widgets[0].text()
    spectrum(panel, 1200, 1400)
    assert '450 nm' in panel._spec_plot._break_widgets[0].text()
    panel.remove_reference(ref['id'])
    assert len(panel._spec_plot.segment_plots) == 1
    assert panel._spec_plot._plot.viewRange()[0][0] > 1150


def test_comparison_preferences_round_trip_without_persisting_acquisitions(panel):
    spectrum(panel)
    panel._comparison_mode.setCurrentText('Compare')
    panel._hide_gaps_chk.setChecked(False)
    panel._normalize_references_chk.setChecked(True)
    state = panel.capture_session_state()
    restored = SpectrumPanel()
    try:
        restored.restore_session_state(state)
        assert restored._comparison_mode.currentText() == 'Compare'
        assert not restored._hide_gaps_chk.isChecked()
        assert restored._normalize_references_chk.isChecked()
        assert restored._last_data is None
        assert restored.references == []
    finally:
        restored.close()


def test_small_unsampled_gap_and_zero_intensity_do_not_break_axis(panel):
    reference(panel, 650, 750, 0)
    spectrum(panel, 755, 850, 0)
    panel._comparison_mode.setCurrentText('Compare')
    assert len(panel._spec_plot.segment_plots) == 1
    xmin, xmax = panel._spec_plot._plot.viewRange()[0]
    assert xmin < 650 < 850 < xmax


def test_segment_y_zoom_is_shared_and_stays_fixed_during_live_updates(panel):
    reference(panel)
    spectrum(panel, 1100, 1300)
    panel._comparison_mode.setCurrentText('Compare')
    second = panel._spec_plot.segment_plots[1]
    second.setYRange(2, 4, padding=0)
    second.getViewBox().sigRangeChangedManually.emit([False, True])
    spectrum(panel, 1100, 1300, 100)
    for plot in panel._spec_plot.segment_plots:
        np.testing.assert_allclose(plot.viewRange()[1], [2, 4])


def test_internal_wavelength_gap_does_not_draw_a_bridge(panel):
    wl = np.r_[np.arange(650., 751.), np.arange(1100., 1301.)]
    counts = np.ones(wl.size)
    panel.push_spectrum(wl, counts, {'instrument_identity': {'backend': 'lightfield'}})
    panel._comparison_mode.setCurrentText('Compare')
    assert len(panel._spec_plot.segment_plots) == 2
    assert not panel._spec_plot._curve.opts['connect'][100]
    np.testing.assert_array_equal(panel._spec_plot._curve.xData, wl)


def test_changed_coverage_preserves_manual_y_and_initializes_new_segment_x(panel):
    reference(panel)
    spectrum(panel, 1100, 1300)
    panel._comparison_mode.setCurrentText('Compare')
    panel._spec_plot._plot.setRange(xRange=(670, 710), yRange=(2, 4), padding=0)
    spectrum(panel, 1200, 1400)
    plots = panel._spec_plot.segment_plots
    np.testing.assert_allclose(plots[0].viewRange()[0], [670, 710])
    assert 1150 < plots[1].viewRange()[0][0] < 1200 < 1400 < plots[1].viewRange()[0][1]
    for plot in plots:
        np.testing.assert_allclose(plot.viewRange()[1], [2, 4])


def test_switch_clears_hidden_reference_selection_before_remove(panel):
    ref = reference(panel)
    panel._reference_list.setCurrentRow(0)
    switch(panel, 'andor_ingaas')
    panel._remove_reference_btn.click()
    assert panel.references == [ref]
    assert panel._selected_reference() is None


def test_restoring_compare_preferences_keeps_auto_y_disabled(panel):
    panel._comparison_mode.setCurrentText('Compare')
    panel._spec_plot._autoscale_chk.setChecked(False)
    restored = SpectrumPanel()
    try:
        restored.restore_session_state(panel.capture_session_state())
        assert not restored._spec_plot._autoscale_chk.isChecked()
    finally:
        restored.close()


def test_restored_view_matches_bands_after_comparison_selection_changes(panel):
    reference(panel)
    panel.add_reference(np.arange(1100., 1301.), np.ones(201), auto_save=False)
    panel._comparison_mode.setCurrentText('Compare')
    panel._spec_plot._plot.setRange(xRange=(670, 710), yRange=(2, 4), padding=0)
    switch(panel, 'andor_ingaas')
    reference(panel, 450, 500)
    switch(panel, 'lightfield')
    plots = panel._spec_plot.segment_plots
    assert len(plots) == 3
    assert plots[0].viewRange()[0][0] < 450 < 500 < plots[0].viewRange()[0][1]
    np.testing.assert_allclose(plots[1].viewRange()[0], [670, 710])
    assert 1000 < plots[2].viewRange()[0][0] < 1100 < 1300 < plots[2].viewRange()[0][1]
    for plot in plots:
        np.testing.assert_allclose(plot.viewRange()[1], [2, 4])


def test_compare_chosen_on_another_setup_still_applies_when_switching_back(panel):
    reference(panel)
    switch(panel, 'andor_ingaas')
    reference(panel, 1100, 1300)
    panel._comparison_mode.setCurrentText('Compare')
    switch(panel, 'lightfield')
    assert len(panel._spec_plot.segment_plots) == 2


def test_segments_use_same_pixels_per_nm_after_axis_layout(panel):
    reference(panel)
    spectrum(panel, 1100, 1300)
    panel._comparison_mode.setCurrentText('Compare')
    for _ in range(6):
        QApplication.processEvents()
    scales = [plot.getViewBox().width() / np.diff(plot.viewRange()[0])[0]
              for plot in panel._spec_plot.segment_plots]
    assert abs(scales[0] - scales[1]) / max(scales) < .015


def test_normalize_refits_y_without_losing_x_zoom(panel):
    reference(panel, peak=1000)
    panel._spec_plot._plot.setRange(xRange=(670, 710), yRange=(100, 400), padding=0)
    panel._normalize_references_chk.setChecked(True)
    xmin, xmax = panel._spec_plot._plot.viewRange()[0]
    ymin, ymax = panel._spec_plot._plot.viewRange()[1]
    assert (xmin, xmax) == (670, 710)
    assert ymin <= 0 < 1 <= ymax < 2


def test_normalized_view_does_not_restore_another_setups_raw_y_scale(panel):
    reference(panel, peak=1000)
    panel._spec_plot._plot.setYRange(100, 400, padding=0)
    switch(panel, 'andor_ingaas')
    reference(panel, 1100, 1300, 5000)
    panel._normalize_references_chk.setChecked(True)
    switch(panel, 'lightfield')
    ymin, ymax = panel._spec_plot._plot.viewRange()[1]
    assert ymin <= 0 < 1 <= ymax < 2


def test_load_with_normalization_updates_live_curve_and_axis_label(panel, tmp_path):
    spectrum(panel, peak=1000)
    path = tmp_path / 'external.csv'
    path.write_text('wavelength_nm,intensity_counts\n650,20\n750,100\n', encoding='utf-8')
    ref = panel.load_spectra([path], normalize=True)[0]
    assert panel._normalize_references_chk.isChecked()
    assert panel._spec_plot._plot.getAxis('left').labelText == 'Normalized intensity'
    assert np.max(panel._spec_plot._curve.yData) == 1.
    np.testing.assert_array_equal(ref['counts'], [20, 100])
