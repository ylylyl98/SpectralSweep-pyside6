"""Exercise the production serial adapter against a moving, mode-aware supply."""

import os
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.devices.aps100_attodry1000_adapter import (
    APS100AttoDry1000Adapter, APS100Error, APS100SafetyError, APS100TimeoutError,
)
from controllers.magnet_controller import _MagnetWorker
from tests.test_aps100_adapter import _FakeAPS100Resource
from tests.test_mcd_workflow import _EmptyController, _PanelMagnetController
from ui.mcd_panel import MCDPanel


class _MovingSupply(_FakeAPS100Resource):
    def __init__(self, field=1.0, output=1.0, heater=False):
        super().__init__()
        self.field_kg = field * 10
        self.output_kg = output * 10
        self.heater = int(heater)
        self.motion_target = None
        self.zeroing = False
        self.fast = False
        self.stalled = False
        self.voltage_after_fast = 0.0
        self.heater_after_fast = False
        self.fault_after_fast = False
        self.events = []

    def write(self, command):
        upper = str(command).strip().upper()
        if (upper == "IOUT?" or (upper == "IMAG?" and self.heater)) and self.motion_target is not None and not self.stalled:
            distance = self.motion_target - self.output_kg
            step = min(abs(distance), 100.0 if self.fast else 0.05)
            self.output_kg += step if distance > 0 else -step
            if self.heater:
                self.field_kg = self.output_kg
            if abs(self.output_kg - self.motion_target) < 1e-8:
                self.motion_target = None
                self.sweep = "standby" if self.zeroing else "pause"
                self.status = 2 if self.zeroing else 0
        self.events.append((upper, self.heater, self.fast, self.output_kg, self.magnet_voltage_v))
        super().write(command)
        if upper in self.block_commands:
            return
        if upper.startswith("SWEEP "):
            if upper.endswith(" SLOW"):
                self.fast = False
            elif upper.endswith(" FAST"):
                if self.heater:
                    raise AssertionError("FAST command issued with heater ON")
                self.fast = True
                self.magnet_voltage_v = self.voltage_after_fast
                if self.heater_after_fast:
                    self.heater = 1
                if self.fault_after_fast:
                    self.status |= 4
            if upper.startswith("SWEEP PAUSE"):
                self.motion_target = None
            else:
                self.zeroing = upper.startswith("SWEEP ZERO")
                self.motion_target = (0.0 if self.zeroing else
                                      self.high_kg if upper.startswith("SWEEP UP") else self.low_kg)
                self.status = (self.status & ~2) | 1
                self.sweep = "sweep zero" if self.zeroing else self.sweep
        if upper == "PSHTR ON" and self.fast:
            raise AssertionError("FAST was not cleared before enabling heater")


class APS100FastLeadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_adapter(self, **supply_kwargs):
        supply = _MovingSupply(**supply_kwargs)
        adapter = APS100AttoDry1000Adapter(
            visa_resource=supply, heater_warm_s=0, heater_cool_s=0,
            sleep_fn=lambda _: None,
        )
        adapter.connect()
        return adapter, supply

    def zero(self, adapter, **kwargs):
        return adapter.zero_output(
            verify_persistent_switch=True, max_magnet_voltage_v=0.1, timeout_s=2,
            **kwargs,
        )

    def test_zero_probes_slow_then_fast_for_both_polarities_and_keeps_field(self):
        for sign in (-1, 1):
            adapter, supply = self.make_adapter(field=sign * 9, output=sign * 9)
            self.zero(adapter)
            slow = supply.commands.index("SWEEP ZERO SLOW")
            fast = supply.commands.index("SWEEP ZERO FAST")
            self.assertGreaterEqual(supply.commands[slow:fast].count("VMAG?"), 4)
            self.assertIn("RATE? 5", supply.commands[:fast])
            self.assertEqual(supply.field_kg, sign * 90)
            self.assertEqual(supply.output_kg, 0)
            self.assertTrue(adapter.get_status().standby)
            self.assertNotIn("PSHTR ON", supply.commands)
            self.assertFalse(any(c.startswith("RATE ") for c in supply.commands))
            report = adapter.lead_ramp_history[-1]
            self.assertTrue(report["fast_used"])
            self.assertEqual(report["fast_rate_a_per_s"], 5)
            self.assertEqual(report["outcome"], "completed")

    def test_matching_uses_fast_then_clears_fast_before_heater_on(self):
        for sign in (-1, 1):
            adapter, supply = self.make_adapter(field=sign * 8, output=0)
            adapter.enter_driven_mode(max_magnet_voltage_v=0.1)
            direction = "DOWN" if sign < 0 else "UP"
            slow = supply.commands.index(f"SWEEP {direction} SLOW")
            fast = supply.commands.index(f"SWEEP {direction} FAST")
            heater = supply.commands.index("PSHTR ON")
            self.assertLess(slow, fast)
            self.assertIn("SWEEP PAUSE SLOW", supply.commands[fast:heater])
            self.assertEqual(supply.field_kg, sign * 80)
            self.assertEqual(supply.output_kg, sign * 80)
            self.assertTrue(adapter.get_heater_status())

    def test_return_from_persistent_nine_uses_fast_only_for_leads(self):
        adapter, supply = self.make_adapter(field=9, output=0)
        result = adapter.safe_move_to_field(
            8, final_mode="driven", persistent_field_confirmed=True,
            max_magnet_voltage_v=0.1, settle_s=0, timeout_s=2,
        )
        self.assertEqual(result.field_t, 8)
        self.assertIn("SWEEP UP FAST", supply.commands)
        heater = supply.commands.index("PSHTR ON")
        self.assertIn("SWEEP DOWN SLOW", supply.commands[heater:])
        self.assertFalse(any(c.endswith(" FAST") for c in supply.commands[heater:]))

    def test_matching_already_satisfied_still_clears_latched_fast(self):
        adapter, supply = self.make_adapter()
        supply.fast = True
        adapter.enter_driven_mode()
        self.assertLess(supply.commands.index("SWEEP PAUSE SLOW"), supply.commands.index("PSHTR ON"))

    def test_driven_zero_explicitly_selects_slow(self):
        adapter, supply = self.make_adapter(heater=True)
        supply.fast = True
        adapter.zero_output(timeout_s=2)
        self.assertIn("SWEEP ZERO SLOW", supply.commands)
        self.assertNotIn("SWEEP ZERO FAST", supply.commands)
        self.assertEqual(supply.field_kg, 0)

    def test_cooling_and_warming_dwell_remain_in_effect(self):
        adapter, supply = self.make_adapter(heater=True)
        clock = [0.0]
        adapter.heater_cool_s = 120
        adapter.heater_warm_s = 60
        adapter._sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        times = []
        original = supply.write
        def timed_write(command):
            times.append((command, clock[0]))
            original(command)
        supply.write = timed_write
        with patch("app.devices.aps100_attodry1000_adapter.time.monotonic", side_effect=lambda: clock[0]):
            adapter.enter_persistent_mode(max_magnet_voltage_v=0.1)
            off = next(t for c, t in times if c == "PSHTR OFF")
            zero = next(t for c, t in times if c == "SWEEP ZERO SLOW")
            self.assertGreaterEqual(zero - off, 120)
            adapter.enter_driven_mode(max_magnet_voltage_v=0.1)
            on = next(t for c, t in times if c == "PSHTR ON")
            self.assertGreaterEqual(clock[0] - on, 60)

    def test_unknown_cooling_on_connect_waits_before_lead_motion(self):
        adapter, supply = self.make_adapter()
        clock = [0.0]
        adapter.heater_cool_s = 120
        adapter._sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        observed = []
        original = supply.write
        def capture(command):
            if command == "SWEEP ZERO SLOW":
                observed.append(clock[0])
            original(command)
        supply.write = capture
        with patch("app.devices.aps100_attodry1000_adapter.time.monotonic", side_effect=lambda: clock[0]):
            self.zero(adapter)
        self.assertGreaterEqual(observed[0], 120)

    def test_nonzero_probe_voltage_does_not_qualify_for_fast(self):
        adapter, supply = self.make_adapter()
        supply.magnet_voltage_v = 0.01  # Below abort limit, but not isolation evidence.
        self.zero(adapter)
        self.assertNotIn("SWEEP ZERO FAST", supply.commands)
        self.assertFalse(adapter.lead_ramp_history[-1]["fast_used"])

    def test_zero_voltage_without_output_movement_does_not_qualify(self):
        adapter, supply = self.make_adapter()
        supply.stalled = True
        with self.assertRaises(APS100TimeoutError):
            adapter.zero_output(verify_persistent_switch=True, max_magnet_voltage_v=0.1, timeout_s=0.01)
        self.assertNotIn("SWEEP ZERO FAST", supply.commands)
        self.assertIn("SWEEP PAUSE SLOW", supply.commands)

    def test_invalid_or_unavailable_fast_rate_falls_back_to_slow(self):
        for value in (0, -1, "nan", "bad"):
            adapter, supply = self.make_adapter()
            supply.rates[5] = value
            self.zero(adapter)
            self.assertNotIn("SWEEP ZERO FAST", supply.commands)
            self.assertIn("unavailable", adapter.lead_ramp_history[-1]["reason"])
            self.assertEqual(supply.output_kg, 0)

    def test_fast_can_be_disabled_without_changing_rate_table(self):
        adapter, supply = self.make_adapter()
        adapter.fast_leads_enabled = False
        self.zero(adapter)
        self.assertNotIn("SWEEP ZERO FAST", supply.commands)
        self.assertNotIn("RATE? 5", supply.commands)
        self.assertIn("disabled", adapter.lead_ramp_history[-1]["reason"])

    def test_heater_on_blocks_persistent_lead_operation_before_motion(self):
        adapter, supply = self.make_adapter(heater=True)
        with self.assertRaisesRegex(APS100SafetyError, "heater OFF"):
            self.zero(adapter)
        self.assertFalse(any(c.startswith("SWEEP ") for c in supply.commands))

    def test_faults_after_fast_pause_and_do_not_enable_heater(self):
        for fault in ("voltage_after_fast", "heater_after_fast", "fault_after_fast"):
            for operation in ("zero", "match"):
                with self.subTest(fault=fault, operation=operation):
                    adapter, supply = self.make_adapter(output=0 if operation == "match" else 1)
                    setattr(supply, fault, 0.001 if fault == "voltage_after_fast" else True)
                    with self.assertRaises(APS100SafetyError):
                        if operation == "zero":
                            self.zero(adapter)
                        else:
                            adapter.enter_driven_mode(max_magnet_voltage_v=0.1)
                    self.assertIn("SWEEP PAUSE SLOW", supply.commands)
                    self.assertNotIn("PSHTR ON", supply.commands)
                    self.assertEqual(adapter.lead_ramp_history[-1]["outcome"], "failed")

    def test_rejected_fast_command_pauses_and_reports_failure(self):
        adapter, supply = self.make_adapter()
        supply.block_commands.add("SWEEP ZERO FAST")
        with self.assertRaises(APS100Error):
            self.zero(adapter)
        self.assertIn("SWEEP PAUSE SLOW", supply.commands)
        self.assertEqual(adapter.lead_ramp_history[-1]["outcome"], "failed")

    def test_stop_during_fast_matching_prevents_heater_on(self):
        adapter, supply = self.make_adapter(output=0)
        stop = threading.Event()
        with self.assertRaisesRegex(APS100SafetyError, "stopped"):
            adapter.enter_driven_mode(
                max_magnet_voltage_v=0.1, stop_event=stop,
                progress=lambda label, value: stop.set() if "(FAST)" in label else None,
            )
        self.assertNotIn("PSHTR ON", supply.commands)
        self.assertIn("SWEEP PAUSE SLOW", supply.commands)

    def test_telemetry_failure_while_moving_pauses(self):
        adapter, supply = self.make_adapter()
        original = adapter.get_magnet_voltage_v
        def read():
            if "SWEEP ZERO FAST" in supply.commands:
                raise APS100Error("VMAG read failed")
            return original()
        with patch.object(adapter, "get_magnet_voltage_v", side_effect=read):
            with self.assertRaisesRegex(APS100Error, "VMAG read failed"):
                self.zero(adapter)
        self.assertIn("SWEEP PAUSE SLOW", supply.commands)

    def test_nonfinite_telemetry_blocks_fast_and_heater_on(self):
        for method in ("get_magnet_voltage_v", "get_output_field_t"):
            adapter, supply = self.make_adapter(output=0)
            with patch.object(adapter, method, return_value=float("nan")):
                with self.assertRaises(APS100SafetyError):
                    adapter.enter_driven_mode(max_magnet_voltage_v=0.1)
            self.assertNotIn("PSHTR ON", supply.commands)
            self.assertFalse(any(c.endswith(" FAST") for c in supply.commands))

    def test_wrong_direction_current_is_rejected_before_fast(self):
        adapter, supply = self.make_adapter()
        original = adapter.get_output_field_t
        def read():
            return 2.0 if "SWEEP ZERO SLOW" in supply.commands else original()
        with patch.object(adapter, "get_output_field_t", side_effect=read):
            with self.assertRaisesRegex(APS100SafetyError, "requested interval"):
                self.zero(adapter)
        self.assertNotIn("SWEEP ZERO FAST", supply.commands)
        self.assertIn("SWEEP PAUSE SLOW", supply.commands)

    def test_stop_arriving_during_last_pre_fast_read_prevents_fast(self):
        adapter, supply = self.make_adapter()
        stop = threading.Event()
        original = adapter.get_magnet_voltage_v
        reads = [0]
        def read():
            if "SWEEP ZERO SLOW" in supply.commands:
                reads[0] += 1
                if reads[0] == 4:  # Three probe samples, then final pre-FAST check.
                    stop.set()
            return original()
        with patch.object(adapter, "get_magnet_voltage_v", side_effect=read):
            with self.assertRaisesRegex(APS100SafetyError, "stopped"):
                self.zero(adapter, stop_event=stop)
        self.assertNotIn("SWEEP ZERO FAST", supply.commands)
        self.assertIn("SWEEP PAUSE SLOW", supply.commands)

    def test_ui_retains_normal_rates_when_fast_read_fails(self):
        adapter, supply = self.make_adapter()
        supply.rates[5] = 0
        worker = _MagnetWorker()
        worker.adapter = adapter
        rates = []
        worker.rates_updated.connect(rates.append)
        worker.refresh_rates()
        self.assertEqual(len(rates[0]["rates"]), 5)
        self.assertIsNone(rates[0]["fast_rate_a_per_s"])
        self.assertIn("finite and positive", rates[0]["fast_rate_error"])

    def test_controller_reads_fast_rate_and_audits_actual_lead_speed(self):
        adapter, supply = self.make_adapter()
        worker = _MagnetWorker()
        worker.adapter = adapter
        rates, audits = [], []
        worker.rates_updated.connect(rates.append)
        worker.safe_move_audit.connect(audits.append)
        worker.refresh_rates()
        self.assertEqual(rates[0]["fast_rate_a_per_s"], 5)
        worker.safe_move_to_field(1, "persistent", True, True, 0, 2)
        self.assertEqual(audits[0]["outcome"], "completed")
        self.assertTrue(audits[0]["lead_ramps"][0]["fast_used"])
        self.assertTrue(any(c["command"] == "RATE? 5" for c in audits[0]["commands"]))

    def test_ui_displays_fast_readback_and_keeps_lead_progress_in_tesla(self):
        panel = MCDPanel(_PanelMagnetController(), _EmptyController(), _EmptyController(), _EmptyController())
        self.addCleanup(panel.close)
        panel._on_rates_updated({"fast_rate_a_per_s": 5})
        self.assertIn("5 A/s", panel._safe_rate.text())
        panel._on_transition("matching leads (FAST)", 2.0)
        self.assertIn("FAST", panel._transition_status.text())
        self.assertIn("2.000000 T", panel._output.text())
        self.assertNotIn("s remaining", panel._transition_status.text())


if __name__ == "__main__":
    unittest.main()
