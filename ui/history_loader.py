"""Cancelable history I/O with results delivered on the Qt owner thread."""
import json
import threading
import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from app.history_reader import read_preview, read_settings
from app.subprocess_command import module_command
from app.metadata_storage import STORAGE_FORMAT, TABLE_STORAGE_FORMAT


class _Results(QObject):
    finished = Signal(str, int, object, object)


class _Task(QRunnable):
    def __init__(self, signals, key, token, cancel, work):
        super().__init__()
        self.signals, self.key, self.token = signals, key, token
        self.cancel, self.work = cancel, work

    def run(self):
        result = error = None
        try:
            if not self.cancel.is_set():
                result = self.work(self.cancel)
        except Exception as exc:
            error = str(exc)
        if not self.cancel.is_set():
            self.signals.finished.emit(self.key, self.token, result, error)


class HistoryTaskRunner(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._signals = _Results()
        self._signals.finished.connect(self._finish, Qt.ConnectionType.QueuedConnection)
        self._jobs = {}
        self._token = 0
        jobs = self._jobs
        self.destroyed.connect(lambda: [job[1].set() for job in jobs.values()])

    def submit(self, key, work, callback):
        self.cancel(key)
        self._token += 1
        cancel = threading.Event()
        self._jobs[key] = (self._token, cancel, callback)
        QThreadPool.globalInstance().start(_Task(self._signals, key, self._token, cancel, work))

    def cancel(self, key):
        job = self._jobs.pop(key, None)
        if job:
            job[1].set()

    def cancel_all(self):
        for key in list(self._jobs):
            self.cancel(key)

    @Slot(str, int, object, object)
    def _finish(self, key, token, result, error):
        job = self._jobs.get(key)
        if job and job[0] == token:
            self._jobs.pop(key)
            job[2](result, error)


def resolve_metadata_path(raw_path, output_root, cancel):
    path = Path(raw_path)
    if raw_path and path.is_file():
        return path.resolve()
    if raw_path:
        for candidate in Path(output_root).rglob(path.name):
            if cancel.is_set():
                break
            if candidate.is_file():
                return candidate.resolve()
    raise FileNotFoundError("Metadata sidecar could not be found")


def _requires_isolated_reader(path):
    # A small compressed file can expand into a very large plan. Its on-disk
    # size alone is no longer a reliable measure of Python/GIL work.
    if path.stat().st_size >= 1_000_000:
        return True
    with path.open(encoding="utf-8") as stream:
        text = stream.read(1_000_000)
    if len(text) == 1_000_000:
        return True
    try:
        raw = json.loads(text)
    except ValueError:
        return False  # The reader reports the original parse error.
    storage = raw.get("metadata_storage") if isinstance(raw, dict) else None
    return isinstance(storage, dict) and storage.get("format") in {STORAGE_FORMAT, TABLE_STORAGE_FORMAT}


def load_preview(raw_path, output_root, event_filter, offset, cancel):
    path = resolve_metadata_path(raw_path, output_root, cancel)
    if _requires_isolated_reader(path):
        return _read_isolated({"operation": "preview", "path": str(path),
                               "event_filter": event_filter, "offset": offset}, cancel)
    return read_preview(path, event_filter, offset, cancel)


def load_settings(raw_path, output_root, cancel, *, supported_keys=None):
    path = resolve_metadata_path(raw_path, output_root, cancel)
    if _requires_isolated_reader(path):
        return _read_isolated({"operation": "settings", "path": str(path), "supported_keys": supported_keys}, cancel)
    return read_settings(path, supported_keys)


def _read_isolated(request, cancel):
    """Keep JSON's CPU/GIL cost out of the GUI interpreter; cancel stale reads."""
    if cancel.is_set():
        return None
    process = subprocess.Popen(
        module_command("app.history_reader"),
        cwd=str(Path(__file__).resolve().parents[1]),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        payload = json.dumps(request)
        while True:
            if cancel.is_set():
                return None
            try:
                output, error = process.communicate(input=payload, timeout=.1)
                break
            except subprocess.TimeoutExpired:
                payload = None
        if process.returncode:
            raise RuntimeError(error.strip() or "History reader failed")
        response = json.loads(output)
        if "error" in response:
            raise RuntimeError(response["error"])
        return response["result"]
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
