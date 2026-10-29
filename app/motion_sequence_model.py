"""Owned motion-preview rows shared by Widgets and Qt Quick views.

Input and exported rows are deeply copied. Model reads, checkboxes, counts and
row moves operate on the owned rows without copying their payloads.
"""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Property, Qt, Signal, Slot


MAX_SEQUENCE_ROWS = 100_000
COLUMNS = (
    ("Use", "use"), ("Sequence", "sequence"), ("Condition", "condition_index"),
    ("Label", "label"), ("D", "D"), ("F", "F"), ("Vtg", "Vtg"),
    ("Vbg", "Vbg"), ("Vbias", "Vbias"), ("Rot1", "rot1"),
    ("Rot2", "rot2"), ("Repeat", "repeat"), ("Points", "point_count"),
)
_ALIASES = {"D": "doping_v", "F": "efield_v", "Vtg": "vtg_v", "Vbg": "vbg_v",
            "Vbias": "vbias_v", "rot1": "rotation"}


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


class MotionSequenceModel(QAbstractTableModel):
    """Selectable, reorderable sequence with stable source identifiers.

    QML names: count, selectedCount, truncated, display/checkState/checked/
    sourceIndex/value roles, setChecked, isChecked, setAllChecked, moveEntry,
    rowData, selectedRows and selectedIndices. Selection lives in the model,
    never in recycled delegates. moveEntry uses final row coordinates.
    """

    selectionChanged = Signal()
    CheckedRole = Qt.ItemDataRole.UserRole + 1
    SourceIndexRole = Qt.ItemDataRole.UserRole + 2
    ValueRole = Qt.ItemDataRole.UserRole + 3

    def __init__(self, rows: Sequence[Mapping[str, Any]] | None = None, *,
                 point_count: int | None = None, parent=None) -> None:
        super().__init__(parent)
        rows = rows if rows is not None else ()
        self._truncated = len(rows) > MAX_SEQUENCE_ROWS
        self._rows = []
        self._selected_count = 0
        for index in range(min(len(rows), MAX_SEQUENCE_ROWS)):
            row = copy.deepcopy(dict(rows[index]))
            row.setdefault("sequence", index + 1)
            row.setdefault("condition_index", row.get("condition", index + 1))
            row.setdefault("label", row.get("condition_label", ""))
            row.setdefault("point_count", row.get("points", point_count))
            row["enabled"] = bool(row.get("enabled", row.get("use", True)))
            row["use"] = row["enabled"]
            row["_source_index"] = int(row.get("_source_index", row.get("source_index", index)))
            self._rows.append(row)
            self._selected_count += int(row["enabled"])

    count = Property(int, lambda self: len(self._rows), constant=True)
    selectedCount = Property(int, lambda self: self._selected_count, notify=selectionChanged)
    truncated = Property(bool, lambda self: self._truncated, constant=True)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMNS)

    def roleNames(self):
        return {int(Qt.ItemDataRole.DisplayRole): b"display",
                int(Qt.ItemDataRole.CheckStateRole): b"checkState",
                int(self.CheckedRole): b"checked", int(self.SourceIndexRole): b"sourceIndex",
                int(self.ValueRole): b"value"}

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        column = index.column()
        if not 0 <= column < len(COLUMNS):
            return None
        if role == self.CheckedRole:
            return row["enabled"]
        if role == self.SourceIndexRole:
            return row["_source_index"]
        if role == Qt.ItemDataRole.CheckStateRole:
            if column == 0:
                return Qt.CheckState.Checked if row["enabled"] else Qt.CheckState.Unchecked
            return None
        key = COLUMNS[column][1]
        value = row.get(key, row.get(_ALIASES.get(key)))
        if role == self.ValueRole:
            # Delegates receive scalar cell values, never mutable owned data.
            return value if value is None or isinstance(value, (str, int, float, bool)) else _display(value)
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return "" if column == 0 else _display(value)
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(COLUMNS):
            if role == Qt.ItemDataRole.DisplayRole:
                return {1: "Seq.", 2: "Cond.\n#"}.get(section, COLUMNS[section][0])
            if role == Qt.ItemDataRole.ToolTipRole and section == 2:
                return "Condition number"
        if orientation == Qt.Orientation.Vertical and role == Qt.ItemDataRole.DisplayRole:
            return section + 1
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return flags | Qt.ItemFlag.ItemIsUserCheckable if index.column() == 0 else flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if not index.isValid() or index.column() != 0:
            return False
        if role == self.CheckedRole:
            return self.set_checked(index.row(), bool(value))
        if role == Qt.ItemDataRole.CheckStateRole:
            if value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value):
                return self.set_checked(index.row(), True)
            if value in (Qt.CheckState.Unchecked, Qt.CheckState.Unchecked.value):
                return self.set_checked(index.row(), False)
        return False

    def set_checked(self, row: int, checked: bool) -> bool:
        if not 0 <= row < len(self._rows):
            return False
        checked = bool(checked)
        entry = self._rows[row]
        if entry["enabled"] == checked:
            return True
        entry["enabled"] = entry["use"] = checked
        self._selected_count += 1 if checked else -1
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1),
                              [int(Qt.ItemDataRole.CheckStateRole), int(self.CheckedRole), int(self.ValueRole)])
        self.selectionChanged.emit()
        return True

    def is_checked(self, row: int) -> bool:
        return 0 <= row < len(self._rows) and self._rows[row]["enabled"]

    def set_all_checked(self, checked: bool) -> None:
        checked = bool(checked)
        target = len(self._rows) if checked else 0
        if self._selected_count == target:
            return
        for row in self._rows:
            row["enabled"] = row["use"] = checked
        self._selected_count = target
        self.dataChanged.emit(self.index(0, 0), self.index(len(self._rows) - 1, len(COLUMNS) - 1),
                              [int(Qt.ItemDataRole.CheckStateRole), int(self.CheckedRole), int(self.ValueRole)])
        self.selectionChanged.emit()

    def moveRows(self, sourceParent, sourceRow, count, destinationParent, destinationChild):
        if (sourceParent.isValid() or destinationParent.isValid() or count < 1
                or sourceRow < 0 or sourceRow + count > len(self._rows)
                or destinationChild < 0 or destinationChild > len(self._rows)
                or sourceRow <= destinationChild <= sourceRow + count):
            return False
        if not self.beginMoveRows(sourceParent, sourceRow, sourceRow + count - 1,
                                  destinationParent, destinationChild):
            return False
        target = destinationChild - count if destinationChild > sourceRow else destinationChild
        moved = self._rows[sourceRow:sourceRow + count]  # references only; payloads stay owned
        del self._rows[sourceRow:sourceRow + count]
        self._rows[target:target] = moved
        self.endMoveRows()
        return True

    def move_entry(self, source: int, target: int) -> bool:
        if not 0 <= target < len(self._rows):
            return False
        destination = target + 1 if target > source else target
        return self.moveRows(QModelIndex(), source, 1, QModelIndex(), destination)

    def row_data(self, row: int) -> dict[str, Any]:
        return copy.deepcopy(self._rows[row]) if 0 <= row < len(self._rows) else {}

    @Slot(int, result="QVariantList")
    def displayFields(self, row: int):
        """Read one visible record's scalars without exporting its payload.

        Quick requests this only for instantiated list delegates. The table
        model remains unchanged for Widgets and planner consumers.
        """
        if not 0 <= row < len(self._rows):
            return []
        return [{"column": column, "label": title,
                 "text": self.data(self.index(row, column), Qt.ItemDataRole.DisplayRole)}
                for column, (title, _key) in enumerate(COLUMNS)]

    def selected_rows(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(row) for row in self._rows if row["enabled"]]

    def selected_indices(self) -> list[int]:
        return [row["_source_index"] for row in self._rows if row["enabled"]]

    # Use actual Python names for QML slots. Renaming @Slot(name=...) can recurse
    # through PySide's dynamic dispatch when invoked by QQmlEngine (Qt 6.11).
    @Slot(int, bool, result=bool)
    def setChecked(self, row: int, checked: bool) -> bool:
        return self.set_checked(row, checked)

    @Slot(int, result=bool)
    def isChecked(self, row: int) -> bool:
        return self.is_checked(row)

    @Slot(bool)
    def setAllChecked(self, checked: bool) -> None:
        self.set_all_checked(checked)

    @Slot(int, int, result=bool)
    def moveEntry(self, source: int, target: int) -> bool:
        return self.move_entry(source, target)

    @Slot(int, result="QVariantMap")
    def rowData(self, row: int) -> dict[str, Any]:
        return self.row_data(row)

    @Slot(result="QVariantList")
    def selectedRows(self) -> list[dict[str, Any]]:
        return self.selected_rows()

    @Slot(result="QVariantList")
    def selectedIndices(self) -> list[int]:
        return self.selected_indices()
