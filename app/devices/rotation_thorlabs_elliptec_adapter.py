from __future__ import annotations
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

    def move_to(self, angle_deg: float) -> None:
        self._drv.move_to(float(angle_deg))
        self._last = float(angle_deg)

    def get_position(self) -> float:
        try:
            return float(self._drv.get_position())
        except Exception:
            return float(self._last)

    def close(self) -> None:
        try:
            self._drv.close()
        except Exception:
            pass
