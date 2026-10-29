"""Report source edits without importing code or disturbing instrument owners."""
from __future__ import annotations

import hashlib
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal


def source_digest(path: Path) -> str:
    try:
        source = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return "missing-or-unreadable"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _restart_only_asset(name: str) -> bool:
    # All Quick surfaces share a QML engine and its compiled component/import
    # cache. Rebuilding one Python page does not reload these shared assets.
    path = Path(name)
    return path.suffix in {".qml", ".js", ".mjs"} or path.name == "qmldir"


class SourceChangeTracker(QObject):
    """Content fingerprints since startup/last successful, explicitly scoped reload.

    Watching never reloads Python. Directory watches cover atomic replacement;
    file watches cover edits that do not change a directory's entries.
    """

    changed = Signal()
    style_path = "ui/styles/application.qss"

    def __init__(self, root, groups, *, shared=(), style_path=None, parent=None):
        super().__init__(parent)
        self.root = Path(root).resolve()
        self.style_path = self._source_name(style_path or self.style_path)
        self.groups = {key: tuple(paths) for key, paths in groups.items()}
        self.shared = set(shared)
        self._file_cache = {}
        self._changed_files = set()
        self._baseline = self.snapshot()
        self._current = dict(self._baseline)
        self._page_loaded = {key: {name: self._baseline.get(name) for name in paths}
                             for key, paths in self.groups.items()}
        self._watcher = QFileSystemWatcher(self)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self.refresh)
        self._watcher.fileChanged.connect(self._schedule)
        self._watcher.directoryChanged.connect(self._schedule)
        self._sync_watches()

    def _source_name(self, path):
        path = Path(path)
        path = path if path.is_absolute() else self.root / path
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            # User-selected external stylesheets are not repository changes.
            return None

    def _paths(self):
        # The legacy instrument adapters at the repository root are live
        # dependencies too (LF6, IV automation and TLPMX), not just main.py.
        for path in self.root.glob("*.py"):
            if path.is_file():
                yield path.name
        for name in ("requirements.txt", "launch.bat", "tools/launch_app.py"):
            if (self.root / name).is_file():
                yield name
        for name in ("ui", "controllers", "app", "utils", "CRYO2100"):
            folder = self.root / name
            if folder.is_dir():
                for path in folder.rglob("*"):
                    if path.is_file() and (path.suffix in {".py", ".qss"}
                                           or _restart_only_asset(path.name)):
                        yield path.relative_to(self.root).as_posix()

    def snapshot(self, paths=None):
        # A full source read is needed once at startup, not on every editor
        # save. Explicit reload snapshots and fileChanged events bypass this
        # cache, including same-size saves with a preserved timestamp.
        explicit = paths is not None
        result = {}
        for name in (self._paths() if paths is None else paths):
            path = self.root / name
            try:
                stat = path.stat()
                signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
            except OSError:
                result[name] = "missing-or-unreadable"
                self._file_cache.pop(name, None)
                continue
            cached = self._file_cache.get(name)
            if explicit or name in self._changed_files or cached is None or cached[0] != signature:
                digest = source_digest(path)
                if digest != "missing-or-unreadable":
                    self._file_cache[name] = (signature, digest)
                else:
                    self._file_cache.pop(name, None)
            else:
                digest = cached[1]
            result[name] = digest
        return result

    def _schedule(self, path):
        try:
            name = Path(path).relative_to(self.root).as_posix()
        except ValueError:
            name = ""
        if name in self._file_cache:
            self._changed_files.add(name)
        self._timer.start()

    def _sync_watches(self):
        paths = {self.root / name for name in self._current}
        paths.update(path.parent for path in tuple(paths))
        paths.update(self.root / name for name in ("ui", "controllers", "app", "utils", "CRYO2100", "tools"))
        paths.add(self.root)
        desired = {str(path) for path in paths if path.exists()}
        current = set(self._watcher.files() + self._watcher.directories())
        if current - desired:
            self._watcher.removePaths(sorted(current - desired))
        if desired - current:
            self._watcher.addPaths(sorted(desired - current))

    def refresh(self):
        previous = self._current
        self._current = self.snapshot()
        self._changed_files.clear()
        self._sync_watches()
        if previous != self._current:
            self.changed.emit()

    def pending(self):
        result = []
        for name in sorted(self._baseline.keys() | self._current.keys()):
            current = self._current.get(name)
            owners = [] if _restart_only_asset(name) else [
                key for key, paths in self.groups.items() if name in paths]
            pages = [key for key in owners if self._page_loaded[key].get(name) != current]
            baseline_changed = self._baseline.get(name) != current
            if not baseline_changed and not pages:
                continue
            restart = baseline_changed and (not owners or name in self.shared)
            result.append({"path": name, "pages": pages,
                           "styles": name == self.style_path,
                           "restart": name != self.style_path and restart})
        return result

    def mark_page_applied(self, page_id, snapshot):
        # A shared helper may still have old instances elsewhere in the shell.
        for name in self.groups.get(page_id, ()):
            if name in snapshot and not _restart_only_asset(name):
                self._page_loaded[page_id][name] = snapshot[name]
                if name not in self.shared:
                    self._baseline[name] = snapshot[name]
        self.refresh()
        self.changed.emit()

    def mark_styles_applied(self, source, path=None):
        # Quick mode can switch the application's stylesheet after tracking
        # starts. Acknowledge the manager's actual file, not the default QSS.
        if path is not None:
            self.style_path = self._source_name(path)
        if self.style_path is not None:
            self._baseline[self.style_path] = hashlib.sha256(source.encode("utf-8")).hexdigest()
        self.refresh()
        self.changed.emit()
