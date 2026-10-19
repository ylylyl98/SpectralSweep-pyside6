from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QElapsedTimer
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from app.ui_preview import PROJECT_ROOT, RELOAD_EXIT_CODE, prepare_preview_environment, run_preview
from ui.application_style import ApplicationStyle


class StyleReloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "application.qss"
        self.path.write_text("QLineEdit { color: #123456; }", encoding="utf-8")
        self.original = self.app.styleSheet()
        self.manager = ApplicationStyle(self.app, self.path)
        self.edit = QLineEdit("unsaved parameter")
        self.edit.show()

    def tearDown(self):
        self.manager.set_watching(False)
        self.manager.deleteLater()
        self.edit.close()
        self.edit.deleteLater()
        self.app.setStyleSheet(self.original)
        self.app.processEvents()
        self.temp.cleanup()

    def wait_for(self, predicate):
        timer = QElapsedTimer()
        timer.start()
        while not predicate() and timer.elapsed() < 4000:
            QTest.qWait(25)
        self.assertTrue(predicate())

    def test_reload_changes_existing_widget_without_replacing_its_value_or_connections(self):
        changes = []
        self.edit.textChanged.connect(changes.append)
        self.assertTrue(self.manager.reload())
        self.assertEqual(self.edit.palette().color(QPalette.ColorRole.Text).name(), "#123456")
        self.path.write_text("QLineEdit { color: #654321; font-size: 14px; }", encoding="utf-8")
        self.assertTrue(self.manager.reload())
        self.assertEqual(self.edit.palette().color(QPalette.ColorRole.Text).name(), "#654321")
        self.assertEqual(self.edit.text(), "unsaved parameter")
        self.assertEqual(changes, [])
        self.edit.setText("changed")
        self.assertEqual(changes, ["changed"])

    def test_incomplete_or_missing_file_preserves_last_style(self):
        self.manager.reload()
        old = self.app.styleSheet()
        for source in ("", "QLineEdit { color: red;", "/* unfinished", "QWidget {{}}"):
            self.path.write_text(source, encoding="utf-8")
            self.assertFalse(self.manager.reload())
            self.assertEqual(self.app.styleSheet(), old)
        self.path.unlink()
        self.assertFalse(self.manager.reload())
        self.assertEqual(self.app.styleSheet(), old)

    def test_watch_survives_atomic_replace_delete_and_recreate(self):
        self.manager.reload()
        self.manager.set_watching(True)
        replacement = self.path.with_suffix(".new")
        replacement.write_text("QLineEdit { color: #234567; }", encoding="utf-8")
        replacement.replace(self.path)
        self.wait_for(lambda: "#234567" in self.app.styleSheet())
        self.path.unlink()
        self.wait_for(lambda: bool(self.manager.last_error))
        self.assertIn("#234567", self.app.styleSheet())
        self.path.write_text("QLineEdit { color: #345678; }", encoding="utf-8")
        self.wait_for(lambda: "#345678" in self.app.styleSheet())
        self.assertFalse(self.manager.last_error)

    def test_main_window_menu_refreshes_styles_without_building_controllers(self):
        from PySide6.QtWidgets import QMainWindow, QStatusBar
        from ui.main_window import MainWindow
        window = MainWindow.__new__(MainWindow)
        QMainWindow.__init__(window)
        window._status = QStatusBar(window)
        window.setStatusBar(window._status)
        window._application_style = self.manager
        window._initialize_style_actions()
        try:
            action = next(a for a in window.menuBar().actions()[0].menu().actions()
                          if a.objectName() == "ReloadApplicationStyles")
            action.trigger()
            self.assertIn("#123456", self.app.styleSheet())
            self.assertEqual(window._status.currentMessage(), "Styles reloaded")
            self.assertEqual(action.shortcut().toString(), "Ctrl+Alt+R")
        finally:
            window.deleteLater()


class PreviewIsolationTests(unittest.TestCase):
    def test_supervisor_restarts_only_preview_and_cleans_its_scratch_directory(self):
        calls = []

        def child(command, *, env, cwd):
            calls.append((command, env, Path(cwd)))
            config = json.loads(Path(env["SPECTRALSWEEP_CONFIG_PATH"]).read_text())
            self.assertEqual(config["session"]["sample_id"], "PREVIEW")
            return subprocess.CompletedProcess(command, RELOAD_EXIT_CODE if len(calls) == 1 else 0)

        with patch("app.ui_preview.subprocess.run", side_effect=child):
            self.assertEqual(run_preview(), 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertIn("--preview-child", calls[0][0])
        self.assertFalse(calls[0][2].exists())

    def test_real_preview_process_uses_only_simulator_and_private_settings(self):
        script = r'''
import json, os, sys
from pathlib import Path
from PySide6.QtCore import QTimer
import app.ui_preview as preview
from PySide6.QtWidgets import QApplication

def exercise():
    try:
        window = next(w for w in QApplication.topLevelWidgets() if hasattr(w, 'controller'))
        from utils.config import cfg, _CONFIG_FILE
        assert _CONFIG_FILE == Path(os.environ[preview.PREVIEW_ROOT_ENV]) / 'config.json'
        assert cfg.session.sample_id == 'PREVIEW'
        cfg.save()
        from app.experiment_metadata import ExperimentHistory
        history = ExperimentHistory()
        assert history.path == Path(os.environ[preview.PREVIEW_ROOT_ENV]) / 'history.sqlite3'
        assert not any(n == 'controllers' or n.startswith('controllers.') for n in sys.modules)
        assert 'ui.main_window' not in sys.modules
        assert 'app.ntfy_notifications' not in sys.modules
        assert len(window.panel._references) == 2
        original = window.panel._last_data.copy()
        window.panel._exposure.setValue(300)
        window._style.reload()
        assert window.panel._exposure.value() == 300
        assert (window.panel._last_data == original).all()
        window.controller.apply_settings(300, 800, 1)
        window.controller._timer.stop()
        window.controller._complete()
        window.controller.acquire_2d()
        window.controller._timer.stop()
        window.controller._complete()
        assert window.panel._last_data.shape == (128, 1024)
        window.controller.acquire_single()
        window.controller.abort_acquisition()
        assert not window.controller._timer.isActive()
        window.controller.set_connected(False)
        assert not window.panel._acquire_btn.isEnabled()
        window.controller.set_connected(True)
        assert window.panel._acquire_btn.isEnabled()
        window.show_sample_error()
        assert 'Simulated acquisition failure' in window.panel._error_details.toPlainText()
        Path(os.environ['PREVIEW_RESULT']).write_text('passed')
        window.reload_preview()
    except BaseException:
        import traceback
        traceback.print_exc()
        QApplication.instance().exit(99)

# Install the exercise after the window has been built, without importing it
# before run_preview_child has isolated config/QSettings.
original_exec = QApplication.exec
def timed_exec(self):
    QTimer.singleShot(250, exercise)
    QTimer.singleShot(10000, lambda: self.exit(98))
    return original_exec()
QApplication.exec = timed_exec
raise SystemExit(preview.run_preview_child())
'''
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = prepare_preview_environment(root / "preview")
            sentinel = root / "production.json"
            sentinel.write_text('{"production": true}')
            env["SPECTRALSWEEP_CONFIG_PATH"] = str(sentinel)
            env["QT_QPA_PLATFORM"] = "offscreen"
            env["PYTHONPATH"] = str(PROJECT_ROOT)
            env["PREVIEW_RESULT"] = str(root / "result.txt")
            result = subprocess.run([sys.executable, "-c", script], env=env, cwd=root,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, RELOAD_EXIT_CODE, result.stdout + result.stderr)
            self.assertEqual((root / "result.txt").read_text(), "passed")
            self.assertEqual(sentinel.read_text(), '{"production": true}')


if __name__ == "__main__":
    unittest.main()
