"""Read-only temperature policy shared with the Python 2.7 XP bridge.

Never writes cooling settings. A trip attempts Stop and invalidates the frame.
"""
from __future__ import print_function

import math
import time

LIMIT_C = -100.0
POLL_SECONDS = 0.5
MAX_GAP_SECONDS = 3.0
VERSION = 4


def validate_temperature(settings):
    value = settings.get('actual_temperature_c')
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value) or math.isinf(value):
        raise RuntimeError('WinSpec temperature interlock: fresh temperature unavailable; acquisition prohibited')
    if value > LIMIT_C and settings.get('temperature_locked') is not True:
        raise RuntimeError('WinSpec temperature interlock: temperature above -100 C and not locked; acquisition prohibited')
    return float(value)


class TemperatureGuard(object):
    def __init__(self, read, stop, clock=None, boundary_only=False):
        self.read, self.stop = read, stop
        self.boundary_only = boundary_only
        # Python 2 time.clock is a monotonic wall clock on Windows.
        self.clock = clock or getattr(time, 'monotonic', None) or time.clock
        self.last = None
        self.count = 0
        self.minimum = self.maximum = None
        self.error = None
        self.max_gap = 0.0
        self.last_settings = None

    def trip(self, message):
        self.error = 'WinSpec temperature interlock: ' + str(message)
        try:
            self.stop()
        except Exception as exc:
            self.error += '; Stop failed: ' + str(exc)
        raise RuntimeError(self.error)

    def check(self):
        if self.error:
            raise RuntimeError(self.error)
        started = self.clock()
        try:
            settings = self.read()
            value = validate_temperature(settings)
        except Exception as exc:
            self.trip(str(exc))
        now = self.clock()
        gap = now - (self.last if self.last is not None and not self.boundary_only else started)
        if gap > MAX_GAP_SECONDS:
            self.trip('temperature read gap exceeded %.1f s' % MAX_GAP_SECONDS)
        self.max_gap = max(self.max_gap, gap)
        self.last = now
        self.last_settings = dict(settings)
        self.count += 1
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        self.checked_at = time.time()
        return value

    def report(self):
        if self.error:
            raise RuntimeError(self.error)
        if self.last is None or self.clock() - self.last > MAX_GAP_SECONDS:
            self.trip('temperature monitor is stale')
        return dict(version=VERSION, policy="cold_or_locked", monitoring_mode="before_after" if self.boundary_only else "continuous", limit_c=LIMIT_C, passed=True,
                    sample_count=self.count, minimum_c=self.minimum,
                    maximum_c=self.maximum, last_checked_unix=self.checked_at,
                    max_gap_s=self.max_gap, poll_interval_s=POLL_SECONDS)
