from __future__ import annotations

from app.devices.motion_verification import move_and_verify

from app.devices.esp300_shared import acquire_shared_esp300, release_shared_esp300


class SharedESP300Rotation:
    """Rotation adapter that reuses a shared ESP300/ESP301 controller session."""

    backend_key = "esp300"
    display_name = "Newport ESP300"

    def __init__(
        self,
        resource: str,
        *,
        axis: int = 1,
        role: str = "rotation",
        model: str = "esp300",
        baud: int | None = None,
        velocity_fraction: float = 1.0,
        acceleration_fraction: float = 0.5,
    ):
        self._resource = resource
        self._axis = int(axis)
        self._role = role
        self.motion_tolerance = 0.25
        self._closed = True
        self._controller = acquire_shared_esp300(resource, model=model, baud=baud)
        self.backend_key = str(model).strip().lower()
        self.display_name = f"Newport {self.backend_key.upper()}"
        try:
            axes = self._controller.get_axes(conservative=True)
            if self._axis not in axes:
                raise ValueError(
                    f"{self.display_name} axis {self._axis} was not detected on {resource}; "
                    f"detected axes: {axes}"
                )
            self.motion_profile = self._controller.apply_fast_safe_motion_profile(
                axis=self._axis,
                velocity_fraction=velocity_fraction,
                acceleration_fraction=acceleration_fraction,
            )
            self._controller.motor_on(axis=self._axis)
        except Exception:
            release_shared_esp300(self._resource)
            raise
        self._closed = False
        print(f"[{self.display_name}] {self._role} attached to {self._resource} axis {self._axis}")

    @property
    def address(self) -> str:
        return self._resource

    @property
    def axis(self) -> int:
        return self._axis

    def move_to(self, angle_deg: float, *, stop_event=None, timeout_s=None) -> bool:
        move_and_verify(self, angle_deg, stop_event=stop_event, timeout_s=timeout_s)
        return True

    def _move_to_unverified(self, angle_deg: float, *, stop_event=None, timeout_s: float = 60.0) -> bool:
        target = float(angle_deg)
        print(f"[{self.display_name}] {self._role} axis {self._axis} move_to {target:g} deg")
        return bool(
            self._controller.move_to(
                target,
                axis=self._axis,
                stop_event=stop_event,
                timeout_s=float(timeout_s),
            )
        )

    def get_position(self) -> float:
        pos = float(self._controller.get_position(axis=self._axis))
        print(f"[{self.display_name}] {self._role} axis {self._axis} readback -> {pos:g} deg")
        return pos

    def motion_status(self):
        status = self._controller.motion_status(axis=self._axis)
        # Generic idle status is also used before connect enables the motor.
        # The rotation verifier, however, must reject power loss after a move.
        self._controller.require_motor_enabled(axis=self._axis)
        return status

    def stop_motion(self) -> None:
        self._controller.stop_motion(axis=self._axis)

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
        print(f"[{self.display_name}] {self._role} axis {self._axis} home -> 0 deg")
        self.move_to(0.0)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        print(f"[{self.display_name}] {self._role} detaching from {self._resource} axis {self._axis}")
        release_shared_esp300(self._resource)
