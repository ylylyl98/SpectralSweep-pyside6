"""Virtualized Quick table editors over the existing validated editor state.

The source table remains the compatibility boundary for planner/session code.
Only visible records are inspected; their fields fit in the same view without
horizontal scrolling or field paging. Sources may request a single record line.
Edits use the original cell control or
item delegate, retaining validation.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from PySide6.QtCore import (QAbstractTableModel, QEvent, QItemSelection, QItemSelectionModel,
                           QModelIndex, Property, Qt, QTimer, QUrl, Signal, Slot)
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtGui import QFont, QFontMetrics
from ui.quick_engine import QuickSurface
from PySide6.QtWidgets import (QAbstractButton, QCheckBox, QComboBox, QDoubleSpinBox,
                               QLineEdit, QSpinBox, QStyleOptionViewItem,
                               QTableWidget, QVBoxLayout, QWidget)
from shiboken6 import isValid


class TableEditorModel(QAbstractTableModel):
    changed = Signal()
    schemaChanged = Signal()
    cellChanged = Signal(int, int)
    rowSelectionChanged = Signal(int, bool)
    CellRole = int(Qt.ItemDataRole.UserRole) + 1

    def __init__(self, table, parent=None):
        super().__init__(parent)
        self.table = table
        self._single_line_rows = bool(table.property("quickSingleLineRows"))
        self._closed = False
        self._resetting = False
        self._error = ""
        self._drafts = {}
        self._generation = 0
        self._selection_anchor = -1
        self._selecting_rows = False
        self._columns = self._visible_columns()
        font = QFont("Segoe UI" if sys.platform == "win32" else "sans-serif")
        font.setPixelSize(12)
        self._font_metrics = QFontMetrics(font)
        self._widths = [self._column_width(column) for column in self._columns]
        self._titles = [self.headerData(column, Qt.Orientation.Horizontal) for column in range(len(self._columns))]
        self._cells = {}
        self._active_rows = set()
        self._dirty_cells = set()
        self._choice_dirty = set()
        self._choices = {}
        self._controls = {}
        self._control_connections = {}
        self._control_widgets = {}
        self._control_watches = {}
        self._schema_dirty = False
        self._active = True
        self._source = table.model()
        self._connections = []
        for before, after in (("rowsAboutToBeInserted", "rowsInserted"),
                              ("rowsAboutToBeRemoved", "rowsRemoved"),
                              ("columnsAboutToBeInserted", "columnsInserted"),
                              ("columnsAboutToBeRemoved", "columnsRemoved"),
                              ("modelAboutToBeReset", "modelReset"),
                              ("layoutAboutToBeChanged", "layoutChanged")):
            self._connect(getattr(self._source, before), self._begin_reset)
            self._connect(getattr(self._source, after), self._end_reset)
        self._connect(self._source.dataChanged, self._data_changed)
        self._connect(table.selectionModel().selectionChanged, self._selection_changed)
        self._connect(table.selectionModel().currentChanged, self._current_changed)
        self._connect(table.horizontalHeader().geometriesChanged, self.schedule)
        self._connect(table.horizontalHeader().sectionMoved, self.schedule)
        self._connect(table.horizontalHeader().sectionResized, self.schedule)
        self._connect(self._source.headerDataChanged, self.schedule)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush_dirty)
        table.installEventFilter(self)
        table.viewport().installEventFilter(self)

    def _connect(self, signal, callback):
        signal.connect(callback)
        self._connections.append((signal, callback))

    def _visible_columns(self):
        header = self.table.horizontalHeader()
        return [header.logicalIndex(i) for i in range(header.count())
                if not self.table.isColumnHidden(header.logicalIndex(i))]

    def roleNames(self):
        return {int(Qt.ItemDataRole.DisplayRole): b"display", self.CellRole: b"cell"}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() or self._closed else self.table.rowCount()

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() or self._closed else len(self._columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal and 0 <= section < len(self._columns):
            item = self.table.horizontalHeaderItem(self._columns[section])
            return item.text() if item is not None else str(section + 1)
        return super().headerData(section, orientation, role)

    @Slot(int, int, result="QVariantMap")
    def cellAt(self, row, column):
        key = row, column
        valid = not self._closed and 0 <= row < self.rowCount() and 0 <= column < self.columnCount()
        if valid and (key not in self._cells or row not in self._active_rows):
            self._cells[key] = self.data(self.index(row, column), self.CellRole)
        return self._cells.get(key) or dict(
            kind="text", value="", enabled=False, editable=False, selected=False,
            token="", label="", tooltip="", options=[], optionEnabled=[],
            currentIndex=-1, choiceEditable=False, editText="", checked=False, drafted=False)

    @Slot(int)
    def watchRow(self, row):
        if not self._closed and 0 <= row < self.rowCount():
            self._active_rows.add(row)

    @Slot(int)
    def releaseRow(self, row):
        self._active_rows.discard(row)
        for key in [key for key in self._cells if key[0] == row]:
            self._cells.pop(key, None)
            self._choices.pop(key, None)
            self._dirty_cells.discard(key)
            self._choice_dirty.discard(key)
            self._unbind_control(key)

    def _control(self, row, column):
        container = self.table.cellWidget(row, column)
        key = row, self._columns.index(column)
        cached = self._controls.get(key)
        if cached is not None and cached[0] is container and (cached[1] is None or isValid(cached[1])):
            return cached[1]
        self._unbind_control(key)
        if container is None:
            self._controls[key] = (None, None)
            return None
        types = (QCheckBox, QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit)
        control = container if isinstance(container, types) else next(
            (child for kind in types if (child := container.findChild(kind)) is not None), container)
        self._controls[key] = container, control
        self._choices.pop(key, None)
        self._bind_control(key, container, control)
        return control

    def _bind_control(self, key, container, control):
        connections = []
        widgets = {container, control}
        if not isinstance(control, (QCheckBox, QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit)):
            widgets.update(control.findChildren(QAbstractButton))
        for widget in widgets:
            if widget not in self._control_watches:
                widget.installEventFilter(self)
                self._control_watches[widget] = set()
            self._control_watches[widget].add(key)
            for name in ("valueChanged", "textChanged", "currentIndexChanged", "currentTextChanged", "toggled", "clicked"):
                signal = getattr(widget, name, None)
                if signal is not None:
                    callback = lambda *args, current=key: self._queue_cell(current)
                    signal.connect(callback)
                    connections.append((signal, callback))
        if isinstance(control, QComboBox):
            for signal in (control.model().dataChanged, control.model().rowsInserted,
                           control.model().rowsRemoved, control.model().modelReset):
                callback = lambda *args, current=key: self._queue_cell(current, choices=True)
                signal.connect(callback)
                connections.append((signal, callback))
        self._control_connections[key] = connections
        self._control_widgets[key] = widgets

    def _unbind_control(self, key):
        self._controls.pop(key, None)
        self._choices.pop(key, None)
        for signal, callback in self._control_connections.pop(key, []):
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                pass
        for widget in self._control_widgets.pop(key, ()):
            keys = self._control_watches.get(widget)
            if keys is None:
                continue
            keys.discard(key)
            if not keys:
                if isValid(widget):
                    widget.removeEventFilter(self)
                del self._control_watches[widget]

    def _queue_cell(self, key, *, choices=False):
        if self._closed:
            return
        self._dirty_cells.add(key)
        if choices:
            self._choice_dirty.add(key)
        if self._active:
            self._timer.start(0)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < self.rowCount() or not 0 <= index.column() < len(self._columns):
            return None
        row, column = index.row(), self._columns[index.column()]
        item = self.table.item(row, column)
        control = self._control(row, column)
        enabled = self.table.isEnabled() and (control is None or control.isEnabled())
        cell = dict(kind="text", value=item.text() if item else "", enabled=enabled,
                    token=f"{self._generation}:{row}:{index.column()}",
                    label=str(self.headerData(index.column(), Qt.Orientation.Horizontal)),
                    editable=bool(item and item.flags() & Qt.ItemFlag.ItemIsEditable),
                    selected=self.table.selectionModel().isRowSelected(row, QModelIndex()),
                    tooltip=item.toolTip() if item else "", options=[], optionEnabled=[], currentIndex=-1,
                    choiceEditable=False, editText="", checked=False, drafted=False)
        if isinstance(control, QCheckBox):
            cell.update(kind="check", checked=control.isChecked(), editable=True)
        elif isinstance(control, QComboBox):
            key = row, index.column()
            if key not in self._choices or key in self._choice_dirty:
                source = control.model()
                source_column = control.modelColumn()
                self._choices[key] = (
                    [control.itemText(i) for i in range(control.count())],
                    [bool(source.flags(source.index(i, source_column)) & Qt.ItemFlag.ItemIsEnabled) for i in range(control.count())])
                self._choice_dirty.discard(key)
            options, option_enabled = self._choices[key]
            cell.update(kind="choice", currentIndex=control.currentIndex(), editable=True,
                        choiceEditable=control.isEditable(), editText=control.currentText(),
                        options=options, optionEnabled=option_enabled)
        elif isinstance(control, (QSpinBox, QDoubleSpinBox)):
            cell.update(value=str(control.value()), editable=not control.isReadOnly())
        elif isinstance(control, QLineEdit):
            cell.update(value=control.text(), editable=not control.isReadOnly())
        elif control is not None:
            buttons = ([control] if isinstance(control, QAbstractButton) else control.findChildren(QAbstractButton))
            if buttons:
                cell.update(kind="actions", options=[button.text() for button in buttons],
                            optionEnabled=[button.isEnabled() for button in buttons])
            else:
                cell.update(value="Open editor", kind="unsupported")
        elif item is not None and item.data(Qt.ItemDataRole.CheckStateRole) is not None and item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            cell.update(kind="check", checked=item.checkState() == Qt.CheckState.Checked, editable=True)
        if control is not None and control.toolTip():
            cell["tooltip"] = control.toolTip()
        draft = self._drafts.get(cell["token"])
        if draft is not None:
            cell["value"] = str(draft[2])
            cell["drafted"] = True
            if cell["kind"] == "choice":
                cell["editText"] = str(draft[2])
        if self._single_line_rows and self._column_key(column) in {"When", "condition_label"}:
            # Long expressions and condition names remain available in full,
            # including a staged edit that has not reached the source table.
            cell["tooltip"] = "\n".join(part for part in (cell["value"], cell["tooltip"]) if part)
        return cell if role == self.CellRole else cell["value"] if role == Qt.ItemDataRole.DisplayRole else None

    error = Property(str, lambda self: self._error, notify=changed)
    rowTotal = Property(int, lambda self: self.rowCount(), notify=schemaChanged)
    columnWidths = Property("QVariantList", lambda self: self._widths, notify=schemaChanged)
    columnTitles = Property("QVariantList", lambda self: self._titles, notify=schemaChanged)
    columnKeys = Property("QVariantList", lambda self: [self._column_key(column) for column in self._columns], notify=schemaChanged)
    singleLineRows = Property(bool, lambda self: self._single_line_rows, constant=True)
    minimumColumnWidths = Property("QVariantList", lambda self: [self._minimum_column_width(column) for column in self._columns], notify=schemaChanged)
    displayColumnTitles = Property("QVariantList", lambda self: [self._display_column_title(self._column_key(column)) for column in self._columns] if self._single_line_rows else self._titles, notify=schemaChanged)

    def _column_key(self, column):
        header = self.table.horizontalHeaderItem(column)
        name = " ".join(header.text().split()) if header else ""
        # The Widgets table uses human-readable multiline headers. Keep its
        # public labels intact while applying the same sizing to either schema.
        name = {"Measure power": "MeasurePower", "Condition": "condition_label",
                "Repeat": "repeat", "Frames": "frames"}.get(name, name)
        if name.startswith(("Vbg ", "Vtg ", "Vbias ")):
            name = name.removesuffix(" (V)").replace(" ", "_")
        return name

    def _display_column_title(self, title):
        if not self._single_line_rows:
            return title
        if title.startswith(("Vbg_", "Vtg_", "Vbias_")):
            gate, endpoint = title.split("_", 1)
            return f"{gate}\n{endpoint} (V)"
        return {"MeasurePower": "Power", "condition_label": "Condition",
                "repeat": "Repeat", "frames": "Frames"}.get(title, title.replace("_", " "))

    def _minimum_column_width(self, column):
        name = self._column_key(column)
        return {"Run": 26, "When": 46, "MeasurePower": 26,
                "condition_label": 120, "repeat": 30, "frames": 36}.get(
                    name, 42 if name.startswith(("Vbg_", "Vtg_", "Vbias_")) else self._column_width(column))

    def _set_error(self, value):
        if value != self._error:
            self._error = value
            self.changed.emit()

    def _column_width(self, column):
        header = self.table.horizontalHeaderItem(column)
        name = header.text() if header else ""
        if self._single_line_rows:
            name = self._column_key(column)
            preferred = {"Run": 32, "When": 140, "MeasurePower": 54,
                         "condition_label": 220, "repeat": 46, "frames": 54}.get(name)
            if preferred is not None:
                return preferred
            if name.startswith(("Vbg_", "Vtg_", "Vbias_")):
                return 72
        # Size the schema, not every live cell: polling/telemetry must not scan
        # rows, enumerate choices, or continually change the wrapping geometry.
        # Scalar voltage/count fields need much less room than value sequences.
        label_width = self._font_metrics.horizontalAdvance(name) + 12
        preferred = {
            "Run": 36, "Enable": 50, "MeasurePower": 92, "Group": 50,
            "Parameter": 180, "Values": 140, "This run": 116,
            "When": 132, "condition_label": 108, "repeat": 50, "frames": 54,
            "Use": 36, "#": 30, "Input type": 140,
            "Vtg": 60, "Vbg": 60, "Vbias": 64, "Doping": 72, "E-field": 72,
        }.get(name)
        if preferred is None:
            preferred = 76 if name.startswith(("Vbg_", "Vtg_", "Vbias_")) else max(80, min(180, self.table.columnWidth(column)))
        return max(label_width, preferred)

    def eventFilter(self, watched, event):
        if self._closed:
            return False
        if watched in self._control_watches:
            if event.type() in (QEvent.Type.EnabledChange, QEvent.Type.ReadOnlyChange,
                                QEvent.Type.DynamicPropertyChange, QEvent.Type.ToolTipChange,
                                QEvent.Type.ChildAdded, QEvent.Type.ChildRemoved):
                for key in self._control_watches[watched]:
                    if event.type() in (QEvent.Type.ChildAdded, QEvent.Type.ChildRemoved):
                        self._controls.pop(key, None)
                    self._queue_cell(key)
            return False
        if event.type() == QEvent.Type.EnabledChange:
            self._dirty_cells.update(self._cells)
        elif event.type() in (QEvent.Type.ChildAdded, QEvent.Type.ChildRemoved):
            self._dirty_cells.update(self._cells)
            for key in list(self._controls):
                self._unbind_control(key)
        if event.type() in (QEvent.Type.EnabledChange, QEvent.Type.ChildAdded,
                            QEvent.Type.ChildRemoved, QEvent.Type.LayoutRequest):
            self.schedule()
        return False

    def schedule(self, *_args):
        self._schema_dirty = True
        if not self._closed and self._active:
            self._timer.start(0)

    def set_active(self, active):
        self._active = bool(active)
        if not active:
            self._timer.stop()

    def _begin_reset(self, *_args):
        if not self._closed and not self._resetting:
            if self._drafts:
                self._set_error("Table rows changed. Review the value before continuing.")
            self._drafts.clear()
            self._selection_anchor = -1
            self._generation += 1
            for key in list(self._controls):
                self._unbind_control(key)
            self._cells.clear()
            self._choices.clear()
            self._dirty_cells.clear()
            self._choice_dirty.clear()
            self._active_rows.clear()
            self._resetting = True
            self.beginResetModel()

    def _end_reset(self, *_args):
        if self._resetting:
            self._columns = self._visible_columns()
            self._widths = [self._column_width(column) for column in self._columns]
            self._titles = [self.headerData(column, Qt.Orientation.Horizontal) for column in range(len(self._columns))]
            self._resetting = False
            self.endResetModel()
            self.schemaChanged.emit()
            self.changed.emit()

    def _data_changed(self, first, last, *_args):
        if self._closed or self._resetting or not self._columns:
            return
        for key in self._cells:
            row, column = key
            if first.row() <= row <= last.row() and first.column() <= self._columns[column] <= last.column():
                self._dirty_cells.add(key)
        if self._dirty_cells and self._active:
            self._timer.start(0)

    def _selection_changed(self, selected, deselected):
        # Selection is row decoration, not cell data. Do not reread editors or
        # enumerate their choices just because a row was clicked.
        ranges = list(selected) + list(deselected)
        changed_rows = {row for row, _ in self._cells
                        if any(part.top() <= row <= part.bottom() for part in ranges)}
        for row in changed_rows:
            is_selected = self.rowSelected(row)
            for column in range(len(self._columns)):
                cell = self._cells.get((row, column))
                if cell is not None:
                    cell["selected"] = is_selected
            self.rowSelectionChanged.emit(row, is_selected)

    def _current_changed(self, current, _previous):
        # Backend reorder/restore operations may move the current row. A later
        # Shift selection must start there rather than at a stale row number.
        if not self._selecting_rows:
            self._selection_anchor = current.row()

    @Slot(int, result=bool)
    def rowSelected(self, row):
        return (not self._closed and 0 <= row < self.rowCount()
                and self.table.selectionModel().isRowSelected(row, QModelIndex()))

    def refresh(self):
        """Reconcile rendered records, including blocked-signal changes."""
        if self._closed or self._resetting:
            return
        for row in {key[0] for key in self._cells if key[0] not in self._active_rows}:
            self.releaseRow(row)
        self._schema_dirty = True
        self._dirty_cells.update(self._cells)
        self._choice_dirty.update(self._cells)
        self._flush_dirty()

    def _flush_dirty(self):
        if self._closed or self._resetting:
            return
        if self._schema_dirty:
            self._schema_dirty = False
            self._refresh_schema()
        dirty = self._dirty_cells
        self._dirty_cells = set()
        for key in dirty:
            if key not in self._cells or key[0] not in self._active_rows:
                continue
            previous = self._cells[key]
            current = self.data(self.index(*key), self.CellRole)
            if current and previous and current["selected"] != previous["selected"]:
                previous["selected"] = current["selected"]
                self.rowSelectionChanged.emit(key[0], current["selected"])
            if current != previous:
                self._cells[key] = current
                self.cellChanged.emit(*key)
                index = self.index(*key)
                self.dataChanged.emit(index, index, [self.CellRole, int(Qt.ItemDataRole.DisplayRole)])

    def _refresh_schema(self):
        columns = self._visible_columns()
        if columns != self._columns:
            self._begin_reset()
            self._end_reset()
            return
        widths = [self._column_width(column) for column in self._columns]
        titles = [self.headerData(column, Qt.Orientation.Horizontal) for column in range(len(self._columns))]
        if widths != self._widths or titles != self._titles:
            changed_titles = {column for column, title in enumerate(titles) if column >= len(self._titles) or title != self._titles[column]}
            self._widths, self._titles = widths, titles
            self._dirty_cells.update(key for key in self._cells if key[1] in changed_titles)
            self.schemaChanged.emit()

    @Slot(int, bool)
    def selectRow(self, row, extend=False):
        self.selectRowWithModifiers(row, Qt.KeyboardModifier.ControlModifier.value if extend else 0)

    @Slot(int, int)
    def selectRowWithModifiers(self, row, modifiers=0):
        if self._closed or not 0 <= row < self.rowCount():
            return
        modifiers = Qt.KeyboardModifier(modifiers)
        control = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        flags = QItemSelectionModel.SelectionFlag.Rows
        if shift:
            anchor = self._selection_anchor
            if not 0 <= anchor < self.rowCount():
                anchor = self.table.currentRow()
            if not 0 <= anchor < self.rowCount():
                anchor = row
            self._selection_anchor = anchor
            selection = QItemSelection(self._source.index(min(anchor, row), 0),
                                       self._source.index(max(anchor, row), self._source.columnCount() - 1))
            flags |= (QItemSelectionModel.SelectionFlag.Select if control
                      else QItemSelectionModel.SelectionFlag.ClearAndSelect)
        else:
            self._selection_anchor = row
            selection = self._source.index(row, 0)
            flags |= (QItemSelectionModel.SelectionFlag.Toggle if control
                      else QItemSelectionModel.SelectionFlag.ClearAndSelect)
        self._selecting_rows = True
        try:
            self.table.selectionModel().select(selection, flags)
            self.table.setCurrentCell(row, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
        finally:
            self._selecting_rows = False

    @Slot(int)
    def focusRow(self, row):
        """Keyboard focus within an already selected row keeps the selection."""
        if not self.rowSelected(row):
            self.selectRow(row)

    @Slot(int, int, result=int)
    def selectionKey(self, key, modifiers):
        """Handle row navigation after a cell has declined the key event."""
        if self._closed or not self.rowCount():
            return -1
        current = self.table.currentRow()
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            row = max(0, min(self.rowCount() - 1,
                            current + (-1 if key == Qt.Key.Key_Up else 1))) if current >= 0 else 0
        elif key in (Qt.Key.Key_Home, Qt.Key.Key_End):
            row = 0 if key == Qt.Key.Key_Home else self.rowCount() - 1
        elif key == Qt.Key.Key_A and Qt.KeyboardModifier(modifiers) & Qt.KeyboardModifier.ControlModifier:
            self.table.selectAll()
            return max(0, current)
        else:
            return -1
        if (Qt.KeyboardModifier(modifiers) & Qt.KeyboardModifier.ControlModifier
                and not Qt.KeyboardModifier(modifiers) & Qt.KeyboardModifier.ShiftModifier):
            self.table.setCurrentCell(row, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
        else:
            self.selectRowWithModifiers(row, modifiers)
        return row

    @Slot(int, int)
    def prepareChoice(self, row, visible_column):
        if self._closed or not 0 <= row < self.rowCount() or not 0 <= visible_column < len(self._columns):
            return
        control = self._control(row, self._columns[visible_column])
        from ui.loop_run_selection import RunSelectionCombo
        if isinstance(control, RunSelectionCombo):
            control.prepare_choices()
            self._queue_cell((row, visible_column), choices=True)
            self._flush_dirty()

    @Slot(str, str)
    def stageEdit(self, token, text):
        if self._closed:
            return
        try:
            generation, row, column = map(int, token.split(":"))
        except (ValueError, AttributeError):
            return
        if generation == self._generation and 0 <= row < self.rowCount() and 0 <= column < self.columnCount():
            self._drafts[token] = (row, column, text)
            self._set_error("")
            self._queue_cell((row, column))

    @Slot(str, result=bool)
    def commitCell(self, token):
        if self._closed:
            return False
        draft = self._drafts.get(token)
        if draft is None:
            return True
        if not self.editCell(*draft):
            return False
        self._drafts.pop(token, None)
        self._queue_cell((draft[0], draft[1]))
        self._flush_dirty()
        return True

    @Slot(result=bool)
    def commitEdits(self):
        return not self._closed and all(self.commitCell(token) for token in list(self._drafts))

    @Slot()
    def discardEdits(self):
        self._drafts.clear()
        self._set_error("")
        if not self._closed:
            self.refresh()

    @Slot(int, int, "QVariant", result=bool)
    def editCell(self, row, visible_column, value):
        if self._closed or not self.table.isEnabled() or not 0 <= row < self.rowCount() or not 0 <= visible_column < len(self._columns):
            return False
        column = self._columns[visible_column]
        control = self._control(row, column)
        if control is not None and not control.isEnabled():
            return False
        if isinstance(control, (QSpinBox, QDoubleSpinBox, QLineEdit)) and control.isReadOnly():
            return False
        self._set_error("")
        editor = None
        try:
            if isinstance(control, QCheckBox):
                if control.isChecked() != bool(value):
                    control.click()
            elif isinstance(control, QComboBox):
                if control.isEditable() and isinstance(value, str):
                    validator = control.lineEdit().validator()
                    if validator and validator.validate(value, len(value))[0] != validator.State.Acceptable:
                        raise ValueError("Enter a valid value")
                    control.setEditText(value)
                    control.lineEdit().textEdited.emit(value)
                    control.lineEdit().editingFinished.emit()
                    self.refresh()
                    return True
                selected = int(value)
                if not 0 <= selected < control.count() or not control.model().flags(control.model().index(selected, control.modelColumn())) & Qt.ItemFlag.ItemIsEnabled:
                    raise ValueError("This choice is unavailable")
                control.setCurrentIndex(selected)
                control.activated.emit(selected)
            elif isinstance(control, (QSpinBox, QDoubleSpinBox)):
                self._set_number(control, value)
                control.editingFinished.emit()
            elif isinstance(control, QLineEdit):
                if control.isReadOnly():
                    return False
                text = str(value)
                if control.validator() and control.validator().validate(text, len(text))[0] != control.validator().State.Acceptable:
                    raise ValueError("Enter a valid value")
                control.setText(text)
                control.textEdited.emit(text)
                control.editingFinished.emit()
            elif control is not None:
                buttons = [control] if isinstance(control, QAbstractButton) else control.findChildren(QAbstractButton)
                selected = int(value)
                if not 0 <= selected < len(buttons) or not buttons[selected].isEnabled():
                    return False
                buttons[selected].click()
            else:
                item = self.table.item(row, column)
                if item is None:
                    return False
                if item.data(Qt.ItemDataRole.CheckStateRole) is not None and item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                    item.setCheckState(Qt.CheckState.Checked if bool(value) else Qt.CheckState.Unchecked)
                elif item.flags() & Qt.ItemFlag.ItemIsEditable:
                    index = self._source.index(row, column)
                    delegate = self.table.itemDelegateForColumn(column) or self.table.itemDelegate()
                    editor = delegate.createEditor(self.table, QStyleOptionViewItem(), index)
                    if editor is None:
                        raise ValueError("Use the dedicated editor for this value")
                    delegate.setEditorData(editor, index)
                    if isinstance(editor, (QSpinBox, QDoubleSpinBox)):
                        if str(value).strip() == "" and isinstance(editor, QDoubleSpinBox):
                            editor.lineEdit().clear()
                        else:
                            self._set_number(editor, value)
                    elif isinstance(editor, QLineEdit):
                        editor.setText(str(value))
                        if not editor.hasAcceptableInput():
                            raise ValueError("Enter a valid value")
                    else:
                        raise ValueError("Use the dedicated editor for this value")
                    delegate.setModelData(editor, self._source, index)
                else:
                    return False
            self._queue_cell((row, visible_column))
            self._flush_dirty()
            return True
        except (ValueError, TypeError, OverflowError) as error:
            self._set_error(f"Row {row + 1}: {error}")
            return False
        finally:
            if editor is not None:
                editor.deleteLater()

    @staticmethod
    def _set_number(editor, value):
        number = float(value)
        if not math.isfinite(number) or not editor.minimum() <= number <= editor.maximum():
            raise ValueError(f"Enter a value from {editor.minimum()} to {editor.maximum()}")
        if isinstance(editor, QSpinBox) and not number.is_integer():
            raise ValueError("Enter a whole number")
        editor.setValue(int(number) if isinstance(editor, QSpinBox) else number)

    def shutdown(self):
        self._closed = True
        self._drafts.clear()
        self._timer.stop()
        self.table.removeEventFilter(self)
        self.table.viewport().removeEventFilter(self)
        for key in list(self._controls):
            self._unbind_control(key)
        self._cells.clear()
        for signal, callback in self._connections:
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                pass
        self._connections.clear()


class QuickTableEditor(QWidget):
    def __init__(self, table, parent=None):
        super().__init__(parent)
        self.setObjectName("QuickTable_" + table.objectName())
        self.source_table = table
        self.qml_warnings = []
        self.quick_widget = QuickSurface(self, match_host_background=True)
        self.model = TableEditorModel(table, self.quick_widget)
        self.quick_widget.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self.quick_widget.engine().warnings.connect(self._record_qml_warnings)
        self.quick_widget.setInitialProperties({"tableModel": self.model})
        self.quick_widget.setSource(QUrl.fromLocalFile(str(Path(__file__).with_name("qml") / "TableEditor.qml")))
        if self.quick_widget.status() == QQuickWidget.Status.Error:
            raise RuntimeError("\n".join(e.toString() for e in self.quick_widget.errors()))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.quick_widget)
        self.setMinimumWidth(240)
        self.setSizePolicy(table.sizePolicy())
        self._height_timer = QTimer(self)
        self._height_timer.setSingleShot(True)
        self._height_timer.timeout.connect(self._fit_height)
        self.model.changed.connect(self._schedule_height)
        self.quick_widget.rootObject().preferredHeightChanged.connect(self._schedule_height)
        self._fit_height()
        self._poll = QTimer(self)
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self.model.refresh)
        self.model.set_active(False)
        table.installEventFilter(self)

    def _schedule_height(self):
        if not self.model._closed:
            self._height_timer.start(0)

    def _record_qml_warnings(self, errors):
        self.qml_warnings.extend(error.toString() for error in errors)

    def _fit_height(self):
        # Most recipes stay compact. The main batch editor can instead use
        # its parent's spare height while continuing to virtualize long lists.
        root = self.quick_widget.rootObject()
        if root is None or self.model._closed:
            return
        height = max(60, min(420, math.ceil(root.property("preferredHeight"))))
        self.setMinimumHeight(min(height, 220))
        self.setMaximumHeight(16777215 if self.source_table.property("quickFillAvailableHeight") else height)

    def eventFilter(self, watched, event):
        if watched is self.source_table and event.type() in (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.ShowToParent, QEvent.Type.HideToParent):
            self.setVisible(not self.source_table.isHidden())
        return False

    def showEvent(self, event):
        super().showEvent(event)
        if not self.model._closed:
            self.model.set_active(True)
            self.model.refresh()
            self._poll.start()

    def hideEvent(self, event):
        self._poll.stop()
        self.model.set_active(False)
        super().hideEvent(event)

    def shutdown(self):
        self._poll.stop()
        self._height_timer.stop()
        if not self.model._closed:
            self.quick_widget.engine().warnings.disconnect(self._record_qml_warnings)
        self.source_table.removeEventFilter(self)
        self.model.shutdown()
        self.quick_widget.setSource(QUrl())


def install_table_editors(panel):
    """Replace concrete tables while retaining their operation/session owners."""
    installed = getattr(panel, "_quick_table_editors", None)
    if installed is not None:
        return installed
    installed = []
    for table in panel.findChildren(QTableWidget):
        if getattr(table, "_quick_editor", None) is not None:
            continue
        parent = table.parentWidget()
        layout = parent.layout() if parent is not None else None
        if layout is None or layout.indexOf(table) < 0:
            continue
        hidden = table.isHidden()
        view = QuickTableEditor(table, parent)
        item = layout.replaceWidget(table, view)
        if item is None:
            view.shutdown()
            view.deleteLater()
            continue
        # Retain the source's own visible flag so dynamically disclosed tables
        # (calculator results, diagnostics) reveal the new view, not the old
        # table. The hidden parking parent prevents legacy rendering.
        parking = QWidget(parent)
        parking.setObjectName("QuickTableStateHost")
        parking.hide()
        table.setParent(parking)
        table.setVisible(not hidden)
        view._parking = parking
        view.setVisible(not hidden)
        table._quick_editor = view
        installed.append(view)
    panel._quick_table_editors = installed
    return installed
