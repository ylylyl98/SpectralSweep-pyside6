import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import Qt, QObject, QPointF
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QCheckBox, QComboBox, QTableWidget, QTableWidgetItem
from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.presets_panel import PresetsPanel, BATCH_SCHEMA, _read_loop_table
from ui.quick_table_editor import QuickTableEditor, TableEditorModel, install_table_editors
from utils.config import cfg


@pytest.fixture
def panel(monkeypatch):
    app = quick_application()
    monkeypatch.setattr(cfg, "save", lambda: None)
    widget = PresetsPanel()
    widget._batch_table.setProperty("quickSingleLineRows", True)
    views = install_table_editors(widget)
    widget.resize(1300, 900)
    widget.show()
    QTest.qWait(60)
    yield widget
    for view in views:
        view.shutdown()
    widget.dispose_reloaded_page()
    widget.close()
    widget.deleteLater()
    drain_qt_deletions()


def test_loop_controls_and_values_update_the_original_draft(panel):
    view = panel._loop_table._quick_editor
    model = view.model
    columns = model._columns
    assert model.editCell(0, columns.index(2), "750,810")
    assert panel._loop_table.item(0, 2).text() == "750,810"
    assert model.editCell(0, columns.index(0), True)
    assert bool(_read_loop_table(panel._loop_table).iloc[0]["Enable"])
    assert not view.qml_warnings
    assert view.quick_widget.rootObject().property("liveCells") > 0


def test_batch_delegate_validation_selection_and_undo_state(panel):
    view = panel._batch_table._quick_editor
    model = view.model
    repeat = model._columns.index(BATCH_SCHEMA.index("repeat"))
    initial = panel._batch_table.item(0, BATCH_SCHEMA.index("repeat")).text()
    assert not model.editCell(0, repeat, "1.5")
    assert model.error and panel._batch_table.item(0, BATCH_SCHEMA.index("repeat")).text() == initial
    assert model.editCell(0, repeat, "3")
    assert not model.error and panel._batch_table.item(0, BATCH_SCHEMA.index("repeat")).text() == "3"
    model.selectRow(0, False)
    panel._batch_duplicate_btn.click()
    assert model.rowCount() == panel._batch_table.rowCount()
    assert model.rowCount() >= 2
    assert panel._batch_table.item(1, BATCH_SCHEMA.index("repeat")).text() == "3"
    panel._batch_table.setEnabled(False)
    assert not model.editCell(0, repeat, "5")
    assert not view.qml_warnings


def test_optional_bias_and_when_remain_true_draft_values(panel):
    view = panel._batch_table._quick_editor
    model = view.model
    bias = model._columns.index(BATCH_SCHEMA.index("Vbias_start"))
    when = model._columns.index(BATCH_SCHEMA.index("When"))
    assert model.editCell(0, bias, "")
    assert panel._batch_table.item(0, BATCH_SCHEMA.index("Vbias_start")).text() == ""
    assert model.editCell(0, when, "unknown_name > 10")
    panel._refresh_draft_state()
    assert not panel._apply_btn.isEnabled()
    assert "unknown_name" in panel._batch_table.item(0, BATCH_SCHEMA.index("When")).text()
    assert panel._run_thread is None


def test_qml_keyboard_edit_commits_through_original_delegate(panel):
    view = panel._loop_table._quick_editor
    column = view.model._columns.index(2)
    root = view.quick_widget.rootObject()
    def visual_item(item, name):
        if item.objectName() == name:
            return item
        for child in item.childItems():
            result = visual_item(child, name)
            if result is not None:
                return result
    editor = visual_item(root, f"tableCell_0_{column}")
    assert editor is not None
    position = editor.mapToScene(QPointF(editor.width() / 2, editor.height() / 2)).toPoint()
    QTest.mouseClick(view.quick_widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, position)
    QTest.keyClick(view.quick_widget, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(view.quick_widget, "750,810")
    QTest.keyClick(view.quick_widget, Qt.Key.Key_Return)
    QTest.qWait(20)
    assert panel._loop_table.item(0, 2).text() == "750,810"
    assert not view.qml_warnings


def test_disclosed_table_follows_source_and_shutdown_does_not_commit_focus(panel):
    view = panel._sweep_calc._multi_preview._quick_editor
    assert view.isHidden()
    panel._sweep_calc._multi_preview.show()
    assert not view.isHidden()
    panel._sweep_calc._multi_preview.hide()
    assert view.isHidden()
    view.model.shutdown()
    assert not view.model.editCell(0, 0, "edited during close")


def test_focused_draft_commits_before_reload_capture(panel):
    from ui.dual_gate_reload_state import capture_dual_gate_state
    view = panel._loop_table._quick_editor
    model = view.model
    column = model._columns.index(2)
    token = model.data(model.index(0, column), model.CellRole)["token"]
    model.stageEdit(token, "750,810")
    assert panel._loop_table.item(0, 2).text() != "750,810"
    state = capture_dual_gate_state(panel)
    assert panel._loop_table.item(0, 2).text() == "750,810"
    assert state["session"]["draft_loop"][0]["Values"] == "750,810"


def test_staged_invalid_draft_blocks_commit_then_can_be_corrected(panel):
    from ui.quick_editing import commit_quick_edits
    model = panel._batch_table._quick_editor.model
    column = model._columns.index(BATCH_SCHEMA.index("repeat"))
    token = model.data(model.index(0, column), model.CellRole)["token"]
    before = panel._batch_table.item(0, BATCH_SCHEMA.index("repeat")).text()
    model.stageEdit(token, "1.5")
    assert not commit_quick_edits(panel)
    assert panel._batch_table.item(0, BATCH_SCHEMA.index("repeat")).text() == before
    assert model.data(model.index(0, column), model.CellRole)["value"] == "1.5"
    model.stageEdit(token, "3")
    assert commit_quick_edits(panel)
    assert panel._batch_table.item(0, BATCH_SCHEMA.index("repeat")).text() == "3"


def test_structural_change_discards_tokens_without_writing_a_different_row(panel):
    model = panel._batch_table._quick_editor.model
    column = model._columns.index(BATCH_SCHEMA.index("repeat"))
    token = model.data(model.index(0, column), model.CellRole)["token"]
    model.stageEdit(token, "91")
    panel._batch_table.insertRow(0)
    assert model.commitCell(token)
    assert not model._drafts
    assert panel._batch_table.item(1, BATCH_SCHEMA.index("repeat")).text() != "91"


def test_batch_table_keeps_one_line_without_horizontal_scrolling_or_field_pages(panel):
    view = panel._batch_table._quick_editor
    view.resize(450, 260)
    QTest.qWait(40)
    root = view.quick_widget.rootObject()
    grid = root.findChild(QObject, "QuickEditableTable")
    def visual(item, name):
        if item.objectName() == name:
            return item
        for child in item.childItems():
            found = visual(child, name)
            if found is not None:
                return found
    assert root.metaObject().indexOfProperty("columnPages") < 0
    assert root.property("sharedHeaders")
    # Production headers contain aliases/newlines instead of schema keys.
    assert view.model.columnWidths[BATCH_SCHEMA.index("condition_label")] == 220
    assert view.model.columnWidths[BATCH_SCHEMA.index("repeat")] == 46
    assert view.model.columnWidths[BATCH_SCHEMA.index("MeasurePower")] == 54
    assert view.model.columnKeys == BATCH_SCHEMA
    assert view.model.displayColumnTitles[BATCH_SCHEMA.index("Vbg_start")] == "Vbg\nstart (V)"
    assert view.model.displayColumnTitles[BATCH_SCHEMA.index("Vbias_stop")] == "Vbias\nstop (V)"
    fields = []
    for column in range(view.model.columnCount()):
        field = visual(root, f"tableCell_0_{column}")
        fields.append(field)
        assert field is not None, column
        left = field.mapToScene(QPointF(0, 0)).x()
        assert left >= 0 and left + field.width() <= root.width() + 1
        assert field.width() > 0
    assert len({field.mapToScene(QPointF()).y() for field in fields}) == 1
    assert fields[BATCH_SCHEMA.index("condition_label")].width() > fields[BATCH_SCHEMA.index("repeat")].width() * 3
    assert visual(root, "tableRecord_0").height() == 36
    assert grid.property("contentWidth") <= grid.property("width") + 1
    assert not view.qml_warnings


@pytest.fixture
def selection_table():
    quick_application()
    source = QTableWidget(8, 3)
    source.setHorizontalHeaderLabels(["Run", "condition_label", "This run"])
    for row in range(source.rowCount()):
        source.setCellWidget(row, 0, QCheckBox())
        source.setItem(row, 1, QTableWidgetItem(f"Condition {row + 1}"))
        choice = QComboBox()
        choice.addItems(["All values", "700"])
        source.setCellWidget(row, 2, choice)
    view = QuickTableEditor(source)
    view.resize(700, 400)
    view.show()
    QTest.qWait(30)
    yield source, view
    view.shutdown()
    view.close()
    view.deleteLater()
    source.deleteLater()
    drain_qt_deletions()


def _selected_rows(source):
    return sorted(index.row() for index in source.selectionModel().selectedRows())


def _click_table_item(view, name, modifiers=Qt.KeyboardModifier.NoModifier, *, gutter=False):
    from tests.test_quick_parameter_forms import find_item
    item = find_item(view.quick_widget.rootObject(), name)
    assert item is not None, name
    position = item.mapToScene(QPointF(2 if gutter else item.width() / 2, item.height() / 2)).toPoint()
    QTest.mouseClick(view.quick_widget, Qt.MouseButton.LeftButton, modifiers, position)
    QTest.qWait(5)
    return item


def test_separate_keystrokes_keep_focused_draft_text_and_cursor(selection_table):
    source, view = selection_table
    editor = _click_table_item(view, "tableCell_1_1")
    original = source.item(1, 1).text()
    QTest.keyClick(view.quick_widget, Qt.Key.Key_End)
    expected = original
    resets = QSignalSpy(view.model.modelReset)
    cells = QSignalSpy(view.model.cellChanged)
    for char in "xyz":
        QTest.keyClicks(view.quick_widget, char)
        # Real typing lets the staged-cell timer and QML binding run between
        # keys; a single keyClicks("xyz") call misses this transition.
        QTest.qWait(20)
        expected += char
        assert editor.property("text") == expected
        assert editor.property("cursorPosition") == len(expected)
        assert editor.hasActiveFocus()
        assert source.item(1, 1).text() == original
    assert resets.count() == 0
    assert [cells.at(i) for i in range(cells.count())] == [[1, 1]] * 3
    QTest.keyClick(view.quick_widget, Qt.Key.Key_Left)
    QTest.keyClick(view.quick_widget, Qt.Key.Key_Left)
    cursor = len(expected) - 2
    QTest.keyClicks(view.quick_widget, "m")
    QTest.qWait(20)
    assert editor.property("text") == expected[:cursor] + "m" + expected[cursor:]
    assert editor.property("cursorPosition") == cursor + 1
    QTest.keyClick(view.quick_widget, Qt.Key.Key_Backspace)
    QTest.qWait(20)
    assert editor.property("text") == expected
    assert editor.property("cursorPosition") == cursor
    view._poll.timeout.emit()
    QTest.qWait(10)
    assert editor.property("text") == expected
    assert editor.property("cursorPosition") == cursor
    QTest.keyClick(view.quick_widget, Qt.Key.Key_Return)
    assert source.item(1, 1).text() == expected
    assert _selected_rows(source) == [1]
    assert source.currentRow() == 1
    assert not view.qml_warnings


def test_editable_choice_keeps_separate_keystrokes_until_focus_commit(selection_table):
    source, view = selection_table
    control = source.cellWidget(1, 2)
    control.setEditable(True)
    view.model.refresh()
    QTest.qWait(10)
    choice = _click_table_item(view, "tableCell_1_2")
    text_input = choice.property("contentItem")
    QTest.keyClick(view.quick_widget, Qt.Key.Key_End)
    original = control.currentText()
    expected = original
    for char in "xyz":
        QTest.keyClicks(view.quick_widget, char)
        QTest.qWait(20)
        expected += char
        assert choice.property("editText") == expected
        assert text_input.property("text") == expected
        assert text_input.property("cursorPosition") == len(expected)
        assert control.currentText() == original
    view._poll.timeout.emit()
    assert text_input.property("text") == expected
    _click_table_item(view, "tableCell_1_1")
    assert control.currentText() == expected
    assert _selected_rows(source) == [1]
    assert not view.qml_warnings


def test_invalid_typed_number_survives_refresh_and_escape_restores_source(panel):
    view = panel._batch_table._quick_editor
    column = view.model._columns.index(BATCH_SCHEMA.index("repeat"))
    editor = _click_table_item(view, f"tableCell_0_{column}")
    original = panel._batch_table.item(0, BATCH_SCHEMA.index("repeat")).text()
    QTest.keyClick(view.quick_widget, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    for char in "1.5":
        QTest.keyClicks(view.quick_widget, char)
        QTest.qWait(20)
    assert editor.property("text") == "1.5"
    QTest.keyClick(view.quick_widget, Qt.Key.Key_Return)
    assert view.model.error
    view._poll.timeout.emit()
    QTest.qWait(10)
    assert editor.property("text") == "1.5"
    assert panel._batch_table.item(0, BATCH_SCHEMA.index("repeat")).text() == original
    condition = view.model._columns.index(BATCH_SCHEMA.index("condition_label"))
    _click_table_item(view, f"tableCell_0_{condition}")
    _click_table_item(view, f"tableCell_0_{column}")
    assert editor.hasActiveFocus()
    assert editor.property("text") == "1.5"
    QTest.keyClick(view.quick_widget, Qt.Key.Key_Escape)
    QTest.qWait(10)
    assert editor.property("text") == original
    assert not view.model._drafts
    assert not view.model.error
    assert not view.qml_warnings


def test_mouse_ctrl_shift_selection_works_over_text_and_checkbox_editors(selection_table):
    source, view = selection_table
    editor = _click_table_item(view, "tableCell_1_1")
    assert editor.hasActiveFocus()
    assert _selected_rows(source) == [1]
    _click_table_item(view, "tableCell_3_1", Qt.KeyboardModifier.ControlModifier)
    assert _selected_rows(source) == [1, 3]
    QTest.keyClick(view.quick_widget, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(view.quick_widget, "Edited condition")
    QTest.keyClick(view.quick_widget, Qt.Key.Key_Return)
    assert source.item(3, 1).text() == "Edited condition"
    assert _selected_rows(source) == [1, 3]
    _click_table_item(view, "tableCell_5_0", Qt.KeyboardModifier.ControlModifier)
    assert _selected_rows(source) == [1, 3, 5]
    assert source.cellWidget(5, 0).isChecked()
    _click_table_item(view, "tableCell_7_1", Qt.KeyboardModifier.ShiftModifier)
    assert _selected_rows(source) == [5, 6, 7]
    _click_table_item(view, "tableCell_2_1", Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert _selected_rows(source) == [2, 3, 4, 5, 6, 7]
    _click_table_item(view, "tableCell_4_1", Qt.KeyboardModifier.ControlModifier)
    assert _selected_rows(source) == [2, 3, 5, 6, 7]
    assert not view.qml_warnings


def test_row_keyboard_shift_range_and_external_current_row_anchor(selection_table):
    source, view = selection_table
    _click_table_item(view, "tableRecord_1", gutter=True)
    QTest.keyClick(view.quick_widget, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    assert _selected_rows(source) == [1, 2]
    QTest.keyClick(view.quick_widget, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    assert _selected_rows(source) == [1, 2, 3]
    QTest.keyClick(view.quick_widget, Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier)
    assert _selected_rows(source) == [1, 2]
    # Reordering in the backend restores a different current row.
    source.setCurrentCell(4, 0)
    _click_table_item(view, "tableCell_6_1", Qt.KeyboardModifier.ShiftModifier)
    assert _selected_rows(source) == [4, 5, 6]
    assert not view.qml_warnings


def test_batch_quick_multiselection_buttons_keep_highlight_current_and_shift_anchor(panel):
    from tests.test_quick_parameter_forms import find_item
    source = panel._batch_table
    view = source._quick_editor
    defaults = panel._batch_row_values(0)
    if source.rowCount() < 6:
        panel._insert_batch_rows(source.rowCount(), [defaults.copy() for _ in range(6 - source.rowCount())])
    QTest.qWait(30)
    column = view.model._columns.index(BATCH_SCHEMA.index("condition_label"))
    _click_table_item(view, f"tableCell_1_{column}")
    _click_table_item(view, f"tableCell_3_{column}", Qt.KeyboardModifier.ControlModifier)
    assert _selected_rows(source) == [1, 3]
    assert source.currentRow() == 3
    root = view.quick_widget.rootObject()

    def check_selected(expected, current):
        QTest.qWait(10)
        assert _selected_rows(source) == expected
        assert source.currentRow() == current
        for row in range(5):
            record = find_item(root, f"tableRecord_{row}")
            if record is not None:
                assert record.property("selected") == (row in expected)

    panel._batch_down_btn.click()
    check_selected([2, 4], 4)
    panel._batch_up_btn.click()
    check_selected([1, 3], 3)
    panel._batch_up_btn.click()
    check_selected([0, 2], 2)
    _click_table_item(view, f"tableCell_4_{column}", Qt.KeyboardModifier.ShiftModifier)
    check_selected([2, 3, 4], 4)
    assert not view.qml_warnings


@pytest.mark.parametrize("row_count", [100, 1000])
def test_row_selection_never_rereads_or_rebuilds_visible_cell_editors(monkeypatch, row_count):
    quick_application()
    source = QTableWidget(row_count, 13)
    model = TableEditorModel(source)
    for row in range(10):
        model.watchRow(row)
        for column in range(13):
            model.cellAt(row, column)
    model._timer.stop()
    model._dirty_cells.clear()
    cached = dict(model._cells)
    reads = []
    original = model.data
    monkeypatch.setattr(model, "data", lambda *args: (reads.append(args), original(*args))[1])
    cells = QSignalSpy(model.cellChanged)
    changes = QSignalSpy(model.dataChanged)
    rows = QSignalSpy(model.rowSelectionChanged)
    model.selectRow(2)
    model.selectRow(5, True)
    source.selectAll()
    model._flush_dirty()
    assert not reads
    assert cells.count() == changes.count() == 0
    assert rows.count() == 10  # Two individual rows, then the other eight.
    assert all(model._cells[key] is cell and cell["selected"] for key, cell in cached.items())
    model.shutdown()
    source.deleteLater()
    drain_qt_deletions()
