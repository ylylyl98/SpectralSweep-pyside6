import os
from pathlib import Path
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QDoubleSpinBox, QFormLayout, QGridLayout, QLabel, QLineEdit, QWidget,
)
from ui.application_style import STYLE_PATH, read_style
from ui.workflow_sections import compact_form_rows, divide_grid_fields


class WorkflowRowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.font, cls.style = cls.app.font(), cls.app.styleSheet()
        for name in ("segoeui.ttf", "segoeuib.ttf"):
            path = Path("C:/Windows/Fonts") / name
            if path.is_file():
                QFontDatabase.addApplicationFont(str(path))
        if "Segoe UI" in QFontDatabase.families():
            cls.app.setFont(QFont("Segoe UI", 9))
        cls.app.setStyleSheet(read_style(STYLE_PATH))

    @classmethod
    def tearDownClass(cls):
        cls.app.setFont(cls.font)
        cls.app.setStyleSheet(cls.style)

    def settle(self):
        for _ in range(8):
            self.app.processEvents()

    def test_compact_form_fields_remain_visible_and_reflow_with_values_intact(self):
        for above in (False, True):
            with self.subTest(labels_above=above):
                host = QWidget()
                self.addCleanup(host.close)
                form = QFormLayout(host)
                fields = [QDoubleSpinBox() for _ in range(3)]
                labels = []
                for name, field in zip(("Vbg", "Vtg", "Vbias"), fields):
                    field.setRange(-200, 200)
                    field.setDecimals(3)
                    field.setSuffix(" V")
                    field.setValue(-123.456)
                    form.addRow(name, field)
                    labels.append(form.labelForField(field))
                changes = []
                fields[0].valueChanged.connect(changes.append)
                row = compact_form_rows(form, fields, labels_above=above)
                host.resize(700, 300)
                host.show()
                self.settle()
                for width, stacked in ((700, False), (205, True), (700, False)):
                    host.resize(width, 300)
                    self.settle()
                    positions = [field.mapTo(row, QPoint()) for field in fields]
                    self.assertEqual(len({p.y() for p in positions}) > 1, stacked)
                    for label, field in zip(labels, fields):
                        self.assertTrue(label.isVisible())
                        self.assertTrue(field.isVisible())
                        self.assertIs(label.buddy(), field)
                        self.assertGreaterEqual(field.height(), field.minimumSizeHint().height())
                        self.assertTrue(row.rect().contains(QRect(field.mapTo(row, QPoint()), field.size())))
                        self.assertEqual(field.value(), -123.456)
                    self.assertTrue(all(div.isVisible() != stacked for div in row._dividers))
                fields[0].setValue(20)
                self.assertEqual(changes, [20])
                row.hide()
                self.settle()
                self.assertTrue(all(not field.isVisible() for field in fields))
                row.show()
                self.settle()
                self.assertTrue(all(field.isVisible() for field in fields))
                # The application loads/reloads QSS after panels are built.
                # Re-measure chrome so a padded spinbox never loses its value.
                host.setStyleSheet("QDoubleSpinBox { padding: 8px 12px; }")
                host.resize(600, 300)
                self.settle()
                for field in fields:
                    self.assertGreaterEqual(field.width(), field.minimumSizeHint().width())
                    self.assertGreaterEqual(field.height(), field.minimumSizeHint().height())

    def test_grid_divider_preserves_spanning_paths_and_paired_fields(self):
        host = QWidget()
        self.addCleanup(host.close)
        grid = QGridLayout(host)
        path, left, right = QLineEdit("sample.csv"), QLineEdit("3"), QLineEdit("9")
        grid.addWidget(path, 0, 0, 1, 4)
        grid.addWidget(QLabel("Minimum"), 1, 0)
        grid.addWidget(left, 1, 1)
        grid.addWidget(QLabel("Maximum"), 1, 2)
        grid.addWidget(right, 1, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        divide_grid_fields(grid, 2, (1,))
        host.resize(600, 160)
        host.show()
        self.settle()
        self.assertEqual(grid.getItemPosition(grid.indexOf(path)), (0, 0, 1, 5))
        self.assertEqual(left.y(), right.y())
        self.assertGreater(right.x(), left.geometry().right())
        self.assertEqual(grid.columnStretch(4), 1)
        self.assertEqual((path.text(), left.text(), right.text()), ("sample.csv", "3", "9"))
