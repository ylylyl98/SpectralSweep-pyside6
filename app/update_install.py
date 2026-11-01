from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import uuid

from PySide6.QtCore import QObject, QTimer, Signal


class InstallHandoff(QObject):
    """Prepare an external installer helper without blocking the GUI thread."""

    ready = Signal()
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._poll)
        self._directory = None
        self._token = None
        self._process = None
        self._deadline = 0.0
        self._ready = False
        self._armed = False

    def prepare(self, installer: Path, sha256: str, install_dir: Path, restart_args=()) -> bool:
        if self._directory is not None:
            self.failed.emit('An installation handoff is already in progress.')
            return False
        try:
            installer = Path(installer).resolve(strict=True)
            install_dir = Path(install_dir).resolve(strict=True)
            target = install_dir / 'SpectralSweep.exe'
            if not installer.is_file() or not target.is_file():
                raise ValueError('Installer or installed SpectralSweep.exe was not found.')
            if not re.fullmatch(r'[0-9a-fA-F]{64}', sha256):
                raise ValueError('Invalid installer SHA-256.')
            if any(not isinstance(arg, str) or '\x00' in arg for arg in restart_args):
                raise ValueError('Invalid restart arguments.')
            cache = Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()) / 'SpectralSweep' / 'update-handoffs'
            cache.mkdir(parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix='handoff-', dir=cache)).resolve()
            if directory.is_relative_to(install_dir):
                raise ValueError('Update handoff must be outside the installation directory.')
            self._directory = directory
            self._token = uuid.uuid4().hex
            script = directory / 'update_install.ps1'
            shutil.copyfile(Path(__file__).with_suffix('.ps1'), script)
            request = directory / 'request.json'
            request.write_text(json.dumps({'parent_pid': os.getpid(), 'setup_file': str(installer),
                'sha256': sha256.lower(), 'install_dir': str(install_dir), 'restart_args': list(restart_args),
                'token': self._token}, ensure_ascii=False), encoding='utf-8')
            powershell = Path(os.environ['WINDIR']) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
            self._process = subprocess.Popen([str(powershell), '-NoProfile', '-NonInteractive',
                '-ExecutionPolicy', 'Bypass', '-File', str(script), '-Request', str(request)],
                cwd=directory, close_fds=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self._deadline = time.monotonic() + 120
            self._timer.start()
            return True
        except Exception as exc:
            self.cancel()
            self.failed.emit(str(exc))
            return False

    def _poll(self):
        try:
            marker = self._directory / 'ready'
            if marker.is_file() and marker.read_text(encoding='utf-8-sig').strip() == self._token:
                self._timer.stop()
                self._ready = True
                self.ready.emit()
                return
            result_path = self._directory / 'result.json'
            if result_path.is_file():
                try:
                    result = json.loads(result_path.read_text(encoding='utf-8-sig'))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    result = None
                if isinstance(result, dict) and result.get('status') == 'failed':
                    raise RuntimeError(str(result.get('detail') or 'The update helper failed.'))
            if self._process.poll() is not None:
                raise RuntimeError('The update helper exited before becoming ready.')
            if time.monotonic() >= self._deadline:
                raise RuntimeError('The update helper did not finish verification in time.')
        except Exception as exc:
            self.cancel()
            self.failed.emit(str(exc))

    def arm(self) -> bool:
        if not self._ready or self._armed or self._directory is None:
            return False
        try:
            if self._process.poll() is not None:
                raise RuntimeError('The update helper exited before installation was confirmed.')
            (self._directory / 'arm').write_text(self._token, encoding='utf-8')
            self._armed = True
            return True
        except (OSError, RuntimeError) as exc:
            self.cancel()
            self.failed.emit(str(exc))
            return False

    def cancel(self):
        self._timer.stop()
        if self._directory is not None and not self._armed:
            try:
                (self._directory / 'cancel').write_text(self._token, encoding='utf-8')
            except OSError:
                pass
        self._ready = False
        if not self._armed:
            self._directory = None
            self._token = None
            self._process = None

    def shutdown(self):
        if not self._armed:
            self.cancel()
        else:
            self._timer.stop()
