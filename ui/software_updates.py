"""Native, nonmodal update notices. Downloads remain an explicit browser action."""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QToolButton, QVBoxLayout,
)

from app.application_updates import RELEASES_URL, REPOSITORY, UpdateRelease
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
        hint = QLabel('Your browser opens the official release page. Install after stopping measurements and closing SpectralSweep.')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QHBoxLayout()
        self.check_button = QPushButton('&Check now')
        self.download_button = QPushButton('&Open download page')
        self.ignore_button = QPushButton('&Ignore version')
        close = QPushButton('C&lose')
        for button in (self.check_button, self.download_button, self.ignore_button, close):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.check_button.clicked.connect(controller.check_manual)
        self.download_button.clicked.connect(controller.open_download)
        self.ignore_button.clicked.connect(controller.ignore_version)
        close.clicked.connect(self.close)
        self.channel.currentIndexChanged.connect(controller.change_channel)
        self.automatic.toggled.connect(controller.set_automatic)
        self.download_button.setEnabled(True)
        self.ignore_button.setEnabled(False)


class SoftwareUpdateController(QObject):
    def __init__(self, window, *, service=None, busy=None, open_url=None):
        super().__init__(window)
        self.window = window
        self.service = service if service is not None else UpdateService(self)
        self._busy = busy or self._measurement_active
        self._open_url = open_url or QDesktopServices.openUrl
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
        return (bool(getattr(presets, 'metadata_finalizing', False)) or
                any(getattr(panel, '_metadata_finalizing', False)
                    for panel in getattr(self.window, '_session_panels', {}).values()))

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
        if self.dialog:
            self.dialog.check_button.setEnabled(not busy)
            self.dialog.channel.setEnabled(not busy)

    def _ignored(self):
        return self.service.settings.value('updates/ignoredTag/' + self.service.channel, '')

    def _finished(self, release, error, manual):
        if self._closed:
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
        self.service.set_channel(self.dialog.channel.currentData())
        self.release = None
        self._pending = False
        self._notice_timer.stop()
        self.indicator.setText('Updates')
        self.dialog.notes.clear()
        self.dialog.ignore_button.setEnabled(False)
        self.dialog.status.setText('Channel changed. Check now to refresh the available version.')

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
        self.service.shutdown()
        if self.dialog:
            self.dialog.close()
