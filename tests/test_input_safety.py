from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ui.input_safety import ValueControlWheelGuard, install_input_wheel_guard


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


class ValueControlWheelSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.guard = ValueControlWheelGuard(self.app)
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

    def test_mouse_wheel_cannot_change_combo_box_selection(self):
        combo = QComboBox()
        combo.addItems(["First", "Second", "Third"])
        combo.setCurrentIndex(1)

        QApplication.sendEvent(combo, _wheel_event())

        self.assertEqual(combo.currentIndex(), 1)

        combo.setEditable(True)
        QApplication.sendEvent(combo.lineEdit(), _wheel_event(-120))

        self.assertEqual(combo.currentIndex(), 1)

    def test_mouse_wheel_cannot_change_selected_tab(self):
        tabs = QTabWidget()
        for label in ("First", "Second", "Third"):
            tabs.addTab(QWidget(), label)
        tabs.setCurrentIndex(1)

        QApplication.sendEvent(tabs.tabBar(), _wheel_event())

        self.assertEqual(tabs.currentIndex(), 1)

    def test_open_combo_popup_keeps_wheel_browsing(self):
        combo = QComboBox()
        combo.addItems([f"Item {index}" for index in range(30)])
        combo.setMaxVisibleItems(5)
        combo.show()
        combo.showPopup()
        self.app.processEvents()

        view = combo.view()
        self.assertTrue(view.isVisible())
        self.assertFalse(
            self.guard.eventFilter(view.viewport(), _wheel_event(-120))
        )

        combo.hidePopup()
        combo.close()

    def test_wheel_over_control_scrolls_enclosing_panel(self):
        scroll = QScrollArea()
        content = QWidget()
        layout = QVBoxLayout(content)
        combo = QComboBox()
        combo.addItems(["First", "Second"])
        layout.addWidget(combo)
        spacer = QWidget()
        spacer.setMinimumHeight(1000)
        layout.addWidget(spacer)
        scroll.setWidget(content)
        scroll.resize(240, 160)
        scroll.show()
        self.app.processEvents()

        scrollbar = scroll.verticalScrollBar()
        self.assertGreater(scrollbar.maximum(), 0)
        scrollbar.setValue(0)
        QApplication.sendEvent(combo, _wheel_event(-120))

        self.assertEqual(combo.currentIndex(), 0)
        self.assertGreater(scrollbar.value(), 0)
        scroll.close()

    def test_arrow_button_and_typed_changes_remain_available(self):
        spin = QSpinBox()
        spin.setRange(0, 100)
        spin.setValue(10)

        spin.stepUp()
        spin.setValue(25)

        self.assertEqual(spin.value(), 25)

    def test_installer_is_idempotent(self):
        first = install_input_wheel_guard(self.app)
        try:
            second = install_input_wheel_guard(self.app)
            self.assertIs(first, second)
        finally:
            self.app.removeEventFilter(first)
            for attribute in (
                "_spectralsweep_input_wheel_guard",
                "_spectralsweep_spinbox_wheel_guard",
            ):
                if getattr(self.app, attribute, None) is first:
                    delattr(self.app, attribute)


if __name__ == "__main__":
    unittest.main()
