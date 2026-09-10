"""Shared, conservative motion completion and readback verification.

Adapters use this policy by default and expose a private raw command hook.
This module owns completion verification so a move cannot be reported complete from
an optimistic cache or a single transient read.
"""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import math
import time
from typing import Any, Callable, Optional


class MotionVerificationError(RuntimeError):
    """The device did not prove that the requested position was reached."""


class MotionCancelledError(MotionVerificationError):
    """The caller requested cancellation while a move was in progress."""


@dataclass(frozen=True)
class MotionVerificationConfig:
    """Conservative defaults shared by stage and rotation callers."""

    poll_interval_s: float = 0.10
    read_retries: int = 3
    read_retry_delay_s: float = 0.10
    stable_readings: int = 2
    settling_s: float = 0.10
    max_corrections: int = 1
    default_timeout_s: float = 60.0
    timeout_margin_s: float = 5.0


DEFAULT_MOTION_CONFIG = MotionVerificationConfig()


def _profile_value(adapter: Any, *names: str) -> Optional[float]:
    profile = getattr(adapter, "motion_profile", None)
    for source in (profile, adapter):
        for name in names:
            try:
                value = float(getattr(source, name))
            except (AttributeError, TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0:
                return value
    return None


def motion_tolerance(adapter: Any, *, fallback: Optional[float] = None) -> float:
    """Return an adapter-declared tolerance, preserving conservative units."""
    if fallback is not None:
        return abs(float(fallback))
    for name in ("motion_tolerance", "position_tolerance"):
        try:
            value = float(getattr(adapter, name))
        except (AttributeError, TypeError, ValueError):
            continue
        if math.isfinite(value) and value >= 0:
            return value
    # Keep the historical application tolerances for unknown adapters.
    unit = str(getattr(adapter, "position_unit", "")).lower()
    return 0.01 if unit in {"mm", "stage units"} else 0.25


def motion_timeout_s(
    adapter: Any,
    distance: Optional[float],
    *,
    config: MotionVerificationConfig = DEFAULT_MOTION_CONFIG,
) -> float:
    """Derive a bounded deadline from configured speed and acceleration.

    A configured adapter timeout wins.  Otherwise the estimate includes two
    acceleration periods and a settling/read margin.  Unknown profiles use a
    finite conservative default rather than waiting forever.
    """
    try:
        configured = float(getattr(adapter, "motion_timeout_s"))
    except (AttributeError, TypeError, ValueError):
        configured = 0.0
    if math.isfinite(configured) and configured > 0:
        return max(configured, config.timeout_margin_s)

    # Hardware maxima are limits, not the configured operating speed.
    speed = _profile_value(adapter, "velocity", "speed")
    acceleration = _profile_value(adapter, "acceleration")
    deceleration = _profile_value(adapter, "deceleration")
    if distance is None or not math.isfinite(distance) or speed is None:
        estimate = config.default_timeout_s
    else:
        estimate = abs(float(distance)) / speed
        if acceleration is not None:
            estimate += speed / acceleration
        if deceleration is not None:
            estimate += speed / deceleration
        elif acceleration is not None:
            estimate += speed / acceleration
        estimate += config.timeout_margin_s + config.settling_s
        # Allow controller polling and conservative profile variation.
        estimate = max(estimate * 2.0, config.default_timeout_s)
    return max(float(estimate), config.timeout_margin_s)


def _strict_position(adapter: Any) -> float:
    reader = getattr(adapter, "get_position_strict", None)
    if not callable(reader):
        reader = getattr(adapter, "get_position", None)
    if not callable(reader):
        raise MotionVerificationError("adapter has no position readback")
    value = float(reader())
    if not math.isfinite(value):
        raise MotionVerificationError(f"position readback is nonfinite: {value!r}")
    return value


def _status_reader(adapter: Any) -> Optional[Callable[[], Optional[bool]]]:
    for name in ("motion_status", "get_motion_status"):
        reader = getattr(adapter, name, None)
        if callable(reader):
            return reader
    reader = getattr(adapter, "is_motion_done", None)
    return reader if callable(reader) else None


def _status_value(reader: Callable[[], Any]) -> Optional[bool]:
    try:
        value = reader()
    except Exception:
        return None
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    try:
        numeric = float(value)
        return bool(numeric) if numeric in (0.0, 1.0) else None
    except (TypeError, ValueError):
        return None


def _call_move(adapter: Any, target: float, stop_event: Any, timeout_s: float) -> Any:
    move = getattr(adapter, "_move_to_unverified", None)
    if not callable(move):
        move = getattr(adapter, "move_to", None)
    if not callable(move):
        raise MotionVerificationError("adapter has no move_to operation")
    if stop_event is not None:
        try:
            params = inspect.signature(move).parameters
        except (TypeError, ValueError):
            params = {}
        kwargs = {}
        if "stop_event" in params:
            kwargs["stop_event"] = stop_event
        if "timeout_s" in params:
            kwargs["timeout_s"] = max(0.001, float(timeout_s))
        if kwargs:
            return move(target, **kwargs)
    else:
        try:
            params = inspect.signature(move).parameters
        except (TypeError, ValueError):
            params = {}
        if "timeout_s" in params:
            return move(target, timeout_s=max(0.001, float(timeout_s)))
    return move(target)


def _cancel(adapter: Any) -> None:
    for name in ("stop_motion", "stop", "abort_move"):
        callback = getattr(adapter, name, None)
        if callable(callback):
            try:
                callback()
            except Exception:
                pass
            return


def move_and_verify(
    adapter: Any,
    target: float,
    *,
    tolerance: Optional[float] = None,
    stop_event: Any = None,
    timeout_s: Optional[float] = None,
    issue_move: bool = True,
    config: MotionVerificationConfig = DEFAULT_MOTION_CONFIG,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> float:
    """Verify a move under one deadline, with at most one stopped correction.

    Hardware adapters call this from their public move_to and expose their raw
    command privately, so direct callers and helper callers share one policy.
    Failed moves remain latched until a stopped status is explicitly observed;
    cleanup must not silently start another move while motion is uncertain.
    """
    target = float(target)
    if not math.isfinite(target):
        raise MotionVerificationError(f"invalid motion target: {target!r}")
    tolerance = motion_tolerance(adapter, fallback=tolerance)
    if not math.isfinite(tolerance):
        raise MotionVerificationError(f"invalid motion tolerance: {tolerance!r}")
    status_reader = _status_reader(adapter)
    if stop_event is not None and stop_event.is_set():
        raise MotionCancelledError("motion cancelled before command")
    if getattr(adapter, "_motion_uncertain", False):
        if status_reader is None or _status_value(status_reader) is not True:
            raise MotionVerificationError("previous motion is unconfirmed; no new move issued")

    refresh = getattr(adapter, "refresh_motion_profile", None)
    if callable(refresh):
        try:
            refresh()
        except Exception:
            # Do not estimate from an old operating speed after a failed refresh.
            adapter.motion_profile = None
    try:
        initial = _strict_position(adapter)
    except Exception:
        initial = None
    timeout_budget = (motion_timeout_s(adapter, abs(target - initial) if initial is not None else None,
                                       config=config) if timeout_s is None else float(timeout_s))
    if not math.isfinite(timeout_budget) or timeout_budget <= 0:
        raise MotionVerificationError("motion timeout must be finite and positive")
    deadline = clock() + timeout_budget

    def check_deadline():
        if stop_event is not None and stop_event.is_set():
            _cancel(adapter)
            raise MotionCancelledError("motion cancelled")
        if clock() >= deadline:
            _cancel(adapter)
            raise MotionVerificationError(
                f"move to {target:g} timed out after {timeout_budget:g}s (remained in motion, unknown or still moving, or unstable readback)"
            )

    def pause(duration):
        check_deadline()
        sleep(min(max(float(duration), 0.001), max(0.0, deadline - clock())))
        check_deadline()

    corrections = 0
    required_stable = max(1, int(config.stable_readings))
    adapter._motion_uncertain = True
    try:
        while True:
            check_deadline()
            result = _call_move(adapter, target, stop_event, deadline - clock()) if issue_move else True
            check_deadline()
            if result is False:
                raise MotionVerificationError(f"adapter reported move to {target:g} was not completed")

            stable = 0
            short_stable = 0
            previous = None
            read_failures = 0
            settled_since = None
            actual = None
            while True:
                check_deadline()
                # Refresh status for every sample. Unknown status is not proof
                # of completion when a device offers a status query.
                status = _status_value(status_reader) if status_reader else None
                check_deadline()
                if status_reader is not None and status is not True:
                    stable = short_stable = 0
                    previous = None
                    settled_since = None
                    pause(config.poll_interval_s)
                    continue
                if settled_since is None:
                    settled_since = clock()
                if clock() - settled_since < config.settling_s:
                    pause(min(config.poll_interval_s, config.settling_s - (clock() - settled_since)))
                    continue
                try:
                    sample = _strict_position(adapter)
                except Exception as exc:
                    stable = short_stable = 0
                    previous = None
                    read_failures += 1
                    if read_failures > max(0, int(config.read_retries)):
                        raise MotionVerificationError(f"move to {target:g} has no genuine readback: {exc}") from exc
                    pause(config.read_retry_delay_s)
                    continue
                check_deadline()
                read_failures = 0
                actual = sample
                same = previous is None or abs(sample - previous) <= tolerance
                if abs(sample - target) <= tolerance:
                    stable = stable + 1 if same else 1
                    short_stable = 0
                else:
                    stable = 0
                    short_stable = short_stable + 1 if same else 1
                previous = sample
                if stable >= required_stable:
                    # Position queries can outlast a status query: prove stopped
                    # once more before success or issuing a corrective command.
                    if status_reader is None or _status_value(status_reader) is True:
                        check_deadline()
                        adapter._motion_uncertain = False
                        return sample
                    stable = short_stable = 0
                    settled_since = None
                if short_stable >= required_stable and status_reader is None:
                    raise MotionVerificationError(
                        f"move to {target:g} readback {actual:g} differs by {abs(actual-target):g} "
                        f"(tolerance {tolerance:g}, device status unknown; no correction permitted)"
                    )
                if short_stable >= required_stable and status is True:
                    if corrections < max(0, int(config.max_corrections)):
                        if _status_value(status_reader) is True:
                            check_deadline()
                            corrections += 1
                            issue_move = True
                            break
                        short_stable = 0
                    else:
                        raise MotionVerificationError(
                            f"move to {target:g} readback {actual:g} differs by {abs(actual-target):g} "
                            f"(tolerance {tolerance:g}, device status stopped)"
                        )
                pause(config.read_retry_delay_s)
    except Exception:
        # Keep the failure latch even if a stop command was sent. Sending stop
        # is not evidence that the hardware actually stopped.
        raise
