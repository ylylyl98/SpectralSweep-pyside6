"""Full, selectable preview of an expanded motion sweep sequence."""
from __future__ import annotations

from ui.style_roles import set_style_role

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from PySide6.QtCore import Signal
from ui.table_layout import fit_table_columns
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QLabel, QPushButton, QTableView, QVBoxLayout,
)


from app.motion_sequence_model import COLUMNS, MAX_SEQUENCE_ROWS, MotionSequenceModel


class _SequenceTableView(QTableView):
    """Row/column counts used by the shared visible-row column fitter."""

    def rowCount(self):
        return self.model().rowCount()

    def columnCount(self):
        return self.model().columnCount()

    def currentRow(self):
        return self.currentIndex().row()


class MotionSequencePreviewDialog(QDialog):
    """Review and customize every expanded sequence entry.

    ``rows`` are copied at construction.  Every selected result retains the
    original mapping, including source identifiers that the worker may need,
    while the table order determines execution order.
    """

    selectionApplied = Signal()

    COLUMNS = COLUMNS

    def __init__(self, rows: Sequence[Mapping[str, Any]] | None, *, point_count: int | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Motion sweep sequence preview")
        self.resize(1000, 600)
        self.setMinimumSize(700, 420)
        self.sequence_model = MotionSequenceModel(rows, point_count=point_count, parent=self)
        self._selected_result: list[dict[str, Any]] | None = None
        self._selected_source_indices: list[int] = []
        self._build_ui()
        self.sequence_model.selectionChanged.connect(self._selection_changed)
        if self.sequence_model.truncated:
            self._status.setText(f"Sequence exceeds {MAX_SEQUENCE_ROWS:,} entries; preview capped at {MAX_SEQUENCE_ROWS:,}.")
        if self.sequence_model.count:
            self._table.selectRow(0)
        self._update_summary()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(5)
        self._summary = QLabel()
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        self._table = _SequenceTableView()
        self._table.setModel(self.sequence_model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setMinimumHeight(260)
        fit_table_columns(self._table, (40, 46, 54, 100, 64, 64, 64, 64, 64, 58, 58, 56, 56))
        self.table = self._table
        root.addWidget(self._table, 1)

        self._status = QLabel()
        self._status.setWordWrap(True)
        set_style_role(self._status, 'text', tone='danger')
        self.status_label = self._status
        root.addWidget(self._status)

        movement = QHBoxLayout()
        self._up_button = QPushButton("Move up")
        self._down_button = QPushButton("Move down")
        self._select_all_button = QPushButton("Select all")
        self._reset_button = QPushButton("Reset / all")
        movement.addWidget(self._up_button)
        movement.addWidget(self._down_button)
        movement.addStretch(1)
        movement.addWidget(self._select_all_button)
        movement.addWidget(self._reset_button)
        root.addLayout(movement)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self._apply_button = QPushButton("Apply custom selection")
        self._cancel_button = QPushButton("Cancel")
        self._apply_button.setDefault(True)
        actions.addWidget(self._apply_button)
        actions.addWidget(self._cancel_button)
        root.addLayout(actions)
        self._up_button.clicked.connect(lambda: self.move_selected(-1))
        self._down_button.clicked.connect(lambda: self.move_selected(1))
        self._select_all_button.clicked.connect(self.select_all)
        self._reset_button.clicked.connect(self.reset_all)
        self._apply_button.clicked.connect(self.apply_selection)
        self._cancel_button.clicked.connect(self.reject)

    def _update_summary(self) -> None:
        self._summary.setText(f"{self.sequence_model.count:,} sequence entries · {self.sequence_model.selectedCount:,} selected · order follows the table")
        self._apply_button.setEnabled(bool(self.sequence_model.count))

    def _selection_changed(self) -> None:
        self._status.clear()
        self._update_summary()

    def selected_rows(self) -> list[dict[str, Any]]:
        return self.sequence_model.selected_rows()

    def selected_sequence(self) -> list[dict[str, Any]]:
        return self.selected_rows()

    def selected_indices(self) -> list[int]:
        return self.sequence_model.selected_indices()

    def selection_result(self) -> dict[str, Any]:
        rows = self.selected_rows()
        return {"rows": rows, "indices": self.selected_indices()}

    def select_all(self) -> None:
        self._set_all(True)

    def reset_all(self) -> None:
        self._set_all(True)
        self._selected_result = None
        self._selected_source_indices = []
        self._status.clear()

    def _set_all(self, checked: bool) -> None:
        self.sequence_model.set_all_checked(checked)
        self._status.clear()

    def move_selected(self, delta: int) -> bool:
        row = self._table.currentRow()
        target = row + int(delta)
        if not self.sequence_model.move_entry(row, target):
            return False
        self._table.selectRow(target)
        return True

    move_up = lambda self: self.move_selected(-1)
    move_down = lambda self: self.move_selected(1)

    def apply_selection(self) -> list[dict[str, Any]] | None:
        rows = self.selected_rows()
        if not rows:
            self._status.setText("Select at least one sequence entry before applying.")
            return None
        self._selected_result = rows
        self._selected_source_indices = [row["_source_index"] for row in rows]
        self.selectionApplied.emit()
        self.accept()
        return copy.deepcopy(rows)

    def result_rows(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._selected_result) if self._selected_result is not None else self.selected_rows()

    def result_indices(self) -> list[int]:
        if self._selected_result is not None:
            return list(self._selected_source_indices)
        return self.selected_indices()


def create_motion_sequence_preview(rows, *, point_count=None, parent=None):
    """Use the opt-in Quick frontend without importing Quick in Widgets mode."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is not None and app.property("spectralQuick"):
        from ui.quick_motion_sequence import QuickMotionSequencePreviewDialog
        return QuickMotionSequencePreviewDialog(rows, point_count=point_count, parent=parent)
    return MotionSequencePreviewDialog(rows, point_count=point_count, parent=parent)


__all__ = ["MotionSequencePreviewDialog", "create_motion_sequence_preview"]
