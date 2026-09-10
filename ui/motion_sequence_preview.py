"""Full, selectable preview of an expanded motion sweep sequence."""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from typing import Any

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout,
)


_ROW_ROLE = Qt.ItemDataRole.UserRole
_SOURCE_ROLE = Qt.ItemDataRole.UserRole + 1
_MAX_ROWS = 100_000


def _display(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "1" if value else "0"
    try:
        number = float(value)
        if math.isfinite(number):
            return f"{number:.8g}"
    except (TypeError, ValueError):
        pass
    return str(value)


class MotionSequencePreviewDialog(QDialog):
    """Review and customize every expanded sequence entry.

    ``rows`` are copied at construction.  Every selected result retains the
    original mapping, including source identifiers that the worker may need,
    while the table order determines execution order.
    """

    selectionApplied = Signal()

    COLUMNS = (
        ("Use", "use"), ("Sequence", "sequence"), ("Condition", "condition_index"),
        ("Label", "label"), ("D", "D"), ("F", "F"), ("Vtg", "Vtg"),
        ("Vbg", "Vbg"), ("Vbias", "Vbias"), ("Rot1", "rot1"),
        ("Rot2", "rot2"), ("Repeat", "repeat"), ("Points", "point_count"),
    )

    def __init__(self, rows: Sequence[Mapping[str, Any]] | None, *, point_count: int | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Motion sweep sequence preview")
        self.resize(1000, 600)
        self.setMinimumSize(700, 420)
        self._default_point_count = point_count
        self._entries: list[dict[str, Any]] = []
        self._selected_result: list[dict[str, Any]] | None = None
        self._selected_source_indices: list[int] = []
        self._build_ui()
        self._load_rows(rows or [])

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(5)
        self._summary = QLabel()
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        self._table = QTableWidget(0, len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels([title for title, _ in self.COLUMNS])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setMinimumHeight(260)
        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        for column in range(len(self.COLUMNS)):
            header.setSectionResizeMode(column, header.ResizeMode.Interactive)
        for column, width in ((0, 42), (1, 70), (2, 76), (3, 105), (4, 68), (5, 68),
                              (6, 72), (7, 72), (8, 78), (9, 68), (10, 68), (11, 62), (12, 62)):
            self._table.setColumnWidth(column, width)
        self._table.itemChanged.connect(self._table_item_changed)
        self.table = self._table
        root.addWidget(self._table, 1)

        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: #b3261e;")
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

    @staticmethod
    def _normalize(source: Mapping[str, Any], index: int, point_count: int | None) -> dict[str, Any]:
        row = copy.deepcopy(dict(source))
        row.setdefault("sequence", index + 1)
        row.setdefault("condition_index", row.get("condition", index + 1))
        row.setdefault("label", row.get("condition_label", ""))
        row.setdefault("point_count", row.get("points", point_count))
        row.setdefault("enabled", row.get("use", True))
        row["_source_index"] = int(row.get("_source_index", row.get("source_index", index)))
        return row

    def _load_rows(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if len(rows) > _MAX_ROWS:
            rows = rows[:_MAX_ROWS]
            self._status.setText(f"Sequence exceeds {_MAX_ROWS:,} entries; preview capped at {_MAX_ROWS:,}.")
        self._entries = [self._normalize(row, index, self._default_point_count) for index, row in enumerate(rows)]
        self._reload()

    def _reload(self, selected_row: int | None = None) -> None:
        with QSignalBlocker(self._table):
            self._table.setRowCount(0)
            for row_number, row in enumerate(self._entries):
                self._table.insertRow(row_number)
                enabled = bool(row.get("enabled", row.get("use", True)))
                for column, (_, key) in enumerate(self.COLUMNS):
                    item = QTableWidgetItem()
                    if column == 0:
                        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                        item.setCheckState(Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
                    else:
                        value = row.get(key)
                        if key == "D": value = row.get("D", row.get("doping_v"))
                        elif key == "F": value = row.get("F", row.get("efield_v"))
                        elif key == "Vtg": value = row.get("Vtg", row.get("vtg_v"))
                        elif key == "Vbg": value = row.get("Vbg", row.get("vbg_v"))
                        elif key == "Vbias": value = row.get("Vbias", row.get("vbias_v"))
                        elif key == "rot1": value = row.get("rot1", row.get("rotation"))
                        item.setText(_display(value))
                    item.setData(_ROW_ROLE, copy.deepcopy(row))
                    item.setData(_SOURCE_ROLE, row.get("_source_index"))
                    self._table.setItem(row_number, column, item)
        if self._entries:
            self._table.selectRow(max(0, min(selected_row if selected_row is not None else 0, len(self._entries) - 1)))
        self._update_summary()

    def _update_summary(self) -> None:
        selected = len(self.selected_rows())
        self._summary.setText(f"{len(self._entries):,} sequence entries · {selected:,} selected · order follows the table")
        self._apply_button.setEnabled(bool(self._entries))

    def _table_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0 or item.row() >= len(self._entries):
            return
        self._entries[item.row()]["enabled"] = item.checkState() == Qt.CheckState.Checked
        self._entries[item.row()]["use"] = self._entries[item.row()]["enabled"]
        self._status.clear()
        self._update_summary()

    def _row_state(self, row: int) -> dict[str, Any]:
        source = self._table.item(row, 1)
        value = source.data(_ROW_ROLE) if source else None
        entry = copy.deepcopy(value) if isinstance(value, dict) else copy.deepcopy(self._entries[row])
        use = self._table.item(row, 0)
        entry["enabled"] = bool(use and use.checkState() == Qt.CheckState.Checked)
        entry["use"] = entry["enabled"]
        return entry

    def selected_rows(self) -> list[dict[str, Any]]:
        return [self._row_state(row) for row in range(self._table.rowCount()) if self._is_checked(row)]

    def _is_checked(self, row: int) -> bool:
        item = self._table.item(row, 0)
        return bool(item and item.checkState() == Qt.CheckState.Checked)

    def selected_sequence(self) -> list[dict[str, Any]]:
        return self.selected_rows()

    def selected_indices(self) -> list[int]:
        return [int(row.get("_source_index", index)) for index, row in enumerate(self.selected_rows())]

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
        with QSignalBlocker(self._table):
            for row in range(self._table.rowCount()):
                item = self._table.item(row, 0)
                if item is not None:
                    item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                if row < len(self._entries):
                    self._entries[row]["enabled"] = checked
                    self._entries[row]["use"] = checked
        self._status.clear()
        self._update_summary()

    def move_selected(self, delta: int) -> bool:
        row = self._table.currentRow()
        target = row + int(delta)
        if row < 0 or target < 0 or target >= self._table.rowCount():
            return False
        states = [self._row_state(index) for index in range(self._table.rowCount())]
        states.insert(target, states.pop(row))
        self._entries = states
        self._reload(target)
        return True

    move_up = lambda self: self.move_selected(-1)
    move_down = lambda self: self.move_selected(1)

    def apply_selection(self) -> list[dict[str, Any]] | None:
        rows = self.selected_rows()
        if not rows:
            self._status.setText("Select at least one sequence entry before applying.")
            return None
        self._selected_result = copy.deepcopy(rows)
        self._selected_source_indices = self.selected_indices()
        self.selectionApplied.emit()
        self.accept()
        return copy.deepcopy(rows)

    def result_rows(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._selected_result if self._selected_result is not None else self.selected_rows())

    def result_indices(self) -> list[int]:
        if self._selected_result is not None:
            return list(self._selected_source_indices)
        return self.selected_indices()


__all__ = ["MotionSequencePreviewDialog"]
