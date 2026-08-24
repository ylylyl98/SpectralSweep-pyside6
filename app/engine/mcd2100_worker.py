"""Discrete stabilized-field MCD workflow for the isolated attoDRY2100 stack."""
from __future__ import annotations

import csv
import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from utils.mcd_common import build_mcd2100_filename, resolve_gate_conditions

MCD2100_SCALAR_FIELDS = [
    "Bfield_T", "rotation_angle_deg", "Vtg_V", "Vbg_V", "Vbias_V",
    "Doping_V", "Efield_V", "field_target_t", "field_before_t",
    "field_after_t", "angle_label",
]


class WorkflowOutcome(str, Enum):
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class MCD2100Cancelled(RuntimeError):
    pass


class MCD2100StabilizationError(RuntimeError):
    pass


class MCD2100TemperatureStabilizationError(MCD2100StabilizationError):
    """Sample-temperature preparation failed before the field sweep started."""


class MCD2100EndpointTimeoutError(MCD2100StabilizationError):
    """Endpoint confirmation expired without evidence of an unsafe control state."""


class MCD2100FieldProgressTimeoutError(MCD2100StabilizationError):
    """The active field leg made no measurable progress for a bounded window."""


@dataclass(frozen=True)
class StabilizationSettings:
    abs_tolerance_t: float = 0.001
    relative_tolerance: float = 2e-4
    db_dt_t_per_s: Optional[float] = 5e-4
    slope_window_s: float = 1.0
    stable_hold_s: float = 1.0
    poll_interval_s: float = 0.2
    settle_timeout_s: float = 120.0
    operation_timeout_s: float = 180.0
    cleanup_timeout_s: float = 30.0

    @classmethod
    def from_value(cls, value: Any) -> "StabilizationSettings":
        if isinstance(value, cls):
            result = value
        else:
            source = vars(value) if value is not None and not isinstance(value, Mapping) else (value or {})
            aliases = {
                "field_tolerance_t": "abs_tolerance_t",
                "dbdt_t_per_s": "db_dt_t_per_s",
                "polling_interval_s": "poll_interval_s",
                "timeout_s": "settle_timeout_s",
            }
            values = {}
            for key, item in dict(source).items():
                key = aliases.get(key, key)
                if key in cls.__dataclass_fields__:
                    values[key] = item
            result = cls(**values)
        result.validate()
        return result

    def validate(self) -> None:
        for value in (self.abs_tolerance_t, self.relative_tolerance, self.slope_window_s, self.stable_hold_s):
            if not math.isfinite(float(value)) or float(value) < 0:
                raise ValueError("stabilization tolerances and durations must be finite and nonnegative")
        if self.db_dt_t_per_s is not None and (
            not math.isfinite(float(self.db_dt_t_per_s)) or float(self.db_dt_t_per_s) < 0
        ):
            raise ValueError("dB/dt threshold must be finite and nonnegative")
        for value in (self.poll_interval_s, self.settle_timeout_s, self.operation_timeout_s, self.cleanup_timeout_s):
            if not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError("poll and timeout values must be finite and positive")


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return str(value)


class DiscreteMCD2100Worker:
    """Legacy discrete worker retained for internal regression coverage."""

    def __init__(self, controller: Any, optical: Any, targets_t: Iterable[float],
                 angles_deg: Iterable[float], output_dir: str | Path, *,
                 stem: str = "mcd2100", settling: Any = None,
                 metadata: Optional[Mapping[str, Any]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.controller = controller
        self.optical = optical
        self.targets = [float(v) for v in targets_t]
        self.angles = [float(v) for v in angles_deg]
        self.output_dir = Path(output_dir)
        self.stem = stem
        self.settings = StabilizationSettings.from_value(settling)
        self.metadata_seed = dict(metadata or {})
        self.clock = clock
        self.sleep = sleep
        self.stop_event = threading.Event()
        self._active_handle = None
        self._stop_handle = None
        self._validate_inputs()

    def _validate_inputs(self) -> None:
        if not self.targets or any(not math.isfinite(v) for v in self.targets):
            raise ValueError("targets_t must be a non-empty finite sequence")
        if not self.angles or any(not math.isfinite(v) for v in self.angles):
            raise ValueError("angles_deg must be a non-empty finite sequence")
        if not self.stem or Path(self.stem).name != self.stem:
            raise ValueError("stem must be a filename stem")

    def request_cancel(self) -> None:
        self.stop_event.set()
        self._submit_stop()

    def _check_cancelled(self) -> None:
        if self.stop_event.is_set():
            raise MCD2100Cancelled("measurement cancelled")

    def _execute_handle(self, handle: Any, timeout: Optional[float] = None) -> Any:
        if handle is None or not callable(getattr(handle, "result", None)) or not callable(getattr(handle, "wait_drained", None)):
            raise TypeError("controller operation did not return a drainable handle")
        limit = self.settings.operation_timeout_s if timeout is None else timeout
        self._active_handle = handle
        primary = None
        try:
            return handle.result(timeout=limit)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                handle.wait_drained(timeout=limit)
            except BaseException:
                if primary is None:
                    raise
            finally:
                self._active_handle = None

    def _submit_stop(self) -> Any:
        if self._stop_handle is None:
            self._stop_handle = self.controller.request_stop()
        return self._stop_handle

    def _drain_stop(self) -> None:
        self._execute_handle(self._submit_stop(), self.settings.cleanup_timeout_s)
        self._stop_handle = None

    def _read_snapshot(self) -> Any:
        return self._execute_handle(self.controller.read_snapshot_async())

    @staticmethod
    def _snapshot_values(snapshot: Any) -> tuple[float, float]:
        field = getattr(snapshot, "field_t", None)
        temperature = getattr(snapshot, "temperature_k", None)
        status = getattr(snapshot, "status", None)
        details = getattr(status, "backend_details", None)
        if not isinstance(field, (int, float)) or not math.isfinite(float(field)):
            raise MCD2100StabilizationError("finite field telemetry is required")
        if not isinstance(temperature, (int, float)) or not math.isfinite(float(temperature)):
            raise MCD2100StabilizationError("finite temperature telemetry is required")
        if status is None or getattr(status, "quench", None) is not False:
            raise MCD2100StabilizationError("explicit non-quench telemetry is required")
        driven_mode = getattr(status, "driven_mode", None)
        persistent_mode = getattr(status, "persistent_mode", None)
        if type(driven_mode) is not bool or type(persistent_mode) is not bool:
            raise MCD2100StabilizationError(
                "explicit driven/persistent mode telemetry is required"
            )
        if driven_mode is not True or persistent_mode is not False:
            raise MCD2100StabilizationError(
                "attoDRY2100 is not in Driven mode. Set the magnet to Driven mode manually before starting MCD."
            )
        if not isinstance(details, Mapping) or details.get("field_control") is not True:
            raise MCD2100StabilizationError("active field-control telemetry is required")
        return float(field), float(temperature)

    def _wait_stable(self, target: float) -> Any:
        started = self.clock()
        stable_since = None
        samples: list[tuple[float, float]] = []
        while True:
            self._check_cancelled()
            now = self.clock()
            if now - started > self.settings.settle_timeout_s:
                raise MCD2100StabilizationError(f"field did not stabilize at {target:g} T before timeout")
            snapshot = self._read_snapshot()
            field, _ = self._snapshot_values(snapshot)
            now = self.clock()
            samples.append((now, field))
            cutoff = now - self.settings.slope_window_s
            epsilon = max(1e-12, self.settings.slope_window_s * 1e-12)
            samples = [(t, b) for t, b in samples if t >= cutoff - epsilon]
            span = samples[-1][0] - samples[0][0]
            slope_ready = self.settings.slope_window_s == 0 or span + epsilon >= self.settings.slope_window_s
            slope = abs((samples[-1][1] - samples[0][1]) / span) if len(samples) >= 2 and span > 0 else 0.0
            slope_ok = self.settings.db_dt_t_per_s is None or (slope_ready and slope <= self.settings.db_dt_t_per_s)
            tolerance = self.settings.abs_tolerance_t + self.settings.relative_tolerance * abs(target)
            if abs(field - target) <= tolerance and slope_ok:
                stable_since = now if stable_since is None else stable_since
                if now - stable_since >= self.settings.stable_hold_s:
                    return snapshot
            else:
                stable_since = None
            self.sleep(self.settings.poll_interval_s)

    def _prepare_wavelengths(self) -> list[float]:
        prepare = getattr(self.optical, "prepare", None)
        raw = prepare(self.stop_event) if callable(prepare) else getattr(self.optical, "wavelengths", None)
        if raw is None:
            raise RuntimeError("optical service did not provide wavelengths")
        values = [float(v) for v in raw]
        if not values or any(not math.isfinite(v) for v in values):
            raise RuntimeError("optical wavelengths must be finite and non-empty")
        return values

    def _acquire(self, target: float, angle: float) -> tuple[list[Any], list[float]]:
        self._check_cancelled()
        before = self._read_snapshot()
        field_before, _ = self._snapshot_values(before)
        acquired = self.optical.acquire(angle, str(angle), self.stop_event)
        self._check_cancelled()
        if not isinstance(acquired, tuple) or len(acquired) != 3:
            raise RuntimeError("optical acquire must return wavelengths, counts, measured angle")
        wavelengths, counts, measured_angle = acquired
        wavelengths = [float(v) for v in wavelengths]
        counts = [float(v) for v in counts]
        if not counts or len(wavelengths) != len(counts) or any(not math.isfinite(v) for v in counts + wavelengths):
            raise RuntimeError("optical spectrum is invalid")
        after = self._read_snapshot()
        field_after, _ = self._snapshot_values(after)
        row = [(field_before + field_after) / 2.0, float(measured_angle), 0, 0, 0, 0, 0,
               target, field_before, field_after, str(angle)]
        return row, counts

    def _paths(self) -> tuple[Path, Path]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        csv_path = self.output_dir / f"{self.stem}.csv"
        index = 1
        while (csv_path.exists() or csv_path.with_suffix(".meta.json").exists()
               or csv_path.with_suffix(".log").exists()):
            csv_path = self.output_dir / f"{self.stem}_{index}.csv"
            index += 1
        return csv_path, csv_path.with_suffix(".meta.json")

    @staticmethod
    def _write_metadata(path: Path, data: Mapping[str, Any]) -> None:
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(_jsonable(data), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)

    def _best_effort_snapshot(self) -> Any:
        try:
            return self._read_snapshot()
        except BaseException as exc:
            return {"unavailable": str(exc)}

    def run(self) -> dict[str, Any]:
        csv_path, metadata_path = self._paths()
        metadata = dict(self.metadata_seed)
        metadata.update({
            "status": "RUNNING", "error": None, "cleanup_error": None,
            "measurement_mode": "discrete", "magnet_backend": "attodry2100",
            "field_points_t": list(self.targets), "settling_parameters": asdict(self.settings),
            "spectra_written": 0, "completed_field_points": 0,
            "initial_snapshot": _jsonable(self._best_effort_snapshot()),
        })
        self._write_metadata(metadata_path, metadata)
        outcome, error, cleanup_error = WorkflowOutcome.COMPLETED, None, None
        try:
            wavelengths = self._prepare_wavelengths()
            with csv_path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(MCD2100_SCALAR_FIELDS + wavelengths)
                stream.flush()
                for target in self.targets:
                    self._check_cancelled()
                    self._execute_handle(self.controller.set_h_setpoint_async(target))
                    self._check_cancelled()
                    self._execute_handle(self.controller.start_field_control_async())
                    self._wait_stable(target)
                    for angle in self.angles:
                        row, counts = self._acquire(target, angle)
                        writer.writerow(row + counts)
                        stream.flush()
                        metadata["spectra_written"] += 1
                        self._write_metadata(metadata_path, metadata)
                    metadata["completed_field_points"] += 1
                    self._write_metadata(metadata_path, metadata)
        except MCD2100Cancelled as exc:
            outcome, error = WorkflowOutcome.CANCELLED, str(exc)
        except BaseException as exc:
            outcome, error = WorkflowOutcome.FAILED, str(exc)
        finally:
            try:
                cleanup = getattr(self.optical, "cleanup", None)
                if callable(cleanup):
                    cleanup()
            except BaseException as exc:
                cleanup_error = (cleanup_error + "; " if cleanup_error else "") + f"optical cleanup: {exc}"
                outcome = WorkflowOutcome.FAILED
            # Keep field control running across successful discrete points and
            # on normal completion.  A stop is required only after a
            # cancellation/failure (including cleanup failure), or when a
            # cancellation already submitted one asynchronously.
            if outcome is not WorkflowOutcome.COMPLETED or self._stop_handle is not None or self.stop_event.is_set():
                try:
                    self._drain_stop()
                except BaseException as exc:
                    cleanup_error = (cleanup_error + "; " if cleanup_error else "") + f"magnet stop: {exc}"
                    outcome = WorkflowOutcome.FAILED
            metadata.update({
                "status": outcome.value, "error": error, "cleanup_error": cleanup_error,
                "final_snapshot": _jsonable(self._best_effort_snapshot()),
            })
            self._write_metadata(metadata_path, metadata)
        return {
            "status": outcome.value, "error": error, "cleanup_error": cleanup_error,
            "csv_path": str(csv_path), "metadata_path": str(metadata_path),
            "spectra_written": metadata["spectra_written"],
            "completed_field_points": metadata["completed_field_points"],
        }


CONTINUOUS_SCALAR_FIELDS = [
    "timestamp_start_utc", "timestamp_end_utc", "leg", "direction",
    "rotation_angle_deg", "B0_T", "B1_T", "Bmid_T", "Vtg_V", "Vbg_V",
    "Vbias_V", "Doping_V", "Efield_V",
    "sample_T0_K", "sample_T1_K", "sample_Tmid_K",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class MCD2100Worker:
    """Continuous attoDRY2100 MCD worker.

    The worker deliberately exposes only the continuous field-sweep contract.
    The legacy discrete implementation remains available as
    :class:`DiscreteMCD2100Worker` for regression tests and internal migration.
    """

    def __init__(
        self,
        controller: Any,
        optical: Any,
        start_field_t: float,
        stop_field_t: float,
        angles_deg: Iterable[float],
        output_dir: str | Path,
        *,
        stem: str = "mcd2100_continuous",
        bidirectional: bool = False,
        gate_t: Optional[float] = None,
        vtg_v: float = 0.0,
        vbg_v: float = 0.0,
        vbias_v: float = 0.0,
        gate_ratio: float = 1.0,
        apply_voltages: bool = False,
        rotator: str = "rot1",
        lf_center_nm: Optional[float] = None,
        lf_exposure_ms: Optional[float] = None,
        lf_frames: Optional[int] = None,
        poll_interval_s: float = 0.2,
        gate_timeout_s: float = 300.0,
        operation_timeout_s: float = 180.0,
        cleanup_timeout_s: float = 30.0,
        temperature_control_enabled: bool = False,
        sample_target_k: float = 4.0,
        sample_ramp_rate_k_per_min: float = 1.0,
        temperature_tolerance_k: float = 0.05,
        temperature_stable_s: float = 10.0,
        temperature_timeout_s: float = 3600.0,
        conditions: Optional[Iterable[Mapping[str, Any]]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.controller = controller
        self.optical = optical
        self.start_field_t = float(start_field_t)
        self.stop_field_t = float(stop_field_t)
        self.angles = [float(v) for v in angles_deg]
        if self.start_field_t == self.stop_field_t:
            raise ValueError("continuous sweep endpoints must differ")
        self.output_dir = Path(output_dir)
        self.stem = stem
        self.bidirectional = bool(bidirectional)
        self.gate_t = None if gate_t is None else float(gate_t)
        self.vtg_v, self.vbg_v, self.vbias_v = float(vtg_v), float(vbg_v), float(vbias_v)
        self.gate_ratio = float(gate_ratio)
        self.apply_voltages = bool(apply_voltages)
        self.rotator = str(rotator or "rot1")
        self.lf_center_nm = None if lf_center_nm is None else float(lf_center_nm)
        self.lf_exposure_ms = None if lf_exposure_ms is None else float(lf_exposure_ms)
        self.lf_frames = None if lf_frames is None else int(lf_frames)
        self.poll_interval_s = float(poll_interval_s)
        self.gate_timeout_s = float(gate_timeout_s)
        self.operation_timeout_s = float(operation_timeout_s)
        self.cleanup_timeout_s = float(cleanup_timeout_s)
        self.temperature_control_enabled = bool(temperature_control_enabled)
        self.sample_target_k = float(sample_target_k)
        self.sample_ramp_rate_k_per_min = float(sample_ramp_rate_k_per_min)
        self.temperature_tolerance_k = float(temperature_tolerance_k)
        self.temperature_stable_s = float(temperature_stable_s)
        self.temperature_timeout_s = float(temperature_timeout_s)
        self.conditions = self._normalize_conditions(conditions, self.gate_ratio)
        self.sleep, self.clock = sleep, clock
        self.metadata_seed = dict(metadata or {})
        self.stop_event = threading.Event()
        self._active_handle = None
        self._stop_handle = None
        self._magnet_command_issued = False
        self._metadata_path: Optional[Path] = None
        self._metadata: Optional[dict[str, Any]] = None
        self._progress_cb: Optional[Callable[..., None]] = None
        self._spectrum_cb: Optional[Callable[..., None]] = None
        self._spectrum_event_cb: Optional[Callable[[Mapping[str, Any]], None]] = None
        self._log_cb: Optional[Callable[..., None]] = None
        self._validate_inputs()

    @staticmethod
    def _normalize_conditions(conditions: Optional[Iterable[Mapping[str, Any]]], ratio: float = 1.0) -> list[dict[str, Any]]:
        if conditions is None:
            return []
        result = []
        for index, condition in enumerate(conditions):
            if not isinstance(condition, Mapping):
                raise ValueError(f"gate condition {index + 1} must be a mapping")
            item = {
                "enabled": bool(condition.get("enabled", True)),
                "mode": condition.get("mode", "direct"),
                "input_a": condition.get("input_a", condition.get("vtg_v", 0.0)),
                "input_b": condition.get("input_b", condition.get("vbg_v", 0.0)),
                "vtg_v": float(condition.get("vtg_v", 0.0)),
                "vbg_v": float(condition.get("vbg_v", 0.0)),
                "vbias_v": float(condition.get("vbias_v", 0.0)),
                "doping_v": float(condition.get("doping_v", 0.0)),
                "efield_v": float(condition.get("efield_v", 0.0)),
            }
            if any(not math.isfinite(item[key]) for key in ("vtg_v", "vbg_v", "vbias_v", "doping_v", "efield_v")):
                raise ValueError(f"gate condition {index + 1} contains non-finite values")
            result.append(item)
        if not any(item["enabled"] for item in result):
            raise ValueError("at least one enabled gate condition is required")
        try:
            return resolve_gate_conditions(result, ratio)
        except ValueError:
            # Preserve the legacy scalar constructor's permissive shape; the
            # panel performs the authoritative visible validation.
            return result

    def set_callbacks(self, *, progress=None, spectrum=None, spectrum_event=None, log=None) -> None:
        """Attach GUI-safe callbacks without changing the worker contract."""
        self._progress_cb, self._spectrum_cb = progress, spectrum
        self._spectrum_event_cb, self._log_cb = spectrum_event, log

    def _emit_log(self, message: str) -> None:
        if callable(self._log_cb):
            self._log_cb(str(message))

    def _emit_progress(self, field_t: float, percent: float, condition_index: int = 1,
                       condition_count: int = 1) -> None:
        if callable(self._progress_cb):
            self._progress_cb(float(field_t), float(percent), int(condition_index), int(condition_count))

    def _emit_spectrum(self, wavelengths, counts, label: str, field_t: float,
                       event: Optional[Mapping[str, Any]] = None) -> None:
        if callable(self._spectrum_cb):
            self._spectrum_cb(list(wavelengths), list(counts), str(label), float(field_t))
        if callable(self._spectrum_event_cb) and event is not None:
            self._spectrum_event_cb(dict(event))

    def _validate_inputs(self) -> None:
        values = (self.start_field_t, self.stop_field_t, self.gate_ratio,
                  self.poll_interval_s, self.gate_timeout_s,
                  self.operation_timeout_s, self.cleanup_timeout_s)
        if any(not math.isfinite(v) for v in values):
            raise ValueError("continuous sweep settings must be finite")
        if not self.angles or any(not math.isfinite(v) for v in self.angles):
            raise ValueError("angles_deg must be a non-empty finite sequence")
        if self.gate_t is not None and (not math.isfinite(self.gate_t) or self.gate_t <= 0):
            raise ValueError("gate_t must be finite and positive")
        if min(self.poll_interval_s, self.gate_timeout_s, self.operation_timeout_s,
               self.cleanup_timeout_s) <= 0:
            raise ValueError("continuous sweep timings must be positive")
        temperature_values = (
            self.sample_target_k, self.sample_ramp_rate_k_per_min,
            self.temperature_tolerance_k, self.temperature_stable_s,
            self.temperature_timeout_s,
        )
        if any(not math.isfinite(value) for value in temperature_values):
            raise ValueError("sample temperature settings must be finite")
        if not 1.8 <= self.sample_target_k <= 300.0:
            raise ValueError("sample target must be within 1.8 to 300 K")
        if not 0.1 <= self.sample_ramp_rate_k_per_min <= 100.0:
            raise ValueError("sample ramp rate must be within 0.1 to 100 K/min")
        if self.temperature_tolerance_k <= 0 or self.temperature_stable_s < 0 or self.temperature_timeout_s <= 0:
            raise ValueError("temperature tolerance and timeout must be positive")
        if abs(self.start_field_t) > 6.0 or abs(self.stop_field_t) > 6.0:
            raise ValueError("continuous field range is limited to ±6 T")
        if not self.stem or Path(self.stem).name != self.stem:
            raise ValueError("stem must be a filename stem")
        for value in (self.lf_center_nm, self.lf_exposure_ms):
            if value is not None and not math.isfinite(value):
                raise ValueError("LightField settings must be finite")
        if self.lf_frames is not None and self.lf_frames <= 0:
            raise ValueError("lf_frames must be positive")
        for condition in self.conditions:
            if any(abs(condition[key]) > 1_000.0 for key in ("vtg_v", "vbg_v", "vbias_v")):
                raise ValueError("gate condition exceeds the ±1000 V safety ceiling")

    def request_cancel(self) -> None:
        self.stop_event.set()
        if self._magnet_command_issued:
            self._submit_stop()

    def _check_cancelled(self) -> None:
        if self.stop_event.is_set():
            raise MCD2100Cancelled("measurement cancelled")

    def _execute_handle(self, handle: Any, timeout: Optional[float] = None) -> Any:
        if handle is None or not callable(getattr(handle, "result", None)) or not callable(getattr(handle, "wait_drained", None)):
            raise TypeError("controller operation did not return a drainable handle")
        limit = self.operation_timeout_s if timeout is None else timeout
        self._active_handle = handle
        primary = None
        try:
            return handle.result(timeout=limit)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                handle.wait_drained(timeout=limit)
            except BaseException:
                if primary is None:
                    raise
            finally:
                self._active_handle = None

    def _submit_stop(self) -> Any:
        if self._stop_handle is None:
            self._stop_handle = self.controller.request_stop()
        return self._stop_handle

    def _drain_stop(self) -> None:
        try:
            result = self._execute_handle(self._submit_stop(), self.cleanup_timeout_s)
            if self._metadata is not None:
                self._metadata["stop_ack"] = _jsonable(result)
                self._metadata["stop_error"] = None
                if self._metadata_path is not None:
                    self._write_metadata(self._metadata_path, self._metadata)
        except BaseException as exc:
            if self._metadata is not None:
                self._metadata["stop_error"] = str(exc)
                if self._metadata_path is not None:
                    self._write_metadata(self._metadata_path, self._metadata)
            raise
        finally:
            self._stop_handle = None

    def _read_snapshot(self) -> Any:
        snapshot = self._execute_handle(self.controller.read_snapshot_async())
        field, temperature = self._snapshot_values(snapshot)
        if abs(field) > 6.0:
            raise MCD2100StabilizationError("field telemetry exceeds the ±6 T safety ceiling")
        if temperature > 7.0:
            raise MCD2100StabilizationError("temperature exceeds the 7 K safety ceiling")
        if self._metadata is not None:
            status = getattr(snapshot, "status", None)
            details = getattr(status, "backend_details", {})
            record = {
                "timestamp_utc": _utc_now(), "field_t": field,
                "setpoint_t": getattr(snapshot, "setpoint_t", None),
                "temperature_k": temperature,
                "driven_mode": getattr(status, "driven_mode", None),
                "persistent_mode": getattr(status, "persistent_mode", None),
                "h_state": (
                    (details.get("h_state") if details.get("h_state") is not None
                     else getattr(status, "field_control_state", None))
                    if isinstance(details, Mapping) else getattr(status, "field_control_state", None)
                ),
                "field_control": details.get("field_control") if isinstance(details, Mapping) else None,
                "quench": getattr(status, "quench", None),
                "heater": (
                    (details.get("heater") if details.get("heater") is not None
                     else getattr(status, "heater_on", None))
                    if isinstance(details, Mapping) else getattr(status, "heater_on", None)
                ),
                "leads_hot": (
                    (details.get("leads_hot") if details.get("leads_hot") is not None
                     else getattr(status, "leads_hot", None))
                    if isinstance(details, Mapping) else getattr(status, "leads_hot", None)
                ),
                "lead_field": (
                    (details.get("lead_field") if details.get("lead_field") is not None
                     else getattr(snapshot, "lead_field_t", None))
                    if isinstance(details, Mapping) else getattr(snapshot, "lead_field_t", None)
                ),
            }
            self._metadata.setdefault("snapshots", []).append(record)
            self._metadata.setdefault("initial_snapshot", record)
            if self._metadata_path is not None:
                self._write_metadata(self._metadata_path, self._metadata)
        return snapshot

    def _read_sample_temperature(self) -> float:
        read = getattr(self.controller, "read_sample_temperature_async", None)
        if not callable(read):
            raise RuntimeError("shared attoDRY2100 controller has no sample-temperature readback")
        value = float(self._execute_handle(read()))
        if not math.isfinite(value):
            raise RuntimeError("sample temperature readback is non-finite")
        return value

    def _read_temperature_snapshot(self) -> Any:
        read = getattr(self.controller, "read_temperature_snapshot_async", None)
        if not callable(read):
            raise RuntimeError("shared attoDRY2100 controller has no temperature telemetry")
        return self._execute_handle(read())

    def _stabilize_sample_temperature(self, metadata: dict[str, Any]) -> Any:
        configure = getattr(self.controller, "configure_sample_temperature_async", None)
        if not callable(configure):
            raise MCD2100TemperatureStabilizationError(
                "shared attoDRY2100 controller has no sample-temperature control"
            )
        self._emit_log(
            f"Sample temperature: target={self.sample_target_k:g} K, "
            f"ramp={self.sample_ramp_rate_k_per_min:g} K/min"
        )
        try:
            configured = self._execute_handle(configure(
                self.sample_target_k, self.sample_ramp_rate_k_per_min
            ))
        except MCD2100Cancelled:
            raise
        except BaseException as exc:
            raise MCD2100TemperatureStabilizationError(
                f"sample-temperature control could not be started: {exc}"
            ) from exc
        started = self.clock()
        stable_since = None
        last = configured
        while True:
            self._check_cancelled()
            last = self._read_temperature_snapshot()
            sample_temperature = getattr(last, "sample_temperature_k", None)
            sample_control = getattr(last, "sample_control_active", None)
            vti_control = getattr(last, "vti_control_active", None)
            if not isinstance(sample_temperature, (int, float)) or not math.isfinite(float(sample_temperature)):
                raise MCD2100TemperatureStabilizationError(
                    "sample temperature telemetry is unavailable"
                )
            if sample_control is not True:
                raise MCD2100TemperatureStabilizationError(
                    "sample temperature control is not active"
                )
            vti_ready = self.sample_target_k < 10.0 or vti_control is True
            within = abs(float(sample_temperature) - self.sample_target_k) <= self.temperature_tolerance_k
            now = self.clock()
            if within and vti_ready:
                stable_since = now if stable_since is None else stable_since
                if now - stable_since >= self.temperature_stable_s:
                    elapsed = now - started
                    metadata["temperature_stabilization"] = {
                        "status": "stable", "elapsed_s": elapsed,
                        "final": _jsonable(last),
                    }
                    self._emit_log(
                        f"Sample temperature stable at {float(sample_temperature):.6g} K "
                        f"after {elapsed:.1f} s"
                    )
                    return last
            else:
                stable_since = None
            if now - started > self.temperature_timeout_s:
                metadata["temperature_stabilization"] = {
                    "status": "timeout", "elapsed_s": now - started,
                    "last": _jsonable(last),
                }
                vti_note = "" if self.sample_target_k < 10.0 else f", VTI control={vti_control!r}"
                raise MCD2100TemperatureStabilizationError(
                    f"sample temperature did not stabilize at {self.sample_target_k:g} K "
                    f"within {self.temperature_timeout_s:g} s "
                    f"(last={float(sample_temperature):g} K{vti_note})"
                )
            self._sleep_checked(self.poll_interval_s)

    def _read_field(self) -> float:
        """Timing-critical field-only read through the shared controller owner."""
        read = getattr(self.controller, "read_field_async", None)
        if not callable(read):
            raise RuntimeError("attoDRY2100 controller does not expose field-only telemetry")
        value = self._execute_handle(read())
        try:
            field = float(value)
        except (TypeError, ValueError) as exc:
            raise MCD2100StabilizationError("field-only telemetry is not numeric") from exc
        if not math.isfinite(field):
            raise MCD2100StabilizationError("field-only telemetry is not finite")
        if abs(field) > 6.0:
            raise MCD2100StabilizationError("field-only telemetry exceeds the ±6 T safety ceiling")
        return field

    @staticmethod
    def _snapshot_values(snapshot: Any) -> tuple[float, float]:
        # Share the exact fail-closed telemetry contract with the discrete path.
        return DiscreteMCD2100Worker._snapshot_values(snapshot)

    def _target_gate(self, target: float) -> float:
        return self.gate_t if self.gate_t is not None else max(0.001, abs(target) * 2e-4)

    def _gate(self) -> float:
        return self._target_gate(self.stop_field_t)

    def _preposition_gate(self) -> float:
        return self._target_gate(self.start_field_t)

    def _sleep_checked(self, seconds: float) -> None:
        deadline = self.clock() + seconds
        while True:
            self._check_cancelled()
            remaining = deadline - self.clock()
            if remaining <= 0:
                return
            self.sleep(min(remaining, self.poll_interval_s))

    def _wait_gate(self, target: float, *, increasing: bool, initial: Any = None) -> Any:
        gate = self._target_gate(target)
        started = self.clock()
        consecutive = 0
        if initial is not None:
            initial_field, _ = self._snapshot_values(initial)
            consecutive = 1 if abs(initial_field - target) <= gate else 0
        last = None
        while True:
            self._check_cancelled()
            if self.clock() - started > self.gate_timeout_s:
                raise MCD2100EndpointTimeoutError(
                    f"field did not reach {target:g} T within gate timeout"
                )
            snapshot = self._read_snapshot()
            field, _ = self._snapshot_values(snapshot)
            last = snapshot
            within = abs(field - target) <= gate
            consecutive = consecutive + 1 if within else 0
            if consecutive >= 5:
                return last
            self._sleep_checked(self.poll_interval_s)

    def _set_and_start(self, target: float) -> None:
        self._check_cancelled()
        # From this point on, cleanup must leave the magnet in Hold if the
        # workflow fails or is cancelled.
        self._magnet_command_issued = True
        self._execute_handle(self.controller.set_h_setpoint_async(target))
        self._check_cancelled()
        self._execute_handle(self.controller.start_field_control_async())

    def _direction_reached(self, field: float, target: float, increasing: bool) -> bool:
        gate = self._target_gate(target)
        return field >= target - gate if increasing else field <= target + gate

    def _check_direction_step(self, previous: float | None, current: float,
                              target: float, increasing: bool) -> None:
        if previous is None:
            return
        gate = self._target_gate(target)
        if increasing and current < previous - gate:
            raise MCD2100StabilizationError(
                f"field moved in the wrong direction while increasing: "
                f"{previous:g} -> {current:g} T"
            )
        if not increasing and current > previous + gate:
            raise MCD2100StabilizationError(
                f"field moved in the wrong direction while decreasing: "
                f"{previous:g} -> {current:g} T"
            )

    def _prepare_wavelengths(self) -> list[float]:
        prepare = getattr(self.optical, "prepare", None)
        raw = prepare(self.stop_event) if callable(prepare) else getattr(self.optical, "wavelengths", None)
        if raw is None:
            raise RuntimeError("optical service did not provide wavelengths")
        values = [float(value) for value in raw]
        if not values or any(not math.isfinite(value) for value in values):
            raise RuntimeError("optical wavelengths must be finite and non-empty")
        return values

    def _apply_setup(self, *, condition: Optional[Mapping[str, Any]] = None,
                     configure: bool = True, apply_gate: bool = True) -> dict[str, Any]:
        """Apply optical/gate setup once through the existing service surface."""
        applied: dict[str, Any] = {}
        if condition is not None:
            self.vtg_v = float(condition.get("vtg_v", 0.0))
            self.vbg_v = float(condition.get("vbg_v", 0.0))
            self.vbias_v = float(condition.get("vbias_v", 0.0))
        configure_fn = getattr(self.optical, "configure", None)
        if configure and callable(configure_fn):
            # Complete optical readiness/configuration before any magnet or SMU
            # mutation can begin. This is the MCD2100 preflight boundary.
            applied["lightfield"] = _jsonable(configure_fn(
                center_nm=self.lf_center_nm, exposure_ms=self.lf_exposure_ms,
                frames=self.lf_frames,
            ))
        apply_gates = getattr(self.optical, "apply_gates", None)
        if apply_gate and self.apply_voltages and callable(apply_gates):
            applied["smu"] = _jsonable(apply_gates(
                vtg_v=self.vtg_v, vbg_v=self.vbg_v, vbias_v=self.vbias_v,
                ratio=self.gate_ratio, stop_cb=self.stop_event.is_set,
            ))
        return applied

    def _paths(self) -> tuple[Path, Path]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        csv_path = self.output_dir / f"{self.stem}.csv"
        index = 1
        while (csv_path.exists() or csv_path.with_suffix(".meta.json").exists()
               or csv_path.with_suffix(".log").exists()):
            csv_path = self.output_dir / f"{self.stem}_{index}.csv"
            index += 1
        return csv_path, csv_path.with_suffix(".meta.json")

    @staticmethod
    def _write_metadata(path: Path, data: Mapping[str, Any]) -> None:
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            clean = {key: value for key, value in data.items() if not str(key).startswith("_")}
            json.dump(_jsonable(clean), stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _align_counts(header: list[float], row_wavelengths: list[float], counts: list[float]) -> list[float]:
        if len(row_wavelengths) != len(counts) or not row_wavelengths:
            raise RuntimeError("optical spectrum axis/count length mismatch")
        if len(header) == len(row_wavelengths) and all(abs(a - b) <= 1e-9 for a, b in zip(header, row_wavelengths)):
            return counts
        if len(row_wavelengths) < 2:
            raise RuntimeError("optical wavelength axis has insufficient points for interpolation")
        if any(b <= a for a, b in zip(row_wavelengths, row_wavelengths[1:])):
            raise RuntimeError("optical wavelength axis is not strictly increasing")
        if header[0] < row_wavelengths[0] or header[-1] > row_wavelengths[-1]:
            raise RuntimeError("optical wavelength axis does not cover prepared calibration")
        aligned = []
        for x in header:
            index = 1
            while row_wavelengths[index] < x:
                index += 1
            x0, x1 = row_wavelengths[index - 1], row_wavelengths[index]
            y0, y1 = counts[index - 1], counts[index]
            aligned.append(y0 + (x - x0) / (x1 - x0) * (y1 - y0))
        return aligned

    def _overall_progress_percent(
        self,
        *,
        leg: int,
        leg_fraction: float,
        condition_index: int = 1,
        condition_count: int = 1,
    ) -> float:
        legs_per_condition = 2 if (self.conditions or self.bidirectional) else 1
        total_units = max(1, int(condition_count) * legs_per_condition)
        completed_units = (
            (max(1, int(condition_index)) - 1) * legs_per_condition
            + (max(1, int(leg)) - 1)
            + min(1.0, max(0.0, float(leg_fraction)))
        )
        return min(100.0, max(0.0, 100.0 * completed_units / total_units))

    def _leg(self, *, leg: int, start: float, target: float, writer: csv.writer,
             stream: Any, wavelengths: list[float], metadata: dict[str, Any],
             condition_index: int = 1, condition_count: int = 1,
             verified_start_snapshot: Any = None) -> Any:
        increasing = target >= start
        direction = "increasing" if increasing else "decreasing"
        leg_label = "forward" if leg == 1 else "backward"
        metadata["current_leg"] = {"leg": leg, "direction": direction,
                                    "start_field_t": start, "target_field_t": target}
        self._emit_log(
            f"Gate {condition_index}/{condition_count}: {direction} leg "
            f"{start:+g} T to {target:+g} T"
        )
        initial = verified_start_snapshot if verified_start_snapshot is not None else self._read_snapshot()
        initial_field, _ = self._snapshot_values(initial)
        current_detail = metadata.get("current_file_detail")
        if isinstance(current_detail, dict):
            field_detail = current_detail.setdefault("field", {})
            field_detail[f"{leg_label}_actual_start_T"] = initial_field
            if leg == 1:
                field_detail["actual_start_T"] = initial_field
        if verified_start_snapshot is not None and abs(initial_field - start) <= self._target_gate(start):
            # The preceding leg ended with this fresh, in-gate, safety-validated
            # snapshot. Reuse it instead of performing five more slow telemetry
            # reads before reversing direction.
            metadata["preposition_skipped"] = True
            gate_snapshot = initial
        elif abs(initial_field - start) <= self._target_gate(start):
            metadata["preposition_skipped"] = True
            gate_snapshot = self._wait_gate(start, increasing=increasing, initial=initial)
        else:
            metadata["preposition_skipped"] = False
            self._set_and_start(start)
            gate_snapshot = self._wait_gate(start, increasing=increasing)
        # Position the first polarization angle while the magnet is still at the
        # verified leg start. This prevents rotation time from consuming the ramp.
        first_angle_prepositioned = False
        if self.angles:
            move = getattr(self.optical, "move_to", None)
            if callable(move):
                move(float(self.angles[0]))
                first_angle_prepositioned = True
        self._sleep_checked(0.5)
        self._set_and_start(target)
        previous_field, _ = self._snapshot_values(gate_snapshot)
        gate = self._target_gate(target)
        best_progress_field = previous_field
        last_progress_at = self.clock()

        def record_progress(field: float) -> None:
            nonlocal best_progress_field, last_progress_at
            advanced = (
                field > best_progress_field + gate
                if increasing else field < best_progress_field - gate
            )
            if advanced:
                best_progress_field = field
                last_progress_at = self.clock()

        while True:
            self._check_cancelled()
            field = self._read_field()
            if self._direction_reached(field, target, increasing):
                # A full snapshot remains authoritative at each endpoint and is
                # reused by the return leg.  The fast read only avoids spending
                # the ramp itself inside unrelated telemetry calls.
                snapshot = self._read_snapshot()
                verified_field, _ = self._snapshot_values(snapshot)
                if not self._direction_reached(verified_field, target, increasing):
                    previous_field = verified_field
                    continue
                self._emit_progress(
                    verified_field,
                    self._overall_progress_percent(
                        leg=leg, leg_fraction=1.0,
                        condition_index=condition_index,
                        condition_count=condition_count,
                    ),
                    condition_index,
                    condition_count,
                )
                return snapshot
            self._check_direction_step(previous_field, field, target, increasing)
            record_progress(field)
            inactive_s = self.clock() - last_progress_at
            if inactive_s > self.operation_timeout_s:
                raise MCD2100FieldProgressTimeoutError(
                    f"continuous field leg {leg} made no progress toward {target:g} T "
                    f"for {inactive_s:.1f} s (last field {field:g} T)"
                )
            previous_field = field
            for angle_index, angle in enumerate(self.angles):
                self._check_cancelled()
                move = getattr(self.optical, "move_to", None)
                if first_angle_prepositioned and angle_index == 0:
                    first_angle_prepositioned = False
                elif callable(move):
                    move(float(angle))
                position = getattr(self.optical, "get_position", None)
                measured_angle = float(position()) if callable(position) else float(angle)
                sample_t0 = self._read_sample_temperature() if self.temperature_control_enabled else None
                b0 = self._read_field()
                timestamp_start = _utc_now()
                acquired = self.optical.acquire(angle, str(angle), self.stop_event)
                timestamp_end = _utc_now()
                self._check_cancelled()
                if not isinstance(acquired, tuple) or len(acquired) != 3:
                    raise RuntimeError("optical acquire must return wavelengths, counts, measured angle")
                raw_wavelengths, raw_counts, reported_angle = acquired
                row_wavelengths = [float(v) for v in raw_wavelengths]
                counts = [float(v) for v in raw_counts]
                if not counts or len(row_wavelengths) != len(counts) or any(
                    not math.isfinite(v) for v in row_wavelengths + counts
                ):
                    raise RuntimeError("optical spectrum is invalid")
                counts = self._align_counts(wavelengths, row_wavelengths, counts)
                b1 = self._read_field()
                sample_t1 = self._read_sample_temperature() if self.temperature_control_enabled else None
                self._check_direction_step(previous_field, b1, target, increasing)
                record_progress(b1)
                previous_field = b1
                measured_angle = float(reported_angle) if isinstance(reported_angle, (int, float)) else measured_angle
                doping = self.vtg_v + self.gate_ratio * self.vbg_v
                efield = self.vtg_v - self.gate_ratio * self.vbg_v
                sample_tmid = (
                    (sample_t0 + sample_t1) / 2.0
                    if sample_t0 is not None and sample_t1 is not None else None
                )
                writer.writerow([
                    timestamp_start, timestamp_end, leg_label, direction, measured_angle,
                    b0, b1, (b0 + b1) / 2.0, self.vtg_v, self.vbg_v,
                    self.vbias_v, doping, efield,
                    sample_t0, sample_t1, sample_tmid, *counts,
                ])
                metadata["spectra_written"] += 1
                metadata["last_field_t"] = b1
                metadata["last_direction"] = direction
                metadata["last_angle_deg"] = measured_angle
                if sample_tmid is not None:
                    metadata["last_sample_temperature_k"] = sample_tmid
                current_detail = metadata.get("current_file_detail")
                if isinstance(current_detail, dict):
                    field_detail = current_detail.setdefault("field", {})
                    field_detail[f"{leg_label}_actual_stop_T"] = b1
                    field_detail["actual_stop_T"] = b1
                    current_detail["spectra_written"] = int(
                        metadata["spectra_written"] - current_detail.get("spectra_start", 0)
                    )
                stream.flush()
                os.fsync(stream.fileno())
                # The durable CSV row is the live-data boundary.  Plot it
                # immediately; the larger metadata snapshot follows and must
                # not delay the user's spectrum update.
                self._emit_spectrum(
                    wavelengths, counts,
                    chr(ord("A") + angle_index) if angle_index < 26 else str(angle_index + 1),
                    b1,
                    event={
                        "label": chr(ord("A") + angle_index) if angle_index < 26 else str(angle_index + 1),
                        "wavelengths": list(wavelengths), "counts": list(counts),
                        "requested_angle_deg": float(angle), "measured_angle_deg": measured_angle,
                        "B0_T": b0, "B1_T": b1, "Bmid_T": (b0 + b1) / 2.0,
                        "gate_index": condition_index, "gate_count": condition_count,
                        "direction": leg_label, "file_path": metadata.get("current_file_path"),
                        "total_spectra": int(metadata["spectra_written"]),
                    },
                )
                span = abs(target - start)
                leg_fraction = 1.0 if span <= 1e-12 else min(
                    1.0, max(0.0, abs(b1 - start) / span)
                )
                self._emit_progress(
                    b1,
                    self._overall_progress_percent(
                        leg=leg, leg_fraction=leg_fraction,
                        condition_index=condition_index,
                        condition_count=condition_count,
                    ),
                    condition_index,
                    condition_count,
                )
                self._write_metadata(metadata["_path"], metadata)
            # Complete the angle cycle that started below the direction gate;
            # crossing during that cycle is retained as real data.  The next
            # loop performs the direction gate check.  A full safety snapshot
            # is mandatory at the cycle boundary, but never inserted between
            # B0, acquisition, B1, durable write, and live plot.
            safety_snapshot = self._read_snapshot()
            safety_field, _ = self._snapshot_values(safety_snapshot)
            if abs(safety_field - target) <= gate:
                self._emit_progress(
                    safety_field,
                    self._overall_progress_percent(
                        leg=leg, leg_fraction=1.0,
                        condition_index=condition_index,
                        condition_count=condition_count,
                    ),
                    condition_index,
                    condition_count,
                )
                return safety_snapshot
            if not self._direction_reached(previous_field, target, increasing):
                self._check_direction_step(previous_field, safety_field, target, increasing)
            record_progress(safety_field)
            previous_field = safety_field

    def run(self) -> dict[str, Any]:
        csv_path, metadata_path = self._paths()
        metadata: dict[str, Any] = {
            "status": "RUNNING", "error": None, "cleanup_error": None,
            "stop_ack": None, "stop_error": None,
            "measurement_mode": "continuous", "magnet_backend": "attodry2100",
            "start_field_t": self.start_field_t, "stop_field_t": self.stop_field_t,
            "bidirectional": self.bidirectional, "spectra_written": 0,
            "completed_legs": 0, "snapshots": [], "_path": metadata_path,
            "requested_endpoints_t": [self.start_field_t, self.stop_field_t],
            "angles_deg": list(self.angles), "start_utc": _utc_now(),
            "safety": {"maximum_field_t": 6.0, "maximum_temperature_k": 7.0},
            "lf_requested": {"center_nm": self.lf_center_nm, "exposure_ms": self.lf_exposure_ms, "frames": self.lf_frames},
            "smu_requested": {"apply": self.apply_voltages, "Vtg_V": self.vtg_v, "Vbg_V": self.vbg_v, "Vbias_V": self.vbias_v},
            "temperature_requested": {
                "enabled": self.temperature_control_enabled,
                "sample_target_k": self.sample_target_k,
                "ramp_rate_k_per_min": self.sample_ramp_rate_k_per_min,
                "tolerance_k": self.temperature_tolerance_k,
                "stable_s": self.temperature_stable_s,
                "timeout_s": self.temperature_timeout_s,
                "vti_coordination": "cryostat automatic",
            },
        }
        if self.conditions:
            metadata["gate_conditions"] = _jsonable(self.conditions)
            metadata["file_details"] = []
        self._metadata_path, self._metadata = metadata_path, metadata
        self._write_metadata(metadata_path, {k: v for k, v in metadata.items() if k != "_path"})
        outcome, error, cleanup_error = WorkflowOutcome.COMPLETED, None, None
        failure_exception: BaseException | None = None
        detached_success = False
        completion_snapshot: Any = None
        try:
            if self.temperature_control_enabled:
                self._stabilize_sample_temperature(metadata)
                self._write_metadata(metadata_path, metadata)
            metadata["setup_applied"] = self._apply_setup(
                configure=True, apply_gate=not bool(self.conditions)
            )
            wavelengths = self._prepare_wavelengths()
            if not self.conditions:
                with csv_path.open("w", newline="", encoding="utf-8") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(CONTINUOUS_SCALAR_FIELDS + wavelengths)
                    stream.flush(); os.fsync(stream.fileno())
                    turnaround_snapshot = self._leg(
                        leg=1, start=self.start_field_t, target=self.stop_field_t,
                        writer=writer, stream=stream, wavelengths=wavelengths,
                        metadata=metadata,
                    )
                    completion_snapshot = turnaround_snapshot
                    metadata["completed_legs"] = 1
                    if self.bidirectional:
                        completion_snapshot = self._leg(
                            leg=2, start=self.stop_field_t, target=self.start_field_t,
                            writer=writer, stream=stream, wavelengths=wavelengths,
                            metadata=metadata,
                            verified_start_snapshot=turnaround_snapshot,
                        )
                        metadata["completed_legs"] = 2
            else:
                enabled = [item for item in self.conditions if item.get("enabled", True)]
                metadata["batch_file_paths"] = []
                for condition_index, condition in enumerate(enabled, start=1):
                    self._check_cancelled()
                    applied = self._apply_setup(condition=condition, configure=False, apply_gate=True)
                    legs = [(1, self.start_field_t, self.stop_field_t, "forward")]
                    # A condition batch is always a round trip.  The legacy
                    # scalar constructor retains its explicit flag.
                    if self.conditions or self.bidirectional:
                        legs.append((2, self.stop_field_t, self.start_field_t, "backward"))
                    file_direction = "roundtrip" if len(legs) == 2 else "forward"
                    batch_name = build_mcd2100_filename(
                        self.metadata_seed.get("device_id", self.stem), condition_index,
                        self.start_field_t, self.stop_field_t, file_direction,
                        doping_v=condition.get("doping_v"),
                        efield_v=condition.get("efield_v"),
                        vtg_v=condition.get("vtg_v"),
                        vbg_v=condition.get("vbg_v"),
                        vbias_v=condition.get("vbias_v"), ratio=self.gate_ratio,
                    )
                    batch_path = self.output_dir / batch_name
                    suffix = 1
                    while batch_path.exists():
                        batch_path = self.output_dir / batch_name.replace(".csv", f"_{suffix}.csv")
                        suffix += 1
                    detail = {
                        "path": str(batch_path), "role": "raw",
                        "kind": "continuous_mcd_spectrum",
                        "condition_index": condition_index,
                        "direction": file_direction,
                        "directions": [item[3] for item in legs],
                        "gate_condition_index": condition_index,
                        "gate_condition_count": len(enabled),
                        "gate_mode": condition.get("mode", "direct"),
                        "gate_parameters": {
                            "input_a": condition.get("input_a"), "input_b": condition.get("input_b"),
                            "gate_ratio": self.gate_ratio,
                            "provenance": condition.get("provenance", {}),
                        },
                        "requested_gate": {
                            "Vtg_V": condition.get("vtg_v"), "Vbg_V": condition.get("vbg_v"),
                            "Vbias_V": condition.get("vbias_v"),
                        },
                        "observed_gate": applied.get("smu", {}) if isinstance(applied, dict) else {},
                        "derived_coordinates": {
                            "Doping_V": condition.get("doping_v"), "Efield_V": condition.get("efield_v"),
                            "gate_ratio": self.gate_ratio,
                        },
                        "field": {
                            "requested_start_T": self.start_field_t,
                            "requested_stop_T": self.stop_field_t,
                            "requested_return_T": self.start_field_t if len(legs) == 2 else None,
                        },
                        "spectra_start": int(metadata["spectra_written"]),
                        "spectra_written": 0, "file_status": "partial", "complete": False,
                    }
                    metadata["batch_file_paths"].append(str(batch_path))
                    metadata["file_details"].append(detail)
                    metadata["current_file_detail"] = detail
                    metadata["current_file_path"] = str(batch_path)
                    self._write_metadata(metadata_path, metadata)
                    turnaround_snapshot = None
                    with batch_path.open("w", newline="", encoding="utf-8") as stream:
                        writer = csv.writer(stream)
                        writer.writerow(CONTINUOUS_SCALAR_FIELDS + wavelengths)
                        stream.flush(); os.fsync(stream.fileno())
                        for leg, start, target, _direction in legs:
                            self._check_cancelled()
                            endpoint_snapshot = self._leg(
                                leg=leg, start=start, target=target, writer=writer,
                                stream=stream, wavelengths=wavelengths, metadata=metadata,
                                condition_index=condition_index, condition_count=len(enabled),
                                verified_start_snapshot=(turnaround_snapshot if leg == 2 else None),
                            )
                            endpoint_field, _ = self._snapshot_values(endpoint_snapshot)
                            completion_snapshot = endpoint_snapshot
                            detail["field"][f"{_direction}_actual_stop_T"] = endpoint_field
                            detail["field"]["actual_stop_T"] = endpoint_field
                            turnaround_snapshot = endpoint_snapshot if leg == 1 else None
                            metadata["completed_legs"] += 1
                            self._write_metadata(metadata_path, metadata)
                    detail["spectra_written"] = int(metadata["spectra_written"] - detail["spectra_start"])
                    detail["file_status"] = "complete"
                    detail["complete"] = True
                    self._write_metadata(metadata_path, metadata)
        except MCD2100Cancelled as exc:
            failure_exception = exc
            outcome, error = WorkflowOutcome.CANCELLED, str(exc)
        except BaseException as exc:
            failure_exception = exc
            outcome, error = WorkflowOutcome.FAILED, str(exc)
        finally:
            try:
                cleanup = getattr(self.optical, "cleanup", None)
                if callable(cleanup):
                    cleanup()
            except BaseException as exc:
                cleanup_error = f"optical cleanup: {exc}"
                outcome = WorkflowOutcome.FAILED
            if outcome is WorkflowOutcome.COMPLETED:
                try:
                    if self.temperature_control_enabled:
                        try:
                            metadata["final_temperature_snapshot"] = _jsonable(
                                self._read_temperature_snapshot()
                            )
                        except BaseException as exc:
                            metadata["final_temperature_snapshot_error"] = str(exc)
                    metadata["final_snapshot"] = metadata.get("snapshots", [])[-1] if metadata.get("snapshots") else None
                    metadata.pop("_path", None)
                    metadata.update({"status": WorkflowOutcome.COMPLETED.value,
                                     "error": None, "cleanup_error": None,
                                     "completion_utc": _utc_now()})
                    self._write_metadata(metadata_path, metadata)
                    detach = getattr(self.controller, "detach_completed_run_async", None)
                    if not callable(detach):
                        raise RuntimeError("controller does not expose completed-run detach")
                    self._execute_handle(detach(
                        self.start_field_t if (self.conditions or self.bidirectional) else self.stop_field_t,
                        self._gate(),
                        completion_snapshot,
                    ), self.cleanup_timeout_s)
                    detached_success = True
                except BaseException as exc:
                    error = str(exc)
                    outcome = WorkflowOutcome.FAILED
                    cleanup_error = f"completed detach: {exc}"
            endpoint_timeout_only = (
                isinstance(failure_exception, MCD2100EndpointTimeoutError)
                and cleanup_error is None
                and not self.stop_event.is_set()
                and self._stop_handle is None
            )
            prefield_temperature_failure = (
                isinstance(failure_exception, MCD2100TemperatureStabilizationError)
                and not self._magnet_command_issued
                and self._stop_handle is None
            )
            prefield_cancel = (
                isinstance(failure_exception, MCD2100Cancelled)
                and not self._magnet_command_issued
                and self._stop_handle is None
            )
            stop_required = (
                not detached_success
                and (outcome is not WorkflowOutcome.COMPLETED
                     or self._stop_handle is not None
                     or self.stop_event.is_set())
                and not endpoint_timeout_only
                and not prefield_temperature_failure
                and not prefield_cancel
            )
            if endpoint_timeout_only:
                metadata["stop_ack"] = None
                metadata["stop_error"] = None
                metadata["magnet_stop_requested"] = False
                metadata["magnet_stop_reason"] = "endpoint timing failure; field control left active"
            if prefield_temperature_failure or prefield_cancel:
                metadata["stop_ack"] = None
                metadata["stop_error"] = None
                metadata["magnet_stop_requested"] = False
                metadata["magnet_stop_reason"] = "no magnet command was issued"
            if stop_required:
                try:
                    metadata["magnet_stop_requested"] = True
                    metadata["magnet_stop_reason"] = (
                        "measurement cancelled" if outcome is WorkflowOutcome.CANCELLED
                        else "safety, control, or non-timing workflow failure"
                    )
                    self._drain_stop()
                except BaseException as exc:
                    cleanup_error = (cleanup_error + "; " if cleanup_error else "") + f"magnet stop: {exc}"
                    outcome = WorkflowOutcome.FAILED
            if not detached_success:
                metadata.pop("_path", None)
                metadata.update({"status": outcome.value, "error": error, "cleanup_error": cleanup_error})
                self._write_metadata(metadata_path, metadata)
        csv_paths = metadata.get("batch_file_paths") or [str(csv_path)]
        return {"status": outcome.value, "error": error, "cleanup_error": cleanup_error,
                "csv_path": str(csv_paths[0]), "csv_paths": list(csv_paths),
                "file_details": list(metadata.get("file_details", [])),
                "metadata_path": str(metadata_path),
                "spectra_written": metadata["spectra_written"],
                "completed_legs": metadata["completed_legs"]}
