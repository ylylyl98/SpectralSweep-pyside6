"""Shared, conservative attoDRY2100 magnet preparation workflow."""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable, Mapping, Optional


class MagnetPreparationCancelled(RuntimeError):
    pass


class MagnetPreparationTimeout(RuntimeError):
    pass


def preparation_mode_observation(snapshot):
    status = getattr(snapshot, "status", None)
    return {key: getattr(status, key, None) for key in (
        "driven_mode", "persistent_mode", "heater_on", "leads_hot"
    )} | {"lead_field_t": getattr(snapshot, "lead_field_t", None)}


def format_preparation_progress(progress, now):
    """Render elapsed time locally; a timer must never refresh a sample's age."""
    field = progress.get("field_t")
    sampled_at = progress.get("sampled_at")
    target = progress["target_t"]
    value = "N/A" if field is None else f"{field:+.6g}"
    error = "N/A" if field is None else f"{field - target:+.6g}"
    age = "N/A" if sampled_at is None else f"{max(0., now - sampled_at):.1f} s"
    text = (f"{progress['stage']} · field={value} T · target={target:+.6g} T · "
            f"error={error} T · age={age} · "
            f"elapsed={max(0., now - progress['started_at']):.0f} s · "
            f"remaining={max(0., progress['deadline'] - now):.0f} s")
    if progress["stage"] == "mode readiness":
        observed = progress.get("mode_observation", {})
        def state(key):
            value = observed.get(key)
            return "yes" if value is True else "no" if value is False else "unknown"
        lead = observed.get("lead_field_t")
        lead_text = "N/A" if lead is None else f"{lead:+.6g} T"
        return (text + f" · Driven={state('driven_mode')} · Persistent={state('persistent_mode')}"
                f" · Heater={state('heater_on')} · Leads hot={state('leads_hot')} · Lead field={lead_text}")
    return text + f" · stable={progress['stable_count']}/5"


class MagnetPreparation:
    def __init__(self, controller, start_field_t, *, targets_t=(), gate_t=0.001,
                 poll_interval_s=.2, timeout_s=300., operation_timeout_s=180.,
                 cleanup_timeout_s=30., stop_event=None, phase=None, log=None,
                 clock=time.monotonic, sleep=time.sleep, position_timeout_s=None,
                 preparation_progress=None):
        self.controller = controller
        self.start_field_t = float(start_field_t)
        self.targets_t = tuple(float(v) for v in targets_t) or (self.start_field_t,)
        if self.start_field_t not in self.targets_t:
            self.targets_t = (self.start_field_t,) + self.targets_t
        self.gate_t = float(gate_t)
        self.poll_interval_s = float(poll_interval_s)
        self.timeout_s = float(timeout_s)
        # Preserve explicit legacy callers; production entry points pass the
        # independent configured positioning budget.
        self.position_timeout_s = float(timeout_s if position_timeout_s is None else position_timeout_s)
        if not math.isfinite(self.position_timeout_s) or self.position_timeout_s <= 0:
            raise ValueError("position_timeout_s must be finite and positive")
        self.operation_timeout_s = float(operation_timeout_s)
        self.cleanup_timeout_s = float(cleanup_timeout_s)
        self.stop_event = stop_event or threading.Event()
        self._phase = phase
        self._log = log
        self.clock, self.sleep = clock, sleep
        self._cancel_lock = threading.Lock()
        self._coordinator_cancelled = False
        self._active_handle = None
        self.field_command_issued = False
        self.mode_requested = False
        self.last_snapshot = None
        self._sampled_at = None
        self._preparation_progress = preparation_progress
        self._stage = "checking"
        self._started_at = self.clock()
        self._deadline = self._started_at + self.operation_timeout_s
        self._stable = 0
        self._last_log_at = float("-inf")
        self._last_log_state = None

    def _remember(self, snapshot):
        self.last_snapshot = snapshot
        self._sampled_at = getattr(snapshot, "monotonic_s", self.clock())

    def _publish(self, stage=None):
        if stage is not None: self._stage = stage
        now = self.clock()
        progress = dict(stage=self._stage, target_t=self.start_field_t,
                        field_t=getattr(self.last_snapshot, "field_t", None),
                        sampled_at=self._sampled_at, started_at=self._started_at,
                        deadline=self._deadline, stable_count=self._stable,
                        mode_observation=preparation_mode_observation(self.last_snapshot))
        if callable(self._preparation_progress): self._preparation_progress(progress)
        state = (self._stage, self._stable > 0)
        if state != self._last_log_state or now - self._last_log_at >= 15:
            self._emit_log(format_preparation_progress(progress, now))
            self._last_log_at, self._last_log_state = now, state
        return progress

    def _check_position_deadline(self):
        self._check_cancelled()
        if self.clock() >= self._deadline:
            progress = self._publish("position timeout")
            raise MagnetPreparationTimeout("magnet did not reach Start before timeout; " +
                                           format_preparation_progress(progress, self.clock()))

    def _emit_phase(self, value):
        if callable(self._phase): self._phase(str(value))

    def _emit_log(self, value):
        if callable(self._log): self._log(str(value))

    def request_cancel(self):
        with self._cancel_lock:
            self._coordinator_cancelled = True
            self.stop_event.set()
        cancel = getattr(self.controller, "cancel_magnet_preparation", None)
        if callable(cancel): cancel()

    def _check_cancelled(self):
        if self.stop_event.is_set() or self._coordinator_cancelled:
            raise MagnetPreparationCancelled("magnet preparation cancelled")

    def _execute(self, handle, timeout=None):
        if handle is None or not callable(getattr(handle, "result", None)) or not callable(getattr(handle, "wait_drained", None)):
            raise TypeError("controller operation did not return a drainable handle")
        budget = self.operation_timeout_s if timeout is None else timeout
        self._active_handle = handle
        primary = None
        try:
            result = handle.result(timeout=budget)
            return result
        except BaseException as exc:
            primary = exc
            if self.stop_event.is_set() or self._coordinator_cancelled:
                raise MagnetPreparationCancelled("magnet preparation cancelled") from exc
            raise
        finally:
            try:
                handle.wait_drained(timeout=budget)
            except BaseException:
                if primary is None: raise
            finally:
                self._active_handle = None

    @staticmethod
    def _field_control(snapshot):
        status = getattr(snapshot, "status", None)
        details = getattr(status, "backend_details", {})
        if isinstance(details, Mapping) and "field_control" in details:
            return details.get("field_control")
        return None

    def _safe(self, snapshot):
        status = getattr(snapshot, "status", None)
        field = float(getattr(snapshot, "field_t"))
        temp = getattr(snapshot, "temperature_k", None)
        config = getattr(self.controller, "config", None)
        configured_field = getattr(config, "maximum_field_t", 6.0)
        configured_temp = getattr(config, "maximum_temperature_k", 7.0)
        try:
            max_field = min(6.0, float(configured_field))
        except (TypeError, ValueError):
            max_field = 6.0
        try:
            max_temp = min(7.0, float(configured_temp))
        except (TypeError, ValueError):
            max_temp = 7.0
        if not math.isfinite(field) or abs(field) > max_field:
            raise MagnetPreparationTimeout("field telemetry is outside the safety ceiling")
        if temp is None or not math.isfinite(float(temp)) or float(temp) > max_temp:
            raise MagnetPreparationTimeout("magnet temperature telemetry is unavailable or unsafe")
        if getattr(status, "quench", None) is not False:
            raise MagnetPreparationTimeout("quench telemetry is not explicitly safe")
        if getattr(status, "driven_mode", None) is not True or getattr(status, "persistent_mode", None) is not False:
            raise MagnetPreparationTimeout("magnet is not observed in Driven mode")
        if self._field_control(snapshot) is not True:
            raise MagnetPreparationTimeout("field control is not active")
        return field

    def _read(self):
        self._check_cancelled()
        preflight = getattr(self.controller, "preflight_magnet_async", None)
        if not callable(preflight):
            raise RuntimeError("controller has no magnet preparation API")
        handle = preflight((self.start_field_t,))
        snap = self._execute(handle)
        self._remember(snap)
        return snap

    def _submit_stop(self):
        stop = getattr(self.controller, "request_stop", None)
        if callable(stop):
            result = self._execute(stop(), self.cleanup_timeout_s)
            self._emit_log("Magnet Stop completed during cleanup")
            return result
        return None

    def _mutation(self, submit, *, field=False):
        with self._cancel_lock:
            self._check_cancelled()
            if field:
                self.field_command_issued = True
            return submit()

    def prepare(self):
        self._started_at = self.clock()
        self._deadline = self._started_at + self.operation_timeout_s
        self._emit_phase("Checking magnet")
        self._publish("checking")
        # Cancellation is a submission boundary: a GUI cancel that wins
        # before the first owner request must prevent even read-only work.
        self._check_cancelled()
        first = self._execute(self.controller.preflight_magnet_async(self.targets_t))
        self._remember(first)
        self._check_cancelled()
        self._emit_phase("Switching to Driven / waiting for readiness")
        self._started_at = self.clock()
        self._deadline = self._started_at + self.timeout_s
        self._publish("mode readiness")
        try:
            mode_handle = self._mutation(
                lambda: self.controller.prepare_driven_mode_async()
            )
            mode = self._execute(mode_handle, self.timeout_s + self.operation_timeout_s)
            self.mode_requested = bool(getattr(mode, "mode_requested", False))
        except AttributeError as exc:
            raise RuntimeError("controller has no magnet preparation API") from exc
        self._check_cancelled()
        self._started_at = self.clock()
        self._deadline = self._started_at + self.position_timeout_s
        self._publish("positioning")
        self._check_position_deadline()
        fresh = self._execute(self.controller.preflight_magnet_async(self.targets_t))
        self._remember(fresh)
        self._check_position_deadline()
        current = float(getattr(fresh, "field_t"))
        setpoint = getattr(fresh, "setpoint_t", None)
        active = self._field_control(fresh) is True
        needs_setpoint = (not active or setpoint is None or abs(float(setpoint) - self.start_field_t) > 1e-6)
        if needs_setpoint:
            self._emit_phase(f"Moving to Start: {current:g} → {self.start_field_t:g} T")
            self._check_position_deadline()
            set_handle = self._mutation(
                lambda: self.controller.set_h_setpoint_async(self.start_field_t), field=True
            )
            self._execute(set_handle)
            self._check_position_deadline()
        if not active:
            start_handle = self._mutation(
                lambda: self.controller.start_field_control_async(), field=True
            )
            self._execute(start_handle)
            self._check_position_deadline()
        self._publish()
        while True:
            self._check_position_deadline()
            snap = self._read()
            # Owner operations still drain naturally. A result arriving after
            # the positioning deadline is evidence, never a late success.
            self._check_position_deadline()
            field = self._safe(snap)
            if abs(field - self.start_field_t) <= self.gate_t:
                self._stable += 1
                if self._stable >= 5:
                    self._publish("ready")
                    self._emit_phase("Magnet ready")
                    return snap
            else:
                self._stable = 0
            self._publish("confirming Start" if self._stable else "positioning")
            self.sleep(self.poll_interval_s)


class MagnetPreparationWorker:
    def __init__(self, controller, start_field_t, **preparation_options):
        self.preparation = MagnetPreparation(controller, start_field_t, **preparation_options)
        self.controller = controller
        self._phase = None
        self._log = None

    def set_callbacks(self, *, phase=None, log=None, preparation_progress=None, **ignored):
        self._phase, self._log = phase, log
        self.preparation._phase, self.preparation._log = phase, log
        self.preparation._preparation_progress = preparation_progress

    def request_cancel(self):
        self.preparation.request_cancel()

    def run(self) -> dict:
        try:
            snapshot = self.preparation.prepare()
            return {"operation": "magnet_preparation", "status": "COMPLETED",
                    "error": None, "cleanup_error": None, "snapshot": snapshot,
                    "mode_requested": bool(self.preparation.mode_requested),
                    "recovery_required": bool(getattr(self.controller, "mode_recovery_required", False))}
        except MagnetPreparationCancelled as exc:
            cleanup_error = None
            if self.preparation.field_command_issued:
                try: self.preparation._submit_stop()
                except BaseException as stop_exc: cleanup_error = str(stop_exc)
            status = "FAILED" if cleanup_error else "CANCELLED"
            return {"operation": "magnet_preparation", "status": status,
                    "error": str(exc), "cleanup_error": cleanup_error,
                    "snapshot": self.preparation.last_snapshot,
                    "mode_requested": bool(self.preparation.mode_requested),
                    "recovery_required": bool(getattr(self.controller, "mode_recovery_required", False))}
        except BaseException as exc:
            cleanup_error = None
            if self.preparation.field_command_issued:
                try: self.preparation._submit_stop()
                except BaseException as stop_exc: cleanup_error = str(stop_exc)
            return {"operation": "magnet_preparation", "status": "FAILED",
                    "error": str(exc), "cleanup_error": cleanup_error,
                    "snapshot": self.preparation.last_snapshot,
                    "mode_requested": bool(self.preparation.mode_requested),
                    "recovery_required": bool(getattr(self.controller, "mode_recovery_required", False))}
