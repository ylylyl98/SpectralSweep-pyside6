"""Transfer Settings drafts without writing config, sending alerts or hardware calls."""
from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QCheckBox, QComboBox, QDoubleSpinBox, QLineEdit, QSpinBox


def settings_reload_block_reason(panel):
    if any(bool(getattr(panel._ctrl, name, False)) for name in
           ("is_busy", "switching_locked", "spectrum_actions_blocked")):
        return "Wait for instrument operations and settings to finish before reloading Settings."
    return ""


def capture_settings_state(panel):
    from ui.quick_editing import require_committed_quick_edits
    require_committed_quick_edits(panel)
    controls = {}
    for name, widget in vars(panel).items():
        if isinstance(widget, QLineEdit):
            controls[name] = ("text", widget.text())
        elif isinstance(widget, QComboBox):
            controls[name] = ("combo", widget.currentText())
        elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            controls[name] = ("value", widget.value())
        elif isinstance(widget, QCheckBox):
            controls[name] = ("checked", widget.isChecked())
    labels = {}
    for name in ("_status_lbl", "_lf6_status"):
        widget = getattr(panel, name)
        labels[name] = (widget.text(), widget.toolTip(), widget.styleSheet(),
                        {bytes(key).decode(): widget.property(bytes(key).decode())
                         for key in widget.dynamicPropertyNames()
                         if not bytes(key).startswith(b"_")})
    return {
        "controls": controls,
        "labels": labels,
        "category": panel._categories.currentIndex(),
        "scroll": [panel._categories.widget(i).verticalScrollBar().value()
                   for i in range(panel._categories.count())],
        "notification_name": panel._notifications.name_edit.text(),
        "notification_status": panel._notifications.status_label.text(),
    }


def restore_settings_state(panel, state):
    for name, (kind, value) in state["controls"].items():
        widget = getattr(panel, name)
        with QSignalBlocker(widget):
            if kind == "text":
                widget.setText(value)
            elif kind == "combo":
                widget.setCurrentText(value)
            elif kind == "value":
                widget.setValue(value)
            elif kind == "checked":
                widget.setChecked(value)
    for name, (text, tooltip, style, properties) in state["labels"].items():
        widget = getattr(panel, name)
        widget.setText(text)
        widget.setToolTip(tooltip)
        if style:
            widget.setStyleSheet(style)
        for key, value in properties.items():
            widget.setProperty(key, value)
        widget.style().unpolish(widget)
        widget.style().polish(widget)
    panel._categories.setCurrentIndex(state["category"])
    for i, value in enumerate(state["scroll"]):
        panel._categories.widget(i).verticalScrollBar().setValue(value)
    panel._notifications.name_edit.setText(state["notification_name"])
    panel._notifications.status_label.setText(state["notification_status"])
