"""Virtualized Qt Quick motion selection inside a native QDialog."""
from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtCore import Property, QUrl, Signal, Slot
from PySide6.QtQuickWidgets import QQuickWidget
from ui.quick_engine import QuickSurface
from PySide6.QtWidgets import QDialog, QVBoxLayout

from app.motion_sequence_model import MAX_SEQUENCE_ROWS, MotionSequenceModel


class QuickMotionSequencePreviewDialog(QDialog):
    """Public selection/result API matches MotionSequencePreviewDialog.

    QML receives a model reference; it never receives the complete row list.
    Exporting selected_rows/result_rows is an explicit deep-copy boundary.
    """

    selectionApplied = Signal()
    currentRowChanged = Signal()
    statusChanged = Signal()

    def __init__(self, rows, *, point_count=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Motion sweep sequence preview")
        self.resize(1000, 600)
        self.setMinimumSize(480, 360)
        self._selected_result = None
        self._selected_source_indices = []
        self._status_text = ""
        self.qml_warnings = []
        self.quick_widget = QuickSurface(self)
        self.quick_widget.setObjectName("QuickMotionSequenceWidget")
        self.quick_widget.setAccessibleName("Motion sweep sequence selection")
        self.quick_widget.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        # Keep the model alive until the Quick view has released its root.
        self.sequence_model = MotionSequenceModel(rows, point_count=point_count,
                                                  parent=self.quick_widget)
        self._current_row = 0 if self.sequence_model.count else -1
        if self.sequence_model.truncated:
            self._status_text = f"Sequence exceeds {MAX_SEQUENCE_ROWS:,} entries; preview capped at {MAX_SEQUENCE_ROWS:,}."
        self.sequence_model.selectionChanged.connect(self._clear_status)
        self.quick_widget.engine().warnings.connect(self._warnings)
        self.quick_widget.setInitialProperties({"sequenceModel": self.sequence_model, "bridge": self})
        self.quick_widget.setSource(QUrl.fromLocalFile(str(Path(__file__).resolve().parent / "qml" / "MotionSequence.qml")))
        if self.quick_widget.status() == QQuickWidget.Status.Error:
            raise RuntimeError("\n".join(error.toString() for error in self.quick_widget.errors()))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.quick_widget)

    currentRow = Property(int, lambda self: self._current_row, notify=currentRowChanged)
    statusText = Property(str, lambda self: self._status_text, notify=statusChanged)

    def _warnings(self, errors):
        self.qml_warnings.extend(error.toString() for error in errors)

    def _set_status(self, text):
        if text != self._status_text:
            self._status_text = text
            self.statusChanged.emit()

    def _clear_status(self):
        self._set_status("")

    @Slot(int)
    def selectRow(self, row):
        if 0 <= row < self.sequence_model.count and row != self._current_row:
            self._current_row = row
            self.currentRowChanged.emit()

    def selected_rows(self):
        return self.sequence_model.selected_rows()

    selected_sequence = selected_rows

    def selected_indices(self):
        return self.sequence_model.selected_indices()

    def selection_result(self):
        return {"rows": self.selected_rows(), "indices": self.selected_indices()}

    @Slot()
    def selectAll(self):
        self.select_all()

    def select_all(self):
        self._set_all(True)

    @Slot()
    def clearSelection(self):
        self._set_all(False)

    def _set_all(self, checked):
        self.sequence_model.set_all_checked(checked)
        self._clear_status()

    def reset_all(self):
        self.select_all()
        self._selected_result = None
        self._selected_source_indices = []

    @Slot(int, result=bool)
    def moveSelection(self, delta):
        return self.move_selected(delta)

    def move_selected(self, delta):
        target = self._current_row + int(delta)
        if not self.sequence_model.move_entry(self._current_row, target):
            return False
        self._current_row = target
        self.currentRowChanged.emit()
        return True

    def move_up(self):
        return self.move_selected(-1)

    def move_down(self):
        return self.move_selected(1)

    @Slot()
    def applySelection(self):
        self.apply_selection()

    def apply_selection(self):
        if not self.sequence_model.selectedCount:
            self._set_status("Select at least one sequence entry before applying.")
            return None
        rows = self.selected_rows()
        self._selected_result = rows
        self._selected_source_indices = [row["_source_index"] for row in rows]
        self.selectionApplied.emit()
        self.accept()
        return copy.deepcopy(rows)

    @Slot()
    def cancelSelection(self):
        self.reject()

    def result_rows(self):
        return copy.deepcopy(self._selected_result) if self._selected_result is not None else self.selected_rows()

    def result_indices(self):
        return list(self._selected_source_indices) if self._selected_result is not None else self.selected_indices()

    def set_theme(self, mode):
        if mode not in {"system", "light", "dark"}:
            raise ValueError("Theme must be system, light or dark")
        self.quick_widget.rootObject().setProperty("themeMode", mode)
