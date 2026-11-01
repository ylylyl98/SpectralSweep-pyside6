"""Native update notices, verified downloads and explicit install handoff."""
from __future__ import annotations

from pathlib import Path
import sys

from PySide6.QtCore import QObject, QSignalBlocker, QTimer, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QToolButton, QVBoxLayout,
)

from app.application_updates import RELEASES_URL, REPOSITORY, UpdateRelease, validate_download_release
from app.update_download import UpdateDownload
from app.update_install import InstallHandoff
from app.update_service import UpdateService


class SoftwareUpdateDialog(QDialog):
    def __init__(self, controller):
        super().__init__(controller.window)
        self.setObjectName('SoftwareUpdateDialog')
        self.setWindowTitle('Software updates')
        self.resize(640, 480)
        self.controller = controller
        layout = QVBoxLayout(self)
        version = QLabel(f'Current version: {controller.service.current.version}')
        version.setTextFormat(Qt.TextFormat.PlainText)
        version.setWordWrap(True)
        layout.addWidget(version)
        channels = QHBoxLayout()
        label = QLabel('&Update channel:')
        self.channel = QComboBox()
        self.channel.setAccessibleName('Update channel')
        self.channel.addItem('Stable releases', 'stable')
        self.channel.addItem('Include prereleases', 'preview')
        self.channel.setCurrentIndex(self.channel.findData(controller.service.channel))
        label.setBuddy(self.channel)
        channels.addWidget(label)
        channels.addWidget(self.channel, 1)
        layout.addLayout(channels)
        self.automatic = QCheckBox('Check &automatically (every 6 hours while running)')
        self.automatic.setChecked(controller.service.automatic_enabled)
        layout.addWidget(self.automatic)
        self.status = QLabel('Check GitHub Releases for a newer Windows installer.')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setAccessibleName('Update status')
        layout.addWidget(self.status)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setAccessibleName('Release notes')
        self.notes.setPlaceholderText('Release notes appear here when an update is available.')
        layout.addWidget(self.notes, 1)
        self.progress = QProgressBar()
        self.progress.setAccessibleName('Update download progress')
        self.progress.setRange(0, 1000)
        self.progress.hide()
        layout.addWidget(self.progress)
        hint = QLabel('Download in the background. Install and restart saves your settings and closes SpectralSweep normally. Stop measurements before installing.')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        primary = QHBoxLayout()
        self.download_button = QPushButton('&Download update')
        self.install_button = QPushButton('Install and &restart')
        self.cancel_button = QPushButton('Cancel &download')
        self.cancel_button.hide()
        for button in (self.download_button, self.install_button, self.cancel_button):
            primary.addWidget(button)
        primary.addStretch()
        layout.addLayout(primary)
        buttons = QHBoxLayout()
        self.check_button = QPushButton('&Check now')
        self.release_page_button = QPushButton('Release &page')
        self.ignore_button = QPushButton('&Ignore version')
        close = QPushButton('C&lose')
        for button in (self.check_button, self.release_page_button, self.ignore_button, close):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.check_button.clicked.connect(controller.check_manual)
        self.download_button.clicked.connect(controller.start_download)
        self.install_button.clicked.connect(controller.install_update)
        self.cancel_button.clicked.connect(controller.cancel_pending)
        self.release_page_button.clicked.connect(controller.open_download)
        self.ignore_button.clicked.connect(controller.ignore_version)
        close.clicked.connect(self.close)
        self.channel.currentIndexChanged.connect(controller.change_channel)
        self.automatic.toggled.connect(controller.set_automatic)
        self.download_button.setEnabled(False)
        self.install_button.setEnabled(False)
        self.ignore_button.setEnabled(False)

    def closeEvent(self, event):
        if self.controller._closing_for_update and not self.controller._closed:
            event.ignore()
            return
        self.controller._activity_timer.stop()
        if not self.controller._closing_for_update and not self.controller._closed:
            self.controller.cancel_install()
        super().closeEvent(event)


class SoftwareUpdateController(QObject):
    def __init__(self, window, *, service=None, busy=None, open_url=None,
                 downloader=None, handoff=None, install_dir=None):
        super().__init__(window)
        self.window = window
        self.service = service if service is not None else UpdateService(self)
        self._busy = busy or self._measurement_active
        self._open_url = open_url or QDesktopServices.openUrl
        self.downloader = downloader if downloader is not None else UpdateDownload(self)
        self.handoff = handoff if handoff is not None else InstallHandoff(self)
        self._install_dir = Path(install_dir) if install_dir is not None else Path(sys.executable).parent
        self._installing = False
        self._closing_for_update = False
        self.dialog = None
        self.release = None
        self._closed = False
        self._pending = False
        self._notified = set()
        self._checking = False
        self._startup = QTimer(self)
        self._startup.setSingleShot(True)
        self._startup.setInterval(15_000)
        self._startup.timeout.connect(self.service.start_automatic)
        self._notice_timer = QTimer(self)
        self._notice_timer.setInterval(5_000)
        self._notice_timer.timeout.connect(self._show_pending)
        menu = next((action.menu() for action in window.menuBar().actions()
                     if action.text().replace('&', '').lower() == 'help'), None)
        if menu is None:
            menu = window.menuBar().addMenu('&Help')
        self.action = QAction('Check for &updates…', self)
        self.action.setObjectName('CheckForSoftwareUpdates')
        menu.addAction(self.action)
        self.action.triggered.connect(self.check_manual)
        self.indicator = QToolButton(window)
        self.indicator.setObjectName('SoftwareUpdateStatusButton')
        self.indicator.setText('Updates')
        self.indicator.setToolTip('Check for software updates')
        self.indicator.clicked.connect(self.show_dialog)
        window.statusBar().addPermanentWidget(self.indicator)
        self.service.finished.connect(self._finished)
        self.service.busyChanged.connect(self._set_busy)
        self.downloader.progress.connect(self._download_progress)
        self.downloader.busyChanged.connect(lambda *_: self._refresh_actions())
        self.downloader.finished.connect(self._download_finished)
        self.handoff.ready.connect(self._handoff_ready)
        self.handoff.failed.connect(self._handoff_failed)
        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(1000)
        self._activity_timer.timeout.connect(self._refresh_actions)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    def _measurement_active(self):
        check = getattr(self.window, '_acquisition_active', None)
        if check and check():
            return True
        status = getattr(getattr(self.window, '_lf6', None), 'capture_status', {})
        if isinstance(status, dict) and any(status.get(key) for key in (
                'active', 'acquisition_in_progress', 'sensor_preflight_pending')):
            return True
        lf6 = getattr(self.window, '_lf6', None)
        if any(getattr(lf6, name, False) is True for name in (
                'acquisition_in_progress', 'sensor_preflight_pending')):
            return True
        presets = getattr(self.window, '_presets', None)
        panels = [presets, *getattr(self.window, '_session_panels', {}).values()]
        for panel in panels:
            if (getattr(panel, 'metadata_finalizing', False)
                    or getattr(panel, '_metadata_finalizing', False)
                    or getattr(panel, '_safe_magnet_operation_active', False)):
                return True
            building = getattr(panel, 'plan_build_running', None)
            if callable(building) and building():
                return True
            for name in ('_run_thread', '_thread', 'thread'):
                thread = getattr(panel, name, None)
                running = getattr(thread, 'isRunning', None)
                if callable(running) and running():
                    return True
        return False

    def start_automatic(self):
        if not self._closed and self.service.current.can_check:
            self._startup.start()

    def show_dialog(self):
        if self._closed:
            return
        if self.dialog is None:
            self.dialog = SoftwareUpdateDialog(self)
            self._set_busy(self._checking)
            if self.release:
                self._render_result()
        self.dialog.show()
        self._activity_timer.start()
        self._refresh_actions()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def check_manual(self):
        if self._closed:
            return
        self.show_dialog()
        self.dialog.status.setText('Checking GitHub Releases…')
        self.service.check(manual=True)

    def _set_busy(self, busy):
        self._checking = bool(busy)
        self._refresh_actions()

    def _download_ready(self):
        return (self.release is not None and self.downloader.verified_path is not None
                and Path(self.downloader.verified_path).is_file()
                and self.downloader.verified_release == self.release)

    def _refresh_actions(self):
        if self._closed or self.dialog is None:
            return
        downloading = self.downloader.busy
        locked = downloading or self._installing or self._checking
        self.dialog.check_button.setEnabled(not locked)
        self.dialog.channel.setEnabled(not locked)
        self.dialog.ignore_button.setEnabled(self.release is not None and not locked)
        downloadable = False
        download_error = ''
        if self.release is not None:
            try:
                validate_download_release(self.release)
                downloadable = True
            except ValueError as exc:
                download_error = str(exc)
        ready = self._download_ready()
        self.dialog.download_button.setEnabled(downloadable and not locked and not ready)
        self.dialog.download_button.setToolTip(download_error)
        self.dialog.cancel_button.setVisible(downloading or self._installing)
        self.dialog.cancel_button.setText('Cancel &download' if downloading else 'Cancel &installation')
        self.dialog.install_button.setEnabled(ready and not locked and not self._busy())
        self.dialog.install_button.setToolTip(
            'Stop measurements and wait for saving to finish before installing.' if self._busy()
            else 'Close safely, install the verified update, and reopen SpectralSweep.')

    def start_download(self):
        if self._closed or self._installing or self.downloader.busy or self.release is None:
            return False
        self.show_dialog()
        self.dialog.progress.setRange(0, 0)
        self.dialog.progress.show()
        self.dialog.status.setText('Downloading and verifying the update…')
        result = self.downloader.start(self.release)
        self._refresh_actions()
        return result

    def cancel_download(self):
        if not self._closed:
            self.downloader.cancel()

    def cancel_pending(self):
        if self.downloader.busy:
            self.cancel_download()
        else:
            self.cancel_install()

    def cancel_install(self):
        if self._closed or self._closing_for_update or not self._installing:
            return
        self.handoff.cancel()
        self._installing = False
        if self.dialog:
            self.dialog.status.setText('Installation cancelled. The verified download is ready when you are.')
        self._refresh_actions()

    def _download_progress(self, received, total):
        if self._closed or self.dialog is None:
            return
        self.dialog.progress.setRange(0, 1000)
        self.dialog.progress.setValue(round(1000 * received / total) if total else 0)
        self.dialog.status.setText(f'Downloading update: {received / 1024**2:.1f} / {total / 1024**2:.1f} MB')

    def _download_finished(self, path, error):
        if self._closed:
            return
        if self.dialog is not None:
            self.dialog.progress.setRange(0, 1000)
            if error:
                self.dialog.progress.hide()
                self.dialog.status.setText(error)
            else:
                self.dialog.progress.setValue(1000)
                self.dialog.status.setText('Download verified. Stop measurements before installing.' if self._busy()
                                           else 'Download verified. Ready to install and restart.')
        self.indicator.setText('Update ready' if path is not None and not error else 'Update available')
        self._refresh_actions()

    def install_update(self):
        if self._closed or self._installing or not self._download_ready():
            return False
        if self._busy():
            self.dialog.status.setText('Stop measurements and wait for saving to finish before installing.')
            return False
        self._installing = True
        self.dialog.status.setText('Preparing installation. SpectralSweep will close safely before Setup starts…')
        self._refresh_actions()
        mode = getattr(self.window, '_ui_mode', 'quick')
        return self.handoff.prepare(self.downloader.verified_path, self.downloader.verified_sha256,
                                    self._install_dir, ['--ui', mode])

    def _handoff_ready(self):
        if self._closed or not self._installing:
            return
        if self._busy():
            self.handoff.cancel()
            self._handoff_failed('A measurement started. Stop it before installing the update.')
            return
        self._closing_for_update = True
        self.dialog.setEnabled(False)
        try:
            accepted = self.window.close()
        except Exception as exc:
            self.handoff.cancel()
            self._handoff_failed(f'Could not close safely: {exc}')
            return
        finally:
            self._closing_for_update = False
            if not self._closed:
                self.dialog.setEnabled(True)
        if not accepted:
            self.handoff.cancel()
            self._handoff_failed('Installation cancelled because SpectralSweep stayed open. You can try again when ready.')
        elif not self.handoff.arm():
            QMessageBox.critical(None, 'Update could not start',
                                 'The update could not be started. Reopen SpectralSweep and try again.')

    def _handoff_failed(self, message):
        self._installing = False
        if self._closed:
            return
        self.show_dialog()
        self.dialog.status.setText(message)
        self._refresh_actions()

    def _ignored(self):
        return self.service.settings.value('updates/ignoredTag/' + self.service.channel, '')

    def _finished(self, release, error, manual):
        if self._closed or self.downloader.busy or self._installing:
            return
        if error:
            if manual:
                self.show_dialog()
                self.dialog.status.setText(error)
            return
        self.release = release
        self._pending = False
        self._notice_timer.stop()
        self.indicator.setText('Update available' if release else 'Updates')
        if release:
            self.indicator.setToolTip('Available version: ' + release.tag)
        if manual:
            self.show_dialog()
            self._render_result()
        elif release and release.tag != self._ignored() and release.tag not in self._notified:
            self._pending = True
            self._notice_timer.start()
            self._show_pending()
        elif self.dialog:
            self._render_result()

    def _render_result(self):
        if not self.dialog:
            return
        release = self.release
        self.dialog.status.setText(
            f'New version available: {release.tag}' if release else
            'No newer compatible Windows installer was found in this channel.')
        self.dialog.notes.setPlainText(release.body if release else '')
        self.dialog.ignore_button.setEnabled(release is not None)
        self._refresh_actions()

    def _show_pending(self):
        if self._closed or not self._pending or not self.service.automatic_enabled:
            self._notice_timer.stop()
            return
        if self._busy() or QApplication.activeModalWidget() is not None:
            return
        self._pending = False
        self._notice_timer.stop()
        self._notified.add(self.release.tag)
        self.show_dialog()
        self._render_result()

    def change_channel(self):
        if self.downloader.busy or self._installing:
            with QSignalBlocker(self.dialog.channel):
                self.dialog.channel.setCurrentIndex(self.dialog.channel.findData(self.service.channel))
            return
        self.service.set_channel(self.dialog.channel.currentData())
        self.release = None
        self._pending = False
        self._notice_timer.stop()
        self.indicator.setText('Updates')
        self.dialog.notes.clear()
        self.dialog.ignore_button.setEnabled(False)
        self.dialog.status.setText('Channel changed. Check now to refresh the available version.')
        self.dialog.progress.hide()
        self._refresh_actions()

    def set_automatic(self, enabled):
        self.service.set_automatic_enabled(enabled)
        if not enabled:
            self._pending = False
            self._notice_timer.stop()

    def ignore_version(self):
        if self.release:
            self.service.settings.setValue('updates/ignoredTag/' + self.service.channel, self.release.tag)
            self.dialog.status.setText('Automatic notices disabled for ' + self.release.tag)
            self._pending = False
            self._notice_timer.stop()

    def open_download(self):
        url = RELEASES_URL
        if self.release:
            expected = f'https://github.com/{REPOSITORY}/releases/tag/{self.release.tag}'
            if self.release.html_url != expected or not isinstance(self.release, UpdateRelease):
                return
            url = expected
        self._open_url(QUrl(url))

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._startup.stop()
        self._notice_timer.stop()
        self._activity_timer.stop()
        self.service.shutdown()
        self.downloader.shutdown()
        if not self._closing_for_update:
            self.handoff.shutdown()
        if self.dialog:
            self.dialog.close()
