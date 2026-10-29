"""Exercise the real entry point in a disposable process, with no device I/O."""
from pathlib import Path
import os
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
_QSETTINGS = """
from pathlib import Path
import PySide6.QtCore as QtCore
_NativeQSettings = QtCore.QSettings
_settings_root = Path(os.environ['STARTUP_QSETTINGS_PATH']).resolve()
_settings_root.mkdir(parents=True, exist_ok=True)
class IsolatedQSettings(_NativeQSettings):
    def __init__(self, *args, **kwargs):
        # setDefaultFormat/setPath do not redirect the explicit organization/
        # application overload. Force an INI filename for every construction.
        super().__init__(str(_settings_root / 'startup.ini'), _NativeQSettings.Format.IniFormat)
        assert Path(self.fileName()).resolve().is_relative_to(_settings_root), self.fileName()
QtCore.QSettings = QSettings = IsolatedQSettings
assert QSettings('SpectralSweep', 'SpectralSweep').format() == _NativeQSettings.Format.IniFormat
print('QSETTINGS_ISOLATED', QSettings('SpectralSweep', 'SpectralSweep').fileName(), flush=True)
"""

_SEED_MAXIMIZED = """
import os
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMainWindow
""" + _QSETTINGS + """
app = QApplication([])
window = QMainWindow()
window.resize(1000, 700)
window.setWindowState(Qt.WindowState.WindowMaximized)
settings = QSettings('SpectralSweep', 'SpectralSweep')
settings.setValue('geometry', window.saveGeometry())
settings.setValue('windowState', window.saveState())
settings.sync()
"""

_STARTUP_PROBE = r"""
import faulthandler
import importlib
import json
import os
from pathlib import Path
import sys
import threading
import traceback
from unittest.mock import patch

faulthandler.enable(all_threads=True)
faulthandler.dump_traceback_later(20, repeat=False)
sys.path.insert(0, str(Path.cwd()))
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
""" + _QSETTINGS + r"""
failures, windows, watchdog_events = [], [], []

def report_error(kind, value, tb):
    failures.append(str(value))
    traceback.print_exception(kind, value, tb)
    app = QApplication.instance()
    if app is not None:
        app.exit(2)

sys.excepthook = report_error
threading.excepthook = lambda event: report_error(event.exc_type, event.exc_value, event.exc_traceback)

def forbidden(*args, **kwargs):
    raise AssertionError('Startup attempted device/network access')

# Retain real controllers and worker threads, but make every connection and
# resource-enumeration boundary fail before a driver or socket can be reached.
patches = []
for target, replacement in (
    ('socket.socket.connect', forbidden), ('urllib.request.urlopen', forbidden),
    ('serial.tools.list_ports.comports', lambda *a, **k: []),
    ('pyvisa.ResourceManager', forbidden),
):
    handle = patch(target, replacement)
    handle.start()
    patches.append(handle)
for module_name in ('lf6_controller', 'smu_controller', 'rotation_controller',
                    'stage_controller', 'esp32_stage_controller', 'pm100d_controller',
                    'mff101_controller', 'magnet_controller', 'attodry2100_controller'):
    module = importlib.import_module('controllers.' + module_name)
    for value in vars(module).values():
        if isinstance(value, type) and value.__module__ == module.__name__:
            for name in ('connect_instrument', 'connect_async', 'connect', 'scan_axes'):
                if callable(getattr(value, name, None)):
                    handle = patch.object(value, name, forbidden)
                    handle.start()
                    patches.append(handle)

import ui.instrument_panel as instruments
for class_name, name in (('_SMUSection', '_on_refresh'), ('_RotationBlock', '_on_refresh'),
                         ('_StageSection', '_on_refresh'), ('_ESP32ImagingStageSection', '_refresh_ports')):
    handle = patch.object(getattr(instruments, class_name), name, lambda *a, **k: None)
    handle.start()
    patches.append(handle)
handle = patch.object(instruments._VisaScanWorker, 'run', lambda self: self.finished.emit([]))
handle.start()
patches.append(handle)

class QuietNotifier:
    def set_url(self, *args, **kwargs):
        pass
    def notify_warning(self, *args, **kwargs):
        pass
    def notify_crash(self, *args, **kwargs):
        pass
    def shutdown(self):
        pass

class QuietWatchdog:
    def __init__(self, *args, **kwargs):
        pass
    def start(self):
        watchdog_events.append('start')
    def beat(self):
        pass
    def enable(self, *args):
        pass
    def close(self, *, normal_exit=False):
        watchdog_events.append('normal' if normal_exit else 'cleanup')

import app.ntfy_notifications as notifications
import app.process_watchdog as watchdog
notifications.NtfyNotifier = QuietNotifier
watchdog.WatchdogSession = QuietWatchdog
for name in ('critical', 'warning', 'question'):
    handle = patch.object(QMessageBox, name, forbidden)
    handle.start()
    patches.append(handle)

from utils.config import cfg
cfg.filename.base_out = str(Path(os.environ['STARTUP_QSETTINGS_PATH']).parent / 'output')
import ui.main_window as window_module
RealMainWindow = window_module.MainWindow

def construct(*args, **kwargs):
    window = RealMainWindow(*args, **kwargs)
    windows.append(window)
    print('REAL_MAINWINDOW_CONSTRUCTED', flush=True)
    assert type(window._lf6).__name__ == 'LF6Controller'
    assert window._lf6.sensor_readout_intents == {}
    assert not window._lf6.sensor_preflight_pending
    def close_normally():
        assert window.isVisible()
        assert window.isMaximized()
        assert window._quick_workbench is not None
        print('REAL_MAINWINDOW_VISIBLE_MAXIMIZED', flush=True)
        assert window.close(), 'Real MainWindow rejected normal shutdown'
    QTimer.singleShot(400, window, close_normally)
    return window

window_module.MainWindow = construct
import main
sys.argv = ['main.py', '--ui', 'quick']
try:
    main.main()
except SystemExit as result:
    assert result.code == 0, result.code
    assert not failures, failures
    assert len(windows) == 1 and not windows[0].isVisible()
    assert watchdog_events == ['start', 'normal', 'cleanup'], watchdog_events
    print('REAL_MAIN_ENTRY_CLOSED_NORMALLY', flush=True)
    faulthandler.cancel_dump_traceback_later()
else:
    raise AssertionError('main.main did not exit through its normal event loop')
"""


@pytest.mark.parametrize("saved_maximized", [False, True], ids=["first-quick-maximize", "saved-maximized"])
def test_real_main_entry_shows_and_closes_maximized_window(tmp_path, saved_maximized):
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")  # Prevent fallback to repository/user configuration.
    environment = dict(os.environ)
    environment.update({
        "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software",
        "SPECTRALSWEEP_CONFIG_PATH": str(config),
        "SPECTRALSWEEP_HISTORY_PATH": str(tmp_path / "history.sqlite3"),
        "STARTUP_QSETTINGS_PATH": str(tmp_path / "qsettings"),
        "PROGRAMDATA": str(tmp_path / "programdata"),
    })
    environment.pop("QSG_RHI_BACKEND", None)
    if saved_maximized:
        seed = subprocess.run([sys.executable, "-X", "faulthandler", "-u", "-c", _SEED_MAXIMIZED],
                              cwd=ROOT, env=environment, capture_output=True, text=True, timeout=20)
        assert seed.returncode == 0, seed.stdout + seed.stderr
    try:
        result = subprocess.run([sys.executable, "-X", "faulthandler", "-u", "-c", _STARTUP_PROBE],
                                cwd=ROOT, env=environment, capture_output=True, text=True, timeout=35)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"Real main entry did not close within 35 seconds: {exc.stdout!r}\n{exc.stderr!r}")
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"Main entry exited {result.returncode}:\n{output}"
    assert "REAL_MAINWINDOW_VISIBLE_MAXIMIZED" in output
    assert "REAL_MAIN_ENTRY_CLOSED_NORMALLY" in output
    assert "QSETTINGS_ISOLATED" in output and tmp_path.as_posix() in output
