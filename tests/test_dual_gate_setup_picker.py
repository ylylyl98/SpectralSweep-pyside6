"""Setup selection through real Qt controls; no hardware is connected."""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QPushButton

from ui import presets_panel as panel


@pytest.fixture(scope='module', autouse=True)
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture
def widget():
    result = panel.PresetsPanel()
    result._load_dual_setup_recipe()
    yield result
    result.close()


def choose(combo, backend):
    combo.setCurrentIndex(combo.findData(backend))


def open_picker(widget, edit, *, accept=True, row=0):
    """Drive the modal editor without substituting its editing/commit logic."""
    button = widget._loop_table.cellWidget(row, 2)
    assert isinstance(button, QPushButton), 'Setup Values must offer a selection control'
    errors = []
    def interact():
        dialog = QApplication.activeModalWidget()
        try:
            assert dialog is not None
            edit(dialog)
            if accept:
                dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).click()
                assert dialog.result() == dialog.DialogCode.Accepted
            else:
                dialog.reject()
        except BaseException as exc:
            errors.append(exc)
        finally:
            if dialog is not None and dialog.isVisible():
                dialog.reject()
    QTimer.singleShot(0, interact)
    button.click()
    if errors:
        raise errors[0]


def test_setup_cell_offers_picker_while_numeric_cells_remain_text(widget):
    table = widget._loop_table
    button = table.cellWidget(0, 2)
    assert isinstance(button, QPushButton)
    assert 'PIXIS' in button.text() and 'WinSpec' in button.text()
    assert not table.item(0, 2).flags() & Qt.ItemFlag.ItemIsEditable
    assert table.cellWidget(1, 2) is None
    assert table.item(1, 2).flags() & Qt.ItemFlag.ItemIsEditable


def test_setup_button_fits_its_row_with_application_stylesheet(app):
    from ui.main_window import _STYLESHEET
    previous_style = app.styleSheet()
    result = None
    try:
        app.setStyleSheet(_STYLESHEET)
        result = panel.PresetsPanel()
        result._load_dual_setup_recipe()
        result.show()
        app.processEvents()
        table = result._loop_table
        assert table.cellWidget(0, 2).height() < table.rowHeight(0)
    finally:
        if result is not None:
            result.close()
        app.setStyleSheet(previous_style)


def test_adding_reordering_removing_and_repeating_setup_updates_pairing(widget):
    def edit(dialog):
        dialog.add_button.click()
        choose(dialog.rows[-1].combo, 'lightfield')
        dialog.rows[-1].up_button.click()
        assert dialog.selected_backends() == ['lightfield', 'lightfield', 'winspec_ingaas']
        dialog.rows[0].down_button.click()
        dialog.rows[-1].remove_button.click()
        choose(dialog.rows[0].combo, 'winspec_ingaas')
    open_picker(widget, edit)
    assert panel._read_loop_table(widget._loop_table).iloc[0].Values == 'WinSpec, PIXIS'
    assert widget._tables_dirty
    sequence, _, _ = panel._build_plan(panel._read_loop_table(widget._loop_table), panel._DEFAULT_BATCH, 'Customized')
    assert [(c['Measurement setup'], c['Center Wavelength (nm)']) for c in sequence] == [
        ('winspec_ingaas', 720.), ('lightfield', 1100.)]


def test_cancel_does_not_commit_any_changes(widget):
    widget._on_apply()
    before = widget.capture_session_state()['draft_loop']
    open_picker(widget, lambda dialog: choose(dialog.rows[0].combo, 'winspec_ingaas'), accept=False)
    assert widget.capture_session_state()['draft_loop'] == before
    assert not widget._tables_dirty


def test_accepted_choices_survive_restore_and_discard(widget):
    open_picker(widget, lambda dialog: choose(dialog.rows[1].combo, 'lightfield'))
    widget._on_apply()
    state = widget.capture_session_state()
    restored = panel.PresetsPanel()
    try:
        restored.restore_session_state(state)
        assert restored._loop_table.item(0, 2).text() == 'PIXIS, PIXIS'
        assert 'PIXIS' in restored._loop_table.cellWidget(0, 2).text()
        open_picker(restored, lambda dialog: choose(dialog.rows[0].combo, 'winspec_ingaas'))
        restored._on_discard()
        assert restored._loop_table.item(0, 2).text() == 'PIXIS, PIXIS'
        assert 'WinSpec' not in restored._loop_table.cellWidget(0, 2).text()
    finally:
        restored.close()


def test_old_saved_aliases_and_invalid_values_are_not_silently_replaced(widget):
    widget._loop_table.item(0, 2).setText('lightfield;winspec_ingaas')
    open_picker(widget, lambda dialog: (
        pytest.fail('Aliases did not restore') if dialog.selected_backends() != ['lightfield', 'winspec_ingaas'] else None
    ), accept=False)
    assert widget._loop_table.item(0, 2).text() == 'lightfield;winspec_ingaas'
    widget._loop_table.item(0, 2).setText('PIXIS, unknown-camera')
    def repair(dialog):
        assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
        assert not dialog.rows[1].combo.isEditable()
        dialog.accept()
        assert dialog.result() != dialog.DialogCode.Accepted
        choose(dialog.rows[1].combo, 'winspec_ingaas')
    open_picker(widget, repair)
    assert widget._loop_table.item(0, 2).text() == 'PIXIS, WinSpec'


def test_manual_parameter_change_uses_correct_values_editor(widget):
    table = widget._loop_table
    combo = table.cellWidget(0, 1)
    combo.setCurrentIndex(combo.findData('Exposure Time (ms)'))
    assert table.cellWidget(0, 2) is None
    assert table.item(0, 2).flags() & Qt.ItemFlag.ItemIsEditable
    table.item(0, 2).setText('100, 500')
    combo.setCurrentIndex(combo.findData('Measurement setup'))
    def edit(dialog):
        choose(dialog.rows[0].combo, 'lightfield')
        choose(dialog.rows[1].combo, 'winspec_ingaas')
    open_picker(widget, edit)
    assert table.item(0, 2).text() == 'PIXIS, WinSpec'


def test_incomplete_new_position_cannot_be_accepted_and_count_mismatch_is_reported(widget):
    def edit(dialog):
        dialog.add_button.click()
        assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
        choose(dialog.rows[-1].combo, 'lightfield')
    open_picker(widget, edit)
    assert widget._loop_table.item(0, 2).text() == 'PIXIS, WinSpec, PIXIS'
    assert any('value-count mismatch' in issue for issue in widget._draft_validation_issues())
    assert not widget._apply_btn.isEnabled()


def test_keyboard_tab_order_follows_reordered_setup_positions(widget):
    def edit(dialog):
        dialog.activateWindow()
        QApplication.processEvents()
        dialog.rows[1].up_button.click()
        dialog.rows[0].remove_button.setFocus()
        QTest.keyClick(dialog.rows[0].remove_button, Qt.Key.Key_Tab)
        assert QApplication.focusWidget() is dialog.rows[1].combo
    open_picker(widget, edit, accept=False)
