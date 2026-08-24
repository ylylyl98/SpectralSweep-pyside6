from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.devices.rotation_eps300_adapter import NewportEPS300
from app.devices.rotation_esp300_shared_adapter import SharedESP300Rotation


class FakeESP300(NewportEPS300):
    def __init__(self, axes: dict[int, dict[str, object]]):
        self._axis = 1
        self.axes = axes
        self.writes: list[str] = []
        self.stopped = {axis: True for axis in axes}

    def _query(self, command: str) -> str:
        axis = int(command[0])
        key = command[1:].replace("?", "")
        if key == "ID":
            return str(self.axes[axis][key])
        return str(self.axes[axis][key])

    def _write(self, command: str):
        self.writes.append(command)
        if command == "SM":
            return
        axis = int(command[0])
        key = command[1:3]
        self.axes[axis][key] = float(command[3:])

    def is_motion_done(self, axis=None) -> bool:
        return self.stopped[int(self._axis if axis is None else axis)]


def axis(stage_id: str, *, va: float, vu: float, ac: float, au: float, ag: float):
    return {"ID": stage_id, "VA": va, "VU": vu, "AC": ac, "AU": au, "AG": ag}


class RotationMotionProfileTests(unittest.TestCase):
    def test_shared_rotator_applies_selected_axis_profile_before_motor_on(self):
        calls = []

        class Controller:
            def apply_fast_safe_motion_profile(self, **kwargs):
                calls.append(("apply", kwargs))
                return SimpleNamespace(axis=kwargs["axis"])

            def motor_on(self, *, axis):
                calls.append(("motor_on", axis))

        controller = Controller()
        with patch(
            "app.devices.rotation_esp300_shared_adapter.acquire_shared_esp300",
            return_value=controller,
        ):
            adapter = SharedESP300Rotation(
                "ASRL1::INSTR",
                axis=2,
                role="rot2",
                velocity_fraction=0.75,
                acceleration_fraction=0.25,
            )

        self.assertEqual(calls[0], (
            "apply",
            {"axis": 2, "velocity_fraction": 0.75, "acceleration_fraction": 0.25},
        ))
        self.assertEqual(calls[1], ("motor_on", 2))
        self.assertEqual(adapter.motion_profile.axis, 2)

    def test_failed_profile_application_releases_shared_controller(self):
        class Controller:
            def apply_fast_safe_motion_profile(self, **_kwargs):
                raise RuntimeError("profile rejected")

        with patch(
            "app.devices.rotation_esp300_shared_adapter.acquire_shared_esp300",
            return_value=Controller(),
        ), patch(
            "app.devices.rotation_esp300_shared_adapter.release_shared_esp300"
        ) as release:
            with self.assertRaisesRegex(RuntimeError, "profile rejected"):
                SharedESP300Rotation("ASRL1::INSTR", axis=1, role="rot1")

        release.assert_called_once_with("ASRL1::INSTR")

    def test_each_axis_discovers_and_uses_its_own_limits(self):
        device = FakeESP300({
            1: axis("ROT1", va=20, vu=80, ac=10, au=320, ag=10),
            2: axis("ROT2", va=5, vu=30, ac=5, au=100, ag=5),
        })

        rot1 = device.apply_fast_safe_motion_profile(
            axis=1, velocity_fraction=1.0, acceleration_fraction=0.5
        )
        rot2 = device.apply_fast_safe_motion_profile(
            axis=2, velocity_fraction=1.0, acceleration_fraction=0.5
        )

        self.assertEqual((rot1.velocity, rot1.acceleration, rot1.deceleration), (80, 160, 160))
        self.assertEqual((rot2.velocity, rot2.acceleration, rot2.deceleration), (30, 50, 50))
        self.assertEqual(
            device.writes,
            ["1VU80", "1AU320", "1VA80", "1AC160", "1AG160",
             "2VU30", "2AU100", "2VA30", "2AC50", "2AG50"],
        )

    def test_profile_change_is_rejected_while_axis_is_moving(self):
        device = FakeESP300({1: axis("ROT1", va=20, vu=80, ac=10, au=320, ag=10)})
        device.stopped[1] = False

        with self.assertRaisesRegex(RuntimeError, "must be stopped"):
            device.apply_fast_safe_motion_profile(axis=1)

        self.assertEqual(device.writes, [])

    def test_invalid_fractions_and_limits_are_rejected(self):
        device = FakeESP300({1: axis("ROT1", va=20, vu=80, ac=10, au=320, ag=10)})
        with self.assertRaises(ValueError):
            device.apply_fast_safe_motion_profile(axis=1, velocity_fraction=1.01)
        with self.assertRaises(ValueError):
            device.apply_fast_safe_motion_profile(axis=1, acceleration_fraction=0)

        device.axes[1]["VU"] = 0
        with self.assertRaisesRegex(RuntimeError, "invalid motion limits"):
            device.apply_fast_safe_motion_profile(axis=1)

    def test_readback_mismatch_fails_authoritatively(self):
        class IgnoresVelocity(FakeESP300):
            def _write(self, command: str):
                if command.startswith("1VA"):
                    self.writes.append(command)
                    return
                super()._write(command)

        device = IgnoresVelocity({1: axis("ROT1", va=20, vu=80, ac=10, au=320, ag=10)})
        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            device.apply_fast_safe_motion_profile(axis=1)

    def test_nonvolatile_save_is_explicit(self):
        device = FakeESP300({1: axis("ROT1", va=20, vu=80, ac=10, au=320, ag=10)})
        device.apply_fast_safe_motion_profile(axis=1)
        self.assertNotIn("SM", device.writes)

        device.save_settings()

        self.assertEqual(device.writes[-1], "SM")


if __name__ == "__main__":
    unittest.main()
