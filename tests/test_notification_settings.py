import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from app import notification_config
from ui.notification_settings import NotificationSettings


class NotificationSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "notifications.json"
        self.patcher = patch.object(notification_config, "config_path", return_value=self.path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def panel(self):
        panel = NotificationSettings()
        self.addCleanup(panel.deleteLater)
        return panel

    def test_save_locks_and_reopen_preserves_subscription(self):
        panel = self.panel()
        self.assertFalse(self.path.exists())
        self.assertFalse(panel.save_button.isEnabled())
        panel.name_edit.setText("Attodry-PC")
        emitted = []
        panel.configured.connect(emitted.append)
        panel.save_button.click()
        url = notification_config.get_ntfy_url()
        self.assertEqual(emitted, [url])
        self.assertTrue(panel.name_edit.isReadOnly())
        self.assertFalse(panel.save_button.isEnabled())
        self.assertEqual(panel.url_edit.text(), url)
        panel.copy_button.click()
        self.assertEqual(self.app.clipboard().text(), url)
        reopened = self.panel()
        self.assertEqual(reopened.name_edit.text(), "Attodry-PC")
        self.assertEqual(reopened.url_edit.text(), url)
        self.assertFalse(reopened.save_button.isEnabled())

    def test_blank_name_and_write_failure_do_not_lock(self):
        panel = self.panel()
        panel.name_edit.setText("   ")
        self.assertFalse(panel.save_button.isEnabled())
        panel.name_edit.setText("PC")
        with patch.object(notification_config, "configure", side_effect=PermissionError("Access denied")):
            panel.save_button.click()
        self.assertIn("Access denied", panel.status_label.text())
        self.assertFalse(panel.name_edit.isReadOnly())
        self.assertTrue(panel.save_button.isEnabled())
        self.assertFalse(self.path.exists())

    def test_damaged_config_cannot_be_overwritten(self):
        self.path.write_text("broken")
        panel = self.panel()
        self.assertFalse(panel.save_button.isEnabled())
        self.assertIn("unavailable", panel.status_label.text().lower())
        self.assertEqual(self.path.read_text(), "broken")

    def test_existing_long_url_can_be_shortened_without_unlocking_name(self):
        old_url = "https://ntfy.sh/lab-spectra-sweep-9f4c2a7e-attodry2100-1234567890abcdef"
        self.path.write_text(json.dumps({"name": "Attodry2100", "url": old_url}))
        panel = self.panel()
        self.assertFalse(panel.shorten_button.isHidden())
        panel.shorten_button.click()
        self.assertIn("/ss-attodry2100-", panel.url_edit.text())
        self.assertTrue(panel.name_edit.isReadOnly())
        self.assertTrue(panel.shorten_button.isHidden())
        self.assertIn("Restart", panel.status_label.text())

    def test_save_activates_notifier_and_watchdog_without_restart(self):
        from app.ntfy_notifications import NtfyNotifier
        from app.process_watchdog import WatchdogSession
        notifier = NtfyNotifier()
        self.addCleanup(notifier.shutdown)
        watchdog = WatchdogSession(None)
        panel = self.panel()
        panel.configured.connect(notifier.set_url)
        panel.configured.connect(watchdog.enable)
        with patch.object(watchdog, "start") as start:
            panel.name_edit.setText("Live-PC")
            panel.save_button.click()
            self.assertEqual(notifier.URL, panel.url_edit.text())
            self.assertEqual(watchdog.url, notifier.URL)
            start.assert_called_once()
