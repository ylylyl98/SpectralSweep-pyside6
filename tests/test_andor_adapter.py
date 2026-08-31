from __future__ import annotations

import threading
import types
import unittest
from unittest import mock

import numpy as np

from app.devices.andor_adapter import (
    AndorConnectionOptions,
    AndorSDK2Setup,
    SpectrometerAndor,
)


class _FakeCamera:
    def __init__(self, module, index):
        self.module = module
        self.index = index
        self.snap_count = 0
        self.closed = False

    def _record(self, name):
        self.module.calls.append((name, threading.get_ident(), self.index))

    def get_device_info(self):
        self._record("get_device_info")
        return ("controller", self.module.models[self.index], self.module.serials[self.index])

    def get_detector_size(self):
        self._record("get_detector_size")
        return (4, 2)

    def get_pixel_size(self):
        self._record("get_pixel_size")
        return (25e-6, 25e-6)

    def set_temperature(self, value, enable_cooler=True):
        self._record("set_temperature")
        self.temperature = float(value)
        self.cooler = bool(enable_cooler)

    def get_temperature(self):
        self._record("get_temperature")
        return getattr(self, "temperature", -20.0)

    def get_temperature_status(self):
        return "stabilized" if getattr(self, "cooler", False) else "off"

    def get_temperature_setpoint(self):
        return getattr(self, "temperature", -20.0)

    def get_temperature_range(self):
        return (-100, 20)

    def is_cooler_on(self):
        return getattr(self, "cooler", False)

    def set_fan_mode(self, value):
        self._record("set_fan_mode")
        self.fan_mode = value
        return value

    def get_fan_mode(self):
        return getattr(self, "fan_mode", "full")

    def setup_shutter(self, value):
        self._record("setup_shutter")

    def set_cooler(self, on):
        self._record("set_cooler")
        self.cooler = bool(on)

    def set_exposure(self, value):
        self._record("set_exposure")
        self.exposure = float(value)

    def get_exposure(self):
        return getattr(self, "exposure", 1.0)

    def set_read_mode(self, value):
        self._record("set_read_mode")
        self.read_mode = value

    def get_read_mode(self):
        return getattr(self, "read_mode", "fvb")

    def get_roi(self):
        return getattr(self, "roi", (0, 4, 0, 2, 1, 1))

    def set_roi(self, hstart=0, hend=4, vstart=0, vend=2, hbin=1, vbin=1):
        self._record("set_roi")
        self.roi = (hstart, hend, vstart, vend, hbin, vbin)

    def snap(self, timeout):
        self._record("snap")
        self.snap_count += 1
        base = np.arange(8, dtype=float).reshape(2, 4)
        return base + (self.snap_count - 1) * 2.0

    def stop_acquisition(self):
        self._record("stop_acquisition")

    def clear_acquisition(self):
        self._record("clear_acquisition")

    def close(self):
        self._record("camera_close")
        self.closed = True


class _FakeSpectrograph:
    def __init__(self, module, index):
        self.module = module
        self.index = index

    def _record(self, name):
        self.module.calls.append((name, threading.get_ident(), self.index))

    def set_grating(self, value):
        self._record("set_grating")
        self.grating = int(value)
        return self.grating

    def get_grating(self):
        self._record("get_grating")
        return getattr(self, "grating", 1)

    def set_slit_width(self, side, width):
        self._record("set_slit_width")
        self.slit = (side, float(width))
        return float(width)

    def is_slit_present(self, side):
        return side == "input_side"

    def get_slit_width(self, side):
        return getattr(self, "slit", (side, 50e-6))[1]

    def set_wavelength(self, value):
        self._record("set_wavelength")
        self.wavelength = float(value)
        return self.wavelength

    def get_wavelength(self):
        self._record("get_wavelength")
        return getattr(self, "wavelength", 860e-9)

    def setup_pixels_from_camera(self, camera):
        self._record("setup_pixels_from_camera")

    def get_calibration(self):
        self._record("get_calibration")
        return np.array([1.0, 2.0, 3.0, 4.0]) * 1e-9

    def get_device_info(self):
        return ("SR-TEST",)

    def get_optical_parameters(self):
        return (0.5, 0.0, 0.0)

    def get_gratings_number(self):
        return 1

    def get_grating_info(self, grating):
        return (150.0, "1200", 0, 0)

    def get_wavelength_limits(self, grating):
        return (0.0, 2.5e-6)

    def is_shutter_present(self):
        return True

    def get_shutter(self):
        return getattr(self, "shutter", "closed")

    def set_shutter(self, value):
        self.shutter = value
        return value

    def is_flipper_present(self, flipper):
        return flipper == "output"

    def get_flipper_port(self, flipper):
        return getattr(self, "output_port", "direct")

    def set_flipper_port(self, flipper, port):
        self._record("set_flipper_port")
        self.output_port = port
        return port

    def close(self):
        self._record("spectrograph_close")


class _FakeAndorModule:
    serials = ("INGAAS-001", "SI-002")
    models = ("InGaAs", "Si CCD")

    def __init__(self):
        self.calls = []
        self.camera_opens = []

    def get_cameras_number_SDK2(self):
        self.calls.append(("camera_count", threading.get_ident(), None))
        return len(self.serials)

    def AndorSDK2Camera(self, idx, **kwargs):
        self.camera_opens.append((idx, dict(kwargs)))
        return _FakeCamera(self, idx)

    def ShamrockSpectrograph(self, idx):
        return _FakeSpectrograph(self, idx)


class AndorAdapterTests(unittest.TestCase):
    def make_setup(self, **changes):
        module = _FakeAndorModule()
        values = {
            "camera_role": "si",
            "camera_index": 0,
            "camera_serial": "SI-002",
            "spectrograph_index": 0,
            "temperature_c": -70.0,
            "cooler_on_connect": True,
            "grating": 1,
            "slit_width_um": 50.0,
            "invert_wavelength_axis": True,
            "operation_timeout_s": 2.0,
        }
        values.update(changes)
        return module, AndorSDK2Setup(
            AndorConnectionOptions(**values), andor_module=module
        )

    def test_serial_selection_configuration_and_identity(self):
        module, setup = self.make_setup()
        try:
            self.assertEqual(setup.identity["camera_index"], 1)
            self.assertEqual(setup.identity["camera_serial"], "SI-002")
            self.assertEqual(setup.identity["camera_role"], "si")
            self.assertEqual(module.camera_opens[0][1]["temperature"], "off")
            self.assertEqual(module.camera_opens[1][1]["temperature"], "off")
            self.assertEqual(module.camera_opens[-1][1]["temperature"], -70.0)
            setup.configure_for_acquisition(
                center_nm=1210.0, exposure_ms=250.0, frames=2
            )
            self.assertEqual(setup.get_temperature(), -70.0)
            owner_ids = {thread_id for _, thread_id, _ in module.calls}
            self.assertEqual(owner_ids, {setup.identity["owner_thread_id"]})
            self.assertNotIn(threading.get_ident(), owner_ids)
        finally:
            setup.close()

    def test_acquire_averages_frames_and_spatial_rows(self):
        _, setup = self.make_setup()
        adapter = SpectrometerAndor(setup)
        try:
            adapter.configure_for_acquisition(
                center_nm=1210.0, exposure_ms=100.0, frames=2
            )
            wavelengths, counts = adapter.acquire()
            np.testing.assert_allclose(wavelengths, [4.0, 3.0, 2.0, 1.0])
            # Frame mean adds 1; spatial mean of rows [0..3] and [4..7] adds 2.
            np.testing.assert_allclose(counts, [3.0, 4.0, 5.0, 6.0])
        finally:
            setup.close()

    def test_full_sensor_acquisition_preserves_two_dimensions(self):
        _, setup = self.make_setup(invert_wavelength_axis=False)
        try:
            setup.configure_for_acquisition(
                center_nm=800.0, exposure_ms=50.0, frames=1
            )
            setup.change_roi_FullSensor()
            frame = setup.acquire_2d()
            self.assertEqual(frame.shape, (2, 4))
            np.testing.assert_allclose(setup.get_wavelength_calibration(), [1, 2, 3, 4])
        finally:
            setup.close()

    def test_missing_configured_serial_fails_closed(self):
        module = _FakeAndorModule()
        with self.assertRaisesRegex(RuntimeError, "was not found"):
            AndorSDK2Setup(
                AndorConnectionOptions(
                    camera_role="si",
                    camera_serial="MISSING",
                    operation_timeout_s=2.0,
                ),
                andor_module=module,
            )

    def test_stored_calibration_is_cached_until_optics_change(self):
        module, setup = self.make_setup()
        try:
            setup.configure_for_acquisition(
                center_nm=1210.0, exposure_ms=100.0, frames=1
            )
            reads_after_configure = sum(
                name == "get_calibration" for name, *_ in module.calls
            )
            setup.get_wavelength_calibration()
            setup.get_wavelength_calibration()
            self.assertEqual(
                sum(name == "get_calibration" for name, *_ in module.calls),
                reads_after_configure,
            )

            setup.apply_controls({"wavelength_nm": 1220.0})
            self.assertEqual(
                sum(name == "get_calibration" for name, *_ in module.calls),
                reads_after_configure + 1,
            )
        finally:
            setup.close()

    def test_control_snapshot_reports_verified_hardware_and_stored_calibration(self):
        _, setup = self.make_setup()
        try:
            snapshot = setup.apply_controls(
                {
                    "wavelength_nm": 1220.0,
                    "grating": 1,
                    "input_slit_width_um": 75.0,
                    "shutter_mode": "opened",
                    "output_port": "side",
                    "temperature_setpoint_c": -65.0,
                    "cooler_on": True,
                    "fan_mode": "low",
                }
            )
            self.assertAlmostEqual(snapshot["wavelength_nm"], 1220.0)
            self.assertEqual(snapshot["grating"], 1)
            self.assertAlmostEqual(snapshot["input_slit_width_um"], 75.0)
            self.assertEqual(snapshot["shutter_mode"], "opened")
            self.assertEqual(snapshot["output_port"], "side")
            self.assertTrue(snapshot["cooler_on"])
            self.assertEqual(snapshot["fan_mode"], "low")
            self.assertEqual(snapshot["calibration_pixel_count"], 4)
            self.assertEqual(
                snapshot["calibration_source"], "Shamrock stored coefficients"
            )
        finally:
            setup.close()

    def test_si_image_roi_and_binning_survive_acquisition_mode_transition(self):
        _, setup = self.make_setup()
        try:
            snapshot = setup.apply_controls(
                {
                    "read_mode": "image",
                    "roi_hstart": 0,
                    "roi_hend": 4,
                    "roi_vstart": 0,
                    "roi_vend": 2,
                    "horizontal_binning": 2,
                    "vertical_binning": 1,
                }
            )
            self.assertEqual(snapshot["roi"], (0, 4, 0, 2, 2, 1))

            setup.configure_for_acquisition(
                center_nm=1210.0, exposure_ms=100.0, frames=1
            )
            setup.change_roi_FullSensor()
            self.assertEqual(
                setup.get_control_snapshot(include_calibration=False)["roi"],
                (0, 4, 0, 2, 2, 1),
            )
        finally:
            setup.close()

    def test_disconnect_safety_snapshot_reports_live_cooling_state(self):
        _, setup = self.make_setup()
        try:
            state = setup.get_disconnect_safety_snapshot()
            self.assertEqual(state["camera_role"], "si")
            self.assertTrue(state["cooler_on"])
            self.assertEqual(state["temperature_status"], "stabilized")
            self.assertAlmostEqual(state["temperature_c"], -70.0)
        finally:
            setup.close()

    def test_shamrock_decode_failure_retries_with_raw_device_count(self):
        module = _FakeAndorModule()
        real_constructor = module.ShamrockSpectrograph
        attempts = 0

        def flaky_constructor(idx):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise UnicodeDecodeError("charmap", b"\x90", 0, 1, "invalid")
            return real_constructor(idx)

        module.ShamrockSpectrograph = flaky_constructor
        module.ShamrockSpectrograph.__module__ = "fake_shamrock_implementation"

        class _TempOpen:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        implementation = types.SimpleNamespace(
            get_spectrographs_number=lambda: 0,
            libctl=types.SimpleNamespace(temp_open=lambda: _TempOpen()),
            lib=types.SimpleNamespace(ShamrockGetNumberDevices=lambda: 7),
        )
        with mock.patch(
            "app.devices.andor_adapter.importlib.import_module",
            return_value=implementation,
        ):
            setup = AndorSDK2Setup(
                AndorConnectionOptions(operation_timeout_s=2.0),
                andor_module=module,
            )
        try:
            self.assertEqual(attempts, 2)
            self.assertIn(
                "opened configured spectrograph index 0 directly",
                setup.identity["connection_warnings"][0],
            )
        finally:
            setup.close()


if __name__ == "__main__":
    unittest.main()
