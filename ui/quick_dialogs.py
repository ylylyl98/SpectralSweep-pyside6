"""Owned, explicitly admitted application dialogs rendered with Qt Quick.

This is a scoped presentation installer, not a global dialog mirror. It never
opens or accepts a dialog and never invokes a domain/hardware command itself.
Native standard dialogs and existing Quick dialogs retain their own presenters.
"""
from pathlib import Path

from PySide6.QtCore import (QAbstractListModel, QEvent, QModelIndex, QObject,
                           Property, Signal, Slot, QTimer, QUrl, Qt)
from PySide6.QtQuickWidgets import QQuickWidget
from ui.quick_engine import QuickSurface
from PySide6.QtWidgets import (QApplication, QColorDialog, QDialog, QFileDialog,
                               QFontDialog, QInputDialog, QListView, QMessageBox,
                               QVBoxLayout, QWidget)
from shiboken6 import isValid

from ui.quick_parameter_forms import install_parameter_forms
from ui.quick_table_editor import install_table_editors


ADMITTED_DIALOGS = {
    ("ui.setup_sequence_editor", "SetupSequenceDialog"),
    ("ui.loop_run_selection", "LoopSelectionDialog"),
    ("ui.motion_sequence_preview", "MotionSequencePreviewDialog"),
    ("ui.wavelength_calibration_dialog", "WavelengthCalibrationDialog"),
    ("ui.broad_calibration_dialog", "BroadCalibrationDialog"),
    ("ui.mcd2100_panel", "_RampTablesDialog"),
}
ADMITTED_PLAIN_TITLES = {"Set imaging reference", "Code update status", "Full measurement sequence"}
NATIVE_DIALOGS = (QFileDialog, QColorDialog, QFontDialog, QInputDialog, QMessageBox)


class DialogValueModel(QAbstractListModel):
    """Virtualized typed roles over the original run-selection proxy model."""
    TextRole, CheckedRole, EnabledRole = Qt.ItemDataRole.UserRole + 1, Qt.ItemDataRole.UserRole + 2, Qt.ItemDataRole.UserRole + 3

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view, self.source, self._closed = view, view.model(), False
        self._connections = []
        for before, after in ((self.source.modelAboutToBeReset, self.source.modelReset),
                              (self.source.rowsAboutToBeInserted, self.source.rowsInserted),
                              (self.source.rowsAboutToBeRemoved, self.source.rowsRemoved),
                              (self.source.layoutAboutToBeChanged, self.source.layoutChanged)):
            self._connect(before, lambda *a: self.beginResetModel() if not self._closed else None)
            self._connect(after, lambda *a: self.endResetModel() if not self._closed else None)
        self._connect(self.source.dataChanged, self._data_changed)

    def _connect(self, signal, callback):
        signal.connect(callback)
        self._connections.append((signal, callback))

    def roleNames(self):
        return {self.TextRole: b"label", self.CheckedRole: b"checked", self.EnabledRole: b"available"}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() or self._closed else self.source.rowCount()

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if self._closed or not index.isValid() or not 0 <= index.row() < self.source.rowCount():
            return None
        source = self.source.index(index.row(), 0)
        if role in (self.TextRole, Qt.ItemDataRole.DisplayRole):
            return str(self.source.data(source, Qt.ItemDataRole.DisplayRole) or "")
        if role == self.CheckedRole:
            checked = self.source.data(source, Qt.ItemDataRole.CheckStateRole)
            return checked in (Qt.CheckState.Checked, Qt.CheckState.Checked.value)
        if role == self.EnabledRole:
            return bool(self.source.flags(source) & Qt.ItemFlag.ItemIsEnabled)

    def _data_changed(self, first, last, *args):
        if not self._closed:
            self.dataChanged.emit(self.index(first.row()), self.index(last.row()), [])

    @Slot(int, result=bool)
    def toggle(self, row):
        if self._closed or not self.view.isEnabled() or not 0 <= row < self.rowCount():
            return False
        index = self.source.index(row, 0)
        flags = self.source.flags(index)
        if not flags & Qt.ItemFlag.ItemIsEnabled or not flags & Qt.ItemFlag.ItemIsUserCheckable:
            return False
        checked = self.source.data(index, Qt.ItemDataRole.CheckStateRole)
        value = Qt.CheckState.Unchecked if checked in (Qt.CheckState.Checked, 2) else Qt.CheckState.Checked
        self.view.setCurrentIndex(index)
        return bool(self.source.setData(index, value, Qt.ItemDataRole.CheckStateRole))

    def shutdown(self):
        self._closed = True
        for signal, callback in self._connections:
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                pass
        self._connections.clear()


class _MotionBridge(QObject):
    changed = Signal()

    def __init__(self, dialog, parent=None):
        super().__init__(parent)
        self.dialog, self._closed = dialog, False
        dialog._table.selectionModel().currentChanged.connect(self.changed)
        dialog.sequence_model.selectionChanged.connect(self.changed)

    currentRow = Property(int, lambda s: s.dialog._table.currentIndex().row(), notify=changed)
    statusText = Property(str, lambda s: s.dialog._status.text(), notify=changed)

    @Slot(int)
    def selectRow(self, row):
        if not self._closed:
            self.dialog._table.selectRow(row)
            self.changed.emit()

    @Slot(int, result=bool)
    def moveSelection(self, delta):
        if self._closed:
            return False
        result = self.dialog.move_selected(delta)
        self.changed.emit()
        return bool(result)

    @Slot()
    def selectAll(self):
        if not self._closed:
            self.dialog.select_all()

    @Slot()
    def clearSelection(self):
        if not self._closed:
            self.dialog._set_all(False)

    @Slot()
    def applySelection(self):
        if not self._closed:
            self.dialog.apply_selection()
            self.changed.emit()

    @Slot()
    def cancelSelection(self):
        if not self._closed:
            self.dialog.reject()

    def shutdown(self):
        self._closed = True


class DialogMigration(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window, self._closed = window, False
        self._entries, self._installing, self._pending = {}, set(), set()
        QApplication.instance().installEventFilter(self)
        window.destroyed.connect(self.shutdown)

    def _owned(self, dialog):
        parent = dialog.parentWidget()
        while parent is not None:
            if parent is self.window:
                return True
            parent = parent.parentWidget()
        return False

    def _admitted(self, dialog):
        if isinstance(dialog, NATIVE_DIALOGS) or not dialog.isWindow() or not self._owned(dialog):
            return False
        identity = type(dialog).__module__, type(dialog).__name__
        return identity in ADMITTED_DIALOGS or type(dialog) is QDialog and dialog.windowTitle() in ADMITTED_PLAIN_TITLES

    def eventFilter(self, watched, event):
        if self._closed or not isinstance(watched, QWidget):
            return False
        dialog = watched if isinstance(watched, QDialog) else watched.window()
        if not isinstance(dialog, QDialog) or not self._admitted(dialog):
            return False
        if event.type() == QEvent.Type.Show and watched is dialog:
            entry = self._entries.get(id(dialog))
            if entry and entry["forms"]:
                for view in entry["forms"]["forms"]:
                    view.adapter._suppress_commits = False
            # QWidget is traversing its layouts during Show. Transferring a
            # layout from inside that traversal can invalidate a C++ iterator;
            # the queued install runs before the dialog's first settled frame.
            self._schedule(dialog)
        elif event.type() in (QEvent.Type.Close, QEvent.Type.Hide) and watched is dialog:
            self._discard(id(dialog), suppress=True)
        elif event.type() in (QEvent.Type.Show, QEvent.Type.ChildAdded) and dialog.isVisible():
            self._schedule(dialog)
        return False

    def _schedule(self, dialog):
        key = id(dialog)
        if key not in self._pending and key not in self._installing:
            self._pending.add(key)
            QTimer.singleShot(0, lambda: self._extend(dialog, key))

    def _extend(self, dialog, key):
        self._pending.discard(key)
        if not self._closed and isValid(dialog) and dialog.isVisible():
            self.migrate(dialog)

    def _surface(self, parent, model, filename, properties=None):
        view = QuickSurface(parent)
        view.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        view.setInitialProperties(properties or {"bridge": model})
        view.setSource(QUrl.fromLocalFile(str(Path(__file__).with_name("qml") / filename)))
        if view.status() == QQuickWidget.Status.Error:
            raise RuntimeError("\n".join(error.toString() for error in view.errors()))
        return view

    def migrate(self, dialog):
        """Explicit factory entry for an admitted owned dialog; safe to repeat."""
        if self._closed or not self._admitted(dialog) or id(dialog) in self._installing:
            return None
        key = id(dialog)
        self._installing.add(key)
        try:
            entry = self._entries.get(key)
            if entry is None:
                entry = dict(dialog=dialog, forms=None, tables=[], extras=[], models=[])
                self._entries[key] = entry
                dialog.finished.connect(lambda result, current=key: self._discard(current))
                dialog.destroyed.connect(lambda obj=None, current=key: self._release(current))
            if type(dialog).__module__ == "ui.motion_sequence_preview":
                if not entry["extras"]:
                    bridge = _MotionBridge(dialog, self)
                    view = self._surface(dialog, bridge, "MotionSequence.qml",
                                         {"bridge": bridge, "sequenceModel": dialog.sequence_model})
                    source_layout = dialog.layout()
                    host = QWidget(dialog)
                    host.hide()
                    host.setLayout(source_layout)
                    layout = QVBoxLayout(dialog)
                    layout.setContentsMargins(0, 0, 0, 0)
                    layout.addWidget(view)
                    entry["extras"].append(view)
                    entry["models"].append(bridge)
                return entry
            # Retain concrete source table delegates and their domain validators.
            entry["tables"] = install_table_editors(dialog)
            if type(dialog).__module__ == "ui.loop_run_selection" and not entry["extras"]:
                for source in dialog.findChildren(QListView):
                    if source.model() is None:
                        continue
                    model = DialogValueModel(source, self)
                    view = self._surface(dialog, model, "DialogValueList.qml")
                    owner = source.parentWidget()
                    layout = owner.layout()
                    if layout is not None and layout.replaceWidget(source, view) is not None:
                        source.hide()
                        entry["extras"].append(view)
                        entry["models"].append(model)
            entry["forms"] = install_parameter_forms(dialog, "calibration", refresh=entry["forms"] is not None)
            for view in entry["forms"]["forms"]:
                view.adapter.cancel_page = lambda current=key: self._discard(current, suppress=True)
            dialog.setProperty("quickDialogMigrated", True)
            return entry
        finally:
            self._installing.discard(key)

    def _discard(self, key, suppress=False):
        entry = self._entries.get(key)
        if entry:
            for view in entry["tables"]:
                if isValid(view) and hasattr(view.model, "discardEdits"):
                    view.model.discardEdits()
        if entry and entry["forms"]:
            for view in entry["forms"]["forms"]:
                if isValid(view):
                    view.adapter._drafts.clear()
                    view.adapter._suppress_commits = suppress or not entry["dialog"].isVisible()
                    for field in view.adapter._fields:
                        field.publish(invalid=False, error="")
                    view.adapter.refresh()

    def _release(self, key):
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        for model in entry["models"]:
            model.shutdown()
        for view in entry["extras"]:
            if isValid(view):
                view.setSource(QUrl())
        for view in entry["tables"]:
            if isValid(view):
                view.shutdown()
        if entry["forms"]:
            for view in entry["forms"]["forms"]:
                if isValid(view):
                    view.shutdown()

    def shutdown(self, *_):
        if self._closed:
            return
        self._closed = True
        QApplication.instance().removeEventFilter(self)
        for key in list(self._entries):
            self._release(key)


def install_dialog_migration(window):
    manager = getattr(window, "_quick_dialog_migration", None)
    if manager is None:
        manager = DialogMigration(window)
        window._quick_dialog_migration = manager
    return manager
