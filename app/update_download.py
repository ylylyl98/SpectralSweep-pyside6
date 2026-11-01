"""Stream and verify a published installer without executing it."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QStandardPaths, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from app.application_updates import validate_download_release

_ALLOWED_HOSTS = frozenset({'github.com', 'release-assets.githubusercontent.com',
                            'objects.githubusercontent.com', 'github-releases.githubusercontent.com'})
_CHUNK = 64 * 1024
_CHECKSUM_LIMIT = 4096


def _redirect_url(current, target):
    resolved = current.resolved(target if isinstance(target, QUrl) else QUrl(str(target)))
    try:
        parsed = urlsplit(resolved.toString())
        if (parsed.scheme != 'https' or parsed.hostname not in _ALLOWED_HOSTS
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443)):
            raise ValueError('Untrusted download redirect')
    except ValueError as exc:
        raise ValueError('Untrusted download redirect') from exc
    return resolved


class UpdateDownload(QObject):
    progress = Signal(int, int)
    busyChanged = Signal(bool)
    finished = Signal(object, str)

    def __init__(self, parent=None, *, manager=None, cache_dir=None):
        super().__init__(parent)
        self.manager = manager if manager is not None else QNetworkAccessManager(self)
        default = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else Path(default or tempfile.gettempdir()) / 'updates'
        self.busy = False
        self.verified_path = None
        self.verified_release = None
        self.verified_sha256 = ''
        self._reply = None
        self._file = None
        self._part = None
        self._closed = False
        self._finishing = False
        self._inactivity = QTimer(self)
        self._inactivity.setSingleShot(True)
        self._inactivity.setInterval(30_000)
        self._inactivity.timeout.connect(lambda: self._fail('Download timed out due to inactivity.'))
        self._overall = QTimer(self)
        self._overall.setSingleShot(True)
        self._overall.setInterval(30 * 60 * 1000)
        self._overall.timeout.connect(lambda: self._fail('Download exceeded its time limit.'))

    def start(self, release):
        if self.busy or self._closed or self._finishing:
            return False
        self.verified_path = self.verified_release = None
        self.verified_sha256 = ''
        fd = None
        try:
            validate_download_release(release)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            fd, path = tempfile.mkstemp(prefix='spectralsweep-', suffix='.part', dir=self.cache_dir)
            self._part = Path(path)
            self._file = os.fdopen(fd, 'wb')
            fd = None
        except (ValueError, OSError) as exc:
            if fd is not None:
                os.close(fd)
            if self._part is not None:
                self._part.unlink(missing_ok=True)
                self._part = None
            self.finished.emit(None, str(exc))
            return False
        self.busy = True
        self._release = release
        self._digest = str(release.setup_sha256 or '').lower()
        self._hash = hashlib.sha256()
        self._received = 0
        self._checksum = bytearray()
        self._phase = 'installer' if self._digest else 'checksum'
        self._redirects = 0
        self._overall.start()
        self.busyChanged.emit(True)
        self._request(QUrl(release.setup_url if self._digest else release.checksum_url))
        return True

    def _request(self, url):
        if not self.busy:
            return
        self._url = url
        request = QNetworkRequest(url)
        request.setRawHeader(b'User-Agent', b'SpectralSweep-update-download')
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        try:
            reply = self.manager.get(request)
            self._reply = reply
            reply.setReadBufferSize(_CHUNK)
            reply.readyRead.connect(lambda: self._read(reply))
            reply.finished.connect(lambda: self._complete(reply))
            if hasattr(reply, 'sslErrors'):
                reply.sslErrors.connect(lambda *_: self._ssl_failure(reply))
            self._inactivity.start()
        except Exception as exc:
            self._fail(f'Could not start download: {exc}')

    def _ssl_failure(self, reply):
        if self.busy and reply is self._reply:
            self._fail('TLS verification failed.')

    def _read(self, reply):
        if not self.busy or reply is not self._reply:
            return
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if status != 200:
            return
        limit = _CHECKSUM_LIMIT if self._phase == 'checksum' else self._release.setup_size
        try:
            while self.busy and reply is self._reply:
                used = len(self._checksum) if self._phase == 'checksum' else self._received
                data = bytes(reply.read(min(_CHUNK, limit - used + 1)))
                if not data:
                    break
                self._inactivity.start()
                if used + len(data) > limit:
                    self._fail('Download exceeds expected size.')
                    return
                if self._phase == 'checksum':
                    self._checksum.extend(data)
                else:
                    self._file.write(data)
                    self._hash.update(data)
                    self._received += len(data)
                    self.progress.emit(self._received, self._release.setup_size)
        except (OSError, ValueError) as exc:
            self._fail(f'Could not save download: {exc}')

    def _complete(self, reply):
        if not self.busy or reply is not self._reply:
            return
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if status in (301, 302, 303, 307, 308):
            target = reply.attribute(QNetworkRequest.Attribute.RedirectionTargetAttribute)
            try:
                if not target or self._redirects >= 5:
                    raise ValueError('Missing or excessive download redirect')
                url = _redirect_url(self._url, target)
            except ValueError as exc:
                self._fail(str(exc))
                return
            self._reply = None
            reply.deleteLater()
            self._redirects += 1
            self._request(url)
            return
        if status != 200 or reply.error() != QNetworkReply.NetworkError.NoError:
            self._fail(f'Download failed (HTTP {status or "unavailable"}).')
            return
        self._read(reply)
        if not self.busy or reply is not self._reply:
            return
        self._reply = None
        reply.deleteLater()
        if self._phase == 'checksum':
            filename = f'SpectralSweep-{self._release.tag}-windows-x64-Setup.exe'
            try:
                content = self._checksum.decode('ascii')
                matches = re.findall(r'^([0-9a-fA-F]{64})[ \t]+\*?([^\r\n]+)\r?$', content, re.MULTILINE)
                digests = [digest.lower() for digest, name in matches if name == filename]
                if len(digests) != 1:
                    raise ValueError('Checksum file does not identify the expected installer.')
                self._digest = digests[0]
            except (UnicodeError, ValueError) as exc:
                self._fail(str(exc))
                return
            self._phase = 'installer'
            self._redirects = 0
            self._request(QUrl(self._release.setup_url))
            return
        if self._received != self._release.setup_size or self._hash.hexdigest() != self._digest:
            self._fail('Installer size or SHA-256 does not match the release.')
            return
        try:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            self._file = None
            destination = self._part.with_suffix('.exe')
            os.replace(self._part, destination)
            self._part = None
        except OSError as exc:
            self._fail(f'Could not finalize download: {exc}')
            return
        self.verified_path, self.verified_release = destination, self._release
        self.verified_sha256 = self._hash.hexdigest()
        self._finish(destination, '')

    def _finish(self, path, error):
        if not self.busy:
            return
        self._finishing = True
        self.busy = False
        self._inactivity.stop()
        self._overall.stop()
        reply, self._reply = self._reply, None
        if reply is not None:
            reply.abort()
            reply.deleteLater()
        try:
            if self._file is not None:
                try:
                    self._file.close()
                except OSError as exc:
                    error = f'{error} Cleanup failed: {exc}'.strip()
                self._file = None
            if self._part is not None:
                try:
                    self._part.unlink(missing_ok=True)
                except OSError as exc:
                    error = f'{error} Cleanup failed: {exc}'.strip()
                self._part = None
            self.busyChanged.emit(False)
            self.finished.emit(path, error)
        finally:
            self._finishing = False

    def _fail(self, message):
        self._finish(None, message)

    def cancel(self):
        self._fail('Download cancelled.')

    def shutdown(self):
        self._closed = True
        if self.busy:
            self._fail('Download cancelled.')
