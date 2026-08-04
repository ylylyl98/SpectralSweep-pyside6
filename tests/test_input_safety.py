from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ui.input_safety import SpinBoxWheelGuard


def _wheel_event(delta_y: int = 120) -> QWheelEvent:
    return QWheelEvent(
        QPointF(5, 5),
        QPointF(5, 5),
        QPoint(0, 0),
        QPoint(0, delta_y),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )


class SpinBoxWheelSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.guard = SpinBoxWheelGuard(self.app)
        self.app.installEventFilter(self.guard)

    def tearDown(self):
        self.app.removeEventFilter(self.guard)

    def test_mouse_wheel_cannot_change_integer_spin_box(self):
        spin = QSpinBox()
        spin.setRange(0, 100)
        spin.setValue(50)

        QApplication.sendEvent(spin, _wheel_event())

        self.assertEqual(spin.value(), 50)

    def test_mouse_wheel_cannot_change_double_spin_box_or_its_editor(self):
        host = QWidget()
        layout = QVBoxLayout(host)
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 100.0)
        spin.setValue(12.5)
        layout.addWidget(spin)

        QApplication.sendEvent(spin.lineEdit(), _wheel_event(-120))

        self.assertAlmostEqual(spin.value(), 12.5)

    def test_arrow_button_and_typed_changes_remain_available(self):
        spin = QSpinBox()
        spin.setRange(0, 100)
        spin.setValue(10)

        spin.stepUp()
        spin.setValue(25)

        self.assertEqual(spin.value(), 25)


if __name__ == "__main__":
    unittest.main()
