from __future__ import annotations

import base64
import ctypes
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import zlib

import pytest
from PySide6.QtGui import QIcon, QPixmapCache
from PySide6.QtWidgets import QApplication

from ui import application_identity as identity
from ui._app_icon_data import ICON_B85


ROOT = Path(__file__).resolve().parents[1]
ICON_PATH = ROOT / "assets" / "icons" / "spectralsweep.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def assert_original_icon(icon):
    assert sorted(size.width() for size in icon.availableSizes()) == list(SIZES)
    original = QIcon(str(ICON_PATH))
    for size in SIZES:
        actual = icon.pixmap(size, size)
        assert not actual.isNull()
        assert actual.toImage() == original.pixmap(size, size).toImage()


def test_embedded_icon_matches_source_asset():
    assert zlib.decompress(base64.b85decode(ICON_B85)) == ICON_PATH.read_bytes()


def test_icon_loads_from_an_unrelated_working_directory(application, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert_original_icon(identity.load_app_icon())


@pytest.mark.parametrize("contents", [None, b"invalid ico", ICON_PATH.read_bytes()[:100]])
def test_unavailable_or_corrupt_packaged_icon_uses_original_artwork(
    application, monkeypatch, tmp_path, caplog, contents,
):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    if contents is not None:
        icon_path = tmp_path / "assets" / "icons" / "spectralsweep.ico"
        icon_path.parent.mkdir(parents=True)
        icon_path.write_bytes(contents)
    assert_original_icon(identity.load_app_icon())
    assert "using embedded icon" in caplog.text


def test_decoded_sizes_survive_file_removal_and_qt_cache_eviction(
    application, monkeypatch, tmp_path,
):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    icon_path = tmp_path / "assets" / "icons" / "spectralsweep.ico"
    icon_path.parent.mkdir(parents=True)
    icon_path.write_bytes(ICON_PATH.read_bytes())
    icon = identity.load_app_icon()
    icon_path.unlink()
    QPixmapCache.clear()
    assert_original_icon(icon)


def test_missing_ico_decoder_uses_visible_system_icon(application, monkeypatch, caplog):
    monkeypatch.setattr(identity, "_decode_icon", lambda data: QIcon())
    icon = identity.load_app_icon()
    assert not icon.pixmap(32, 32).isNull()
    assert "using a system icon" in caplog.text


def test_failed_windows_identity_is_reported_without_aborting_startup(monkeypatch, caplog):
    setter = Mock(return_value=-2147467259)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(shell32=SimpleNamespace(
        SetCurrentProcessExplicitAppUserModelID=setter,
    )), raising=False)
    identity.set_windows_app_user_model_id()
    setter.assert_called_once_with(identity.APP_ID)
    assert "HRESULT 0x80004005" in caplog.text


def test_windows_identity_api_unavailable_does_not_abort_startup(monkeypatch, caplog):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(), raising=False)
    identity.set_windows_app_user_model_id()
    assert "Could not set Windows taskbar identity" in caplog.text


def test_other_platforms_do_not_load_windows_shell(monkeypatch):
    setter = Mock()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(shell32=SimpleNamespace(
        SetCurrentProcessExplicitAppUserModelID=setter,
    )), raising=False)
    identity.set_windows_app_user_model_id()
    setter.assert_not_called()


def test_taskbar_icon_is_persistent_and_repairs_corruption(application, tmp_path):
    path = identity._taskbar_icon_path(tmp_path / "图标 cache")
    assert path.read_bytes() == ICON_PATH.read_bytes()
    stamp = path.stat().st_mtime_ns
    assert identity._taskbar_icon_path(path.parent) == path
    assert path.stat().st_mtime_ns == stamp
    path.write_bytes(b"broken icon")
    assert identity._taskbar_icon_path(path.parent) == path
    assert path.read_bytes() == ICON_PATH.read_bytes()


def test_relaunch_command_preserves_modes_and_quotes_paths(monkeypatch, tmp_path):
    executable = tmp_path / "Python with spaces" / "python.exe"
    executable.parent.mkdir()
    executable.with_name("pythonw.exe").touch()
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "argv", ["main.py", "--mock", "--watch-styles"])
    assert identity._relaunch_command(False) == subprocess.list2cmdline([
        str(executable.with_name("pythonw.exe")), str(ROOT / "main.py"),
        "--mock", "--watch-styles",
    ])
    monkeypatch.setattr(sys, "argv", ["main.py", "--preview-child"])
    assert identity._relaunch_command(True) == subprocess.list2cmdline([
        str(executable.with_name("pythonw.exe")), str(ROOT / "main.py"), "--preview-spectrum",
    ])
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert identity._relaunch_command(True) == subprocess.list2cmdline([
        str(executable), "--preview-spectrum",
    ])


def test_unwritable_taskbar_icon_does_not_abort_startup(application, monkeypatch, caplog):
    from PySide6.QtWidgets import QWidget
    window = QWidget()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(QApplication, "platformName", lambda: "windows")
    monkeypatch.setattr(identity, "_taskbar_icon_path", Mock(side_effect=PermissionError("read only")))
    try:
        identity.configure_window_taskbar(window)
        assert not hasattr(window, "_taskbar_identity")
        assert "Could not prepare the Windows taskbar icon" in caplog.text
    finally:
        window.deleteLater()


@pytest.mark.skipif(sys.platform != "win32", reason="Checks real Windows window icons")
@pytest.mark.parametrize("entry_point", ["main", "preview"])
def test_entry_points_supply_native_taskbar_icons_without_external_assets(tmp_path, entry_point):
    # Use real Qt/Win32, but no visible windows, instruments, notifications or
    # production settings. A subprocess also gives each entry point a fresh AppID.
    script = r'''
import ctypes, os, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget
from ui.application_identity import APP_ID, PREVIEW_APP_ID
from ui import application_identity as identity
from ui.windows_taskbar import read_properties
from ui import windows_taskbar

original_icon_path = identity._taskbar_icon_path
identity._taskbar_icon_path = lambda: original_icon_path(Path.cwd() / 'persistent 图标 cache')
property_writes = []
original_write = windows_taskbar.write_properties
def record_write(hwnd, values):
    property_writes.append((hwnd, values.copy()))
    original_write(hwnd, values)
windows_taskbar.write_properties = record_write

entry = sys.argv[1]
sys.argv = ['main.py']
sys._MEIPASS = str(Path.cwd() / 'missing-assets')
original_show = QWidget.show
def hidden_show(window):
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    original_show(window)
QWidget.show = hidden_show

def check_identity():
    shell32 = ctypes.WinDLL('shell32')
    getter = shell32.GetCurrentProcessExplicitAppUserModelID
    getter.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    getter.restype = ctypes.c_long
    value = ctypes.c_void_p()
    assert getter(ctypes.byref(value)) == 0
    try:
        assert ctypes.wstring_at(value) == (APP_ID if entry == 'main' else PREVIEW_APP_ID)
    finally:
        free = ctypes.WinDLL('ole32').CoTaskMemFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = None
        free(value)

def exercise(self):
    check_identity()
    window = next(w for w in self.topLevelWidgets() if isinstance(w, QMainWindow))
    user32 = ctypes.WinDLL('user32')
    get_icon = user32.SendMessageW
    get_icon.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    get_icon.restype = ctypes.c_ssize_t
    for _ in range(3):
        self.processEvents()
        hwnd = int(window.winId())
        assert get_icon(hwnd, 0x007F, 0, 0), 'Windows small icon is missing'
        assert get_icon(hwnd, 0x007F, 1, 0), 'Windows big icon is missing'
        properties = read_properties(hwnd)
        assert properties['app_id'] == (APP_ID if entry == 'main' else PREVIEW_APP_ID)
        assert properties['name'] == ('SpectralSweep' if entry == 'main' else 'SpectralSweep Preview')
        assert properties['icon'].endswith(',0')
        icon_path = Path(properties['icon'][:-2])
        assert icon_path.is_file()
        assert 'missing-assets' not in str(icon_path)
        assert properties['command'] == identity._relaunch_command(entry == 'preview')
        assert '--preview-child' not in properties['command']
        for size in (16, 32, 48, 256):
            assert not window.windowIcon().pixmap(size, size).isNull()
        window.hide()
        window.show()
    # Native-handle recreation must also restore Explorer's metadata.
    old_hwnd = int(window.winId())
    window.destroy()
    assert any(hwnd == old_hwnd and all(value is None for value in values.values())
               for hwnd, values in property_writes), 'Properties were not released before destruction'
    window.show()
    self.processEvents()
    assert read_properties(int(window.winId())) == properties
    # Verify explicit cleanup, not just that a non-null HICON exists.
    window._taskbar_identity.clear()
    assert all(value is None for value in read_properties(int(window.winId())).values())
    print('native icons verified:', entry)
    return 0
QApplication.exec = exercise

if entry == 'main':
    class MainWindow(QMainWindow):
        notification_configured = Signal()
        def __init__(self, **kwargs):
            check_identity()  # Must be in place before a native window is created.
            super().__init__()
    sys.modules['ui.main_window'] = SimpleNamespace(MainWindow=MainWindow)
    sys.modules['app.ntfy_notifications'] = SimpleNamespace(NtfyNotifier=Mock(), NTFY_URL='')
    sys.modules['app.runtime_exception_reporting'] = SimpleNamespace(install=Mock(return_value=Mock()))
    sys.modules['app.process_watchdog'] = SimpleNamespace(WatchdogSession=Mock())
    import main
    main.main()
else:
    from app.ui_preview import prepare_preview_environment, run_preview_child
    os.environ.update(prepare_preview_environment(Path.cwd() / 'preview'))
    raise SystemExit(run_preview_child())
'''
    env = dict(os.environ, QT_QPA_PLATFORM="windows", PYTHONPATH=str(ROOT))
    result = subprocess.run(
        [sys.executable, "-c", script, entry_point], env=env, cwd=tmp_path,
        capture_output=True, text=True, timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"native icons verified: {entry_point}" in result.stdout
