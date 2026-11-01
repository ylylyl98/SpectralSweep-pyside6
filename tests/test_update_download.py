import hashlib

import pytest
from PySide6.QtCore import QCoreApplication, QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest

from app.application_updates import UpdateRelease
from app.update_download import UpdateDownload

DATA = b'fake installer bytes'
DIGEST = hashlib.sha256(DATA).hexdigest()
TAG = 'v1.0.3'
NAME = f'SpectralSweep-{TAG}-windows-x64-Setup.exe'
BASE = f'https://github.com/ylylyl98/SpectralSweep-pyside6/releases/download/{TAG}/{NAME}'


class Reply(QObject):
    readyRead = Signal()
    finished = Signal()
    sslErrors = Signal(object)

    def __init__(self):
        super().__init__()
        self.data = b''
        self.status = 200
        self.target = None
        self.aborted = False
        self.network_error = QNetworkReply.NetworkError.NoError

    def setReadBufferSize(self, size):
        self.buffer_size = size

    def read(self, size):
        data, self.data = self.data[:size], self.data[size:]
        return data

    def attribute(self, key):
        if key == QNetworkRequest.Attribute.HttpStatusCodeAttribute:
            return self.status
        if key == QNetworkRequest.Attribute.RedirectionTargetAttribute:
            return self.target
        return None

    def error(self):
        return self.network_error

    def abort(self):
        self.aborted = True
        self.finished.emit()

    def chunk(self, data):
        self.data += data
        self.readyRead.emit()

    def complete(self, data=b'', status=200, target=None):
        self.status, self.target = status, target
        self.data += data
        self.readyRead.emit()
        self.finished.emit()


class Manager:
    def __init__(self):
        self.requests = []
        self.replies = []

    def get(self, request):
        self.requests.append(request)
        reply = Reply()
        self.replies.append(reply)
        return reply


def release(*, digest=DIGEST, checksum=''):
    return UpdateRelease(TAG, TAG, f'https://github.com/ylylyl98/SpectralSweep-pyside6/releases/tag/{TAG}',
                         BASE, len(DATA), '', '2026-01-01T00:00:00Z', digest, checksum)


@pytest.fixture
def service(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    manager = Manager()
    service = UpdateDownload(manager=manager, cache_dir=tmp_path)
    finished, progress = [], []
    service.finished.connect(lambda path, error: finished.append((path, error)))
    service.progress.connect(lambda n, total: progress.append((n, total)))
    yield service, manager, finished, progress, tmp_path
    service.shutdown()


def test_streamed_download_verified_and_persisted(service):
    subject, manager, done, progress, folder = service
    candidate = release()
    assert subject.start(candidate)
    assert not subject.start(candidate)
    manager.replies[-1].chunk(DATA[:5])
    manager.replies[-1].complete(DATA[5:])
    path, error = done[-1]
    assert error == '' and path.read_bytes() == DATA
    assert subject.verified_path == path and subject.verified_release == candidate
    assert subject.verified_sha256 == DIGEST
    assert progress == [(5, len(DATA)), (len(DATA), len(DATA))]
    assert not list(folder.glob('*.part'))
    assert not manager.requests[0].hasRawHeader('Authorization')


@pytest.mark.parametrize('payload', [DATA[:-1], DATA + b'!', b'x' * len(DATA)])
def test_invalid_download_removed(service, payload):
    subject, manager, done, _, folder = service
    assert subject.start(release())
    manager.replies[-1].complete(payload)
    assert done[-1][0] is None and done[-1][1]
    assert not list(folder.iterdir())


def test_checksum_sidecar_and_exact_name(service):
    subject, manager, done, _, _ = service
    assert subject.start(release(digest='', checksum=BASE + '.sha256'))
    manager.replies[-1].complete(f'{DIGEST}  {NAME}\n'.encode())
    assert len(manager.replies) == 2
    manager.replies[-1].complete(DATA)
    assert done[-1][0].read_bytes() == DATA
    assert subject.verified_sha256 == DIGEST


@pytest.mark.parametrize('body', [b'x' * 4097, (DIGEST + '  other.exe\n').encode()])
def test_invalid_checksum_fails(service, body):
    subject, manager, done, _, folder = service
    assert subject.start(release(digest='', checksum=BASE + '.sha256'))
    manager.replies[-1].complete(body)
    assert done[-1][0] is None and done[-1][1]
    assert not list(folder.iterdir())


def test_trusted_redirect_and_untrusted_redirect(service):
    subject, manager, done, _, _ = service
    assert subject.start(release())
    manager.replies[-1].complete(b'ignored', 302, QUrl('https://release-assets.githubusercontent.com/a'))
    manager.replies[-1].complete(DATA)
    assert done[-1][0] is not None
    assert subject.start(release())
    manager.replies[-1].complete(b'ignored', 302, QUrl('https://evil.example/a'))
    assert done[-1][0] is None and 'redirect' in done[-1][1].lower()


@pytest.mark.parametrize('url', ['http://github.com/a', 'https://user@github.com/a',
                                 'https://github.com:444/a'])
def test_rejects_unsafe_redirects(service, url):
    subject, manager, done, _, _ = service
    assert subject.start(release())
    manager.replies[-1].complete(status=302, target=QUrl(url))
    assert done[-1][0] is None and done[-1][1]


def test_cancel_and_stale_callbacks(service):
    subject, manager, done, _, folder = service
    assert subject.start(release())
    old = manager.replies[-1]
    old.chunk(DATA[:3])
    subject.cancel()
    assert old.aborted and len(done) == 1 and not list(folder.iterdir())
    assert subject.start(release())
    old.complete(DATA)
    assert len(done) == 1 and subject.busy
    manager.replies[-1].complete(DATA)
    assert done[-1][0].read_bytes() == DATA


@pytest.mark.parametrize('kind', ['http', 'tls', 'timeout'])
def test_errors_fail_closed(service, kind):
    subject, manager, done, _, folder = service
    assert subject.start(release())
    reply = manager.replies[-1]
    if kind == 'http':
        reply.complete(b'error', 404)
    elif kind == 'tls':
        reply.sslErrors.emit([])
    else:
        subject._inactivity.timeout.emit()
    assert done[-1][0] is None and done[-1][1]
    assert not list(folder.iterdir())


def test_redirect_limit_and_transport_error(service):
    subject, manager, done, _, folder = service
    assert subject.start(release())
    for _ in range(5):
        manager.replies[-1].complete(status=302, target=QUrl('https://github.com/a'))
    manager.replies[-1].complete(status=302, target=QUrl('https://github.com/a'))
    assert done[-1][0] is None and not list(folder.iterdir())
    assert subject.start(release())
    reply = manager.replies[-1]
    reply.network_error = QNetworkReply.NetworkError.RemoteHostClosedError
    reply.complete(DATA)
    assert done[-1][0] is None and not list(folder.iterdir())


def test_shutdown_cleans_partial_and_ignores_stale_tls(service):
    subject, manager, done, _, folder = service
    assert subject.start(release())
    old = manager.replies[-1]
    subject.cancel()
    assert subject.start(release())
    old.sslErrors.emit([])
    assert subject.busy
    manager.replies[-1].chunk(DATA[:2])
    subject.shutdown()
    assert not subject.busy and not list(folder.iterdir())
    assert not subject.start(release())


def test_disk_write_failure_removes_partial_and_reports_error(service):
    subject, manager, done, _, folder = service
    assert subject.start(release())
    file = subject._file
    class BrokenWrite:
        def write(self, _):
            raise OSError('disk full')
        def close(self):
            file.close()
    subject._file = BrokenWrite()
    manager.replies[-1].complete(DATA)
    assert done[-1][0] is None and 'disk full' in done[-1][1]
    assert not list(folder.iterdir())
    assert not subject.verified_sha256


def test_finish_signals_do_not_allow_reentrant_start(service):
    subject, manager, done, _, _ = service
    attempted = []
    subject.busyChanged.connect(lambda busy: attempted.append(subject.start(release())) if not busy else None)
    assert subject.start(release())
    manager.replies[-1].complete(DATA)
    assert attempted == [False] and len(done) == 1
    assert subject.start(release())
    assert subject.verified_sha256 == '' and subject.verified_path is None
