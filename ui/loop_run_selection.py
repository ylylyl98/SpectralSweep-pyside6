"""Shared run selections and a bounded, searchable Qt value picker."""
from copy import deepcopy

from PySide6.QtCore import QAbstractListModel, QModelIndex, QSortFilterProxyModel, Qt, QEvent
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
    QListView, QPushButton, QVBoxLayout,
)


class LoopRunSelections:
    """Keep explicit positions tied to a complete group definition.

    Membership uses stable row IDs, never a displayed group number. Bulk array
    edits cannot reliably identify repeated occurrences, so an explicit choice
    needs review after any parsed value change. All remains an explicit mode.
    """

    def __init__(self, records=None):
        self.states = {}
        for record in records or []:
            if isinstance(record, dict) and isinstance(record.get("rows"), list):
                state = deepcopy(record)
                positions = state.get("positions")
                if ("positions" not in state or positions is not None and
                        (not isinstance(positions, list) or any(type(i) is not int or i < 0 for i in positions))):
                    state.update(positions=[], stale=True)
                elif positions is not None:
                    state["positions"] = sorted(set(positions))
                self.states[tuple(sorted(str(v) for v in state["rows"]))] = state
        self.groups = {}

    def synchronize(self, rows, mode, parse):
        groups = {}
        for row_i, row in enumerate(rows):
            if not row["Enable"]:
                continue
            level = len(groups) + 1 if mode == "Synchronize" else 1 if mode == "Zip" else int(row["Group"] or 1)
            groups.setdefault(level, []).append((row_i, row))
        self.groups = {}
        for level, members in groups.items():
            key = tuple(sorted(row["_run_id"] for _, row in members))
            baseline = {}
            valid = True
            for _, row in members:
                try:
                    values = parse(row["Values"], row["Parameter"])
                except ValueError:
                    values = None
                valid = valid and bool(values)
                baseline[row["_run_id"]] = {"parameter": row["Parameter"], "values": values}
            if key not in self.states:
                overlap = any(set(key) & set(old) and state.get("positions") is not None
                              for old, state in self.states.items())
                self.states[key] = dict(rows=list(key), positions=[] if overlap else None,
                                        baseline=deepcopy(baseline), stale=overlap)
            state = self.states[key]
            if state.get("positions") is not None and valid and state.get("baseline") != baseline:
                state["stale"] = True
            if state.get("positions") is None and valid:
                state["baseline"] = deepcopy(baseline)
            self.groups[level] = dict(key=key, members=members, baseline=baseline, valid=valid)
        return self.groups

    def choose(self, level, positions):
        group = self.groups[level]
        key = group["key"]
        # A new explicit choice supersedes overlapping retired memberships.
        self.states = {old: state for old, state in self.states.items()
                       if old == key or not (set(old) & set(key))}
        self.states[key] = dict(rows=list(key), positions=None if positions is None else sorted(set(positions)),
                                baseline=deepcopy(group["baseline"]), stale=False)

    def positions(self):
        return {level: "reselect" if self.states[group["key"]].get("stale") else self.states[group["key"]]["positions"]
                for level, group in self.groups.items()
                if self.states[group["key"]].get("positions") is not None}

    def export(self):
        return deepcopy(list(self.states.values()))


class RunSelectionCombo(QComboBox):
    """Load individual choices only when opened; large arrays use the search picker."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.labels = []
        self.choice = "all"
        self.summary = "All values"
        self.tab_to_group = None

    def event(self, event):
        # Keep continuous table editing after tabbing through the new selector.
        if (event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Tab
                and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                and not self.view().isVisible() and self.tab_to_group is not None
                and self.tab_to_group()):
            event.accept()
            return True
        return super().event(event)

    def configure(self, labels, choice, summary):
        self.labels, self.choice, self.summary = labels, choice, summary
        blocked = self.blockSignals(True)
        self.clear()
        self.addItem(summary, choice)
        self.blockSignals(blocked)
        self.setToolTip(summary + "\nPaired rows share the same positions. Values remain unchanged.")
        self.setAccessibleDescription(self.toolTip())

    def showPopup(self):
        blocked = self.blockSignals(True)
        self.clear()
        self.addItem("All values", "all")
        for position, label in enumerate(self.labels[:100]):
            self.addItem(label, position)
        self.addItem("Select several…" if len(self.labels) <= 100 else "Find / select values…", "several")
        index = self.findData(self.choice)
        if index < 0:
            self.addItem(self.summary, self.choice)
            index = self.count() - 1
        self.setCurrentIndex(index)
        self.blockSignals(blocked)
        super().showPopup()


class _ValueListModel(QAbstractListModel):
    def __init__(self, labels, positions, parent=None):
        super().__init__(parent)
        self.labels = labels
        self.checked = set(range(len(labels))) if positions is None else set(positions)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.labels)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return self.labels[index.row()]
        if role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if index.row() in self.checked else Qt.CheckState.Unchecked

    def flags(self, index):
        return super().flags(index) | Qt.ItemFlag.ItemIsUserCheckable

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.CheckStateRole or not index.isValid():
            return False
        if value == Qt.CheckState.Checked or value == Qt.CheckState.Checked.value:
            self.checked.add(index.row())
        else:
            self.checked.discard(index.row())
        self.dataChanged.emit(index, index, [role])
        return True

    def set_all(self, checked):
        self.checked = set(range(len(self.labels))) if checked else set()
        if self.labels:
            self.dataChanged.emit(self.index(0), self.index(len(self.labels) - 1), [Qt.ItemDataRole.CheckStateRole])


class LoopSelectionDialog(QDialog):
    def __init__(self, labels, positions, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select values for this run")
        self.resize(440, 420)
        layout = QVBoxLayout(self)
        search = QLineEdit()
        search.setPlaceholderText("Find a value or paired setting…")
        search.setAccessibleName("Find run values")
        layout.addWidget(search)
        self.model = _ValueListModel(labels, positions, self)
        proxy = QSortFilterProxyModel(self)
        proxy.setSourceModel(self.model)
        proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        search.textChanged.connect(proxy.setFilterFixedString)
        view = QListView()
        view.setAccessibleName("Values for this run")
        view.setModel(proxy)
        view.setUniformItemSizes(True)
        layout.addWidget(view)
        row = QHBoxLayout()
        for text, checked in (("Select all", True), ("Clear", False)):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, value=checked: self.model.set_all(value))
            row.addWidget(button)
        row.addStretch()
        self.count_label = QLabel()
        row.addWidget(self.count_label)
        layout.addLayout(row)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.model.dataChanged.connect(self._update_count)
        self._update_count()

    def _update_count(self, *_args):
        count = len(self.model.checked)
        self.count_label.setText(f"{count} selected")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(count > 0)

    def positions(self):
        return sorted(self.model.checked)
