from __future__ import annotations

import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from controllers.lf6_controller import LF6Controller


class SpectrometerControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for(self, predicate, timeout_s=2.0):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("timed out waiting for controller signal")

    def test_mock_andor_backend_uses_common_controller_surface(self):
        controller = LF6Controller()
        connected = []
        applied = []
        spectra = []
        disconnected = []
        controller.connected.connect(lambda experiments: connected.append(experiments))
        controller.settings_applied.connect(lambda: applied.append(True))
        controller.spectrum_ready.connect(lambda wl, counts: spectra.append((wl, counts)))
        controller.disconnected.connect(lambda: disconnected.append(True))
        try:
            controller.connect_instrument(use_mock=True, backend="andor_si")
            self.wait_for(lambda: bool(connected))
            self.assertTrue(controller.is_connected)
            self.assertEqual(controller.backend, "andor_si")
            self.assertEqual(controller.identity["backend"], "mock_andor_si")

            controller.apply_settings(50.0, 730.0, 2)
            self.wait_for(lambda: bool(applied))
            controller.acquire_single()
            self.wait_for(lambda: bool(spectra))
            self.assertEqual(len(spectra[0][0]), len(spectra[0][1]))

            controller.disconnect_instrument()
            self.wait_for(lambda: bool(disconnected))
            self.assertFalse(controller.is_connected)
        finally:
            controller.shutdown()


if __name__ == "__main__":
    unittest.main()
