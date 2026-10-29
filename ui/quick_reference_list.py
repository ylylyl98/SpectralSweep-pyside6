"""Qt Quick reference-spectrum rows using Spectrum's explicit reference schema.

The original list remains the selection/action owner. Each row command rechecks
that the reference and its source controls still exist before invoking the same
rename, visibility and color paths used by Spectrum.
"""
from pathlib import Path

from PySide6.QtCore import QObject, Property, Signal, Slot, QTimer, QUrl, Qt
from PySide6.QtQuickWidgets import QQuickWidget
from ui.quick_engine import QuickSurface
from PySide6.QtWidgets import QStackedWidget, QSizePolicy
from shiboken6 import isValid


class ReferenceRow(QObject):
    changed = Signal()

    def __init__(self, reference_id, model):
        super().__init__(model)
        self.model, self._id = model, reference_id
        self._state = {}
        self.refresh()

    referenceId = Property(str, lambda s: s._id, constant=True)
    name = Property(str, lambda s: s._state.get("name", ""), notify=changed)
    color = Property(str, lambda s: s._state.get("color", "#0084c4"), notify=changed)
    checked = Property(bool, lambda s: s._state.get("checked", False), notify=changed)
    selected = Property(bool, lambda s: s._state.get("selected", False), notify=changed)
    enabled = Property(bool, lambda s: s._state.get("enabled", False), notify=changed)
    visible = Property(bool, lambda s: s._state.get("visible", False), notify=changed)
    status = Property(str, lambda s: s._state.get("status", ""), notify=changed)
    setup = Property(str, lambda s: s._state.get("setup", ""), notify=changed)

    def _resolve(self):
        if self.model._closed:
            return None, None
        panel = self.model.panel
        ref = next((ref for ref in panel._references if str(ref["id"]) == self._id), None)
        table = panel._reference_list
        item = next((table.item(i) for i in range(table.count())
                     if str(table.item(i).data(Qt.ItemDataRole.UserRole)) == self._id), None)
        return ref, item

    def refresh(self):
        ref, item = self._resolve()
        if ref is None or item is None:
            state = dict(enabled=False, visible=False)
        else:
            source = ref.get("_row_source")
            status = ref.get("_row_status")
            state = dict(name=self.model._drafts.get(self._id, str(ref["name"])), color=str(ref["color"]), checked=bool(ref["visible"]),
                         selected=item.isSelected(), visible=not item.isHidden(),
                         enabled=self.model.panel._reference_list.isEnabled(),
                         setup=source.text() if source is not None and isValid(source) else str(ref.get("setup_key", "")),
                         status=status.text() if status is not None and isValid(status) else "")
        if any(self._state.get(key) != value for key, value in state.items()):
            self._state.update(state)
            self.changed.emit()

    @Slot(result=bool)
    def select(self):
        ref, item = self._resolve()
        if ref is None or item is None or item.isHidden() or not self.model.panel._reference_list.isEnabled():
            return False
        self.model.panel._reference_list.setCurrentItem(item)
        self.model.refresh()
        return True

    def _control(self, key):
        ref, item = self._resolve()
        if ref is None or item is None or item.isHidden():
            return None
        control = ref.get(key)
        return control if control is not None and isValid(control) and control.isEnabled() else None

    @Slot(str, result=bool)
    def rename(self, text):
        if not text.strip():
            return False
        control = self._control("_row_name")
        if control is None or control.isReadOnly():
            return False
        self.select()
        control.setText(text)
        control.textEdited.emit(text)
        control.editingFinished.emit()
        self.model._drafts.pop(self._id, None)
        self.model.refresh()
        return True

    @Slot(str)
    def stageName(self, text):
        if self._control("_row_name") is not None:
            self.model._drafts[self._id] = text
            self.refresh()

    @Slot(result=bool)
    def toggle(self):
        control = self._control("_row_checkbox")
        if control is None:
            return False
        self.select()
        control.click()
        self.model.refresh()
        return True

    @Slot(result=bool)
    def chooseColor(self):
        control = self._control("_row_swatch")
        if control is None:
            return False
        self.select()
        control.click()
        self.model.refresh()
        return True


class ReferenceListModel(QObject):
    changed = Signal()
    renameRequested = Signal(str)

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel, self._closed = panel, False
        self._rows, self._cache, self._drafts = [], {}, {}
        self._connections = []
        source = panel._reference_list
        for signal in (source.itemSelectionChanged, source.itemChanged,
                       source.model().rowsInserted, source.model().rowsRemoved, source.model().modelReset):
            callback = lambda *args: QTimer.singleShot(0, self.refresh)
            signal.connect(callback)
            self._connections.append((signal, callback))
        panel._rename_reference_btn.clicked.connect(self.request_rename)
        self._connections.append((panel._rename_reference_btn.clicked, self.request_rename))
        self.refresh()

    rows = Property("QVariantList", lambda s: s._rows, notify=changed)

    def refresh(self):
        if self._closed:
            return
        live_ids = {str(ref["id"]) for ref in self.panel._references}
        self._drafts = {key: value for key, value in self._drafts.items() if key in live_ids}
        rows = []
        for ref in self.panel._references:
            key = str(ref["id"])
            row = self._cache.get(key)
            if row is None:
                row = ReferenceRow(key, self)
                self._cache[key] = row
            row.refresh()
            rows.append(row)
        for key, row in self._cache.items():
            if row not in rows:
                row.refresh()
        if rows != self._rows:
            self._rows = rows
            self.changed.emit()

    def request_rename(self, *_):
        item = self.panel._reference_list.currentItem()
        if item is not None and not self._closed:
            self.renameRequested.emit(str(item.data(Qt.ItemDataRole.UserRole)))

    def commitEdits(self):
        if self._closed:
            return False
        for key, text in list(self._drafts.items()):
            row = self._cache.get(key)
            if not text.strip() or row is None or not row.rename(text):
                return False
        return True

    def discardEdits(self):
        self._drafts.clear()
        self.refresh()

    def shutdown(self):
        self._closed = True
        self._drafts.clear()
        for signal, callback in self._connections:
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                pass
        self._connections.clear()


class QuickReferenceList(QuickSurface):
    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.model = ReferenceListModel(panel, self)
        self.setObjectName("QuickSpectrumReferences")
        self.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self.setMinimumHeight(80)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setInitialProperties({"bridge": self.model})
        self.setSource(QUrl.fromLocalFile(str(Path(__file__).with_name("qml") / "ReferenceList.qml")))
        if self.status() == QQuickWidget.Status.Error:
            raise RuntimeError("\n".join(error.toString() for error in self.errors()))
        self._poll = QTimer(self)
        self._poll.setInterval(150)
        self._poll.timeout.connect(self.model.refresh)

    def showEvent(self, event):
        super().showEvent(event)
        if not self.model._closed:
            self.model.refresh()
            self._poll.start()

    def hideEvent(self, event):
        self._poll.stop()
        super().hideEvent(event)

    def shutdown(self):
        self._poll.stop()
        self.model.shutdown()
        self.setSource(QUrl())

    def commitEdits(self):
        return self.model.commitEdits()

    def discardEdits(self):
        return self.model.discardEdits()


def install_reference_list(panel):
    existing = getattr(panel, "_quick_reference_list", None)
    if existing is not None:
        return existing
    source = panel._reference_list
    stack = source.parentWidget()
    if not isinstance(stack, QStackedWidget):
        raise RuntimeError("Spectrum reference list must retain its audited stack owner")
    view = QuickReferenceList(panel, stack)
    source._quick_reference_editor = view
    stack.addWidget(view)
    def route(index):
        if not view.model._closed and stack.widget(index) is source:
            stack.setCurrentWidget(view)
    stack.currentChanged.connect(route)
    view.model._connections.append((stack.currentChanged, route))
    if stack.currentWidget() is source:
        stack.setCurrentWidget(view)
    panel._quick_reference_list = view
    return view
