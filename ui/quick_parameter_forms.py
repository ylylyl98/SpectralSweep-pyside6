"""Audited scalar layout migration with a named Widgets compatibility boundary.

Only the types in SCALAR_TYPES/CONTAINER_TYPES are translated. Scientific
views, tables, custom editors and Quick views remain explicit native boundaries;
they are never inferred from arbitrary object properties. Source controls remain
alive under a hidden host to retain existing domain validation and connections.
"""
from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Property, Signal, Slot, QTimer, QUrl, Qt
from PySide6.QtGui import QTextCursor, QValidator
from PySide6.QtQuickWidgets import QQuickWidget
from ui.quick_engine import QuickSurface
from PySide6.QtWidgets import (
    QAbstractButton, QBoxLayout, QCheckBox, QComboBox, QDoubleSpinBox,
    QDialog, QDialogButtonBox, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QRadioButton, QScrollArea,
    QLayout, QProgressBar, QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QTabWidget, QTextEdit,
    QToolButton, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from app.parameter_form_model import ParameterField, ParameterFormModel, ParameterSourceRow
from ui.workflow_sections import CompactRow, DisclosureSection


SCALAR_TYPES = (QDoubleSpinBox, QSpinBox, QLineEdit, QComboBox, QCheckBox,
                QRadioButton, QPushButton, QToolButton, QLabel)
CONTAINER_TYPES = (QWidget, QGroupBox, CompactRow, DisclosureSection)
CUSTOM_CONTAINER_TYPES = {("ui.presets_panel", "_MeasurementModeSelector")}
PAGE_IDS = {"spectrum", "settings", "bfp", "mega_sweep", "power_sweep", "mcd", "mcd2100", "instruments", "dual_gate", "calibration"}
LAYOUT_TYPES = (QBoxLayout, QFormLayout, QGridLayout)
LOG_PREVIEW_BLOCKS = 100
# Qt document positions count UTF-16 code units: bound the copied text to 16 KiB.
LOG_PREVIEW_CHARS = 8192
LOG_PREVIEW_INTERVAL_MS = 200


def _log_tail(document):
    """Copy only the bounded document suffix, including a very long final line."""
    end = document.characterCount() - 1  # omit the final paragraph terminator
    first_block = document.findBlockByNumber(max(0, document.blockCount() - LOG_PREVIEW_BLOCKS))
    start = max(first_block.position(), end - LOG_PREVIEW_CHARS)
    cursor = QTextCursor(document)
    cursor.setPosition(start)
    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    return cursor.selectedText().replace("\u2029", "\n").replace("\u2028", "\n"), start > 0


def _measurement_mode_selector(widget):
    return (type(widget).__module__, type(widget).__name__) == ("ui.presets_panel", "_MeasurementModeSelector")


def _correction_action(field, widget):
    """Audited discard/reset and row-deletion captions; never infer Preview."""
    text = widget.text().replace("&", "").strip().casefold()
    if field.fieldId in {"discard_btn", "reset_filename_btn", "loop_del_btn", "batch_del_btn"}:
        return True
    return text in {"discard", "reset", "reset / all", "reset filename defaults", "undo", "redo",
                    "remove", "remove row", "delete", "delete row", "remove selected", "delete selected", "− row", "- row"}


def _button_appearance(field, widget):
    """Carry explicit source roles across the Quick compatibility boundary.

    The small caption fallback covers existing unstyled scalar actions. Match
    whole captions so a readback or preview containing "run" stays ordinary.
    """
    role = widget.property("spectralRole")
    appearances = {"runAction": "primary", "primaryAction": "primary",
                   "applyAction": "accent", "accentAction": "accent",
                   "stopAction": "danger", "dangerAction": "danger"}
    if role in appearances:
        return appearances[role]
    button_box = widget.parentWidget()
    if isinstance(button_box, QDialogButtonBox):
        dialog_role = button_box.buttonRole(widget)
        if dialog_role in (QDialogButtonBox.ButtonRole.AcceptRole, QDialogButtonBox.ButtonRole.YesRole):
            return "primary"
        if dialog_role == QDialogButtonBox.ButtonRole.ApplyRole:
            return "accent"
        if dialog_role == QDialogButtonBox.ButtonRole.DestructiveRole:
            return "danger"
        # Dismissing a dialog is an ordinary secondary action, even when its
        # caption is Cancel; the explicit stop/acquisition roles above differ.
        return "standard"
    text = " ".join(widget.text().replace("&", "").strip().casefold().split())
    if text in {"connect", "acquire", "acquire 1d", "acquire 2d", "run 1d", "run 2d", "run", "start",
                "start mcd 1000", "start mcd 2100", "compute"}:
        return "primary"
    if text in {"apply", "apply plan", "apply target", "apply spectrograph", "apply to spectrometer",
                "apply temperature", "apply speed", "apply maximum", "apply direction", "apply scale",
                "apply + verify", "apply all assigned smus", "apply / revert background",
                "save", "save config to disk", "save current…", "save csv", "save png",
                "save current as maximum", "save controller (sm)", "retry save", "add", "add row", "add 1 row",
                "+ row", "add spectrum", "add to batch table ▸"}:
        return "accent"
    if field.fieldId in {"loop_del_btn", "batch_del_btn"} or text in {
            "cancel", "stop", "■ stop", "stop / cancel", "abort", "remove", "remove row", "delete", "delete row",
            "remove selected", "delete selected", "− row", "- row", "discard"}:
        return "danger"
    return "standard"


def _menu_edit_policy(panel, action):
    """Existing batch actions have explicit edit boundaries, not run admission."""
    discard = ("_batch_undo_action", "_batch_redo_action", "_batch_delete_disabled_action")
    commit = ("_batch_copy_action", "_batch_paste_action", "_batch_duplicate_action", "_batch_rev_action",
              "_batch_enable_selected_action", "_batch_disable_selected_action", "_batch_only_selected_action",
              "_batch_enable_all_action", "_batch_disable_all_action", "_batch_move_top_action",
              "_batch_move_bottom_action", "_batch_auto_frames_action", "_batch_validate_action")
    for attribute in discard:
        if getattr(panel, attribute, None) is action:
            return "discard"
    for attribute in commit:
        if getattr(panel, attribute, None) is action:
            return "commit"
    return "independent"


class ParameterMenuAction(QObject):
    """Explicit QAction presentation; triggering follows its original route."""
    changed = Signal()

    def __init__(self, action, adapter):
        super().__init__(adapter)
        self.action, self.adapter = action, adapter
        self._state = {}
        self._actions = []
        action.changed.connect(self.refresh)
        action.destroyed.connect(self._destroyed)
        self.refresh(force=True)

    def _destroyed(self):
        self.action = None
        self._state.update(enabled=False, visible=False)
        self.changed.emit()

    def refresh(self, force=False):
        if self.action is None or self.adapter._closed or not force and not self.adapter._active:
            return
        action = self.action
        actions = self.adapter.menu_actions(action.menu()) if action.menu() else []
        state = dict(text=action.text().replace("&", ""), enabled=action.isEnabled(),
                     visible=action.isVisible(), checked=action.isChecked(),
                     checkable=action.isCheckable(), separator=action.isSeparator())
        if state != self._state or actions != self._actions:
            self._state, self._actions = state, actions
            self.changed.emit()

    text = Property(str, lambda s: s._state.get("text", ""), notify=changed)
    enabled = Property(bool, lambda s: s._state.get("enabled", False), notify=changed)
    visible = Property(bool, lambda s: s._state.get("visible", False), notify=changed)
    checked = Property(bool, lambda s: s._state.get("checked", False), notify=changed)
    checkable = Property(bool, lambda s: s._state.get("checkable", False), notify=changed)
    separator = Property(bool, lambda s: s._state.get("separator", False), notify=changed)
    actions = Property("QVariantList", lambda s: s._actions, notify=changed)

    @Slot(result=bool)
    def openMenu(self):
        if self.action is None or self.adapter._closed or not self.action.isEnabled():
            return False
        if self.action.menu():
            self.action.menu().aboutToShow.emit()
            self.refresh(force=True)
        return True

    @Slot(result=bool)
    def trigger(self):
        if (self.action is None or self.adapter._closed or not self.action.isEnabled()
                or not self.action.isVisible() or self.action.isSeparator()):
            return False
        panel = self.adapter.visibility_root
        policy = _menu_edit_policy(panel, self.action)
        if policy == "commit":
            from ui.quick_editing import commit_quick_edits
            if not commit_quick_edits(panel):
                return False
        elif policy == "discard":
            from ui.quick_editing import discard_quick_edits
            discard_quick_edits(panel)
        if not self.action.isEnabled():
            return False
        self.action.trigger()
        self.refresh(force=True)
        return True


class WidgetsParameterAdapter(QObject):
    """Typed scalar adapter. All mutations recheck current interlocks."""

    def __init__(self, rows, *, parking=None, visibility_root=None, parent=None):
        super().__init__(parent)
        self.parking = parking
        self.visibility_root = visibility_root
        self._closed = False
        self._active = True
        self._fields = {}
        self._dirty_fields = set()
        self._choice_dirty = set()
        self._watched_fields = {}
        self._source_connections = {}
        self._log_snapshots = {}
        self._log_dialogs = {}
        self._log_dirty_fields = set()
        self._names = {}
        self._drafts = {}
        self._menu_nodes = {}
        self._editing = False
        self.validate_page = None
        self.cancel_page = None
        self._suppress_commits = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.refresh_dirty)
        self._log_timer = QTimer(self)
        self._log_timer.setSingleShot(True)
        self._log_timer.setInterval(LOG_PREVIEW_INTERVAL_MS)
        self._log_timer.timeout.connect(self._refresh_logs)
        field_rows = []
        for row in rows:
            field_row = []
            previous_label = ""
            for key, widget, kind in row:
                field = ParameterField(key, kind, self)
                field._adapter = self
                self._fields[field] = widget
                if kind == "label":
                    previous_label = widget.text().replace("&", "").rstrip(": ")
                self._names[field] = previous_label or key.replace("_", " ")
                field_row.append(field)
                self._bind_source(field, widget)
            if field_row:
                field_rows.append(ParameterSourceRow(field_row, bounded=getattr(row, "bounded", False)))
        # Grid forms frequently place label buddies in the row above editors.
        # Preserve those explicit pairs before responsive packing; row-by-row
        # flattening alone would separate Start/Stop labels from their values.
        by_widget = {id(widget): field for field, widget in self._fields.items()}
        buddy_labels = {id(widget.buddy()): field for field, widget in self._fields.items()
                        if isinstance(widget, QLabel) and widget.buddy() is not None
                        and id(widget.buddy()) in by_widget}
        consumed, paired_rows = set(), []
        for row in field_rows:
            paired = []
            for field in row:
                if field in consumed:
                    continue
                widget = self._fields[field]
                target = by_widget.get(id(widget.buddy())) if isinstance(widget, QLabel) and widget.buddy() is not None else None
                label = field if target is not None else buddy_labels.get(id(widget))
                editor = target if target is not None else field
                if label is not None and label not in consumed and editor not in consumed:
                    paired.extend((label, editor))
                    consumed.update((label, editor))
                    self._names[editor] = self._fields[label].text().replace("&", "").rstrip(": ")
                else:
                    paired.append(field)
                    consumed.add(field)
            if paired:
                paired_rows.append(ParameterSourceRow(paired, bounded=row.bounded))
        self.model = ParameterFormModel(paired_rows, self)
        self._readback_fields = tuple(field for field, widget in self._fields.items()
                                      if isinstance(widget, (QLabel, QGroupBox)))
        self.refresh()

    def _bind_source(self, field, widget):
        self._watch(widget, field)
        self._dirty_fields.add(field)
        self._choice_dirty.add(field)
        connections = []
        # Explicit signal sets: unsupported widgets never enter here.
        for signal in ("valueChanged", "textChanged", "currentIndexChanged", "currentTextChanged", "toggled", "clicked"):
            bound = getattr(widget, signal, None)
            if bound is not None:
                callback = lambda *args, current=field: self._source_changed(current)
                bound.connect(callback)
                connections.append((bound, callback))
        if isinstance(widget, QComboBox):
            for signal in (widget.model().dataChanged, widget.model().rowsInserted,
                           widget.model().rowsRemoved, widget.model().modelReset):
                callback = lambda *args, current=field: self._options_changed(current)
                signal.connect(callback)
                connections.append((signal, callback))
        callback = lambda *args, current=field: self._mark_dirty(current)
        widget.destroyed.connect(callback)
        connections.append((widget.destroyed, callback))
        self._source_connections[field] = connections
        ancestor = widget.parentWidget()
        while ancestor is not None:
            self._watch(ancestor, field)
            if ancestor is self.parking:
                break
            ancestor = ancestor.parentWidget()

    def _watch(self, widget, field):
        if widget not in self._watched_fields:
            widget.installEventFilter(self)
            self._watched_fields[widget] = set()
        self._watched_fields[widget].add(field)

    def _mark_dirty(self, field):
        if not self._closed:
            if field.kind == "log":
                self._log_dirty_fields.add(field)
                if self._active and not self._log_timer.isActive():
                    self._log_timer.start()
                return
            self._dirty_fields.add(field)
            self.schedule()

    def _options_changed(self, field):
        self._choice_dirty.add(field)
        self._mark_dirty(field)

    def rebind_source(self, field, widget):
        previous = self._fields[field]
        self._log_snapshots.pop(field, None)
        self._log_dirty_fields.discard(field)
        if not self._log_dirty_fields:
            self._log_timer.stop()
        dialog = self._log_dialogs.pop(field, None)
        if dialog is not None:
            dialog.close()
        if isValid(previous):
            previous.removeEventFilter(self)
        self._watched_fields.pop(previous, None)
        for signal, callback in self._source_connections.pop(field, []):
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                pass
        self._fields[field] = widget
        self._drafts.pop(field, None)
        field.publish(invalid=False, error="")
        self._bind_source(field, widget)
        self.refresh()

    def eventFilter(self, watched, event):
        if self._closed:
            return False
        fields = self._watched_fields.get(watched, ())
        if event.type() == QEvent.Type.LayoutRequest:
            # Hidden QLabel/readback owners lack a text notification signal.
            # A value edit must not resnapshot every combo in its ancestor row.
            self._dirty_fields.update(field for field in fields if field.kind in {"label", "heading"})
        elif event.type() in (QEvent.Type.EnabledChange, QEvent.Type.Show, QEvent.Type.Hide,
                              QEvent.Type.ShowToParent, QEvent.Type.HideToParent,
                              QEvent.Type.DynamicPropertyChange, QEvent.Type.ParentChange):
            self._dirty_fields.update(fields)
        else:
            return False
        if self._dirty_fields:
            self.schedule()
        return False

    def schedule(self, *_):
        if not self._closed and self._active:
            self._timer.start(0)

    def set_active(self, active):
        self._active = bool(active)
        if not active:
            self._timer.stop()
            self._log_timer.stop()

    def _source_changed(self, field):
        if self._closed:
            return
        if not self._editing:
            self._drafts.pop(field, None)
            field.publish(invalid=False, error="")
        self._mark_dirty(field)

    def _visible(self, widget):
        current = widget
        while current is not None and current is not self.visibility_root:
            if current is not self.parking and current.isHidden():
                return False
            current = current.parentWidget()
        return True

    def refresh(self):
        """Explicit fresh snapshot for command/session boundaries and fallback."""
        self._refresh_fields(self._fields, choices=True)
        self._dirty_fields.clear()

    def refresh_dirty(self):
        if self._closed or not self._dirty_fields:
            return
        dirty = self._dirty_fields
        self._dirty_fields = set()
        self._refresh_fields(dirty, choices=False)

    def _refresh_logs(self):
        """Publish the latest suffix once per burst; controls keep their 0ms queue."""
        if self._closed or not self._active:
            return
        self._refresh_fields(tuple(self._log_dirty_fields), choices=False)

    def refresh_readbacks(self):
        """Keep signal-free label telemetry responsive without a form scan.

        Called on the GUI thread only. It reads presentation text, never a
        controller/device, options, metadata, or ancestor visibility.
        """
        if self._closed or not self._active:
            return
        for field in self._readback_fields:
            if not field.visible:
                continue
            widget = self._fields.get(field)
            if widget is None or not isValid(widget):
                field.publish(visible=False, enabled=False)
            else:
                field.publish(text=widget.text() if isinstance(widget, QLabel) else widget.title())

    def _refresh_fields(self, fields, *, choices):
        if self._closed:
            return
        for field in fields:
            widget = self._fields.get(field)
            if widget is None:
                continue
            if not isValid(widget):
                self._drafts.pop(field, None)
                field.publish(enabled=False, visible=False, invalid=False, error="")
                continue
            state = dict(enabled=widget.isEnabled(), visible=self._visible(widget),
                         tooltip=widget.toolTip(), accessibleName=widget.accessibleName() or self._names[field],
                         textTone=widget.property("spectralTone") or "",
                         strong=bool(widget.property("spectralStrong")))
            preferred = widget.property("quickPreferredWidth")
            state["preferredWidth"] = float(preferred) if isinstance(preferred, (int, float)) and math.isfinite(preferred) and preferred > 0 else 0.0
            state["labelAbove"] = bool(widget.property("quickLabelAbove"))
            if isinstance(widget, (QDoubleSpinBox, QSpinBox)):
                text = widget.cleanText()
                state.update(text=self._drafts.get(field, text), minimum=widget.minimum(),
                             maximum=widget.maximum(), step=widget.singleStep(),
                             decimals=widget.decimals() if isinstance(widget, QDoubleSpinBox) else 0,
                             prefix=widget.prefix(), suffix=widget.suffix(),
                             specialText=widget.specialValueText(), readOnly=widget.isReadOnly())
            elif isinstance(widget, QLineEdit):
                state.update(text=self._drafts.get(field, widget.text()), readOnly=widget.isReadOnly(),
                             placeholder=widget.placeholderText(), inputMask=widget.inputMask(),
                             echoMode=widget.echoMode().value, maximumLength=widget.maxLength())
            elif isinstance(widget, QComboBox):
                state.update(text=widget.currentText(), currentIndex=widget.currentIndex(), editable=widget.isEditable())
                if choices or field in self._choice_dirty:
                    source_model = widget.model()
                    column = widget.modelColumn()
                    state.update(options=[widget.itemText(i) for i in range(widget.count())],
                                 optionEnabled=[bool(source_model.flags(source_model.index(i, column)) & Qt.ItemFlag.ItemIsEnabled) for i in range(widget.count())])
            elif _measurement_mode_selector(widget):
                buttons = [widget._group.button(index) for index in range(len(widget._modes))]
                state.update(text=widget.currentText(), currentIndex=widget.findText(widget.currentText()),
                             options=[button.text() for button in buttons],
                             optionEnabled=[button.isEnabled() for button in buttons], editable=False,
                             preferredWidth=state["preferredWidth"] or 92.0)
            elif isinstance(widget, QAbstractButton):
                state.update(text=widget.text().replace("&", ""), checked=widget.isChecked(),
                             cancels=any(word in widget.text().lower() for word in ("cancel", "close", "stop", "abort", "disconnect")),
                             corrects=field.kind == "button" and _correction_action(field, widget),
                             appearance=_button_appearance(field, widget) if field.kind in {"button", "menu"} else "standard")
                if field.kind == "menu":
                    actions = self.menu_actions(widget.menu())
                    if actions != field._actions:
                        field._actions = actions
                        field.changed.emit()
            elif isinstance(widget, QLabel):
                state.update(text=widget.text())
            elif isinstance(widget, (QPlainTextEdit, QTextEdit)):
                # Reconciliation or ancestor layout notifications must not turn
                # each acquisition log line into a fresh QML text layout.
                if self._active and not self._log_timer.isActive():
                    document = widget.document()
                    stamp = (document, document.revision())
                    cached = self._log_snapshots.get(field)
                    if cached is None or cached[0] != stamp:
                        text, truncated = _log_tail(document)
                        cached = (stamp, text, truncated)
                        self._log_snapshots[field] = cached
                    state.update(text=cached[1], readOnly=True,
                                 logNotice=f"Showing latest log entries (up to {LOG_PREVIEW_BLOCKS} lines / {LOG_PREVIEW_CHARS // 1024}K characters)."
                                 if cached[2] else "")
                    self._log_dirty_fields.discard(field)
            elif isinstance(widget, QProgressBar):
                state.update(text=widget.text(), minimum=widget.minimum(), maximum=widget.maximum(),
                             step=widget.value())
            elif isinstance(widget, QGroupBox):
                state.update(text=widget.title(), checked=widget.isChecked())
            field.publish(**state)
            self._choice_dirty.discard(field)

    def open_log(self, field):
        """Expose the retained full document on demand, without copying it to QML."""
        widget = self._fields.get(field)
        if self._closed or field.kind != "log" or not isinstance(widget, (QPlainTextEdit, QTextEdit)) or not isValid(widget):
            return False
        dialog = self._log_dialogs.get(field)
        if dialog is None:
            parent = self.visibility_root or widget.window()
            dialog = QDialog(parent)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.setObjectName("QuickFullLogDialog")
            dialog.setWindowTitle("Full log")
            dialog.resize(900, 600)
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel("Complete log · updates live · select text to copy", dialog))
            # Use the same widget kind so its document layout remains compatible.
            viewer = QPlainTextEdit(dialog) if isinstance(widget, QPlainTextEdit) else QTextEdit(dialog)
            viewer.setObjectName("FullLogText")
            viewer.setReadOnly(True)
            viewer.setDocument(widget.document())
            layout.addWidget(viewer)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=dialog)
            buttons.rejected.connect(dialog.close)
            layout.addWidget(buttons)
            widget.destroyed.connect(dialog.close)
            dialog.finished.connect(lambda _result, key=field: self._log_dialogs.pop(key, None))
            self._log_dialogs[field] = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return True

    def menu_actions(self, menu):
        result = []
        for action in menu.actions():
            node = self._menu_nodes.get(id(action))
            if node is None or node.action is not action:
                node = ParameterMenuAction(action, self)
                self._menu_nodes[id(action)] = node
            else:
                node.refresh(force=True)
            result.append(node)
        return result

    def open_menu(self, field):
        widget = self._writable(field)
        if not isinstance(widget, (QPushButton, QToolButton)) or widget.menu() is None:
            return False
        widget.menu().aboutToShow.emit()
        self.refresh()
        return True

    def open_choice(self, field):
        widget = self._writable(field)
        if widget is not None and _measurement_mode_selector(widget):
            self._mark_dirty(field)
            self.refresh_dirty()
            return True
        if not isinstance(widget, QComboBox):
            return False
        if type(widget).__module__ == "ui.loop_run_selection" and type(widget).__name__ == "RunSelectionCombo":
            widget.prepare_choices()
        self._options_changed(field)
        self.refresh_dirty()
        return True

    def _writable(self, field):
        widget = self._fields.get(field)
        return (widget if not self._closed and widget is not None and isValid(widget) and widget.isEnabled()
                and self._visible(widget) else None)

    def edit(self, field, text, *, commit):
        if commit and self._suppress_commits:
            return False
        widget = self._writable(field)
        if widget is None:
            return False
        if isinstance(widget, (QDoubleSpinBox, QSpinBox)):
            if widget.isReadOnly():
                return False
            if not commit:
                # Motion target fields use this user-only signal to stop live
                # telemetry overwriting a target while the operator is typing.
                widget.lineEdit().textEdited.emit(text)
            valid = widget.validate(text, len(text))[0] == QValidator.State.Acceptable
            if valid:
                number = widget.valueFromText(text)
                valid = math.isfinite(float(number)) and widget.minimum() <= number <= widget.maximum()
            if not valid:
                self._drafts[field] = text
                field.publish(text=text, invalid=True,
                              error=f"Enter a value from {widget.minimum():g} to {widget.maximum():g}.")
                return False
            self._drafts[field] = text
            if commit or widget.keyboardTracking():
                self._editing = True
                try:
                    widget.setValue(number)
                finally:
                    self._editing = False
            if commit:
                self._drafts.pop(field, None)
                widget.editingFinished.emit()
        elif isinstance(widget, QLineEdit):
            if widget.isReadOnly():
                return False
            validator = widget.validator()
            valid = validator is None or validator.validate(text, len(text))[0] != QValidator.State.Invalid
            if len(text) > widget.maxLength() or not valid:
                self._drafts[field] = text
                field.publish(text=text, invalid=True, error="This value does not match the required format.")
                return False
            self._drafts.pop(field, None)
            self._editing = True
            try:
                widget.setText(text)
            finally:
                self._editing = False
            if not commit:
                widget.textEdited.emit(widget.text())
            elif widget.hasAcceptableInput():
                widget.editingFinished.emit()
            if not widget.hasAcceptableInput():
                field.publish(invalid=True, error="Complete the required value before continuing.")
                return False
        elif isinstance(widget, QComboBox) and widget.isEditable():
            widget.setEditText(text)
            if commit:
                widget.lineEdit().editingFinished.emit()
            else:
                widget.lineEdit().textEdited.emit(text)
        else:
            return False
        field.publish(invalid=False, error="")
        self._dirty_fields.add(field)
        self.refresh_dirty()
        return True

    def accept(self, field, text):
        if not self.edit(field, text, commit=True):
            return False
        if self.validate_page is not None and not self.validate_page():
            return False
        widget = self._fields[field]
        if isinstance(widget, (QDoubleSpinBox, QSpinBox, QComboBox)):
            widget.lineEdit().returnPressed.emit()
        elif isinstance(widget, QLineEdit):
            widget.returnPressed.emit()
        self.refresh_dirty()
        return True

    def select(self, field, index):
        widget = self._writable(field)
        if widget is not None and _measurement_mode_selector(widget):
            if not 0 <= index < len(widget._modes) or not widget._group.button(index).isEnabled():
                field.changed.emit()
                return False
            widget.setCurrentText(widget._modes[index])
            self._mark_dirty(field)
            self.refresh_dirty()
            return True
        if not isinstance(widget, QComboBox) or not 0 <= index < widget.count():
            field.changed.emit()
            return False
        # Respect disabled model rows, including separator entries.
        if not widget.model().flags(widget.model().index(index, widget.modelColumn())) & Qt.ItemFlag.ItemIsEnabled:
            field.changed.emit()
            return False
        widget.setCurrentIndex(index)
        widget.activated.emit(index)
        widget.textActivated.emit(widget.itemText(index))
        self._dirty_fields.add(field)
        self.refresh_dirty()
        return True

    def trigger(self, field):
        widget = self._writable(field)
        if isinstance(widget, QGroupBox) and widget.isCheckable():
            widget.setChecked(not widget.isChecked())
            widget.clicked[bool].emit(widget.isChecked())
            self.refresh_dirty()
            return True
        if not isinstance(widget, QAbstractButton):
            return False
        command = widget.text().lower()
        # Scalar command buttons may capture several fields, including values
        # owned by another batch. Never dispatch one while a visible draft is
        # invalid; cancellation and mode toggles remain independently usable.
        correction = field.kind == "button" and _correction_action(field, widget)
        mutating = field.kind == "button" and not correction and not any(
            word in command for word in ("stop", "abort", "cancel", "pause", "disconnect", "close"))
        if mutating and (any(other.invalid and other.visible and other.enabled for other in self._fields)
                         or self.validate_page is not None and not self.validate_page()):
            return False
        if field.cancels and self.cancel_page is not None:
            self.cancel_page()
        if correction and self.visibility_root is not None:
            from ui.quick_editing import discard_quick_edits
            discard_quick_edits(self.visibility_root)
        widget.click()
        self._dirty_fields.add(field)
        self.refresh_dirty()
        return True

    def step(self, field, steps):
        widget = self._writable(field)
        if not isinstance(widget, (QDoubleSpinBox, QSpinBox)) or widget.isReadOnly():
            return False
        self._drafts.pop(field, None)
        widget.stepBy(steps)
        field.publish(invalid=False, error="")
        self._dirty_fields.add(field)
        self.refresh_dirty()
        return True

    def shutdown(self):
        self._closed = True
        self._timer.stop()
        self._log_timer.stop()
        self._log_dirty_fields.clear()
        for dialog in list(self._log_dialogs.values()):
            dialog.close()
        self._log_dialogs.clear()
        self._log_snapshots.clear()
        self._dirty_fields.clear()
        for field in self._fields:
            field._adapter = None
        for connections in self._source_connections.values():
            for signal, callback in connections:
                try:
                    signal.disconnect(callback)
                except (RuntimeError, TypeError):
                    pass
        self._source_connections.clear()


class QuickParameterForm(QuickSurface):
    heightHintChanged = Signal()
    def __init__(self, adapter, parent=None):
        super().__init__(parent, match_host_background=True)
        self.adapter = adapter
        adapter.setParent(self)
        self._height_timer = QTimer(self)
        self._height_timer.setSingleShot(True)
        self._height_timer.timeout.connect(self._resize_height)
        self.setObjectName("QuickParameterForm")
        self.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setInitialProperties({"bridge": adapter.model})
        self.setSource(QUrl.fromLocalFile(str(Path(__file__).parent / "qml" / "ParameterForm.qml")))
        if self.status() == QQuickWidget.Status.Error:
            raise RuntimeError("\n".join(error.toString() for error in self.errors()))
        self.rootObject().implicitHeightChanged.connect(self._schedule_height)
        self.rootObject().widthChanged.connect(self._schedule_height)
        self._resize_height()
        # Labels and blocked-signal changes have no Qt notification signal.
        self._refresh = QTimer(self)
        # Notification-driven updates cover normal interaction. Reconciliation
        # retains blocked-signal mutations and metadata without Qt signals.
        self._refresh.setInterval(1000)
        self._refresh.timeout.connect(adapter.refresh)
        self._readback = QTimer(self)
        self._readback.setInterval(150)
        self._readback.timeout.connect(adapter.refresh_readbacks)
        adapter.set_active(False)

    def _resize_height(self):
        height = max(1, math.ceil(self.rootObject().implicitHeight()))
        if self.height() == height and self.minimumHeight() == height:
            return
        self.setFixedHeight(height)
        owner = self.parentWidget()
        if owner is not None:
            if owner.layout() is not None:
                owner.layout().invalidate()
                owner.layout().activate()
            owner.updateGeometry()
        self.heightHintChanged.emit()

    def _schedule_height(self):
        if not self.adapter._closed:
            self._height_timer.start(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_height()

    def showEvent(self, event):
        super().showEvent(event)
        if not self.adapter._closed:
            self.adapter.set_active(True)
            self.adapter.refresh()
            self._refresh.start()
            if self.adapter._readback_fields:
                self._readback.start()

    def hideEvent(self, event):
        self._refresh.stop()
        self._readback.stop()
        self.adapter.set_active(False)
        super().hideEvent(event)

    def shutdown(self):
        self._height_timer.stop()
        self._refresh.stop()
        self._readback.stop()
        self.adapter.shutdown()
        self.setSource(QUrl())


def _kind(widget):
    if _measurement_mode_selector(widget):
        return "choice"
    if isinstance(widget, (QDoubleSpinBox, QSpinBox)):
        return "number"
    if isinstance(widget, QLineEdit):
        return "text"
    if isinstance(widget, QComboBox):
        return "choice"
    if (isinstance(widget, QToolButton) and widget.isCheckable()
            and (widget.property("fluentRole") == "sectionHeader" or widget.property("quickDisclosure"))):
        return "disclosure"
    if isinstance(widget, (QCheckBox, QRadioButton)) or isinstance(widget, (QPushButton, QToolButton)) and widget.isCheckable() and widget.menu() is None:
        return "check"
    if isinstance(widget, (QPushButton, QToolButton)):
        return "button" if widget.menu() is None else "menu"
    if isinstance(widget, QLabel):
        # Pixmaps/animations and interactive hyperlinks need specialized views.
        return "label" if widget.pixmap().isNull() and widget.movie() is None and "<a " not in widget.text().lower() else None
    if isinstance(widget, (QPlainTextEdit, QTextEdit)) and widget.isReadOnly():
        return "log"
    if isinstance(widget, QProgressBar):
        return "progress"
    if type(widget) is QFrame and widget.frameShape() in (QFrame.Shape.HLine, QFrame.Shape.VLine):
        return "divider"
    return None


def _supported_widget(widget):
    # An explicitly grouped workflow row owns its own responsive form. Keep
    # that boundary visible instead of flattening it into an ancestor's Flow.
    if widget.property("quickSectionBoundary"):
        return False
    # Group ownership and native responsive layouts remain visible. Flattening
    # sibling group boxes loses their column boundaries and parks the layout
    # that the page's resize handler still controls.
    if isinstance(widget, QGroupBox) and getattr(widget, "_quick_parallel_group", False):
        return False
    container = type(widget) in CONTAINER_TYPES or (type(widget).__module__, type(widget).__name__) in CUSTOM_CONTAINER_TYPES
    return _kind(widget) is not None or container and widget.layout() is not None and _supported_layout(widget.layout())


def _supported_layout(layout):
    return isinstance(layout, LAYOUT_TYPES) and all(
        _supported_widget(item.widget()) if item.widget() is not None else
        _supported_layout(item.layout()) if item.layout() is not None else True
        for item in (layout.itemAt(i) for i in range(layout.count())))


def _rows(layout, identifiers, prefix):
    """Translate audited layout structure, retaining reading order and labels."""
    rows = []

    def item_rows(item):
        widget = item.widget()
        if widget is not None:
            kind = _kind(widget)
            if kind is not None:
                key = identifiers.get(id(widget), widget.objectName()) or f"{prefix}_{len(identifiers)}"
                identifiers.setdefault(id(widget), key)
                return [[(key, widget, kind)]]
            result = []
            if isinstance(widget, QGroupBox):
                key = f"{prefix}_heading_{len(identifiers)}"
                identifiers.setdefault(id(widget), key)
                result.append([(key, widget, "groupcheck" if widget.isCheckable() else "heading")])
            return result + _rows(widget.layout(), identifiers, prefix)
        return _rows(item.layout(), identifiers, prefix) if item.layout() else []

    if isinstance(layout, QFormLayout):
        for row in range(layout.rowCount()):
            entries = []
            # A spanning item is also returned for FieldRole by Qt. Querying
            # both roles duplicates the editor and its signal connections.
            for role in (QFormLayout.ItemRole.LabelRole, QFormLayout.ItemRole.FieldRole):
                item = layout.itemAt(row, role)
                if item:
                    entries.extend(item_rows(item))
            if entries:
                fields = [field for entry in entries for field in entry]
                rows.append(ParameterSourceRow(fields, bounded=len(fields) > 2))
    elif isinstance(layout, QGridLayout):
        grouped = {}
        for i in range(layout.count()):
            row, col, _, _ = layout.getItemPosition(i)
            grouped.setdefault(row, []).append((col, item_rows(layout.itemAt(i))))
        for row in sorted(grouped):
            rows.append([field for _, entries in sorted(grouped[row], key=lambda e: e[0]) for entry in entries for field in entry])
    elif isinstance(layout, QBoxLayout) and layout.direction() in (QBoxLayout.Direction.LeftToRight, QBoxLayout.Direction.RightToLeft):
        rows.append(ParameterSourceRow(
            [field for i in range(layout.count()) for row in item_rows(layout.itemAt(i)) for field in row],
            bounded=bool(layout.property("quickRowBoundary"))))
    else:
        for i in range(layout.count()):
            rows.extend(item_rows(layout.itemAt(i)))
    return rows


def install_parameter_forms(panel, page_id, *, refresh=False):
    """Replace maximal supported scalar layouts; return a coverage manifest.

    Called only for the explicitly admitted application pages. Tables, plot
    controls and unknown custom widgets are reported; no placeholder is created.
    This installer does not register or operate an instrument.
    """
    if page_id not in PAGE_IDS:
        raise ValueError(f"No audited parameter migration for page {page_id!r}")
    existing = getattr(panel, "_quick_parameter_forms", None)
    if existing is not None and not refresh:
        return existing
    forms = [form for form in existing["forms"] if isValid(form)] if existing else []
    boundaries, migrated = set(), set()
    previous_count = existing["migrated_fields"] if existing else 0
    refresh_mega_geometry = type(panel).__module__ == "ui.megasweep_panel" and type(panel).__name__ == "MegaSweepPanel"
    if refresh_mega_geometry and not hasattr(panel, "_quick_form_geometry_timer"):
        timer = QTimer(panel)
        timer.setSingleShot(True)
        timer.timeout.connect(panel._refresh_settings_scroll_range)
        panel._quick_form_geometry_timer = timer
    identifiers = {id(value): key.lstrip("_") for key, value in vars(panel).items() if isinstance(value, QWidget)}

    def mark_parallel_groups(layout):
        if layout is None:
            return
        # Retain semantic parallel columns, rather than every group box. Plain
        # vertical groups can share the compact Quick form as before.
        groups = [layout.itemAt(i).widget() for i in range(layout.count())
                  if isinstance(layout.itemAt(i).widget(), QGroupBox)]
        if layout.property("quickSectionColumns"):
            groups.extend(layout.itemAt(i).widget().content for i in range(layout.count())
                          if isinstance(layout.itemAt(i).widget(), DisclosureSection)
                          and isinstance(layout.itemAt(i).widget().content, QGroupBox))
        if (isinstance(layout, QBoxLayout) and
                (layout.property("quickSectionColumns") or len(groups) > 1 and layout.direction() in
                 (QBoxLayout.Direction.LeftToRight, QBoxLayout.Direction.RightToLeft))):
            for group in groups:
                if not group.isCheckable():
                    group._quick_parallel_group = True
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item.layout() is not None:
                mark_parallel_groups(item.layout())

    mark_parallel_groups(panel.layout())
    for container in panel.findChildren(QWidget):
        # QWidget layout ownership includes scroll/tab pages and ordinary row
        # containers; nested layouts themselves are visited by the helper.
        if type(container) in CONTAINER_TYPES:
            mark_parallel_groups(container.layout())

    def supported_item(item):
        return (_supported_widget(item.widget()) if item.widget() is not None else
                _supported_layout(item.layout()) if item.layout() is not None else True)

    def split_scalar_runs(layout, owner):
        """Isolate supported runs inside mixed scientific/table layouts.

        A scalar next to an opaque native view must remain reachable in Quick;
        the opaque view itself retains its original owner and placement.
        """
        if isinstance(layout, QBoxLayout):
            runs, current = [], []
            for index in range(layout.count()):
                if supported_item(layout.itemAt(index)):
                    current.append(index)
                elif current:
                    runs.append(current)
                    current = []
            if current:
                runs.append(current)
            for run in reversed(runs):
                # Avoid surfaces that contain only text and spacers.
                items = [layout.itemAt(i) for i in run]
                if not any(item.widget() is not None and _kind(item.widget()) not in {None, "label", "divider"}
                           or item.layout() is not None for item in items):
                    continue
                holder = QBoxLayout(layout.direction())
                holder.setContentsMargins(0, 0, 0, 0)
                for _ in run:
                    holder.addItem(layout.takeAt(run[0]))
                layout.insertLayout(run[0], holder)
                migrate(holder, owner)
        elif isinstance(layout, QFormLayout):
            candidates = []
            for row in range(layout.rowCount()):
                items = [layout.itemAt(row, role) for role in (QFormLayout.ItemRole.LabelRole, QFormLayout.ItemRole.FieldRole)]
                if all(item is None or supported_item(item) for item in items):
                    candidates.append(row)
            runs = []
            for row in candidates:
                if not runs or row != runs[-1][-1] + 1:
                    runs.append([])
                runs[-1].append(row)
            for run in reversed(runs):
                holder_widget = QWidget(owner)
                holder = QFormLayout(holder_widget)
                holder.setContentsMargins(0, 0, 0, 0)
                taken = [layout.takeRow(row) for row in reversed(run)]
                for row, source in enumerate(reversed(taken)):
                    for item, role in ((source.labelItem, QFormLayout.ItemRole.LabelRole),
                                       (source.fieldItem, QFormLayout.ItemRole.FieldRole)):
                        if item is not None:
                            holder.setItem(row, role, item)
                layout.insertRow(run[0], holder_widget)
                migrate(holder, holder_widget)
        elif isinstance(layout, QGridLayout):
            candidates = {}
            for index in range(layout.count()):
                row, col, rows, cols = layout.getItemPosition(index)
                candidates.setdefault(row, []).append((index, col, rows, cols))
            for target_row in sorted(candidates, reverse=True):
                entries = []
                for index in range(layout.count()):
                    row, col, rows, cols = layout.getItemPosition(index)
                    if row == target_row:
                        entries.append((index, col, rows, cols))
                if any(rows != 1 or not supported_item(layout.itemAt(index)) for index, col, rows, cols in entries):
                    continue
                # Preserve the grid's column-span placement while consolidating
                # a scalar row into a responsive Quick flow.
                holder_widget = QWidget(owner)
                holder = QHBoxLayout(holder_widget)
                holder.setContentsMargins(0, 0, 0, 0)
                row = target_row
                items = [(col, layout.takeAt(index)) for index, col, rows, cols in sorted(entries, reverse=True)]
                for _, item in sorted(items, key=lambda entry: entry[0]):
                    holder.addItem(item)
                column = min(col for _, col, _, _ in entries)
                span = max(col + cols for _, col, _, cols in entries) - column
                layout.addWidget(holder_widget, row, column, 1, span)
                migrate(holder, holder_widget)

    def migrate(layout, owner):
        if layout is None:
            return
        if _supported_layout(layout):
            rows = _rows(layout, identifiers, page_id)
            if not any(kind not in {"label", "heading", "divider"} for row in rows for _, _, kind in row):
                return
            parking = QWidget(owner)
            parking.setObjectName("QuickParameterStateHost")
            parking.hide()
            # Preserve the original layout identity as well as its controls.
            # Controllers use QFormLayout.labelForField/setRowVisible at runtime.
            # Emptying that layout would sever those existing behavior paths.
            explicit_visibility = []
            def remember(source_layout):
                for i in range(source_layout.count()):
                    source_item = source_layout.itemAt(i)
                    if source_item.widget() is not None:
                        source = source_item.widget()
                        explicit_visibility.append((source, source.isHidden()))
                    elif source_item.layout() is not None:
                        remember(source_item.layout())
            remember(layout)
            if owner.layout() is layout:
                margins = layout.contentsMargins()
                parking.setLayout(layout)  # Qt transfers layout from owner.
                replacement = QVBoxLayout(owner)
                replacement.setContentsMargins(margins)
                replacement.setSpacing(layout.spacing())
                replacement.setAlignment(Qt.AlignmentFlag.AlignTop)
                # Fixed-height Widgets rows cannot contain a responsive Quick
                # form after wrapping. Their source layout remains unchanged
                # under parking; the visible owner must follow the new hint.
                if owner.maximumHeight() < 16777215:
                    owner.setMaximumHeight(16777215)
            else:
                parent_layout = layout.parent()
                if not isinstance(parent_layout, QLayout):
                    raise RuntimeError("Scalar layout has no auditable parent layout")
                index = next(i for i in range(parent_layout.count()) if parent_layout.itemAt(i).layout() is layout)
                position = (parent_layout.getItemPosition(index) if isinstance(parent_layout, (QGridLayout, QFormLayout)) else None)
                parent_layout.takeAt(index)
                layout.setParent(None)
                parking.setLayout(layout)
                replacement = QVBoxLayout()
                replacement.setContentsMargins(0, 0, 0, 0)
                replacement.setSpacing(layout.spacing())
                replacement.setAlignment(Qt.AlignmentFlag.AlignTop)
                if isinstance(parent_layout, QBoxLayout):
                    parent_layout.insertLayout(index, replacement)
                elif isinstance(parent_layout, QGridLayout):
                    parent_layout.addLayout(replacement, *position)
                elif isinstance(parent_layout, QFormLayout):
                    parent_layout.setLayout(*position, replacement)
                else:
                    raise RuntimeError("Unsupported parent layout at scalar migration boundary")
            for source, hidden in explicit_visibility:
                source.setVisible(not hidden)
            adapter = WidgetsParameterAdapter(rows, parking=parking, visibility_root=panel)
            def commit_page():
                from ui.quick_editing import commit_quick_edits
                return commit_quick_edits(panel)
            adapter.validate_page = commit_page
            form = QuickParameterForm(adapter, owner)
            if refresh_mega_geometry:
                form.heightHintChanged.connect(lambda: panel._quick_form_geometry_timer.start(0))
            replacement.addWidget(form)
            forms.append(form)
            migrated.update(id(widget) for row in rows for _, widget, kind in row if kind not in {"label", "heading", "divider"})
            return
        split_scalar_runs(layout, owner)
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item.layout() is not None:
                migrate(item.layout(), owner)
            elif item.widget() is not None:
                descend(item.widget())

    def descend(widget):
        if isinstance(widget, QQuickWidget):
            return
        if type(widget).__module__ == "ui.quick_table_editor" and type(widget).__name__ == "QuickTableEditor":
            return
        if getattr(widget, "_quick_reference_editor", None) is not None:
            return
        if (isinstance(widget, QGroupBox) and widget.isCheckable()
                and widget.layout() is not None and not _supported_layout(widget.layout())
                and not getattr(widget, "_quick_group_header", None)):
            # Opaque child editors keep this group native. Render its enable
            # control as a sibling: a child of an unchecked QGroupBox would be
            # disabled by Qt and could never turn the group back on.
            owner = widget.parentWidget()
            owner_layout = owner.layout() if owner is not None else None
            if owner_layout is not None:
                wrapper = QWidget(owner)
                wrapper.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
                wrapper_layout = QVBoxLayout(wrapper)
                wrapper_layout.setContentsMargins(0, 0, 0, 0)
                wrapper_layout.setSpacing(0)
                previous = owner_layout.replaceWidget(widget, wrapper)
                if previous is None:
                    wrapper.deleteLater()
                else:
                    key = identifiers.get(id(widget), widget.objectName()) or f"{page_id}_groupcheck_{len(forms)}"
                    adapter = WidgetsParameterAdapter([[(key, widget, "groupcheck")]], visibility_root=panel)
                    header = QuickParameterForm(adapter, wrapper)
                    widget._quick_group_header = header
                    widget.setProperty("quickHeaderAdapter", True)
                    widget.setStyleSheet(widget.styleSheet() + '\n'
                        'QGroupBox[quickHeaderAdapter="true"] { margin-top: 0px; padding-top: 0px; }\n'
                        'QGroupBox[quickHeaderAdapter="true"]::title { width: 0px; height: 0px; color: transparent; }\n'
                        'QGroupBox[quickHeaderAdapter="true"]::indicator { width: 0px; height: 0px; }')
                    wrapper_layout.addWidget(header)
                    wrapper_layout.addWidget(widget)
                    wrapper_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
                    forms.append(header)
                    migrated.add(id(widget))
        if isinstance(widget, QDialogButtonBox):
            # QDialogButtonBox owns a private C++ layout and may rebuild it
            # while visible. Moving that layout causes a native crash. Keep
            # the entire button box as the hidden action owner instead.
            owner = widget.parentWidget()
            layout = owner.layout() if owner is not None else None
            if layout is None or layout.indexOf(widget) < 0:
                boundaries.add(type(widget).__name__)
                return
            rows = [[(f"dialog_{widget.buttonRole(button).name}_{index}", button, "button")
                     for index, button in enumerate(widget.buttons())]]
            adapter = WidgetsParameterAdapter(rows, parking=widget, visibility_root=panel)
            def commit_page():
                from ui.quick_editing import commit_quick_edits
                return commit_quick_edits(panel)
            adapter.validate_page = commit_page
            form = QuickParameterForm(adapter, owner)
            if layout.replaceWidget(widget, form) is None:
                form.shutdown()
                form.deleteLater()
                raise RuntimeError("Cannot replace the audited dialog button box")
            widget.hide()
            forms.append(form)
            migrated.update(id(button) for button in widget.buttons())
            return
        # A scroll/tab/stack owns pages outside its ordinary layout.
        if isinstance(widget, QScrollArea):
            if widget.widget():
                descend(widget.widget())
        elif isinstance(widget, (QTabWidget, QStackedWidget, QSplitter)):
            for index in range(widget.count()):
                descend(widget.widget(index))
        elif widget.layout() is not None:
            # Tables and scientific plots own implementation layouts. They
            # must remain opaque; only application-level owners are traversed.
            module = type(widget).__module__
            if module.startswith(("pyqtgraph", "matplotlib")) or "Table" in type(widget).__name__ or "Plot" in type(widget).__name__:
                boundaries.add(type(widget).__name__)
                return
            migrate(widget.layout(), widget)
            if (isinstance(widget, QGroupBox) and getattr(widget, "_quick_group_header", None)
                    and widget.property("quickCollapseWhenUnchecked")
                    and not hasattr(widget, "_quick_group_body_maximum")):
                # These opt-in groups already hide their optional contents on
                # toggle. Keep their native body at zero height when collapsed;
                # setting visible=False would also hide the sibling's source
                # field. Capture the limit after child migration, which may
                # legitimately release an old fixed-height owner.
                widget._quick_group_body_maximum = widget.maximumHeight()

                def update_group_body(expanded, group=widget):
                    group.setMaximumHeight(group._quick_group_body_maximum if expanded else 0)
                    group.updateGeometry()

                widget.toggled.connect(update_group_body)
                update_group_body(widget.isChecked())
        elif _kind(widget) is None:
            boundaries.add(type(widget).__name__)

    descend(panel)
    result = dict(page_id=page_id, forms=forms, migrated_fields=previous_count + len(migrated),
                  boundaries=sorted(boundaries))
    panel._quick_parameter_forms = result
    return result


def parameters_valid(panel, *, refresh=True):
    """Used by workflow admission before applying or starting parameter work."""
    coverage = getattr(panel, "_quick_parameter_forms", None)
    if coverage:
        coverage["forms"][:] = [form for form in coverage["forms"] if isValid(form)]
        if refresh:
            for form in coverage["forms"]:
                form.adapter.refresh()
    return not coverage or all(not field.invalid or not field.enabled or not field.visible for form in coverage["forms"]
                               for field in form.adapter._fields)


def rebind_parameter_sources(panel, replacements):
    """Retarget explicitly replaced source controls without rebuilding QML.

    The caller installs each new source in the retained source layout first.
    Field identity, focus, theme and the rendered surface remain unchanged.
    """
    coverage = getattr(panel, "_quick_parameter_forms", None)
    if not coverage:
        return []
    replacements = {id(old): new for old, new in replacements.items()}
    rebound = []
    for form in coverage["forms"]:
        if not isValid(form) or form.adapter._closed:
            continue
        for field, source in list(form.adapter._fields.items()):
            new = replacements.get(id(source))
            if new is None:
                continue
            if not isValid(new) or _kind(new) != field.kind:
                raise TypeError(f"Replacement for {field.fieldId} must retain its explicit scalar type")
            form.adapter.rebind_source(field, new)
            rebound.append(field.fieldId)
    return rebound


def commit_parameter_edits(panel):
    """Commit valid deferred drafts immediately before dispatching a command."""
    if not parameters_valid(panel):
        return False
    coverage = getattr(panel, "_quick_parameter_forms", None)
    if coverage:
        for form in coverage["forms"]:
            for field, draft in list(form.adapter._drafts.items()):
                if field.visible and field.enabled and not field.commit(draft):
                    return False
    return True
