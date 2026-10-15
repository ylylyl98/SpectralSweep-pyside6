"""Application identity shared by the measurement and isolated preview entry points."""
from __future__ import annotations

import base64
import hashlib
import logging
from pathlib import Path
import subprocess
import sys
import zlib

from PySide6.QtCore import (
    QBuffer, QByteArray, QEvent, QIODevice, QObject, QSaveFile, QStandardPaths, QTimer, Qt,
)
from PySide6.QtGui import QIcon, QImageReader, QPixmap, QPlatformSurfaceEvent
from PySide6.QtWidgets import QApplication, QStyle, QWidget


APP_NAME = "SpectralSweep"
APP_ID = "SpectralSweep.SpectralSweep.Desktop"
PREVIEW_APP_ID = "SpectralSweep.SpectralSweep.Preview"
_ROOT = Path(__file__).resolve().parents[1]
_LOG = logging.getLogger(__name__)


def set_windows_app_user_model_id(app_id: str = APP_ID) -> None:
    """Call before QApplication so Windows can group our windows separately from Python."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        setter = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        setter.argtypes = [ctypes.c_wchar_p]
        setter.restype = ctypes.c_long  # HRESULT is signed, including on 64-bit Windows.
        result = setter(app_id)
        if result < 0:
            _LOG.warning("Could not set Windows taskbar identity %s (HRESULT 0x%08X)",
                         app_id, result & 0xFFFFFFFF)
    except (AttributeError, OSError) as exc:
        _LOG.warning("Could not set Windows taskbar identity %s: %s", app_id, exc)


def _decode_icon(data: bytes) -> QIcon:
    """Keep decoded sizes in memory; no lazy file access after window creation."""
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer, b"ico")
    icon = QIcon()
    for index in range(reader.imageCount()):
        if not reader.jumpToImage(index):
            return QIcon()
        image = reader.read()
        if image.isNull():
            return QIcon()
        icon.addPixmap(QPixmap.fromImage(image))
    return icon


def load_app_icon() -> QIcon:
    """Load after QApplication; retain the original artwork if assets are unavailable."""
    root = Path(getattr(sys, "_MEIPASS", _ROOT))
    path = root / "assets" / "icons" / "spectralsweep.ico"
    try:
        icon = _decode_icon(path.read_bytes())
        if not icon.isNull():
            return icon
        _LOG.warning("Could not decode application icon %s; using embedded icon", path)
    except OSError as exc:
        _LOG.warning("Could not read application icon %s; using embedded icon: %s", path, exc)

    from ui._app_icon_data import ICON_B85
    icon = _decode_icon(zlib.decompress(base64.b85decode(ICON_B85)))
    if not icon.isNull():
        return icon
    # A deployment without Qt's ICO image plugin should still have a visible icon.
    _LOG.warning("Qt could not decode the embedded application icon; using a system icon")
    return QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)


def _taskbar_icon_path(cache_root: Path | None = None) -> Path:
    """Explorer needs a real, persistent file, including after PyInstaller exits."""
    from ui._app_icon_data import ICON_B85
    data = zlib.decompress(base64.b85decode(ICON_B85))
    if cache_root is None:
        location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
        if not location:
            raise OSError("No local application data directory is available")
        cache_root = Path(location) / APP_NAME / "icons"
    cache_root.mkdir(parents=True, exist_ok=True)
    # A changed image gets a new filename instead of relying on Explorer's icon cache.
    path = cache_root / f"spectralsweep-{hashlib.sha256(data).hexdigest()[:16]}.ico"
    try:
        if path.read_bytes() == data:
            return path
    except FileNotFoundError:
        pass
    output = QSaveFile(str(path))
    if not output.open(QIODevice.OpenModeFlag.WriteOnly):
        raise OSError(output.errorString())
    if output.write(data) != len(data):
        message = output.errorString()
        output.cancelWriting()
        raise OSError(message)
    if not output.commit():
        raise OSError(output.errorString())
    return path


def _relaunch_command(preview: bool) -> str:
    executable = Path(sys.executable)
    if getattr(sys, "frozen", False):
        command = [str(executable)]
    else:
        pythonw = executable.with_name("pythonw.exe")
        command = [str(pythonw if pythonw.is_file() else executable), str(_ROOT / "main.py")]
    if preview:
        # The child needs a temporary config owned by its supervisor; never pin --preview-child.
        command.append("--preview-spectrum")
    else:
        command.extend(arg for arg in sys.argv[1:] if arg in ("--mock", "--watch-styles"))
    return subprocess.list2cmdline(command)


class _TaskbarIdentity(QObject):
    def __init__(self, window: QWidget, values: dict[str, str]):
        super().__init__(window)
        self._window = window
        self._values = values
        self._hwnd = 0
        self._surface = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._apply)
        window.installEventFilter(self)
        window.winId()  # Create the native handle before the first taskbar button is shown.
        self._apply()
        QApplication.instance().aboutToQuit.connect(self.clear)

    def _apply(self):
        if not self._window.testAttribute(Qt.WidgetAttribute.WA_WState_Created):
            return
        surface = self._window.windowHandle()
        if surface is None:
            return
        if surface is not self._surface:
            self._surface = surface
            surface.installEventFilter(self)
        self._hwnd = int(surface.winId())
        try:
            from ui.windows_taskbar import write_properties
            write_properties(self._hwnd, self._values)
        except (AttributeError, OSError) as exc:
            _LOG.warning("Could not set Windows taskbar properties: %s", exc)

    def clear(self):
        self._timer.stop()
        if self._hwnd:
            hwnd, self._hwnd = self._hwnd, 0
            try:
                from ui.windows_taskbar import PROPERTY_IDS, write_properties
                write_properties(hwnd, dict.fromkeys(PROPERTY_IDS))
            except (AttributeError, OSError) as exc:
                _LOG.warning("Could not release Windows taskbar properties: %s", exc)

    def eventFilter(self, watched, event):
        if watched is self._window and event.type() == QEvent.Type.WinIdChange:
            self._timer.start(0)
        elif watched is self._surface and event.type() == QEvent.Type.PlatformSurface:
            if event.surfaceEventType() == QPlatformSurfaceEvent.SurfaceEventType.SurfaceAboutToBeDestroyed:
                self.clear()
            else:
                self._timer.start(0)
        return False


def configure_window_taskbar(window: QWidget, *, preview: bool = False) -> None:
    """Provide Explorer with the app icon/name/command, not Python's executable metadata."""
    if sys.platform != "win32" or QApplication.platformName() != "windows":
        return
    if getattr(window, "_taskbar_identity", None) is not None:
        return
    try:
        icon_path = _taskbar_icon_path()
    except OSError as exc:
        _LOG.warning("Could not prepare the Windows taskbar icon: %s", exc)
        return
    window._taskbar_identity = _TaskbarIdentity(window, {
        "command": _relaunch_command(preview),
        "icon": f"{icon_path},0",
        "name": f"{APP_NAME} Preview" if preview else APP_NAME,
        "app_id": PREVIEW_APP_ID if preview else APP_ID,
    })
