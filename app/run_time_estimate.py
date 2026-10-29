"""Observed run timing without Qt, instrument, or measurement-plan dependencies."""

from dataclasses import dataclass
import time
from typing import Callable, Optional


@dataclass(frozen=True)
class RunTimeSnapshot:
    running: bool
    done: int
    total: int
    elapsed_seconds: float
    remaining_seconds: Optional[float]


class RunTimeEstimate:
    """Estimate remaining time from completed units, using a smoothed unit time.

    ``start`` begins a new run; observations never start or finish a run by
    themselves. A changing total retains the learned unit time. A backwards
    counter starts a fresh timing segment, retaining the run's elapsed time.
    ``finish`` freezes elapsed time and clears the ETA without changing counts,
    so the caller can distinguish success, cancellation, and failure.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.reset()

    def reset(self) -> None:
        self._running = False
        self._done = 0
        self._total = 0
        self._started_at = None
        self._ended_at = None
        self._progress_at = None
        self._seconds_per_unit = None

    @staticmethod
    def _counts(done: int, total: int) -> tuple[int, int]:
        total = max(0, int(total))
        done = max(0, int(done))
        return (min(done, total) if total else done), total

    def start(self, done: int = 0, total: int = 0) -> RunTimeSnapshot:
        self.reset()
        self._done, self._total = self._counts(done, total)
        self._started_at = self._progress_at = self._clock()
        self._running = True
        return self.snapshot()

    def observe(self, done: int, total: int) -> RunTimeSnapshot:
        if not self._running:
            return self.snapshot()
        done, total = self._counts(done, total)
        now = self._clock()
        if done < self._done:
            self._seconds_per_unit = None
            self._progress_at = now
        elif done > self._done:
            interval = max(0.0, now - self._progress_at)
            if interval > 0:
                sample = interval / (done - self._done)
                if self._seconds_per_unit is None:
                    self._seconds_per_unit = sample
                else:
                    self._seconds_per_unit += 0.3 * (sample - self._seconds_per_unit)
            self._progress_at = now
        # Duplicate notifications must not shorten the measured interval.
        self._done, self._total = done, total
        return self.snapshot()

    def finish(self) -> RunTimeSnapshot:
        if self._running:
            self._ended_at = self._clock()
            self._running = False
        return self.snapshot()

    def snapshot(self) -> RunTimeSnapshot:
        now = self._clock() if self._running else self._ended_at
        elapsed = (max(0.0, now - self._started_at)
                   if now is not None and self._started_at is not None else 0.0)
        remaining = None
        if self._running and self._total:
            units_left = max(0, self._total - self._done)
            if not units_left:
                remaining = 0.0
            elif self._seconds_per_unit is not None:
                # Keep the last observed pace until another unit completes.
                # A unit taking longer than usual extends the estimate instead
                # of allowing a stalled measurement to count down to zero.
                waiting = max(0.0, now - self._progress_at)
                overdue = max(0.0, waiting - self._seconds_per_unit)
                remaining = units_left * self._seconds_per_unit + overdue
        return RunTimeSnapshot(self._running, self._done, self._total,
                               elapsed, remaining)

    @property
    def remaining_seconds(self) -> Optional[float]:
        return self.snapshot().remaining_seconds

    @property
    def elapsed_seconds(self) -> float:
        return self.snapshot().elapsed_seconds
