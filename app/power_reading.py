"""Shared meter correction. The hardware adapter always returns raw watts."""
from dataclasses import dataclass
import math
import threading

from utils.config import cfg


# PM100D adapters are shared by the sidebar poller, calibration worker, and
# sweep workers.  Serialize the short hardware transactions so a wavelength
# write cannot overlap a read already in progress.
power_reading_lock = threading.RLock()


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
    with power_reading_lock:
        return PowerReading(float(adapter.get_power()), factor)
