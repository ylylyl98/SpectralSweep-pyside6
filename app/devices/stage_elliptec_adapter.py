from __future__ import annotations

import time

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

    @property
    def address(self) -> str:
        return self._port

    @property
    def axis(self) -> None:
        return None

    @property
    def position_unit(self) -> str:
        return "stage units"

    def validate_position(self, position: float) -> float:
        pos = float(position)
        if not (self.minimum_position <= pos <= self.maximum_position):
            raise ValueError(
                f"{self.display_name} position must be between "
                f"{self.minimum_position:g} and {self.maximum_position:g}."
            )
        return pos

    def move_to(self, position: float) -> None:
        target = self.validate_position(position)
        self.stage.move_to(target)
        time.sleep(0.5)

    def get_position(self) -> float:
        return float(self.stage.get_position())

    def home(self) -> None:
        if hasattr(self.stage, "home"):
            self.stage.home()
            time.sleep(0.5)
            return
        self.move_to(self.minimum_position)

    def close(self) -> None:
        self.stage.close()
