"""Exercise the real shell's first show without hardware, network or user state."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


_STARTUP = r'''
import faulthandler
import importlib
from pathlib import Path
import sys
from unittest.mock import patch

faulthandler.enable(all_threads=True)
mode = sys.argv[1]
scratch = Path(sys.argv[2])
patches = []

def forbidden(*args, **kwargs):
    raise AssertionError('Hardware/network access forbidden during startup regression')

for target, replacement in (
    ('socket.socket.connect', forbidden), ('urllib.request.urlopen', forbidden),
    ('serial.tools.list_ports.comports', lambda *a, **k: []),
    ('pyvisa.ResourceManager', forbidden),
):
    handle = patch(target, replacement)
    handle.start()
    patches.append(handle)

for name in ('lf6_controller', 'smu_controller', 'rotation_controller',
             'stage_controller', 'esp32_stage_controller', 'pm100d_controller',
             'mff101_controller', 'magnet_controller', 'attodry2100_controller'):
    module = importlib.import_module('controllers.' + name)
    for value in vars(module).values():
        if isinstance(value, type) and value.__module__ == module.__name__:
            for method in ('connect_instrument', 'connect_async', 'connect', 'scan_axes'):
                if callable(getattr(value, method, None)):
                    handle = patch.object(value, method, forbidden)
                    handle.start()
                    patches.append(handle)

import ui.instrument_panel as instruments
for name, method in (('_SMUSection', '_on_refresh'), ('_RotationBlock', '_on_refresh'),
                     ('_StageSection', '_on_refresh'),
                     ('_ESP32ImagingStageSection', '_refresh_ports')):
    handle = patch.object(getattr(instruments, name), method, lambda *a, **k: None)
    handle.start()
    patches.append(handle)
handle = patch.object(instruments._VisaScanWorker, 'run', lambda self: self.finished.emit([]))
handle.start()
patches.append(handle)

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication
from ui.main_window import MainWindow
import ui.main_window as main_window_module
from utils.config import cfg

QSettings.setDefaultFormat(QSettings.Format.IniFormat)
for scope in (QSettings.Scope.UserScope, QSettings.Scope.SystemScope):
    QSettings.setPath(QSettings.Format.IniFormat, scope, str(scratch / 'qsettings'))
# Explicit organization/application constructors still choose NativeFormat on
# Windows, regardless of setDefaultFormat. Redirect that exact factory too.
main_window_module.QSettings = lambda *args: QSettings(
    str(scratch / 'window-settings.ini'), QSettings.Format.IniFormat)
cfg.filename.base_out = str(scratch / 'output')
cfg.save = lambda *a, **k: None

class NoNetworkNotifier:
    def set_url(self, *a, **k): pass
    def notify_warning(self, *a, **k): pass
    def shutdown(self): pass

app = QApplication([])
app.setProperty('spectralQuick', mode == 'quick')
app.setApplicationName('IsolatedMainWindowStartupTest')
app.setOrganizationName('IsolatedMainWindowStartupTest')
original_restore = MainWindow._restore_geometry
restored = []

def restore_after_migration(window):
    central = window.centralWidget()
    if mode == 'quick':
        workbench = getattr(window, '_quick_workbench', None)
        assert workbench is not None, 'Restoring geometry before Quick migration risks a Qt show crash'
        assert central is workbench.command_header.parentWidget()
    original_restore(window)
    assert window.centralWidget() is central
    restored.append(True)

window = MainWindow.__new__(MainWindow)
try:
    with patch.object(MainWindow, '_restore_geometry', restore_after_migration):
        MainWindow.__init__(window, notifier=NoNetworkNotifier(), ui_mode=mode)
    assert restored == [True]
    window.show()
    assert window.isVisible()
    QTimer.singleShot(100, app.quit)
    app.exec()
    window._source_changes.refresh()
    assert window._source_changes.pending() == [], window._source_changes.pending()
    assert window._source_status.text() == 'Code: up to date'
    expected_style = 'quick_application.qss' if mode == 'quick' else 'application.qss'
    assert window._source_changes.style_path == 'ui/styles/' + expected_style
    print('REAL_MAINWINDOW_FIRST_SHOW_OK', mode, flush=True)
finally:
    # Keep partial-construction QThreads alive until their disconnected owners
    # drain, including when the ordering assertion intentionally rejects a regression.
    for name in ('_quick_workbench', '_power_sweep', '_mcd2100', '_mcd',
                 '_magnet2100', '_magnet', '_lf6', '_smu', '_rot', '_stg',
                 '_imaging_stg', '_pm', '_mff101'):
        value = getattr(window, name, None)
        shutdown = getattr(value, 'shutdown', None)
        if callable(shutdown):
            shutdown()
'''


@pytest.mark.parametrize('mode', ['quick', 'widgets'])
def test_real_main_window_restores_geometry_after_migration_and_shows(tmp_path, mode):
    (tmp_path / 'config.json').write_text('{}', encoding='utf-8')
    environment = os.environ.copy()
    environment.update({
        'QT_QPA_PLATFORM': 'offscreen', 'QT_QUICK_BACKEND': 'software',
        'SPECTRALSWEEP_CONFIG_PATH': str(tmp_path / 'config.json'),
        'SPECTRALSWEEP_HISTORY_PATH': str(tmp_path / 'history.sqlite3'),
        'PROGRAMDATA': str(tmp_path / 'programdata'),
    })
    result = subprocess.run(
        [sys.executable, '-c', _STARTUP, mode, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], env=environment,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f'REAL_MAINWINDOW_FIRST_SHOW_OK {mode}' in result.stdout
