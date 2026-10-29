"""Bounded, asynchronous public release checks; never installs anything."""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from email.utils import parsedate_to_datetime

from PySide6.QtCore import QObject, QSettings, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from app.application_updates import (
    RELEASES_API, LATEST_RELEASE_API, MAX_RESPONSE_BYTES, decode_releases, read_installed_version,
    select_update,
)


class UpdateService(QObject):
    finished = Signal(object, str, bool)
    busyChanged = Signal(bool)

    def __init__(self, parent=None, *, installed=None, settings=None, manager=None, clock=None):
        super().__init__(parent)
        self.current = installed if installed is not None else read_installed_version()
        self.settings = settings if settings is not None else QSettings('SpectralSweep', 'SpectralSweep')
        self.manager = manager if manager is not None else QNetworkAccessManager(self)
        self._clock = clock or time.time
        self._reply = None
        self._closed = False
        self._manual = False
        self._payload = bytearray()
        self._failure = ''
        self._request_channel = ''
        self._request_key = ''
        self._timer = QTimer(self)
        self._timer.setInterval(15 * 60 * 1000)
        self._timer.timeout.connect(lambda: self.check(manual=False))
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.setInterval(10_000)
        self._timeout.timeout.connect(lambda: self._abort('The update check timed out.'))

    @property
    def channel(self):
        value = self.settings.value('updates/channel', self.current.channel)
        return value if value in ('stable', 'preview') else 'stable'

    @property
    def automatic_enabled(self):
        value = self.settings.value('updates/enabled', True)
        return value is True or str(value).lower() in ('true', '1')

    def set_channel(self, value):
        if value not in ('stable', 'preview'):
            raise ValueError('Unknown update channel')
        self.settings.setValue('updates/channel', value)

    def set_automatic_enabled(self, value):
        self.settings.setValue('updates/enabled', bool(value))

    def _key(self):
        identity = hashlib.sha256((self.current.version + '/' + self.channel).encode()).hexdigest()[:24]
        return 'updates/checks/' + identity

    def _number(self, key):
        try:
            value = float(self.settings.value(key, 0))
            return value if math.isfinite(value) and value >= 0 else 0
        except (ValueError, TypeError, OverflowError):
            return 0

    def start_automatic(self):
        if not self._closed:
            self._timer.start()
            self.check(manual=False)

    def check(self, manual=True):
        if self._closed:
            return False
        if self._reply is not None:
            self._manual = self._manual or manual
            return False
        if not manual and (not self.automatic_enabled or
                           os.environ.get('SPECTRALSWEEP_DISABLE_UPDATE_CHECK') == '1'):
            return False
        if not self.current.can_check:
            if manual:
                self.finished.emit(None, 'This source/local build cannot check EXE updates. '
                                   'Update a source checkout through Git, or obtain a published build from GitHub Releases.', True)
            return False
        key = self._key()
        now = self._clock()
        blocked = max(self._number(key + '/retryAfter'), self._number('updates/serverRetryAfter'))
        if now < blocked:
            if manual:
                self.finished.emit(None, 'Update checks are temporarily delayed by the release server. Try again later.', True)
            return False
        if not manual and now < self._number(key + '/nextCheck'):
            return False
        request = QNetworkRequest(QUrl(LATEST_RELEASE_API if self.channel == 'stable' else RELEASES_API))
        request.setRawHeader(b'User-Agent', b'SpectralSweep-update-check')
        request.setRawHeader(b'Accept', b'application/vnd.github+json')
        request.setRawHeader(b'X-GitHub-Api-Version', b'2026-03-10')
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        request.setTransferTimeout(10_000)
        self._manual = bool(manual)
        self._payload = bytearray()
        self._failure = ''
        self._request_channel = self.channel
        self._request_key = key
        try:
            reply = self.manager.get(request)
        except Exception:
            self.settings.setValue(key + '/nextCheck', now + 3600)
            self.finished.emit(None, 'Could not start the release check.', bool(manual))
            return False
        self._reply = reply
        # Bound Qt's unread transport buffer as well as our JSON accumulator.
        reply.setReadBufferSize(64 * 1024)
        reply.readyRead.connect(lambda: self._read(reply))
        reply.finished.connect(lambda: self._complete(reply))
        self._timeout.start()
        self.busyChanged.emit(True)
        return True

    def _read(self, reply):
        if self._closed or reply is not self._reply or self._failure:
            return
        remaining = MAX_RESPONSE_BYTES - len(self._payload)
        chunk = bytes(reply.read(remaining + 1))
        self._payload.extend(chunk[:remaining])
        if len(chunk) > remaining:
            self._abort('The release response exceeded the size limit.')

    def _abort(self, message):
        reply = self._reply
        if reply is not None:
            self._failure = message
            reply.abort()

    def _server_delay(self, reply, now, status):
        def header(name):
            return bytes(reply.rawHeader(name)).decode('ascii', errors='ignore').strip()
        def number(value):
            parsed = float(value)
            if not math.isfinite(parsed) or parsed < 0:
                raise ValueError('Invalid delay')
            return parsed
        delays = []
        retry = header('Retry-After')
        if retry:
            try:
                delays.append(now + number(retry))
            except ValueError:
                try:
                    date = parsedate_to_datetime(retry)
                    if date.tzinfo is not None:
                        delays.append(date.timestamp())
                except (ValueError, TypeError, OverflowError):
                    pass
        if status in (403, 429):
            try:
                delays.append(number(header('X-RateLimit-Reset')))
            except ValueError:
                pass
        try:
            delays.append(now + number(header('X-Poll-Interval')))
        except ValueError:
            pass
        return max([now] + [delay for delay in delays if math.isfinite(delay)])

    def _complete(self, reply):
        if self._closed or reply is not self._reply:
            return
        self._read(reply)
        # abort() may deliver finished synchronously in injected transports.
        if reply is not self._reply:
            return
        self._timeout.stop()
        now = self._clock()
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        delay = self._server_delay(reply, now, status)
        error = self._failure
        result = None
        if not error:
            if status == 404 and self._request_channel == 'stable':
                error = 'No stable release has been published yet. You can choose the preview channel to check pre-releases.'
            elif status != 200 or reply.error() != QNetworkReply.NetworkError.NoError:
                error = f'Could not check releases (HTTP {status or "unavailable"}).'
            else:
                try:
                    if self._request_channel == 'stable':
                        try:
                            release = json.loads(bytes(self._payload).decode('utf-8'))
                        except (UnicodeError, ValueError, RecursionError) as exc:
                            raise ValueError('The release server returned invalid JSON.') from exc
                        if not isinstance(release, dict):
                            raise ValueError('The release server did not return a release object.')
                        releases = [release]
                    else:
                        releases = decode_releases(bytes(self._payload))
                    result = select_update(releases, self.current, self._request_channel)
                except (ValueError, RecursionError) as exc:
                    error = str(exc)
        next_check = max(delay, now + (3600 if error else 6 * 3600))
        self.settings.setValue(self._request_key + '/nextCheck', next_check)
        if delay > now:
            self.settings.setValue('updates/serverRetryAfter', delay)
        if status in (403, 429):
            self.settings.setValue('updates/serverRetryAfter', max(delay, now + 3600))
        manual = self._manual
        self._reply = None
        self._payload.clear()
        reply.deleteLater()
        self.busyChanged.emit(False)
        self.finished.emit(result, error, manual)

    def shutdown(self):
        self._closed = True
        self._timer.stop()
        self._timeout.stop()
        reply, self._reply = self._reply, None
        self._payload.clear()
        if reply is not None:
            reply.abort()
            reply.deleteLater()
