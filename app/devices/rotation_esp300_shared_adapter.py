from __future__ import annotations

from app.devices.esp300_shared import acquire_shared_esp300, release_shared_esp300


class SharedESP300Rotation:
    """Rotation adapter that reuses a shared ESP300 controller session."""

    backend_key = "esp300"
    display_name = "Newport ESP300"

    def __init__(
        self,
        resource: str,
        *,
        axis: int = 1,
        role: str = "rotation",
        velocity_fraction: float = 1.0,
        acceleration_fraction: float = 0.5,
    ):
        self._resource = resource
        self._axis = int(axis)
        self._role = role
        self._controller = acquire_shared_esp300(resource)
        try:
            self.motion_profile = self._controller.apply_fast_safe_motion_profile(
                axis=self._axis,
                velocity_fraction=velocity_fraction,
                acceleration_fraction=acceleration_fraction,
            )
            self._controller.motor_on(axis=self._axis)
        except Exception:
            release_shared_esp300(self._resource)
            raise
        print(f"[ESP300] {self._role} attached to {self._resource} axis {self._axis}")

    @property
    def address(self) -> str:
        return self._resource

    @property
    def axis(self) -> int:
        return self._axis

    def move_to(self, angle_deg: float) -> None:
        target = float(angle_deg)
        print(f"[ESP300] {self._role} axis {self._axis} move_to {target:g} deg")
        self._controller.move_to(target, axis=self._axis)

    def get_position(self) -> float:
        pos = float(self._controller.get_position(axis=self._axis))
        print(f"[ESP300] {self._role} axis {self._axis} readback -> {pos:g} deg")
        return pos

    def refresh_motion_profile(self):
        self.motion_profile = self._controller.get_motion_profile(axis=self._axis)
        return self.motion_profile

    def apply_fast_safe_motion_profile(
        self, *, velocity_fraction: float = 1.0, acceleration_fraction: float = 0.5
    ):
        self.motion_profile = self._controller.apply_fast_safe_motion_profile(
            axis=self._axis,
            velocity_fraction=velocity_fraction,
            acceleration_fraction=acceleration_fraction,
        )
        return self.motion_profile

    def save_settings(self) -> None:
        self._controller.save_settings()

    def home(self) -> None:
        print(f"[ESP300] {self._role} axis {self._axis} home -> 0 deg")
        self.move_to(0.0)

    def close(self) -> None:
        print(f"[ESP300] {self._role} detaching from {self._resource} axis {self._axis}")
        release_shared_esp300(self._resource)
