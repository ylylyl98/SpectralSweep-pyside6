"""Loop ordering, independent setups and live detector reminders; simulated only."""
import os
import threading
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pandas as pd
import pytest
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QCheckBox, QMessageBox

from ui import presets_panel as panel
from tests.test_dual_gate_multi_setup import loops, batch, Device, Detector
from tests.test_dual_gate_setup_picker import open_picker, choose
from tests import test_smu_resilience as fixtures


@pytest.fixture(scope='module', autouse=True)
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def widget():
    result = panel.PresetsPanel()
    result._load_dual_setup_recipe()
    yield result
    result.close()


@pytest.mark.parametrize('mode', ['Customized', 'Synchronize'])
def test_separate_setup_and_center_groups_expand_all_combinations(mode):
    definition = loops().iloc[:2].copy()
    definition.loc[1, 'Group'] = 2
    sequence, _, _ = panel._build_plan(definition, batch(), mode)
    assert [(ctx['Measurement setup'], ctx['Center Wavelength (nm)']) for ctx in sequence] == [
        ('lightfield', 730.), ('lightfield', 1150.),
        ('winspec_ingaas', 730.), ('winspec_ingaas', 1150.)]


@pytest.mark.parametrize('order', [
    ['group:1', 'conditions', 'points', 'group:2'],
    ['group:2', 'conditions', 'points', 'group:1'],
])
def test_independent_groups_acquire_complete_recipes_and_default_exposure(tmp_path, monkeypatch, order):
    definition = loops().iloc[:2].copy()
    definition.loc[1, 'Group'] = 2
    sequence, rows, _ = panel._build_plan(definition, batch(), 'Customized')
    schedule = panel._build_nested_execution_schedule(definition, batch(), mode='Customized', execution_order=order)
    events = []
    device = Device(events)
    detector = Detector(events, device)
    metadata = fixtures.SMUResilienceTests._run_meta()
    metadata.update(initial_voltage_settle_s=0., voltage_settle_s=0., spectrometer_defaults={
        'Center Wavelength (nm)': 800., 'Exposure Time (ms)': 234., 'Accumulations (EPF)': 4})
    worker = panel._RunWorker(sequence, rows, lf6_ctrl=detector,
        smu_ctrl=SimpleNamespace(is_connected=True, device=device), out_dir=tmp_path,
        run_meta=metadata, filename_parts=['center'], stop_event=threading.Event(), acquisition_schedule=schedule)
    result = []
    worker.finished.connect(lambda *args: result.append(args))
    monkeypatch.setattr(panel.cfg.ramp, 'delay_s', 0.)
    worker.run()
    assert result[-1][0], result
    captures = [event for event in events if event[0] == 'acquire']
    assert len(captures) == 8
    assert {(event[1], event[2], event[3], event[4], event[5][0]) for event in captures} == {
        (setup, center, 234., 4, gate) for setup in ('lightfield', 'winspec_ingaas')
        for center in (730., 1150.) for gate in (1., 2.)}
    first_gate = next(i for i, event in enumerate(events) if event[0] == 'gates')
    assert {(e[1], e[2]) for e in events[:first_gate] if e[0] == 'prepare'} == {
        ('lightfield', 730.), ('lightfield', 1150.), ('winspec_ingaas', 730.), ('winspec_ingaas', 1150.)}


def test_move_setup_row_preserves_pairing_editor_and_saved_order(widget):
    widget._on_apply()
    before = [dict(task['ctx']) for task in widget._acquisition_schedule]
    table = widget._loop_table
    table.selectRow(0)
    widget._loop_down_btn.click()
    assert table.currentRow() == 1
    assert table.cellWidget(1, 1).currentData() == 'Measurement setup'
    assert [task['ctx'] for task in widget._tree._last_plan['acquisition_schedule']] == before
    open_picker(widget, lambda dialog: choose(dialog.rows[1].combo, 'lightfield'), row=1)
    assert table.item(1, 2).text() == 'PIXIS, PIXIS'
    restored = panel.PresetsPanel()
    try:
        restored.restore_session_state(widget.capture_session_state())
        assert restored._loop_table.cellWidget(1, 1).currentData() == 'Measurement setup'
        assert restored._loop_table.item(1, 2).text() == 'PIXIS, PIXIS'
    finally:
        restored.close()
    widget._on_discard()
    assert table.cellWidget(0, 1).currentData() == 'Measurement setup'
    assert table.item(0, 2).text() == 'PIXIS, WinSpec'


def test_row_movement_keeps_unfinished_cells_and_boundary_states(widget):
    table = widget._loop_table
    table.item(1, 2).setText('730, unfinished')
    table.item(1, 3).setText('')
    table.selectRow(1)
    widget._loop_top_btn.click()
    assert table.currentRow() == 0
    assert table.item(0, 2).text() == '730, unfinished'
    assert table.item(0, 3).text() == ''
    assert not widget._loop_up_btn.isEnabled()
    widget._loop_bottom_btn.click()
    assert table.currentRow() == table.rowCount() - 1
    assert not widget._loop_down_btn.isEnabled()
    assert table.item(table.currentRow(), 2).text() == '730, unfinished'


def test_row_movement_keeps_implicit_synchronize_nesting(widget):
    state = widget.capture_session_state()
    state.update(draft_loop=[
        dict(Enable=True, Parameter='Center Wavelength (nm)', Values='730, 740', Group=1),
        dict(Enable=True, Parameter='Exposure Time (ms)', Values='100, 200', Group=2),
    ], loop_mode='Synchronize', execution_order=None, nested_schedule_enabled=False)
    widget.restore_session_state(state)
    widget._on_apply()
    before = [dict(task['ctx']) for task in widget._acquisition_schedule]
    widget._loop_table.selectRow(1)
    widget._loop_up_btn.click()
    assert widget._loop_table.cellWidget(0, 1).currentData() == 'Exposure Time (ms)'
    assert [task['ctx'] for task in widget._tree._last_plan['acquisition_schedule']] == before


def test_movement_cannot_change_active_run(widget):
    widget._loop_table.selectRow(0)
    before = panel._read_loop_table_raw(widget._loop_table)
    widget._run_thread = SimpleNamespace(isRunning=lambda: True)
    try:
        widget._refresh_readiness()
        assert not widget._loop_down_btn.isEnabled()
        widget._move_loop_row(1)
        assert panel._read_loop_table_raw(widget._loop_table) == before
    finally:
        widget._run_thread = None


def test_live_warning_pairs_correct_detector_and_clears_after_edit(widget):
    table = widget._loop_table
    table.item(1, 2).setText('1100, 730')
    message = widget._detector_warning_lbl.text()
    assert 'PIXIS' in message and '1100' in message
    assert 'InGaAs' in message and '730' in message
    assert widget._apply_btn.isEnabled()  # A reminder, not an invalid draft.
    assert 'Continue' not in message  # Only the Run dialog asks for confirmation.
    table.item(1, 2).setText('730, 1100')
    assert widget._detector_warning_lbl.isHidden()


def test_separate_groups_warn_for_cross_combinations_in_preview(widget):
    widget._loop_table.item(1, 3).setText('2')
    widget._reset_execution_order()
    contexts = [task['ctx'] for task in widget._tree._last_plan['acquisition_schedule']]
    assert len(contexts) == 4
    message = widget._detector_warning_lbl.text()
    assert 'PIXIS' in message and '1100' in message
    assert 'InGaAs' in message and '720' in message


def test_invalid_draft_does_not_show_stale_detector_results(widget):
    widget._loop_table.item(1, 2).setText('1100, 730')
    assert '1100' in widget._detector_warning_lbl.text()
    widget._loop_table.item(1, 2).setText('unfinished')
    assert 'pending' in widget._detector_warning_lbl.text()
    assert '1100' not in widget._detector_warning_lbl.text()


def test_when_filter_omits_unmeasured_detector_centers(widget):
    widget._loop_table.item(0, 2).setText('PIXIS, PIXIS')
    widget._loop_table.item(1, 2).setText('730, 1100')
    widget._batch_table.item(0, panel.BATCH_SCHEMA.index('When')).setText('Center_Wavelength < 1000')
    assert not widget._draft_validation_issues()
    assert widget._detector_warning_lbl.isHidden()


def test_run_reminder_can_be_explicitly_accepted(widget):
    from ui.detector_wavelength_advice import confirm_sequence_wavelength
    def proceed():
        box = QApplication.activeModalWidget()
        next(button for button in box.buttons() if box.buttonRole(button) == QMessageBox.ButtonRole.AcceptRole).click()
    QTimer.singleShot(0, proceed)
    assert confirm_sequence_wavelength(widget,
        [{'Measurement setup': 'lightfield', 'Center Wavelength (nm)': 1100}], {}, 730)


def test_moving_unfinished_values_keeps_implicit_synchronize_nesting(widget):
    state = widget.capture_session_state()
    state.update(draft_loop=[
        dict(Enable=True, Parameter='Center Wavelength (nm)', Values='730, 740', Group=1),
        dict(Enable=True, Parameter='Exposure Time (ms)', Values='100, 200', Group=2),
    ], loop_mode='Synchronize', execution_order=None, nested_schedule_enabled=False)
    widget.restore_session_state(state)
    widget._on_apply()
    before = [dict(task['ctx']) for task in widget._acquisition_schedule]
    widget._loop_table.item(1, 2).setText('unfinished')
    widget._loop_table.selectRow(1)
    widget._loop_up_btn.click()
    widget._loop_table.item(0, 2).setText('100, 200')
    assert not widget._draft_validation_issues()
    assert [task['ctx'] for task in widget._tree._last_plan['acquisition_schedule']] == before


def test_moving_rows_preserves_legacy_batch_repetitions_after_apply_and_restore(widget):
    state = widget.capture_session_state()
    state.update(draft_loop=[
        dict(Enable=True, Parameter='Center Wavelength (nm)', Values='730, 740', Group=1),
        dict(Enable=True, Parameter='Exposure Time (ms)', Values='100, 200', Group=2),
    ], loop_mode='Synchronize', execution_order=None, nested_schedule_enabled=False,
       acquisition_grouping='batch_first')
    widget.restore_session_state(state)
    widget._batch_table.item(0, panel.BATCH_SCHEMA.index('repeat')).setText('2')
    widget._on_apply()

    def captures(schedule):
        return [(task['ctx']['Center Wavelength (nm)'], task['ctx']['Exposure Time (ms)'])
                for task in schedule for _ in range(1 if task.get('nested') else int(task['row']['repeat']))]

    expected = [(730., 100.), (730., 100.), (730., 200.), (730., 200.),
                (740., 100.), (740., 100.), (740., 200.), (740., 200.)]
    assert captures(widget._acquisition_schedule) == expected
    widget._loop_table.selectRow(1)
    widget._loop_up_btn.click()
    assert captures(widget._tree._last_plan['acquisition_schedule']) == expected
    widget._on_apply()
    assert captures(widget._acquisition_schedule) == expected
    restored = panel.PresetsPanel()
    try:
        restored.restore_session_state(widget.capture_session_state())
        assert captures(restored._acquisition_schedule) == expected
        restored._loop_table.item(1, 2).setText('750, 740')
        assert captures(restored._tree._last_plan['acquisition_schedule'])[:4] == [
            (750., 100.), (750., 100.), (750., 200.), (750., 200.)]
    finally:
        restored.close()


class DefaultDetector(QObject):
    connected = Signal()
    disconnected = Signal()
    settings_applied = Signal()
    identity = {'backend': 'lightfield'}
    is_connected = False


def test_moving_newly_enabled_rows_retains_their_current_nesting(widget):
    state = widget.capture_session_state()
    state.update(draft_loop=[
        dict(Enable=True, Parameter='Center Wavelength (nm)', Values='730, 740', Group=1),
        dict(Enable=False, Parameter='Rotation1 Angle (deg)', Values='0, 10', Group=2),
        dict(Enable=False, Parameter='Rotation2 Angle (deg)', Values='0, 20', Group=3),
    ], loop_mode='Synchronize', execution_order=None, nested_schedule_enabled=False)
    widget.restore_session_state(state)
    table = widget._loop_table
    table.selectRow(0)
    widget._loop_down_btn.click()
    table.cellWidget(0, 0).findChild(QCheckBox).setChecked(True)
    table.cellWidget(2, 0).findChild(QCheckBox).setChecked(True)
    before = [dict(task['ctx']) for task in widget._tree._last_plan['acquisition_schedule']]
    table.selectRow(2)
    widget._loop_top_btn.click()
    assert [task['ctx'] for task in widget._tree._last_plan['acquisition_schedule']] == before


@pytest.mark.parametrize('refresh', ['settings_applied', 'return_to_page'])
def test_default_center_reminder_refreshes_after_external_change(app, monkeypatch, refresh):
    detector = DefaultDetector()
    monkeypatch.setattr(panel.cfg.lf6, 'center_nm', 730.)
    result = panel.PresetsPanel(lf6_ctrl=detector)
    try:
        state = result.capture_session_state()
        state.update(draft_loop=[dict(Enable=False, Parameter='Center Wavelength (nm)',
                                      Values='730', Group=1)],
                     execution_order=None, nested_schedule_enabled=False)
        result.restore_session_state(state)
        result.show()
        app.processEvents()
        assert result._detector_warning_lbl.isHidden(), result._detector_warning_lbl.text()
        if refresh == 'return_to_page':
            result.hide()
        monkeypatch.setattr(panel.cfg.lf6, 'center_nm', 1100.)
        if refresh == 'settings_applied':
            detector.settings_applied.emit()
        else:
            result.show()
        app.processEvents()
        assert '1100' in result._detector_warning_lbl.text()
    finally:
        result.close()


def test_run_shows_one_combined_reminder_and_cancel_stops_before_metadata(widget, monkeypatch, tmp_path):
    widget._loop_table.item(1, 2).setText('1100, 730')
    widget._on_apply()
    monkeypatch.setattr(widget, '_validate_before_run', lambda: ({}, None))
    monkeypatch.setattr(widget, '_current_output_dir', lambda _meta: tmp_path)
    monkeypatch.setattr(panel, 'ExperimentMetadataService', lambda *_a, **_k: pytest.fail('Metadata started after Cancel'))
    errors = []
    def cancel_reminder():
        box = QApplication.activeModalWidget()
        try:
            assert isinstance(box, QMessageBox)
            assert all(text in box.text() for text in ('PIXIS', '1100', 'InGaAs', '730'))
            assert box.defaultButton() is box.button(QMessageBox.StandardButton.Cancel)
        except BaseException as exc:
            errors.append(exc)
        finally:
            if box is not None:
                box.reject()
    QTimer.singleShot(0, cancel_reminder)
    widget._on_run()
    if errors:
        raise errors[0]
    assert widget._run_thread is None
