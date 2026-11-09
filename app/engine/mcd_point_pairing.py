"""Hardware-independent field grid and validation for paired MCD acquisition."""
from __future__ import annotations

import math
from decimal import Decimal


def field_points(start: float, end: float, step_t: float) -> list[float]:
    """Include both endpoints, including a shorter final step in either direction."""
    if not all(math.isfinite(v) for v in (start, end, step_t)) or step_t <= 0:
        raise ValueError("field endpoints must be finite and step must be positive")
    first, last, step = map(lambda v: Decimal(str(v)), (start, end, step_t))
    count = int(abs(last - first) // step)
    if count > 100_000:
        raise ValueError("field grid exceeds 100000 intervals; increase the step")
    direction = 1 if last >= first else -1
    points = [first + direction * step * i for i in range(count + 1)]
    if points[-1] != last:
        points.append(last)
    return [float(v) for v in points]


class FieldStability:
    """Track consecutive in-tolerance samples with a monotonic clock supplied by caller."""

    def __init__(self, tolerance_t: float = 0.010, hold_s: float = 1.0):
        if not math.isfinite(tolerance_t) or tolerance_t <= 0:
            raise ValueError("field tolerance must be finite and positive")
        if not math.isfinite(hold_s) or hold_s < 0:
            raise ValueError("stable hold duration must be finite and nonnegative")
        self.tolerance_t = tolerance_t
        self.hold_s = hold_s
        self.reset()

    def reset(self) -> None:
        self._since = None
        self._target = None
        self._last_time = None

    def observe(self, field_t: float, target_t: float, now: float) -> bool:
        if not all(math.isfinite(v) for v in (field_t, target_t, now)):
            self.reset()
            return False
        if self._target != target_t or (self._last_time is not None and now < self._last_time):
            self.reset()
        self._target, self._last_time = target_t, now
        if abs(field_t - target_t) >= self.tolerance_t:
            self._since = None
            return False
        if self._since is None:
            self._since = now
        return now - self._since >= self.hold_s


def validate_pair_fields(fields, target_t: float, tolerance_t: float = 0.010) -> bool:
    """Require all exposure endpoints near target and within one tolerance of each other."""
    values = list(fields)
    if (not values or not math.isfinite(target_t) or not math.isfinite(tolerance_t)
            or tolerance_t <= 0 or not all(math.isfinite(v) for v in values)):
        return False
    return (all(abs(v - target_t) < tolerance_t for v in values)
            and max(values) - min(values) < tolerance_t)
