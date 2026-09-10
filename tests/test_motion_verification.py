from __future__ import annotations

import unittest
from unittest.mock import patch

from app.devices.motion_verification import (
    MotionVerificationConfig,
    MotionVerificationError,
    move_and_verify,
    motion_timeout_s,
)
from app.devices.rotation_eps300_adapter import NewportEPS300


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, value):
        self.now += float(value)


class _Device:
    position_unit = "mm"
    motion_tolerance = 0.01

    def __init__(self, *, statuses=(True,), positions=(10.0,), result=True):
        self.position = 0.0
        self.statuses = list(statuses)
        self.positions = list(positions)
        self.result = result
        self.moves = []
        self.status_calls = 0

    def move_to(self, target):
        self.moves.append(float(target))
        self.position = float(target)
        return self.result

    def motion_status(self):
        self.status_calls += 1
        if self.statuses:
            return self.statuses.pop(0)
        return True

    def get_position(self):
        if self.positions:
            return float(self.positions.pop(0))
        return self.position


class MotionVerificationTests(unittest.TestCase):
    def _config(self, **overrides):
        values = dict(
            poll_interval_s=0.5,
            read_retries=2,
            read_retry_delay_s=0.25,
            settling_s=0.0,
            max_corrections=1,
            default_timeout_s=2.0,
            timeout_margin_s=0.1,
        )
        values.update(overrides)
        return MotionVerificationConfig(**values)

    def test_slow_profile_gets_distance_based_deadline(self):
        clock = _Clock()

        class Slow(_Device):
            motion_profile = type("Profile", (), {"velocity": 1.0, "acceleration": 1.0})()

            def motion_status(self):
                clock.now += 1.0
                return clock.now >= 10.0

        device = Slow(positions=(0.0, 10.0))
        actual = move_and_verify(
            device,
            10.0,
            config=self._config(default_timeout_s=1.0),
            clock=clock.monotonic,
            sleep=clock.sleep,
        )
        self.assertEqual(actual, 10.0)
        self.assertEqual(device.moves, [10.0])

    def test_stall_times_out_without_second_command(self):
        clock = _Clock()
        device = _Device(statuses=[False] * 20, positions=(0.0,))
        with self.assertRaisesRegex(MotionVerificationError, "remained in motion"):
            move_and_verify(
                device,
                10.0,
                config=self._config(default_timeout_s=1.0),
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
        self.assertEqual(device.moves, [10.0])

    def test_read_glitches_retry_without_reissuing_move(self):
        clock = _Clock()

        class Glitch(_Device):
            def __init__(self):
                super().__init__(statuses=(True,), positions=(0.0,))
                self.reads = 0

            def get_position(self):
                self.reads += 1
                if self.reads <= 2:
                    raise OSError("temporary read glitch")
                return 10.0

        device = Glitch()
        self.assertEqual(
            move_and_verify(device, 10.0, config=self._config(), clock=clock.monotonic, sleep=clock.sleep),
            10.0,
        )
        self.assertEqual(device.moves, [10.0])

    def test_stopped_short_move_has_only_one_bounded_correction(self):
        clock = _Clock()

        class Short(_Device):
            def __init__(self):
                super().__init__(statuses=(True,), positions=(0.0, *([9.0] * 10)))

        device = Short()
        with self.assertRaisesRegex(MotionVerificationError, "differs"):
            move_and_verify(
                device,
                10.0,
                config=self._config(stable_readings=1),
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
        self.assertEqual(device.moves, [10.0, 10.0])

    def test_mismatch_with_unknown_status_is_not_retried(self):
        clock = _Clock()

        class Unknown(_Device):
            def motion_status(self):
                return None

        device = Unknown(statuses=(None,), positions=(0.0, *([9.0] * 10)))
        with self.assertRaisesRegex(MotionVerificationError, "unknown or still moving"):
            move_and_verify(device, 10.0, config=self._config(), clock=clock.monotonic, sleep=clock.sleep)
        self.assertEqual(device.moves, [10.0])

    def test_false_move_result_is_failure(self):
        clock = _Clock()
        device = _Device(result=False, positions=(0.0,))
        with self.assertRaisesRegex(MotionVerificationError, "was not completed"):
            move_and_verify(device, 10.0, config=self._config(), clock=clock.monotonic, sleep=clock.sleep)
        self.assertEqual(device.moves, [10.0])

    def test_strict_readback_cannot_fall_back_to_cached_target(self):
        clock = _Clock()

        class Cached(_Device):
            def get_position(self):
                return 10.0

            def get_position_strict(self):
                raise OSError("controller read failed")

        device = Cached(positions=(0.0,))
        with self.assertRaisesRegex(MotionVerificationError, "no genuine readback"):
            move_and_verify(device, 10.0, config=self._config(read_retries=1), clock=clock.monotonic, sleep=clock.sleep)
        self.assertEqual(device.moves, [10.0])

    def test_operating_profile_uses_deceleration_not_hardware_maximum(self):
        from types import SimpleNamespace
        device = SimpleNamespace(motion_profile=SimpleNamespace(
            velocity=1.0, acceleration=10.0, deceleration=0.01, max_velocity=100.0))
        self.assertGreater(motion_timeout_s(device, 10.0, config=self._config()), 200.0)
        device.motion_profile = SimpleNamespace(max_velocity=1000.0, max_acceleration=1000.0)
        self.assertEqual(motion_timeout_s(device, 10.0, config=self._config()), 2.0)

    def test_deadline_reaches_raw_backend_without_nested_move(self):
        clock = _Clock()
        class Raw(_Device):
            motion_profile = type("Profile", (), {"velocity": 0.01})()
            def move_to(self, target):
                raise AssertionError("verified adapter must not be reentered")
            def _move_to_unverified(self, target, *, timeout_s):
                self.budget = timeout_s
                self.position = target
                self.moves.append(target)
                return True
        device = Raw(positions=(0.0,))
        move_and_verify(device, 10.0, config=self._config(), clock=clock.monotonic, sleep=clock.sleep)
        self.assertGreater(device.budget, 2000.0)
        self.assertEqual(device.moves, [10.0])

    def test_read_failure_breaks_consecutive_stability(self):
        clock = _Clock()
        class Glitch(_Device):
            def __init__(self):
                super().__init__()
                self.reads = 0
            def get_position(self):
                self.reads += 1
                if self.reads == 3:
                    raise OSError("transient")
                return 0.0 if self.reads == 1 else 10.0
        device = Glitch()
        move_and_verify(device, 10.0, config=self._config(), clock=clock.monotonic, sleep=clock.sleep)
        self.assertEqual(device.reads, 5)  # initial, good, error, good, good

    def test_settling_starts_after_motion_stops(self):
        clock = _Clock()
        class Moving(_Device):
            def motion_status(self):
                return clock.now >= 1.0
            def get_position(self):
                if self.moves:
                    self.first_read_time = getattr(self, "first_read_time", clock.now)
                return self.position
        device = Moving()
        move_and_verify(device, 10.0, config=self._config(settling_s=0.5, default_timeout_s=3),
                        clock=clock.monotonic, sleep=clock.sleep)
        self.assertGreaterEqual(device.first_read_time, 1.5)
        self.assertEqual(device.moves, [10.0])

    def test_late_blocking_return_cannot_succeed_or_restore(self):
        clock = _Clock()
        class Late(_Device):
            def move_to(self, target):
                super().move_to(target)
                clock.now = 3.0
            def motion_status(self):
                return None
        device = Late(positions=(0.0,))
        with self.assertRaisesRegex(MotionVerificationError, "timed out"):
            move_and_verify(device, 10.0, config=self._config(), clock=clock.monotonic, sleep=clock.sleep)
        with self.assertRaisesRegex(MotionVerificationError, "previous motion is unconfirmed"):
            move_and_verify(device, 0.0, config=self._config(), clock=clock.monotonic, sleep=clock.sleep)
        self.assertEqual(device.moves, [10.0])

    def test_correction_requires_fresh_stopped_status_after_readback(self):
        clock = _Clock()
        device = _Device(statuses=(True, True, False, *([False] * 20)), positions=(0.0, *([9.0] * 10)))
        with self.assertRaisesRegex(MotionVerificationError, "timed out"):
            move_and_verify(device, 10.0, config=self._config(), clock=clock.monotonic, sleep=clock.sleep)
        self.assertEqual(device.moves, [10.0])

    def test_stable_readbacks_with_unknown_status_do_not_prove_completion(self):
        clock = _Clock()
        class Unknown(_Device):
            def motion_status(self):
                return None
        device = Unknown(positions=(0.0, *([10.0] * 10)))
        with self.assertRaisesRegex(MotionVerificationError, "timed out"):
            move_and_verify(device, 10.0, config=self._config(), clock=clock.monotonic, sleep=clock.sleep)
        self.assertEqual(device.moves, [10.0])

    def test_direct_esp_stage_uses_configured_axis_speed_and_verifies(self):
        from types import SimpleNamespace
        from app.devices.stage_newport_adapter import NewportESP300LinearStage
        class Controller:
            def __init__(self):
                self.position = 0.0
                self.reads = 0
                self.moves = []
            def get_motion_profile(self, *, axis):
                self.profile_axis = axis
                return SimpleNamespace(velocity=0.01, acceleration=0.1, deceleration=0.1)
            def get_position(self, *, axis):
                self.reads += 1
                return self.position
            def motion_status(self, *, axis):
                return True
            def move_to(self, target, **kwargs):
                self.moves.append(kwargs)
                self.position = target
                return True
        device = NewportESP300LinearStage.__new__(NewportESP300LinearStage)
        device._controller = controller = Controller()
        device._axis = 3
        device.motion_tolerance = 0.01
        self.assertTrue(device.move_to(10.0))
        self.assertEqual(len(controller.moves), 1)
        self.assertEqual(controller.profile_axis, 3)
        self.assertGreater(controller.moves[0]["timeout_s"], 2000.0)
        self.assertGreaterEqual(controller.reads, 3)

    def test_direct_elliptec_passes_timeout_and_rejects_false(self):
        # Use the real repository wrapper with a fake synchronous SDK. No port opens.
        from app.devices.rotation_thorlabs_elliptec_adapter import ElliptecRotation
        from types import SimpleNamespace
        from unittest.mock import Mock
        device = ElliptecRotation.__new__(ElliptecRotation)
        device._last = 0.0
        device._drv = SimpleNamespace(move_to=Mock(return_value=False), get_position=lambda: 0.0)
        with self.assertRaisesRegex(RuntimeError, "failed"):
            device.move_to(10.0, timeout_s=120.0)
        self.assertGreater(device._drv.move_to.call_args.kwargs["timeout"], 119.0)
        self.assertEqual(device._last, 0.0)
        with self.assertRaisesRegex(MotionVerificationError, "previous motion is unconfirmed"):
            device.move_to(0.0)
        self.assertEqual(device._drv.move_to.call_count, 1)

    def test_elliptec_display_readback_does_not_substitute_cached_target(self):
        from app.devices.rotation_thorlabs_elliptec_adapter import ElliptecRotation
        from types import SimpleNamespace
        from unittest.mock import Mock
        device = ElliptecRotation.__new__(ElliptecRotation)
        device._last = 45.0
        device._drv = SimpleNamespace(get_position=Mock(side_effect=OSError("read failed")))
        with self.assertRaisesRegex(OSError, "read failed"):
            device.get_position()

    def test_esp300_timeout_raises_instead_of_returning_success(self):
        class StalledESP(NewportEPS300):
            def __init__(self):
                self._axis = 1
                self._inst = object()
                self.writes = []

            def _write(self, command):
                self.writes.append(command)

            def motion_status(self, axis=None):
                return False

        device = StalledESP()
        with patch(
            "app.devices.rotation_eps300_adapter.time.monotonic",
            side_effect=(0.0, 2.0),
        ):
            with self.assertRaisesRegex(TimeoutError, "did not report completion"):
                device.move_to(10.0, timeout_s=1.0)
        self.assertEqual(device.writes, ["1PA10.0"])


if __name__ == "__main__":
    unittest.main()
