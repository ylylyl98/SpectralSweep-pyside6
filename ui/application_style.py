"""Reload the existing application QSS without replacing widgets or controllers.

This is a loading boundary for the legacy light theme, not a new theme system.
Panel-local styles are deliberately left intact for incremental migration.
"""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication


STYLE_PATH = Path(__file__).resolve().parent / "styles" / "application.qss"


def read_style(path: Path) -> str:
    """Reject incomplete saves before replacing the last working stylesheet.

Qt remains responsible for QSS semantics; this only checks basic structure.
Quoted strings/comments can contain braces and are excluded from the check.
"""
    source = path.read_text(encoding="utf-8-sig")
    if not source.strip():
        raise ValueError("The stylesheet is empty")
    structural = re.sub(r'/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
                        "", source, flags=re.S)
    if "/*" in structural:
        raise ValueError("Unclosed stylesheet comment")
    depth = 0
    rules = 0
    for char in structural:
        if char == "{":
            depth += 1
            rules += 1
            if depth > 1:
                raise ValueError("Nested stylesheet blocks are not supported")
        elif char == "}":
            depth -= 1
            if depth < 0:
                raise ValueError("Unexpected closing brace in stylesheet")
    if depth or not rules:
        raise ValueError("The stylesheet has incomplete or missing rules")
    return source


class ApplicationStyle(QObject):
    reloaded = Signal(str)
    failed = Signal(str)

    def __init__(self, app: QApplication, path: Path = STYLE_PATH):
        super().__init__(app)
        self.app = app
        self.path = Path(path).resolve()
        self.last_error = ""
        self._watching = False
        self._watcher = QFileSystemWatcher(self)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._reload_if_changed)
        self._watcher.fileChanged.connect(self._changed)
        self._watcher.directoryChanged.connect(self._changed)
        self._last_source = None

    def reload(self) -> bool:
        try:
            source = read_style(self.path)
        except (OSError, UnicodeError, ValueError) as exc:
            self.last_error = str(exc)
            self.failed.emit(self.last_error)
            self._sync_watches()
            return False
        # Only visual properties change. Existing objects, signal connections,
        # editable values and in-flight acquisitions remain owned by their UI.
        self.app.setStyleSheet(source)
        self._last_source = source
        self.last_error = ""
        self._sync_watches()
        self.reloaded.emit(str(self.path))
        return True

    def set_watching(self, enabled: bool) -> None:
        self._watching = bool(enabled)
        self._timer.stop()
        self._sync_watches()

    def _sync_watches(self) -> None:
        desired = set()
        if self._watching:
            # The directory watch survives atomic save/replace and file removal.
            desired = {str(p) for p in (self.path, self.path.parent) if p.exists()}
        current = set(self._watcher.files() + self._watcher.directories())
        if current - desired:
            self._watcher.removePaths(sorted(current - desired))
        if desired - current:
            self._watcher.addPaths(sorted(desired - current))

    def _changed(self, _path: str) -> None:
        if self._watching:
            self._timer.start()

    def _reload_if_changed(self) -> None:
        self._sync_watches()
        try:
            if read_style(self.path) == self._last_source:
                return
        except (OSError, UnicodeError, ValueError):
            pass
        self.reload()


def application_style(app: QApplication) -> ApplicationStyle:
    manager = getattr(app, "_spectral_application_style", None)
    if manager is None:
        manager = ApplicationStyle(app)
        app._spectral_application_style = manager
    return manager
