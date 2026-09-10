from __future__ import annotations

from app.devices.motion_verification import move_and_verify

from app.devices.esp300_shared import acquire_shared_esp300, release_shared_esp300


class NewportESP300LinearStage:
    """Linear-stage wrapper for a Newport ESP300 controller axis."""

    backend_key = "esp300"
    display_name = "Newport ESP300"
    minimum_position = 0.0
    maximum_position = 50.0
    default_axis = 3
    default_address_kind = "visa"

    def __init__(self, resource: str, *, axis: int = default_axis):
        self._resource = resource
        self._axis = int(axis)
        self.motion_tolerance = 0.01
        self._controller = acquire_shared_esp300(resource)
        try:
            self._controller.motor_on(axis=self._axis)
        except Exception:
            pass
        print(f"[ESP300] linear attached to {self._resource} axis {self._axis}")

    @property
    def address(self) -> str:
        return self._resource

    @property
    def axis(self) -> int:
        return self._axis

    @property
    def position_unit(self) -> str:
        return "mm"

    def validate_position(self, position: float) -> float:
        pos = float(position)
        if not (self.minimum_position <= pos <= self.maximum_position):
            raise ValueError(
                f"{self.display_name} axis {self._axis} position must be between "
                f"{self.minimum_position:g} and {self.maximum_position:g} {self.position_unit}."
            )
        return pos

    def move_to(self, position: float, *, stop_event=None, timeout_s=None) -> bool:
        move_and_verify(self, position, stop_event=stop_event, timeout_s=timeout_s)
        return True

    def _move_to_unverified(self, position: float, *, stop_event=None, timeout_s: float = 60.0) -> bool:
        target = self.validate_position(position)
        print(f"[ESP300] linear axis {self._axis} move_to {target:g} {self.position_unit}")
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
        print(f"[ESP300] linear axis {self._axis} readback -> {pos:g} {self.position_unit}")
        return pos

    def motion_status(self):
        return self._controller.motion_status(axis=self._axis)

    def stop_motion(self) -> None:
        self._controller.stop_motion(axis=self._axis)

    def refresh_motion_profile(self):
        self.motion_profile = self._controller.get_motion_profile(axis=self._axis)
        return self.motion_profile

    def home(self) -> None:
        # The ESP300 axis is treated as a linear stage with 0 as the home position.
        self.move_to(self.minimum_position)

    def scan_axes(self) -> list[int]:
        axes = self._controller.get_axes(conservative=True) or [self._axis]
        return [int(ax) for ax in axes]

    def close(self) -> None:
        print(f"[ESP300] linear detaching from {self._resource} axis {self._axis}")
        release_shared_esp300(self._resource)
