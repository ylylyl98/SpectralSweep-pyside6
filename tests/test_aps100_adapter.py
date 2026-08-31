from __future__ import annotations

import unittest
from unittest.mock import patch

from app.devices.aps100_attodry1000_adapter import (
    APS100AttoDry1000Adapter,
    APS100CommandBlockedError,
    APS100IdentityError,
    APS100SafetyError,
    MockAPS100Adapter,
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
        self.sweep = "pause"
        self.pause_response = "pause"
        self.status = 0
        self.event_status = 0
        self.magnet_voltage_v = 0.0
        self.mode = "Manual"
        self.units = "kG"
        self.block_commands: set[str] = set()
        self.ranges = [40.0, 44.28, 45.0, 89.0, 100.0]
        self.rates = [0.0343, 0.0171, 0.0100, 0.0002, 0.0002, 5.0]

    def write(self, command):
        command = str(command).strip()
        self.commands.append(command)
        self._queue.append(command)
        upper = command.upper()
        response = None
        if upper in self.block_commands:
            self._queue.append("Command blocked")
            return
        if upper == "*IDN?":
            response = self.idn
        elif upper == "*ESR?":
            response = str(self.event_status)
            self.event_status = 0
        elif upper == "*STB?":
            response = str(self.status)
        elif upper == "MODE?":
            response = self.mode
        elif upper == "UNITS?":
            response = self.units
        elif upper == "UNITS G":
            # Firmware 1.67.323 accepts UNITS G but continues reporting kG.
            pass
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
            response = f"{self.magnet_voltage_v}V"
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
            self.sweep = self.pause_response
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


class _InstantSweepAPS100Resource(_FakeAPS100Resource):
    """Fake firmware that completes each commanded SLOW sweep immediately."""

    def write(self, command):
        super().write(command)
        upper = str(command).strip().upper()
        if upper.startswith("SWEEP UP"):
            self.output_kg = self.high_kg
            if self.heater:
                self.field_kg = self.output_kg
            self.sweep = "pause"
            self.status &= ~1
        elif upper.startswith("SWEEP DOWN"):
            self.output_kg = self.low_kg
            if self.heater:
                self.field_kg = self.output_kg
            self.sweep = "pause"
            self.status &= ~1
        elif upper == "SWEEP ZERO":
            self.output_kg = 0.0
            if self.heater:
                self.field_kg = 0.0
            self.sweep = "standby"
            self.status = (self.status & ~1) | 2


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
        self.assertEqual(snapshot.operating_mode, "Manual")

    def test_non_manual_mode_is_rejected_before_mutation(self):
        adapter, fake = self.make_adapter()
        adapter.connect()
        adapter.take_remote()
        fake.mode = "Shim"
        with self.assertRaises(APS100SafetyError):
            adapter.start_sweep_to(0.1)
        self.assertFalse(any(command.startswith("SWEEP UP") for command in fake.commands))

    def test_field_units_kG_is_accepted_like_real_firmware(self):
        adapter, fake = self.make_adapter()
        adapter.connect()
        adapter.take_remote()
        fake.units = "kG"
        units = adapter.select_field_units()
        self.assertEqual(units, "kG")

    def test_ampere_units_are_rejected(self):
        adapter, fake = self.make_adapter()
        adapter.connect()
        adapter.take_remote()
        fake.units = "A"
        with self.assertRaises(Exception):
            adapter.select_field_units()

    def test_command_blocked_has_actionable_error(self):
        adapter, fake = self.make_adapter()
        adapter.connect()
        fake.block_commands.add("REMOTE")
        with self.assertRaisesRegex(APS100CommandBlockedError, "front-panel menu"):
            adapter.take_remote()

    def test_stale_esr_is_cleared_before_remote_and_not_misattributed(self):
        adapter, fake = self.make_adapter()
        adapter.connect()
        fake.event_status = 97

        adapter.take_remote()

        self.assertTrue(adapter._remote)
        remote_index = fake.commands.index("REMOTE")
        self.assertEqual(fake.commands[remote_index - 1], "*ESR?")
        self.assertEqual(fake.commands[remote_index + 1], "*ESR?")

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

    def test_pause_accepts_standby_response_from_firmware_1_67(self):
        adapter, fake = self.make_adapter()
        adapter.connect()
        adapter.take_remote()
        fake.pause_response = "standby"
        fake.status = 0b10

        adapter.pause()

        self.assertEqual(fake.sweep, "standby")
        self.assertFalse(decode_status_byte(fake.status).sweep_active)

    def test_pause_accepts_short_pause_response_from_firmware_1_67(self):
        adapter, fake = self.make_adapter()
        adapter.connect()
        adapter.take_remote()
        fake.pause_response = "pause"
        fake.status = 0b10

        adapter.pause()

        self.assertEqual(fake.sweep, "pause")
        self.assertFalse(decode_status_byte(fake.status).sweep_active)

    def test_heater_off_is_blocked_when_lead_current_is_not_matched(self):
        adapter, fake = self.make_adapter()
        adapter.connect()
        fake.heater = 1
        fake.field_kg = 10.0
        fake.output_kg = 0.0

        with self.assertRaises(APS100SafetyError):
            adapter.enter_persistent_mode(zero_leads=False)
        self.assertNotIn("PSHTR OFF", fake.commands)

    def test_safe_move_from_persistent_field_to_persistent_zero(self):
        adapter = MockAPS100Adapter(time_scale=6000.0)
        adapter.connect()
        adapter._field_t = 8.0
        adapter._output_t = 0.0
        adapter._heater = False

        result = adapter.safe_move_to_field(
            0.0,
            final_mode="persistent",
            zero_leads=True,
            persistent_field_confirmed=True,
            max_magnet_voltage_v=0.1,
            settle_s=0.0,
            timeout_s=10.0,
        )

        self.assertFalse(result.heater_on)
        self.assertAlmostEqual(result.field_t, 0.0, places=3)
        self.assertAlmostEqual(result.output_current_a, 0.0, places=3)

    def test_safe_move_can_finish_driven_without_zeroing_leads(self):
        adapter = MockAPS100Adapter(time_scale=6000.0)
        adapter.connect()
        adapter._field_t = 0.02
        adapter._output_t = 0.0
        adapter._heater = False

        result = adapter.safe_move_to_field(
            0.01,
            final_mode="driven",
            persistent_field_confirmed=True,
            settle_s=0.0,
            timeout_s=5.0,
        )

        self.assertTrue(result.heater_on)
        self.assertAlmostEqual(result.field_t, 0.01, places=3)
        self.assertAlmostEqual(result.output_field_t, 0.01, places=3)

    def test_real_adapter_matches_persistent_leads_before_heating_and_zeroing(self):
        fake = _InstantSweepAPS100Resource()
        adapter, fake = self.make_adapter(
            fake,
            heater_warm_s=0.0,
            heater_cool_s=0.0,
        )
        adapter.connect()
        fake.field_kg = 80.0
        fake.output_kg = 0.0
        fake.heater = 0

        result = adapter.safe_move_to_field(
            0.0,
            final_mode="persistent",
            zero_leads=True,
            persistent_field_confirmed=True,
            max_magnet_voltage_v=0.1,
            settle_s=0.0,
            timeout_s=2.0,
        )

        match_index = fake.commands.index("SWEEP UP SLOW")
        heater_on_index = fake.commands.index("PSHTR ON")
        zero_index = fake.commands.index("SWEEP ZERO")
        heater_off_index = fake.commands.index("PSHTR OFF")
        self.assertLess(match_index, heater_on_index)
        self.assertLess(heater_on_index, zero_index)
        self.assertLess(zero_index, heater_off_index)
        self.assertFalse(any(command.startswith("RATE ") for command in fake.commands))
        self.assertTrue(result.status.standby)
        self.assertFalse(result.heater_on)
        self.assertAlmostEqual(result.field_t, 0.0)
        self.assertAlmostEqual(result.output_current_a, 0.0)

    def test_persistent_matching_requires_explicit_stored_field_confirmation(self):
        adapter = MockAPS100Adapter()
        adapter.connect()
        adapter._field_t = 8.0
        adapter._output_t = 0.0
        adapter._heater = False
        with self.assertRaisesRegex(APS100SafetyError, "Confirm.*stored persistent"):
            adapter.safe_move_to_field(0.0, final_mode="driven", settle_s=0.0)

    def test_persistent_lead_zero_requires_commissioned_vmag_limit(self):
        adapter = MockAPS100Adapter()
        adapter.connect()
        adapter._field_t = adapter._output_t = 1.0
        adapter._heater = False
        with self.assertRaisesRegex(APS100SafetyError, "VMAG safety limit"):
            adapter.safe_move_to_field(
                1.0,
                final_mode="persistent",
                zero_leads=True,
                settle_s=0.0,
            )

    def test_persistent_noop_does_not_cycle_heater(self):
        adapter = MockAPS100Adapter()
        adapter.connect()
        adapter._field_t = 1.0
        adapter._output_t = 0.0
        adapter._heater = False
        result = adapter.safe_move_to_field(
            1.0,
            final_mode="persistent",
            zero_leads=True,
            settle_s=0.0,
        )
        self.assertFalse(result.heater_on)
        self.assertEqual(adapter._zero_commands, 0)

    def test_persistent_lead_zero_stops_on_excess_magnet_voltage(self):
        adapter = MockAPS100Adapter()
        adapter.connect()
        adapter._field_t = adapter._output_t = 1.0
        adapter._heater = False
        adapter._magnet_voltage_v = 0.2
        with self.assertRaisesRegex(APS100SafetyError, "VMAG exceeded"):
            adapter.safe_move_to_field(
                1.0,
                final_mode="persistent",
                zero_leads=True,
                max_magnet_voltage_v=0.1,
                settle_s=0.0,
            )
        self.assertAlmostEqual(adapter._output_t, 1.0)

    def test_safe_move_pauses_an_active_sweep_before_new_target(self):
        adapter = MockAPS100Adapter(time_scale=6000.0)
        adapter.connect()
        adapter._heater = True
        adapter._target_t = 2.0
        result = adapter.safe_move_to_field(
            0.5,
            final_mode="driven",
            settle_s=0.0,
            timeout_s=5.0,
        )
        self.assertTrue(result.heater_on)
        self.assertFalse(result.status.sweep_active)
        self.assertAlmostEqual(result.field_t, 0.5, delta=0.002)


if __name__ == "__main__":
    unittest.main()
