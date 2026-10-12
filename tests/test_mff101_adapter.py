from __future__ import annotations

from collections import namedtuple
from contextlib import contextmanager
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch


from app.devices import mff101_adapter


_DeviceInfo = namedtuple(
    "TDeviceInfo",
    "serial_no model_no fw_ver hw_type hw_ver mod_state nchannels notes",
)


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _Backend:
    def __init__(self):
        self.timeout = 3.0

    def set_timeout(self, timeout):
        self.timeout = timeout

    @contextmanager
    def using_timeout(self, timeout):
        previous = self.timeout
        self.set_timeout(timeout)
        try:
            yield
        finally:
            self.set_timeout(previous)


class _Flipper:
    """Fake only the external MFF hardware boundary; never open FTDI."""

    def __init__(self, states=(0,), *, model="MFF101", serial=37000123):
        self.instr = _Backend()
        self.info = _DeviceInfo(serial, model, "1.0.0", 37, 1, 0, 1, "")
        self.states = list(states)
        self.state = self.states[-1] if self.states else 0
        self.moves = []
        self.closed = False
        self.read_error = None
        self.info_error = None
        self.move_error = None
        self.close_error = None

    def get_device_info(self):
        if not math.isfinite(self.instr.timeout) or self.instr.timeout <= 0:
            raise AssertionError("Device I/O must have a finite timeout")
        if self.info_error:
            raise self.info_error
        return self.info

    def get_state(self):
        if self.closed:
            raise OSError("closed FTDI handle")
        if self.read_error:
            raise self.read_error
        if self.states:
            self.state = self.states.pop(0)
        return self.state

    def move_to_state(self, state):
        if self.closed:
            raise OSError("closed FTDI handle")
        if self.move_error:
            raise self.move_error
        if state not in (0, 1):
            raise AssertionError("MFF accepts only zero-based states")
        self.moves.append(state)

    def close(self):
        if self.close_error:
            raise self.close_error
        self.closed = True


class MFF101AdapterTests(unittest.TestCase):
    def test_failed_close_retains_handle_for_retry(self):
        driver = _Flipper()
        adapter = self._connect(driver)
        driver.close_error = OSError("close failed")
        with self.assertRaisesRegex(OSError, "close failed"):
            adapter.close()
        driver.close_error = None
        adapter.close()
        self.assertTrue(driver.closed)

    def _connect(self, driver, serial_number="37000123"):
        def open_mff(serial):
            self.assertEqual(serial, "37000123")
            return driver

        library = SimpleNamespace(MFF=open_mff)
        with patch.object(mff101_adapter, "_load_thorlabs", return_value=library):
            return mff101_adapter.MFF101Adapter(serial_number)

    def test_scan_returns_only_mff_serials_without_opening_devices(self):
        def unexpected_open(_serial):
            raise AssertionError("Discovery must not open a device")

        library = SimpleNamespace(
            list_kinesis_devices=lambda: [
                ("27000123", "Kinesis K-Cube DC Driver"),
                ("37000456", "APT Filter Flipper"),
                ("37000123", "APT Filter Flipper"),
                ("37000123", "APT Filter Flipper"),
                ("3700123", "Malformed address"),
                ("37abcdef", "Unrelated FTDI device"),
            ],
            MFF=unexpected_open,
        )
        with patch.object(mff101_adapter, "_load_thorlabs", return_value=library):
            self.assertEqual(
                mff101_adapter.scan_mff101_devices(), ["37000123", "37000456"]
            )

    def test_readback_maps_both_endpoints_and_in_motion(self):
        device = self._connect(_Flipper((0, 1, None)))
        self.assertEqual(device.get_position(), 1)
        self.assertEqual(device.get_position(), 2)
        self.assertIsNone(device.get_position())

    def test_unknown_state_is_not_reported_as_a_position(self):
        device = self._connect(_Flipper((2,)))
        with self.assertRaises(RuntimeError):
            device.get_position()

    def test_serial_number_is_normalized(self):
        device = self._connect(_Flipper(), " 37000123 ")
        self.assertEqual(device.serial_number, "37000123")

    def test_invalid_serial_is_rejected_before_hardware_open(self):
        def unexpected_load():
            raise AssertionError("Invalid serial must fail before driver loading")

        with patch.object(mff101_adapter, "_load_thorlabs", side_effect=unexpected_load):
            for serial in ("", "27000123", "3700123", "37abcdef"):
                with self.subTest(serial=serial), self.assertRaises(ValueError):
                    mff101_adapter.MFF101Adapter(serial)

    def test_wrong_model_is_closed_without_any_move(self):
        driver = _Flipper(model="KDC101")
        with self.assertRaisesRegex(RuntimeError, "MFF101"):
            self._connect(driver)
        self.assertTrue(driver.closed)
        self.assertEqual(driver.moves, [])

    def test_wrong_serial_identity_is_closed(self):
        driver = _Flipper(serial=37000456)
        with self.assertRaises(RuntimeError):
            self._connect(driver)
        self.assertTrue(driver.closed)

    def test_legacy_mff002_accepts_zero_firmware_serial_using_selected_usb_identity(self):
        driver = _Flipper(model="MFF002", serial=0)
        driver.info = _DeviceInfo(0, "MFF002", "1.0.3", 16, 2, 0, 1, "APT Filter Flipper")
        adapter = self._connect(driver)
        self.assertEqual(adapter.serial_number, "37000123")
        self.assertEqual(adapter.get_position(), 1)
        self.assertEqual(driver.moves, [])

    def test_legacy_mff002_with_matching_firmware_serial_connects(self):
        driver = _Flipper(model="MFF002")
        driver.info = driver.info._replace(hw_type=16)
        self.assertEqual(self._connect(driver).get_position(), 1)

    def test_legacy_identity_exception_does_not_accept_wrong_device(self):
        identities = (
            _DeviceInfo(37000456, "MFF002", "1.0.3", 16, 2, 0, 1, "APT Filter Flipper"),
            _DeviceInfo(0, "MFF002", "1.0.3", 99, 2, 0, 1, "APT Filter Flipper"),
            _DeviceInfo(0, "MFF002", "1.0.3", 16, 2, 0, 2, "APT Filter Flipper"),
            _DeviceInfo(0, "MFF101", "1.0.3", 16, 2, 0, 1, "APT Filter Flipper"),
            _DeviceInfo(0, "MFF102", "1.0.3", 16, 2, 0, 1, "APT Filter Flipper"),
        )
        for info in identities:
            with self.subTest(info=info):
                driver = _Flipper()
                driver.info = info
                with self.assertRaises(RuntimeError):
                    self._connect(driver)
                self.assertTrue(driver.closed)
                self.assertEqual(driver.moves, [])

    def test_identity_read_failure_closes_connection(self):
        driver = _Flipper()
        driver.info_error = OSError("USB disconnected")
        with self.assertRaisesRegex(OSError, "USB disconnected"):
            self._connect(driver)
        self.assertTrue(driver.closed)

    def test_moves_map_positions_and_wait_for_actual_target(self):
        for target, states, command in ((1, (1, None, 0), 0), (2, (0, None, 1), 1)):
            with self.subTest(target=target):
                driver = _Flipper(states)
                device = self._connect(driver)
                clock = _Clock()
                with patch.object(mff101_adapter.time, "monotonic", clock.monotonic), \
                        patch.object(mff101_adapter.time, "sleep", clock.sleep):
                    self.assertEqual(device.move_to(target), target)
                self.assertEqual(driver.moves, [command])
                self.assertGreater(clock.now, 0)
                self.assertEqual(driver.states, [])

    def test_never_reaching_target_times_out_without_resending(self):
        driver = _Flipper((0, None))
        device = self._connect(driver)
        clock = _Clock()
        with patch.object(mff101_adapter.time, "monotonic", clock.monotonic), \
                patch.object(mff101_adapter.time, "sleep", clock.sleep):
            with self.assertRaises(TimeoutError):
                device.move_to(2, timeout_s=0.15)
        self.assertEqual(driver.moves, [1])
        self.assertAlmostEqual(clock.now, 0.3)

    def test_stopped_at_wrong_endpoint_does_not_count_as_completion(self):
        driver = _Flipper((0,))
        device = self._connect(driver)
        clock = _Clock()
        with patch.object(mff101_adapter.time, "monotonic", clock.monotonic), \
                patch.object(mff101_adapter.time, "sleep", clock.sleep):
            with self.assertRaises(TimeoutError):
                device.move_to(2, timeout_s=0.1)
        self.assertEqual(driver.moves, [1, 1])

    def test_invalid_position_never_sends_a_move(self):
        driver = _Flipper()
        device = self._connect(driver)
        for position in (0, 3, -1, True, 1.5, "2", None):
            with self.subTest(position=position), self.assertRaises(ValueError):
                device.move_to(position)
        self.assertEqual(driver.moves, [])

    def test_invalid_timeout_never_sends_a_move(self):
        driver = _Flipper()
        device = self._connect(driver)
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                device.move_to(2, timeout_s=timeout)
        self.assertEqual(driver.moves, [])

    def test_disconnected_read_is_reported_without_retrying_move(self):
        driver = _Flipper()
        device = self._connect(driver)
        driver.read_error = OSError("USB disconnected")
        with self.assertRaisesRegex(OSError, "USB disconnected"):
            device.move_to(2)
        self.assertEqual(driver.moves, [])

    def test_failed_command_is_not_retried(self):
        driver = _Flipper()
        device = self._connect(driver)
        driver.move_error = OSError("write failed")
        with self.assertRaisesRegex(OSError, "write failed"):
            device.move_to(2)
        self.assertEqual(driver.moves, [])

    def test_close_releases_handle_without_moving(self):
        driver = _Flipper()
        device = self._connect(driver)
        device.close()
        device.close()
        self.assertTrue(driver.closed)
        self.assertEqual(driver.moves, [])
        with self.assertRaises(RuntimeError):
            device.get_position()
        with self.assertRaises(RuntimeError):
            device.move_to(2)


if __name__ == "__main__":
    unittest.main()
