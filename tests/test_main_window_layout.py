from __future__ import annotations

import inspect
import os
import copy
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QAction, QKeyEvent
from PySide6.QtWidgets import QApplication, QMainWindow, QPushButton, QSplitter, QWidget, QLineEdit, QTabWidget

from app.sample_settings import SampleSettingsStore
from ui.main_window import MainWindow, _SessionChangeWatcher
from ui.main_window import _SharedSampleIdBinder
from ui.presets_panel import PresetsPanel
from utils.config import cfg


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

    def test_sample_profile_filter_keeps_sample_settings_and_removes_globals(self):
        state = {
            "panels": {
                "settings": {"lf6": {"exposure_ms": 12}, "output": {"base_out": "global", "temperature": "3.6"}},
                "instruments": {"smu": {"vbg_resource": "GPIB::1", "termination": "\\n"}, "stage": {"address": "COM1", "jog": 0.2}},
            }
        }
        filtered = MainWindow._sample_scoped_state(state)
        self.assertEqual(filtered["panels"]["settings"]["lf6"]["exposure_ms"], 12)
        self.assertNotIn("base_out", filtered["panels"]["settings"]["output"])
        self.assertNotIn("vbg_resource", filtered["panels"]["instruments"]["smu"])
        self.assertNotIn("termination", filtered["panels"]["instruments"]["smu"])
        self.assertEqual(filtered["panels"]["instruments"]["stage"]["jog"], 0.2)
        self.assertIn("base_out", state["panels"]["settings"]["output"])

    def test_sample_switch_saves_outgoing_profile_before_restoring_target(self):
        host = MainWindow.__new__(MainWindow)
        QMainWindow.__init__(host)
        edits = [QLineEdit("YZ365")]
        host._sample_id_binder = _SharedSampleIdBinder(edits, initial="YZ365", commit_on_edit=True)
        host._sample_store = SampleSettingsStore()
        host._current_value = "draft-365"
        host._default_profile_state = {"panels": {"dual_gate": {"value": "defaults"}}}
        host._sample_restore_in_progress = False
        host._status = type("Status", (), {"showMessage": lambda *_args: None})()
        host._acquisition_active = lambda: False
        host._refresh_sample_selector = lambda: None
        host._restore_state = lambda state: setattr(
            host, "_current_value", state.get("panels", {}).get("dual_gate", {}).get("value")
        )
        host._persist_session = lambda: host._sample_store.save(
            host._sample_id_binder.value,
            {"panels": {"dual_gate": {"value": host._current_value}}},
        )
        self.addCleanup(host.deleteLater)

        self.assertTrue(MainWindow._commit_sample_selection(host, "YZ366"))
        self.assertEqual(host._sample_store.load("YZ365")["panels"]["dual_gate"]["value"], "draft-365")
        self.assertEqual(host._sample_store.load("YZ366")["panels"]["dual_gate"]["value"], "defaults")

    def test_real_main_window_round_trips_two_sample_drafts_without_hardware(self):
        original_cfg = copy.deepcopy(cfg.__dict__)
        window = MainWindow.__new__(MainWindow)
        QMainWindow.__init__(window)
        panel = None
        try:
            with patch.object(cfg, "save") as save:
                panel = PresetsPanel()
                tabs = QTabWidget(window)
                tabs.addTab(panel, "Dual Gate")
                window._tabs = tabs
                window._tab_ids = {panel: "dual_gate"}
                window._session_panels = {"dual_gate": panel}
                window._sample_store = SampleSettingsStore()
                window._sample_id_binder = _SharedSampleIdBinder(
                    [panel._sample_edit], initial="", commit_on_edit=True
                )
                window._sample_restore_in_progress = False
                window._default_profile_state = {"panels": {"dual_gate": panel.capture_session_state()}}
                window._status = type("Status", (), {"showMessage": lambda *_args: None})()
                window._refresh_sample_selector = lambda: None
                window._active_tab_id = lambda: "dual_gate"

                self.assertTrue(window._commit_sample_selection("__ui-test-a__"))
                panel._point_edit.setText("point-a")
                window._persist_session()
                self.assertTrue(window._commit_sample_selection("__ui-test-b__"))
                panel._point_edit.setText("point-b")
                window._persist_session()
                self.assertTrue(window._commit_sample_selection("__ui-test-a__"))

                self.assertEqual(panel._point_edit.text(), "point-a")
                self.assertEqual(window._sample_id_binder.value, "__ui-test-a__")
                self.assertGreaterEqual(save.call_count, 1)
        finally:
            if panel is not None:
                panel.deleteLater()
            window.deleteLater()
            self.app.processEvents()
            cfg.__dict__.clear()
            cfg.__dict__.update(original_cfg)

    def test_sample_switch_guard_covers_pending_spectrum_acquisition(self):
        host = MainWindow.__new__(MainWindow)
        host._power_sweep_running = False
        host._active_mcd_panel = None
        host._presets = host._mega = host._bfp = None
        host._spectrum = type("Spectrum", (), {
            "_pending_acquisition": "1d",
            "_continuous_mode": None,
            "_abort_btn": QPushButton(),
        })()
        self.assertTrue(MainWindow._acquisition_active(host))

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
