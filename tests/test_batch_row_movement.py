"""Batch row ordering is stable for extended selections and remains undoable."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
import pytest
from PySide6.QtCore import QItemSelection, QItemSelectionModel, Qt
from PySide6.QtWidgets import QApplication

from ui import presets_panel as panel


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def widget(app, monkeypatch):
    result = panel.PresetsPanel()
    # These tests exercise table edits and the real undo history without
    # rebuilding the acquisition preview after each edit.
    monkeypatch.setattr(result, "_on_draft_edited", lambda: None)
    defaults = result._batch_row_values(0)
    rows = [dict(defaults, condition_label=str(row), Run=row % 2 == 0,
                 MeasurePower=row % 3 == 0, When=f"repeat >= {row}")
            for row in range(6)]
    panel._populate_batch_table(result._batch_table, pd.DataFrame(rows))
    result._reset_batch_history()
    yield result
    result.close()


def select_rows(widget, rows, *, current=None, column=3):
    table = widget._batch_table
    selection = QItemSelection()
    for row in rows:
        selection.select(table.model().index(row, 0),
                         table.model().index(row, table.columnCount() - 1))
    table.setCurrentCell(rows[0] if current is None else current, column,
                         QItemSelectionModel.SelectionFlag.NoUpdate)
    table.selectionModel().select(selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)
    widget._update_batch_row_buttons()


def labels(widget):
    table = widget._batch_table
    column = panel.BATCH_SCHEMA.index("condition_label")
    return [table.item(row, column).text() for row in range(table.rowCount())]


@pytest.mark.parametrize("rows,offset,expected,selection,current", [
    ([2, 3], -1, [0, 2, 3, 1, 4, 5], [1, 2], 2),
    ([1, 2], 1, [0, 3, 1, 2, 4, 5], [2, 3], 3),
    ([1, 3], -1, [1, 0, 3, 2, 4, 5], [0, 2], 2),
    ([1, 3], 1, [0, 2, 1, 4, 3, 5], [2, 4], 4),
    ([0, 1, 3, 5], -1, [0, 1, 3, 2, 5, 4], [0, 1, 2, 4], 4),
    ([0, 2, 4, 5], 1, [1, 0, 3, 2, 4, 5], [1, 3, 4, 5], 5),
])
def test_up_down_moves_blocks_and_preserves_selected_rows(
        widget, rows, offset, expected, selection, current):
    select_rows(widget, rows, current=rows[-1], column=7)
    history = widget._batch_history_index

    (widget._batch_up_btn if offset < 0 else widget._batch_down_btn).click()

    assert labels(widget) == list(map(str, expected))
    assert widget._selected_batch_rows() == selection
    assert widget._batch_table.currentRow() == current
    assert widget._batch_table.currentColumn() == 7
    assert widget._batch_history_index == history + 1


@pytest.mark.parametrize("rows,up,down", [
    ([0, 1], False, True),
    ([4, 5], True, False),
    ([0, 3, 5], True, True),
    (list(range(6)), False, False),
    ([], False, False),
])
def test_movement_controls_allow_any_movable_selected_block(widget, rows, up, down):
    if rows:
        select_rows(widget, rows)
    else:
        widget._batch_table.clearSelection()
        widget._update_batch_row_buttons()
    assert widget._batch_up_btn.isEnabled() is up
    assert widget._batch_down_btn.isEnabled() is down
    assert widget._batch_move_top_action.isEnabled() is up
    assert widget._batch_move_bottom_action.isEnabled() is down


@pytest.mark.parametrize("rows,offset", [([0, 1], -1), ([4, 5], 1), (list(range(6)), -1)])
def test_pinned_selection_is_a_noop_without_history(widget, rows, offset):
    select_rows(widget, rows)
    history = widget._batch_history_index
    widget._move_batch_row(offset)
    assert labels(widget) == list(map(str, range(6)))
    assert widget._selected_batch_rows() == rows
    assert widget._batch_history_index == history


@pytest.mark.parametrize("top,expected,selection,current", [
    (True, [0, 2, 5, 1, 3, 4], [0, 1, 2], 1),
    (False, [1, 3, 4, 0, 2, 5], [3, 4, 5], 4),
])
def test_move_to_edge_keeps_selection_order(widget, top, expected, selection, current):
    select_rows(widget, [0, 2, 5], current=2)
    widget._move_batch_row_to_edge(top=top)
    assert labels(widget) == list(map(str, expected))
    assert widget._selected_batch_rows() == selection
    assert widget._batch_table.currentRow() == current


def test_move_preserves_items_check_states_and_roles_without_repopulation(widget, monkeypatch):
    table = widget._batch_table
    originals = [[table.item(row, column) for column in range(table.columnCount())]
                 for row in range(table.rowCount())]
    originals[3][3].setData(Qt.ItemDataRole.UserRole, "retained row metadata")
    select_rows(widget, [1, 3], current=3)

    def unexpected_repopulation(*args, **kwargs):
        raise AssertionError("Row movement must retain existing table items")

    monkeypatch.setattr(panel, "_populate_batch_table", unexpected_repopulation)
    widget._move_batch_row_up()
    for destination, source in enumerate([1, 0, 3, 2, 4, 5]):
        for column, item in enumerate(originals[source]):
            assert table.item(destination, column) is item
        assert table.item(destination, 0).checkState() == (
            Qt.CheckState.Checked if source % 2 == 0 else Qt.CheckState.Unchecked)
        assert table.item(destination, 2).checkState() == (
            Qt.CheckState.Checked if source % 3 == 0 else Qt.CheckState.Unchecked)
    assert table.item(2, 3).data(Qt.ItemDataRole.UserRole) == "retained row metadata"


@pytest.mark.parametrize("operation", ["up", "down", "top", "bottom"])
def test_multirow_move_is_one_undo_redo_step(widget, operation):
    select_rows(widget, [1, 3], current=3)
    before = widget._batch_snapshot()
    history = widget._batch_history_index
    if operation in {"up", "down"}:
        widget._move_batch_row(-1 if operation == "up" else 1)
    else:
        widget._move_batch_row_to_edge(top=operation == "top")
    after = widget._batch_snapshot()
    assert not after.equals(before)
    assert widget._batch_history_index == history + 1
    widget._undo_batch_edit()
    assert widget._batch_snapshot().equals(before)
    widget._redo_batch_edit()
    assert widget._batch_snapshot().equals(after)
