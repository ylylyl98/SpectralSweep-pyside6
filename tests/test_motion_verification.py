from __future__ import annotations

import unittest
from unittest.mock import patch

from app.devices.motion_verification import (
    MotionVerificationConfig,
    MotionVerificationError,
    MotionHardwareFault,
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
    def test_default_retries_stopped_misses_up_to_three_times(self):
        from app.devices.motion_verification import DEFAULT_MOTION_CONFIG

        class MissedTarget(_Device):
            def get_position(self):
                return 10.0 if len(self.moves) >= 4 else 8.0

        device = MissedTarget()
        clock = _Clock()
        actual = move_and_verify(device, 10.0, config=DEFAULT_MOTION_CONFIG,
                                 clock=clock.monotonic, sleep=clock.sleep)
        self.assertEqual(actual, 10.0)
        self.assertEqual(device.moves, [10.0] * 4)
        self.assertFalse(device._motion_uncertain)

    def test_default_retry_exhaustion_reports_three_retries(self):
        from app.devices.motion_verification import DEFAULT_MOTION_CONFIG

        class NeverReached(_Device):
            def get_position(self):
                return 8.0

        device = NeverReached()
        clock = _Clock()
        with self.assertRaisesRegex(MotionVerificationError, "after 3 correction retries"):
            move_and_verify(device, 10.0, config=DEFAULT_MOTION_CONFIG,
                            clock=clock.monotonic, sleep=clock.sleep)
        self.assertEqual(device.moves, [10.0] * 4)
        self.assertTrue(device._motion_uncertain)

    def test_elliptec_stage_one_unit_tolerance(self):
        from types import SimpleNamespace
        from app.devices.stage_elliptec_adapter import ElliptecLinearStage

        # Use the real adapter default and verification with a stationary
        # simulated controller; never open a hardware connection.
        for offset, accepted in ((0.25390625, True), (1.0, True), (1.01, False)):
            with self.subTest(offset=offset):
                driver = SimpleNamespace(
                    get_status=lambda: "ok",
                    get_position=lambda: 3400.0 + offset,
                    move_to=lambda target, **kwargs: True,
                )
                with patch("app.devices.stage_elliptec_adapter.Thorlabs.ElliptecMotor", return_value=driver):
                    device = ElliptecLinearStage("simulated")
                clock = _Clock()
                kwargs = dict(config=self._config(), clock=clock.monotonic, sleep=clock.sleep)
                if accepted:
                    self.assertEqual(move_and_verify(device, 3400.0, **kwargs), 3400.0 + offset)
                else:
                    with self.assertRaisesRegex(MotionVerificationError, "tolerance 1,"):
                        move_and_verify(device, 3400.0, **kwargs)

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

    def test_uncertain_move_recovers_after_stopped_status_and_stable_readback(self):
        clock = _Clock()

        class Recoverable(_Device):
            def __init__(self):
                super().__init__(statuses=(False, True, True, True), positions=(5.0, 5.0, 5.0))
                self._motion_uncertain = True

        device = Recoverable()
        self.assertEqual(
            move_and_verify(
                device,
                10.0,
                config=self._config(
                    stable_readings=2,
                    uncertain_recovery_timeout_s=2.0,
                ),
                clock=clock.monotonic,
                sleep=clock.sleep,
            ),
            10.0,
        )
        self.assertEqual(device.moves, [10.0])
        self.assertFalse(device._motion_uncertain)

    def test_uncertain_move_does_not_wait_when_status_capability_is_absent(self):
        clock = _Clock()

        class Unknown(_Device):
            motion_status_available = False

        device = Unknown(statuses=(None,), positions=(5.0, 5.0))
        device._motion_uncertain = True
        with self.assertRaisesRegex(MotionVerificationError, "stopped status unavailable"):
            move_and_verify(
                device,
                10.0,
                config=self._config(uncertain_recovery_timeout_s=1.0),
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
        self.assertEqual(device.moves, [])
        self.assertEqual(clock.now, 0.0)

    def test_uncertain_move_times_out_without_stopped_status(self):
        clock = _Clock()
        device = _Device(statuses=[False] * 20, positions=(5.0,))
        device._motion_uncertain = True
        with self.assertRaisesRegex(MotionVerificationError, "recovery timed out"):
            move_and_verify(
                device,
                10.0,
                config=self._config(uncertain_recovery_timeout_s=1.0),
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
        self.assertEqual(device.moves, [])

    def test_uncertain_move_distinguishes_unknown_status_from_active_motion(self):
        clock = _Clock()
        unknown = _Device(statuses=[None] * 20, positions=(5.0,))
        unknown._motion_uncertain = True
        with self.assertRaisesRegex(MotionVerificationError, "unavailable or unreadable"):
            move_and_verify(
                unknown,
                10.0,
                config=self._config(uncertain_recovery_timeout_s=0.75),
                clock=clock.monotonic,
                sleep=clock.sleep,
            )

        clock = _Clock()
        active = _Device(statuses=[False] * 20, positions=(5.0,))
        active._motion_uncertain = True
        with self.assertRaisesRegex(MotionVerificationError, "reports motion active"):
            move_and_verify(
                active,
                10.0,
                config=self._config(uncertain_recovery_timeout_s=0.75),
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
        self.assertEqual(active.moves, [])

    def test_uncertain_recovery_and_new_move_share_explicit_timeout_budget(self):
        clock = _Clock()
        device = _Device(statuses=[True, True, True], positions=(5.0, 5.0))
        device._motion_uncertain = True
        with self.assertRaisesRegex(MotionVerificationError, "timed out"):
            move_and_verify(
                device,
                10.0,
                timeout_s=0.5,
                config=self._config(stable_readings=2, uncertain_recovery_timeout_s=5.0),
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
        self.assertEqual(device.moves, [])

    def test_late_final_recovery_status_cannot_clear_latch(self):
        clock = _Clock()

        class LateStatus(_Device):
            def motion_status(self):
                value = super().motion_status()
                if self.status_calls >= 3:
                    clock.now = 1.0
                return value

        device = LateStatus(statuses=[True, True, True], positions=(5.0, 5.0))
        device._motion_uncertain = True
        with self.assertRaisesRegex(MotionVerificationError, "timed out"):
            move_and_verify(
                device,
                10.0,
                timeout_s=0.75,
                config=self._config(stable_readings=2, uncertain_recovery_timeout_s=5.0),
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
        self.assertEqual(device.moves, [])
        self.assertTrue(device._motion_uncertain)

    def test_uncertain_move_recovery_rejects_unstable_position(self):
        clock = _Clock()
        device = _Device(statuses=[True] * 20, positions=(5.0, 5.2, 5.0, 5.2))
        device._motion_uncertain = True
        with self.assertRaisesRegex(MotionVerificationError, "stable position"):
            move_and_verify(
                device,
                10.0,
                config=self._config(
                    stable_readings=2,
                    uncertain_recovery_timeout_s=0.75,
                ),
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
        self.assertEqual(device.moves, [])

    def test_uncertain_recovery_rejects_out_of_range_position(self):
        clock = _Clock()

        class Bounded(_Device):
            def validate_position(self, position):
                if not 0.0 <= float(position) <= 50.0:
                    raise ValueError("outside stage range")
                return float(position)

        device = Bounded(statuses=[True] * 20, positions=(100.0,) * 20)
        device._motion_uncertain = True
        with self.assertRaisesRegex(MotionVerificationError, "stable position unavailable"):
            move_and_verify(
                device,
                10.0,
                config=self._config(uncertain_recovery_timeout_s=0.75),
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
        self.assertEqual(device.moves, [])

    def test_uncertain_recovery_requires_final_stopped_status(self):
        clock = _Clock()
        device = _Device(statuses=[True, True, False] + [False] * 20, positions=(5.0, 5.0))
        device._motion_uncertain = True
        with self.assertRaisesRegex(MotionVerificationError, "recovery timed out"):
            move_and_verify(
                device,
                10.0,
                config=self._config(
                    stable_readings=2,
                    uncertain_recovery_timeout_s=0.75,
                ),
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
        self.assertEqual(device.moves, [])

    def test_uncertain_move_recovery_honors_cancellation(self):
        clock = _Clock()

        class Stop:
            def __init__(self):
                self.calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls > 1

        device = _Device(statuses=[False] * 5, positions=(5.0,))
        device._motion_uncertain = True
        with self.assertRaisesRegex(MotionVerificationError, "cancelled"):
            move_and_verify(
                device,
                10.0,
                stop_event=Stop(),
                config=self._config(uncertain_recovery_timeout_s=1.0),
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
        self.assertEqual(device.moves, [])

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

    def test_elliptec_status_maps_only_ok_to_stopped(self):
        from app.devices.stage_elliptec_adapter import ElliptecLinearStage
        from types import SimpleNamespace

        device = ElliptecLinearStage.__new__(ElliptecLinearStage)
        device.stage = SimpleNamespace(get_status=lambda: "busy")
        self.assertTrue(device.motion_status_available)
        self.assertFalse(device.motion_status())
        device.stage.get_status = lambda: "ok"
        self.assertTrue(device.motion_status())
        device.stage.get_status = lambda: "mech_timeout"
        with self.assertRaisesRegex(MotionHardwareFault, "mech_timeout"):
            device.motion_status()
        device.stage.get_status = lambda: "busy"
        self.assertFalse(device.motion_status())
        device.stage.get_status = lambda: "comm_timeout"
        self.assertIsNone(device.motion_status())
        device.stage.get_status = lambda: None
        self.assertIsNone(device.motion_status())
        device.stage.get_status = lambda: "unexpected"
        self.assertIsNone(device.motion_status())

    def test_elliptec_without_status_api_is_reported_unavailable(self):
        from app.devices.stage_elliptec_adapter import ElliptecLinearStage
        from types import SimpleNamespace

        device = ElliptecLinearStage.__new__(ElliptecLinearStage)
        device.stage = SimpleNamespace()
        self.assertFalse(device.motion_status_available)
        self.assertIsNone(device.motion_status())

    def test_elliptec_rotation_recovers_latch_before_issuing_new_move(self):
        from app.devices.rotation_thorlabs_elliptec_adapter import ElliptecRotation
        from types import SimpleNamespace
        from unittest.mock import Mock

        class Driver:
            def __init__(self):
                self.statuses = []
                self.positions = [0.0]
                self.move_result = False
                self.move_calls = []
                self.position = 0.0

            def move_to(self, target, *, timeout):
                self.move_calls.append((float(target), float(timeout)))
                if self.move_result:
                    self.position = float(target)
                return self.move_result

            def get_status(self):
                return self.statuses.pop(0) if self.statuses else "ok"

            def get_position(self):
                return self.positions.pop(0) if self.positions else self.position

        device = ElliptecRotation.__new__(ElliptecRotation)
        device._last = 0.0
        device.motion_tolerance = 0.25
        device.position_unit = "deg"
        device._drv = Driver()
        clock = _Clock()
        with self.assertRaisesRegex(RuntimeError, "failed"):
            device.move_to(10.0, timeout_s=1.0)
        self.assertTrue(device._motion_uncertain)
        self.assertEqual(len(device._drv.move_calls), 1)

        device._drv.move_result = True
        device._drv.statuses = ["ok", "ok", "ok"]
        self.assertTrue(device.move_to(10.0, timeout_s=2.0))
        self.assertEqual(len(device._drv.move_calls), 2)
        self.assertFalse(device._motion_uncertain)

    def test_elliptec_rotation_unknown_status_rejects_without_new_move(self):
        from app.devices.rotation_thorlabs_elliptec_adapter import ElliptecRotation

        class Driver:
            def __init__(self):
                self.move_calls = []
                self.position = 0.0

            def move_to(self, target, *, timeout):
                self.move_calls.append(float(target))
                return True

            def get_status(self):
                return None

            def get_position(self):
                return self.position

        device = ElliptecRotation.__new__(ElliptecRotation)
        device._last = 0.0
        device.motion_tolerance = 0.25
        device.position_unit = "deg"
        device._drv = Driver()
        device._motion_uncertain = True
        clock = _Clock()
        with self.assertRaisesRegex(MotionVerificationError, "unavailable or unreadable"):
            move_and_verify(
                device,
                10.0,
                config=self._config(uncertain_recovery_timeout_s=0.5),
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
        self.assertEqual(device._drv.move_calls, [])

    def test_elliptec_rotation_hardware_fault_is_terminal_during_recovery(self):
        from app.devices.rotation_thorlabs_elliptec_adapter import ElliptecRotation

        class Driver:
            def __init__(self):
                self.statuses = ["motor_error", "ok", "ok"]
                self.move_calls = []

            def move_to(self, target, *, timeout):
                self.move_calls.append(float(target))
                return True

            def get_status(self):
                return self.statuses.pop(0) if self.statuses else "ok"

            def get_position(self):
                return 0.0

        device = ElliptecRotation.__new__(ElliptecRotation)
        device._drv = Driver()
        device._last = 0.0
        device.motion_tolerance = 0.25
        device.position_unit = "deg"
        device._motion_uncertain = True
        with self.assertRaisesRegex(MotionHardwareFault, "motor_error"):
            move_and_verify(device, 10.0, config=self._config(uncertain_recovery_timeout_s=1.0))
        self.assertEqual(device._drv.move_calls, [])
        self.assertTrue(device._motion_uncertain)

    def test_elliptec_stage_hardware_fault_is_terminal_during_recovery(self):
        from app.devices.stage_elliptec_adapter import ElliptecLinearStage

        class Driver:
            def __init__(self):
                self.statuses = ["overcurrent", "ok", "ok"]
                self.move_calls = []

            def move_to(self, target, *, timeout):
                self.move_calls.append(float(target))
                return True

            def get_status(self):
                return self.statuses.pop(0) if self.statuses else "ok"

            def get_position(self):
                return 0.0

        device = ElliptecLinearStage.__new__(ElliptecLinearStage)
        device.stage = Driver()
        device.motion_tolerance = 0.01
        device._motion_uncertain = True
        with self.assertRaisesRegex(MotionHardwareFault, "overcurrent"):
            move_and_verify(device, 10.0, config=self._config(uncertain_recovery_timeout_s=1.0))
        self.assertEqual(device.stage.move_calls, [])
        self.assertTrue(device._motion_uncertain)

    def test_elliptec_hardware_fault_is_terminal_during_normal_verification(self):
        from app.devices.rotation_thorlabs_elliptec_adapter import ElliptecRotation

        class Driver:
            def __init__(self):
                self.move_calls = []

            def move_to(self, target, *, timeout):
                self.move_calls.append(float(target))
                return True

            def get_status(self):
                return "therm_error"

            def get_position(self):
                return 0.0

        device = ElliptecRotation.__new__(ElliptecRotation)
        device._drv = Driver()
        device._last = 0.0
        device.motion_tolerance = 0.25
        device.position_unit = "deg"
        with self.assertRaisesRegex(MotionHardwareFault, "therm_error"):
            move_and_verify(device, 10.0, config=self._config())
        self.assertEqual(device._drv.move_calls, [10.0])
        self.assertTrue(device._motion_uncertain)

    def test_esp300_rotation_recovers_latch_and_verifies_target(self):
        from app.devices.rotation_esp300_shared_adapter import SharedESP300Rotation
        from types import SimpleNamespace

        class Controller:
            def __init__(self):
                self.statuses = [True, True, True, True]
                self.positions = [0.0, 0.0, 10.0, 10.0]
                self.moves = []
                self.position = 0.0

            def get_motion_profile(self, *, axis):
                return SimpleNamespace(velocity=1.0, acceleration=1.0, deceleration=1.0)

            def motion_status(self, *, axis):
                return self.statuses.pop(0) if self.statuses else True

            def get_position(self, *, axis):
                return self.positions.pop(0) if self.positions else self.position

            def move_to(self, target, *, axis, stop_event, timeout_s):
                self.moves.append((float(target), float(timeout_s)))
                self.position = float(target)
                return True

        device = SharedESP300Rotation.__new__(SharedESP300Rotation)
        device._controller = Controller()
        device._axis = 2
        device._role = "rotation"
        device.motion_tolerance = 0.25
        device._motion_uncertain = True
        clock = _Clock()
        observed = move_and_verify(
            device,
            10.0,
            config=self._config(uncertain_recovery_timeout_s=2.0),
            clock=clock.monotonic,
            sleep=clock.sleep,
        )
        self.assertEqual(observed, 10.0)
        self.assertEqual([target for target, _ in device._controller.moves], [10.0])
        self.assertFalse(device._motion_uncertain)

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
