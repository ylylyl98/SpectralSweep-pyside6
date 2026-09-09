"""Shared meter correction. The hardware adapter always returns raw watts."""
from dataclasses import dataclass
import math

from utils.config import cfg


def power_correction_factor(value=None):
    factor = float(cfg.pm100d.correction_factor if value is None else value)
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError("Power correction factor must be finite and positive")
    return factor


@dataclass(frozen=True)
class PowerReading:
    raw_w: float
    correction_factor: float

    @property
    def corrected_w(self):
        return self.raw_w * self.correction_factor

    def csv_values(self):
        return {"Power_raw_uW": self.raw_w * 1e6,
                "Power_correction_factor": self.correction_factor,
                "Power_uW": self.corrected_w * 1e6}


def read_power(adapter, *, factor=None):
    # Capture the factor before the hardware read; callers can freeze it per run.
    factor = power_correction_factor(factor)
    return PowerReading(float(adapter.get_power()), factor)
