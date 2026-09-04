import time
import unittest
from unittest.mock import patch
from app.experiment_lifecycle import ExperimentTerminalEvent, publish
from app.ntfy_notifications import NtfyNotifier

class NtfyNotificationTests(unittest.TestCase):
    def test_titles_cancel_and_duplicate(self):
        calls = []
        class Resp:
            def read(self): return b""
        def urlopen(req, timeout): calls.append((req.headers["Title"], req.data)); return Resp()
        with patch("app.ntfy_notifications.urllib.request.urlopen", side_effect=urlopen):
            n = NtfyNotifier()
            publish(ExperimentTerminalEvent("a", "motion_sweep", "completed"))
            publish(ExperimentTerminalEvent("a", "motion_sweep", "completed"))
            publish(ExperimentTerminalEvent("b", "motion_sweep", "failed"))
            publish(ExperimentTerminalEvent("c", "motion_sweep", "cancelled"))
            deadline = time.time() + 2
            while len(calls) < 2 and time.time() < deadline: time.sleep(.01)
            n.shutdown()
        self.assertEqual([x[0] for x in calls], ["Spectra Sweep Complete", "Spectra Sweep ERROR"])

    def test_network_errors_are_isolated(self):
        with patch("app.ntfy_notifications.urllib.request.urlopen", side_effect=OSError("offline")) as urlopen:
            n = NtfyNotifier(); publish(ExperimentTerminalEvent("x", "motion_sweep", "completed"))
            deadline = time.time() + 2
            while not urlopen.called and time.time() < deadline: time.sleep(.01)
            n.shutdown()
        self.assertTrue(urlopen.called)

if __name__ == "__main__": unittest.main()
