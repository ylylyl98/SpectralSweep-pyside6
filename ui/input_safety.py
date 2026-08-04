"""Application-wide guards against accidental mouse-wheel input changes."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QTabBar,
    QWidget,
)


class ValueControlWheelGuard(QObject):
    """Consume wheel input over numeric editors and selection controls.

    When the editor is inside a scrollable panel, the gesture continues to
    scroll that panel. Typing, arrow buttons, keyboard selection, and browsing
    an open combo-box popup remain available.
    """

    @staticmethod
    def _is_open_combo_popup_child(combo: QComboBox, watched: QWidget) -> bool:
        view = combo.view()
        return bool(
            view.isVisible()
            and (watched is view or view.isAncestorOf(watched))
        )

    @classmethod
    def _value_control_ancestor(cls, watched: QObject) -> Optional[QWidget]:
        widget = watched if isinstance(watched, QWidget) else None
        while widget is not None:
            if isinstance(widget, QAbstractSpinBox):
                return widget
            if isinstance(widget, QComboBox):
                if cls._is_open_combo_popup_child(widget, watched):
                    return None
                return widget
            if isinstance(widget, QTabBar):
                return widget
            widget = widget.parentWidget()
        return None

    @staticmethod
    def _scroll_parent(control: QWidget, event) -> None:
        widget = control.parentWidget()
        while widget is not None:
            if isinstance(widget, QAbstractScrollArea):
                vertical = widget.verticalScrollBar()
                horizontal = widget.horizontalScrollBar()
                pixel = event.pixelDelta()
                angle = event.angleDelta()
                use_horizontal = abs(pixel.x() or angle.x()) > abs(
                    pixel.y() or angle.y()
                )
                bar = horizontal if use_horizontal else vertical
                delta = (
                    pixel.x() if use_horizontal else pixel.y()
                )
                if not delta:
                    angle_delta = angle.x() if use_horizontal else angle.y()
                    delta = int(
                        round(
                            (float(angle_delta) / 120.0)
                            * max(24, bar.singleStep() * 3)
                        )
                    )
                if delta and bar.maximum() > bar.minimum():
                    bar.setValue(bar.value() - int(delta))
                    return
            widget = widget.parentWidget()

    def eventFilter(self, watched: QObject, event) -> bool:
        if event.type() != QEvent.Type.Wheel:
            return False
        control = self._value_control_ancestor(watched)
        if control is None:
            return False
        self._scroll_parent(control, event)
        event.accept()
        return True


SpinBoxWheelGuard = ValueControlWheelGuard


def install_input_wheel_guard(app: QApplication) -> ValueControlWheelGuard:
    """Install and retain the process-wide value-control wheel guard once."""
    attributes = (
        "_spectralsweep_input_wheel_guard",
        "_spectralsweep_spinbox_wheel_guard",
    )
    for attribute in attributes:
        existing = getattr(app, attribute, None)
        if isinstance(existing, ValueControlWheelGuard):
            for retained_attribute in attributes:
                setattr(app, retained_attribute, existing)
            return existing

    guard = ValueControlWheelGuard(app)
    app.installEventFilter(guard)
    for attribute in attributes:
        setattr(app, attribute, guard)
    return guard


def install_spinbox_wheel_guard(app: QApplication) -> ValueControlWheelGuard:
    """Compatibility wrapper for the former spin-box-only installer."""
    return install_input_wheel_guard(app)
