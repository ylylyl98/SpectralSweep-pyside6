from __future__ import annotations

import unittest
from unittest.mock import patch

from app.devices.aps100_attodry1000_adapter import (
    APS100AttoDry1000Adapter,
    APS100IdentityError,
    APS100SafetyError,
    decode_status_byte,
    field_response_to_tesla,
)


class _FakeAPS100Resource:
    def __init__(self, idn="Attocube,APS100,2301029,1.67,323"):
        self.idn = idn
        self.commands: list[str] = []
        self._queue: list[str] = []
        self.timeout = 1500
        self.baud_rate = 9600
        self.write_termination = "\r"
        self.read_termination = "\n"
        self.closed = False
        self.field_kg = 0.0
        self.output_kg = 0.0
        self.low_kg = 0.0
        self.high_kg = 0.0
        self.heater = 1
        self.sweep = "sweep paused"
        self.status = 0
        self.ranges = [40.0, 44.28, 45.0, 89.0, 100.0]
        self.rates = [0.0343, 0.0171, 0.0100, 0.0002, 0.0002, 5.0]

    def write(self, command):
        command = str(command).strip()
        self.commands.append(command)
        self._queue.append(command)
        upper = command.upper()
        response = None
        if upper == "*IDN?":
            response = self.idn
        elif upper == "*ESR?":
            response = "0"
        elif upper == "*STB?":
            response = str(self.status)
        elif upper == "UNITS?":
            response = "kG"
        elif upper == "IMAG?":
            response = f"{self.field_kg:.6f}kG"
        elif upper == "IOUT?":
            response = f"{self.output_kg:.6f}kG"
        elif upper == "LLIM?":
            response = f"{self.low_kg:.6f}kG"
        elif upper == "ULIM?":
            response = f"{self.high_kg:.6f}kG"
        elif upper == "PSHTR?":
            response = str(self.heater)
        elif upper == "SWEEP?":
            response = self.sweep
        elif upper == "VLIM?":
            response = "3.0V"
        elif upper == "VMAG?":
            response = "0.0V"
        elif upper == "VOUT?":
            response = "0.0V"
        elif upper.startswith("RANGE?"):
            response = str(self.ranges[int(command.split()[1])])
        elif upper.startswith("RATE?"):
            response = str(self.rates[int(command.split()[1])])
        elif upper.startswith("LLIM "):
            self.low_kg = float(command.split()[1])
        elif upper.startswith("ULIM "):
            self.high_kg = float(command.split()[1])
        elif upper.startswith("RATE "):
            _, index, value = command.split()
            self.rates[int(index)] = float(value)
        elif upper == "PSHTR ON":
            self.heater = 1
        elif upper == "PSHTR OFF":
            self.heater = 0
        elif upper == "SWEEP PAUSE":
            self.sweep = "sweep paused"
            self.status &= ~1
        elif upper.startswith("SWEEP UP"):
            self.sweep = "sweep up"
            self.status |= 1
        elif upper.startswith("SWEEP DOWN"):
            self.sweep = "sweep down"
            self.status |= 1
        if response is not None:
            self._queue.append(response)

    def read(self):
        if not self._queue:
            raise TimeoutError("fake APS100 read timeout")
        return self._queue.pop(0)

    def close(self):
        self.closed = True


class APS100AdapterTests(unittest.TestCase):
    def make_adapter(self, resource=None, **kwargs):
        fake = resource or _FakeAPS100Resource()
        adapter = APS100AttoDry1000Adapter(
            visa_resource=fake,
            resource_name="ASRL5::INSTR",
            **kwargs,
        )
        return adapter, fake

    def test_field_response_unit_conversion(self):
        self.assertAlmostEqual(field_response_to_tesla("20.000kG", 0.20328), 2.0)
        self.assertAlmostEqual(field_response_to_tesla("500G", 0.20328), 0.05)
        self.assertAlmostEqual(field_response_to_tesla("-1.5T", 0.20328), -1.5)
        self.assertAlmostEqual(field_response_to_tesla("2A", 0.20328), 0.40656)

    def test_status_byte_matches_aps100_manual(self):
        status = decode_status_byte(0b10001101)
        self.assertTrue(status.sweep_active)
        self.assertTrue(status.quench)
        self.assertTrue(status.power_module_failure)
        self.assertTrue(status.menu_locked)
        self.assertFalse(status.standby)

    def test_connect_is_read_only_and_validates_identity(self):
        adapter, fake = self.make_adapter()
        identity = adapter.connect()
        self.assertEqual(identity.serial, "2301029")
        self.assertEqual(fake.commands, ["*IDN?"])

        bad, _ = self.make_adapter(_FakeAPS100Resource("Other,Supply,1,1.0,1"))
        with self.assertRaises(APS100IdentityError):
            bad.connect()

    def test_snapshot_parses_real_style_kg_responses(self):
        adapter, fake = self.make_adapter()
        adapter.connect()
        fake.field_kg = -12.5
        fake.output_kg = -12.4
        fake.low_kg = -20
        fake.high_kg = 20
        snapshot = adapter.read_snapshot()
        self.assertAlmostEqual(snapshot.field_t, -1.25)
        self.assertAlmostEqual(snapshot.output_field_t, -1.24)
        self.assertAlmostEqual(snapshot.lower_limit_t, -2.0)
        self.assertAlmostEqual(snapshot.upper_limit_t, 2.0)
        self.assertTrue(snapshot.heater_on)

    def test_limits_are_written_in_kg_and_verified(self):
        adapter, fake = self.make_adapter()
        adapter.connect()
        adapter.take_remote()
        low, high = adapter.set_limits_t(-2.0, 2.0)
        self.assertAlmostEqual(low, -2.0)
        self.assertAlmostEqual(high, 2.0)
        self.assertIn("UNITS G", fake.commands)
        self.assertIn("LLIM -20.000000", fake.commands)
        self.assertIn("ULIM 20.000000", fake.commands)

    def test_rate_is_converted_to_amps_per_second(self):
        adapter, fake = self.make_adapter()
        adapter.connect()
        adapter.take_remote()
        changed = adapter.set_rate_t_per_min(0.1, max_abs_field_t=2.0)
        expected = 0.1 / (0.20328 * 60.0)
        self.assertAlmostEqual(fake.rates[0], expected, places=6)
        self.assertEqual(set(changed), {0})

    def test_heater_transitions_hold_for_configured_times(self):
        clock = [0.0]

        def advance(seconds):
            clock[0] += float(seconds)

        adapter, fake = self.make_adapter(
            sleep_fn=advance,
            heater_warm_s=60.0,
            heater_cool_s=120.0,
        )
        adapter.connect()
        fake.field_kg = fake.output_kg = 10.0

        with patch(
            "app.devices.aps100_attodry1000_adapter.time.monotonic",
            side_effect=lambda: clock[0],
        ):
            fake.heater = 0
            adapter.enter_driven_mode()
            self.assertAlmostEqual(clock[0], 60.0)
            self.assertIn("PSHTR ON", fake.commands)

            adapter.enter_persistent_mode(zero_leads=False)
            self.assertAlmostEqual(clock[0], 180.0)
            self.assertLess(fake.commands.index("PSHTR ON"), fake.commands.index("PSHTR OFF"))

    def test_heater_off_is_blocked_when_lead_current_is_not_matched(self):
        adapter, fake = self.make_adapter()
        adapter.connect()
        fake.heater = 1
        fake.field_kg = 10.0
        fake.output_kg = 0.0

        with self.assertRaises(APS100SafetyError):
            adapter.enter_persistent_mode(zero_leads=False)
        self.assertNotIn("PSHTR OFF", fake.commands)


if __name__ == "__main__":
    unittest.main()
