"""Refresh costs are bounded without delaying source edits or Run admission."""
import gc
import weakref
from unittest.mock import patch

import numpy as np
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton

from app.scientific_plot_model import ScientificPlotModel
from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.quick_scientific_plot import QuickImageView, QuickScientificPlot, ScientificPlotItem, quick_spectrum_plot_class


def dispose(widget):
    teardown = getattr(widget, 'dispose_reloaded_page', None)
    if teardown:
        teardown()
    widget.close()
    widget.deleteLater()
    drain_qt_deletions()


def test_loop_population_emits_no_partial_edits_and_preserves_raw_values():
    quick_application()
    from ui import presets_panel as panel
    from PySide6.QtWidgets import QTableWidget
    table = QTableWidget(0, len(panel.LOOP_SCHEMA) + 1)
    events = []
    table.itemChanged.connect(lambda item: events.append(item.text()))
    frame = panel._DEFAULT_LOOP.astype(object).copy(deep=True)
    frame.loc[0, 'Values'] = 'unfinished, '
    frame.loc[0, 'Group'] = 'incomplete'
    frame.attrs['loop_row_ids'] = ['stable-id']
    try:
        panel._populate_loop_table(table, frame)
        assert not events
        assert not table.signalsBlocked() and table.updatesEnabled()
        raw = panel._read_loop_table_raw(table)[0]
        assert raw['Values'] == 'unfinished, ' and raw['Group'] == 'incomplete'
        assert raw['_run_id'] == 'stable-id'
        table.item(0, 2).setText('700, 800')
        assert events == ['700, 800']
        table.blockSignals(True)
        table.setUpdatesEnabled(False)
        panel._populate_loop_table(table, frame)
        assert table.signalsBlocked() and not table.updatesEnabled()
    finally:
        dispose(table)


def test_presets_construction_does_not_validate_intermediate_loop_rows():
    quick_application()
    from ui import presets_panel as panel
    original = panel.PresetsPanel._refresh_draft_state
    calls = []
    def count(self, *args, **kwargs):
        calls.append(self._loop_table.rowCount())
        return original(self, *args, **kwargs)
    with patch.object(panel.PresetsPanel, '_refresh_draft_state', count):
        widget = panel.PresetsPanel(bind_controller=False)
    try:
        assert len(calls) <= 4, calls  # Was 22; includes initial empty mode UI.
        assert widget._loop_table.rowCount() == len(widget._loop_src)
        assert widget._acquisition_schedule and not widget._tables_dirty
    finally:
        dispose(widget)


def test_metadata_burst_coalesces_presentation_but_fresh_run_rejects_latest_input():
    quick_application()
    from ui import presets_panel as panel
    widget = panel.PresetsPanel(bind_controller=False)
    try:
        widget._preview_timer.stop()
        with patch.object(widget, '_refresh_readiness', wraps=widget._refresh_readiness) as readiness:
            with patch.object(widget, '_refresh_filename_preview', wraps=widget._refresh_filename_preview) as filename:
                for i in range(20):
                    widget._laser_edit.setText(str(700 + i))
                assert widget._current_run_meta()['laser_nm'] == '719'
                assert not widget._run_btn.isEnabled()
                assert readiness.call_count == filename.call_count == 0
                QTest.qWait(170)
                assert readiness.call_count == filename.call_count == 1
        widget._sample_edit.setText('')
        widget._temp_edit.setText('4')
        metadata, error = widget._validate_before_run(defer_plan_checks=True)
        assert metadata is None and 'Sample ID' in error
        assert widget._preview_timer.isActive()  # Admission did not wait for debounce.
        widget._loop_table.item(0, 2).setText('invalid')
        assert widget._tables_dirty and widget._draft_issues
        assert not widget._run_btn.isEnabled() and not widget._apply_btn.isEnabled()
        sequence = widget._acquisition_schedule
        widget._on_apply()
        assert widget._acquisition_schedule is sequence
    finally:
        dispose(widget)


def test_immutable_curve_refresh_preserves_buffers_gaps_and_emits_nothing():
    model = ScientificPlotModel()
    x = np.arange(100_000.)
    y = np.sin(x)
    y[15] = np.nan
    connect = np.ones(len(x), bool)
    connect[100] = False
    curve = model.set_curve('live', x, y, connect=connect)
    snapshots = (curve.x, curve.y, curve.runs)
    events = []
    model.changed.connect(lambda: events.append(1))
    with patch.object(model, 'fit', wraps=model.fit) as fit:
        for _ in range(20):
            model.set_curve('live', curve.x, curve.y, connect=connect)
        assert fit.call_count == 0
    assert not events
    assert all(a is b for a, b in zip(snapshots, (curve.x, curve.y, curve.runs)))
    connect[10] = False
    assert curve.connect[10]  # Caller cannot mutate the published gap mask.
    model.set_curve('live', curve.x, curve.y, connect=connect)
    assert not curve.connect[10] and events


def test_borrowed_readonly_curve_is_owned_and_external_buffer_mutations_are_seen():
    model = ScientificPlotModel()
    source = np.arange(100.)
    readonly = source.view()
    readonly.setflags(write=False)
    curve = model.set_curve('ref', readonly, readonly)
    original = (curve.x, curve.y)
    model.set_curve('ref', readonly, readonly)
    assert curve.x is original[0] and curve.y is original[1]
    source[10] = 999
    assert curve.x[10] == 10 and curve.y[10] == 10
    model.set_curve('ref', readonly, readonly)
    assert curve.x[10] == 999 and curve.y[10] == 999
    np.testing.assert_array_equal(model.export_arrays()['ref'][1], source)


def test_range_noop_keeps_manual_admission_and_image_does_not_fit_twice():
    app = quick_application()
    model = ScientificPlotModel()
    changes, manual = [], []
    model.changed.connect(lambda: changes.append(1))
    model.manualRangeChanged.connect(lambda x, y: manual.append((x, y)))
    model.set_ranges(model.x_range, model.y_range, manual=True)
    assert not changes and manual == [(True, True)]
    assert not model.auto_x and not model.auto_y
    widget = QuickImageView()
    try:
        with patch.object(widget.model, 'fit', wraps=widget.model.fit) as fit:
            widget.set_image(np.arange(12).reshape(3, 4))
            assert fit.call_count == 1
            widget.setRange(xRange=[0.2, 1.8], yRange=[0.5, 2.5])
            bounds = widget.viewRange()
            widget.set_image(np.arange(12).reshape(3, 4) + 1, auto_range=False)
            assert fit.call_count == 1 and widget.viewRange() == bounds
        np.testing.assert_array_equal(widget.model.image, np.arange(12).reshape(3, 4) + 1)
    finally:
        dispose(widget)


def test_idle_plot_does_not_repaint_and_repeated_styles_do_not_invalidate_geometry():
    quick_application()
    calls = []
    original = ScientificPlotItem.paint
    def paint(self, painter):
        calls.append(1)
        return original(self, painter)
    with patch.object(ScientificPlotItem, 'paint', paint):
        widget = QuickScientificPlot()
        try:
            curve = widget.plot(np.arange(100.), np.arange(100.))
            widget.resize(600, 400)
            widget.show()
            QTest.qWait(100)
            settled = len(calls)
            events = []
            widget.model.changed.connect(lambda: events.append(1))
            for _ in range(10):
                curve.setVisible(True)
                curve.setPen(curve.opts['pen'])
                curve.setZValue(curve.zValue())
                curve.setData(curve.xData, curve.yData)
            QTest.qWait(150)
            assert settled > 0 and len(calls) == settled
            assert not events
        finally:
            dispose(widget)


def test_lazy_plot_uses_current_application_theme_and_engine_does_not_retain_destroyed_host():
    app = quick_application()
    old = app.property('quickThemeMode')
    app.setProperty('quickThemeMode', 'light')
    widget = QuickScientificPlot()
    try:
        app.setProperty('quickThemeMode', 'dark')
        view = widget.quick_widget
        engine = view.engine()
        assert view.rootObject().property('themeMode') == 'dark'
        reference = weakref.ref(widget)
        dispose(widget)
        del view, widget
        gc.collect()
        assert reference() is None
        assert engine is not None  # Shared engine continues to outlive its views.
    finally:
        app.setProperty('quickThemeMode', old)


def test_spectrum_footer_is_quick_and_actions_keep_exact_fit_policy():
    quick_application()
    widget = quick_spectrum_plot_class()()
    try:
        widget.update_spectrum(np.arange(700., 710.), np.arange(10.))
        widget.resize(650, 450)
        widget.show()
        QTest.qWait(80)
        assert widget._quick_footer_forms['forms']
        assert not any(button.isVisibleTo(widget) for button in widget.findChildren(QAbstractButton))
        adapter = next(form.adapter for form in widget._quick_footer_forms['forms']
                       if widget._fit_visible_btn in form.adapter._fields.values())
        field = next(field for field, source in adapter._fields.items() if source is widget._fit_visible_btn)
        with patch.object(widget, 'fit', wraps=widget.fit) as fit:
            assert adapter.trigger(field)
            fit.assert_called_with('visible')
        widget._plot.model.pan(.1, .1)
        assert not widget._autoscale_chk.isChecked()
        adapter = next(form.adapter for form in widget._quick_footer_forms['forms']
                       if widget._autoscale_chk in form.adapter._fields.values())
        field = next(field for field, source in adapter._fields.items() if source is widget._autoscale_chk)
        assert adapter.trigger(field)
        assert widget._autoscale_chk.isChecked() and widget._follow_y
    finally:
        dispose(widget)

def test_sequence_full_view_and_old_session_cannot_reset_application_dark_theme():
    app = quick_application()
    old = app.property('quickThemeMode')
    app.setProperty('quickThemeMode', 'light')
    from ui.quick_dual_gate_sequence import QuickDualGateSequencePreview
    from tests.test_dual_gate_sequence_model import flat_plan
    contexts, rows, tasks = flat_plan()
    widget = QuickDualGateSequencePreview()
    try:
        widget.update_plan(contexts, rows, acquisition_schedule=tasks)
        widget.show()
        QTest.qWait(30)
        state = widget.capture_view_state()
        state['theme'] = 'light'
        state['full_open'] = True
        state['full_state'] = {'theme': 'light'}
        app.setProperty('quickThemeMode', 'dark')
        widget.restore_view_state(state)
        QTest.qWait(30)
        assert widget.quick_widget.rootObject().property('themeMode') == 'dark'
        assert widget._full_sequence_preview.quick_widget.rootObject().property('themeMode') == 'dark'
        assert widget._theme_mode == widget._full_sequence_preview._theme_mode == 'dark'
    finally:
        dispose(widget)
        app.setProperty('quickThemeMode', old)

def test_connection_list_is_snapshotted_and_republishing_mutation_updates_real_gaps():
    model = ScientificPlotModel()
    connect = [True] * 5
    curve = model.set_curve('live', np.arange(5.), np.arange(5.), connect=connect)
    connect[1] = False
    assert len(curve.runs) == 1 and curve.connect[1]
    model.set_curve('live', curve.x, curve.y, connect=connect)
    assert [part.tolist() for part in curve.runs] == [[0, 1], [2, 3, 4]]
    assert not curve.connect[1]
