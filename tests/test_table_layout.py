"""Layout regressions: all columns stay reachable without horizontal scrolling."""
import contextlib
import io
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem

from ui.table_layout import fit_table_columns


class FittedTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.old_font = cls.app.font()
        for name in ("segoeui.ttf", "segoeuib.ttf"):
            path = Path("C:/Windows/Fonts") / name
            if path.exists():
                QFontDatabase.addApplicationFont(str(path))
        if "Segoe UI" in QFontDatabase.families():
            cls.app.setFont(QFont("Segoe UI", 9))

    @classmethod
    def tearDownClass(cls):
        cls.app.setFont(cls.old_font)

    def settle(self):
        for _ in range(8):
            self.app.processEvents()

    def assert_fits(self, table):
        self.settle()
        self.assertEqual(table.horizontalScrollBar().maximum(), 0)
        self.assertFalse(table.horizontalScrollBar().isVisible())
        self.assertLessEqual(table.horizontalHeader().length(), table.viewport().width())
        for column in range(table.columnCount()):
            if not table.isColumnHidden(column):
                left = table.columnViewportPosition(column)
                self.assertGreaterEqual(left, 0)
                self.assertLessEqual(left + table.columnWidth(column), table.viewport().width())

    def test_resize_hide_reorder_and_repopulate_keep_last_column_visible(self):
        table = QTableWidget(2, 12)
        self.addCleanup(table.close)
        fit_table_columns(table, (34, 104, 58, 120, 48, 50, 72, 72, 72, 72, 72, 72))
        table.show()
        for width in (1100, 850, 700, 1100):
            table.resize(width, 250)
            self.assert_fits(table)
        table.hideColumn(2)
        table.horizontalHeader().moveSection(11, 0)
        self.assert_fits(table)
        table.showColumn(2)
        table.setRowCount(30)  # introduces a vertical scrollbar
        table.setItem(0, 11, QTableWidgetItem("-12.345678"))
        self.assert_fits(table)
        self.assertEqual(table.item(0, 11).text(), "-12.345678")

    def test_long_cell_wraps_and_remains_editable(self):
        table = QTableWidget(1, 2)
        self.addCleanup(table.close)
        fit_table_columns(table, (100, 100))
        text = "Long condition label with several words to keep visible " * 3
        table.setItem(0, 1, QTableWidgetItem(text))
        table.resize(400, 400)
        table.show()
        self.assert_fits(table)
        self.assertGreater(table.rowHeight(0), table.verticalHeader().defaultSectionSize())
        self.assertTrue(table.item(0, 1).flags() & Qt.ItemFlag.ItemIsEditable)
        self.assertEqual(table.item(0, 1).text(), text)

    def test_pending_resize_is_safe_when_table_is_deleted(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            table = QTableWidget(1, 4)
            fit_table_columns(table, (20, 80, 80, 80))
            table.show()
            table.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.settle()
        self.assertNotIn("Traceback", errors.getvalue())

    def test_thousand_row_bulk_edit_coalesces_layout_and_uses_final_content(self):
        table = QTableWidget(1000, 12)
        self.addCleanup(table.close)
        controller = fit_table_columns(table, (80,) * 12)
        table.resize(950, 360)
        table.show()
        self.settle()
        fits = QSignalSpy(controller._timer.timeout)
        with patch.object(controller._timer, "start", wraps=controller._timer.start) as starts, \
                patch.object(table, "resizeRowToContents", wraps=table.resizeRowToContents) as resize_rows:
            # Real model signals from a full refill, rather than direct calls
            # to schedule, reproduce undo/repopulate on large Batch tables.
            for row in range(1000):
                for column in range(12):
                    table.setItem(row, column, QTableWidgetItem(f"{row}:{column}"))
            table.item(0, 4).setText("Long condition with several words " * 12)
            table.hideColumn(11)
            fit_table_columns(table, (30, 30, 30, 180) + (80,) * 8)
            table.resize(780, 360)
            self.assertLessEqual(starts.call_count, 1)
            self.assertEqual(fits.count(), 0)
            self.assert_fits(table)
            self.assertGreater(fits.count(), 0)
            self.assertLessEqual(fits.count(), 3)
            self.assertLessEqual(starts.call_count, 3)
            self.assertLessEqual(resize_rows.call_count, 3 * 61)
        self.assertGreater(table.rowHeight(0), table.verticalHeader().defaultSectionSize())
        self.assertGreater(table.columnWidth(3), table.columnWidth(1))
        self.assertTrue(table.isColumnHidden(11))
        self.assertEqual(table.item(999, 11).text(), "999:11")

    def test_queued_changes_while_hidden_fit_latest_geometry_on_show(self):
        table = QTableWidget(1, 3)
        self.addCleanup(table.close)
        controller = fit_table_columns(table, (80, 80, 80))
        self.settle()
        table.setItem(0, 1, QTableWidgetItem("Long text while hidden " * 20))
        table.hideColumn(2)
        fit_table_columns(table, (30, 140, 80))
        table.resize(650, 260)
        self.assertTrue(controller._timer.isActive())
        self.settle()
        table.resize(380, 260)
        table.show()
        self.assert_fits(table)
        self.assertGreater(table.columnWidth(1), table.columnWidth(0))
        self.assertGreater(table.rowHeight(0), table.verticalHeader().defaultSectionSize())

    def test_motion_preview_fits_at_its_minimum_width_and_preserves_selection(self):
        from ui.motion_sequence_preview import MotionSequencePreviewDialog
        rows = [{"sequence": 1, "condition_index": 1, "label": "long condition label",
                 "Vtg": -12.345, "Vbg": 1.234, "repeat": 2, "point_count": 1234}]
        dialog = MotionSequencePreviewDialog(rows)
        self.addCleanup(dialog.close)
        dialog.resize(700, 420)
        dialog.show()
        self.assert_fits(dialog._table)
        self.assertEqual(dialog.selected_rows()[0]["repeat"], 2)

    def test_optical_editors_fit_inside_cells_with_large_values(self):
        from ui.megasweep_panel import MegaSweepPanel
        panel = MegaSweepPanel()
        self.addCleanup(panel.close)
        panel._center_spin.setValue(2000)
        panel._exp_spin.setValue(600000)
        panel._frames_spin.setValue(1000)
        panel.resize(850, 850)
        panel.show()
        table = panel._optical_widget._table
        self.assert_fits(table)
        for column in range(2, 7):
            widget = table.cellWidget(0, column)
            self.assertLessEqual(widget.width(), table.columnWidth(column))
        self.assertEqual(panel._optical_widget.conditions()[0].exposure_ms, 600000)

    def test_motion_conditions_fit_in_sidebar_after_adding_rows(self):
        from ui.motion_conditions_widget import MotionConditionsWidget
        widget = MotionConditionsWidget()
        self.addCleanup(widget.close)
        widget.a.setText("1,2,3")
        widget.b.setText("4")
        widget.add_batch()
        widget.resize(400, 700)
        widget.show()
        self.assert_fits(widget.table)
        self.assertEqual(widget.table.rowCount(), 3)

    def test_calibration_and_ramp_dialog_tables_fit(self):
        from ui.broad_calibration_dialog import BroadCalibrationDialog
        from ui.wavelength_calibration_dialog import WavelengthCalibrationDialog
        from ui.mcd2100_panel import _RampTablesDialog
        row = SimpleNamespace(index=0, raw_range=1.23, raw_rate=0.01, error=None)
        report = SimpleNamespace(channel="test", **{
            kind: SimpleNamespace(kind=kind, reported_count=1, rows=[row], errors=[])
            for kind in ("current", "default")})
        for dialog in (BroadCalibrationDialog(), WavelengthCalibrationDialog(), _RampTablesDialog(report)):
            with self.subTest(dialog=type(dialog).__name__):
                self.addCleanup(dialog.close)
                dialog.resize(720, 650)
                dialog.show()
                for table in dialog.findChildren(QTableWidget):
                    # Hidden tab tables receive their final geometry on show.
                    if table.isVisible():
                        self.assert_fits(table)


if __name__ == "__main__":
    unittest.main()
