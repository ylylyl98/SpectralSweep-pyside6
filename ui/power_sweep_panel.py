# ui/power_sweep_panel.py
# ──────────────────────────────────────────────────────────────────────────────
# Motion-dependent measurement panel.
#
# Moves a selected actuator (linear stage, rot1, or rot2) through user-defined
# positions. At each position:
#   1. Moves the selected actuator to the target position
#   2. Reads optical power from PM100D when connected (stored in µW)
#   3. Acquires a spectrum from the LF6 spectrometer
#   4. Saves a CSV row with metadata columns + wavelength spectrum
#
# Rules:
#   - No instrument state stored here.  All state lives in controllers/.
#   - All blocking operations run in a QThread worker.
#   - importlib.reload() safe.
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import ast
import csv
import json
import math
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional

import numpy as np
from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout, QGroupBox,
    QLabel, QPushButton, QLineEdit, QDoubleSpinBox, QSpinBox,
    QCheckBox, QSplitter, QScrollArea, QProgressBar,
    QTextEdit, QMessageBox, QFrame, QComboBox, QDialog, QSizePolicy, QButtonGroup,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pyqtgraph as pg
from utils.config import cfg
from app.lightfield_metadata import bind_lightfield_metadata, set_lightfield_context
from app.experiment_metadata import ExperimentMetadataService, instrument_inventory
from app.power_reading import power_correction_factor, power_reading_lock, read_power
from app.nd_calibration import make_power_points, positions_for_power, predict_power
from app.devices.motion_verification import MotionCancelledError, MotionVerificationConfig, move_and_verify
from utils.motion_conditions import (MODE_DOPING_EFIELD, build_conditions,
    expand_sequence, resolve_rotation_plan, sequence_preview, build_motion_plan,
    normalize_motion_axis, parse_motion_values, MOTION_NOT_USED, MOTION_HOLD, MOTION_FIXED, MOTION_SWEEP,
    MOTION_AXES)
from ui.motion_conditions_widget import MotionConditionsWidget
from ui.motion_sequence_preview import MotionSequencePreviewDialog
from ui.nd_calibration_widget import NDCalibrationWidget
from utils.filename_builder import (
    FilenameContext, build_base_filename, sanitize_token, format_compact_number,
)

pg.setConfigOption("background", "w")
pg.setConfigOption("foreground", "k")

# ── constants ──────────────────────────────────────────────────────────────────
NAN = float("nan")

_MOTION_SPECS = {
    "stage": {
        "label": "Linear Stage",
        "value_label": "Positions",
        "unit": "stage units",
        "column": "stage_pos",
    },
    "rot1": {
        "label": "Rot1",
        "value_label": "Angles",
        "unit": "deg",
        "column": "rot1_deg",
    },
    "rot2": {
        "label": "Rot2",
        "value_label": "Angles",
        "unit": "deg",
        "column": "rot2_deg",
    },
}
_ROTATION_MIN_DEG = -3600.0
_ROTATION_MAX_DEG = 3600.0
# Allow small hardware readback errors after a completed rotation move.
_ROTATION_POSITION_TOLERANCE_DEG = 0.01
_GATE_READBACK_RETRIES = 5
_GATE_READBACK_INTERVAL_S = 0.1


# ── stop exception ─────────────────────────────────────────────────────────────
class _StopRequested(Exception):
    pass


# ── position parsing ───────────────────────────────────────────────────────────
def _parse_sweep_values(text: str) -> np.ndarray:
    """Parse tuple-style linspace spec or a direct list.

    (0, 50, 51)   → np.linspace(0, 50, 51)
    [0, 2, 5, 10] → np.array([0., 2., 5., 10.])
    """
    text = text.strip()
    if not text:
        raise ValueError("Position input is empty.")

    node = ast.literal_eval(text)

    if isinstance(node, tuple) and len(node) == 3:
        start, stop, count = float(node[0]), float(node[1]), int(node[2])
        if count < 2:
            raise ValueError("Tuple count must be >= 2.")
        return np.linspace(start, stop, count)

    if isinstance(node, (list, tuple)):
        arr = np.array([float(x) for x in node], dtype=float)
        if arr.size == 0:
            raise ValueError("Position list is empty.")
        return arr

    try:
        return np.array([float(text)], dtype=float)
    except ValueError:
        raise ValueError(
            "Expected (start,stop,count), [v1,v2,...], or a single number."
        )


# Compatibility alias for callers/tests that used the old stage-specific name.
_parse_stage_positions = _parse_sweep_values


def _describe_positions(pos: np.ndarray, max_show: int = 5) -> str:
    if pos.size == 0:
        return "[]"
    if pos.size <= max_show * 2:
        return str([round(x, 4) for x in pos.tolist()])
    head = ", ".join(f"{x:.4g}" for x in pos[:max_show])
    tail = ", ".join(f"{x:.4g}" for x in pos[-2:])
    return f"[{head}, ..., {tail}]"


def _parse_power_values(text: str) -> np.ndarray:
    """Parse a power list using the same safe syntax as positions."""
    return _parse_sweep_values(text)


# ── SMU readback ───────────────────────────────────────────────────────────────
def _read_gates(iv) -> tuple[float, float]:
    if iv is None or not hasattr(iv, "read_current_gates"):
        return NAN, NAN
    try:
        bg, tg = iv.read_current_gates()
        return (
            float(bg) if bg is not None else NAN,
            float(tg) if tg is not None else NAN,
        )
    except Exception:
        return NAN, NAN


def _read_bias(iv) -> float:
    if iv is None or not hasattr(iv, "read_current_bias"):
        return NAN
    try:
        return float(iv.read_current_bias())
    except Exception:
        return NAN


def _read_currents(iv) -> tuple[float, float, float]:
    if iv is None or not hasattr(iv, "read_currents"):
        return NAN, NAN, NAN
    try:
        Ibg, Itg, Ib = iv.read_currents()

        def _clean(x):
            try:
                v = float(x)
                return v if math.isfinite(v) else NAN
            except Exception:
                return NAN

        return _clean(Ibg), _clean(Itg), _clean(Ib)
    except Exception:
        return NAN, NAN, NAN


def _read_currents_strict(iv) -> tuple[float, float, float]:
    """Read currents while preserving communication failures for new plans."""
    if iv is None or not callable(getattr(iv, "read_currents", None)):
        return NAN, NAN, NAN
    values = iv.read_currents(strict=True)
    if values is None or len(values) != 3:
        raise RuntimeError("SMU current read returned incomplete data")
    cleaned = tuple(NAN if value is None and index == 2 else float(value)
                    for index, value in enumerate(values))
    if any(not math.isfinite(value) for index, value in enumerate(cleaned)
           if index < 2) or (not math.isfinite(cleaned[2]) and
                             callable(getattr(iv, "has_role", None)) and
                             iv.has_role("Vbias")):
        raise RuntimeError("SMU current read returned nonfinite data")
    return cleaned
    try:
        Ibg, Itg, Ib = iv.read_currents()

        def _clean(x):
            try:
                v = float(x)
                return v if math.isfinite(v) else NAN
            except Exception:
                return NAN

        return _clean(Ibg), _clean(Itg), _clean(Ib)
    except Exception:
        return NAN, NAN, NAN


# ── CSV helpers ────────────────────────────────────────────────────────────────
_SMU_COLUMNS = [
    "Vbg_set", "Vbg_meas",
    "Vtg_set", "Vtg_meas",
    "Vbias_set", "Vbias_meas",
    "Ibg", "Itg", "Ibias",
]


def _scalar_column_names(
    smu_available: bool,
    pm_available: bool = True,
    motion_column: str = "stage_pos",
    target_power: bool = False,
    condition_metadata: bool = False,
) -> list[str]:
    cols = []
    if pm_available:
        if target_power:
            cols.append("Target_power_uW")
        cols.extend(["Power_uW", "Power_raw_uW", "Power_correction_factor"])
    cols.extend([motion_column, f"{motion_column}_actual"])
    if smu_available:
        cols.extend(_SMU_COLUMNS)
    if condition_metadata:
        cols.extend(["condition_id", "condition_index", "condition_label", "doping_v", "efield_v",
                     "condition_vtg", "condition_vbg", "condition_vbias",
                     "rotation_requested", "rotation_actual", "rot1_requested", "rot1_actual",
                     "rot2_requested", "rot2_actual", "sequence", "repeat"])
    return cols


def _read_scalar_row(
    iv,
    power_uw: float,
    target: float,
    actual: float,
    smu_available: bool,
    pm_available: bool = True,
    Vbg_set: float = NAN,
    Vtg_set: float = NAN,
    Vbias_set: float = NAN,
    raw_power_uw: float = NAN,
    correction_factor: float = 1.0,
    target_power_uw: float = NAN,
    target_power_enabled: bool = False,
) -> list:
    values = []
    if pm_available:
        # Keep the target column present for every row whenever the header
        # contains it, including a blank target for a malformed partial plan.
        if target_power_enabled or math.isfinite(target_power_uw):
            values.append(target_power_uw)
        values.extend([power_uw, raw_power_uw, correction_factor])
    values.extend([target, actual])
    if smu_available and iv is not None:
        vbg_m, vtg_m = _read_gates(iv)
        vbias_m = _read_bias(iv)
        Ibg, Itg, Ib = _read_currents(iv)
        values.extend([
            Vbg_set, vbg_m,
            Vtg_set, vtg_m,
            Vbias_set, vbias_m,
            Ibg, Itg, Ib,
        ])
    return values


def _csv_cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if not math.isfinite(x):
        return ""
    return format(x, ".15g")


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _cancelable_sleep(seconds: float, stop_event: threading.Event, *, quantum: float = 0.02) -> None:
    """Sleep in short slices so a stop request interrupts settling promptly."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        if stop_event.is_set():
            raise _StopRequested()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        stop_event.wait(min(quantum, remaining))


def _build_gate_token(Vbg: float, Vtg: float, Vbias: float) -> str:
    """Gate-voltage filename token: Vbg-2p5_Vtg+1p0_Vb0"""
    parts = []
    for name, val in [("Vbg", Vbg), ("Vtg", Vtg), ("Vb", Vbias)]:
        if not math.isfinite(val):
            continue
        txt = format_compact_number(val, keep_sign=True, decimals=2)
        if txt:
            parts.append(f"{name}{txt}")
    return "_".join(parts)


# ── worker ─────────────────────────────────────────────────────────────────────
class _PowerSweepWorker(QObject):
    """Runs the motion-dependent measurement loop in a background QThread."""

    log = Signal(str)
    progress = Signal(int, int)
    spectrum = Signal(object, object)  # wl, cts
    finished = Signal()
    error = Signal(str)

    def __init__(
        self, params: dict, stage_ctrl, rotation_ctrl, pm_ctrl, lf6_ctrl,
        smu_ctrl,
        experiment_run=None,
    ):
        super().__init__()
        self._p = dict(params)
        self._p["power_correction_factor"] = power_correction_factor(params.get("power_correction_factor"))
        self._stg = stage_ctrl
        self._rot = rotation_ctrl
        self._pm = pm_ctrl
        self._lf6 = lf6_ctrl
        self._smu = smu_ctrl
        self._experiment_run = experiment_run
        self._stop = threading.Event()
        self._progress_offset = 0
        self._progress_total_override = None

    def request_stop(self):
        self._stop.set()
        self.log.emit(f"[{_ts()}] Stop requested — will halt after current position.")

    @Slot()
    def run(self):
        try:
            self._run_sweep(self._p)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()

    def _run_sweep(self, p):
        p.setdefault("power_correction_factor", self._p.get("power_correction_factor", 1.0))
        if p.get("plan_schema") != "legacy" and p.get("motion_axes") is not None:
            return self._run_motion_axes(p)
        # A multi-condition plan is expanded into ordinary full sweeps.  This
        # keeps the proven acquisition path and its cleanup policies while
        # making every condition independently inspectable on disk.
        if "condition_sequence" in p:
            return self._run_condition_sequence(p)
        motion_key = p.get("motion_key", "stage")
        motion_spec = _MOTION_SPECS[motion_key]
        if motion_key == "stage":
            motion = self._stg.adapter
        else:
            motion = self._rot.adapter(motion_key)
        motion_label = motion_spec["label"]
        motion_unit = (
            getattr(motion, "position_unit", motion_spec["unit"])
            if motion_key == "stage"
            else motion_spec["unit"]
        )
        start_position = float(motion.get_position())
        if p.get("return_motion_to_start", True):
            start_position = self._validated_restore_position(motion_key, motion, start_position)

        pm_ok = (
            self._pm is not None
            and getattr(self._pm, "is_connected", False)
        )
        pm = self._pm.adapter if pm_ok else None
        spec = self._lf6.adapter
        setup = self._lf6.setup

        smu_ok = (
            self._smu is not None
            and getattr(self._smu, "is_connected", False)
        )
        iv = self._smu.device if smu_ok else None

        # ── apply LF6 settings ───────────────────────────────────────────────
        if setup is not None:
            try:
                prepare = getattr(self._lf6, "configure_for_acquisition", None)
                if callable(prepare):
                    prepare(center_nm=float(p["center_nm"]), exposure_ms=float(p["exp_ms"]), frames=int(p["frames"]))
                else:
                    raise RuntimeError("LF6 acquisition preparation surface is unavailable")
            except Exception as exc:
                self.log.emit(f"[{_ts()}] LF6 settings warning: {exc}")
                if p.get("strict_sequence"):
                    raise RuntimeError(f"LF6 acquisition preparation failed: {exc}") from exc

        # ── wavelength calibration ───────────────────────────────────────────
        self.log.emit(f"[{_ts()}] Acquiring wavelength calibration...")
        wls = np.array([])
        if spec is not None:
            try:
                wls = spec.calibration_wavelengths(force=False)
            except Exception:
                pass
        if wls.size <= 2 and setup is not None:
            try:
                wls = np.asarray(
                    setup.get_wavelength_calibration(), dtype=float
                ).ravel()
            except Exception:
                pass
        if wls.size <= 2:
            raise RuntimeError(
                "Could not obtain wavelength calibration. Aborting."
            )

        # ── set PM wavelength ────────────────────────────────────────────────
        if pm is None:
            self.log.emit(
                f"[{_ts()}] NOTICE: PM100D is not connected. "
                "The sweep will continue without saving optical power values."
            )
        else:
            try:
                with power_reading_lock:
                    pm.configure_wavelength(float(p["pm_wl_nm"]))
                self.log.emit(
                    f"[{_ts()}] PM wavelength set to "
                    f"{p['pm_wl_nm']:.1f} nm"
                )
            except Exception as exc:
                self.log.emit(f"[{_ts()}] PM wavelength warning: {exc}")

        # ── apply gate voltages ──────────────────────────────────────────────
        if p.get("apply_gates") and iv is not None:
            self.log.emit(f"[{_ts()}] Ramping gates...")
            ramp = p.get("ramp_step_V", float(getattr(cfg.ramp, "step_V", 0.1)))
            delay = p.get("step_delay_s", ramp / 5.0)
            try:
                iv.set_gates(
                    Vtg=p["Vtg_target"],
                    Vbg=p["Vbg_target"],
                    ramp_step=ramp,
                    delay_s=delay,
                    stop_cb=self._stop.is_set,
                    stop_exc=_StopRequested,
                )
                if hasattr(iv, "set_bias"):
                    iv.set_bias(
                        Vbias=p["Vbias_target"],
                        ramp_step=p.get("vbias_step_V", ramp),
                        delay_s=delay,
                        stop_cb=self._stop.is_set,
                        stop_exc=_StopRequested,
                    )
                _cancelable_sleep(p.get("settle_s", float(getattr(cfg.ramp, "settle_s", 0.05))), self._stop)
                vbg_m, vtg_m = _read_gates(iv)
                vbias_m = _read_bias(iv)
                self.log.emit(
                    f"[{_ts()}] Gates set — "
                    f"Vbg={vbg_m:.4f}, Vtg={vtg_m:.4f}, Vbias={vbias_m:.4f}"
                )
            except _StopRequested:
                raise
            except Exception as exc:
                self.log.emit(f"[{_ts()}] Gate ramp error: {exc}")
                if p.get("strict_gate_errors"):
                    raise RuntimeError(f"Gate transition failed: {exc}") from exc

        # ── create output file ───────────────────────────────────────────────
        out_path = Path(p["out_path"])
        out_path.mkdir(parents=True, exist_ok=True)
        fp = out_path / f"{p['base_name']}.csv"
        k = 2
        while fp.exists():
            fp = out_path / f"{p['base_name']}_{k:03d}.csv"
            k += 1
        self._last_output_path = fp

        cols = _scalar_column_names(
            smu_ok,
            pm_available=pm_ok,
            motion_column=motion_spec["column"],
            target_power=bool(p.get("target_powers")) and pm_ok,
            condition_metadata=bool(p.get("condition_metadata")),
        )
        wl_headers = [f"{float(w):.4f}" for w in wls]

        with open(fp, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(cols + wl_headers)
        self.log.emit(f"[{_ts()}] Writing to {fp.name}")

        # ── main loop ────────────────────────────────────────────────────────
        positions = p["positions"]
        total = len(positions)
        nwls = len(wls)

        failed = False
        try:
            with open(fp, "a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                for done, pos in enumerate(positions, start=1):
                    if self._stop.is_set():
                        raise _StopRequested()

                    self.log.emit(
                        f"[{_ts()}] {done}/{total}: moving "
                        f"{motion_label} to {pos:.3f} {motion_unit}"
                    )

                    # move selected actuator
                    move_ok = True
                    arrived = NAN
                    try:
                        if motion_key in ("rot1", "rot2"):
                            verified = self._sequence_apply_rotations(p, {motion_key: float(pos)}, {})
                            arrived = verified[f"{motion_key}_actual"]
                        else:
                            arrived = move_and_verify(
                                motion,
                                float(pos),
                                stop_event=self._stop,
                                config=MotionVerificationConfig(
                                    settling_s=max(0.0, float(p.get("motion_settle_s", 0.3))),
                                ),
                            )
                        self.log.emit(
                            f"[{_ts()}]   arrived at {arrived:.3f} "
                            f"{motion_unit}"
                        )
                    except _StopRequested:
                        raise
                    except Exception as exc:
                        self.log.emit(
                            f"[{_ts()}]   {motion_label} error: {exc}"
                        )
                        move_ok = False
                        if p.get("strict_sequence") or motion_key in ("rot1", "rot2"):
                            raise RuntimeError(f"{motion_label} move failed: {exc}") from exc

                    # read power
                    power_uw = NAN
                    raw_power_uw = NAN
                    if move_ok and pm is not None:
                        try:
                            reading = read_power(pm, factor=p["power_correction_factor"])
                            raw_power_uw = reading.raw_w * 1e6
                            power_uw = reading.corrected_w * 1e6
                            self.log.emit(
                                f"[{_ts()}]   power: {power_uw:.3f} µW"
                            )
                        except Exception as exc:
                            self.log.emit(
                                f"[{_ts()}]   power error: {exc}"
                            )

                    # acquire spectrum
                    row_rotation_actual = p.get("rotation_actual", NAN)
                    row_rot_actual = {}
                    row_gate_observed = {}
                    if move_ok:
                        try:
                            if p.get("strict_sequence") and self._rot is not None:
                                for row_axis in ("rot1", "rot2"):
                                    if math.isfinite(float(p.get(f"{row_axis}_requested", NAN))):
                                        try:
                                            row_rot_actual[row_axis] = float(self._rot.adapter(row_axis).get_position())
                                        except Exception:
                                            row_rot_actual[row_axis] = NAN
                            if iv is not None:
                                row_bg, row_tg = _read_gates(iv)
                                row_gate_observed = {"Vbg_observed": row_bg, "Vtg_observed": row_tg,
                                                     "Vbias_observed": _read_bias(iv)}
                            row_rotation_actual = (row_rot_actual.get("rot1", p.get("rot1_actual", p.get("rotation_actual")))
                                                   if p.get("rotation_axis") == "rot1" else
                                                   row_rot_actual.get("rot2", p.get("rot2_actual", p.get("rotation_actual"))))
                            set_lightfield_context(self._lf6, output_file=fp, point_index=done, position=float(pos),
                                                   condition_id=p.get("condition_id"), sequence=p.get("sequence"),
                                                   repeat=p.get("repeat"), rotation_requested=p.get("rotation_requested"),
                                                   rotation_actual=row_rotation_actual,
                                                   rot1_requested=p.get("rot1_requested"), rot1_actual=row_rot_actual.get("rot1", p.get("rot1_actual")),
                                                   rot2_requested=p.get("rot2_requested"), rot2_actual=row_rot_actual.get("rot2", p.get("rot2_actual")),
                                                   Vtg=p.get("Vtg_target"), Vbg=p.get("Vbg_target"), Vbias=p.get("Vbias_target"),
                                                   Vtg_observed=row_gate_observed.get("Vtg_observed", p.get("Vtg_observed")),
                                                   Vbg_observed=row_gate_observed.get("Vbg_observed", p.get("Vbg_observed")),
                                                   Vbias_observed=row_gate_observed.get("Vbias_observed", p.get("Vbias_observed")))
                            wl, y = spec.acquire()
                            self.spectrum.emit(wl, y)
                        except Exception as exc:
                            self.log.emit(
                                f"[{_ts()}]   acquire error: {exc}"
                            )
                            y = np.full(nwls, NAN, dtype=float)
                            if p.get("strict_sequence"):
                                raise RuntimeError(f"LF6 acquisition failed: {exc}") from exc
                    else:
                        y = np.full(nwls, NAN, dtype=float)
                        if p.get("strict_sequence"):
                            raise RuntimeError(f"{motion_label} move failed; acquisition skipped")

                    if p.get("strict_sequence") and (
                        np.asarray(y).size != nwls or np.any(~np.isfinite(np.asarray(y, dtype=float)))
                    ):
                        raise RuntimeError("LF6 acquisition returned invalid spectral data")

                    # write row
                    row_rot1_actual = (row_rot_actual.get("rot1", p.get("rot1_actual", NAN))
                                       if move_ok else p.get("rot1_actual", NAN))
                    row_rot2_actual = (row_rot_actual.get("rot2", p.get("rot2_actual", NAN))
                                       if move_ok else p.get("rot2_actual", NAN))
                    scalar_vals = _read_scalar_row(
                        iv, power_uw, pos, arrived, smu_ok,
                        pm_available=pm_ok,
                        raw_power_uw=raw_power_uw,
                        correction_factor=p["power_correction_factor"],
                        target_power_uw=(
                            p["target_powers"][done - 1]
                            if p.get("target_powers") and done <= len(p["target_powers"])
                            else NAN
                        ),
                        target_power_enabled=bool(p.get("target_powers")) and pm_ok,
                        Vbg_set=p.get("Vbg_target", NAN),
                        Vtg_set=p.get("Vtg_target", NAN),
                        Vbias_set=p.get("Vbias_target", NAN),
                    )
                    if p.get("condition_metadata"):
                        scalar_vals.extend([
                            p.get("condition_id", ""), p.get("condition_index", ""), p.get("condition_label", ""),
                            p.get("doping_v", NAN), p.get("efield_v", NAN),
                            p.get("Vtg_target", NAN), p.get("Vbg_target", NAN), p.get("Vbias_target", NAN),
                            p.get("rotation_requested", NAN), row_rotation_actual if move_ok else p.get("rotation_actual", NAN),
                            p.get("rot1_requested", NAN), row_rot1_actual,
                            p.get("rot2_requested", NAN), row_rot2_actual,
                            p.get("sequence", ""), p.get("repeat", ""),
                        ])
                    row = [_csv_cell(v) for v in scalar_vals]
                    row.extend(_csv_cell(float(v)) for v in y)
                    writer.writerow(row)
                    fh.flush()

                    progress_total = self._progress_total_override or total
                    self.progress.emit(self._progress_offset + done, progress_total)

        except _StopRequested:
            self.log.emit(f"[{_ts()}] Stopped by user.")
        except Exception:
            failed = True
            raise

        finally:
            # return gates to zero
            if failed and iv is not None:
                self.log.emit(
                    f"[{_ts()}] Sweep failed; preserving the last commanded "
                    "SMU state. No automatic gate cleanup was sent."
                )
            elif p.get("return_to_zero", True) and iv is not None:
                try:
                    zero_result = iv.ramp_all_to_zero(
                        ramp_step=p.get("ramp_step_V", 0.1),
                        delay_s=p.get("step_delay_s", 0.02),
                    )
                    if zero_result:
                        raise RuntimeError(str(zero_result))
                    self.log.emit(
                        f"[{_ts()}] Gates returned to 0 V."
                    )
                except Exception as exc:
                    self.log.emit(
                        f"[{_ts()}] Gate return-to-zero failed: {exc}"
                    )
            # restore the selected actuator to its pre-sweep position
            if p.get("return_motion_to_start", True):
                try:
                    move_and_verify(
                        motion,
                        float(start_position),
                        config=MotionVerificationConfig(settling_s=0.0),
                    )
                    self.log.emit(
                        f"[{_ts()}] {motion_label} returned to its starting "
                        f"position ({start_position:.3f} {motion_unit})."
                    )
                except Exception as exc:
                    self.log.emit(
                        f"[{_ts()}] {motion_label} return failed: {exc}"
                    )

        if self._stop.is_set():
            self.log.emit(
                f"[{_ts()}] Stopped. Partial data saved → {fp.name}"
            )
        else:
            self.log.emit(f"[{_ts()}] Done. Saved → {fp.name}")
        return fp

    def _run_motion_axes(self, p: dict):
        """Execute the versioned three-axis plan in one CSV.

        This path performs all connection/readback checks before the first
        command.  It intentionally does not reuse the cached-position
        behavior of legacy adapters: every commanded point gets a fresh
        finite readback and the same rotation tolerance as the old path.
        """
        raw_axes = p.get("motion_axes") or {}
        if not isinstance(raw_axes, Mapping):
            raise ValueError("motion_axes must be a mapping")
        conditions = list(p.get("conditions") or [])
        if p.get("apply_gates") and not conditions:
            conditions = [{"enabled": True, "vtg_v": p.get("Vtg_target", 0.0),
                           "vbg_v": p.get("Vbg_target", 0.0), "vbias_v": p.get("Vbias_target", 0.0)}]
        if not p.get("apply_gates"):
            conditions = [{}]
        plan = build_motion_plan(raw_axes, conditions, repeat=p.get("repeat", p.get("repeats", 1)),
                                 maximum_entries=int(p.get("maximum_entries", 100000)),
                                 axis_order=p.get("axis_order"))
        adapters = {}
        hold_positions = {}
        for axis, spec in plan["axes"].items():
            mode = spec["mode"]
            if mode == MOTION_NOT_USED:
                continue
            if axis == "stage":
                ctrl = self._stg
                adapter = getattr(ctrl, "adapter", None) if ctrl is not None and bool(getattr(ctrl, "is_connected", False)) else None
            else:
                adapter = None
                if self._rot is not None and callable(getattr(self._rot, "adapter", None)):
                    try:
                        adapter = self._rot.adapter(axis) if self._rot.is_connected(axis) else None
                    except Exception:
                        adapter = None
            if adapter is None:
                raise RuntimeError(f"{axis} is required by the motion plan but is not connected")
            adapters[axis] = adapter
            if mode == MOTION_HOLD:
                try:
                    held = float(adapter.get_position())
                except Exception as exc:
                    raise RuntimeError(f"{axis} Hold position read failed: {exc}") from exc
                if not math.isfinite(held):
                    raise RuntimeError(f"{axis} Hold position readback is nonfinite")
                hold_positions[axis] = held
            lower = float(getattr(adapter, "minimum_position", _ROTATION_MIN_DEG if axis != "stage" else -math.inf))
            upper = float(getattr(adapter, "maximum_position", _ROTATION_MAX_DEG if axis != "stage" else math.inf))
            for value in spec["values"]:
                if not math.isfinite(float(value)) or float(value) < lower or float(value) > upper:
                    raise ValueError(f"{axis} value {value:g} is outside [{lower:g}, {upper:g}]")
        if self._lf6 is None or not bool(getattr(self._lf6, "is_connected", False)) or getattr(self._lf6, "adapter", None) is None:
            raise RuntimeError("Motion sweep requires a connected LF6")
        prepare = getattr(self._lf6, "configure_for_acquisition", None)
        if callable(prepare):
            try:
                prepare(center_nm=float(p["center_nm"]), exposure_ms=float(p["exp_ms"]), frames=int(p["frames"]))
            except Exception as exc:
                raise RuntimeError(f"LF6 acquisition preparation failed: {exc}") from exc
        else:
            raise RuntimeError("LF6 acquisition preparation surface is unavailable")
        pm_adapter = self._pm.adapter if self._pm is not None and bool(getattr(self._pm, "is_connected", False)) else None
        if pm_adapter is not None:
            configure_pm = getattr(pm_adapter, "configure_wavelength", None)
            if callable(configure_pm):
                try:
                    with power_reading_lock:
                        configure_pm(float(p["pm_wl_nm"]))
                except Exception as exc:
                    raise RuntimeError(f"PM wavelength configuration failed: {exc}") from exc
        iv = None
        if p.get("apply_gates"):
            if self._smu is None or not bool(getattr(self._smu, "is_connected", False)) or getattr(self._smu, "device", None) is None:
                raise RuntimeError("Motion sweep gate conditions require a connected SMU")
            iv = self._smu.device
            # Resolve channel roles and health before any gate or motion
            # command.  A zero Vbias is optional only when the device
            # explicitly reports that no Vbias role exists.
            checker = getattr(iv, "role_is_available", None) or getattr(iv, "has_role", None)
            health = getattr(iv, "health_states", {})
            for role in ("Vbg", "Vtg", "Vbias"):
                needed = role != "Vbias" or any(float(c.get("vbias_v", 0.0)) != 0.0 for c in conditions)
                available = None
                if callable(checker):
                    try: available = bool(checker(role))
                    except Exception: available = False
                if role == "Vbias" and not needed and available is False:
                    continue
                if available is False:
                    raise RuntimeError(f"Required SMU channel role {role} is unavailable")
                if isinstance(health, Mapping) and health.get(role, "ready") != "ready":
                    raise RuntimeError(f"SMU channel role {role} is not ready; reconnect required")
            limit = abs(float(getattr(getattr(self._smu, "limits", None), "volt_compliance_V", getattr(cfg.smu, "volt_compliance_V", 20.0))))
            for index, condition in enumerate(conditions, 1):
                for key in ("vtg_v", "vbg_v", "vbias_v"):
                    value = float(condition.get(key, 0.0))
                    if not math.isfinite(value) or abs(value) > limit + 1e-9:
                        raise ValueError(f"condition {index} {key} exceeds SMU voltage compliance")
        spec_lf6 = self._lf6.adapter
        try:
            wls = np.asarray(spec_lf6.calibration_wavelengths(force=False), dtype=float).ravel()
        except Exception:
            wls = np.array([])
        if wls.size <= 2:
            try:
                wls = np.asarray(self._lf6.setup.get_wavelength_calibration(), dtype=float).ravel()
            except Exception:
                pass
        if wls.size <= 2 or np.any(~np.isfinite(wls)):
            raise RuntimeError("Could not obtain wavelength calibration. Aborting.")
        out_path = Path(p["out_path"]); out_path.mkdir(parents=True, exist_ok=True)
        fp = out_path / f"{p['base_name']}.csv"; suffix = 2
        while fp.exists():
            fp = out_path / f"{p['base_name']}_{suffix:03d}.csv"; suffix += 1
        axis_columns = []
        for axis in plan["axis_order"]:
            if plan["axes"][axis]["mode"] != MOTION_NOT_USED:
                axis_columns.extend([f"{axis}_requested", f"{axis}_actual"])
        pm_available = self._pm is not None and bool(getattr(self._pm, "is_connected", False))
        headers = (["Target_power_uW"] if pm_available and p.get("target_powers") else [])
        headers += (["Power_uW", "Power_raw_uW", "Power_correction_factor"] if pm_available else [])
        headers += axis_columns
        if iv is not None: headers += _SMU_COLUMNS
        headers += ["condition_id", "condition_index", "condition_label", "doping_v", "efield_v",
                    "condition_vtg", "condition_vbg", "condition_vbias", "sequence", "repeat"]
        headers += [f"{float(w):.4f}" for w in wls]
        manifest_path = out_path / f"{p['base_name']}_manifest.json"
        manifest = {"version": plan["version"], "status": "running", "acquisition_status": "running",
                    "axis_order": plan["axis_order"], "axes": plan["axes"], "count": plan["count"],
                    "repeat": plan["repeat"], "file": str(fp), "entries": [],
                    "cleanup_status": "pending", "cleanup_errors": []}
        for entry in plan["entries"]:
            manifest["entries"].append({"sequence": entry["sequence"], "condition_index": entry["condition_index"] + 1,
                                        "repeat": entry["repeat"], "targets": entry["targets"], "status": "not_started"})
        self._atomic_manifest_write(manifest_path, manifest)
        initial = {}
        restore = {}
        try:
            for axis, adapter in adapters.items():
                try: value = float(adapter.get_position())
                except Exception as exc: raise RuntimeError(f"initial {axis} position read failed: {exc}") from exc
                if not math.isfinite(value): raise RuntimeError(f"initial {axis} position is nonfinite")
                initial[axis] = value
                if p.get("return_motion_to_start", True):
                    restore[axis] = self._validated_restore_position(axis, adapter, value)
        except Exception as exc:
            manifest["status"] = "failed"; manifest["acquisition_status"] = "not_started"; manifest["error"] = str(exc)
            self._atomic_manifest_write(manifest_path, manifest)
            raise
        manifest["initial_motion_positions"] = initial; manifest["restore_targets"] = restore
        self._atomic_manifest_write(manifest_path, manifest)
        cleanup_errors = []
        current_targets = dict(hold_positions)
        commanded_targets = dict(hold_positions)
        last_rotation = {}
        moved_axes = set()
        current_gate = None
        complete = False
        stopped_cleanly = False
        try:
            with open(fp, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh); writer.writerow(headers)
                for number, entry in enumerate(plan["entries"], 1):
                    if self._stop.is_set(): raise _StopRequested()
                    row_manifest = manifest["entries"][number - 1]; row_manifest["status"] = "running"
                    condition = entry["condition"]
                    gate = (float(condition.get("vbg_v", 0.0)), float(condition.get("vtg_v", 0.0)), float(condition.get("vbias_v", 0.0)))
                    if iv is not None and (current_gate is None or any(not math.isclose(a, b, abs_tol=1e-9) for a,b in zip(current_gate, gate))):
                        iv.set_gates(Vtg=gate[1], Vbg=gate[0], ramp_step=p.get("ramp_step_V", 0.1),
                                     delay_s=p.get("step_delay_s", 0.02), stop_cb=self._stop.is_set, stop_exc=_StopRequested)
                        if hasattr(iv, "set_bias"):
                            iv.set_bias(Vbias=gate[2], ramp_step=p.get("vbias_step_V", p.get("ramp_step_V", 0.1)),
                                        delay_s=p.get("step_delay_s", 0.02), stop_cb=self._stop.is_set, stop_exc=_StopRequested)
                        _cancelable_sleep(p.get("settle_s", 0.0), self._stop)
                        observed_gate = self._sequence_verify_gates(gate)
                        current_gate = gate
                    else:
                        observed_gate = self._sequence_verify_gates(gate) if iv is not None else {}
                    requested = dict(entry["targets"])
                    actual = dict(current_targets)
                    for axis, target in requested.items():
                        if self._stop.is_set(): raise _StopRequested()
                        if target is None: continue
                        adapter = adapters[axis]
                        if axis in ("rot1", "rot2"):
                            # Shared verification owns read retries and the single
                            # correction permitted after confirmed stopped status.
                            if axis not in last_rotation or not math.isclose(last_rotation[axis], float(target), rel_tol=0.0, abs_tol=_ROTATION_POSITION_TOLERANCE_DEG):
                                moved_axes.add(axis)
                            last_rotation = self._sequence_apply_rotations(
                                {**p, "rotation_settle_s": p.get("motion_settle_s", 0.0)},
                                {axis: float(target)}, last_rotation,
                            )
                            observed = last_rotation[f"{axis}_actual"]
                        else:
                            target = float(target)
                            should_move = axis not in commanded_targets or not math.isclose(commanded_targets[axis], target, rel_tol=0.0, abs_tol=1e-9)
                            if should_move:
                                moved_axes.add(axis)
                                move_and_verify(
                                    adapter, target, stop_event=self._stop,
                                    config=MotionVerificationConfig(settling_s=float(p.get("motion_settle_s", 0.0))),
                                )
                            try: observed = float(adapter.get_position())
                            except Exception as exc: raise RuntimeError(f"{axis} readback failed: {exc}") from exc
                            if not math.isfinite(observed): raise RuntimeError(f"{axis} readback is nonfinite")
                            commanded_targets[axis] = target
                        actual[axis] = observed; current_targets[axis] = observed
                    # Hold is deliberately motionless but its readback is
                    # refreshed for every acquisition; cached startup values
                    # cannot masquerade as an actual position.
                    for axis in hold_positions:
                        try: observed = float(adapters[axis].get_position())
                        except Exception as exc: raise RuntimeError(f"{axis} Hold readback failed: {exc}") from exc
                        if not math.isfinite(observed): raise RuntimeError(f"{axis} Hold readback is nonfinite")
                        actual[axis] = observed
                    if self._stop.is_set(): raise _StopRequested()
                    cond_id = f"condition-{entry['condition_index'] + 1:04d}"
                    power = raw = NAN
                    if pm_available:
                        try:
                            reading = read_power(self._pm.adapter, factor=p.get("power_correction_factor", 1.0)); raw = reading.raw_w * 1e6; power = reading.corrected_w * 1e6
                        except Exception: pass
                    if self._stop.is_set(): raise _StopRequested()
                    if bool(getattr(iv, "io_frozen", False)) or bool(getattr(iv, "requires_reconnect", False)):
                        raise RuntimeError("SMU is frozen or requires reconnect; acquisition stopped")
                    context_position = next(iter(actual.values()), 0.0)
                    set_lightfield_context(self._lf6, output_file=fp, point_index=number, position=float(context_position), condition_id=cond_id, sequence=number, repeat=entry["repeat"])
                    wl, y = spec_lf6.acquire(); y = np.asarray(y, dtype=float)
                    if y.size != wls.size or np.any(~np.isfinite(y)): raise RuntimeError("LF6 acquisition returned invalid spectral data")
                    vals = []
                    if pm_available and p.get("target_powers"):
                        target_powers = p["target_powers"]
                        stage_target = requested.get("stage")
                        try:
                            positions = list(p.get("positions") or [])
                            power_index = min(range(len(positions)), key=lambda i: abs(float(positions[i]) - float(stage_target))) if stage_target is not None and positions else number - 1
                        except Exception:
                            power_index = number - 1
                        vals.append(target_powers[power_index] if 0 <= power_index < len(target_powers) else NAN)
                    if pm_available: vals += [power, raw, p.get("power_correction_factor", 1.0)]
                    for axis in plan["axis_order"]:
                        if plan["axes"][axis]["mode"] == MOTION_NOT_USED: continue
                        vals += [requested.get(axis, hold_positions.get(axis, NAN)), actual.get(axis, hold_positions.get(axis, NAN))]
                    if iv is not None:
                        vals += [gate[0], observed_gate.get("Vbg_observed", NAN), gate[1], observed_gate.get("Vtg_observed", NAN), gate[2], observed_gate.get("Vbias_observed", NAN), *_read_currents_strict(iv)]
                    vals += [cond_id, entry["condition_index"] + 1, condition.get("label", ""), condition.get("doping_v", NAN), condition.get("efield_v", NAN), gate[1], gate[0], gate[2], number, entry["repeat"]]
                    writer.writerow([_csv_cell(v) for v in vals] + [_csv_cell(v) for v in y]); fh.flush()
                    row_manifest.update({"status": "complete", "condition_id": cond_id})
                    self.progress.emit(number, len(plan["entries"]))
            complete = True; manifest["status"] = "complete"; manifest["acquisition_status"] = "complete"
        except _StopRequested:
            manifest["status"] = "stopped"; manifest["acquisition_status"] = "partial"
            stopped_cleanly = True
        except Exception as exc:
            manifest["status"] = "failed"; manifest["acquisition_status"] = "partial"; manifest["error"] = str(exc)
            raise
        finally:
            if p.get("return_to_zero", True) and iv is not None and (complete or stopped_cleanly):
                try:
                    result = iv.ramp_all_to_zero(ramp_step=p.get("ramp_step_V", 0.1), delay_s=p.get("step_delay_s", 0.02))
                    if result: cleanup_errors.append(f"Gate return-to-zero: {result}")
                except Exception as exc: cleanup_errors.append(f"Gate return-to-zero failed: {exc}")
            if p.get("return_motion_to_start", True):
                for axis, value in restore.items():
                    if axis not in moved_axes:
                        continue
                    try: move_and_verify(adapters[axis], value)
                    except Exception as exc: cleanup_errors.append(f"{axis} restore failed: {exc}")
            manifest["cleanup_errors"] = cleanup_errors; manifest["cleanup_status"] = "failed" if cleanup_errors else "complete"
            self._atomic_manifest_write(manifest_path, manifest)
        return manifest_path

    def _run_condition_sequence(self, p):
        """Run a planned batch while keeping transitions outside child sweeps."""
        sequence = list(p.get("condition_sequence", ()))
        conditions = list(p.get("conditions", ()))
        self._sequence_preflight(p, sequence, conditions)
        axes = self._sequence_rotation_axes(p)
        values = self._sequence_rotation_values(p, axes)
        out_path = Path(p["out_path"])
        out_path.mkdir(parents=True, exist_ok=True)
        manifest_path = out_path / f"{p['base_name']}_manifest.json"
        suffix = 2
        while manifest_path.exists():
            manifest_path = out_path / f"{p['base_name']}_manifest_{suffix:03d}.json"
            suffix += 1
        entries = []
        for number, item in enumerate(sequence, 1):
            ci = int(item["condition_index"])
            requested = {axis: self._requested_rotation(item, axis, axes, values) for axis in axes}
            entries.append({"sequence": number, "condition_index": ci + 1,
                            "condition_id": f"sequence-{number:04d}-condition-{ci + 1:04d}",
                            "repeat": int(item.get("repeat", 1)), "rotation_requested": requested,
                            "status": "not_started", "file": None})
        manifest = {"status": "running", "base_name": p["base_name"],
                    "requested_rotation_axes": axes, "sequence": sequence,
                    "total_sweeps": len(sequence), "points_per_sweep": len(p["positions"]),
                    "total_points": len(sequence) * len(p["positions"]),
                    "conditions": entries, "files": []}
        self._atomic_manifest_write(manifest_path, manifest)
        try:
            restore = self._capture_motion_positions(p, axes)
            manifest["initial_motion_positions"] = self._initial_motion_positions
            manifest["restore_targets"] = restore if p.get("return_motion_to_start", True) else {}
            self._atomic_manifest_write(manifest_path, manifest)
        except Exception as exc:
            manifest["status"] = "failed"
            manifest["error"] = f"Could not capture initial motion positions: {exc}"
            self._atomic_manifest_write(manifest_path, manifest)
            self.log.emit(f"[{_ts()}] Sequence failed before transitions: {exc}")
            raise RuntimeError(manifest["error"]) from exc
        self._sequence_cleanup_errors = []
        self._progress_offset = 0
        self._progress_total_override = len(sequence) * len(p["positions"])
        last_gate = None
        last_rotation = {}
        completed_ok = False
        failure = None
        try:
            for number, item in enumerate(sequence, 1):
                entry = entries[number - 1]
                if self._stop.is_set():
                    break
                self._last_output_path = None
                ci = int(item["condition_index"])
                cond = dict(conditions[ci])
                gate = (float(cond.get("vbg_v", 0.0)), float(cond.get("vtg_v", 0.0)),
                        float(cond.get("vbias_v", 0.0)))
                requested = {axis: self._requested_rotation(item, axis, axes, values) for axis in axes}
                entry["status"] = "running"
                self._atomic_manifest_write(manifest_path, manifest)
                try:
                    order = str(p.get("sequence_order", p.get("order", "gate-first"))).lower()
                    if order.startswith("gate"):
                        last_gate = self._sequence_apply_gate(p, gate, last_gate)
                        last_rotation = self._sequence_apply_rotations(p, requested, last_rotation)
                    else:
                        last_rotation = self._sequence_apply_rotations(p, requested, last_rotation)
                        last_gate = self._sequence_apply_gate(p, gate, last_gate)
                    if self._stop.is_set():
                        raise _StopRequested()
                    observed_gate = self._sequence_verify_gates(gate)
                    observed_rotation = self._sequence_rotation_readback(axes)
                    condition_id = entry["condition_id"]
                    applied = {"Vbg": gate[0], "Vtg": gate[1], "Vbias": gate[2], **observed_rotation}
                    if self._experiment_run is not None:
                        self._experiment_run.register_condition(
                            cond, condition_id=condition_id,
                            requested={"sequence": number, "repeat": entry["repeat"], **requested},
                            applied=applied, observed={**observed_gate, **observed_rotation})
                    child = dict(p)
                    child.pop("condition_sequence", None)
                    child.update({"apply_gates": False, "strict_sequence": True,
                                  "strict_gate_errors": True, "condition_metadata": True,
                                  "condition_id": condition_id, "condition_index": ci + 1,
                                  "condition_label": self._condition_label(cond),
                                  "doping_v": cond.get("doping_v", NAN), "efield_v": cond.get("efield_v", NAN),
                                  "Vtg_target": gate[1], "Vbg_target": gate[0], "Vbias_target": gate[2],
                                  "sequence": number, "repeat": entry["repeat"],
                                  "rotation_requested": requested.get(axes[0]) if axes else NAN,
                                  "rotation_actual": observed_rotation.get(f"{axes[0]}_actual", NAN) if axes else NAN,
                                  "rot1_requested": requested.get("rot1", NAN), "rot1_actual": observed_rotation.get("rot1_actual", NAN),
                                  "rot2_requested": requested.get("rot2", NAN), "rot2_actual": observed_rotation.get("rot2_actual", NAN),
                                  "Vbg_observed": observed_gate.get("Vbg_observed", NAN), "Vtg_observed": observed_gate.get("Vtg_observed", NAN),
                                  "Vbias_observed": observed_gate.get("Vbias_observed", NAN),
                                  "return_to_zero": False, "return_motion_to_start": False})
                    tokens = [f"c{ci + 1:03d}"]
                    for axis in ("rot1", "rot2"):
                        if axis in requested:
                            token = "keep" if requested[axis] is None else format_compact_number(requested[axis], keep_sign=True)
                            tokens.append(f"{axis}_{token}")
                    tokens.extend((f"s{number:04d}", f"rep{entry['repeat']:03d}", _build_gate_token(*gate)))
                    child["base_name"] = sanitize_token(f"{p['base_name']}_{'_'.join(tokens)}")
                    self.log.emit(f"[{_ts()}] Sweep {number}/{len(sequence)} {self._condition_label(cond)}")
                    child_fp = self._run_sweep(child)
                    entry["status"] = "stopped" if self._stop.is_set() else "complete"
                    entry["file"] = str(Path(child_fp))
                    manifest["files"].append(str(Path(child_fp)))
                    if self._experiment_run is not None:
                        self._experiment_run.register_file(child_fp, "raw", details={"condition_id": condition_id})
                    self._atomic_manifest_write(manifest_path, manifest)
                    self._progress_offset += len(p["positions"])
                except _StopRequested:
                    entry["status"] = "stopped"
                    self._atomic_manifest_write(manifest_path, manifest)
                    break
                except Exception as exc:
                    entry["status"] = "failed"; entry["error"] = str(exc)
                    partial = getattr(self, "_last_output_path", None)
                    if partial is not None and Path(partial).exists():
                        entry["file"] = str(Path(partial))
                        if entry["file"] not in manifest["files"]:
                            manifest["files"].append(entry["file"])
                        if self._experiment_run is not None:
                            try:
                                self._experiment_run.register_file(partial, "raw", details={"condition_id": entry["condition_id"], "partial": True})
                            except Exception:
                                pass
                    failure = exc
                    self._atomic_manifest_write(manifest_path, manifest)
                    break
            if failure is not None:
                manifest["status"] = "failed"
            elif self._stop.is_set() or any(e["status"] == "stopped" for e in entries):
                manifest["status"] = "stopped"
            elif all(e["status"] == "complete" for e in entries):
                manifest["status"] = "complete"; completed_ok = True
            else:
                manifest["status"] = "stopped"
        finally:
            # One restoration pass for the entire sequence. Failure deliberately
            # preserves the last commanded SMU state, matching legacy policy.
            if p.get("return_motion_to_start", True):
                self._restore_motion_positions(restore)
            if failure is None and (completed_ok or self._stop.is_set()) and p.get("return_to_zero", True):
                self._sequence_return_gates_zero(p)
            if self._sequence_cleanup_errors:
                manifest["cleanup_errors"] = list(self._sequence_cleanup_errors)
                if manifest["status"] == "complete":
                    manifest["status"] = "failed"
                    if failure is None:
                        failure = RuntimeError(
                            "Acquisition complete; sequence cleanup failed: "
                            + "; ".join(self._sequence_cleanup_errors)
                        )
                    self.log.emit(f"[{_ts()}] All {len(entries)} sweeps were acquired and saved; cleanup failed.")
            self._atomic_manifest_write(manifest_path, manifest)
            self.log.emit(f"[{_ts()}] Sequence {manifest['status']}. Manifest → {manifest_path}")
            self._progress_total_override = None
        if failure is not None:
            raise RuntimeError(str(failure)) from failure
        return manifest_path

    @staticmethod
    def _atomic_manifest_write(path: Path, manifest: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(manifest, indent=2, allow_nan=False, default=str), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _condition_label(condition: dict) -> str:
        if condition.get("label"):
            return str(condition["label"])
        if condition.get("doping_v") is not None or condition.get("efield_v") is not None:
            return f"D={float(condition.get('doping_v', NAN)):g},F={float(condition.get('efield_v', NAN)):g}"
        return f"Vtg={float(condition.get('vtg_v', NAN)):g},Vbg={float(condition.get('vbg_v', NAN)):g}"

    def _sequence_rotation_axes(self, p: dict) -> list[str]:
        raw = p.get("rotation_axes")
        if isinstance(raw, Mapping):
            axes = [str(axis) for axis in raw]
        elif isinstance(raw, (list, tuple)):
            axes = [str(axis) for axis in raw]
        else:
            axis = p.get("rotation_axis")
            axes = [str(axis)] if axis else []
        return [axis for axis in ("rot1", "rot2") if axis in axes]

    @staticmethod
    def _sequence_rotation_values(p: dict, axes: list[str]) -> dict[str, list[float]]:
        raw = p.get("rotation_values")
        if isinstance(raw, Mapping):
            result = {axis: [float(value) for value in raw.get(axis, ())] for axis in axes}
        else:
            result = {axis: [float(value) for value in (raw or ())] for axis in axes}
        # Keep-current plans are represented by an empty list and do not need
        # an adapter. An explicit axis with no values is therefore omitted.
        return {axis: values for axis, values in result.items() if values}

    @staticmethod
    def _requested_rotation(item: dict, axis: str, axes: list[str], values: dict[str, list[float]]):
        if axis not in values:
            return None
        key = "rot1_index" if axis == "rot1" else "rot2_index"
        if axis == axes[0] and key not in item:
            key = "rotation_index"
        index = int(item.get(key, 0))
        if not (0 <= index < len(values[axis])):
            raise ValueError(f"Rotation index {index} is out of range for {axis}")
        return float(values[axis][index])

    def _sequence_preflight(self, p: dict, sequence: list, conditions: list) -> None:
        if not sequence:
            raise ValueError("Across conditions plan is empty")
        if p.get("positions") is None or len(p["positions"]) < 1:
            raise ValueError("Across conditions requires at least one inner sweep point")
        if not p.get("apply_gates"):
            raise ValueError("Across conditions requires Apply gate voltages")
        if self._smu is None or not bool(getattr(self._smu, "is_connected", False)) or getattr(self._smu, "device", None) is None:
            raise RuntimeError("Across conditions requires a connected SMU")
        if self._lf6 is None or not bool(getattr(self._lf6, "is_connected", False)) or getattr(self._lf6, "adapter", None) is None:
            raise RuntimeError("Across conditions requires a connected LF6")
        inner = str(p.get("motion_key", "stage"))
        if inner == "stage":
            adapter = getattr(self._stg, "adapter", None) if self._stg and bool(getattr(self._stg, "is_connected", False)) else None
        else:
            adapter = self._rot.adapter(inner) if self._rot and callable(getattr(self._rot, "adapter", None)) and self._rot.is_connected(inner) else None
        if adapter is None:
            raise RuntimeError(f"{_MOTION_SPECS.get(inner, {'label': inner})['label']} is not connected")
        try:
            lower = float(getattr(adapter, "minimum_position", _ROTATION_MIN_DEG if inner != "stage" else -math.inf))
            upper = float(getattr(adapter, "maximum_position", _ROTATION_MAX_DEG if inner != "stage" else math.inf))
        except (TypeError, ValueError):
            lower, upper = (-math.inf, math.inf)
        for point in p["positions"]:
            point = float(point)
            if not math.isfinite(point) or point < lower or point > upper:
                raise ValueError(f"Inner sweep value {point:g} is outside configured actuator limits")
        axes = self._sequence_rotation_axes(p)
        values = self._sequence_rotation_values(p, axes)
        if any(axis == inner and axis in values for axis in axes):
            raise ValueError("Explicit outer rotation axis cannot equal the inner sweep actuator")
        for axis in axes:
            if axis in values:
                if self._rot is None or not self._rot.is_connected(axis) or self._rot.adapter(axis) is None:
                    raise RuntimeError(f"{axis} is required by the rotation plan but is not connected")
                if any(not math.isfinite(value) or value < _ROTATION_MIN_DEG or value > _ROTATION_MAX_DEG for value in values[axis]):
                    raise ValueError(f"{axis} rotation is outside [{_ROTATION_MIN_DEG:g}, {_ROTATION_MAX_DEG:g}] degrees")
        try:
            limit = float(getattr(cfg.smu, "volt_compliance_V", 20.0))
        except Exception:
            limit = 20.0
        for index, condition in enumerate(conditions):
            for key in ("vtg_v", "vbg_v", "vbias_v"):
                value = float(condition.get(key, 0.0))
                if not math.isfinite(value) or abs(value) > abs(limit) + 1e-9:
                    raise ValueError(f"condition {index + 1} {key} exceeds SMU voltage compliance")
        for item in sequence:
            ci = int(item.get("condition_index", -1))
            if not (0 <= ci < len(conditions)):
                raise ValueError("Condition index is out of range")

    def _capture_motion_positions(self, p: dict, axes: list[str]) -> dict[str, float]:
        restore = {}
        self._initial_motion_positions = {}
        inner = str(p.get("motion_key", "stage"))
        adapter = self._stg.adapter if inner == "stage" else self._rot.adapter(inner)
        restore[inner] = float(adapter.get_position())
        for axis in axes:
            if axis == inner or axis not in self._sequence_rotation_values(p, axes):
                continue
            restore[axis] = float(self._rot.adapter(axis).get_position())
        for axis, value in restore.items():
            self._initial_motion_positions[axis] = value
            if not math.isfinite(value):
                raise ValueError(f"{axis} initial position is nonfinite: {value}")
            if p.get("return_motion_to_start", True):
                adapter = self._stg.adapter if axis == "stage" else self._rot.adapter(axis)
                restore[axis] = self._validated_restore_position(axis, adapter, value)
        return restore

    def _validated_restore_position(self, axis: str, adapter, value: float) -> float:
        try:
            if not math.isfinite(value):
                raise ValueError("position must be finite")
            normalize = getattr(adapter, "normalize_restore_position", None)
            target = float(normalize(value)) if callable(normalize) else value
            lower = float(getattr(adapter, "minimum_position", -math.inf if axis == "stage" else _ROTATION_MIN_DEG))
            upper = float(getattr(adapter, "maximum_position", math.inf if axis == "stage" else _ROTATION_MAX_DEG))
            if not math.isfinite(target) or not lower <= target <= upper:
                raise ValueError(f"allowed range is [{lower:g}, {upper:g}]")
            validate = getattr(adapter, "validate_position", None)
            if callable(validate):
                validate(target)
        except Exception as exc:
            raise ValueError(f"{axis} initial position {value:.12g} cannot be used for restoration: {exc}") from exc
        if target != value:
            self.log.emit(f"[{_ts()}] WARNING: {axis} initial readback {value:.12g} is outside the command range; restore target adjusted to {target:.12g}.")
        return target

    def _restore_motion_positions(self, restore: dict[str, float]) -> None:
        for axis, value in restore.items():
            try:
                adapter = self._stg.adapter if axis == "stage" else self._rot.adapter(axis)
                move_and_verify(adapter, value)
            except Exception as exc:
                self.log.emit(f"[{_ts()}] {axis} restore to {value:.12g} failed: {exc}")
                self._sequence_cleanup_errors.append(f"{axis} restore to {value:.12g} failed: {exc}")

    def _sequence_apply_rotations(self, p: dict, requested: dict, last: dict) -> dict:
        for axis, target in requested.items():
            if target is None:
                continue
            adapter = self._rot.adapter(axis)
            current = last.get(axis)
            if current is None:
                try:
                    current = float(adapter.get_position())
                except Exception:
                    current = NAN
            needs_move = not math.isfinite(current) or not math.isclose(float(current), target, abs_tol=1e-9)
            if self._stop.is_set():
                raise _StopRequested()
            try:
                observed = move_and_verify(
                    adapter, target, stop_event=self._stop, issue_move=needs_move,
                    tolerance=_ROTATION_POSITION_TOLERANCE_DEG,
                    config=MotionVerificationConfig(
                        settling_s=float(p.get("rotation_settle_s", p.get("motion_settle_s", 0.3))),
                    ),
                )
            except MotionCancelledError as exc:
                raise _StopRequested() from exc
            last[axis] = target
            last[f"{axis}_actual"] = observed
        return last

    def _sequence_gate_readback(self) -> dict[str, float]:
        bg, tg = _read_gates(self._smu.device)
        return {"Vbg_observed": bg, "Vtg_observed": tg, "Vbias_observed": _read_bias(self._smu.device)}

    def _sequence_verify_gates(self, gate: tuple[float, float, float]) -> dict[str, float]:
        """Retry transient readback errors without sending additional gate ramps."""
        has_role = getattr(self._smu.device, "has_role", None)
        for attempt in range(_GATE_READBACK_RETRIES + 1):
            if self._stop.is_set():
                raise _StopRequested()
            observed_gate = self._sequence_gate_readback()
            if self._stop.is_set():
                raise _StopRequested()
            issues = []
            for role, target in zip(("Vbg", "Vtg", "Vbias"), gate):
                # An unmapped, unused bias channel has no readback to verify.
                if role == "Vbias" and target == 0.0 and callable(has_role) and not has_role(role):
                    continue
                key = f"{role}_observed"
                observed = observed_gate.get(key, NAN)
                # Match the %.3f voltage command in iv_automation.volt_step.
                commanded = float("%.3f" % target)
                if not math.isfinite(observed):
                    issues.append(f"SMU {key} is unavailable/nonfinite (commanded {commanded:g} V)")
                elif not math.isclose(observed, commanded, rel_tol=0.0, abs_tol=1e-5):
                    issues.append(
                        f"SMU {key} {observed:g} V does not match commanded {commanded:g} V "
                        f"(requested {target:.12g} V, error {abs(observed - commanded):g} V, "
                        "tolerance 1e-05 V)"
                    )
            if not issues:
                return observed_gate
            detail = "; ".join(issues)
            if attempt == _GATE_READBACK_RETRIES:
                raise RuntimeError(f"{detail}; failed after {_GATE_READBACK_RETRIES} readback retries")
            self.log.emit(f"[{_ts()}] {detail}; readback retry {attempt + 1}/{_GATE_READBACK_RETRIES}.")
            _cancelable_sleep(_GATE_READBACK_INTERVAL_S, self._stop)

    def _sequence_rotation_readback(self, axes: list[str]) -> dict[str, float]:
        out = {}
        for axis in axes:
            try:
                out[f"{axis}_actual"] = float(self._rot.adapter(axis).get_position())
            except Exception:
                out[f"{axis}_actual"] = NAN
        return out

    def _sequence_apply_gate(self, p: dict, gate: tuple[float, float, float], last):
        if last is not None and all(math.isclose(a, b, abs_tol=1e-9) for a, b in zip(last, gate)):
            return last
        if self._stop.is_set():
            raise _StopRequested()
        iv = self._smu.device
        iv.set_gates(Vtg=gate[1], Vbg=gate[0], ramp_step=p.get("ramp_step_V", 0.1),
                     delay_s=p.get("step_delay_s", p.get("ramp_step_V", 0.1) / 5),
                     stop_cb=self._stop.is_set, stop_exc=_StopRequested)
        if hasattr(iv, "set_bias"):
            iv.set_bias(Vbias=gate[2], ramp_step=p.get("vbias_step_V", p.get("ramp_step_V", 0.1)),
                        delay_s=p.get("step_delay_s", p.get("ramp_step_V", 0.1) / 5),
                        stop_cb=self._stop.is_set, stop_exc=_StopRequested)
        _cancelable_sleep(p.get("settle_s", 0.0), self._stop)
        return gate

    def _sequence_return_gates_zero(self, p: dict) -> None:
        try:
            result = self._smu.device.ramp_all_to_zero(ramp_step=p.get("ramp_step_V", 0.1),
                                                       delay_s=p.get("step_delay_s", 0.02))
            if result:
                raise RuntimeError(str(result))
        except Exception as exc:
            self.log.emit(f"[{_ts()}] Gate return-to-zero failed: {exc}")
            self._sequence_cleanup_errors.append(f"Gate return-to-zero failed: {exc}")


# ── panel ──────────────────────────────────────────────────────────────────────
class PowerSweepPanel(QWidget):
    busy_changed = Signal(bool)
    """Motion-dependent measurement tab.

    Usage:
        panel = PowerSweepPanel(
            lf6_ctrl=lf6, stage_ctrl=stg, rotation_ctrl=rot,
            pm_ctrl=pm, smu_ctrl=smu
        )
    """

    def __init__(
        self,
        lf6_ctrl=None,
        stage_ctrl=None,
        rotation_ctrl=None,
        pm_ctrl=None,
        smu_ctrl=None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._lf6 = lf6_ctrl
        self._stg = stage_ctrl
        self._rot = rotation_ctrl
        self._pm = pm_ctrl
        self._smu = smu_ctrl
        self._worker: Optional[_PowerSweepWorker] = None
        self._thread: Optional[QThread] = None
        self._positions: np.ndarray = np.array([], dtype=float)
        self._target_powers: np.ndarray | None = None
        self._frozen_positions: np.ndarray | None = None
        self._frozen_target_powers: np.ndarray | None = None
        self._freeze_source: dict | None = None
        self._custom_condition_sequence: list[dict] | None = None
        self._conditions_ui_was_enabled = True
        self._applying_freeze = False
        self._calibration_busy = False
        self._sweep_busy = False
        self._last_motion_key = "stage"
        self._run_finalized = True
        self._last_busy_signal = False
        self._parse_timer = QTimer(self)
        self._parse_timer.setSingleShot(True)
        self._parse_timer.setInterval(120)
        self._parse_timer.timeout.connect(self._update_position_preview)
        self._build()
        self._wire()
        self._on_motion_changed()
        self._update_motion_axis_visibility()
        self._update_position_preview()

    # ── build ─────────────────────────────────────────────────────────────────

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self._splitter = QSplitter(Qt.Horizontal)
        splitter = self._splitter
        root.addWidget(splitter, stretch=1)

        # ── left: scrollable controls ────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(420)
        left = QWidget()
        left.setObjectName("MotionSweepControls")
        left.setStyleSheet("""
            QWidget#MotionSweepControls QGroupBox {
                margin-top: 10px; padding: 3px 4px 4px 4px;
            }
            QWidget#MotionSweepControls QLineEdit,
            QWidget#MotionSweepControls QComboBox,
            QWidget#MotionSweepControls QDoubleSpinBox,
            QWidget#MotionSweepControls QSpinBox {
                min-height: 20px; padding: 1px 4px;
            }
            QWidget#MotionSweepControls QPushButton {
                min-height: 22px; padding: 2px 7px;
            }
            QWidget#MotionSweepControls QCheckBox { min-height: 18px; }
        """)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(4, 4, 4, 4)
        left_lay.setSpacing(3)
        sweep_lay = conditions_lay = setup_lay = left_lay
        scroll.setWidget(left)
        splitter.addWidget(scroll)

        # Sweep motion
        self._motion_grp = QGroupBox("Sweep Motion")
        pos_form = QFormLayout(self._motion_grp)
        self._motion_form = pos_form
        pos_form.setContentsMargins(6, 4, 6, 4)
        pos_form.setSpacing(3)
        self._motion_combo = QComboBox(self._motion_grp)
        for key, spec in _MOTION_SPECS.items():
            self._motion_combo.addItem(spec["label"], key)
        self._motion_combo.setToolTip("Choose the actuator moved at every measurement point: stage, Rot1, or Rot2.")
        # Keep the existing model/state binding; expose all three choices.
        self._motion_combo.hide()
        axis_row = QHBoxLayout()
        axis_row.setSpacing(2)
        self._motion_buttons = {}
        self._motion_button_group = QButtonGroup(self)
        self._motion_button_group.setExclusive(True)
        for key, label in (("stage", "Stage"), ("rot1", "Rot1"), ("rot2", "Rot2")):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setToolTip(f"Sweep {_MOTION_SPECS[key]['label']} at every measurement point")
            button.setStyleSheet("QPushButton:checked { background: #dceafb; color: #174f8a; border: 1px solid #6497d0; font-weight: bold; }")
            self._motion_button_group.addButton(button)
            self._motion_buttons[key] = button
            button.clicked.connect(lambda checked=False, axis=key: self._motion_combo.setCurrentIndex(self._motion_combo.findData(axis)))
            axis_row.addWidget(button, 1)
        self._legacy_axis_label = QLabel("Sweep axis:")
        pos_form.addRow(self._legacy_axis_label, axis_row)
        self._legacy_axis_label.hide()
        for button in self._motion_buttons.values():
            button.hide()
        self._pos_input = QLineEdit("(0, 50, 1)")
        # New versioned axis editor.  The hidden combo/buttons above remain a
        # compatibility surface for saved sessions and older callers.
        self._axis_mode_combos = {}
        self._axis_value_edits = {}
        axis_grid = QGridLayout()
        axis_grid.setContentsMargins(0, 0, 0, 0); axis_grid.setHorizontalSpacing(3); axis_grid.setVerticalSpacing(2)
        axis_grid.addWidget(QLabel("Axis"), 0, 0); axis_grid.addWidget(QLabel("Mode"), 0, 1); axis_grid.addWidget(QLabel("Values"), 0, 2)
        axis_modes = (("Not used", MOTION_NOT_USED), ("Hold position", MOTION_HOLD), ("Fixed", MOTION_FIXED), ("Sweep", MOTION_SWEEP))
        for row, (axis, label) in enumerate((("stage", "Stage"), ("rot1", "Rot1"), ("rot2", "Rot2")), 1):
            axis_grid.addWidget(QLabel(label), row, 0)
            mode_combo = QComboBox(); mode_combo.setObjectName(f"{axis}Mode")
            for text, value in axis_modes: mode_combo.addItem(text, value)
            mode_combo.setCurrentIndex(mode_combo.findData(MOTION_SWEEP if axis == "stage" else MOTION_NOT_USED))
            value_edit = self._pos_input if axis == "stage" else QLineEdit("")
            value_edit.setPlaceholderText("Start, Stop, Step / [values]")
            self._axis_mode_combos[axis] = mode_combo; self._axis_value_edits[axis] = value_edit
            axis_grid.addWidget(mode_combo, row, 1); axis_grid.addWidget(value_edit, row, 2)
            mode_combo.currentIndexChanged.connect(self._update_motion_axis_visibility)
            value_edit.textChanged.connect(self._update_position_preview)
        pos_form.addRow("Motion axes:", axis_grid)
        self._axis_order_combo = QComboBox(); self._axis_order_combo.setObjectName("MotionScanOrder")
        for order in (("stage", "rot1", "rot2"), ("stage", "rot2", "rot1"),
                      ("rot1", "stage", "rot2"), ("rot1", "rot2", "stage"),
                      ("rot2", "stage", "rot1"), ("rot2", "rot1", "stage")):
            self._axis_order_combo.addItem(" → ".join(order).replace("stage", "Stage").replace("rot1", "Rot1").replace("rot2", "Rot2") + " (slow → fast)", order)
        pos_form.addRow("Scan order:", self._axis_order_combo)
        self._pos_input.setToolTip(
            "Range (start, stop, step) → points before stop\n"
            "List [v1, v2, ...] → exact positions\n"
            "Single number → one position"
        )
        self._input_mode = QComboBox()
        self._input_mode.addItem("Stage positions", "position")
        self._input_mode.addItem("Target power", "power")
        pos_form.addRow("Input mode:", self._input_mode)
        self._power_kind = QComboBox()
        self._power_kind.addItem("Numeric range", "range")
        self._power_kind.addItem("Custom list", "list")
        pos_form.addRow("Power input:", self._power_kind)
        self._power_start_spin = QDoubleSpinBox(); self._power_start_spin.setRange(1e-12, 1e12); self._power_start_spin.setDecimals(6); self._power_start_spin.setValue(1.0); self._power_start_spin.setSuffix(" µW")
        self._power_end_spin = QDoubleSpinBox(); self._power_end_spin.setRange(1e-12, 1e12); self._power_end_spin.setDecimals(6); self._power_end_spin.setValue(100.0); self._power_end_spin.setSuffix(" µW")
        self._power_count_spin = QSpinBox(); self._power_count_spin.setRange(1, 100000); self._power_count_spin.setValue(10)
        pos_form.addRow("Power start:", self._power_start_spin)
        pos_form.addRow("Power end:", self._power_end_spin)
        pos_form.addRow("Power count:", self._power_count_spin)
        self._power_range_edit = QLineEdit("[1, 10, 100]")
        self._power_range_edit.setToolTip("Custom target powers [p1, p2, ...], in µW")
        self._power_range_lbl = QLabel("Custom powers (µW):")
        pos_form.addRow(self._power_range_lbl, self._power_range_edit)
        self._power_spacing = QComboBox()
        self._power_spacing.addItem("Linear", "linear")
        self._power_spacing.addItem("Logarithmic", "log")
        pos_form.addRow("Power spacing:", self._power_spacing)
        self._freeze_positions_btn = QPushButton("Use these stage positions")
        self._freeze_positions_btn.setToolTip("Copy the generated positions into the position input for a later sweep.")
        self._freeze_positions_lbl = QLabel("Generated list:")
        pos_form.addRow(self._freeze_positions_lbl, self._freeze_positions_btn)
        self._pos_preview_lbl = QLabel("")
        self._pos_preview_lbl.setStyleSheet("color: #444; font-size: 10px;")
        self._pos_preview_lbl.setTextInteractionFlags(
            Qt.TextSelectableByMouse
        )
        self._pos_preview_lbl.setWordWrap(True)
        pos_form.addRow("Parsed:", self._pos_preview_lbl)
        self._pos_count_lbl = QLabel("")
        self._pos_count_lbl.setStyleSheet("color: gray; font-size: 10px;")
        pos_form.addRow("", self._pos_count_lbl)
        self._power_preview_lbl = QLabel("")
        self._power_preview_lbl.setWordWrap(True)
        self._power_preview_lbl.setStyleSheet("color: #444; font-size: 10px;")
        pos_form.addRow("Power preview:", self._power_preview_lbl)
        self._motion_settle_spin = QDoubleSpinBox()
        self._motion_settle_spin.setRange(0.0, 60.0)
        self._motion_settle_spin.setDecimals(3)
        self._motion_settle_spin.setValue(0.3)
        self._motion_settle_spin.setSuffix(" s")
        self._return_motion_chk = QCheckBox(
            "Return to start"
        )
        self._return_motion_chk.setChecked(True)
        settle_row = QHBoxLayout()
        settle_row.setSpacing(4)
        settle_row.addWidget(self._motion_settle_spin, 1)
        settle_row.addWidget(self._return_motion_chk)
        self._return_motion_chk.setToolTip("Return actuator to starting position after sweep")
        pos_form.addRow("Settle:", settle_row)
        sweep_lay.addWidget(self._motion_grp)

        # Optional condition batch.  Defaults preserve the original one-gate
        # sweep; enabling it expands the two coordinate arrays at run time.
        self._conditions_grp = QGroupBox("Across conditions (optional)")
        self._conditions_grp.setCheckable(True); self._conditions_grp.setChecked(False)
        condition_lay = QVBoxLayout(self._conditions_grp)
        condition_lay.setContentsMargins(3, 3, 3, 3)
        self._condition_editor = MotionConditionsWidget(self)
        self._condition_preview_lbl = QLabel("Disabled")
        self._condition_preview_lbl.setWordWrap(True)
        self._condition_preview_btn = QPushButton("Preview / select sequence…")
        self._condition_preview_btn.setEnabled(False)
        condition_lay.addWidget(self._condition_editor)
        condition_lay.addWidget(self._condition_preview_btn)
        condition_lay.addWidget(self._condition_preview_lbl)
        conditions_lay.addWidget(self._conditions_grp)
        self._condition_editor.setEnabled(False)
        self._conditions_grp.toggled.connect(self._condition_editor.setEnabled)
        self._conditions_grp.toggled.connect(self._condition_preview_btn.setEnabled)
        for widget in (self._condition_editor, self._condition_preview_btn, self._condition_preview_lbl):
            widget.setVisible(False)
            self._conditions_grp.toggled.connect(widget.setVisible)

        # The shared widget is the sole owner of calibration/reference acquisition.
        self._cal_grp = QGroupBox("Shared ND calibration")
        self._cal_grp.setCheckable(True)
        self._cal_grp.setChecked(False)
        cal_lay = QVBoxLayout(self._cal_grp)
        cal_lay.setContentsMargins(6, 4, 6, 4)
        self._cal_widget = NDCalibrationWidget(self._stg, self._pm, self)
        self._cal_widget.setObjectName("NDCalibrationWidget")
        self._cal_widget.changed.connect(self._update_calibration_plot)
        self._cal_widget.changed.connect(self._update_position_preview)
        cal_lay.addWidget(self._cal_widget)
        self._cal_grp.toggled.connect(self._set_calibration_expanded)
        self._set_calibration_expanded(False)
        setup_lay.addWidget(self._cal_grp)

        # Optical settings
        opt_grp = QGroupBox("Optical Settings")
        opt_form = QGridLayout(opt_grp)
        opt_form.setContentsMargins(6, 4, 6, 4)
        opt_form.setSpacing(3)
        self._center_spin = QDoubleSpinBox()
        self._center_spin.setRange(200, 2000)
        self._center_spin.setDecimals(2)
        self._center_spin.setValue(cfg.lf6.center_nm)
        self._center_spin.setSuffix(" nm")
        opt_form.addWidget(QLabel("Center λ:"), 0, 0)
        opt_form.addWidget(self._center_spin, 0, 1)
        self._exp_spin = QDoubleSpinBox()
        self._exp_spin.setRange(1, 600_000)
        self._exp_spin.setDecimals(1)
        self._exp_spin.setValue(cfg.lf6.exposure_ms)
        self._exp_spin.setSuffix(" ms")
        opt_form.addWidget(QLabel("Exposure:"), 1, 0)
        opt_form.addWidget(self._exp_spin, 1, 1)
        self._frames_spin = QSpinBox()
        self._frames_spin.setRange(1, 100000)
        self._frames_spin.setValue(cfg.lf6.accumulations)
        opt_form.addWidget(QLabel("Frames:"), 1, 2)
        self._frames_spin.setToolTip("Frames / exposures per frame")
        opt_form.addWidget(self._frames_spin, 1, 3)
        self._pm_wl_spin = QDoubleSpinBox()
        self._pm_wl_spin.setRange(200, 2000)
        self._pm_wl_spin.setDecimals(1)
        self._pm_wl_spin.setValue(730.0)
        self._pm_wl_spin.setSuffix(" nm")
        opt_form.addWidget(QLabel("PM λ:"), 0, 2)
        opt_form.addWidget(self._pm_wl_spin, 0, 3)
        opt_form.setColumnStretch(1, 1)
        opt_form.setColumnStretch(3, 1)
        sweep_lay.addWidget(opt_grp)

        # Gate settings (SMU)
        self._gate_grp = QGroupBox("Single condition")
        gate_form = QFormLayout(self._gate_grp)
        self._gate_form = gate_form
        gate_form.setContentsMargins(6, 4, 6, 4)
        gate_form.setSpacing(3)
        self._vbg_spin = QDoubleSpinBox()
        self._vbg_spin.setRange(-200, 200)
        self._vbg_spin.setDecimals(3)
        self._vbg_spin.setValue(0.0)
        self._vbg_spin.setSuffix(" V")
        gate_form.addRow("Vbg:", self._vbg_spin)
        self._vtg_spin = QDoubleSpinBox()
        self._vtg_spin.setRange(-200, 200)
        self._vtg_spin.setDecimals(3)
        self._vtg_spin.setValue(0.0)
        self._vtg_spin.setSuffix(" V")
        gate_form.addRow("Vtg:", self._vtg_spin)
        self._vbias_spin = QDoubleSpinBox()
        self._vbias_spin.setRange(-200, 200)
        self._vbias_spin.setDecimals(3)
        self._vbias_spin.setValue(0.0)
        self._vbias_spin.setSuffix(" V")
        gate_form.addRow("Vbias:", self._vbias_spin)
        self._apply_gates_chk = QCheckBox("Apply gates")
        self._apply_gates_chk.setToolTip("Apply gate voltages before sweep")
        self._apply_gates_chk.setChecked(True)
        self._return_zero_chk = QCheckBox("Return gates to 0 V")
        self._return_zero_chk.setToolTip("Return gates to 0 V after sweep")
        self._return_zero_chk.setChecked(True)
        self._gate_voltage_spins = (self._vbg_spin, self._vtg_spin, self._vbias_spin)
        self._gate_grp.setEnabled(False)
        conditions_lay.addWidget(self._gate_grp)
        gate_options = QHBoxLayout()
        gate_options.addWidget(self._apply_gates_chk)
        gate_options.addWidget(self._return_zero_chk)
        conditions_lay.addLayout(gate_options)

        # File / metadata
        meta_grp = QGroupBox("File / Metadata")
        meta_form = QGridLayout(meta_grp)
        meta_form.setContentsMargins(6, 4, 6, 4)
        meta_form.setSpacing(3)
        self._devid_edit = QLineEdit()
        self._devid_edit.setPlaceholderText("Sample ID")
        meta_form.addWidget(QLabel("Sample ID:"), 0, 0)
        meta_form.addWidget(self._devid_edit, 0, 1)
        self._point_edit = QLineEdit()
        self._point_edit.setPlaceholderText("optional")
        meta_form.addWidget(QLabel("Point:"), 0, 2)
        meta_form.addWidget(self._point_edit, 0, 3)
        self._laser_edit = QLineEdit("730")
        meta_form.addWidget(QLabel("Laser (nm):"), 1, 0)
        meta_form.addWidget(self._laser_edit, 1, 1)
        self._subfolder_edit = QLineEdit("motion_sweep")
        meta_form.addWidget(QLabel("Subfolder:"), 1, 2)
        meta_form.addWidget(self._subfolder_edit, 1, 3)
        self._filename_lbl = QLabel("")
        self._filename_lbl.setStyleSheet(
            "color: #555; font-size: 10px;"
        )
        self._filename_lbl.setWordWrap(True)
        meta_form.addWidget(self._filename_lbl, 2, 0, 1, 4)
        self._est_lbl = QLabel("")
        self._est_lbl.setStyleSheet("color: gray; font-size: 10px;")
        meta_form.addWidget(self._est_lbl, 3, 0, 1, 4)
        meta_form.setColumnStretch(1, 1)
        meta_form.setColumnStretch(3, 1)
        sweep_lay.addWidget(meta_grp)

        # Hardware status
        hw_grp = QGroupBox("Hardware Status")
        hw_grp.setCheckable(True)
        hw_grp.setChecked(False)
        hw_body = QWidget()
        hw_container = QVBoxLayout(hw_grp)
        hw_container.setContentsMargins(0, 0, 0, 0)
        hw_container.addWidget(hw_body)
        hw_form = QFormLayout(hw_body)
        hw_form.setContentsMargins(6, 4, 6, 4)
        hw_form.setSpacing(2)
        self._stage_status = QLabel("○ Not connected")
        self._stage_status.setStyleSheet("color: gray; font-weight: bold;")
        hw_form.addRow("Stage:", self._stage_status)
        self._rot1_status = QLabel("○ Not connected")
        self._rot1_status.setStyleSheet("color: gray; font-weight: bold;")
        hw_form.addRow("Rot1:", self._rot1_status)
        self._rot2_status = QLabel("○ Not connected")
        self._rot2_status.setStyleSheet("color: gray; font-weight: bold;")
        hw_form.addRow("Rot2:", self._rot2_status)
        self._pm_status = QLabel("○ Not connected (optional)")
        self._pm_status.setStyleSheet(
            "color: #9a6700; font-weight: bold;"
        )
        hw_form.addRow("PM:", self._pm_status)
        self._pm_notice_lbl = QLabel(
            "PM100D is not connected. The sweep can still run, but no "
            "optical power values will be saved."
        )
        self._pm_notice_lbl.setWordWrap(True)
        self._pm_notice_lbl.setStyleSheet(
            "color: #9a6700; background: #fff8c5; border: 1px solid #d4a72c;"
            " border-radius: 3px; padding: 4px; font-size: 10px;"
        )
        hw_form.addRow("", self._pm_notice_lbl)
        self._lf6_status = QLabel("○ Not connected")
        self._lf6_status.setStyleSheet("color: gray; font-weight: bold;")
        hw_form.addRow("LF6:", self._lf6_status)
        self._smu_status = QLabel("○ Not connected")
        self._smu_status.setStyleSheet("color: gray; font-weight: bold;")
        hw_form.addRow("SMU:", self._smu_status)
        hw_body.setVisible(False)
        hw_grp.toggled.connect(hw_body.setVisible)
        self._hardware_grp = hw_grp
        setup_lay.addWidget(hw_grp)

        # Let paired inputs fit the narrow sidebar instead of forcing a
        # horizontal scrollbar through their platform-default size hints.
        for field in left.findChildren(QWidget):
            if isinstance(field, (QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox)):
                field.setMinimumWidth(0)
                field.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        left_lay.addStretch(1)

        # ── right: output ────────────────────────────────────────────────────
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(6, 4, 4, 4)
        right_lay.setSpacing(6)
        splitter.addWidget(right)
        splitter.setSizes([360, 840])

        # Log
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(130)
        self._log.setMinimumHeight(80)
        self._log.setStyleSheet(
            "QTextEdit { font-family: 'Consolas', 'Courier New', monospace;"
            " font-size: 11px; background: #fafafa;"
            " border: 1px solid #d0d0d0; border-radius: 3px; }"
        )
        right_lay.addWidget(self._log)

        # Spectrum plot
        self._plot = pg.PlotWidget()
        self._plot.setLabel("bottom", "Wavelength", units="nm")
        self._plot.setLabel("left", "Intensity", units="counts")
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        self._curve = self._plot.plot(
            pen=pg.mkPen(color="#1565C0", width=1.5)
        )
        self._plot.enableAutoRange()
        right_lay.addWidget(self._plot, stretch=1)
        self._cal_plot = pg.PlotWidget(); self._cal_plot.setLabel("bottom", "Stage position"); self._cal_plot.setLabel("left", "Predicted sample power", units="µW")
        self._cal_plot.showGrid(x=True, y=True, alpha=0.3); self._cal_plot.setMaximumHeight(180)
        self._cal_curve = self._cal_plot.plot(pen=pg.mkPen("#6a3d9a", width=2), name="Calibration curve")
        self._cal_points = self._cal_plot.plot(pen=None, symbol="o", symbolBrush="#6a3d9a", name="Measured points")
        self._cal_planned_points = self._cal_plot.plot(pen=None, symbol="x", symbolBrush="#e31a1c", symbolPen="#e31a1c", symbolSize=10, name="Planned points")
        self._cal_plot.addLegend(offset=(8, 8))
        self._cal_log_chk = QCheckBox("Log power axis"); right_lay.addWidget(self._cal_log_chk); right_lay.addWidget(self._cal_plot)

        # Controls
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(8)
        self._run_btn = QPushButton("▶  Run Motion Sweep")
        self._run_btn.setMinimumHeight(32)
        self._run_btn.setMinimumWidth(150)
        self._run_btn.setStyleSheet(
            "QPushButton { font-weight: 700; font-size: 12px;"
            " border-color: #5a9060; color: #1a4020;"
            " background: qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            " stop:0 #d8f0d8, stop:1 #b8e0b8); }"
            "QPushButton:hover { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            " stop:0 #e8f8e8, stop:1 #c8ecc8); }"
            "QPushButton:pressed { background: #a8d8a8; }"
            "QPushButton:disabled { color: #aaaaaa; border-color: #d0d0d0;"
            " background: #f0f0f0; }"
        )
        self._stop_btn = QPushButton("■  Stop")
        self._stop_btn.setMinimumHeight(32)
        self._stop_btn.setMinimumWidth(90)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setStyleSheet(
            "QPushButton { font-weight: 700; font-size: 12px;"
            " border-color: #a05050; color: #6a1010;"
            " background: qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            " stop:0 #f8dada, stop:1 #eec0c0); }"
            "QPushButton:hover { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            " stop:0 #ffe8e8, stop:1 #f4cccc); }"
            "QPushButton:pressed { background: #e0a8a8; }"
            "QPushButton:disabled { color: #aaaaaa; border-color: #d0d0d0;"
            " background: #f0f0f0; }"
        )
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._status_lbl = QLabel("Ready")
        self._status_lbl.setStyleSheet("color: #707070; font-size: 11px;")
        ctrl_row.addWidget(self._run_btn)
        ctrl_row.addWidget(self._stop_btn)
        ctrl_row.addWidget(self._progress, stretch=1)
        ctrl_row.addWidget(self._status_lbl)
        right_lay.addLayout(ctrl_row)

    # ── wire ──────────────────────────────────────────────────────────────────

    def _wire(self):
        # Input changes → debounced preview
        self._pos_input.textChanged.connect(
            lambda _: self._parse_timer.start()
        )
        self._pos_input.textChanged.connect(self._on_position_text_changed)
        self._power_range_edit.textChanged.connect(lambda _: self._parse_timer.start())
        self._input_mode.currentIndexChanged.connect(self._on_input_mode_changed)
        self._power_kind.currentIndexChanged.connect(self._on_plan_setting_changed)
        self._power_kind.currentIndexChanged.connect(self._update_power_input_visibility)
        self._power_spacing.currentIndexChanged.connect(self._on_plan_setting_changed)
        for w in (self._power_start_spin, self._power_end_spin, self._power_count_spin):
            w.valueChanged.connect(self._on_plan_setting_changed)
        self._freeze_positions_btn.clicked.connect(self._freeze_generated_positions)
        self._motion_combo.currentIndexChanged.connect(
            self._on_motion_changed
        )
        self._motion_settle_spin.valueChanged.connect(self._update_est)
        for w in (
            self._center_spin, self._exp_spin, self._frames_spin,
            self._pm_wl_spin,
            self._devid_edit, self._point_edit, self._laser_edit,
            self._subfolder_edit,
        ):
            if hasattr(w, "textChanged"):
                w.textChanged.connect(self._update_filename_preview)
            elif hasattr(w, "valueChanged"):
                w.valueChanged.connect(self._update_filename_preview)

        # Buttons
        self._run_btn.clicked.connect(self._on_run)
        self._stop_btn.clicked.connect(self._on_stop)
        self._cal_log_chk.toggled.connect(self._update_calibration_plot)
        self._cal_widget.busy_changed.connect(self._on_calibration_busy)
        self._condition_editor.changed.connect(self._on_condition_editor_changed)
        self._condition_preview_btn.clicked.connect(self._show_condition_preview)
        self._conditions_grp.toggled.connect(self._update_gate_target_visibility)
        self._update_gate_target_visibility(self._conditions_grp.isChecked())
        # A session reference is wavelength-specific. Keep the sweep setting
        # and the calibration/reference setting in one synchronized context so
        # changing PM λ invalidates a prior reference immediately.
        self._pm_wl_spin.setValue(self._cal_widget.wavelength_spin.value())
        self._pm_wl_spin.valueChanged.connect(self._cal_widget.wavelength_spin.setValue)
        self._cal_widget.wavelength_spin.valueChanged.connect(self._pm_wl_spin.setValue)
        self._update_power_input_visibility()
        self._update_plan_visibility()

        # Controller signals → status lamps
        if self._stg is not None:
            for name, slot in (("connected", self._on_stage_connected), ("disconnected", self._on_stage_disconnected)):
                signal = getattr(self._stg, name, None)
                if signal is not None and hasattr(signal, "connect"):
                    signal.connect(slot)
            if bool(getattr(self._stg, "is_connected", False)):
                self._on_stage_connected(
                    getattr(self._stg, "backend_key", "")
                )
        if self._rot is not None:
            for name, slot in (("connected", self._on_rotation_connected), ("disconnected", self._on_rotation_disconnected)):
                signal = getattr(self._rot, name, None)
                if signal is not None and hasattr(signal, "connect"):
                    signal.connect(slot)
            for slot in ("rot1", "rot2"):
                if callable(getattr(self._rot, "is_connected", None)) and self._rot.is_connected(slot):
                    self._set_rotation_status(slot, True)
        if self._pm is not None:
            for name, slot in (("connected", self._on_pm_connected), ("disconnected", self._on_pm_disconnected)):
                signal = getattr(self._pm, name, None)
                if signal is not None and hasattr(signal, "connect"):
                    signal.connect(slot)
            if bool(getattr(self._pm, "is_connected", False)):
                self._on_pm_connected()
        if self._lf6 is not None:
            for name, slot in (("connected", self._on_lf6_connected), ("disconnected", self._on_lf6_disconnected)):
                signal = getattr(self._lf6, name, None)
                if signal is not None and hasattr(signal, "connect"):
                    signal.connect(slot)
            if bool(getattr(self._lf6, "is_connected", False)):
                self._on_lf6_connected([])

        if self._smu is not None:
            for name, slot in (("connected", self._on_smu_connected), ("disconnected", self._on_smu_disconnected)):
                signal = getattr(self._smu, name, None)
                if signal is not None and hasattr(signal, "connect"):
                    signal.connect(slot)
            if bool(getattr(self._smu, "is_connected", False)):
                self._on_smu_connected([])

        # Gate UI changes → live preview
        for w in (
            self._vbg_spin, self._vtg_spin, self._vbias_spin,
            self._apply_gates_chk,
        ):
            if hasattr(w, "valueChanged"):
                w.valueChanged.connect(self._update_filename_preview)
            elif hasattr(w, "toggled"):
                w.toggled.connect(self._update_filename_preview)

    def capture_session_state(self) -> dict:
        """Capture setup controls, excluding run progress, plots, and hardware state."""
        return {
            "positions": self._pos_input.text(),
            "input_mode": self._input_mode.currentData() or "position",
            "power_input": self._power_range_edit.text(),
            "power_input_kind": self._power_kind.currentData() or "range",
            "power_start": float(self._power_start_spin.value()),
            "power_end": float(self._power_end_spin.value()),
            "power_count": int(self._power_count_spin.value()),
            "power_spacing": self._power_spacing.currentData() or "linear",
            "motion_axis": self._motion_combo.currentData() or "stage",
            "motion_plan_version": 2,
            "motion_axes": {axis: {"mode": combo.currentData(), "values": edit.text()}
                            for axis, combo in self._axis_mode_combos.items()
                            for edit in [self._axis_value_edits[axis]]},
            "axis_order": list(self._axis_order_combo.currentData() or ("stage", "rot1", "rot2")),
            "motion_settle_s": float(self._motion_settle_spin.value()),
            "return_motion_to_start": bool(
                self._return_motion_chk.isChecked()
            ),
            "center_nm": float(self._center_spin.value()),
            "exposure_ms": float(self._exp_spin.value()),
            "frames": int(self._frames_spin.value()),
            "pm_wavelength_nm": float(self._pm_wl_spin.value()),
            "vbg": float(self._vbg_spin.value()),
            "vtg": float(self._vtg_spin.value()),
            "vbias": float(self._vbias_spin.value()),
            "apply_gates": bool(self._apply_gates_chk.isChecked()),
            "return_zero": bool(self._return_zero_chk.isChecked()),
            "conditions_enabled": bool(self._conditions_grp.isChecked()),
            "motion_conditions": self._condition_editor.state(),
            "motion_conditions_selection": self._custom_condition_sequence,
            "sample_id": self._devid_edit.text(),
            "point": self._point_edit.text(),
            "laser_nm": self._laser_edit.text(),
            "subfolder": self._subfolder_edit.text(),
            "splitter_sizes": [int(v) for v in self._splitter.sizes()],
        }

    def apply_saved_experiment_settings(self, settings: dict) -> dict:
        allowed = {
            "center_nm": lambda v: self._center_spin.setValue(float(v)),
            "exp_ms": lambda v: self._exp_spin.setValue(float(v)),
            "frames": lambda v: self._frames_spin.setValue(int(v)),
            "Vbg_target": lambda v: self._vbg_spin.setValue(float(v)),
            "Vtg_target": lambda v: self._vtg_spin.setValue(float(v)),
            "Vbias_target": lambda v: self._vbias_spin.setValue(float(v)),
        }
        skipped = []
        for key, value in dict(settings or {}).items():
            setter = allowed.get(key)
            if setter is None:
                skipped.append(key)
                continue
            try:
                setter(value)
            except Exception:
                skipped.append(key)
        return {"applied": [k for k in settings if k not in skipped], "skipped": skipped}

    def restore_session_state(self, state: dict) -> None:
        if not isinstance(state, dict):
            return

        def set_number(widget, key: str) -> None:
            try:
                value = float(state[key])
                widget.setValue(int(value) if isinstance(widget, QSpinBox) else value)
            except (KeyError, TypeError, ValueError):
                pass

        text_fields = {
            "positions": self._pos_input,
            "sample_id": self._devid_edit,
            "point": self._point_edit,
            "laser_nm": self._laser_edit,
            "subfolder": self._subfolder_edit,
            "power_input": self._power_range_edit,
        }
        for key, widget in text_fields.items():
            value = state.get(key)
            if isinstance(value, str):
                widget.setText(value)
        for key, widget in (("power_start", self._power_start_spin), ("power_end", self._power_end_spin), ("power_count", self._power_count_spin)):
            set_number(widget, key)
        for key, widget in (
            ("motion_settle_s", self._motion_settle_spin),
            ("center_nm", self._center_spin),
            ("exposure_ms", self._exp_spin),
            ("frames", self._frames_spin),
            ("pm_wavelength_nm", self._pm_wl_spin),
            ("vbg", self._vbg_spin),
            ("vtg", self._vtg_spin),
            ("vbias", self._vbias_spin),
        ):
            set_number(widget, key)
        if "apply_gates" in state:
            self._apply_gates_chk.setChecked(bool(state["apply_gates"]))
        if "return_zero" in state:
            self._return_zero_chk.setChecked(bool(state["return_zero"]))
        if isinstance(state.get("motion_conditions"), dict):
            self._condition_editor.restore_state(state["motion_conditions"])
        selection = state.get("motion_conditions_selection")
        self._custom_condition_sequence = [dict(item) for item in selection] if isinstance(selection, list) else None
        if "conditions_enabled" in state:
            self._conditions_grp.setChecked(bool(state["conditions_enabled"]))
        if "return_motion_to_start" in state:
            self._return_motion_chk.setChecked(
                bool(state["return_motion_to_start"])
            )
        mode = state.get("input_mode")
        if isinstance(mode, str):
            index = self._input_mode.findData(mode)
            if index >= 0:
                self._input_mode.setCurrentIndex(index)
        spacing = state.get("power_spacing")
        if isinstance(spacing, str):
            index = self._power_spacing.findData(spacing)
            if index >= 0:
                self._power_spacing.setCurrentIndex(index)
        power_kind = state.get("power_input_kind")
        if isinstance(power_kind, str):
            index = self._power_kind.findData(power_kind)
            if index >= 0:
                self._power_kind.setCurrentIndex(index)
        motion_axis = state.get("motion_axis", "stage")
        if isinstance(motion_axis, str):
            index = self._motion_combo.findData(motion_axis)
            if index >= 0:
                self._motion_combo.setCurrentIndex(index)
        motion_axes = state.get("motion_axes")
        if isinstance(motion_axes, dict):
            for axis, spec in motion_axes.items():
                if axis not in self._axis_mode_combos or not isinstance(spec, dict):
                    continue
                mode = str(spec.get("mode", MOTION_NOT_USED)).lower()
                combo = self._axis_mode_combos[axis]
                index = combo.findData(mode)
                if index >= 0:
                    combo.setCurrentIndex(index)
                if "values" in spec:
                    self._axis_value_edits[axis].setText(str(spec.get("values", "")))
        elif "positions" in state:
            # Legacy Stage tuples used (start, stop, count).  Materialize
            # them once while loading so a subsequent save cannot reinterpret
            # the third item as the new Step.
            try:
                legacy_points = _parse_sweep_values(str(state.get("positions", "")))
                legacy_axis = motion_axis if motion_axis in self._axis_mode_combos else "stage"
                self._axis_mode_combos[legacy_axis].setCurrentIndex(
                    self._axis_mode_combos[legacy_axis].findData(MOTION_SWEEP))
                self._axis_value_edits[legacy_axis].setText(str([float(v) for v in legacy_points]))
                for axis in self._axis_mode_combos:
                    if axis != legacy_axis:
                        self._axis_mode_combos[axis].setCurrentIndex(
                            self._axis_mode_combos[axis].findData(MOTION_NOT_USED))
            except Exception:
                pass
        saved_order = state.get("axis_order")
        if isinstance(saved_order, (list, tuple)):
            index = self._axis_order_combo.findData(tuple(saved_order))
            if index >= 0:
                self._axis_order_combo.setCurrentIndex(index)
        sizes = state.get("splitter_sizes")
        if isinstance(sizes, list) and len(sizes) == 2:
            try:
                self._splitter.setSizes([max(0, int(v)) for v in sizes])
            except (TypeError, ValueError):
                pass
        self._update_position_preview()
        self._update_filename_preview()

    def _on_motion_changed(self, _index=None):
        key = self._motion_combo.currentData() or "stage"
        self._motion_buttons[key].setChecked(True)
        self._condition_editor.set_inner_sweep_axis(key)
        if key != self._last_motion_key:
            self._clear_freeze()
            self._last_motion_key = key
        spec = _MOTION_SPECS[key]
        unit = spec["unit"]
        if key == "stage" and self._stg is not None and bool(getattr(self._stg, "is_connected", False)):
            unit = getattr(self._stg.adapter, "position_unit", unit)
        self._pos_input.setToolTip(
            f"Values are interpreted in {unit}.\n"
            "Tuple (start, stop, count) → linspace\n"
            "List [v1, v2, ...] → exact values\n"
            "Single number → one value"
        )
        is_stage = key == "stage"
        self._input_mode.setEnabled(is_stage)
        self._motion_form.setRowVisible(self._input_mode, is_stage)
        self._power_kind.setEnabled(is_stage)
        self._power_range_edit.setEnabled(is_stage)
        for w in (self._power_start_spin, self._power_end_spin, self._power_count_spin):
            w.setEnabled(is_stage)
        self._power_spacing.setEnabled(is_stage)
        self._cal_grp.setVisible(is_stage or self._calibration_busy)
        self._update_plan_visibility()
        self._cal_plot.setVisible(is_stage)
        self._cal_log_chk.setVisible(is_stage)
        self._update_position_preview()
        self._update_filename_preview()

    def _update_motion_axis_visibility(self, *_):
        """Enable value editors only for modes which accept a target."""
        for axis, combo in self._axis_mode_combos.items():
            self._axis_value_edits[axis].setEnabled(combo.currentData() in (MOTION_FIXED, MOTION_SWEEP))
        self._update_position_preview()

    def _motion_axes_from_ui(self) -> dict[str, dict[str, object]]:
        """Return a frozen, validated axis map for the new motion planner."""
        result = {}
        for axis, combo in self._axis_mode_combos.items():
            mode = combo.currentData() or MOTION_NOT_USED
            edit = self._axis_value_edits[axis]
            spec: dict[str, object] = {"mode": mode}
            if mode in (MOTION_FIXED, MOTION_SWEEP):
                text = edit.text().strip()
                if axis == "stage" and self._input_mode.currentData() == "power" and self._positions.size:
                    spec["values"] = self._positions.tolist()
                    result[axis] = spec
                    continue
                # Explicit tuple syntax is Start, Stop, Step.  Lists remain
                # exact points, preserving the point list on state migration.
                import ast as _ast
                try: node = _ast.literal_eval(text)
                except (SyntaxError, ValueError): node = None
                if isinstance(node, tuple) and len(node) == 3:
                    spec.update({"start": node[0], "stop": node[1], "step": node[2]})
                else:
                    spec["values"] = text
            result[axis] = spec
        return result

    def _on_plan_setting_changed(self, _value=None):
        if not self._applying_freeze:
            self._clear_freeze()
        self._update_plan_visibility()
        self._update_position_preview()

    def _update_condition_preview(self, *_):
        """Refresh the visible expanded sequence summary from the editor draft."""
        if not self._conditions_grp.isChecked():
            self._condition_preview_lbl.setText("Disabled")
            return
        try:
            conditions = self._condition_editor.conditions()
            plans = self._condition_editor.rotation_plans() if hasattr(self._condition_editor, "rotation_plans") else {}
            values = {axis: list(plan.get("values", [])) for axis, plan in plans.items() if plan.get("values")}
            planned_sequence = self._condition_editor.sequence(values)
            sequence = self._custom_condition_sequence or planned_sequence
            count = len(sequence) if self._positions.size else 0
            custom = " · custom selection" if self._custom_condition_sequence is not None else ""
            self._condition_preview_lbl.setText(
                f"{count} sweeps × {len(self._positions)} points = "
                f"{count * len(self._positions)} spectra{custom}"
            )
        except Exception as exc:
            if self._positions.size == 0:
                self._condition_preview_lbl.setText("0 sweeps × 0 points = 0 spectra")
            else:
                self._condition_preview_lbl.setText(f"Invalid condition plan: {exc}")

    def _on_condition_editor_changed(self, *_):
        self._custom_condition_sequence = None
        self._update_condition_preview()

    def _update_gate_target_visibility(self, across_conditions: bool) -> None:
        """Hide legacy single-condition voltage targets in batch mode."""
        visible = not bool(across_conditions)
        self._gate_grp.setVisible(visible)
        form = getattr(self, "_gate_form", None)
        for field in getattr(self, "_gate_voltage_spins", ()):
            if form is not None and hasattr(form, "setRowVisible"):
                form.setRowVisible(field, visible)
            else:
                field.setVisible(visible)
                label = form.labelForField(field) if form is not None else None
                if label is not None:
                    label.setVisible(visible)

    def _planned_condition_sequence(self):
        conditions = self._condition_editor.conditions()
        plans = self._condition_editor.rotation_plans() if hasattr(self._condition_editor, "rotation_plans") else {}
        values = {axis: list(plan.get("values", [])) for axis, plan in plans.items() if plan.get("values")}
        sequence = self._condition_editor.sequence(values)
        rows = sequence_preview(sequence, conditions, values)
        for index, row in enumerate(rows):
            # Keep the human-facing one-based condition/rotation numbers from
            # sequence_preview while retaining the planner indexes for the
            # custom selection mapping.
            row.update({key: value for key, value in sequence[index].items()
                        if key not in {"condition_index", "rotation_index", "rotation_index2"}})
            condition = conditions[int(sequence[index]["condition_index"])]
            row["label"] = str(condition.get("label") or condition.get("condition_label") or
                                  (f"D={condition.get('doping_v', NAN):g}, F={condition.get('efield_v', NAN):g}"))
            row["point_count"] = len(self._positions)
            row["_source_index"] = index
        return conditions, values, sequence, rows

    def _show_condition_preview(self):
        try:
            _, _, sequence, rows = self._planned_condition_sequence()
        except Exception as exc:
            self._condition_preview_lbl.setText(f"Invalid condition plan: {exc}")
            return
        if self._custom_condition_sequence:
            by_source = {int(row.get("_source_index", -1)): row for row in rows}
            selected_sources = set()
            reopened = []
            for item in self._custom_condition_sequence:
                source = int(item.get("_source_index", -1))
                if source in by_source:
                    # Reuse the human-facing preview row.  The stored worker
                    # sequence uses zero-based condition indexes, whereas the
                    # dialog displays the one-based index from sequence_preview.
                    preview_row = dict(by_source[source])
                    preview_row["_source_index"] = source
                    preview_row["enabled"] = True
                    preview_row["use"] = True
                    reopened.append(preview_row)
                    selected_sources.add(source)
            excluded = []
            for row in rows:
                source = int(row.get("_source_index", -1))
                if source not in selected_sources:
                    preview_row = dict(row)
                    preview_row["enabled"] = False
                    preview_row["use"] = False
                    excluded.append(preview_row)
            rows = reopened + excluded
        dialog = MotionSequencePreviewDialog(rows, point_count=len(self._positions), parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected_rows = dialog.result_rows()
            self._custom_condition_sequence = []
            for row in selected_rows:
                source = int(row.get("_source_index", -1))
                if 0 <= source < len(sequence):
                    selected = dict(sequence[source]); selected["_source_index"] = source
                    self._custom_condition_sequence.append(selected)
            if not self._custom_condition_sequence:
                self._condition_preview_lbl.setText("Invalid condition plan: selection is empty")
            else:
                self._update_condition_preview()

    def _update_power_input_visibility(self, _index=None):
        is_range = self._power_kind.currentData() == "range"
        is_power = self._motion_combo.currentData() == "stage" and self._input_mode.currentData() == "power"
        self._power_kind.setVisible(is_power)
        for widget in (self._power_start_spin, self._power_end_spin, self._power_count_spin):
            widget.setVisible(is_power and is_range)
        self._power_range_edit.setVisible(is_power and not is_range)
        self._power_range_lbl.setVisible(is_power and not is_range)
        self._power_spacing.setVisible(is_power)
        for widget in (self._power_kind, self._power_start_spin, self._power_end_spin,
                       self._power_count_spin, self._power_range_edit, self._power_spacing):
            label = self._motion_form.labelForField(widget)
            if label is not None:
                label.setVisible(is_power and (widget is self._power_kind or
                                               widget is self._power_spacing or
                                               (widget is self._power_range_edit and not is_range) or
                                               (widget in (self._power_start_spin, self._power_end_spin, self._power_count_spin) and is_range)))

    def _update_plan_visibility(self):
        is_power = self._motion_combo.currentData() == "stage" and self._input_mode.currentData() == "power"
        self._pos_input.setVisible(not is_power)
        self._freeze_positions_btn.setVisible(is_power)
        self._freeze_positions_lbl.setVisible(is_power)
        self._update_power_input_visibility()

    # ── position preview ──────────────────────────────────────────────────────

    def _on_position_text_changed(self, _text: str):
        if not self._applying_freeze:
            self._clear_freeze()
        self._parse_timer.start()

    def _on_input_mode_changed(self, _index: int):
        if not self._applying_freeze and self._freeze_source is not None:
            self._clear_freeze()
        self._update_plan_visibility()
        self._update_position_preview()

    def _clear_freeze(self):
        self._frozen_positions = None
        self._frozen_target_powers = None
        self._freeze_source = None

    def _session_reference(self):
        result = self._cal_widget.reference()
        if result is None:
            return None
        try:
            pos, power = result
            if pos is None or power is None:
                return None
            pos, power = float(pos), float(power)
            if not (math.isfinite(pos) and math.isfinite(power) and power > 0):
                return None
            return pos, power
        except (TypeError, ValueError):
            return None

    def _calibration_snapshot(self, reference=None):
        cal = cfg.nd_calibration
        ref = self._session_reference() if reference is None else reference
        ref_signature = getattr(self._cal_widget, "_reference_signature", None)
        ref_factor = ref_signature[0] if isinstance(ref_signature, tuple) and ref_signature else power_correction_factor()
        ref_wavelength = ref_signature[1] if isinstance(ref_signature, tuple) and len(ref_signature) > 1 else self._pm_wl_spin.value()
        return {
            "profile_name": getattr(cal, "profile_name", "default"),
            "wavelength_nm": getattr(cal, "wavelength_nm", None),
            "positions": list(getattr(cal, "positions", [])),
            "powers": list(getattr(cal, "powers", [])),
            "raw_powers": list(getattr(cal, "raw_powers", [])),
            "correction_factor": float(getattr(cal, "correction_factor", 1.0)),
            "reference": ({"position": ref[0], "corrected_power_uw": ref[1], "source": "session_measurement", "correction_factor": ref_factor, "wavelength_nm": ref_wavelength} if ref else None),
        }

    def _power_targets(self) -> np.ndarray:
        if self._power_kind.currentData() == "list":
            return _parse_power_values(self._power_range_edit.text())
        return make_power_points(self._power_start_spin.value(), self._power_end_spin.value(), self._power_count_spin.value(), self._power_spacing.currentData())

    @staticmethod
    def _frozen_ramp_defaults() -> dict[str, float]:
        """Capture shared voltage ramp settings at run creation time."""
        return {
            "ramp_step_V": float(cfg.ramp.step_V),
            "step_delay_s": float(cfg.ramp.delay_s),
            "settle_s": float(cfg.ramp.settle_s),
            "vbias_step_V": float(cfg.ramp.vbias_step_V),
        }

    @Slot()
    def _update_position_preview(self):
        self._target_powers = None
        try:
            is_power = self._input_mode.currentData() == "power" and self._motion_combo.currentData() == "stage"
            ref = self._session_reference()
            if is_power:
                powers = self._power_targets()
                if ref is None:
                    raise ValueError("Measure a session reference before using target power mode.")
                cal = cfg.nd_calibration
                if len(cal.positions) < 2:
                    raise ValueError("No shared ND calibration is loaded.")
                self._target_powers = powers
                self._positions = positions_for_power(powers, cal.positions, cal.powers, reference_position=ref[0] if ref else None, reference_power=ref[1] if ref else None)
            else:
                if hasattr(self, "_axis_mode_combos"):
                    axis_modes = {axis: combo.currentData() for axis, combo in self._axis_mode_combos.items()}
                    active = [axis for axis in MOTION_AXES if axis_modes.get(axis) != MOTION_NOT_USED]
                    if not active:
                        # A gate-only acquisition still has one measurement
                        # per condition; there is no motion dimension.
                        parsed = np.array([0.0], dtype=float)
                    else:
                        fast = active[-1]
                        mode = axis_modes[fast]
                        values = ([0.0] if mode == MOTION_HOLD else
                                  parse_motion_values(self._axis_value_edits[fast].text(), mode=mode))
                        parsed = np.asarray(values, dtype=float)
                else:
                    parsed = _parse_sweep_values(self._pos_input.text())
                self._positions = parsed
                if self._frozen_positions is not None and np.array_equal(parsed, self._frozen_positions) and self._motion_combo.currentData() == "stage":
                    self._target_powers = self._frozen_target_powers.copy() if self._frozen_target_powers is not None else None
        except Exception as exc:
            self._positions = np.array([], dtype=float)
            self._target_powers = None
            self._clear_calibration_plot(clear_curve=True)
            self._pos_preview_lbl.setText(f"⚠ {exc}"); self._pos_preview_lbl.setStyleSheet("color: #b42318; font-size: 10px;")
            self._pos_count_lbl.setText(""); self._est_lbl.setText(""); self._power_preview_lbl.setText(f"⚠ {exc}")
            self._update_condition_preview()
            return
        self._pos_preview_lbl.setText(_describe_positions(self._positions)); self._pos_preview_lbl.setStyleSheet("color: #444; font-size: 10px;")
        n = len(self._positions); self._pos_count_lbl.setText(f"{n} value{'s' if n != 1 else ''}")
        try:
            cal = cfg.nd_calibration
            if self._motion_combo.currentData() == "stage" and len(cal.positions) >= 2:
                vals = predict_power(self._positions, cal.positions, cal.powers, reference_position=ref[0] if ref else None, reference_power=ref[1] if ref else None)
                unit = "corrected µW" if ref else "relative transmission"
                self._power_preview_lbl.setText(f"{unit}: {_describe_positions(np.asarray(vals))}")
            else:
                self._power_preview_lbl.setText("No shared ND calibration loaded; power preview unavailable.")
        except Exception as exc:
            self._power_preview_lbl.setText(f"⚠ {exc}")
        self._update_est(); self._update_filename_preview(); self._update_calibration_plot(); self._update_condition_preview()

    def _freeze_generated_positions(self):
        """Freeze the currently generated stage list while retaining target provenance."""
        # A text edit is debounced for responsive typing. Resolve all current
        # controls before copying so the button can never freeze stale values.
        self._parse_timer.stop()
        self._update_position_preview()
        if self._motion_combo.currentData() != "stage" or self._positions.size == 0:
            return
        self._frozen_positions = self._positions.copy()
        self._frozen_target_powers = None if self._target_powers is None else self._target_powers.copy()
        self._freeze_source = {
            "mode": self._input_mode.currentData(),
            "power_kind": self._power_kind.currentData(),
            "spacing": self._power_spacing.currentData(),
            "positions": self._positions.tolist(),
            "target_powers": (self._target_powers.tolist() if self._target_powers is not None else None),
            "generation_calibration": self._calibration_snapshot(),
        }
        self._applying_freeze = True
        try:
            self._pos_input.setText(str([float(v) for v in self._positions]))
            self._input_mode.setCurrentIndex(self._input_mode.findData("position"))
        finally:
            self._applying_freeze = False
        self._update_position_preview()
        if self._frozen_target_powers is not None and len(self._frozen_target_powers) == len(self._positions): self._target_powers = self._frozen_target_powers.copy()

    @Slot(bool)
    def _on_calibration_busy(self, busy: bool):
        self._calibration_busy = bool(busy)
        if busy and not self._cal_grp.isChecked():
            self._cal_grp.setChecked(True)
        self._cal_grp.setVisible(self._last_motion_key == "stage" or busy)
        self._motion_grp.setEnabled(not busy)
        self._run_btn.setEnabled(not busy and self._thread is None)
        self._emit_busy_state()

    @Slot(bool)
    def _set_calibration_expanded(self, expanded: bool):
        """Toggle only the calibration body; keep its useful header compact."""
        if self._calibration_busy and not expanded:
            self._cal_grp.setChecked(True)
            return
        self._cal_widget.setVisible(bool(expanded))

    def _emit_busy_state(self):
        state = bool(self._sweep_busy or self._calibration_busy)
        if state != self._last_busy_signal:
            self._last_busy_signal = state
            self.busy_changed.emit(state)

    def _update_est(self):
        if self._positions.size == 0:
            self._est_lbl.setText("")
            return
        n = len(self._positions)
        exp_s = self._exp_spin.value() / 1000.0 * self._frames_spin.value()
        per_pt = 1.2 + self._motion_settle_spin.value() + exp_s
        total_s = n * per_pt
        if total_s < 60:
            self._est_lbl.setText(f"Est: ~{total_s:.0f} s ({n} points)")
        elif total_s < 3600:
            m = total_s // 60
            s = int(total_s % 60)
            self._est_lbl.setText(
                f"Est: ~{m:.0f} min {s} s ({n} points)"
            )
        else:
            h = total_s // 3600
            m = int((total_s % 3600) // 60)
            self._est_lbl.setText(
                f"Est: ~{h:.0f} h {m} min ({n} points)"
            )

    # ── filename preview ──────────────────────────────────────────────────────

    @Slot()
    def _update_filename_preview(self):
        try:
            devid = self._devid_edit.text().strip()
            sub = self._subfolder_edit.text().strip() or "motion_sweep"
            laser = self._laser_edit.text().strip()
            center = self._center_spin.value()
            exp_ms = self._exp_spin.value()
            frames = self._frames_spin.value()

            ctx = FilenameContext(
                device_id=devid,
                tag="",
                temperature="",
                mode="",
                laser_nm=laser,
                nominal_power_uw=None,
                center_nm=center,
                exposure_ms=exp_ms,
                accumulations=frames,
                condition_label=(
                    f"motion_sweep_"
                    f"{self._motion_combo.currentData() or 'stage'}"
                ),
            )
            enabled = ["laser_power", "center", "exposure", "condition"]
            base = build_base_filename(ctx, enabled)
            # A pending run will apply the values in the gate controls.  Using
            # a readback here made the preview lag one operation behind the
            # user's input.  Readbacks remain the source for externally
            # controlled gates.
            vbg, vtg, vbias = self._filename_gate_values()
            if vbg is not None and not self._conditions_grp.isChecked():
                gt = _build_gate_token(vbg, vtg, vbias)
                if gt:
                    base = f"{base}_{gt}"
            folder = Path(cfg.filename.base_out) / (devid or "SampleID") / sub
            self._filename_lbl.setText(
                f"{base}.csv\n→ {folder}"
            )
            self._filename_lbl.setStyleSheet(
                "color: #555; font-size: 10px;"
            )
        except Exception as exc:
            self._filename_lbl.setText(f"⚠ {exc}")
            self._filename_lbl.setStyleSheet(
                "color: #b42318; font-size: 10px;"
            )

    # ── status lamp slots ─────────────────────────────────────────────────────

    @Slot(str)
    def _on_stage_connected(self, _key: str = ""):
        self._stage_status.setText("● Connected")
        self._stage_status.setStyleSheet(
            "color: green; font-weight: bold;"
        )
        if self._motion_combo.currentData() == "stage":
            self._on_motion_changed()

    @Slot()
    def _on_stage_disconnected(self):
        self._stage_status.setText("○ Not connected")
        self._stage_status.setStyleSheet(
            "color: gray; font-weight: bold;"
        )

    def _set_rotation_status(self, slot: str, connected: bool):
        label = {
            "rot1": self._rot1_status,
            "rot2": self._rot2_status,
        }.get(slot)
        if label is None:
            return
        label.setText("● Connected" if connected else "○ Not connected")
        label.setStyleSheet(
            f"color: {'green' if connected else 'gray'}; font-weight: bold;"
        )

    @Slot(str, str)
    def _on_rotation_connected(self, slot: str, _backend: str):
        self._set_rotation_status(slot, True)

    @Slot(str)
    def _on_rotation_disconnected(self, slot: str):
        self._set_rotation_status(slot, False)

    @Slot()
    def _on_pm_connected(self):
        self._pm_status.setText("● Connected")
        self._pm_status.setStyleSheet(
            "color: green; font-weight: bold;"
        )
        self._pm_notice_lbl.setVisible(False)

    @Slot()
    def _on_pm_disconnected(self):
        self._pm_status.setText("○ Not connected (optional)")
        self._pm_status.setStyleSheet(
            "color: #9a6700; font-weight: bold;"
        )
        self._pm_notice_lbl.setVisible(True)

    @Slot(list)
    def _on_lf6_connected(self, _experiments=None):
        self._lf6_status.setText("● Connected")
        self._lf6_status.setStyleSheet(
            "color: green; font-weight: bold;"
        )

    @Slot()
    def _on_lf6_disconnected(self):
        self._lf6_status.setText("○ Not connected")
        self._lf6_status.setStyleSheet(
            "color: gray; font-weight: bold;"
        )

    @Slot(list)
    def _on_smu_connected(self, _opened=None):
        self._smu_status.setText("● Connected")
        self._smu_status.setStyleSheet(
            "color: green; font-weight: bold;"
        )
        self._gate_grp.setEnabled(True)
        has_vb = getattr(self._smu, "has_vbias", False)
        self._vbias_spin.setEnabled(has_vb)

    @Slot()
    def _on_smu_disconnected(self):
        self._smu_status.setText("○ Not connected")
        self._smu_status.setStyleSheet(
            "color: gray; font-weight: bold;"
        )
        self._gate_grp.setEnabled(False)

    # ── gate snapshot ──────────────────────────────────────────────────────────

    def _read_gate_snapshot(self) -> tuple[float, float, float]:
        """Read current gate voltages (non-blocking, main-thread safe)."""
        if self._smu is None or not bool(getattr(self._smu, "is_connected", False)):
            return 0.0, 0.0, 0.0
        iv = self._smu.device
        vbg, vtg = _read_gates(iv)
        vbias = _read_bias(iv)
        return vbg, vtg, vbias

    def _filename_gate_values(self) -> tuple[float, float, float] | tuple[None, None, None]:
        """Return the gate values represented by the filename preview.

        When this panel owns gate application, the targets are the only
        values known before the ramp runs.  Otherwise use the latest hardware
        readback.  The run stores both target and observed values separately.
        """
        if self._apply_gates_chk.isChecked():
            return (float(self._vbg_spin.value()), float(self._vtg_spin.value()),
                    float(self._vbias_spin.value()))
        if self._smu is not None and bool(getattr(self._smu, "is_connected", False)):
            return self._read_gate_snapshot()
        return (None, None, None)

    # ── validation ────────────────────────────────────────────────────────────

    def _validate(self) -> bool:
        if self._positions.size == 0:
            QMessageBox.critical(
                self, "No sweep values",
                "Enter at least one position or angle."
            )
            return False

        if hasattr(self, "_axis_mode_combos"):
            active = [axis for axis, combo in self._axis_mode_combos.items()
                      if combo.currentData() != MOTION_NOT_USED]
            for axis in active:
                if axis == "stage":
                    connected = self._stg is not None and bool(getattr(self._stg, "is_connected", False)) and getattr(self._stg, "adapter", None) is not None
                else:
                    try: connected = self._rot is not None and self._rot.is_connected(axis) and self._rot.adapter(axis) is not None
                    except Exception: connected = False
                if not connected:
                    QMessageBox.critical(self, f"{axis} not connected", f"Connect {axis} before running this motion sweep.")
                    return False
            if self._lf6 is None or not bool(getattr(self._lf6, "is_connected", False)):
                QMessageBox.critical(self, "LF6 not connected", "Connect the spectrometer before running a motion sweep.")
                return False
            if self._apply_gates_chk.isChecked() and (self._smu is None or not bool(getattr(self._smu, "is_connected", False))):
                QMessageBox.critical(self, "SMU not connected", "Gate voltages are requested but SMU is not connected.")
                return False
            return True

        motion_key = self._motion_combo.currentData() or "stage"
        motion_spec = _MOTION_SPECS[motion_key]
        if motion_key == "stage":
            motion_connected = (
                self._stg is not None and bool(getattr(self._stg, "is_connected", False))
            )
            adapter = self._stg.adapter if motion_connected else None
        else:
            motion_connected = (
                self._rot is not None
                and callable(getattr(self._rot, "is_connected", None)) and self._rot.is_connected(motion_key)
            )
            adapter = (
                self._rot.adapter(motion_key) if motion_connected else None
            )
        if not motion_connected or adapter is None:
            QMessageBox.critical(
                self, f"{motion_spec['label']} not connected",
                f"Connect {motion_spec['label']} before running this "
                "motion sweep."
            )
            return False

        if self._lf6 is None or not bool(getattr(self._lf6, "is_connected", False)):
            QMessageBox.critical(
                self, "LF6 not connected",
                "Connect the spectrometer before running a motion sweep."
            )
            return False

        if self._apply_gates_chk.isChecked():
            if self._smu is None or not bool(getattr(self._smu, "is_connected", False)):
                QMessageBox.critical(
                    self, "SMU not connected",
                    "Gate voltages are requested but SMU is not connected.\n"
                    "Connect the SMU or uncheck \"Apply gate voltages\"."
                )
                return False

        # Check values against the selected actuator's limits.
        if motion_key == "stage":
            lo = float(adapter.minimum_position)
            hi = float(adapter.maximum_position)
            unit = getattr(adapter, "position_unit", "stage units")
        else:
            lo, hi = _ROTATION_MIN_DEG, _ROTATION_MAX_DEG
            unit = "deg"
        for i, pos in enumerate(self._positions):
            if not math.isfinite(float(pos)) or pos < lo or pos > hi:
                QMessageBox.critical(
                    self, "Sweep value out of range",
                    f"Value {pos:.3f} (index {i}) is outside the\n"
                    f"{motion_spec['label']} range [{lo:g}, {hi:g}] {unit}."
                )
                return False

        n = len(self._positions)
        if n > 500:
            reply = QMessageBox.warning(
                self, "Large sweep",
                f"This sweep has {n} positions and may take a long time.\n"
                "Continue anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return False

        return True

    # ── run / stop ────────────────────────────────────────────────────────────

    @Slot()
    def _on_run(self):
        # Ensure a pending debounced edit cannot leave stale sweep positions.
        self._update_position_preview()
        if not self._validate():
            return
        devid = self._devid_edit.text().strip()
        if not devid:
            QMessageBox.warning(self, "Sample ID required", "Enter a Sample ID before starting the sweep.")
            return
        sub = self._subfolder_edit.text().strip() or "motion_sweep"
        out_path = str(Path(cfg.filename.base_out) / devid / sub)
        apply_gates = self._apply_gates_chk.isChecked()
        vbg_set, vtg_set, vbias_set = self._vbg_spin.value(), self._vtg_spin.value(), self._vbias_spin.value()
        conditions = []
        condition_sequence = []
        rotation_axis = ""
        rotation_values = {}
        rotation_axes = []
        if self._conditions_grp.isChecked():
            try:
                if not apply_gates:
                    raise ValueError("Across conditions requires Apply gate voltages")
                # The table is the authoritative frozen representation.
                conditions = self._condition_editor.conditions()
                if not conditions:
                    raise ValueError("At least one enabled condition is required")
                # Keep-current plans are intentionally represented by empty
                # arrays. Resolving them never reads or connects a rotation
                # controller; the worker preserves the current angle.
                if hasattr(self._condition_editor, "rotation_plans"):
                    plans = self._condition_editor.rotation_plans()
                    rotation_values = {axis: list(plan.get("values", []))
                                       for axis, plan in plans.items() if plan.get("values")}
                    rotation_axes = list(rotation_values)
                    rotation_axis = rotation_axes[0] if rotation_axes else ""
                    condition_sequence = self._condition_editor.sequence(rotation_values)
                    if self._custom_condition_sequence is not None:
                        condition_sequence = [dict(item) for item in self._custom_condition_sequence]
                else:
                    rotation_axis = self._condition_editor.rot_axis.currentData() or ""
                    plan = resolve_rotation_plan(self._condition_editor.rot_plan.text(), {}, axis=rotation_axis) if rotation_axis else {"values": []}
                    rotation_values = {rotation_axis: plan["values"]} if plan.get("values") else {}
                    rotation_axes = list(rotation_values)
                    condition_sequence = self._condition_editor.sequence(rotation_values)
                preview_rows = sequence_preview(condition_sequence, conditions, rotation_values)
                preview_text = [f"{len(condition_sequence)} sweeps × {len(self._positions)} points = {len(condition_sequence) * len(self._positions)} spectra"]
                for row in preview_rows[:24]:
                    rotations_text = ", ".join(f"{axis}={row[axis]:g}" for axis in ("rot1", "rot2") if row.get(axis) is not None)
                    coords = f"D={row.get('D'):g}, F={row.get('F'):g}" if row.get('D') is not None else f"Vtg={row.get('Vtg'):g}, Vbg={row.get('Vbg'):g}"
                    preview_text.append(f"{row['sequence']}: {coords}" + (f", {rotations_text}" if rotations_text else "") + f", repeat {row['repeat']}")
                if len(preview_rows) > 24:
                    preview_text.append(f"… {len(preview_rows) - 24} more")
                self._condition_preview_lbl.setText("\n".join(preview_text))
                # Retain compatibility gate fields in the run settings for
                # older metadata readers, while the sequence owns the actual
                # gate targets.
                if conditions:
                    vbg_set = conditions[0].get("vbg_v", vbg_set)
                    vtg_set = conditions[0].get("vtg_v", vtg_set)
                    vbias_set = conditions[0].get("vbias_v", vbias_set)
            except Exception as exc:
                QMessageBox.critical(self, "Condition batch error", str(exc)); return
        # Freeze the same values used by the preview.  The worker will apply
        # these targets before acquiring; reading hardware here captured the
        # previous gate condition and produced stale filenames.
        filename_gates = self._filename_gate_values()
        try:
            ctx = FilenameContext(device_id=devid, tag="", temperature="", mode="",
                laser_nm=self._laser_edit.text().strip(), nominal_power_uw=None,
                center_nm=self._center_spin.value(), exposure_ms=self._exp_spin.value(),
                accumulations=self._frames_spin.value(),
                condition_label=f"motion_sweep_{self._motion_combo.currentData() or 'stage'}",
                point=self._point_edit.text().strip())
            base_name = build_base_filename(ctx, ["laser_power", "center", "exposure", "condition"])
            # A condition sequence owns its gate values.  The legacy gate
            # spins are only a compatibility draft and must not leak into its
            # base filename.
            if filename_gates[0] is not None and not condition_sequence:
                gt = _build_gate_token(*filename_gates)
                if gt: base_name = f"{base_name}_{gt}"
        except Exception as exc:
            QMessageBox.critical(self, "Filename error", f"Could not build filename: {exc}")
            return
        params = {"device_id": devid, "positions": self._positions.copy(),
            "target_powers": self._target_powers.tolist() if self._target_powers is not None else None,
            "motion_key": self._motion_combo.currentData() or "stage",
            "motion_settle_s": self._motion_settle_spin.value(),
            "return_motion_to_start": self._return_motion_chk.isChecked(),
            "center_nm": self._center_spin.value(), "exp_ms": self._exp_spin.value(),
            "frames": self._frames_spin.value(), "pm_wl_nm": self._pm_wl_spin.value(),
            "out_path": out_path, "base_name": base_name, "apply_gates": apply_gates,
            "Vbg_target": vbg_set, "Vtg_target": vtg_set, "Vbias_target": vbias_set,
            # Freeze shared voltage-ramp defaults when the run is created so
            # settings edits made while the panel is open are honored.
            **self._frozen_ramp_defaults(),
            "return_to_zero": self._return_zero_chk.isChecked()}
        # Freeze the new independent-axis model in the run manifest.  The
        # legacy motion_key/positions fields above remain for older readers.
        try:
            params["motion_axes"] = self._motion_axes_from_ui()
            params["axis_order"] = list(self._axis_order_combo.currentData() or ("stage", "rot1", "rot2"))
            params["repeat"] = int(self._condition_editor.repeats.value()) if self._conditions_grp.isChecked() else 1
            if params["motion_axes"] and conditions:
                params["conditions"] = conditions
        except Exception as exc:
            QMessageBox.critical(self, "Motion plan error", str(exc)); return
        if condition_sequence:
            params.update({"conditions": conditions, "condition_sequence": condition_sequence,
                           "rotation_axis": rotation_axis, "rotation_axes": rotation_axes,
                           "rotation_values": rotation_values,
                           "rotation_settle_s": self._motion_settle_spin.value(),
                           "sequence_order": self._condition_editor.order.currentData()})
            params["plan_schema"] = "legacy"
        ref = self._session_reference()
        snapshot = self._calibration_snapshot(ref)
        snapshot["planned_positions_source"] = dict(self._freeze_source) if self._freeze_source else {"mode": self._input_mode.currentData() or "position"}
        params["nd_calibration"] = snapshot
        self._run_btn.setEnabled(False); self._run_failed = False
        self._sweep_busy = True
        self._run_finalized = False
        self._emit_busy_state()
        self._cal_widget.set_external_busy(True)
        params["power_correction_factor"] = power_correction_factor()
        self._run_metadata_params = dict(params)
        self._run_files_before = set(Path(out_path).glob("*"))
        try:
            self._experiment_run = ExperimentMetadataService(out_path).begin(
                "motion_sweep", devid, output_dir=out_path, settings=params,
                instruments=instrument_inventory(lightfield=self._lf6, smu=self._smu,
                                                 stage=self._stg, rotation=self._rot,
                                                 power_meter=self._pm),
            )
            bind_lightfield_metadata(self._lf6, self._experiment_run)
        except Exception as exc:
            self._on_error(f"Metadata error; run blocked: {exc}")
            self._experiment_run = None
            self._run_btn.setEnabled(True)
            self._cal_widget.set_external_busy(False)
            self._sweep_busy = False
            self._emit_busy_state()
            return
        self._conditions_ui_was_enabled = self._conditions_grp.isEnabled()
        self._conditions_grp.setEnabled(False)
        self._condition_editor.setEnabled(False)
        self._condition_preview_btn.setEnabled(False)
        self._motion_grp.setEnabled(False); self._stop_btn.setEnabled(True); self._progress.setValue(0)
        self._log.clear(); self._status_lbl.setText("Running…"); self._status_lbl.setStyleSheet("color: #b26a00; font-size: 11px;")
        self._worker = _PowerSweepWorker(params, self._stg, self._rot, self._pm, self._lf6, self._smu, self._experiment_run)
        self._thread = QThread()
        self._worker.moveToThread(self._thread); self._thread.started.connect(self._worker.run)
        self._worker.log.connect(self._on_log); self._worker.progress.connect(self._on_progress)
        self._worker.spectrum.connect(self._on_spectrum)
        # Quit the worker thread directly from the worker's finished emission;
        # waiting for a queued GUI slot here can deadlock during application close.
        self._worker.finished.connect(self._thread.quit, Qt.DirectConnection)
        self._worker.finished.connect(self._on_finished, Qt.QueuedConnection)
        self._thread.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._on_error); self._thread.start()

    def _clear_calibration_plot(self, *, clear_curve=False):
        if clear_curve:
            self._cal_curve.clear()
            self._cal_points.clear()
        self._cal_planned_points.clear()

    def _update_calibration_plot(self):
        cal = cfg.nd_calibration
        try:
            x = np.asarray(cal.positions, dtype=float).ravel()
            y = np.asarray(cal.powers, dtype=float).ravel()
            if x.size < 2 or y.size != x.size or np.any(~np.isfinite(y)) or np.any(y <= 0):
                self._clear_calibration_plot(clear_curve=True)
                return
            order = np.argsort(x); x, y = x[order], y[order]
            dense = np.linspace(float(x[0]), float(x[-1]), max(100, x.size * 20))
            curve = predict_power(dense, x, y, reference_position=None, reference_power=None)
            # Plot relative values without a reference; measured references scale
            # both the curve and the planned points to corrected sample µW.
            ref = self._session_reference()
            if ref:
                curve = predict_power(dense, x, y, reference_position=ref[0], reference_power=ref[1])
                knots = predict_power(x, x, y, reference_position=ref[0], reference_power=ref[1])
            else:
                knots = predict_power(x, x, y)
            self._cal_curve.setData(dense, curve)
            self._cal_points.setData(x, knots)
            if self._positions.size:
                planned = predict_power(self._positions, x, y, reference_position=ref[0] if ref else None, reference_power=ref[1] if ref else None)
                self._cal_planned_points.setData(self._positions, planned)
            else:
                self._cal_planned_points.clear()
            self._cal_plot.setLabel("left", "Corrected power" if ref else "Relative transmission", units="µW" if ref else None)
            self._cal_plot.setLogMode(False, self._cal_log_chk.isChecked())
        except Exception:
            self._clear_calibration_plot(clear_curve=True)

    @Slot()
    def _on_stop(self):
        if self._worker is not None:
            self._worker.request_stop()
        self._stop_btn.setEnabled(False)
        self._status_lbl.setText("Stopping…")
        self._status_lbl.setStyleSheet("color: #b26a00; font-size: 11px;")

    @Slot(str)
    def _on_log(self, msg: str):
        self._log.append(msg)
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    @Slot(int, int)
    def _on_progress(self, done: int, total: int):
        if total > 0:
            self._progress.setValue(int(100.0 * done / total))
        self._status_lbl.setText(f"{done}/{total}")

    @Slot(object, object)
    def _on_spectrum(self, wl: np.ndarray, cts: np.ndarray):
        self._curve.setData(
            np.asarray(wl, dtype=float), np.asarray(cts, dtype=float)
        )

    @Slot(str)
    def _on_error(self, msg: str):
        self._log.append(f"ERROR: {msg}")
        self._status_lbl.setText("Error")
        self._status_lbl.setStyleSheet("color: red; font-size: 11px;")
        self._run_failed = True
        self._positions = np.array([], dtype=float)
        self._target_powers = None
        self._clear_calibration_plot(clear_curve=True)

    @Slot()
    def _on_finished(self):
        if self._run_finalized and self._worker is None:
            return
        worker = self._worker
        self._run_btn.setEnabled(True)
        self._motion_grp.setEnabled(True)
        self._stop_btn.setEnabled(False)
        if self._status_lbl.text() not in ("Error", "Stopping…"):
            self._status_lbl.setText("Ready")
            self._status_lbl.setStyleSheet(
                "color: #707070; font-size: 11px;"
            )
        if self._thread:
            thread = self._thread
            thread.quit()
            if thread.isRunning() and not thread.wait(2000):
                # Keep ownership so shutdown() can retry and the UI never
                # loses track of a still-running hardware worker.
                self._log.append("ERROR: Sweep thread did not stop after completion.")
                self._status_lbl.setText("Stopping…")
                return
            self._thread = None
        run = getattr(self, "_experiment_run", None)
        if run is not None:
            try:
                for data_file in Path(run.path.parent).glob("*"):
                    if data_file == run.path or data_file in getattr(self, "_run_files_before", set()) or data_file.suffix.lower() not in {".csv", ".log", ".json", ".txt"}:
                        continue
                    run.register_file(data_file, "raw" if data_file.suffix.lower() == ".csv" else "intermediate")
                if getattr(self, "_run_failed", False):
                    run.fail("motion sweep failed")
                elif worker is not None and worker._stop.is_set():
                    run.cancel("user stop")
                else:
                    run.complete()
            except Exception as exc:
                self._on_error(f"Metadata finalization error: {exc}")
        self._worker = None
        self._run_finalized = True
        self._cal_widget.set_external_busy(False)
        self._sweep_busy = False
        self._conditions_grp.setEnabled(self._conditions_ui_was_enabled)
        self._condition_editor.setEnabled(self._conditions_ui_was_enabled and self._conditions_grp.isChecked())
        self._condition_preview_btn.setEnabled(self._conditions_ui_was_enabled and self._conditions_grp.isChecked())
        self._emit_busy_state()

    def shutdown(self, timeout_ms=30000):
        """Stop measurement and calibration workers before controller teardown."""
        if self._worker is not None:
            self._worker.request_stop()
        if self._thread is not None:
            thread = self._thread
            thread.quit()
            if not thread.wait(int(timeout_ms)):
                return False
            # closeEvent runs with the GUI event loop blocked, so the queued
            # completion slot may not execute. Finalize here with the captured
            # worker stop state; the later queued slot is idempotent.
            self._on_finished()
        return bool(self._cal_widget.shutdown(timeout_ms))
