from __future__ import annotations

from app.devices.motion_verification import move_and_verify
import pylablib as pll
from pylablib.devices import Thorlabs

class ElliptecRotation:
    """
    Thin wrapper over Thorlabs Elliptec motor API.
    Example user code you gave: Thorlabs.ElliptecMotor("COM4")
    """
    def __init__(self, com_port: str):
        # Import here so app can start even if DLL/lib not present
        self._drv = Thorlabs.ElliptecMotor(com_port)
        self._last = 0.0
        self.backend_key = "elliptec"
        self.position_unit = "deg"
        self.motion_tolerance = 0.25

    def move_to(self, angle_deg: float, *, stop_event=None, timeout_s=None) -> bool:
        move_and_verify(self, angle_deg, stop_event=stop_event, timeout_s=timeout_s)
        return True

    def _move_to_unverified(self, angle_deg: float, *, stop_event=None, timeout_s: float = 60.0) -> bool:
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("motion cancelled")
        result = self._drv.move_to(float(angle_deg), timeout=float(timeout_s))
        # Elliptec returns False for a mechanical timeout.  Do not update the
        # compatibility cache until the command has positively succeeded.
        if result is False:
            raise RuntimeError(f"Elliptec move to {float(angle_deg):g} deg failed")
        self._last = float(angle_deg)
        return True

    def get_position(self) -> float:
        return self.get_position_strict()

    def get_position_strict(self) -> float:
        """Return controller readback, propagating communication failures.

        Both read APIs propagate failures so the sidebar and measurement
        metadata cannot mistake a cached target for a measured position.
        """
        return float(self._drv.get_position())

    def stop_motion(self) -> None:
        stop = getattr(self._drv, "stop", None)
        if callable(stop):
            stop()

    def close(self) -> None:
        try:
            self._drv.close()
        except Exception:
            pass
