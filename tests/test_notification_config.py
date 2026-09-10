import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import notification_config


class NotificationConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "notifications.json"

    def test_each_computer_has_a_stable_separate_topic(self):
        first = notification_config.configure("Attodry-PC", self.path)
        self.assertEqual(first, notification_config.get_ntfy_url(self.path))
        self.assertRegex(first, r"^https://ntfy\.sh/ss-attodry-pc-[abcdefghjkmnpqrstuvwxyz23456789]{6}$")
        self.assertEqual(notification_config.read_config(self.path)["name"], "Attodry-PC")
        second = notification_config.configure("Attodry-PC", self.path.with_name("other.json"))
        self.assertNotEqual(first, second)

    def test_unsafe_names_are_safe_and_do_not_collapse(self):
        first = notification_config.configure("Lab PC/温度", self.path)
        self.assertRegex(first.rsplit("/", 1)[1], r"^[a-z0-9-]+$")

    def test_cannot_reconfigure(self):
        first = notification_config.configure("Attodry-PC", self.path)
        with self.assertRaises(FileExistsError):
            notification_config.configure("Magnet-PC", self.path)
        self.assertEqual(notification_config.get_ntfy_url(self.path), first)

    def test_long_names_fit_topic_length_limit(self):
        url = notification_config.configure("a" * 100, self.path)
        self.assertLessEqual(len(url.rsplit("/", 1)[1]), 26)
        self.assertIn("ss-" + "a" * 16 + "-", url)

    def test_existing_long_subscription_is_preserved(self):
        old_url = "https://ntfy.sh/lab-spectra-sweep-9f4c2a7e-attodry-pc-1234567890abcdef"
        self.path.write_text(json.dumps({"name": "Attodry-PC", "url": old_url}))
        self.assertEqual(notification_config.get_ntfy_url(self.path), old_url)

    def test_explicit_shortening_keeps_name_and_previous_url_and_is_stable(self):
        old_url = "https://ntfy.sh/lab-spectra-sweep-9f4c2a7e-attodry2100-1234567890abcdef"
        self.path.write_text(json.dumps({"name": "Attodry2100", "url": old_url}))
        url = notification_config.shorten_url(self.path)
        self.assertRegex(url, r"^https://ntfy\.sh/ss-attodry2100-[a-z2-9]{6}$")
        saved = notification_config.read_config(self.path)
        self.assertEqual(saved["name"], "Attodry2100")
        self.assertEqual(saved["previous_url"], old_url)
        self.assertEqual(notification_config.shorten_url(self.path), url)
        with self.assertRaises(FileExistsError):
            notification_config.configure("Other-PC", self.path)

    def test_computer_rename_does_not_change_subscription(self):
        first = notification_config.configure("Original-PC", self.path)
        self.assertEqual(notification_config.get_ntfy_url(self.path), first)

    def test_missing_config_waits_for_explicit_setup(self):
        self.assertIsNone(notification_config.get_ntfy_url(self.path))
        self.assertFalse(self.path.exists())

    def test_corrupt_config_is_not_silently_replaced(self):
        self.path.write_text("broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            notification_config.get_ntfy_url(self.path)
        self.assertEqual(self.path.read_text(), "broken")

    def test_runtime_disables_notifications_when_config_is_broken(self):
        self.path.write_text("broken", encoding="utf-8")
        with self.assertWarns(RuntimeWarning):
            self.assertIsNone(notification_config.runtime_url(self.path))
        self.assertEqual(self.path.read_text(), "broken")

    def test_runtime_disables_notifications_when_access_is_denied(self):
        with patch.object(notification_config, "get_ntfy_url", side_effect=PermissionError("denied")):
            with self.assertWarns(RuntimeWarning):
                self.assertIsNone(notification_config.runtime_url(self.path))
