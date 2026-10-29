"""Typed presentation state for the transitional Qt Quick parameter editor.

The model exposes scalar UI state, never devices. WidgetsParameterAdapter is the
explicit compatibility boundary until controllers consume independent models.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Property, Signal, Slot


class ParameterSourceRow(list):
    """Scalar reading order with an explicit source multi-control row boundary."""

    def __init__(self, fields=(), *, bounded=False):
        super().__init__(fields)
        self.bounded = bounded


class ParameterField(QObject):
    changed = Signal()

    def __init__(self, key, kind, parent=None):
        super().__init__(parent)
        self._key, self._kind = key, kind
        self._state = dict(text="", enabled=True, visible=True, readOnly=False,
                           tooltip="", accessibleName="", placeholder="", options=[],
                           currentIndex=-1, checked=False, minimum=0.0, maximum=100.0,
                           step=1.0, decimals=0, prefix="", suffix="", specialText="",
                           editable=False, invalid=False, error="", inputMask="", optionEnabled=[],
                           echoMode=0, maximumLength=32767, cancels=False, corrects=False,
                           preferredWidth=0.0, labelAbove=False,
                           appearance="standard", textTone="", strong=False, logNotice="")
        self._actions = []
        self._adapter = None

    fieldId = Property(str, lambda s: s._key, constant=True)
    kind = Property(str, lambda s: s._kind, constant=True)

    def publish(self, **state):
        if any(self._state.get(key) != value for key, value in state.items()):
            self._state.update(state)
            self.changed.emit()

    text = Property(str, lambda s: s._state["text"], notify=changed)
    enabled = Property(bool, lambda s: s._state["enabled"], notify=changed)
    visible = Property(bool, lambda s: s._state["visible"], notify=changed)
    readOnly = Property(bool, lambda s: s._state["readOnly"], notify=changed)
    tooltip = Property(str, lambda s: s._state["tooltip"], notify=changed)
    accessibleName = Property(str, lambda s: s._state["accessibleName"], notify=changed)
    placeholder = Property(str, lambda s: s._state["placeholder"], notify=changed)
    options = Property("QStringList", lambda s: s._state["options"], notify=changed)
    optionEnabled = Property("QVariantList", lambda s: s._state["optionEnabled"], notify=changed)
    currentIndex = Property(int, lambda s: s._state["currentIndex"], notify=changed)
    checked = Property(bool, lambda s: s._state["checked"], notify=changed)
    minimum = Property(float, lambda s: s._state["minimum"], notify=changed)
    maximum = Property(float, lambda s: s._state["maximum"], notify=changed)
    step = Property(float, lambda s: s._state["step"], notify=changed)
    decimals = Property(int, lambda s: s._state["decimals"], notify=changed)
    prefix = Property(str, lambda s: s._state["prefix"], notify=changed)
    suffix = Property(str, lambda s: s._state["suffix"], notify=changed)
    specialText = Property(str, lambda s: s._state["specialText"], notify=changed)
    editable = Property(bool, lambda s: s._state["editable"], notify=changed)
    invalid = Property(bool, lambda s: s._state["invalid"], notify=changed)
    error = Property(str, lambda s: s._state["error"], notify=changed)
    inputMask = Property(str, lambda s: s._state["inputMask"], notify=changed)
    echoMode = Property(int, lambda s: s._state["echoMode"], notify=changed)
    maximumLength = Property(int, lambda s: s._state["maximumLength"], notify=changed)
    actions = Property("QVariantList", lambda s: s._actions, notify=changed)
    cancels = Property(bool, lambda s: s._state["cancels"], notify=changed)
    corrects = Property(bool, lambda s: s._state["corrects"], notify=changed)
    preferredWidth = Property(float, lambda s: s._state["preferredWidth"], notify=changed)
    labelAbove = Property(bool, lambda s: s._state["labelAbove"], notify=changed)
    appearance = Property(str, lambda s: s._state["appearance"], notify=changed)
    textTone = Property(str, lambda s: s._state["textTone"], notify=changed)
    strong = Property(bool, lambda s: s._state["strong"], notify=changed)
    logNotice = Property(str, lambda s: s._state["logNotice"], notify=changed)

    @Slot(result=bool)
    def openLog(self):
        return bool(self._adapter and self._adapter.open_log(self))

    @Slot(result=bool)
    def openMenu(self):
        return bool(self._adapter and self._adapter.open_menu(self))

    @Slot(result=bool)
    def openChoice(self):
        return bool(self._adapter and self._adapter.open_choice(self))

    @Slot(str, result=bool)
    def edit(self, text):
        return bool(self._adapter and self._adapter.edit(self, text, commit=False))

    @Slot(str, result=bool)
    def commit(self, text):
        return bool(self._adapter and self._adapter.edit(self, text, commit=True))

    @Slot(str, result=bool)
    def accept(self, text):
        return bool(self._adapter and self._adapter.accept(self, text))

    @Slot(int, result=bool)
    def select(self, index):
        return bool(self._adapter and self._adapter.select(self, index))

    @Slot(result=bool)
    def trigger(self):
        return bool(self._adapter and self._adapter.trigger(self))

    @Slot(int, result=bool)
    def stepBy(self, steps):
        return bool(self._adapter and self._adapter.step(self, steps))


class ParameterGroup(QObject):
    def __init__(self, fields, parent=None):
        super().__init__(parent)
        self._fields = fields

    fields = Property("QVariantList", lambda s: s._fields, constant=True)


class ParameterRow(ParameterGroup):
    def __init__(self, fields, parent=None):
        super().__init__(fields, parent)
        groups, pending = [], []
        for field in fields:
            if field.kind == "label":
                if pending:
                    groups.append(pending)
                pending = [field]
            elif field.kind in {"heading", "divider", "groupcheck", "disclosure", "log", "progress"}:
                if pending:
                    groups.append(pending)
                    pending = []
                groups.append([field])
            else:
                groups.append(pending + [field])
                pending = []
        if pending:
            groups.append(pending)
        self._groups = [ParameterGroup(group, self) for group in groups]

    groups = Property("QVariantList", lambda s: s._groups, constant=True)


class ParameterFormModel(QObject):
    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self._rows = [ParameterRow(fields, self) for fields in rows]
        self._groups = [group for row in self._rows for group in row._groups]
        self._bands = []
        pending = []
        for source, row in zip(rows, self._rows):
            if getattr(source, "bounded", False):
                if pending:
                    self._bands.append(ParameterBand(pending, self))
                    pending = []
                self._bands.append(ParameterBand(row._groups, self))
            else:
                pending.extend(row._groups)
        if pending:
            self._bands.append(ParameterBand(pending, self))

    rows = Property("QVariantList", lambda s: s._rows, constant=True)
    groups = Property("QVariantList", lambda s: s._groups, constant=True)
    bands = Property("QVariantList", lambda s: s._bands, constant=True)


class ParameterBand(QObject):
    def __init__(self, groups, parent=None):
        super().__init__(parent)
        self._groups = list(groups)

    groups = Property("QVariantList", lambda s: s._groups, constant=True)
