"""Small, view-independent command/field contract for the migrating desktop UI.

The presenter never imports a widget, controller, driver or application config.
Commands are requests; the owning adapter rechecks backend availability before
dispatch. Large arrays and acquisition plans never cross this boundary.
"""
from __future__ import annotations

from copy import deepcopy
import math

from PySide6.QtCore import QObject, Property, Signal, Slot


class CommandItem(QObject):
    changed = Signal()

    def __init__(self, value, parent=None):
        super().__init__(parent)
        self._info = deepcopy(value)

    @Property("QVariantMap", notify=changed)
    def info(self):
        return deepcopy(self._info)

    def update(self, value):
        if value != self._info:
            self._info = deepcopy(value)
            self.changed.emit()


class CommandPresenter(QObject):
    changed = Signal()
    fieldsChanged = Signal()
    actionsChanged = Signal()
    validationChanged = Signal()
    commandRequested = Signal(str)
    fieldEdited = Signal(str, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._snapshot = {"title": "", "status": "", "actions": [], "fields": []}
        self._errors: dict[str, str] = {}
        self._dispatching = False
        self._closed = False
        self._fields = []
        self._actions = []

    @Property("QVariantList", notify=fieldsChanged)
    def fields(self):
        return self._fields

    @Property("QVariantList", notify=actionsChanged)
    def actions(self):
        return self._actions

    @Property("QVariantMap", notify=changed)
    def snapshot(self):
        return deepcopy(self._snapshot)

    @Property(str, notify=validationChanged)
    def validationError(self):
        return "\n".join(self._errors.values())

    @Property("QVariantList", notify=validationChanged)
    def invalidFields(self):
        return list(self._errors)

    def publish(self, *, title, status="", actions=(), fields=()):
        value = {"title": str(title), "status": str(status),
                 "actions": list(actions), "fields": list(fields)}
        if value != self._snapshot:
            self._snapshot = deepcopy(value)
            for name in ("fields", "actions"):
                items = getattr(self, "_" + name)
                rows = value[name]
                if [item._info["id"] for item in items] != [row["id"] for row in rows]:
                    for item in items:
                        item.deleteLater()
                    setattr(self, "_" + name, [CommandItem(row, self) for row in rows])
                    getattr(self, name + "Changed").emit()
                else:
                    for item, row in zip(items, rows):
                        item.update(row)
            editable = {field["id"] for field in value["fields"] if field.get("enabled")}
            errors = {key: error for key, error in self._errors.items() if key in editable}
            if errors != self._errors:
                self._errors = errors
                self.validationChanged.emit()
            self.changed.emit()

    @Slot(str, str, result=bool)
    def editField(self, key, text):
        field = next((f for f in self._snapshot["fields"] if f["id"] == key), None)
        if self._closed or field is None or not field.get("enabled", False):
            return False
        try:
            value = float(text.strip())
            if not math.isfinite(value):
                raise ValueError()
            if field.get("integer") and not value.is_integer():
                raise ValueError()
            if not field["minimum"] <= value <= field["maximum"]:
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            message = (f"{field['label']}: enter {'a whole number' if field.get('integer') else 'a number'} "
                       f"between {field['minimum']:g} and {field['maximum']:g}.")
            if self._errors.get(key) != message:
                self._errors[key] = message
                self.validationChanged.emit()
            return False
        if self._errors.pop(key, None) is not None:
            self.validationChanged.emit()
        self.fieldEdited.emit(key, value)
        return True

    @Slot(str, result=bool)
    def invoke(self, key):
        action = next((a for a in self._snapshot["actions"] if a["id"] == key), None)
        if (self._closed or self._dispatching or not action or not action.get("enabled")
                or (self._errors and not action.get("cancel", False))):
            return False
        self._dispatching = True
        try:
            self.commandRequested.emit(key)
        finally:
            self._dispatching = False
        return True

    @Slot()
    def discardInvalidEdits(self):
        if self._errors:
            self._errors.clear()
            self.validationChanged.emit()
        self.changed.emit()

    def close(self):
        self._closed = True
