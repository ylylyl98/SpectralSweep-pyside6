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


def test_nested_table_updates_reset_once_and_invalidate_staged_tokens(selection_table):
    source, view = selection_table
    model = view.model
    token = model.cellAt(1, 1)["token"]
    original = source.item(1, 1).text()
    model.stageEdit(token, "stale value")
    before = QSignalSpy(model.modelAboutToBeReset)
    after = QSignalSpy(model.modelReset)
    inserted = QSignalSpy(source.model().rowsInserted)
    with model.batch_updates():
        source.insertRow(2)
        with model.batch_updates():
            source.insertRow(3)
        assert before.count() == 1 and after.count() == 0
        source.setItem(2, 1, QTableWidgetItem("new row"))
    assert before.count() == after.count() == 1
    assert inserted.count() == 2
    assert model.rowCount() == 10
    assert model.commitCell(token)
    assert not model._drafts
    assert source.item(1, 1).text() == original
    assert model.cellAt(2, 1)["value"] == "new row"
    assert not view.qml_warnings


def test_table_updates_preserve_incremental_edits_without_structure_change(selection_table):
    source, view = selection_table
    model = view.model
    resets = QSignalSpy(model.modelReset)
    with model.batch_updates():
        pass
    with model.batch_updates():
        source.item(1, 1).setText("changed")
    model.refresh()
    assert resets.count() == 0
    assert model.cellAt(1, 1)["value"] == "changed"
    assert not model._resetting


def test_table_updates_finish_reset_after_exception_and_allow_next_edit(selection_table):
    source, view = selection_table
    before = QSignalSpy(view.model.modelAboutToBeReset)
    after = QSignalSpy(view.model.modelReset)
    with pytest.raises(RuntimeError, match="interrupted"):
        with view.model.batch_updates():
            source.insertRow(2)
            raise RuntimeError("interrupted")
    assert before.count() == after.count() == 1
    assert not view.model._resetting
    assert view.model.rowCount() == source.rowCount() == 9
    source.removeRow(2)
    assert before.count() == after.count() == 2
    assert view.model.rowCount() == 8
    assert not view.qml_warnings


@pytest.mark.parametrize("width", [450, 650, 1100])
def test_batch_table_keeps_readable_headers_without_horizontal_scrolling(panel, width):
    from PySide6.QtGui import QFontDatabase
    for font in ("segoeui.ttf", "seguisb.ttf"):
        QFontDatabase.addApplicationFont("C:/Windows/Fonts/" + font)
    view = panel._batch_table._quick_editor
    view.setFixedWidth(width)
    view.resize(width, 260)
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
    assert root.property("sharedHeaders")
    assert grid.property("contentWidth") <= grid.width() + 1
    assert view.model.columnKeys == [name for index, name in enumerate(BATCH_SCHEMA)
                                     if not panel._batch_table.isColumnHidden(index)]
    for name, expected in (("MeasurePower", "Pwr"), ("When", "When"),
                           ("repeat", "Rep."), ("frames", "Frm.")):
        column = view.model.columnKeys.index(name)
        header = visual(root, f"tableHeader_{column}")
        assert header.property("text") == expected
        assert not header.property("truncated")
    fields = []
    for column in range(view.model.columnCount()):
        field = visual(root, f"tableCell_0_{column}")
        header = visual(root, f"tableHeader_{column}")
        fields.append(field)
        assert field is not None and header is not None
        assert not header.property("truncated")
        assert field.mapToScene(QPointF()).x() == pytest.approx(header.mapToScene(QPointF()).x())
        assert field.mapToScene(QPointF()).x() + field.width() <= root.width() + 1
    assert len({field.mapToScene(QPointF()).y() for field in fields}) == 1
    assert not view.qml_warnings


def test_batch_headers_with_independent_and_follow_bias2_fit(panel, monkeypatch):
    from PySide6.QtGui import QFontDatabase
    from ui.presets_panel import _configure_batch_table_columns
    for font in ("segoeui.ttf", "seguisb.ttf"):
        QFontDatabase.addApplicationFont("C:/Windows/Fonts/" + font)
    monkeypatch.setattr(cfg.smu, "vbias2_resource", "configured")
    panel._batch_table.item(0, BATCH_SCHEMA.index("bias2_mode")).setText("independent")
    panel._insert_batch_rows(1, [{**panel._batch_row_values(0), "bias2_mode": "follow"}])
    _configure_batch_table_columns(panel._batch_table)
    view = panel._batch_table._quick_editor
    view.setFixedWidth(650)
    view.model.refresh()
    QTest.qWait(40)
    root = view.quick_widget.rootObject()
    assert view.model.columnCount() == len(BATCH_SCHEMA)
    def visual(item, name):
        if item.objectName() == name:
            return item
        return next((result for child in item.childItems() if (result := visual(child, name)) is not None), None)
    for column in range(view.model.columnCount()):
        header = visual(root, f"tableHeader_{column}")
        assert header is not None and not header.property("truncated"), (column, header.property("text"))
    mode_col = view.model.columnKeys.index("bias2_mode")
    independent = visual(root, f"tableCell_0_{mode_col}")
    follow = visual(root, f"tableCell_1_{mode_col}")
    assert independent.property("displayText") == "Ind"
    assert follow.property("displayText") == "Fol"
    from PySide6.QtGui import QFontMetricsF
    for choice in (independent, follow):
        editor = choice.property("contentItem")
        assert QFontMetricsF(editor.property("font")).horizontalAdvance(choice.property("displayText")) <= editor.width()
    assert panel._batch_table.item(0, BATCH_SCHEMA.index("bias2_mode")).text() == "independent"
    assert panel._batch_table.item(1, BATCH_SCHEMA.index("bias2_mode")).text() == "follow"
    assert root.findChild(QObject, "QuickEditableTable").property("contentWidth") <= root.width() + 1
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


@pytest.fixture
def scrolled_batch(panel):
    import pandas as pd
    from ui.presets_panel import _populate_batch_table

    source = panel._batch_table
    view = source._quick_editor
    defaults = panel._batch_row_values(0)
    _populate_batch_table(source, pd.DataFrame([
        dict(defaults, condition_label=f"row {row}") for row in range(100)]))
    panel._reset_batch_history()
    QTest.qWait(30)
    grid = view.quick_widget.rootObject().findChild(QObject, "QuickEditableTable")
    view.model.selectRow(65)
    grid.setProperty("contentY", 64 * 38.)
    QTest.qWait(30)
    offset = grid.property("contentY") - grid.property("originY")
    assert offset > 1000
    _assert_batch_current_visible(panel, grid)
    return panel, grid, offset


def _assert_batch_current_visible(panel, grid):
    from tests.test_quick_parameter_forms import find_item

    view = panel._batch_table._quick_editor
    row = panel._batch_table.currentRow()
    record = find_item(view.quick_widget.rootObject(), f"tableRecord_{row}")
    assert record is not None
    y = record.mapToItem(grid, QPointF()).y()
    assert y >= -1 and y + record.height() <= grid.height() + 1
    assert not view.qml_warnings


def test_batch_bulk_edits_and_structural_history_rebuild_quick_only_once(scrolled_batch):
    panel, grid, _ = scrolled_batch
    source = panel._batch_table
    model = source._quick_editor.model
    resets = QSignalSpy(model.modelReset)
    inserted = QSignalSpy(source.model().rowsInserted)
    removed = QSignalSpy(source.model().rowsRemoved)
    rows = [panel._batch_row_values(65) for _ in range(10)]
    panel._insert_batch_rows(66, rows)
    assert inserted.count() == 10 and resets.count() == 1
    QTest.qWait(30)
    _assert_batch_current_visible(panel, grid)
    panel._undo_batch_edit()
    assert removed.count() == 10 and resets.count() == 2
    panel._redo_batch_edit()
    assert inserted.count() == 20 and resets.count() == 3
    model.selectRow(66)
    model.selectRowWithModifiers(75, Qt.KeyboardModifier.ShiftModifier.value)
    panel._del_batch_row()
    assert removed.count() == 20 and resets.count() == 4
    QTest.qWait(30)
    _assert_batch_current_visible(panel, grid)
    # Value-only history restores keep the existing incremental path.
    panel._set_rows_repeat([source.currentRow()], 3)
    panel._undo_batch_edit()
    panel._redo_batch_edit()
    assert resets.count() == 4


def test_delete_disabled_batch_rows_coalesces_noncontiguous_removals(scrolled_batch):
    panel, grid, _ = scrolled_batch
    panel._set_rows_run_state([10, 30, 50, 70, 90], False)
    source = panel._batch_table
    model = source._quick_editor.model
    resets = QSignalSpy(model.modelReset)
    removed = QSignalSpy(source.model().rowsRemoved)
    panel._delete_disabled_batch_rows()
    assert source.rowCount() == 95
    assert removed.count() == 5 and resets.count() == 1
    QTest.qWait(30)
    _assert_batch_current_visible(panel, grid)
    panel._undo_batch_edit()
    assert source.rowCount() == 100
    assert resets.count() == 2


@pytest.mark.parametrize("operation,expected_row,expected_count", [
    ("duplicate", 66, 101), ("add", 66, 101), ("paste", 68, 103),
    ("delete", 65, 99), ("delete_many", 65, 97),
    ("up", 64, 100), ("down", 66, 100),
    ("top", 0, 100), ("bottom", 99, 100),
    ("repeat", 65, 100), ("edit", 65, 100),
])
def test_batch_operations_keep_current_row_in_scrolled_quick_view(
        scrolled_batch, operation, expected_row, expected_count):
    panel, grid, offset = scrolled_batch
    source = panel._batch_table
    view = source._quick_editor

    if operation == "paste":
        panel._batch_row_clipboard = [panel._batch_row_values(65) for _ in range(3)]
        panel._paste_batch_rows()
    elif operation == "delete_many":
        view.model.selectRowWithModifiers(67, Qt.KeyboardModifier.ShiftModifier.value)
        panel._del_batch_row()
    elif operation == "repeat":
        panel._set_rows_repeat([65], 3)
    elif operation == "edit":
        column = view.model._columns.index(BATCH_SCHEMA.index("condition_label"))
        assert view.model.editCell(65, column, "changed")
    elif operation in {"top", "bottom"}:
        panel._move_batch_row_to_edge(top=operation == "top")
    else:
        getattr(panel, {"duplicate": "_duplicate_batch_row", "add": "_add_batch_row",
                        "delete": "_del_batch_row", "up": "_move_batch_row_up",
                        "down": "_move_batch_row_down"}[operation])()
    QTest.qWait(60)

    assert source.rowCount() == expected_count
    assert source.currentRow() == expected_row
    _assert_batch_current_visible(panel, grid)
    if operation not in {"top", "bottom"}:
        # Keep the position while allowing Qt to reveal a row at the edge.
        assert grid.property("contentY") - grid.property("originY") == pytest.approx(offset, abs=4)
    assert not view.qml_warnings


@pytest.mark.parametrize("delete", [False, True])
def test_batch_structural_undo_redo_preserves_scrolled_position(scrolled_batch, delete):
    panel, grid, offset = scrolled_batch
    before = panel._batch_snapshot()
    if delete:
        panel._del_batch_row()
    else:
        panel._insert_batch_rows(66, [panel._batch_row_values(65) for _ in range(3)])
    after = panel._batch_snapshot()
    QTest.qWait(40)
    for action, expected in [(panel._undo_batch_edit, before), (panel._redo_batch_edit, after)]:
        action()
        QTest.qWait(40)
        assert panel._batch_snapshot().equals(expected)
        _assert_batch_current_visible(panel, grid)
        assert grid.property("contentY") - grid.property("originY") == pytest.approx(offset, abs=4)


def test_batch_undo_with_current_cell_but_no_selection_keeps_location(scrolled_batch):
    panel, grid, offset = scrolled_batch
    source = panel._batch_table
    panel._set_rows_repeat([65], 3)
    source.clearSelection()
    assert source.currentRow() == 65
    assert not panel._selected_batch_rows()
    panel._undo_batch_edit()
    QTest.qWait(40)
    assert source.currentRow() == 65
    _assert_batch_current_visible(panel, grid)
    assert grid.property("contentY") - grid.property("originY") == pytest.approx(offset)


def test_unrelated_row_insertion_keeps_viewport_away_from_old_selection(scrolled_batch):
    panel, grid, offset = scrolled_batch
    source = panel._batch_table
    view = source._quick_editor
    view.model.selectRow(0)
    source.insertRow(source.rowCount())
    QTest.qWait(40)
    assert source.currentRow() == 0
    assert grid.property("contentY") - grid.property("originY") == pytest.approx(offset)
    assert not view.qml_warnings


@pytest.mark.parametrize("delete_all", [False, True])
def test_batch_deletion_clamps_viewport_at_end_and_handles_empty_table(scrolled_batch, delete_all):
    panel, grid, _ = scrolled_batch
    source = panel._batch_table
    view = source._quick_editor
    view.model.selectRow(94)
    view.model.selectRowWithModifiers(99, Qt.KeyboardModifier.ShiftModifier.value)
    grid.setProperty("contentY", grid.property("originY") + grid.property("contentHeight") - grid.height())
    QTest.qWait(30)
    if delete_all:
        source.selectAll()
    panel._del_batch_row()
    QTest.qWait(40)
    assert source.rowCount() == (0 if delete_all else 94)
    if delete_all:
        assert source.currentRow() == -1
        assert grid.property("contentY") == grid.property("originY")
    else:
        assert source.currentRow() == 93
        _assert_batch_current_visible(panel, grid)
    panel._undo_batch_edit()
    QTest.qWait(40)
    assert source.rowCount() == 100
    _assert_batch_current_visible(panel, grid)
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


def test_bulk_cell_notifications_do_not_scan_cached_viewport():
    quick_application()
    source = QTableWidget(1000, 4)
    source.setColumnHidden(1, True)
    source.horizontalHeader().moveSection(3, 0)
    model = TableEditorModel(source)
    for row in range(10):
        model.watchRow(row)
        for column in range(model.columnCount()):
            model.cellAt(row, column)
    model._dirty_cells.clear()

    class LookupOnly(dict):
        def __iter__(self):
            raise AssertionError("A single-cell notification scanned the viewport")

    original = model._cells
    model._cells = LookupOnly(original)
    try:
        for row in range(source.rowCount()):
            source.setItem(row, 3, QTableWidgetItem("3"))
            source.setItem(row, 1, QTableWidgetItem("hidden"))
        visible_column = model._columns.index(3)
        assert model._dirty_cells == {(row, visible_column) for row in range(10)}
    finally:
        model._cells = original
    # Range notifications still refresh only cached cells in source coordinates.
    model._dirty_cells.clear()
    model._data_changed(source.model().index(4, 2), source.model().index(20, 3))
    assert model._dirty_cells == {(row, model._columns.index(column))
                                 for row in range(4, 10) for column in (2, 3)}
    model._flush_dirty()
    assert model.cellAt(4, model._columns.index(3))["value"] == "3"
    model.shutdown()
    source.deleteLater()
    drain_qt_deletions()
