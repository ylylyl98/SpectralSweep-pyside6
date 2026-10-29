"""Motor-off and following-error evidence must prevent corrective moves."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.devices.motion_verification import (
    MotionHardwareFault, MotionVerificationError, move_and_verify,
)
from app.devices.rotation_eps300_adapter import NewportEPS300
from app.devices.rotation_esp300_shared_adapter import SharedESP300Rotation


FOLLOWING_ERROR = "103, 6206874, AXIS-1 FOLLOWING ERROR THRESHOLD EXCEEDED"
MOTOR_DISABLED = "113, 6206900, AXIS-1 MOTOR NOT ENABLED"
NO_ERROR = "0, 6207000, NO ERROR DETECTED"


class Instrument:
    def __init__(self, responses):
        self.responses = {key: list(value) if isinstance(value, list) else value
                          for key, value in responses.items()}
        self.commands = []
        self.closed = False

    def write(self, command):
        self.commands.append(command)

    def read(self):
        response = self.responses.get(self.commands[-1], RuntimeError("unexpected query"))
        if isinstance(response, list):
            response = response.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


class NewportMotionFaultTests(unittest.TestCase):
    def setUp(self):
        self.sleep_patch = patch("app.devices.rotation_eps300_adapter.time.sleep")
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def controller(self, responses):
        instrument = Instrument(responses)
        manager = SimpleNamespace(open_resource=lambda _: instrument)
        device = NewportEPS300("ASRL8::INSTR", model="esp301", rm=manager)
        self.addCleanup(device.close)
        return device, instrument

    def rotation(self, controller):
        rotation = SharedESP300Rotation.__new__(SharedESP300Rotation)
        rotation._controller = controller
        rotation._axis = 1
        rotation._resource = "ASRL8::INSTR"
        rotation._role = "rot1"
        rotation.motion_tolerance = 0.25
        return rotation

    def test_enable_is_written_once_then_power_is_verified(self):
        device, instrument = self.controller({"1MO?": ["0", "1"]})
        device.motor_on(axis=1)
        self.assertEqual(instrument.commands, ["1MO", "1MO?", "1MO?"])

    def test_real_shared_connect_enables_only_selected_axis_before_returning(self):
        for model, axis, baud in (("esp300", 2, 19200), ("esp301", 1, 921600)):
            with self.subTest(model=model, axis=axis):
                events = []

                class InitiallyOffInstrument(Instrument):
                    def __init__(self):
                        mask = (1 << (axis - 1)) | (1 << (axis + 7))
                        super().__init__({"ZU": f"{mask:X}H"})
                        self.power = 0
                        self.enable_requested = False
                        self.power_reads = 0

                    def write(self, command):
                        super().write(command)
                        if command == f"{axis}MO":
                            self.enable_requested = True
                            events.append(("enable", axis))

                    def read(self):
                        if self.commands[-1] == f"{axis}MO?":
                            if not self.enable_requested:
                                raise AssertionError("power verification preceded the enable command")
                            self.power_reads += 1
                            # Simulate activation taking one poll. The adapter
                            # must not return after the initial OFF readback.
                            self.power = 1 if self.power_reads >= 2 else 0
                            events.append(("power_read", self.power))
                            return str(self.power)
                        return super().read()

                instrument = InitiallyOffInstrument()
                self.assertEqual(instrument.power, 0)
                opened = []

                def open_resource(resource):
                    opened.append(resource)
                    return instrument

                manager = SimpleNamespace(open_resource=open_resource)
                resource = f"ASRL1800{axis}::INSTR"
                with patch(
                    "app.devices.rotation_eps300_adapter.pyvisa.ResourceManager",
                    return_value=manager,
                ), patch.object(
                    NewportEPS300, "apply_fast_safe_motion_profile", autospec=True,
                    return_value=SimpleNamespace(axis=axis),
                ) as profile:
                    # Exercise the actual shared acquisition, controller,
                    # discovery, motor_on, and adapter constructor.
                    adapter = SharedESP300Rotation(resource, axis=axis, model=model)
                    events.append(("constructor_returned", axis))
                    try:
                        self.assertEqual(events, [
                            ("enable", axis), ("power_read", 0),
                            ("power_read", 1), ("constructor_returned", axis),
                        ])
                        self.assertEqual(opened, [resource])
                        self.assertEqual(instrument.baud_rate, baud)
                        self.assertEqual(adapter.axis, axis)
                        self.assertFalse(adapter._closed)
                        self.assertEqual(instrument.commands, [
                            "ZU", f"{axis}MO", f"{axis}MO?", f"{axis}MO?",
                        ])
                        profile.assert_called_once_with(
                            adapter._controller, axis=axis,
                            velocity_fraction=1.0, acceleration_fraction=0.5,
                        )
                    finally:
                        adapter.close()
                self.assertTrue(instrument.closed)

    def test_failed_enable_reports_following_error_without_reenable(self):
        device, instrument = self.controller({
            "1MO?": "0", "TB?": [FOLLOWING_ERROR, MOTOR_DISABLED, NO_ERROR],
        })
        with self.assertRaises(MotionHardwareFault) as raised:
            device.motor_on(axis=1)
        message = str(raised.exception)
        self.assertIn("ESP301 axis 1 on ASRL8::INSTR", message)
        self.assertIn("103 AXIS-1 FOLLOWING ERROR THRESHOLD EXCEEDED", message.splitlines()[0])
        self.assertLessEqual(len(message.splitlines()[0]), 220)
        self.assertNotIn("113", message.splitlines()[0])
        self.assertIn("113", "\n".join(message.splitlines()[1:]))
        self.assertEqual(instrument.commands.count("1MO"), 1)
        self.assertEqual(instrument.commands.count("1MO?"), 3)
        self.assertEqual([entry.code for entry in device.error_history], [103, 113])

    def test_disabled_motor_blocks_initial_pa_and_preserves_captured_fifo(self):
        # Reproduce the observed first following-error entry followed by eight
        # motor-disabled entries. No corrective PA should add another 113.
        device, instrument = self.controller({
            "1MO?": "0", "TB?": [FOLLOWING_ERROR] + [MOTOR_DISABLED] * 8 + [NO_ERROR],
        })
        with self.assertRaisesRegex(MotionHardwareFault, "103.*FOLLOWING ERROR"):
            device.move_to(95, axis=1)
        self.assertFalse(any("PA" in command for command in instrument.commands))
        self.assertNotIn("1MO", instrument.commands)
        self.assertEqual(instrument.commands.count("TB?"), 10)
        self.assertEqual([entry.code for entry in device.error_history], [103] + [113] * 8)
        self.assertEqual(device.error_history[0].timestamp, "6206874")

    def test_unknown_power_blocks_move_without_consuming_error_queue(self):
        for response in (TimeoutError("power unreadable"), "unexpected", "", "nan"):
            with self.subTest(response=response):
                device, instrument = self.controller({"1MO?": response})
                with self.assertRaisesRegex(MotionVerificationError, "power could not be verified"):
                    device.move_to(95, axis=1)
                self.assertEqual(instrument.commands, ["1MO?"])

    def test_power_loss_after_pa_is_not_mistaken_for_motion_done(self):
        device, instrument = self.controller({
            "1MO?": ["1", "0"], "1MD?": "1",
            "TB?": [FOLLOWING_ERROR, NO_ERROR],
        })
        with self.assertRaisesRegex(MotionHardwareFault, "103.*FOLLOWING ERROR"):
            device.move_to(95, axis=1)
        self.assertEqual([command for command in instrument.commands if "PA" in command], ["1PA95.0"])
        self.assertNotIn("1MO", instrument.commands)

    def test_verification_power_loss_prevents_all_three_corrections(self):
        device, instrument = self.controller({
            # Both low-level checks pass, then power drops before the higher
            # verifier's next stopped/readback sample.
            "1MO?": ["1", "1", "0"], "1MD?": "1", "1TP": "0.000",
            "TB?": [FOLLOWING_ERROR, NO_ERROR],
        })
        rotation = self.rotation(device)
        with self.assertRaisesRegex(MotionHardwareFault, "103.*FOLLOWING ERROR"):
            move_and_verify(rotation, 95)
        self.assertEqual([command for command in instrument.commands if "PA" in command], ["1PA95.0"])
        self.assertTrue(rotation._motion_uncertain)
        self.assertNotIn("1MO", instrument.commands)

    def test_idle_status_before_enable_still_allows_motion_profile_inspection(self):
        device, instrument = self.controller({"1MD?": "1", "1MO?": "0"})
        self.assertTrue(device.is_motion_done(axis=1))
        self.assertEqual(instrument.commands, ["1MD?"])

    def test_other_axis_errors_retained_without_wrong_axis_attribution(self):
        device, instrument = self.controller({
            "1MO?": "0", "TB?": ["203, 10, AXIS-2 FOLLOWING ERROR", FOLLOWING_ERROR, NO_ERROR],
        })
        with self.assertRaises(MotionHardwareFault) as raised:
            device.require_motor_enabled(axis=1)
        self.assertIn("103", str(raised.exception))
        self.assertNotIn("AXIS-2", str(raised.exception))
        self.assertEqual([entry.axis for entry in device.error_history], [2, 1])
        # A later fault can still reference evidence already consumed by the
        # shared port's other adapter; it is labelled recorded, not newly read.
        instrument.responses.update({"2MO?": "0", "TB?": [NO_ERROR]})
        with self.assertRaises(MotionHardwareFault) as second:
            device.require_motor_enabled(axis=2)
        self.assertIn("203", str(second.exception))
        self.assertNotIn("AXIS-1", str(second.exception))

    def test_fault_queue_drain_is_bounded_and_communication_failure_keeps_history(self):
        device, instrument = self.controller({"1MO?": "0", "TB?": MOTOR_DISABLED})
        with self.assertRaises(MotionHardwareFault):
            device.require_motor_enabled(axis=1)
        self.assertEqual(instrument.commands.count("TB?"), 10)
        self.assertEqual(len(device.error_history), 10)
        instrument.responses["TB?"] = TimeoutError("diagnostic timeout")
        with self.assertRaises(MotionHardwareFault) as raised:
            device.require_motor_enabled(axis=1)
        self.assertEqual(len(device.error_history), 10)
        self.assertIn("diagnostics unavailable", str(raised.exception))

    def test_retained_error_history_is_bounded(self):
        device, _ = self.controller({"1MO?": "0", "TB?": MOTOR_DISABLED})
        for _ in range(11):
            with self.assertRaises(MotionHardwareFault):
                device.require_motor_enabled(axis=1)
        self.assertEqual(len(device.error_history), 100)

    def test_successful_motion_does_not_consume_error_fifo(self):
        device, instrument = self.controller({"1MO?": "1", "1MD?": "1"})
        self.assertTrue(device.move_to(95, axis=1))
        self.assertNotIn("TB?", instrument.commands)
        self.assertEqual(device.error_history, ())

    def test_connect_releases_failed_enable_without_reporting_success(self):
        device, instrument = self.controller({
            "1MO?": "0", "TB?": [FOLLOWING_ERROR, NO_ERROR],
        })
        with patch.object(device, "get_axes", return_value=[1]), patch.object(
            device, "apply_fast_safe_motion_profile", return_value=SimpleNamespace(axis=1)
        ), patch("app.devices.rotation_esp300_shared_adapter.acquire_shared_esp300", return_value=device), patch(
            "app.devices.rotation_esp300_shared_adapter.release_shared_esp300"
        ) as release:
            with self.assertRaisesRegex(MotionHardwareFault, "103.*FOLLOWING ERROR"):
                SharedESP300Rotation("ASRL8::INSTR", model="esp301")
        release.assert_called_once_with("ASRL8::INSTR")
        self.assertEqual(instrument.commands.count("1MO"), 1)


if __name__ == "__main__":
    unittest.main()
