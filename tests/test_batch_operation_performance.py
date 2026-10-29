"""Batch edits keep validation/undo correct without rereading or rebuilding every cell."""
import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pandas as pd
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QPushButton

from ui import presets_panel as panel
from tests.test_batch_row_movement import select_rows


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def widget(app):
    result = panel.PresetsPanel()
    values = dict(result._batch_row_values(0), Run=True, frames="3", repeat="1")
    rows = [dict(values, condition_label=f"row {i}") for i in range(1000)]
    panel._populate_batch_table(result._batch_table, pd.DataFrame(rows))
    result._reset_batch_history()
    result._on_draft_edited()
    result._preview_timer.stop()
    yield result
    result._preview_timer.stop()
    result.close()
    result.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def cell(widget, row, name):
    return widget._batch_table.item(row, panel.BATCH_SCHEMA.index(name))


def test_style_and_help_changes_do_not_enter_history_validation_or_planning(widget):
    item = cell(widget, 500, "condition_label")
    history = widget._batch_history_index
    with patch.object(widget, "_commit_batch_change", wraps=widget._commit_batch_change) as commit:
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setToolTip("A display-only hint")
        assert commit.call_count == 0
        item.setText("Actual data edit")
        assert commit.call_count == 1
    assert widget._batch_history_index == history + 1


def test_edit_reads_batch_cells_once_and_next_edit_cannot_reuse_stale_data(widget):
    table = widget._batch_table
    item = cell(widget, 500, "frames")
    with patch.object(table, "item", wraps=table.item) as reads:
        item.setText("bad")
    # One complete source read plus validation/error/action lookups, rather than
    # the seven complete reads previously triggered by a single data edit.
    assert reads.call_count < 2 * table.rowCount() * table.columnCount()
    assert not hasattr(table, "_batch_read_snapshot")
    assert "frames" in widget._draft_issues[0]
    assert not widget._apply_btn.isEnabled()
    item.setText("4")
    assert not widget._draft_issues
    assert widget._batch_history[-1].iloc[500]["frames"] == "4"
    assert widget._apply_btn.isEnabled()
    assert not hasattr(table, "_batch_read_snapshot")


def test_history_restores_only_changed_cells_and_preserves_unaffected_items(widget):
    table = widget._batch_table
    before = cell(widget, 0, "condition_label")
    edited = cell(widget, 500, "condition_label")
    after = cell(widget, 999, "condition_label")
    edited.setText("changed")
    with patch.object(table, "setRowCount", side_effect=AssertionError("rebuilt all rows")), \
         patch.object(table, "setItem", wraps=table.setItem) as set_item:
        widget._undo_batch_edit()
        assert edited.text() == "row 500"
        widget._redo_batch_edit()
        assert edited.text() == "changed"
        assert set_item.call_count == 0
    assert cell(widget, 0, "condition_label") is before
    assert cell(widget, 999, "condition_label") is after


def test_insert_delete_undo_preserves_suffix_cells_and_raw_values(widget):
    table = widget._batch_table
    tail = cell(widget, 999, "condition_label")
    row = dict(widget._batch_row_values(0), condition_label="inserted", Run=False,
               MeasurePower=True, Vbias_start="", frames="invalid draft")
    widget._insert_batch_rows(10, [row])
    assert table.rowCount() == 1001
    widget._undo_batch_edit()
    assert table.rowCount() == 1000
    assert cell(widget, 999, "condition_label") is tail
    widget._redo_batch_edit()
    assert table.rowCount() == 1001
    assert widget._batch_row_values(10) == row
    select_rows(widget, [10])
    widget._del_batch_row()
    widget._undo_batch_edit()
    assert widget._batch_row_values(10) == row
    assert cell(widget, 1000, "condition_label") is tail


def test_error_highlights_follow_moved_items_and_deleted_errors_are_safe(widget):
    invalid = cell(widget, 20, "frames")
    invalid.setText("bad")
    select_rows(widget, [20, 21])
    widget._move_batch_row_up()
    assert cell(widget, 19, "frames") is invalid
    assert "Batch row 20" in widget._draft_issues[0]
    widget._del_batch_row()
    assert not widget._draft_issues
    widget._undo_batch_edit()
    assert "frames" in widget._draft_issues[0]
    cell(widget, 19, "frames").setText("3")
    assert not widget._draft_issues


def test_failed_refresh_releases_its_read_snapshot(widget):
    with patch.object(widget, "_record_batch_history", side_effect=RuntimeError("test failure")):
        with pytest.raises(RuntimeError, match="test failure"):
            widget._commit_batch_change()
    assert not hasattr(widget._batch_table, "_batch_read_snapshot")


def test_batch_edits_and_history_preserve_loop_and_order_controls(widget):
    order = widget._execution_order_table
    controls = [order.cellWidget(row, 2) for row in range(order.rowCount())]
    buttons = [control.findChildren(QPushButton) for control in controls]
    with patch.object(widget, "_sync_setup_value_editors", wraps=widget._sync_setup_value_editors) as setup, \
         patch.object(widget, "_refresh_execution_order_table", wraps=widget._refresh_execution_order_table) as rebuild, \
         patch.object(widget, "_refresh_draft_state", wraps=widget._refresh_draft_state) as validate:
        cell(widget, 500, "condition_label").setText("changed")
        widget._undo_batch_edit()
        widget._redo_batch_edit()
        assert validate.call_count == 3
        assert setup.call_count == 0
        assert rebuild.call_count == 0
    assert controls == [order.cellWidget(row, 2) for row in range(order.rowCount())]
    assert buttons == [control.findChildren(QPushButton) for control in controls]
    # Existing signal connections still move the correct nesting level.
    buttons[0][1].click()
    assert widget._nested_schedule_enabled
    assert widget._execution_order[1]["kind"] == "group"


def test_loop_and_execution_order_changes_still_rebuild_order_controls(widget):
    order = widget._execution_order_table
    previous = order.cellWidget(0, 2)
    with patch.object(widget, "_refresh_execution_order_table", wraps=widget._refresh_execution_order_table) as rebuild:
        widget._loop_table.item(0, 2).setText("860, 861")
        assert rebuild.call_count == 1
        assert order.cellWidget(0, 2) is not previous
        assert "x2" in order.item(0, 0).text()
        previous = order.cellWidget(0, 2)
        widget._move_execution_order(0, 1)
        assert rebuild.call_count == 2
        assert order.cellWidget(0, 2) is not previous
        assert widget._nested_schedule_enabled


def test_large_draft_selection_does_not_recompute_a_row_independent_filename(widget):
    widget._refresh_filename_preview()
    assert "Apply this large draft" in widget._filename_preview_lbl.text()
    widget._preview_timer.stop()
    with patch.object(widget, "_draft_loop_and_batch", side_effect=AssertionError("selection scanned draft")):
        select_rows(widget, [100, 500])
        assert not widget._preview_timer.isActive()
    cell(widget, 500, "frames").setText("bad")
    assert widget._preview_timer.isActive()
    assert not widget._apply_btn.isEnabled()


def test_power_readbacks_skip_filename_work_and_preserve_real_edit_updates(app):
    result = panel.PresetsPanel(bind_controller=False, initialize_plan=False)
    try:
        cell(result, 0, "MeasurePower").setCheckState(Qt.CheckState.Checked)
        result._refresh_filename_preview()
        result._preview_timer.stop()
        # Deliver any newly scheduled preview immediately while handling the
        # readback burst, without sleeping or relying on a wall-time budget.
        result._preview_timer.setInterval(0)
        displayed = (
            result._filename_preview_lbl.text(),
            result._save_path_preview_lbl.text(),
            result._upcoming_preview.toPlainText(),
        )
        assert "PowerPending" in displayed[0]
        with patch.object(result, "_refresh_filename_preview", wraps=result._refresh_filename_preview) as preview, \
             patch.object(panel, "_read_batch_table", wraps=panel._read_batch_table) as reads:
            for i in range(20):
                result._cache_preview_power((i + 1) * 1e-6)
                app.processEvents()
            assert preview.call_count == 0
            assert reads.call_count == 0
            assert displayed == (
                result._filename_preview_lbl.text(),
                result._save_path_preview_lbl.text(),
                result._upcoming_preview.toPlainText(),
            )

            cell(result, 0, "condition_label").setText("readback test")
            assert result._preview_timer.isActive()
            app.processEvents()
            assert preview.call_count == 1

            result._sample_edit.setText("readback sample")
            assert result._preview_timer.isActive()
            result._cache_preview_power(1e-6)
            assert result._preview_timer.isActive()
            app.processEvents()
            assert preview.call_count == 2
            assert "readback sample" in result._save_path_preview_lbl.text()
    finally:
        result._preview_timer.stop()
        result.close()
        result.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
