"""One-time machine notification setup, separate from portable app settings."""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QApplication, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
)

from app import notification_config


class NotificationSettings(QGroupBox):
    configured = Signal(str)

    def __init__(self, parent=None):
        super().__init__("PC Notifications", parent)
        form = QFormLayout(self)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Attodry-PC")
        self.name_edit.setAccessibleName("PC notification name")
        form.addRow("PC name:", self.name_edit)
        self.save_button = QPushButton("Save and lock PC name")
        self.save_button.setToolTip("Creates this PC's permanent notification subscription.")
        form.addRow("", self.save_button)
        self.url_edit = QLineEdit()
        self.url_edit.setReadOnly(True)
        self.url_edit.setAccessibleName("ntfy subscription URL")
        self.url_edit.setPlaceholderText("Available after setup")
        self.copy_button = QPushButton("Copy URL")
        row = QHBoxLayout()
        row.addWidget(self.url_edit, 1)
        row.addWidget(self.copy_button)
        form.addRow("Subscription:", row)
        self.shorten_button = QPushButton("Use shorter URL (restart required)")
        self.shorten_button.setToolTip("Changes the subscription URL. Subscribe to the new URL after restarting the app.")
        form.addRow("", self.shorten_button)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        form.addRow(self.status_label)
        self.name_edit.textChanged.connect(self._update_save)
        self.save_button.clicked.connect(self._save)
        self.copy_button.clicked.connect(self._copy)
        self.shorten_button.clicked.connect(self._shorten)
        self._load()

    def _update_save(self):
        self.save_button.setEnabled(
            not self.name_edit.isReadOnly() and bool(self.name_edit.text().strip())
        )

    def _load(self):
        self.copy_button.setEnabled(False)
        self.shorten_button.hide()
        try:
            data = notification_config.read_config()
        except (OSError, ValueError) as exc:
            self.name_edit.setReadOnly(True)
            self.status_label.setText(f"Notifications unavailable. Restore the saved configuration. {exc}")
        else:
            self.name_edit.setReadOnly(data is not None)
            if data:
                self.name_edit.setText(data["name"])
                self.url_edit.setText(data["url"])
                self.copy_button.setEnabled(True)
                self.shorten_button.setVisible(not notification_config.is_short_url(data["url"]))
                self.status_label.setText(
                    "PC name is locked. Subscribe to this URL in ntfy to receive this PC's alerts."
                )
            else:
                self.status_label.setText(
                    "Choose a name once. Saving locks the name and subscription on this PC. "
                    "Notifications remain off until setup is saved."
                )
        self._update_save()

    def _save(self):
        if self.name_edit.isReadOnly():
            return
        try:
            url = notification_config.configure(self.name_edit.text())
        except FileExistsError:
            self._load()
            if self.url_edit.text():
                self.configured.emit(self.url_edit.text())
            return
        except (OSError, ValueError) as exc:
            self.status_label.setText(f"Could not save PC name: {exc}")
            return
        self._load()
        self.configured.emit(url)

    def _copy(self):
        QApplication.clipboard().setText(self.url_edit.text())

    def _shorten(self):
        try:
            notification_config.shorten_url()
        except (OSError, ValueError) as exc:
            self.status_label.setText(f"Could not shorten URL: {exc}")
            return
        self._load()
        self.status_label.setText(
            "Restart the app to use this shorter URL, then subscribe to it in ntfy. "
            "The old subscription will stop receiving alerts after restart. PC name remains locked."
        )
