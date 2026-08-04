"""Application-wide guards against accidental numeric input changes."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QWidget,
)


class SpinBoxWheelGuard(QObject):
    """Consume wheel input over spin boxes without changing their values.

    When the editor is inside a scrollable panel, the gesture continues to
    scroll that panel. Typing and the spin-box arrow buttons remain available.
    """

    @staticmethod
    def _spin_box_ancestor(watched: QObject) -> Optional[QAbstractSpinBox]:
        widget = watched if isinstance(watched, QWidget) else None
        while widget is not None:
            if isinstance(widget, QAbstractSpinBox):
                return widget
            widget = widget.parentWidget()
        return None

    @staticmethod
    def _scroll_parent(spin_box: QAbstractSpinBox, event) -> None:
        widget = spin_box.parentWidget()
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
        spin_box = self._spin_box_ancestor(watched)
        if spin_box is None:
            return False
        self._scroll_parent(spin_box, event)
        event.accept()
        return True


def install_spinbox_wheel_guard(app: QApplication) -> SpinBoxWheelGuard:
    """Install and retain the process-wide spin-box wheel guard once."""
    attribute = "_spectralsweep_spinbox_wheel_guard"
    existing = getattr(app, attribute, None)
    if isinstance(existing, SpinBoxWheelGuard):
        return existing
    guard = SpinBoxWheelGuard(app)
    app.installEventFilter(guard)
    setattr(app, attribute, guard)
    return guard
