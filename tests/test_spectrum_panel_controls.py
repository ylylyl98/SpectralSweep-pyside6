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
    shamrock_connection_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.identity = {}
        self.apply_calls = []
        self.acquire_1d_calls = 0
        self.acquire_2d_calls = 0
        self.abort_calls = 0
        self.temperature_pause_sources = set()
        self.shamrock_requests = []

    def set_shamrock_connected(self, connected):
        self.shamrock_requests.append(connected)

    def set_temperature_monitor_paused(self, source, paused):
        if paused:
            self.temperature_pause_sources.add(source)
        else:
            self.temperature_pause_sources.discard(source)

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
    def setUp(self):
        from unittest.mock import patch
        dialog = patch('ui.detector_wavelength_advice.confirm_detector_wavelength', return_value=True)
        dialog.start()
        self.addCleanup(dialog.stop)

    def test_wavelength_reminder_cancel_prevents_settings_and_capture(self):
        from unittest.mock import patch
        controller = _FakeSpectrumController()
        panel = SpectrumPanel(controller)
        controller.connected.emit([])
        panel._add_pending = True
        with patch('ui.detector_wavelength_advice.confirm_detector_wavelength', return_value=False) as ask:
            panel._start_continuous('1d')
            assert ask.call_count == 1
        assert not controller.apply_calls
        assert panel._continuous_mode is None
        assert not panel._add_pending
        assert not panel._stop_btn.isEnabled()
        panel.close()

    def test_wavelength_reminder_once_per_continuous_run(self):
        from unittest.mock import patch
        controller = _FakeSpectrumController()
        panel = SpectrumPanel(controller)
        controller.connected.emit([])
        with patch('ui.detector_wavelength_advice.confirm_detector_wavelength', return_value=True) as ask:
            panel._start_continuous('1d')
            controller.settings_applied.emit()
            panel._acquire_next_continuous()
            panel._acquire_next_continuous()
            assert ask.call_count == 1
            assert controller.acquire_1d_calls == 3
        panel.close()

    def test_winspec_average_provenance_stays_with_spectrum(self):
        controller = _FakeSpectrumController()
        controller.identity = {'backend': 'winspec_ingaas'}
        panel = SpectrumPanel(controller)
        controller.connected.emit([])
        processing = {'output': 'mean_counts_per_exposure', 'accumulations': 2}
        panel._on_acquisition_readback({'winspec_frame_context': None,
            'winspec_intensity_processing': processing,
            'winspec_raw_accumulated_counts': [3., 5.]})
        controller.spectrum_ready.emit(np.array([1., 2.]), np.array([1.5, 2.5]))
        assert panel._last_acquisition_snapshot['intensity_processing'] == processing
        assert panel._last_acquisition_snapshot['raw_accumulated_counts'] == [3., 5.]
        assert panel._latest_winspec_frame['counts'] == [1.5, 2.5]
        panel.close()

    def test_winspec_source_label_respects_applied_wavelength_axis(self):
        name=SpectrumPanel._device_name({'backend':'winspec_ingaas','axis_unit':'nm','calibration_status':'physical_grating_model'})
        self.assertIn('(nm)',name)
        self.assertNotIn('uncalibrated',name)

    def test_saved_physical_winspec_model_is_enabled_on_reconnect(self):
        from unittest.mock import patch
        from utils.config import cfg
        model={'kind':'physical_grating_model','context':{'profile':cfg.lf6.optical_profile}}
        with patch.object(cfg.lf6,'winspec_wavelength_calibrations',[model]):
            controller=_FakeSpectrumController()
            controller.identity={'backend':'winspec_ingaas'}
            panel=SpectrumPanel(controller)
            controller.connected.emit([])
            self.assertTrue(panel._use_winspec_nm.isChecked())
            controller.disconnected.emit()
            controller.connected.emit([])
            self.assertTrue(panel._use_winspec_nm.isChecked())
            panel.close()

    def test_energy_input_tracks_wavelength_and_applies_nm(self):
        controller = _FakeSpectrumController()
        controller.identity = {"backend": "lightfield"}
        calls = []
        # Match the real controller: a no-argument call refreshes readbacks.
        # Qt queues this call on connection, separately from Apply requests.
        controller.lightfield_optics = lambda requested=None: calls.append(requested)
        panel = SpectrumPanel(controller)
        controller.connected.emit([])
        self.app.processEvents()
        self.assertEqual(calls, [None])
        panel._center.setValue(1000)
        self.assertAlmostEqual(panel._energy.value(), 1.239842, places=6)
        panel._energy.setValue(2.0)
        self.assertAlmostEqual(panel._center.value(), 619.9, places=1)
        self.assertAlmostEqual(panel._energy.value(), 1239.841984 / 619.9, places=6)
        panel._apply_optics_btn.click()
        self.assertEqual(calls[-1]["wavelength_nm"], 619.9)
        panel._on_optics_status({"wavelength_nm": 1000})
        self.assertIn("1.2398 eV", panel._optics_readback.text())
        panel._on_acquisition_readback({"center_wavelength": {"readback": None}})
        self.assertNotIn("eV", panel._optics_readback.text())
        controller.disconnected.emit()
        self.assertFalse(panel._energy.isEnabled())
        panel.close()

    def test_energy_axis_uses_reciprocal_wavelength_after_zoom(self):
        panel = SpectrumPanel(_FakeSpectrumController())
        plot = panel._spec_plot._plot
        axis = plot.getAxis("top")
        self.assertEqual(axis.labelText, "Energy")
        labels = axis.tickStrings([0, 500, 1000, 2000], 1.0, 500)
        self.assertEqual(labels[0], "")
        np.testing.assert_allclose([float(v) for v in labels[1:]],
                                   [2.47968, 1.23984, 0.619921], rtol=1e-4)
        plot.setXRange(600, 900, padding=0)
        self.app.processEvents()
        np.testing.assert_allclose(axis.range, [600, 900])
        panel.close()

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_shamrock_button_disconnects_only_spectrograph_and_allows_reconnect(self):
        controller = _FakeSpectrumController()
        panel = SpectrumPanel(controller)
        controls = panel._andor_controls
        controls.set_backend_identity({"backend": "andor_sdk2", "camera_role": "si"})
        controls.shamrock_button.click()
        self.assertEqual(controller.shamrock_requests, [False])
        controller.shamrock_connection_changed.emit(False)
        self.assertIn("Reconnect", controls.shamrock_button.text())
        self.assertFalse(controls.apply_button.isEnabled())
        controls.shamrock_button.click()
        self.assertEqual(controller.shamrock_requests, [False, True])
        controller.error.emit("Shamrock connection change failed: USB unavailable")
        self.assertTrue(controls.shamrock_button.isEnabled())
        self.assertFalse(controls.apply_button.isEnabled())
        controller.shamrock_connection_changed.emit(True)
        self.assertTrue(controls.apply_button.isEnabled())
        controls.set_controls_locked(True)
        self.assertFalse(controls.shamrock_button.isEnabled())
        panel.close()

    def test_switch_restores_each_devices_controls(self):
        controller = _FakeSpectrumController()
        controller.backend = "lightfield"
        panel = SpectrumPanel(controller)
        controller.connected.emit([])
        panel._center.setValue(550)
        panel._exposure.setValue(25)
        controller.backend = "andor_si"
        controller.connected.emit([])
        panel._center.setValue(900)
        panel._exposure.setValue(150)
        controller.backend = "lightfield"
        controller.connected.emit([])
        self.assertEqual(panel._center.value(), 550)
        self.assertEqual(panel._exposure.value(), 25)
        controller.backend = "andor_si"
        controller.connected.emit([])
        self.assertEqual(panel._center.value(), 900)
        self.assertEqual(panel._exposure.value(), 150)
        saved = panel.capture_session_state()
        self.assertIn("backend_profiles", saved)
        panel.close()

    def test_english_device_status_full_error_and_frozen_source(self):
        controller = _FakeSpectrumController()
        controller.backend = "andor_si"
        controller.identity = {"backend": "andor_sdk2", "camera_role": "si", "camera_serial": "123"}
        panel = SpectrumPanel(controller)
        controller.connected.emit([])
        self.assertIn("Andor Si", panel._device_status.text())
        self.assertEqual(panel._frames_label.text(), "Frames to average:")
        controller.shamrock_connection_changed.emit(False)
        self.assertIn("disconnected", panel._device_status.text())
        message = "ShamrockGetFlipperMirror failed: " + "details " * 25 + "20201\nCOMMUNICATION ERROR"
        controller.error.emit(message)
        self.assertEqual(panel._error_details.toPlainText(), message)
        panel._copy_error_btn.click()
        self.assertEqual(self.app.clipboard().text(), message)
        panel.push_spectrum(np.array([500, 501]), np.array([2, 3]), {
            "sample_id": "BO146", "exposure_ms": 100, "accumulations": 10,
            "identity": {"backend": "andor_sdk2", "camera_role": "si"},
            "completed_utc": "2026-09-24T10:00:00Z",
        })
        self.assertIn("BO146", panel._source_label.text())
        old = panel._source_label.text()
        controller.backend = "lightfield"
        controller.identity = {"backend": "lightfield"}
        controller.connected.emit([])
        self.assertEqual(panel._source_label.text(), old)
        panel.close()

    def test_top_optics_controls_are_capability_aware(self):
        controller = _FakeSpectrumController()
        controller.backend = "lightfield"
        controller.identity = {"backend": "lightfield"}
        panel = SpectrumPanel(controller)
        controller.connected.emit([])
        self.assertEqual(panel._acquisition_group.title(), "Acquisition")
        self.assertEqual(panel._spectrograph_group.title(), "Spectrograph")
        self.assertFalse(panel._grating.isEnabled())
        self.assertEqual(panel._grating.currentText(), "Not available")
        lf_calls = []
        controller.lightfield_optics = lf_calls.append
        panel._on_optics_status({"backend": "lightfield", "wavelength_nm": 600,
            "grating": "G2", "grating_infos": [{"index": "G2", "label": "300 lines/mm"}],
            "output_flipper_present": True, "output_ports": ["FrontExit", "SideExit"],
            "output_port": "SideExit"})
        panel._apply_optics_btn.click()
        self.assertEqual(lf_calls[-1]["grating"], "G2")
        self.assertEqual(lf_calls[-1]["output_port"], "SideExit")
        controller.identity = {"backend": "andor_sdk2", "camera_role": "si"}
        controller.backend = "andor_si"
        controller.connected.emit([])
        panel._on_optics_status({"wavelength_nm": 650, "grating": 2,
            "grating_infos": [{"index": 2, "info": {}}],
            "output_flipper_present": True, "output_port": "side"})
        self.assertTrue(panel._grating.isEnabled())
        self.assertTrue(panel._output_port.isEnabled())
        self.assertIn("650", panel._optics_readback.text())
        calls = []
        controller.apply_andor_controls = calls.append
        panel._apply_optics_btn.click()
        self.assertEqual(calls[-1]["output_port"], "side")
        self.assertNotIn("cooler_on", calls[-1])
        panel._andor_controls.center.setValue(500)
        panel._center.setValue(800)
        panel._andor_controls.apply()
        self.assertNotIn("wavelength_nm", calls[-1])
        self.assertNotIn("grating", calls[-1])
        self.assertNotIn("output_port", calls[-1])
        panel.close()

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
        self.assertIn("spectrum", controller.temperature_pause_sources)
        self.assertEqual(len(controller.apply_calls), 1)
        controller.settings_applied.emit()
        self.assertEqual(controller.acquire_1d_calls, 1)

        controller.spectrum_ready.emit(np.array([1.0, 2.0]), np.array([3.0, 4.0]))
        self.assertIn("spectrum", controller.temperature_pause_sources)
        panel._stop_btn.click()
        self.app.processEvents()

        self.assertEqual(controller.acquire_1d_calls, 1)
        self.assertEqual(controller.abort_calls, 1)
        self.assertIsNone(panel._continuous_mode)
        self.assertEqual(panel._status_lbl.text(), "Stopped")
        self.assertNotIn("spectrum", controller.temperature_pause_sources)

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


def test_automatic_capture_settings_are_restored_after_completion():
    from types import SimpleNamespace
    from PySide6.QtWidgets import QDoubleSpinBox,QSpinBox,QCheckBox
    app=QApplication.instance() or QApplication([])
    exposure=QDoubleSpinBox();exposure.setMaximum(100000);exposure.setValue(3000)
    accum=QSpinBox();accum.setValue(4);nm=QCheckBox()
    panel=SimpleNamespace(_exposure=exposure,_accumulations=accum,_use_winspec_nm=nm)
    SpectrumPanel._automatic_calibration_started(panel)
    assert accum.value()==1
    exposure.setValue(10000)
    SpectrumPanel._automatic_calibration_finished(panel,False)
    assert exposure.value()==3000 and accum.value()==4
