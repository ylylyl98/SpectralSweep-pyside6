import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMainWindow, QComboBox, QLineEdit, QLabel, QListWidget, QPlainTextEdit, QPushButton
from app.experiment_metadata import ExperimentHistory
from app.sample_settings import SampleSettingsStore
from ui.main_window import MainWindow


class HistorySampleSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.history = ExperimentHistory(Path(self.tmp.name) / "history.sqlite")
        db = self.history._connect()
        db.execute("INSERT INTO experiments VALUES (?,?,?,?,?,?,?,?,?)", (
            "bo-run", "BO146", "dual_gate_sweep", "2026-09-23T18:20:37Z", None,
            "completed", "bo.json", "{}", "{}"))
        db.commit()
        db.close()
        self.host = MainWindow.__new__(MainWindow)
        QMainWindow.__init__(self.host)
        self.addCleanup(self.host.deleteLater)
        self.host._history = self.history
        self.host._sample_id_binder = SimpleNamespace(value="YZ365")

    def test_selector_includes_history_only_samples_and_filters_them(self):
        host = self.host
        host._sample_store = SampleSettingsStore()
        host._sample_store.save("YZ365", {})
        host._sample_selector = QComboBox(host)
        host._sample_selector.setEditable(True)
        host._sample_filter = QLineEdit(host)
        host._refresh_sample_selector()
        self.assertGreaterEqual(host._sample_selector.findText("BO146"), 0)
        self.assertEqual(host._sample_selector.currentText(), "YZ365")
        host._sample_filter.setText("bo1")
        host._refresh_sample_selector()
        self.assertEqual(host._sample_selector.count(), 1)
        self.assertEqual(host._sample_selector.itemText(0), "BO146")

    def test_switch_sample_resets_history_page_and_clamps_stale_page(self):
        host = self.host
        host._history_device = QLabel(host)
        host._history_type = QComboBox(host)
        host._history_type.addItem("dual_gate_sweep")
        host._history_filter = QLineEdit(host)
        host._history_list = QListWidget(host)
        host._history_preview = QPlainTextEdit(host)
        for name in ("_history_load", "_history_preview_btn", "_history_prev", "_history_next"):
            setattr(host, name, QPushButton(host))
        host._history_page_label = QLabel(host)
        host._history_page = 3
        host._history_sample = "YZ365"
        host._sample_id_binder.value = "BO146"
        host._refresh_history()
        self.assertEqual(host._history_page, 0)
        self.assertEqual(host._history_list.count(), 1)
        host._history_page = 99
        host._refresh_history()
        self.assertEqual(host._history_page, 0)
        self.assertEqual(host._history_list.count(), 1)
