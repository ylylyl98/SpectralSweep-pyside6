from __future__ import annotations

from app.devices.motion_verification import (
    ELLIPTEC_HARD_FAULT_STATUSES,
    MotionHardwareFault,
    move_and_verify,
)

import time
import math

from pylablib.devices import Thorlabs


class ElliptecLinearStage:
    """Adapter for the Thorlabs Elliptec linear stage."""

    backend_key = "elliptec"
    display_name = "Thorlabs Elliptec"
    minimum_position = 0.0
    maximum_position = 3600.0
    default_address_kind = "com"

    def __init__(self, port: str):
        self._port = port
        self.stage = Thorlabs.ElliptecMotor(port)
        self.motion_tolerance = 1.0

    @property
    def address(self) -> str:
        return self._port

    @property
    def axis(self) -> None:
        return None

    @property
    def position_unit(self) -> str:
        return "stage units"

    @property
    def motion_status_available(self) -> bool:
        return callable(getattr(self.stage, "get_status", None))

    def motion_status(self):
        """Return explicit stopped proof from the Elliptec GS protocol status."""
        reader = getattr(self.stage, "get_status", None)
        if not callable(reader):
            return None
        try:
            status = reader()
        except Exception:
            return None
        normalized = str(status).strip().lower()
        if normalized == "ok":
            return True
        if normalized in ELLIPTEC_HARD_FAULT_STATUSES:
            raise MotionHardwareFault(
                f"Elliptec reported hardware fault status {normalized!r}"
            )
        if normalized == "busy":
            return False
        if normalized == "comm_timeout":
            return None
        return None

    def validate_position(self, position: float) -> float:
        pos = float(position)
        if not (self.minimum_position <= pos <= self.maximum_position):
            raise ValueError(
                f"{self.display_name} position must be between "
                f"{self.minimum_position:g} and {self.maximum_position:g} (requested {pos:.12g})."
            )
        return pos

    def normalize_restore_position(self, position: float) -> float:
        """Use the nearest allowed restore target for any finite readback.

        The worker logs and records the original reading separately. Normal
        sweep commands still pass through strict validate_position checks.
        """
        pos = float(position)
        if math.isfinite(pos):
            return min(max(pos, self.minimum_position), self.maximum_position)
        return self.validate_position(pos)

    def move_to(self, position: float, *, stop_event=None, timeout_s=None) -> bool:
        move_and_verify(self, position, stop_event=stop_event, timeout_s=timeout_s)
        return True

    def _move_to_unverified(self, position: float, *, stop_event=None, timeout_s: float = 60.0) -> bool:
        target = self.validate_position(position)
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("motion cancelled")
        result = self.stage.move_to(target, timeout=float(timeout_s))
        if result is False:
            raise RuntimeError(f"Elliptec move to {target:g} failed")
        return True

    def get_position(self) -> float:
        return float(self.stage.get_position())

    def get_position_strict(self) -> float:
        return float(self.stage.get_position())

    def stop_motion(self) -> None:
        stop = getattr(self.stage, "stop", None)
        if callable(stop):
            stop()

    def home(self) -> None:
        if hasattr(self.stage, "home"):
            self.stage.home()
            return
        self.move_to(self.minimum_position)

    def close(self) -> None:
        self.stage.close()
