from __future__ import annotations

import math
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.devices import pm100d_adapter
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
from ui.instrument_panel import _PM100DSection


class _FakePM100DResource:
    def __init__(self, idn="Thorlabs,PM100D,P0011342,2.4.0"):
        self.idn = idn
        self.commands: list[str] = []
        self.timeout = None
        self.closed = False
        self.invalid = False
        self.power = "1.25e-3"
        self.fail_read = False
        self.fail_write = False

    def write(self, command):
        if self.fail_write:
            raise RuntimeError("replacement configuration failed")
        self.commands.append(str(command))

    def query(self, command):
        if self.invalid:
            raise RuntimeError("VI_ERROR_INV_OBJECT: invalid session")
        command = str(command)
        self.commands.append(command)
        if command == "*IDN?":
            return self.idn
        if command == "MEAS:POW?":
            if self.fail_read:
                raise RuntimeError("read timeout")
            return self.power
        raise RuntimeError(f"unexpected query: {command}")

    def close(self):
        self.closed = True


class _FakeResourceManager:
    def __init__(self, resources, instruments=None):
        self.resources = tuple(resources)
        self.instruments = instruments or {}
        self.opened: list[str] = []
        self.closed = False

    @property
    def session(self):
        return None if self.closed else 1

    def list_resources(self):
        return self.resources

    def open_resource(self, resource_name):
        if self.closed:
            raise RuntimeError("VISA resource manager session is closed")
        self.opened.append(resource_name)
        instrument = self.instruments[resource_name]
        if instrument not in getattr(self, "_opened_instruments", []):
            self._opened_instruments = getattr(self, "_opened_instruments", [])
            self._opened_instruments.append(instrument)
        return instrument

    def close(self):
        self.closed = True
        for instrument in getattr(self, "_opened_instruments", []):
            instrument.invalid = True


class _InvalidSessionResourceManager:
    @property
    def session(self):
        raise RuntimeError("VI_ERROR_INV_OBJECT: invalid session")


class _SharedSessionResourceManager(_FakeResourceManager):
    """Models VISA's shared default session invalidation across managers."""

    def __init__(self, resources, instruments, session_state):
        super().__init__(resources, instruments)
        self.session_state = session_state

    def open_resource(self, resource_name):
        if self.session_state["closed"]:
            raise RuntimeError("VISA resource manager session is closed")
        return super().open_resource(resource_name)

    def close(self):
        self.closed = True
        self.session_state["closed"] = True


class _SharedSessionResourceManagerFactory:
    def __init__(self, resources, instruments):
        self.resources = resources
        self.instruments = instruments
        self.session_state = {"closed": False}
        self.managers: list[_SharedSessionResourceManager] = []

    def __call__(self):
        # A newly-created manager gets a fresh session; closing any manager
        # invalidates all managers created before that close.
        self.session_state["closed"] = False
        manager = _SharedSessionResourceManager(
            self.resources, self.instruments, self.session_state
        )
        self.managers.append(manager)
        return manager


class _FakePMController(QObject):
    connected = Signal()
    disconnected = Signal()
    error = Signal(str)
    devices_scanned = Signal(list)

    def scan_devices(self):
        pass

    def connect_instrument(self, _resource):
        pass

    def disconnect_instrument(self):
        pass


class PM100DAdapterTests(unittest.TestCase):
    def setUp(self):
        if hasattr(pm100d_adapter, "_PM100D_RESOURCE_MANAGER"):
            pm100d_adapter._PM100D_RESOURCE_MANAGER = None

    def tearDown(self):
        if hasattr(pm100d_adapter, "_PM100D_RESOURCE_MANAGER"):
            pm100d_adapter._PM100D_RESOURCE_MANAGER = None

    def test_scan_selects_pm100d_usb_resources_and_preserves_identity_metadata(self):
        resource_name = "USB0::0x1313::0x8078::P0011342::INSTR"
        unrelated_name = "USB0::0x9999::0x0001::OTHER::INSTR"
        instrument = _FakePM100DResource()
        unrelated = _FakePM100DResource("Other,Instrument,OTHER,1.0")
        manager = _FakeResourceManager(
            (
                resource_name,
                "USB0::0x1313::0x8079::OTHER::INSTR",
                "ASRL5::INSTR",
                unrelated_name,
            ),
            {resource_name: instrument, unrelated_name: unrelated},
        )
        open_unrelated = manager.open_resource(unrelated_name)

        with patch.object(pm100d_adapter, "pyvisa", create=True) as pyvisa:
            pyvisa.ResourceManager.return_value = manager
            entries = pm100d_adapter.scan_pm100d_resource_entries()

        self.assertEqual(
            entries,
            [
                {
                    "resource": resource_name,
                    "raw_resource": resource_name,
                    "model": "PM100D",
                    "serial": "P0011342",
                    "manufacturer": "Thorlabs",
                    "available": True,
                }
            ],
        )
        self.assertIn(resource_name, manager.opened)
        self.assertTrue(instrument.closed)
        self.assertFalse(manager.closed)
        self.assertEqual(open_unrelated.query("*IDN?"), "Other,Instrument,OTHER,1.0")

    def test_repeated_scan_and_connect_reuse_one_retained_resource_manager(self):
        resource_name = "USB0::0x1313::0x8078::P0011342::INSTR"
        instrument = _FakePM100DResource()
        manager = _FakeResourceManager((resource_name,), {resource_name: instrument})

        with patch.object(pm100d_adapter, "pyvisa", create=True) as pyvisa:
            pyvisa.ResourceManager.return_value = manager
            pm100d_adapter.scan_pm100d_resources()
            pm100d_adapter.scan_pm100d_resources()
            wrapper = pm100d_adapter.ThorlabsPM100D_Wrapper(resource_name)

        self.assertEqual(pyvisa.ResourceManager.call_count, 1)
        self.assertIs(pm100d_adapter._PM100D_RESOURCE_MANAGER, manager)
        wrapper.close()

    def test_invalid_cached_manager_is_replaced_and_cached(self):
        replacement = _FakeResourceManager((), {})
        pm100d_adapter._PM100D_RESOURCE_MANAGER = _InvalidSessionResourceManager()

        with patch.object(pm100d_adapter, "pyvisa", create=True) as pyvisa:
            pyvisa.ResourceManager.return_value = replacement
            manager = pm100d_adapter._get_resource_manager()

        self.assertIs(manager, replacement)
        self.assertIs(pm100d_adapter._PM100D_RESOURCE_MANAGER, replacement)
        pyvisa.ResourceManager.assert_called_once_with()

    def test_wrapper_configures_reads_watts_and_preserves_shared_resource_manager(self):
        resource_name = "USB0::0x1313::0x8078::P0011342::INSTR"
        unrelated_name = "USB0::0x9999::0x0001::OTHER::INSTR"
        instrument = _FakePM100DResource()
        unrelated = _FakePM100DResource("Other,Instrument,OTHER,1.0")
        manager = _FakeResourceManager(
            (resource_name, unrelated_name),
            {resource_name: instrument, unrelated_name: unrelated},
        )

        with patch.object(pm100d_adapter, "pyvisa", create=True) as pyvisa:
            pyvisa.ResourceManager.return_value = manager
            wrapper = pm100d_adapter.ThorlabsPM100D_Wrapper(resource_name)

        self.assertIs(wrapper.inst, instrument)
        self.assertEqual(wrapper.resource_name, resource_name)
        self.assertEqual(instrument.timeout, 5000)
        self.assertEqual(
            instrument.commands,
            [
                "*IDN?",
                "SENS:CORR:WAV 730",
                "SENS:POW:RANG:AUTO ON",
                "SENS:POW:UNIT W",
                "SENS:AVER ON",
                "SENS:AVER:COUN 1000",
            ],
        )
        self.assertAlmostEqual(wrapper.get_power(), 1.25e-3)

        wrapper.close()
        self.assertTrue(instrument.closed)
        self.assertFalse(manager.closed)
        self.assertEqual(
            manager.open_resource(unrelated_name).query("*IDN?"),
            "Other,Instrument,OTHER,1.0",
        )
        self.assertIsNone(wrapper.inst)

    def test_power_read_recovers_from_external_manager_invalidation(self):
        resource_name = "USB0::0x1313::0x8078::P0011342::INSTR"
        first_instrument = _FakePM100DResource()
        replacement_instrument = _FakePM100DResource()
        first_manager = _FakeResourceManager(
            (resource_name,), {resource_name: first_instrument}
        )
        replacement_manager = _FakeResourceManager(
            (resource_name,), {resource_name: replacement_instrument}
        )

        with patch.object(pm100d_adapter, "pyvisa", create=True) as pyvisa:
            pyvisa.ResourceManager.side_effect = [first_manager, replacement_manager]
            wrapper = pm100d_adapter.ThorlabsPM100D_Wrapper(resource_name)
            wrapper.configure_wavelength(660)
            first_manager.close()

            self.assertAlmostEqual(wrapper.get_power(), 1.25e-3)

        self.assertEqual(replacement_manager.opened, [resource_name])
        self.assertEqual(
            replacement_instrument.commands,
            [
                "SENS:CORR:WAV 660",
                "SENS:POW:RANG:AUTO ON",
                "SENS:POW:UNIT W",
                "SENS:AVER ON",
                "SENS:AVER:COUN 1000",
                "MEAS:POW?",
            ],
        )
        wrapper.close()

    def test_failed_recovery_configuration_closes_replacement_instrument(self):
        resource_name = "USB0::0x1313::0x8078::P0011342::INSTR"
        first_instrument = _FakePM100DResource()
        replacement_instrument = _FakePM100DResource()
        replacement_instrument.fail_write = True
        first_manager = _FakeResourceManager(
            (resource_name,), {resource_name: first_instrument}
        )
        replacement_manager = _FakeResourceManager(
            (resource_name,), {resource_name: replacement_instrument}
        )

        with patch.object(pm100d_adapter, "pyvisa", create=True) as pyvisa:
            pyvisa.ResourceManager.side_effect = [first_manager, replacement_manager]
            wrapper = pm100d_adapter.ThorlabsPM100D_Wrapper(resource_name)
            first_manager.close()

            self.assertTrue(math.isnan(wrapper.get_power()))

        self.assertTrue(replacement_instrument.closed)
        self.assertIsNone(wrapper.inst)
        self.assertEqual(wrapper.resource_name, resource_name)
        wrapper.close()

    def test_wrapper_returns_nan_when_power_read_fails(self):
        resource_name = "USB0::0x1313::0x8078::P0011342::INSTR"
        instrument = _FakePM100DResource()
        instrument.fail_read = True
        manager = _FakeResourceManager((resource_name,), {resource_name: instrument})

        with patch.object(pm100d_adapter, "pyvisa", create=True) as pyvisa:
            pyvisa.ResourceManager.return_value = manager
            wrapper = pm100d_adapter.ThorlabsPM100D_Wrapper(resource_name)

        self.assertTrue(math.isnan(wrapper.get_power()))
        self.assertEqual(pyvisa.ResourceManager.call_count, 1)
        wrapper.close()

    def test_wrapper_resolves_placeholder_serial_from_visa_scan(self):
        requested = "USB0::0x1313::0x8078::::INSTR"
        actual = "USB0::0x1313::0x8078::P0011342::INSTR"
        instrument = _FakePM100DResource()
        manager = _FakeResourceManager((actual,), {actual: instrument})

        with patch.object(pm100d_adapter, "pyvisa", create=True) as pyvisa:
            pyvisa.ResourceManager.return_value = manager
            wrapper = pm100d_adapter.ThorlabsPM100D_Wrapper(requested)

        self.assertEqual(manager.opened[-1], actual)
        self.assertEqual(wrapper.resource_name, actual)
        wrapper.close()

    def test_explicit_serial_does_not_fall_back_to_a_different_scanned_serial(self):
        requested = "USB0::0x1313::0x8078::SERIAL_A::INSTR"
        scanned = "USB0::0x1313::0x8078::SERIAL_B::INSTR"
        manager = _FakeResourceManager(
            (scanned,), {scanned: _FakePM100DResource("Thorlabs,PM100D,SERIAL_B,2.4.0")}
        )

        with patch.object(pm100d_adapter, "pyvisa", create=True) as pyvisa:
            pyvisa.ResourceManager.return_value = manager
            with self.assertRaisesRegex(RuntimeError, "Connection failed"):
                pm100d_adapter.ThorlabsPM100D_Wrapper(requested)

        self.assertEqual(manager.opened[-1], requested)
        self.assertEqual(manager.opened.count(scanned), 1)

    def test_placeholder_serial_requires_a_unique_scanned_match(self):
        requested = "USB0::0x1313::0x8078::::INSTR"
        first = "USB0::0x1313::0x8078::SERIAL_A::INSTR"
        second = "USB0::0x1313::0x8078::SERIAL_B::INSTR"
        manager = _FakeResourceManager(
            (first, second),
            {
                first: _FakePM100DResource("Thorlabs,PM100D,SERIAL_A,2.4.0"),
                second: _FakePM100DResource("Thorlabs,PM100D,SERIAL_B,2.4.0"),
            },
        )

        with patch.object(pm100d_adapter, "pyvisa", create=True) as pyvisa:
            pyvisa.ResourceManager.return_value = manager
            with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
                pm100d_adapter.ThorlabsPM100D_Wrapper(requested)

    def test_blank_serial_resource_is_repaired_from_idn_and_raw_name_is_preserved(self):
        blank = "USB0::0x1313::0x8078::::INSTR"
        actual = "USB0::0x1313::0x8078::SERIAL_FROM_IDN::INSTR"
        manager = _FakeResourceManager(
            (blank,),
            {blank: _FakePM100DResource("Thorlabs,PM100D,SERIAL_FROM_IDN,2.4.0")},
        )

        with patch.object(pm100d_adapter, "pyvisa", create=True) as pyvisa:
            pyvisa.ResourceManager.return_value = manager
            entries = pm100d_adapter.scan_pm100d_resource_entries()

        self.assertEqual(entries[0]["resource"], actual)
        self.assertEqual(entries[0]["raw_resource"], blank)
        self.assertEqual(entries[0]["serial"], "SERIAL_FROM_IDN")

    def test_wrapper_reuses_open_manager_after_discovery(self):
        resource_name = "USB0::0x1313::0x8078::P0011342::INSTR"
        instrument = _FakePM100DResource()
        factory = _SharedSessionResourceManagerFactory(
            (resource_name,), {resource_name: instrument}
        )

        with patch.object(pm100d_adapter, "pyvisa", create=True) as pyvisa:
            pyvisa.ResourceManager.side_effect = factory
            wrapper = pm100d_adapter.ThorlabsPM100D_Wrapper(resource_name)

        self.assertEqual(len(factory.managers), 1)
        self.assertFalse(factory.managers[0].closed)
        self.assertIs(wrapper.rm, factory.managers[0])
        self.assertIs(wrapper.inst, instrument)
        wrapper.close()


class PM100DUIErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_pm100d_error_status_preserves_full_driver_message(self):
        section = _PM100DSection(_FakePMController())
        message = "Connection failed: " + ("driver detail; " * 10)

        section._on_pm_error(message)

        self.assertEqual(section._status.text(), f"Error: {message}")


if __name__ == "__main__":
    unittest.main()
