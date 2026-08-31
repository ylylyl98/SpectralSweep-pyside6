from __future__ import annotations

import inspect
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QAction, QKeyEvent
from PySide6.QtWidgets import QApplication, QMainWindow, QPushButton, QSplitter, QWidget

from ui.main_window import MainWindow, _SessionChangeWatcher


class MainWindowLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _layout_stub(self):
        window = MainWindow.__new__(MainWindow)
        QMainWindow.__init__(window)
        window._sidebar_splitter = QSplitter(Qt.Orientation.Horizontal)
        window._sidebar_splitter.addWidget(QWidget())
        window._sidebar_splitter.addWidget(QWidget())
        window._sidebar_splitter.resize(1000, 600)
        window._sidebar_splitter.setSizes([320, 680])
        window._sidebar_width = 320
        window._sidebar_hide = QPushButton()
        window._sidebar_toggle_action = QAction(window)
        self.addCleanup(window.deleteLater)
        return window

    def test_session_change_watcher_only_tracks_input_inside_its_window(self):
        owner = QMainWindow()
        child = QWidget(owner)
        outsider = QWidget()
        changes = []
        watcher = _SessionChangeWatcher(owner, lambda: changes.append(True), owner)
        event = QKeyEvent(
            QEvent.Type.KeyRelease,
            Qt.Key.Key_A,
            Qt.KeyboardModifier.NoModifier,
        )

        watcher.eventFilter(outsider, event)
        self.assertEqual(changes, [])
        watcher.eventFilter(child, event)
        self.assertEqual(changes, [True])

        outsider.deleteLater()
        owner.deleteLater()

    def test_sidebar_collapses_and_restores_prior_width(self):
        window = self._layout_stub()
        MainWindow._toggle_sidebar(window)
        self.assertEqual(window._sidebar_splitter.sizes()[0], 0)
        self.assertEqual(window._sidebar_toggle_action.text(), "Show sidebar")
        MainWindow._toggle_sidebar(window)
        self.assertGreaterEqual(window._sidebar_splitter.sizes()[0], 240)
        self.assertEqual(window._sidebar_toggle_action.text(), "Hide sidebar")

    def test_sidebar_and_history_use_shared_splitter_backend(self):
        source = inspect.getsource(MainWindow)
        self.assertIn("MainSidebarSplitter", source)
        self.assertIn("ExperimentHistory()", source)
        self.assertIn("self._sample_id_binder.value", source)
        self.assertIn("self._history_panel", source)

    def test_experiment_history_collapses_independently_and_persists(self):
        window = MainWindow.__new__(MainWindow)
        QMainWindow.__init__(window)
        window._history_panel = QWidget()
        window._history_toggle = QPushButton()
        window._history_container = QWidget()
        window._sidebar_content_splitter = QSplitter(Qt.Orientation.Vertical)
        window._sidebar_content_splitter.addWidget(QWidget())
        window._sidebar_content_splitter.addWidget(window._history_container)
        window._sidebar_content_splitter.resize(320, 600)
        window._sidebar_content_splitter.setSizes([340, 260])
        window._history_height = 260
        self.addCleanup(window.deleteLater)

        with patch("ui.main_window.QSettings") as settings:
            MainWindow._set_history_collapsed(window, True)
            self.assertTrue(window._history_panel.isHidden())
            self.assertIn("▶", window._history_toggle.text())
            settings.return_value.setValue.assert_called_with("historyCollapsed", True)

        MainWindow._set_history_collapsed(window, False, persist=False)
        self.assertFalse(window._history_panel.isHidden())
        self.assertIn("▼", window._history_toggle.text())
        self.assertGreater(window._history_container.maximumHeight(), 1000)


if __name__ == "__main__":
    unittest.main()
