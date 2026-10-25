"""Observe LightField calls without attempting to cancel or touch the SDK.

A deadline quarantines the connection; it does not end the hardware operation.
Only the operation's owner may unwind it, and only a new setup clears quarantine.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
import traceback
from types import SimpleNamespace
import uuid


_INSTALL_LOCK = threading.Lock()
_DIAGNOSTIC_LOCK = threading.Lock()
_GUARD_ATTR = "_lightfield_capture_guard"
# Local serialization/fsync has its own budget; it must not spend the SDK's
# exposure/readout allowance. This remains bounded even if a disk stops replying.
METADATA_WAIT_TIMEOUT_S = 120.0
_CONTEXT_FIELDS = frozenset({
    "run_id", "experiment_id", "acquisition_id", "point_index", "output_file",
    "Vbg_set", "Vtg_set", "Vbias_set", "measurement_setup", "center_nm",
    "exposure_ms", "accumulations", "purpose", "capture_frames_requested",
})


class LightFieldAcquisitionStalled(TimeoutError):
    """The wait budget expired; this does not confirm hardware cancellation."""


class LightFieldMetadataStalled(LightFieldAcquisitionStalled):
    """Local metadata persistence exceeded its budget, not an SDK operation."""


class LightFieldCaptureOwnershipError(RuntimeError):
    """An operation tried to use a connection owned by another capture."""


def _cached(obj):
    # Do not use getattr: adapters can forward missing attributes to the SDK.
    try:
        return vars(obj)
    except TypeError:
        return {}


def _safe_context(values):
    result = {}
    for key in _CONTEXT_FIELDS.intersection(values):
        value = values[key]
        if value is None or isinstance(value, (bool, int)):
            result[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            result[key] = value
        elif isinstance(value, str):
            result[key] = value[:1024]
        elif isinstance(value, Path):
            result[key] = str(value)[:1024]
    return result


def _context(setup, frames, purpose):
    cached = _cached(setup)
    recipe = cached.get("_acquisition_recipe", {})
    result = _safe_context(recipe if isinstance(recipe, dict) else {})
    if isinstance(recipe, dict) and isinstance(recipe.get("frames"), (int, float)):
        result.update(_safe_context({"accumulations": recipe["frames"]}))
    recorder = cached.get("_metadata_recorder")
    recorder_values = _cached(recorder)
    values = recorder_values.get("context", {})
    if isinstance(values, dict):
        result.update(_safe_context(values))
    # LightFieldRecorder stores a weakref, never an SDK getter.
    import weakref
    reference = recorder_values.get("_run")
    if isinstance(reference, weakref.ReferenceType):
        run_values = _cached(reference())
        run_id = run_values.get("experiment_id")
        if isinstance(run_id, str):
            result.setdefault("run_id", run_id)
            result.setdefault("experiment_id", run_id)
    result.setdefault("purpose", str(purpose)[:128])
    result["capture_frames_requested"] = int(frames)
    return result


def _positive(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite and positive")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be finite and positive") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return number


def _budget(setup, frames):
    cached = _cached(setup)
    frames = _positive(frames, "Capture frame count")
    if frames != int(frames):
        raise ValueError("Capture frame count must be a positive integer")
    override = cached.get("_capture_wait_timeout_s")
    if override is not None:
        return _positive(override, "LightField capture wait budget")
    recipe = cached.get("_acquisition_recipe", {})
    if not isinstance(recipe, dict):
        recipe = {}
    # Missing cached settings get a conservative warning budget. No hardware
    # reads are allowed here, including for externally triggered acquisitions.
    try:
        exposure_ms = _positive(recipe.get("exposure_ms", 0), "Exposure")
        accumulations = _positive(recipe.get("frames", 1), "Accumulations")
    except ValueError:
        return 30.0
    budget = 3.0 * exposure_ms / 1000.0 * accumulations * frames + 15.0
    if not math.isfinite(budget):
        raise ValueError("LightField capture wait budget overflowed")
    return max(30.0, budget)


def _diagnostic_path(setup):
    cached = _cached(setup)
    explicit = cached.get("_capture_diagnostics_path")
    if explicit is not None:
        return Path(explicit)
    if cached.get("auto") is None:
        return None
    root = Path(os.environ.get("APPDATA") or Path.home())
    return root / "SpectralSweep" / "diagnostics" / f"lightfield-capture-{os.getpid()}.jsonl"


def _thread_stacks():
    # Record location only, not source text or locals (which can hold secrets).
    return {
        str(thread_id): [{"file": item.filename, "line": item.lineno, "function": item.name}
                         for item in traceback.extract_stack(frame)]
        for thread_id, frame in sys._current_frames().items()
    }


class CaptureOperation:
    def __init__(self, guard, setup, frames, purpose):
        self._guard = guard
        self.token = str(uuid.uuid4())
        self.owner_thread_id = threading.get_ident()
        self.started = time.monotonic()
        self.started_utc = datetime.now(timezone.utc).isoformat()
        self.timeout_s = _budget(setup, frames)
        cached = _cached(setup)
        recipe = cached.get("_acquisition_recipe", {})
        self._budget_setup = SimpleNamespace(
            _acquisition_recipe=dict(recipe) if isinstance(recipe, dict) else {},
            _capture_wait_timeout_s=cached.get("_capture_wait_timeout_s"),
        )
        self.deadline = self.started + self.timeout_s
        self.metadata_timeout_s = _positive(METADATA_WAIT_TIMEOUT_S, "Metadata wait budget")
        self.metadata_elapsed_s = 0.0
        self._metadata_started = None
        self._metadata_deadline = None
        self._metadata_depth = 0
        self.context = _context(setup, frames, purpose)
        self.phase = "starting"
        self.stalled_phase = None
        self.stalled = False
        self.incident_id = None
        self.reason = None
        self.timeout_domain = None
        self.stalled_timeout_s = None
        self.stalled_wait_elapsed_s = None
        self.ended = None
        self.depth = 1
        self.diagnostic_path = _diagnostic_path(setup)
        self.diagnostic_error = None
        self._done = threading.Event()
        self._wake = threading.Event()
        self._diagnostic_done = threading.Event()

    def stage(self, name, check=True):
        """Record a phase; check=False is reserved for mandatory cleanup."""
        self._guard._stage(self, str(name), check)
        return self

    def check(self):
        self._guard._check(self)

    @contextmanager
    def local_metadata(self, name, *, check=True):
        """Time pure local persistence separately while retaining device ownership.

        No SDK calls may run in this scope. ``check=False`` is only for failure
        records after an error; it never clears or changes an existing incident.
        """
        previous_phase = self._guard._begin_metadata(self, str(name), check)
        try:
            yield
        finally:
            self._guard._end_metadata(self, previous_phase, check)

    def update_context(self, **values):
        with self._guard._lock:
            self._guard._require_owner(self)
            self.context.update(_safe_context(values))

    def set_frames(self, frames):
        """Refine the known request without restarting its absolute budget."""
        budget = _budget(self._budget_setup, frames)
        with self._guard._lock:
            self._guard._require_owner(self)
            expired = self._guard._expire_locked(self, time.monotonic())
            if not self.stalled:
                self.timeout_s = budget
                self.deadline = self.started + budget + self.metadata_elapsed_s
                self.context["capture_frames_requested"] = int(frames)
                expired = self._guard._expire_locked(self, time.monotonic()) or expired
            self._wake.set()
        if expired:
            self._guard._diagnose(self)
        if self.stalled:
            raise self._guard._error(self)

    def quarantine(self, reason):
        """Latch uncertain cleanup without issuing any further SDK commands."""
        with self._guard._lock:
            self._guard._require_owner(self)
            first = self._guard._quarantine_locked(self, str(reason)[:1024])
            self._wake.set()
        if first:
            self._guard._diagnose(self)


class LightFieldCaptureGuard:
    def __init__(self):
        self._lock = threading.RLock()
        self._current = None
        self._active = False
        self._recovery_required = False

    def _error(self, op):
        if op.timeout_domain == "metadata":
            return LightFieldMetadataStalled(
                f"LightField metadata persistence {op.reason} "
                f"({op.stalled_timeout_s:g} s budget; phase: {op.stalled_phase}). "
                "Device ownership is retained until the operation and cleanup return. "
                "This connection requires recovery before another acquisition."
            )
        return LightFieldAcquisitionStalled(
            f"LightField acquisition {op.reason or 'wait budget exceeded'} ({op.timeout_s:g} s budget; "
            f"phase: {op.stalled_phase}). Hardware stop is not confirmed. "
            "This connection requires recovery before another acquisition."
        )

    def _require_owner(self, op):
        if self._current is not op or not self._active:
            raise LightFieldCaptureOwnershipError("LightField capture token is no longer active")
        if op.owner_thread_id != threading.get_ident():
            raise LightFieldCaptureOwnershipError("LightField capture belongs to another thread")

    def _deadline_locked(self, op):
        return op._metadata_deadline if op._metadata_depth else op.deadline

    def _metadata_elapsed_locked(self, op, now):
        return op.metadata_elapsed_s + (
            max(0.0, now - op._metadata_started) if op._metadata_depth else 0.0)

    def _expire_locked(self, op, now):
        if (self._current is not op or not self._active or op.stalled
                or now < self._deadline_locked(op)):
            return False
        domain = "metadata" if op._metadata_depth else "sdk"
        return self._quarantine_locked(op, "wait budget exceeded", domain=domain, now=now)

    def _quarantine_locked(self, op, reason, *, domain="sdk", now=None):
        if self._current is not op or not self._active or op.stalled:
            return False
        op.stalled = True
        op.stalled_phase = op.phase
        op.incident_id = str(uuid.uuid4())
        op.reason = reason
        op.timeout_domain = domain
        op.stalled_timeout_s = op.metadata_timeout_s if domain == "metadata" else op.timeout_s
        now = time.monotonic() if now is None else now
        op.stalled_wait_elapsed_s = (
            max(0.0, now - op._metadata_started) if domain == "metadata"
            else max(0.0, now - op.started - self._metadata_elapsed_locked(op, now)))
        self._recovery_required = True
        return True

    def _begin_metadata(self, op, name, check):
        with self._lock:
            self._require_owner(op)
            now = time.monotonic()
            # Check before switching domains: local work cannot rescue a late SDK.
            expired = self._expire_locked(op, now)
            stalled = op.stalled
            previous_phase = op.phase
            if not stalled or not check:
                if not op._metadata_depth:
                    op._metadata_started = now
                    op._metadata_deadline = now + op.metadata_timeout_s
                op._metadata_depth += 1
                op.phase = name
                op._wake.set()
        if expired:
            self._diagnose(op)
        if stalled and check:
            raise self._error(op)
        return previous_phase

    def _end_metadata(self, op, previous_phase, check):
        with self._lock:
            self._require_owner(op)
            now = time.monotonic()
            expired = self._expire_locked(op, now)
            op._metadata_depth -= 1
            if not op._metadata_depth:
                elapsed = max(0.0, now - op._metadata_started)
                op.metadata_elapsed_s += elapsed
                op.deadline += elapsed
                op._metadata_started = op._metadata_deadline = None
            op.phase = previous_phase
            stalled = op.stalled
            op._wake.set()
        if expired:
            self._diagnose(op)
        if stalled and check:
            raise self._error(op)

    def _diagnose(self, op):
        # Never hold the state lock during filesystem access. Quarantine is
        # already latched even if diagnostics cannot be written.
        try:
            stacks = _thread_stacks()
        except Exception as exc:
            stacks = {"unavailable": type(exc).__name__}
        payload = self.snapshot()
        payload.update(event="lightfield_capture_stalled", started_utc=op.started_utc,
                       utc=datetime.now(timezone.utc).isoformat(), pid=os.getpid(),
                       operation_token=op.token, owner_thread_id=op.owner_thread_id,
                       thread_stacks=stacks)

        def write():
            try:
                if op.diagnostic_path is not None:
                    with _DIAGNOSTIC_LOCK:
                        op.diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
                        with op.diagnostic_path.open("a", encoding="utf-8") as stream:
                            stream.write(json.dumps(payload, allow_nan=False) + "\n")
                            stream.flush()
                            os.fsync(stream.fileno())
            except Exception as exc:
                with self._lock:
                    op.diagnostic_error = f"{type(exc).__name__}: {exc}"[:1024]
            finally:
                op._diagnostic_done.set()

        try:
            threading.Thread(target=write, name="LightField capture diagnostics", daemon=True).start()
        except Exception as exc:
            with self._lock:
                op.diagnostic_error = f"{type(exc).__name__}: {exc}"[:1024]
            op._diagnostic_done.set()

    def _watch(self, op):
        while True:
            with self._lock:
                if self._current is not op or not self._active:
                    return
                expired = self._expire_locked(op, time.monotonic())
                stalled = op.stalled
                wait_s = max(0.0, self._deadline_locked(op) - time.monotonic())
                # Clear while holding the same lock as domain changes, so no
                # wakeup announcing a shorter/new deadline can be lost.
                op._wake.clear()
            if expired:
                self._diagnose(op)
            if stalled:
                return
            op._wake.wait(wait_s)
            if op._done.is_set():
                return

    def _enter(self, setup, frames, purpose):
        entered = False
        with self._lock:
            if self._recovery_required:
                raise self._error(self._current)
            if self._active:
                self._require_owner(self._current)
                op = self._current
                expired = self._expire_locked(op, time.monotonic())
                if not op.stalled:
                    op.depth += 1
                    entered = True
            else:
                op = CaptureOperation(self, setup, frames, purpose)
                self._current = op
                self._active = True
                entered = True
                expired = False
                try:
                    threading.Thread(target=self._watch, args=(op,),
                                     name="LightField capture monitor", daemon=True).start()
                except BaseException:
                    self._active = False
                    op._done.set()
                    op._wake.set()
                    raise
        if expired:
            self._diagnose(op)
        if op.stalled:
            if entered:
                self._leave(op)
            raise self._error(op)
        return op

    def _stage(self, op, name, check):
        with self._lock:
            self._require_owner(op)
            if op._metadata_depth:
                raise LightFieldCaptureOwnershipError("SDK stages are prohibited during local metadata persistence")
            # Attribute the deadline to the phase that was active before this
            # boundary, even if cleanup is now starting.
            expired = self._expire_locked(op, time.monotonic())
            if not op.stalled or not check:
                op.phase = name
            stalled = op.stalled
        if expired:
            self._diagnose(op)
        if stalled and check:
            raise self._error(op)

    def _check(self, op):
        with self._lock:
            self._require_owner(op)
            expired = self._expire_locked(op, time.monotonic())
            stalled = op.stalled
        if expired:
            self._diagnose(op)
        if stalled:
            raise self._error(op)

    def _release_locked(self, op, now):
        op.depth -= 1
        if op.depth == 0:
            op.ended = now
            self._active = False
            op._done.set()
            op._wake.set()

    def _leave(self, op, on_stalled_exit=None):
        with self._lock:
            self._require_owner(op)
            now = time.monotonic()
            expired = self._expire_locked(op, now)
            stalled = op.stalled
            if not stalled:
                # Deadline check and successful handoff are one atomic step.
                self._release_locked(op, now)
                return
        if expired:
            self._diagnose(op)
        try:
            # A raw dataset may still belong to this scope when its final
            # handoff check fails. Dispose it before relinquishing ownership.
            if on_stalled_exit is not None:
                on_stalled_exit(op)
        except BaseException as exc:
            raise self._error(op) from exc
        finally:
            with self._lock:
                self._require_owner(op)
                self._release_locked(op, time.monotonic())
        raise self._error(op)

    def snapshot(self):
        with self._lock:
            op = self._current
            if op is None:
                return {
                    "active": False, "stalled": False, "recovery_required": False,
                    "incident_id": None, "phase": None, "stalled_phase": None,
                    "elapsed_s": 0.0, "timeout_s": None, "context": {},
                    "operation_token": None, "owner_thread_id": None,
                    "diagnostic_path": None, "diagnostic_error": None, "reason": None,
                    "timeout_domain": None, "active_domain": None, "wait_elapsed_s": 0.0,
                    "sdk_elapsed_s": 0.0, "metadata_elapsed_s": 0.0,
                    "sdk_timeout_s": None, "metadata_timeout_s": None,
                }
            now = op.ended if op.ended is not None else time.monotonic()
            elapsed = max(0.0, now - op.started)
            metadata_elapsed = self._metadata_elapsed_locked(op, now)
            sdk_elapsed = max(0.0, elapsed - metadata_elapsed)
            wait_elapsed = (max(0.0, now - op._metadata_started)
                            if op._metadata_depth else sdk_elapsed)
            return {
                "active": self._active, "stalled": op.stalled,
                "recovery_required": self._recovery_required,
                "incident_id": op.incident_id, "phase": op.phase,
                "stalled_phase": op.stalled_phase,
                "elapsed_s": elapsed,
                "timeout_s": (op.stalled_timeout_s if op.stalled else
                              op.metadata_timeout_s if op._metadata_depth else op.timeout_s),
                "timeout_domain": op.timeout_domain,
                "active_domain": "metadata" if op._metadata_depth else "sdk",
                "wait_elapsed_s": op.stalled_wait_elapsed_s if op.stalled else wait_elapsed,
                "sdk_elapsed_s": sdk_elapsed, "metadata_elapsed_s": metadata_elapsed,
                "sdk_timeout_s": op.timeout_s, "metadata_timeout_s": op.metadata_timeout_s,
                "context": dict(op.context),
                "operation_token": op.token, "owner_thread_id": op.owner_thread_id,
                "diagnostic_path": str(op.diagnostic_path) if op.diagnostic_path is not None else None,
                "diagnostic_error": op.diagnostic_error,
                "reason": op.reason,
            }

    def check_available(self):
        """Guard settings/connection calls, allowing the current owner only."""
        with self._lock:
            if self._recovery_required:
                raise self._error(self._current)
            op = self._current if self._active else None
            if op is not None:
                self._require_owner(op)
                if op._metadata_depth:
                    raise LightFieldCaptureOwnershipError("SDK access is prohibited during local metadata persistence")
                expired = self._expire_locked(op, time.monotonic())
            else:
                expired = False
        if expired:
            self._diagnose(op)
        if op is not None and op.stalled:
            raise self._error(op)

    def check_close(self):
        """Only an entirely drained connection may be disposed, even by its owner."""
        with self._lock:
            if self._active:
                raise LightFieldCaptureOwnershipError(
                    "Cannot close LightField while its acquisition operation is still active")


def get_capture_guard(setup):
    """Install a guard on a setup without inspecting any SDK properties."""
    with _INSTALL_LOCK:
        guard = _cached(setup).get(_GUARD_ATTR)
        if guard is None:
            guard = LightFieldCaptureGuard()
            setattr(setup, _GUARD_ATTR, guard)
        return guard


def capture_status(setup):
    """Read cached state; calling this never creates a monitor or reads hardware."""
    guard = _cached(setup).get(_GUARD_ATTR)
    return guard.snapshot() if isinstance(guard, LightFieldCaptureGuard) else {}


@contextmanager
def capture_operation(setup, frames=1, purpose="measurement", *, on_stalled_exit=None):
    """Keep ownership through cleanup, including a rejected raw-data handoff.

    on_stalled_exit(op), if supplied, must idempotently release any result still
    owned by this scope. It runs while ownership is active, including when only
    the final exit check notices the deadline. It never runs on a healthy exit.
    """
    guard = get_capture_guard(setup)
    op = guard._enter(setup, frames, purpose)
    try:
        yield op
    except BaseException as original:
        try:
            guard._leave(op, on_stalled_exit)
        except LightFieldAcquisitionStalled as stalled:
            if isinstance(original, LightFieldAcquisitionStalled):
                raise original
            raise stalled from original
        raise
    else:
        guard._leave(op, on_stalled_exit)
