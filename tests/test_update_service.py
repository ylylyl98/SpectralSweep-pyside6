import pytest
from PySide6.QtCore import QObject, QCoreApplication, QSettings, Signal
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest

from app.application_updates import InstalledVersion, MAX_RESPONSE_BYTES, LATEST_RELEASE_API, RELEASES_API
from app.update_service import UpdateService


class FakeReply(QObject):
    readyRead = Signal()
    finished = Signal()

    def __init__(self):
        super().__init__()
        self.data = b''
        self.status = 200
        self.headers = {}
        self.aborted = False

    def read(self, size):
        value, self.data = self.data[:size], self.data[size:]
        return value

    def setReadBufferSize(self, size):
        self.buffer_size = size

    def rawHeader(self, name):
        return self.headers.get(name.encode(), b'')

    def attribute(self, name):
        return self.status

    def error(self):
        return QNetworkReply.NetworkError.NoError

    def abort(self):
        self.aborted = True
        self.finished.emit()

    def deliver(self, data=b'{}', status=200, headers=None):
        self.data, self.status, self.headers = data, status, headers or {}
        self.readyRead.emit()
        self.finished.emit()


class FakeManager:
    def __init__(self):
        self.requests = []
        self.reply = None

    def get(self, request):
        self.requests.append(request)
        self.reply = FakeReply()
        return self.reply


@pytest.fixture
def service(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    settings = QSettings(str(tmp_path / 'updates.ini'), QSettings.Format.IniFormat)
    manager = FakeManager()
    now = [100000.0]
    installed = InstalledVersion('v1.0.0', 'a' * 40, 'stable', packaged=True)
    subject = UpdateService(installed=installed, settings=settings, manager=manager, clock=lambda: now[0])
    results = []
    subject.finished.connect(lambda *args: results.append(args))
    yield subject, manager, now, results
    subject.shutdown()


def test_construction_is_offline_and_requests_are_bounded(service):
    subject, manager, now, results = service
    assert not manager.requests
    assert subject.check(manual=False)
    assert not subject.check(manual=True)
    assert len(manager.requests) == 1
    assert manager.reply.buffer_size == 64 * 1024
    request = manager.requests[0]
    assert request.url().scheme() == 'https'
    assert request.rawHeader('X-GitHub-Api-Version') == b'2026-03-10'
    assert not request.hasRawHeader('Authorization')
    assert request.attribute(QNetworkRequest.Attribute.RedirectPolicyAttribute) == QNetworkRequest.RedirectPolicy.ManualRedirectPolicy
    manager.reply.deliver()
    assert results == [(None, '', True)]
    assert not subject.check(manual=False)
    assert subject.check(manual=True)


@pytest.mark.parametrize('status', [403, 429])
def test_server_limit_blocks_manual_and_automatic(service, status):
    subject, manager, now, results = service
    subject.check()
    manager.reply.deliver(status=status, headers={b'Retry-After': b'7200', b'X-RateLimit-Reset': b'110000'})
    now[0] += 8000
    assert not subject.check()
    assert len(manager.requests) == 1
    now[0] = 110001
    assert subject.check()


def test_success_cache_is_per_channel_and_version(service):
    subject, manager, now, results = service
    subject.check(False)
    manager.reply.deliver()
    assert not subject.check(False)
    subject.set_channel('preview')
    assert subject.check(False)
    manager.reply.deliver(b'[]')
    assert results[-1][1] == ''
    subject.current = InstalledVersion('v1.0.1', 'b' * 40, 'stable', packaged=True)
    assert subject.check(False)


def test_bad_json_backoff_and_manual_retry(service):
    subject, manager, now, results = service
    subject.check(False)
    manager.reply.deliver(b'not json')
    assert 'JSON' in results[-1][1]
    assert not subject.check(False)
    assert subject.check(True)


def test_timeout_and_oversize_abort(service):
    subject, manager, now, results = service
    subject.check()
    subject._timeout.timeout.emit()
    assert manager.reply.aborted
    assert 'timed out' in results[-1][1]
    subject.check()
    manager.reply.deliver(b'x' * (MAX_RESPONSE_BYTES + 1))
    assert manager.reply.aborted
    assert 'size limit' in results[-1][1]
    assert not subject._payload


def test_source_and_disabled_auto_never_request(service, monkeypatch):
    subject, manager, now, results = service
    monkeypatch.setenv('SPECTRALSWEEP_DISABLE_UPDATE_CHECK', '1')
    assert not subject.check(False)
    subject.current = InstalledVersion('source', '', 'unknown')
    assert not subject.check(True)
    assert 'Git' in results[-1][1]
    assert not manager.requests


def test_shutdown_suppresses_late_results(service):
    subject, manager, now, results = service
    subject.check()
    subject.shutdown()
    manager.reply.deliver()
    assert not results
    assert not subject.check()


def test_poll_interval_only_extends_interval(service):
    subject, manager, now, results = service
    subject.check(False)
    manager.reply.deliver(headers={b'X-Poll-Interval': b'30000'})
    now[0] += 22000
    assert not subject.check(False)
    assert not subject.check(True)
    now[0] += 9000
    assert subject.check(False)


def test_automatic_preference_does_not_disable_manual(service):
    subject, manager, now, results = service
    subject.set_automatic_enabled(False)
    assert not subject.automatic_enabled
    assert not subject.check(False)
    assert subject.check(True)
    manager.reply.deliver()
    subject.set_automatic_enabled(True)
    assert subject.automatic_enabled
    with pytest.raises(ValueError):
        subject.set_channel('invalid')


def test_http_date_retry_after_and_failure_expiry(service):
    subject, manager, now, results = service
    subject.check()
    manager.reply.deliver(status=429, headers={b'Retry-After': b'Fri, 02 Jan 1970 12:00:00 GMT'})
    now[0] = 129599
    assert not subject.check()
    now[0] = 129601
    assert subject.check()


def test_transport_creation_failure_is_reported_without_busy(service):
    subject, manager, now, results = service
    def fail(request):
        raise RuntimeError('transport unavailable')
    manager.get = fail
    assert not subject.check()
    assert 'start' in results[-1][1]
    assert subject._reply is None
    assert not subject.check(False)


@pytest.mark.parametrize('value', [b'NaN', b'inf', b'-inf', b'-5', b'1e1000', b'garbage'])
def test_invalid_limit_headers_cannot_poison_future_checks(service, value):
    subject, manager, now, results = service
    subject.check()
    manager.reply.deliver(status=429, headers={
        b'Retry-After': value, b'X-RateLimit-Reset': value,
        b'X-Poll-Interval': value,
    })
    assert not subject.check()
    now[0] += 3601
    assert subject.check()


@pytest.mark.parametrize('value', ['nan', 'inf', '-inf', '-100', 'broken'])
def test_corrupt_cached_timestamps_do_not_permanently_disable_checks(service, value):
    subject, manager, now, results = service
    subject.settings.setValue('updates/serverRetryAfter', value)
    subject.settings.setValue(subject._key() + '/nextCheck', value)
    assert subject.check(False)


def test_exact_limit_and_chunked_overflow_release_memory(service):
    subject, manager, now, results = service
    subject.check()
    manager.reply.deliver(b'{}' + b' ' * (MAX_RESPONSE_BYTES - 2))
    assert results[-1] == (None, '', True)
    assert not subject._payload
    subject.check()
    reply = manager.reply
    reply.data = b'[' + b' ' * (MAX_RESPONSE_BYTES - 1)
    reply.readyRead.emit()
    assert len(subject._payload) == MAX_RESPONSE_BYTES
    reply.data = b']'
    reply.readyRead.emit()
    assert reply.aborted
    assert 'size limit' in results[-1][1]
    assert not subject._payload


def test_late_ready_read_after_shutdown_does_not_retain_data(service):
    subject, manager, now, results = service
    subject.check()
    manager.reply.data = b'['
    manager.reply.readyRead.emit()
    subject.shutdown()
    manager.reply.deliver(b'x' * 100000)
    assert not subject._payload
    assert not results


def test_stable_uses_latest_endpoint_and_preview_uses_list(service):
    subject, manager, now, results = service
    subject.check()
    assert manager.requests[-1].url().toString() == LATEST_RELEASE_API
    subject.set_channel('preview')  # In-flight response remains in its original domain.
    manager.reply.deliver(b'{}')
    assert results[-1][1] == ''
    subject.check()
    assert manager.requests[-1].url().toString() == RELEASES_API
    manager.reply.deliver(b'[]')
    assert results[-1][1] == ''


@pytest.mark.parametrize('payload', [b'[]', b'null', b'1', b'"release"'])
def test_latest_requires_object(service, payload):
    subject, manager, now, results = service
    subject.check()
    manager.reply.deliver(payload)
    assert 'release object' in results[-1][1]


def test_latest_404_explains_no_published_stable_release(service):
    subject, manager, now, results = service
    subject.check()
    manager.reply.deliver(status=404)
    assert 'No stable release' in results[-1][1]
    assert results[-1][2] is True
    subject.check(False)  # Failed auto check remains cached.
    assert len(manager.requests) == 1
