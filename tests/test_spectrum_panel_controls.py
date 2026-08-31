from __future__ import annotations

import os
import unittest

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from ui.spectrum_panel import SpectrumPanel


class _FakeSpectrumController(QObject):
    connected = Signal(list)
    disconnected = Signal()
    spectrum_ready = Signal(object, object)
    frame_ready = Signal(object)
    settings_applied = Signal()
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self.identity = {}
        self.apply_calls = []
        self.acquire_1d_calls = 0
        self.acquire_2d_calls = 0
        self.abort_calls = 0

    def apply_settings(self, exposure_ms, center_nm, accumulations):
        self.apply_calls.append((exposure_ms, center_nm, accumulations))

    def acquire_single(self):
        self.acquire_1d_calls += 1

    def acquire_2d(self):
        self.acquire_2d_calls += 1

    def abort_acquisition(self):
        self.abort_calls += 1
        return True


class SpectrumPanelControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_acquire_applies_displayed_settings_before_capture(self):
        controller = _FakeSpectrumController()
        panel = SpectrumPanel(controller)
        controller.connected.emit([])
        panel._center.setValue(1220.0)
        panel._exposure.setValue(100.0)
        panel._accumulations.setValue(2)

        panel._acquire_btn.click()
        self.assertEqual(controller.apply_calls, [(100.0, 1220.0, 2)])
        self.assertEqual(controller.acquire_1d_calls, 0)

        controller.settings_applied.emit()
        self.assertEqual(controller.acquire_1d_calls, 1)

    def test_apply_only_does_not_start_capture(self):
        controller = _FakeSpectrumController()
        panel = SpectrumPanel(controller)
        controller.connected.emit([])

        panel._apply_btn.click()
        controller.settings_applied.emit()

        self.assertEqual(len(controller.apply_calls), 1)
        self.assertEqual(controller.acquire_1d_calls, 0)
        self.assertEqual(controller.acquire_2d_calls, 0)
        self.assertEqual(panel._status_lbl.text(), "Settings applied")

    def test_ingaas_profile_disables_two_dimensional_capture(self):
        controller = _FakeSpectrumController()
        controller.identity = {
            "backend": "andor_sdk2",
            "camera_role": "ingaas",
        }
        panel = SpectrumPanel(controller)
        controller.connected.emit([])

        self.assertTrue(panel._acquire_btn.isEnabled())
        self.assertFalse(panel._acquire_2d_btn.isEnabled())
        self.assertFalse(panel._run_2d_btn.isEnabled())
        self.assertIn("one-dimensional", panel._acquire_2d_btn.toolTip())

    def test_continuous_1d_is_sequential_and_stop_prevents_next_frame(self):
        controller = _FakeSpectrumController()
        panel = SpectrumPanel(controller)
        controller.connected.emit([])

        panel._run_1d_btn.click()
        self.assertEqual(len(controller.apply_calls), 1)
        controller.settings_applied.emit()
        self.assertEqual(controller.acquire_1d_calls, 1)

        controller.spectrum_ready.emit(np.array([1.0, 2.0]), np.array([3.0, 4.0]))
        panel._stop_btn.click()
        self.app.processEvents()

        self.assertEqual(controller.acquire_1d_calls, 1)
        self.assertEqual(controller.abort_calls, 1)
        self.assertIsNone(panel._continuous_mode)
        self.assertEqual(panel._status_lbl.text(), "Stopped")

    def test_continuous_2d_requests_next_frame_only_after_result(self):
        controller = _FakeSpectrumController()
        controller.identity = {"backend": "andor_sdk2", "camera_role": "si"}
        panel = SpectrumPanel(controller)
        controller.connected.emit([])

        panel._run_2d_btn.click()
        controller.settings_applied.emit()
        self.assertEqual(controller.acquire_2d_calls, 1)
        controller.frame_ready.emit(np.zeros((2, 4)))
        self.app.processEvents()
        self.assertEqual(controller.acquire_2d_calls, 2)
        panel._stop_btn.click()

    def test_andor_drawer_shows_grating_output_and_stored_calibration(self):
        controller = _FakeSpectrumController()
        controller.identity = {"backend": "andor_sdk2", "camera_role": "si"}
        panel = SpectrumPanel(controller)
        controller.connected.emit([])
        panel._andor_toggle.click()
        panel._andor_controls._on_status(
            {
                "backend": "andor_sdk2",
                "camera_role": "si",
                "camera_serial": "SI-2",
                "spectrograph_serial": "SR-2219",
                "detector_size": (1024, 256),
                "grating": 1,
                "grating_infos": [
                    {
                        "index": 1,
                        "info": {
                            "lines": 150,
                            "blaze_wavelength": 1200,
                            "home": 12,
                            "offset": 3,
                        },
                    }
                ],
                "wavelength_limits_nm": (500.0, 2500.0),
                "output_flipper_present": True,
                "output_port": "side",
                "calibration_pixel_count": 1024,
                "calibration_range_nm": (900.0, 1500.0),
                "calibration_source": "Shamrock stored coefficients",
            }
        )

        self.assertTrue(panel._andor_toggle.isVisibleTo(panel))
        self.assertIn("150 lines/mm", panel._andor_controls.grating.currentText())
        self.assertEqual(panel._andor_controls.output_port.currentText(), "side")
        self.assertIn("1024 pixels", panel._andor_controls.calibration.text())


if __name__ == "__main__":
    unittest.main()
