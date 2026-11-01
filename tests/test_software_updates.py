"""Offline update notices on a dummy window, without hardware services."""
from types import SimpleNamespace
import hashlib
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import QMainWindow

from app.application_updates import InstalledVersion, RELEASES_URL, REPOSITORY, UpdateRelease
from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.software_updates import SoftwareUpdateController


class MemorySettings:
    def __init__(self):
        self.data = {}

    def value(self, key, default=None):
        return self.data.get(key, default)

    def setValue(self, key, value):
        self.data[key] = value


class FakeService(QObject):
    finished = Signal(object, str, bool)
    busyChanged = Signal(bool)

    def __init__(self):
        super().__init__()
        self.current = InstalledVersion('build-9-aaaaaaaa', 'a' * 40, 'preview', 9,
                                        '2026-10-28T00:00:00Z', True)
        self.channel = 'preview'
        self.automatic_enabled = True
        self.settings = MemorySettings()
        self.checks = []
        self.starts = 0
        self.stops = 0

    def check(self, manual=True):
        self.checks.append(manual)
        if not self.current.can_check:
            self.finished.emit(None, 'Source builds update through Git.', manual)

    def start_automatic(self):
        self.starts += 1

    def set_channel(self, channel):
        self.channel = channel
        self.settings.setValue('updates/channel', channel)

    def set_automatic_enabled(self, enabled):
        self.automatic_enabled = bool(enabled)
        self.settings.setValue('updates/enabled', bool(enabled))

    def shutdown(self):
        self.stops += 1


def new_release(*, body='Release notes', html_url=None):
    tag = 'build-10-bbbbbbbb'
    return UpdateRelease(tag, 'New release',
                         html_url or f'https://github.com/{REPOSITORY}/releases/tag/{tag}',
                         f'https://github.com/{REPOSITORY}/releases/download/{tag}/SpectralSweep-{tag}-windows-x64-Setup.exe',
                         9, body, '2026-10-29T00:00:00Z', hashlib.sha256(b'installer').hexdigest())


class FakeDownload(QObject):
    progress = Signal(int, int)
    busyChanged = Signal(bool)
    finished = Signal(object, str)

    def __init__(self, folder):
        super().__init__()
        self.folder = folder
        self.busy = False
        self.verified_path = self.verified_release = None
        self.verified_sha256 = ''
        self.starts = []
        self.stops = 0

    def start(self, release):
        self.starts.append(release)
        self.busy = True
        self.busyChanged.emit(True)
        return True

    def complete(self):
        self.verified_path = self.folder / 'setup.exe'
        self.verified_path.write_bytes(b'installer')
        self.verified_release = self.starts[-1]
        self.verified_sha256 = hashlib.sha256(b'installer').hexdigest()
        self.busy = False
        self.busyChanged.emit(False)
        self.finished.emit(self.verified_path, '')

    def cancel(self):
        self.busy = False
        self.busyChanged.emit(False)
        self.finished.emit(None, 'Download cancelled.')

    def shutdown(self):
        self.stops += 1
        if self.busy:
            self.cancel()


class FakeHandoff(QObject):
    ready = Signal()
    failed = Signal(str)

    def __init__(self):
        super().__init__()
        self.requests = []
        self.arms = self.cancels = self.stops = 0

    def prepare(self, *args):
        self.requests.append(args)
        return True

    def arm(self):
        self.arms += 1
        return True

    def cancel(self):
        self.cancels += 1

    def shutdown(self):
        self.stops += 1


@pytest.fixture
def harness(tmp_path):
    application = quick_application()
    window = QMainWindow()
    service = FakeService()
    opened = []
    activity = {'busy': False}
    downloader = FakeDownload(tmp_path)
    handoff = FakeHandoff()
    controller = SoftwareUpdateController(window, service=service,
                                          busy=lambda: activity['busy'],
                                          open_url=lambda url: opened.append(url.toString()),
                                          downloader=downloader, handoff=handoff)
    state = SimpleNamespace(app=application, window=window, service=service,
                            controller=controller, opened=opened, activity=activity,
                            downloader=downloader, handoff=handoff)
    yield state
    controller.shutdown()
    QMainWindow.close(window)
    window.deleteLater()
    service.deleteLater()
    drain_qt_deletions()


def test_creation_and_opening_dialog_do_not_start_network_checks(harness):
    h = harness
    assert h.service.checks == [] and h.service.starts == 0
    h.controller.show_dialog()
    assert h.controller.dialog.isVisible()
    assert h.service.checks == [] and h.service.starts == 0
    h.controller.start_automatic()
    assert h.service.starts == 0  # Startup checking is deliberately delayed.
    assert h.controller._startup.isActive()


def test_manual_source_error_is_visible_and_does_not_claim_latest(harness):
    h = harness
    h.service.current = InstalledVersion('source', '', 'unknown')
    h.controller.action.trigger()
    assert h.service.checks == [True]
    assert h.controller.dialog.isVisible()
    assert h.controller.dialog.status.text() == 'Source builds update through Git.'
    assert not h.controller.dialog.ignore_button.isEnabled()


def test_automatic_errors_do_not_open_a_dialog(harness):
    harness.service.finished.emit(None, 'Network unavailable', False)
    assert harness.controller.dialog is None
    assert harness.opened == []


def test_manual_errors_are_shown_as_plain_text(harness):
    harness.service.finished.emit(None, '<b>Network unavailable</b>', True)
    dialog = harness.controller.dialog
    assert dialog.isVisible()
    assert dialog.status.text() == '<b>Network unavailable</b>'
    assert dialog.status.textFormat() == Qt.TextFormat.PlainText


def test_update_notice_waits_for_idle_and_only_opens_once(harness):
    h = harness
    h.activity['busy'] = True
    candidate = new_release()
    h.service.finished.emit(candidate, '', False)
    assert h.controller.dialog is None
    assert h.controller._pending
    h.controller._show_pending()
    assert h.controller.dialog is None
    h.activity['busy'] = False
    h.controller._show_pending()
    dialog = h.controller.dialog
    assert dialog.isVisible() and candidate.tag in dialog.status.text()
    assert not h.controller._pending
    dialog.close()
    h.service.finished.emit(candidate, '', False)
    h.controller._show_pending()
    assert not dialog.isVisible()


def test_ignored_release_suppresses_automatic_notice_but_manual_check_shows_it(harness):
    h = harness
    candidate = new_release()
    h.service.finished.emit(candidate, '', True)
    h.controller.dialog.ignore_button.click()
    assert h.service.settings.value('updates/ignoredTag/preview') == candidate.tag
    h.controller.dialog.close()
    h.service.finished.emit(candidate, '', False)
    assert not h.controller.dialog.isVisible()
    h.service.finished.emit(candidate, '', True)
    assert h.controller.dialog.isVisible()
    assert candidate.tag in h.controller.dialog.status.text()


def test_channel_and_automatic_preferences_are_saved_without_a_check(harness):
    h = harness
    h.controller.show_dialog()
    dialog = h.controller.dialog
    h.service.finished.emit(new_release(), '', True)
    dialog.channel.setCurrentIndex(dialog.channel.findData('stable'))
    assert h.service.channel == 'stable'
    assert h.service.settings.value('updates/channel') == 'stable'
    assert h.controller.release is None and dialog.notes.toPlainText() == ''
    assert not dialog.ignore_button.isEnabled()
    dialog.automatic.setChecked(False)
    assert h.service.settings.value('updates/enabled') is False
    assert not h.service.automatic_enabled
    assert h.service.checks == []


def test_disabling_automatic_check_cancels_a_pending_notice(harness):
    h = harness
    h.activity['busy'] = True
    h.service.finished.emit(new_release(), '', False)
    assert h.controller._pending
    h.controller.set_automatic(False)
    h.activity['busy'] = False
    h.controller._show_pending()
    assert not h.controller._pending and h.controller.dialog is None


def test_release_notes_remain_literal_plain_text(harness):
    notes = '<script>alert("hello")</script>\n<b>Important</b>\nhttps://example.invalid'
    harness.service.finished.emit(new_release(body=notes), '', True)
    dialog = harness.controller.dialog
    assert dialog.notes.isReadOnly()
    assert dialog.notes.toPlainText() == notes
    assert dialog.status.textFormat() == Qt.TextFormat.PlainText


def test_secondary_release_page_opens_browser_without_installing(harness, monkeypatch):
    import os
    import subprocess
    from PySide6.QtCore import QProcess

    def forbidden_process(*args, **kwargs):
        pytest.fail('Opening a release page must not install or restart a process')

    monkeypatch.setattr(subprocess, 'Popen', forbidden_process)
    monkeypatch.setattr(subprocess, 'run', forbidden_process)
    monkeypatch.setattr(os, 'startfile', forbidden_process, raising=False)
    monkeypatch.setattr(QProcess, 'startDetached', forbidden_process)
    h = harness
    candidate = new_release()
    h.service.finished.emit(candidate, '', True)
    h.controller.dialog.release_page_button.click()
    assert h.opened == [candidate.html_url]
    assert h.service.stops == 0
    assert h.controller.dialog.isVisible()
    assert h.service.checks == []


def test_no_selected_release_opens_official_release_index(harness):
    harness.controller.open_download()
    assert harness.opened == [RELEASES_URL]


def test_direct_download_progress_completion_and_no_browser_or_install(harness):
    h = harness
    release = new_release()
    h.service.finished.emit(release, '', True)
    dialog = h.controller.dialog
    assert dialog.download_button.isEnabled() and not dialog.install_button.isEnabled()
    dialog.download_button.click()
    assert h.downloader.starts == [release] and h.opened == []
    assert dialog.cancel_button.isVisible()
    assert not dialog.check_button.isEnabled() and not dialog.channel.isEnabled()
    assert not h.controller.start_download()
    h.downloader.progress.emit(4, 9)
    assert dialog.progress.value() == 444
    h.downloader.complete()
    assert dialog.progress.value() == 1000
    assert dialog.install_button.isEnabled() and not dialog.download_button.isEnabled()
    assert h.handoff.requests == [] and h.handoff.arms == 0


def test_cancel_download_and_retry(harness):
    h = harness
    h.service.finished.emit(new_release(), '', True)
    h.controller.dialog.download_button.click()
    h.controller.dialog.cancel_button.click()
    assert 'cancelled' in h.controller.dialog.status.text()
    assert h.controller.dialog.download_button.isEnabled()
    assert h.controller.start_download()
    assert len(h.downloader.starts) == 2 and not h.handoff.requests


def test_download_during_measurement_but_install_waits_for_idle(harness):
    h = harness
    h.activity['busy'] = True
    h.service.finished.emit(new_release(), '', True)
    assert h.controller.start_download()
    h.downloader.complete()
    assert not h.controller.dialog.install_button.isEnabled()
    assert not h.controller.install_update() and not h.handoff.requests
    h.activity['busy'] = False
    h.controller._refresh_actions()
    assert h.controller.dialog.install_button.isEnabled()


def prepare_install(h):
    h.service.finished.emit(new_release(), '', True)
    h.controller.start_download()
    h.downloader.complete()
    assert h.controller.install_update()


def test_new_measurement_blocks_pending_install_before_close(harness, monkeypatch):
    h = harness
    prepare_install(h)
    monkeypatch.setattr(h.window, 'close', lambda: pytest.fail('Active acquisition must not be closed'))
    h.activity['busy'] = True
    h.handoff.ready.emit()
    assert h.handoff.cancels == 1 and h.handoff.arms == 0
    assert not h.controller._installing


def test_close_veto_cancels_handoff_and_preserves_app(harness, monkeypatch):
    h = harness
    prepare_install(h)
    monkeypatch.setattr(h.window, 'close', lambda: False)
    h.handoff.ready.emit()
    assert h.handoff.cancels == 1 and h.handoff.arms == 0
    assert not h.controller._closed and not h.controller._installing
    assert h.controller.dialog.install_button.isEnabled()


def test_accepted_close_arms_helper_after_normal_controller_shutdown(harness, monkeypatch):
    h = harness
    prepare_install(h)
    def normal_close():
        h.controller.shutdown()
        return True
    monkeypatch.setattr(h.window, 'close', normal_close)
    h.handoff.ready.emit()
    assert h.controller._closed
    assert h.handoff.arms == 1 and h.handoff.stops == 0 and h.handoff.cancels == 0
    assert h.service.stops == 1 and h.downloader.stops == 1


def test_nested_close_confirmation_cannot_cancel_an_accepted_install(harness, monkeypatch):
    h = harness
    prepare_install(h)
    def normal_close():
        h.controller.cancel_install()  # A nested modal event loop can deliver queued clicks.
        h.controller.dialog.close()
        assert h.controller._installing and h.handoff.cancels == 0
        h.controller.shutdown()
        return True
    monkeypatch.setattr(h.window, 'close', normal_close)
    h.handoff.ready.emit()
    assert h.handoff.arms == 1 and h.handoff.cancels == 0


def test_missing_cached_file_can_be_downloaded_again(harness):
    h = harness
    h.service.finished.emit(new_release(), '', True)
    h.controller.start_download()
    h.downloader.complete()
    Path(h.downloader.verified_path).unlink()
    h.controller._refresh_actions()
    assert not h.controller.dialog.install_button.isEnabled()
    assert h.controller.dialog.download_button.isEnabled()


def test_cancel_preparation_keeps_verified_download_and_rejects_late_ready(harness):
    h = harness
    prepare_install(h)
    h.controller.dialog.cancel_button.click()
    assert h.handoff.cancels == 1 and not h.controller._installing
    h.handoff.ready.emit()
    assert h.handoff.arms == 0 and not h.controller._closed
    assert h.controller.dialog.install_button.isEnabled()


def test_closing_dialog_cancels_install_preparation_but_not_background_download(harness):
    h = harness
    h.service.finished.emit(new_release(), '', True)
    h.controller.start_download()
    h.controller.dialog.close()
    assert h.downloader.busy
    h.downloader.complete()
    h.controller.show_dialog()
    assert h.controller.install_update()
    h.controller.dialog.close()
    assert h.handoff.cancels == 1 and not h.controller._installing


@pytest.mark.parametrize('url', [
    'https://evil.example/download.exe',
    f'https://github.com/{REPOSITORY}/releases/tag/build-10-bbbbbbbb?redirect=1',
    f'https://user@github.com/{REPOSITORY}/releases/tag/build-10-bbbbbbbb',
    'https://github.com/another/repository/releases/tag/build-10-bbbbbbbb',
])
def test_untrusted_download_page_does_not_open_a_browser(harness, url):
    harness.controller.release = new_release(html_url=url)
    harness.controller.open_download()
    assert harness.opened == []


def test_shutdown_blocks_late_results_and_is_idempotent(harness):
    h = harness
    h.controller.check_manual()
    dialog = h.controller.dialog
    before = dialog.status.text()
    h.controller.shutdown()
    h.service.finished.emit(new_release(), '', True)
    h.service.finished.emit(None, 'Late error', True)
    h.controller._show_pending()
    h.controller.check_manual()
    h.controller.start_automatic()
    h.controller.shutdown()
    assert not dialog.isVisible()
    assert dialog.status.text() == before
    assert h.service.checks == [True] and h.service.starts == 0
    assert h.service.stops == 1
    assert h.opened == []


@pytest.mark.parametrize('state', ['acquisition', 'sensor-preflight', 'capture', 'direct-preflight', 'direct-capture',
                                   'metadata-finalization', 'session-finalization', 'plan-build',
                                   'mcd-thread', 'mcd2100-thread', 'mcd-preparation'])
def test_default_busy_guard_defers_without_constructing_hardware(harness, state):
    h = harness
    h.controller._busy = h.controller._measurement_active
    if state == 'acquisition':
        h.window._acquisition_active = lambda: True
    elif state in ('direct-preflight', 'direct-capture'):
        key = 'sensor_preflight_pending' if state == 'direct-preflight' else 'acquisition_in_progress'
        h.window._lf6 = SimpleNamespace(**{key: True})
    elif state == 'metadata-finalization':
        h.window._presets = SimpleNamespace(metadata_finalizing=True)
    elif state == 'session-finalization':
        h.window._session_panels = {'page': SimpleNamespace(_metadata_finalizing=True)}
    elif state == 'plan-build':
        h.window._presets = SimpleNamespace(plan_build_running=lambda: True)
    elif state in ('mcd-thread', 'mcd2100-thread'):
        key = '_thread' if state == 'mcd-thread' else 'thread'
        h.window._session_panels = {'page': SimpleNamespace(**{key: SimpleNamespace(isRunning=lambda: True)})}
    elif state == 'mcd-preparation':
        h.window._session_panels = {'page': SimpleNamespace(_safe_magnet_operation_active=True)}
    else:
        key = 'sensor_preflight_pending' if state == 'sensor-preflight' else 'acquisition_in_progress'
        h.window._lf6 = SimpleNamespace(capture_status={key: True})
    h.service.finished.emit(new_release(), '', False)
    assert h.controller.dialog is None and h.controller._pending
    if state in ('direct-preflight', 'direct-capture'):
        setattr(h.window._lf6, key, False)
        h.controller._show_pending()
        assert h.controller.dialog.isVisible()
        assert not h.controller._pending


def test_active_modal_dialog_defers_update_notice_until_closed(harness):
    from PySide6.QtWidgets import QDialog

    h = harness
    modal = QDialog(h.window)
    modal.setModal(True)
    modal.show()
    h.app.processEvents()
    try:
        h.service.finished.emit(new_release(), '', False)
        assert h.controller.dialog is None and h.controller._pending
        modal.close()
        h.app.processEvents()
        h.controller._show_pending()
        assert h.controller.dialog.isVisible()
        assert not h.controller._pending
    finally:
        modal.close()
        modal.deleteLater()
