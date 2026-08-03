# ui/presets_panel.py
# ──────────────────────────────────────────────────────────────────────────────
# Presets sweep panel.
#
# Loop table changes (vs original):
#   - "Parameter" column is now a QComboBox dropdown (not free text).
#   - "Level" renamed to "Group".
#   - Three loop modes selected via combobox above the table:
#       Synchronize  – each enabled row = its own loop level, nested from top
#                      to bottom (row 1 = outermost, row 2 = next inner, …).
#                      Result is a Cartesian product.
#       Zip          – all enabled rows are zipped together (must have equal
#                      number of values).
#       Customized   – user assigns a Group number per row.  Rows with the
#                      same Group are zipped; Groups are producted (same
#                      as the old Level system).
#   - Group column hidden in Synchronize and Zip modes; visible in Customized.
#
# Batch table changes:
#   - Column order: Run | When | MeasurePower | condition_label | repeat |
#     frames | Vbg_start | Vbg_stop | Vtg_start | Vtg_stop |
#     Vbias_start | Vbias_stop
#   - Column widths tuned so important fields are always visible.
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import itertools
import json
import re
import sys
import time
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from PySide6.QtCore import Qt, QThread, QObject, Signal, Slot, QTimer
from PySide6.QtGui import QAction, QColor, QKeySequence
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QSplitter,
    QGroupBox, QLabel, QPushButton, QLineEdit,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QProgressBar, QTextEdit, QSizePolicy, QFormLayout,
    QCheckBox, QAbstractItemView, QComboBox, QFrame, QToolButton,
    QCompleter, QStyledItemDelegate, QMessageBox, QDoubleSpinBox, QSpinBox,
    QAbstractSpinBox, QMenu, QScrollArea,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from utils.config import cfg
from utils.hardware_incidents import (
    HardwareIncidentRecorder,
    build_hardware_incident,
    incident_display_text,
)
from utils.when_condition import (
    evaluate_when_expression,
    validate_when_expression,
)
from app.devices.stage_adapter import get_linear_stage_profile
from app.devices.spectrum_alignment import align_wavelengths_to_image, align_wavelengths_to_intensities
from utils.filename_builder import (
    FilenameContext,
    PART_SPECS,
    build_base_filename,
    build_condition_display_label,
    build_filename_tokens,
    build_part_values,
    clean_condition_label,
    make_unique_stem,
    resolve_power_uw,
)
from ui.preview_widget import RunPlanTree
from ui.dual_gate_spectrum_viewer import DualGateSpectrumViewer
from utils.dual_gate_preview import load_last_dual_gate_acquisition

# ── Schema constants ───────────────────────────────────────────────────────────

# Ordered list of selectable loop parameters (shown in dropdown).
LOOP_PARAMS = [
    "Center Wavelength (nm)",
    "Exposure Time (ms)",
    "Accumulations (EPF)",
    "Rotation1 Angle (deg)",
    "Rotation2 Angle (deg)",
    "Stage Position",
]

# Loop table columns.  Group is hidden in Synchronize/Zip modes.
LOOP_SCHEMA = ["Enable", "Parameter", "Values", "Group"]

# Batch table columns – important filter/flag columns first.
BATCH_SCHEMA = [
    "Run", "When", "MeasurePower",
    "condition_label", "repeat", "frames",
    "Vbg_start", "Vbg_stop", "Vtg_start", "Vtg_stop",
    "Vbias_start", "Vbias_stop",
]

# Batch column widths (px). Keep the label readable, but do not let it
# dominate the numeric sweep columns.
_BATCH_COL_WIDTHS = {
    "Run":             38,
    "When":            108,
    "MeasurePower":    82,
    "condition_label": 144,
    "repeat":          52,
    "frames":          52,
    "Vbg_start":       68,
    "Vbg_stop":        68,
    "Vtg_start":       68,
    "Vtg_stop":        68,
    "Vbias_start":     72,
    "Vbias_stop":      72,
}
# Source-drain bias columns stay visible; blank values still mean skip.
_VBIAS_COLUMNS = ("Vbias_start", "Vbias_stop")

_BATCH_BOOL_COLUMNS = {"Run", "MeasurePower"}
_BATCH_INT_COLUMNS = {"repeat", "frames"}
_BATCH_FLOAT_COLUMNS = {"Vbg_start", "Vbg_stop", "Vtg_start", "Vtg_stop", "Vbias_start", "Vbias_stop"}

_BATCH_STRETCH_COLUMNS = {"When", "condition_label"}

# Loop mode labels and their tooltips.
LOOP_MODES = {
    "Synchronize": (
        "Each enabled row is a separate loop level.\n"
        "Row 1 = outermost loop, row 2 = inner, …\n"
        "Result: Cartesian product of all enabled parameters."
    ),
    "Zip": (
        "All enabled rows are iterated together in lockstep.\n"
        "All must have the same number of values.\n"
        "Result: N sequences where N = number of values per row."
    ),
    "Customized": (
        "Rows with the same Group number are zipped together.\n"
        "Different Groups form a Cartesian product.\n"
        "Shows the Group column for manual assignment."
    ),
}

_INVALID_CHARS = r'<>:"/\|?*'

# ── Default table contents ─────────────────────────────────────────────────────

_DEFAULT_LOOP = pd.DataFrame([
    {"Enable": True,  "Parameter": "Center Wavelength (nm)", "Values": "860",  "Group": 1},
    {"Enable": False, "Parameter": "Exposure Time (ms)",     "Values": "2000", "Group": 1},
    {"Enable": False, "Parameter": "Accumulations (EPF)",    "Values": "1",    "Group": 1},
    {"Enable": False, "Parameter": "Rotation1 Angle (deg)",  "Values": "0",    "Group": 2},
    {"Enable": False, "Parameter": "Rotation2 Angle (deg)",  "Values": "0",    "Group": 2},
    {"Enable": False, "Parameter": "Stage Position",         "Values": "0",    "Group": 2},
])

_DEFAULT_BATCH = pd.DataFrame([{
    "Run": True, "When": "", "MeasurePower": False,
    "condition_label": "baseline", "repeat": 1, "frames": 1,
    "Vbg_start": 0.0, "Vbg_stop": 0.0,
    "Vtg_start": 0.0, "Vtg_stop": 0.0,
    "Vbias_start": "", "Vbias_stop": "",
}])


# ── Pure data helpers ──────────────────────────────────────────────────────────

def _to_bool(x) -> bool:
    if isinstance(x, bool): return x
    if isinstance(x, str):  return x.strip().lower() in ("true", "1", "yes", "x", "\u2713")
    try: return bool(int(x))
    except Exception: return False


def _sanitize(s: str) -> str:
    for ch in _INVALID_CHARS:
        s = s.replace(ch, "")
    return s.strip()


def _parse_values(s: str, param: str = "") -> Optional[List[float]]:
    if not s or not str(s).strip():
        return None
    raw = str(s).strip()
    raw_lower = raw.lower()
    paren_linspace = raw.startswith("(") and raw.endswith(")")
    linspace_mode = paren_linspace
    if raw_lower.startswith("linspace(") and raw.endswith(")"):
        raw = raw[len("linspace("):-1].strip()
        linspace_mode = True
    if paren_linspace:
        raw = raw[1:-1].strip()
    try:
        nums = [float(x.strip()) for x in raw.replace(";", ",").split(",") if x.strip()]
    except ValueError:
        return None
    if str(param).startswith("Stage Position") and linspace_mode and len(nums) == 3:
        a, b, n = nums
        if float(n).is_integer() and int(n) >= 2:
            return np.linspace(a, b, int(n)).tolist()
    return nums


def _normalize_loop(df: pd.DataFrame) -> pd.DataFrame:
    df = (df if isinstance(df, pd.DataFrame) else pd.DataFrame()).copy()
    for c in LOOP_SCHEMA:
        if c not in df.columns:
            df[c] = "" if c not in ("Group",) else 1
    df = df[LOOP_SCHEMA].reset_index(drop=True)
    df["Enable"] = df["Enable"].map(_to_bool)
    df["Group"]  = pd.to_numeric(df["Group"], errors="coerce").fillna(1).astype(int)
    return df


def _normalize_batch(df: pd.DataFrame) -> pd.DataFrame:
    df = (df if isinstance(df, pd.DataFrame) else pd.DataFrame()).copy()
    for c in BATCH_SCHEMA:
        if c not in df.columns:
            df[c] = ""
    df = df[BATCH_SCHEMA].reset_index(drop=True)
    df["Run"]          = df["Run"].map(_to_bool)
    df["MeasurePower"] = df["MeasurePower"].map(_to_bool)
    for c in ("repeat", "frames"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(1).astype(int)
    for c in ("Vbg_start", "Vbg_stop", "Vtg_start", "Vtg_stop"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    for c in ("Vbias_start", "Vbias_stop"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["When"] = df["When"].fillna("").astype(str)
    return df


def _when_ok(when_str: str, ctx: dict) -> bool:
    return evaluate_when_expression(when_str, ctx)


def _outer_ctx(ctx: dict) -> dict:
    out = dict(ctx)
    for k, v in list(ctx.items()):
        short = k.split("(")[0].strip().replace(" ", "_")
        if short not in out:
            out[short] = v
    return out


def _active_stage_profile():
    return get_linear_stage_profile(getattr(cfg.stage, "backend", "elliptec"))


def _validate_stage_position_value(value: Any) -> float:
    profile = _active_stage_profile()
    pos = float(value)
    if not (profile.minimum_position <= pos <= profile.maximum_position):
        raise ValueError(
            f"Stage Position {pos:g} is outside the active {profile.display_name} range "
            f"({profile.minimum_position:g} to {profile.maximum_position:g} {profile.position_unit})."
        )
    return pos


def _build_plan(loop_df: pd.DataFrame, batch_df: pd.DataFrame,
                mode: str = "Synchronize"):
    """
    Return (final_sequence, enabled_df_batch, total_acq).

    mode:
      "Synchronize" – each enabled row = its own Level (Cartesian product,
                      ordered top-to-bottom in the table).
      "Zip"         – all enabled rows share Level 1 (zipped together).
      "Customized"  – use the Group column as Level.
    """
    loop = _normalize_loop(loop_df)
    active = loop[loop["Enable"]].reset_index(drop=True).copy()

    if mode == "Zip":
        active["Level"] = 1
    elif mode == "Synchronize":
        active["Level"] = range(1, len(active) + 1)
    else:  # Customized
        active["Level"] = active["Group"].fillna(1).astype(int)

    levels: Dict[int, List[dict]] = {}
    for _, row in active.iterrows():
        vals = _parse_values(str(row["Values"]), str(row["Parameter"]))
        if vals and str(row["Parameter"]).startswith("Stage Position"):
            vals = [_validate_stage_position_value(v) for v in vals]
        if vals:
            levels.setdefault(int(row["Level"]), []).append(
                {"p": row["Parameter"], "v": vals}
            )

    level_combos = []
    for lvl in sorted(levels.keys()):
        specs = levels[lvl]
        lengths = [len(s["v"]) for s in specs]
        if len(set(lengths)) > 1:
            raise ValueError(
                f"Group {lvl}: value-count mismatch {lengths}. "
                "All rows in the same group must have the same number of values."
            )
        zipped = list(zip(*[s["v"] for s in specs]))
        level_combos.append([{s["p"]: v for s, v in zip(specs, z)} for z in zipped])

    if not level_combos:
        final_sequence = [{}]
    else:
        final_sequence = []
        for combo in itertools.product(*level_combos):
            ctx: dict = {}
            for d in combo:
                ctx.update(d)
            final_sequence.append(ctx)

    batch = _normalize_batch(batch_df)
    batch = batch[batch["Run"]].reset_index(drop=True)

    total_acq = 0
    for ctx in final_sequence:
        outer = _outer_ctx(ctx)
        for _, r in batch.iterrows():
            if _when_ok(r.get("When", ""), outer):
                total_acq += max(int(r.get("repeat", 1)), 1)

    return final_sequence, batch, max(total_acq, 0)


def _sweep_point_count(row: Dict[str, Any]) -> int:
    return max(int(row.get("frames", 1) or 1), 1)


def _count_total_points(final_sequence: Sequence[Dict[str, Any]], batch_df: pd.DataFrame) -> int:
    total_points = 0
    for ctx in final_sequence:
        outer = _outer_ctx(ctx)
        for _, row in batch_df.iterrows():
            if _when_ok(row.get("When", ""), outer):
                reps = max(int(row.get("repeat", 1) or 1), 1)
                total_points += reps * _sweep_point_count(row.to_dict())
    return max(int(total_points), 0)


def _resolve_sweep_vectors(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        vbg_s = float(row["Vbg_start"])
        vbg_e = float(row["Vbg_stop"])
        vtg_s = float(row["Vtg_start"])
        vtg_e = float(row["Vtg_stop"])
    except Exception:
        vbg_s = vbg_e = vtg_s = vtg_e = 0.0

    point_count = _sweep_point_count(row)
    vbg_points = np.linspace(vbg_s, vbg_e, point_count, dtype=float).tolist()
    vtg_points = np.linspace(vtg_s, vtg_e, point_count, dtype=float).tolist()

    vbias_s = _valid_bias_value(row.get("Vbias_start"))
    vbias_e = _valid_bias_value(row.get("Vbias_stop"))
    vbias_points: Optional[List[float]] = None
    if vbias_s is not None or vbias_e is not None:
        vb0 = vbias_s if vbias_s is not None else vbias_e
        vb1 = vbias_e if vbias_e is not None else vbias_s
        vbias_s = float(vb0) if vb0 is not None else None
        vbias_e = float(vb1) if vb1 is not None else None
        if vbias_s is not None and vbias_e is not None:
            vbias_points = np.linspace(vbias_s, vbias_e, point_count, dtype=float).tolist()

    if not vbg_points or not vtg_points or len(vbg_points) != len(vtg_points):
        raise ValueError("Invalid gate sweep vectors.")
    if vbias_points is not None and len(vbias_points) != len(vbg_points):
        raise ValueError("Invalid bias sweep vector.")

    def _step_size(start: Optional[float], stop: Optional[float], points: int) -> Optional[float]:
        if start is None or stop is None:
            return None
        return abs(float(stop) - float(start)) / max(int(points) - 1, 1)

    return {
        "frames": point_count,
        "point_count": point_count,
        "vbg_start": vbg_s,
        "vbg_stop": vbg_e,
        "vtg_start": vtg_s,
        "vtg_stop": vtg_e,
        "vbias_start": vbias_s,
        "vbias_stop": vbias_e,
        "vbg_step": _step_size(vbg_s, vbg_e, point_count),
        "vtg_step": _step_size(vtg_s, vtg_e, point_count),
        "vbias_step": _step_size(vbias_s, vbias_e, point_count) if vbias_s is not None and vbias_e is not None else None,
        "vbg_points": vbg_points,
        "vtg_points": vtg_points,
        "vbias_points": vbias_points,
    }


def _validate_safe_jumps(final_sequence: Sequence[Dict[str, Any]], batch_df: pd.DataFrame, safe_jump_v: float) -> List[str]:
    issues: List[str] = []
    limit = float(safe_jump_v)
    for seq_i, ctx in enumerate(final_sequence, start=1):
        outer = _outer_ctx(ctx)
        for row_i, row in batch_df.iterrows():
            row_dict = row.to_dict()
            if not _when_ok(row_dict.get("When", ""), outer):
                continue
            label = build_condition_display_label(
                row_dict.get("condition_label", ""),
                row_dict.get("Vbias_start"),
                row_dict.get("Vbias_stop"),
            ) or clean_condition_label(row_dict.get("condition_label", "")) or f"row {row_i + 1}"
            sweep = _resolve_sweep_vectors(row_dict)
            frames = int(sweep["frames"])
            for channel, start_key, stop_key, step_key in (
                ("Vtg", "vtg_start", "vtg_stop", "vtg_step"),
                ("Vbg", "vbg_start", "vbg_stop", "vbg_step"),
                ("Vbias", "vbias_start", "vbias_stop", "vbias_step"),
            ):
                start = sweep[start_key]
                stop = sweep[stop_key]
                step = sweep[step_key]
                if start is None or stop is None or step is None:
                    continue
                if step > limit + 1e-12:
                    issues.append(
                        f"Unsafe {channel} sweep in Seq {seq_i}, {label}: "
                        f"start={float(start):g} V, stop={float(stop):g} V, frames={frames} "
                        f"-> step size={float(step):g} V, which exceeds the safe jump limit of {limit:g} V. "
                        f"Increase frames or reduce the sweep range."
                    )
    return issues


def _safe_float(value) -> Optional[float]:
    try:
        x = float(value)
    except Exception:
        return None
    return x if np.isfinite(x) else None


def _valid_bias_value(value) -> Optional[float]:
    x = _safe_float(value)
    return x if x is not None else None


_SMU_ROLE_ORDER = ("Vbg", "Vtg", "Vbias")


def _required_smu_roles(
    final_sequence: Sequence[Dict[str, Any]],
    batch_df: pd.DataFrame,
) -> Tuple[str, ...]:
    """Return the Keithley roles used by at least one applicable batch row."""
    required = set()
    for ctx in final_sequence:
        outer = _outer_ctx(ctx)
        for _, row in batch_df.iterrows():
            if not _when_ok(row.get("When", ""), outer):
                continue
            required.update(("Vbg", "Vtg"))
            if (
                _valid_bias_value(row.get("Vbias_start")) is not None
                or _valid_bias_value(row.get("Vbias_stop")) is not None
            ):
                required.add("Vbias")
    return tuple(role for role in _SMU_ROLE_ORDER if role in required)


def _smu_readiness_issues(smu_ctrl, required_roles: Sequence[str]) -> List[str]:
    """Validate that every requested Keithley role is connected and healthy."""
    roles = tuple(role for role in _SMU_ROLE_ORDER if role in set(required_roles))
    if not roles:
        return []
    role_text = ", ".join(roles)
    if smu_ctrl is None or not bool(getattr(smu_ctrl, "is_connected", False)):
        return [f"Required Keithley channels are not connected: {role_text}."]

    device = getattr(smu_ctrl, "device", None)
    if device is None:
        return [f"Required Keithley channels are unavailable: {role_text}."]

    missing: List[str] = []
    for role in roles:
        try:
            availability_check = getattr(device, "role_is_available", None)
            if callable(availability_check):
                available = bool(availability_check(role))
            else:
                has_role = getattr(device, "has_role", None)
                available = bool(callable(has_role) and has_role(role))
        except Exception:
            available = False
        if not available:
            missing.append(role)
    if missing:
        return [f"Required Keithley channels are missing: {', '.join(missing)}."]

    health_states = getattr(device, "health_states", {})
    if not isinstance(health_states, dict):
        health_states = {}
    unhealthy = [
        f"{role}={health_states.get(role, 'unknown')}"
        for role in roles
        if health_states.get(role, "ready") != "ready"
    ]
    if unhealthy:
        return [
            "Reconnect the Keithley SMUs before running "
            f"({', '.join(unhealthy)})."
        ]
    if bool(getattr(device, "requires_reconnect", False)):
        return ["Reconnect the Keithley SMUs after the hardware fault."]
    return []


def _format_current_readback(
    ibg: Optional[float],
    itg: Optional[float],
    ibias: Optional[float],
    *,
    include_bias: bool,
) -> str:
    def _fmt(value: Optional[float]) -> str:
        current = _safe_float(value)
        return f"{current:.4e} A" if current is not None else "unavailable"

    parts = [f"Ibg={_fmt(ibg)}", f"Itg={_fmt(itg)}"]
    if include_bias:
        parts.append(f"Ibias={_fmt(ibias)}")
    return "Keithley current readback: " + ", ".join(parts)


def _solve_condition_line(
    op: str,
    ratio: float,
    constant: float,
    vtg_min: float,
    vtg_max: float,
    vbg_min: float,
    vbg_max: float,
    doping_min: float = -float("inf"),
    doping_max: float = float("inf"),
    efield_min: float = -float("inf"),
    efield_max: float = float("inf"),
) -> Optional[Tuple[float, float, float, float]]:
    """Return the longest condition-line segment inside all supplied limits.

    The line is parameterized by ``t = Vbg`` and uses the same physical
    coordinate definition as Mega Sweep:

        D = Vtg + r*Vbg
        F = Vtg - r*Vbg
    """
    r = float(ratio)
    eff = r if op == "−" else -r
    C = float(constant)
    if (
        vtg_min >= vtg_max
        or vbg_min >= vbg_max
        or doping_min >= doping_max
        or efield_min >= efield_max
    ):
        return None

    t_lo = float(vbg_min)
    t_hi = float(vbg_max)

    def _intersect_linear(
        lo: float,
        hi: float,
        intercept: float,
        slope: float,
        value_min: float,
        value_max: float,
    ) -> Optional[Tuple[float, float]]:
        if abs(slope) < 1e-12:
            if value_min <= intercept <= value_max:
                return lo, hi
            return None
        bound_a = (value_min - intercept) / slope
        bound_b = (value_max - intercept) / slope
        return max(lo, min(bound_a, bound_b)), min(hi, max(bound_a, bound_b))

    # Vtg = C + eff*t; D = C + (eff+r)*t; F = C + (eff-r)*t.
    for intercept, slope, value_min, value_max in (
        (C, eff, float(vtg_min), float(vtg_max)),
        (C, eff + r, float(doping_min), float(doping_max)),
        (C, eff - r, float(efield_min), float(efield_max)),
    ):
        clipped = _intersect_linear(
            t_lo, t_hi, intercept, slope, value_min, value_max
        )
        if clipped is None:
            return None
        t_lo, t_hi = clipped
        if t_lo >= t_hi - 1e-12:
            return None

    return t_lo, t_hi, C + eff * t_lo, C + eff * t_hi


def _compute_frames_from_step(vbg_start: float, vbg_stop: float, vbg_step: float) -> int:
    vbg_range = abs(float(vbg_stop) - float(vbg_start))
    return max(2, int(round(vbg_range / max(float(vbg_step), 1e-12))) + 1)


def _parse_sweep_constants(text: str, *, max_values: int = 500) -> List[float]:
    """Parse one constant, a comma list, or inclusive start:step:stop ranges."""
    expression = str(text).strip()
    if not expression:
        raise ValueError("Enter a constant or an array of constants.")
    if expression.startswith("[") or expression.endswith("]"):
        if not (expression.startswith("[") and expression.endswith("]")):
            raise ValueError("Array brackets must include both '[' and ']'.")
        expression = expression[1:-1].strip()
    if not expression:
        raise ValueError("The constant array is empty.")

    values: List[float] = []
    for raw_token in expression.split(","):
        token = raw_token.strip()
        if not token:
            raise ValueError("Remove the empty item between commas.")
        if ":" not in token:
            try:
                value = float(token)
            except ValueError as exc:
                raise ValueError(f"'{token}' is not a number.") from exc
            if not np.isfinite(value):
                raise ValueError(f"'{token}' must be a finite number.")
            values.append(value)
        else:
            parts = [part.strip() for part in token.split(":")]
            if len(parts) != 3 or any(not part for part in parts):
                raise ValueError(
                    f"'{token}' must use the range form start:step:stop."
                )
            try:
                start, step, stop = (float(part) for part in parts)
            except ValueError as exc:
                raise ValueError(f"'{token}' contains a non-numeric range value.") from exc
            if not all(np.isfinite(value) for value in (start, step, stop)):
                raise ValueError(f"'{token}' must contain only finite numbers.")
            if abs(step) < 1e-15:
                raise ValueError(f"'{token}' has a zero step.")
            if start != stop and (stop - start) * step < 0:
                raise ValueError(
                    f"'{token}' steps away from its stop value; reverse the step sign."
                )
            span = (stop - start) / step
            count = 1 if start == stop else int(np.floor(span + 1e-12)) + 1
            if len(values) + count > max_values:
                raise ValueError(f"At most {max_values} constants can be calculated at once.")
            values.extend(start + index * step for index in range(count))
        if len(values) > max_values:
            raise ValueError(f"At most {max_values} constants can be calculated at once.")

    for value in values:
        if value < -200.0 or value > 200.0:
            raise ValueError("Constants must be between -200 and 200 V.")
    return values


def _format_condition_label(op: str, ratio: float, constant: float) -> str:
    def _fmt(x: float) -> str:
        if abs(x - round(x)) < 1e-12:
            return str(int(round(x)))
        return f"{x:.4g}".rstrip("0").rstrip(".")
    return f"TG{op}{_fmt(float(ratio))}BG={_fmt(float(constant))}"


class _RunFlowError(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.message = message


class _StopRequested(Exception):
    """Internal cooperative-stop signal used to unwind into safe cleanup."""


def _find_smu_communication_error(exc: BaseException):
    from app.devices.iv_adapter import SMUCommunicationError

    current: Optional[BaseException] = exc
    seen = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, SMUCommunicationError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def _enabled_filename_parts() -> List[str]:
    parts = list(cfg.filename.enabled_parts or [])
    return parts or ["device_id", "temp_mode", "center", "exposure", "condition"]


_LOOP_PARAM_FILENAME_PARTS: Dict[str, str] = {
    "Center Wavelength (nm)": "center",
    "Exposure Time (ms)": "exposure",
    "Accumulations (EPF)": "exposure",
    "Rotation1 Angle (deg)": "rotation1",
    "Rotation2 Angle (deg)": "rotation2",
    "Stage Position": "stage_position",
}


def _effective_filename_parts(selected_parts: Sequence[str], ctx: Dict[str, Any]) -> List[str]:
    selected = set(selected_parts or [])
    return [key for key, _label in PART_SPECS if key in selected]


def _first_applicable_seq_ctx(seq: Sequence[Dict[str, Any]], row: Dict[str, Any]) -> Dict[str, Any]:
    when_expr = row.get("When", "")
    for seq_ctx in seq:
        if _when_ok(when_expr, _outer_ctx(seq_ctx)):
            return seq_ctx
    return dict(seq[0]) if seq else {}


def _filename_context_from_row(
    meta: Dict[str, Any],
    ctx: Dict[str, Any],
    row: Dict[str, Any],
    *,
    measured_power_uw: Optional[float] = None,
) -> FilenameContext:
    cond_label = build_condition_display_label(
        row.get("condition_label", ""),
        row.get("Vbias_start"),
        row.get("Vbias_stop"),
    )
    return FilenameContext(
        device_id=meta.get("device_id", ""),
        tag=meta.get("tag", ""),
        temperature=meta.get("temperature", ""),
        mode=meta.get("measurement_mode", ""),
        laser_nm=meta.get("laser_nm", ""),
        nominal_power_uw=meta.get("power_uw"),
        center_nm=ctx.get("Center Wavelength (nm)", cfg.lf6.center_nm),
        exposure_ms=ctx.get("Exposure Time (ms)", cfg.lf6.exposure_ms),
        accumulations=ctx.get("Accumulations (EPF)", cfg.lf6.accumulations),
        rotation1_deg=ctx.get("Rotation1 Angle (deg)"),
        rotation2_deg=ctx.get("Rotation2 Angle (deg)"),
        stage_position=ctx.get("Stage Position"),
        condition_label=cond_label,
        point=meta.get("point", ""),
        measure_power=_to_bool(row.get("MeasurePower", False)),
        measured_power_uw=measured_power_uw,
        power_coefficient=float(meta.get("power_coefficient", 1.0) or 1.0),
    )


def _build_run_filename_base(
    meta: Dict[str, Any],
    ctx: Dict[str, Any],
    row: Dict[str, Any],
    *,
    measured_power_uw: Optional[float] = None,
    enabled_parts: Optional[List[str]] = None,
) -> Tuple[str, FilenameContext, List[Tuple[str, str]]]:
    fname_ctx = _filename_context_from_row(
        meta,
        ctx,
        row,
        measured_power_uw=measured_power_uw,
    )
    parts = _effective_filename_parts(enabled_parts or _enabled_filename_parts(), ctx)
    tokens = build_filename_tokens(fname_ctx, parts)
    base = build_base_filename(fname_ctx, parts)
    return base, fname_ctx, tokens


# ── Cell-widget helpers for the loop table ─────────────────────────────────────

def _make_check_cell(checked: bool) -> QWidget:
    """Return a centred-checkbox widget for use in table cells."""
    container = QWidget()
    lay = QHBoxLayout(container)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
    cb = QCheckBox()
    cb.setChecked(checked)
    lay.addWidget(cb)
    return container


def _cell_checked(widget: QWidget) -> bool:
    if widget is None:
        return False
    cb = widget.findChild(QCheckBox)
    return cb.isChecked() if cb else False


def _make_param_combo(current: str) -> QComboBox:
    combo = QComboBox()
    combo.addItems(LOOP_PARAMS)
    idx = combo.findText(current)
    combo.setCurrentIndex(max(0, idx))
    return combo


# ── When-column delegate ──────────────────────────────────────────────────────

def _param_to_expr_name(param: str) -> tuple[str, str]:
    """
    Return the (full_name, short_name) as they appear in the _when_ok namespace.

    _when_ok does:  re.sub(r"[^a-zA-Z0-9_]", "_", k)  on every ctx key.
    _outer_ctx adds: k.split("(")[0].strip().replace(" ", "_")  as a shorthand.
    """
    full  = re.sub(r"[^a-zA-Z0-9_]", "_", param)
    short = re.sub(r"[^a-zA-Z0-9_]", "_", param.split("(")[0].strip())
    return full, short


class _WhenDelegate(QStyledItemDelegate):
    """
    Cell editor for the 'When' column.

    Opens a QLineEdit with a QCompleter pre-populated from the loop table's
    current parameter names (both the full sanitised form and the short form
    used by _outer_ctx / _when_ok).  The user can still type any expression
    freely; the completer just shows valid name fragments.
    """

    def __init__(self, loop_table: QTableWidget, parent=None):
        super().__init__(parent)
        self._loop_table = loop_table

    def _completions(self) -> list[str]:
        seen: list[str] = []
        for r in range(self._loop_table.rowCount()):
            combo = self._loop_table.cellWidget(r, 1)
            if combo is None:
                continue
            full, short = _param_to_expr_name(combo.currentText())
            for name in (short, full):   # short first — easier to type
                if name and name not in seen:
                    seen.append(name)
        return seen

    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        completions = self._completions()
        if completions:
            completer = QCompleter(completions, editor)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            editor.setCompleter(completer)
        tip_names = "\n  ".join(completions) if completions else "(no loop parameters enabled)"
        editor.setToolTip(
            "Safe condition — row runs only when this evaluates to True.\n"
            "Leave blank to run unconditionally.\n\n"
            "Available parameter names:\n"
            f"  {tip_names}\n\n"
            "Examples:\n"
            "  Center_Wavelength__nm_ == 860\n"
            "  Rotation1_Angle_ > 45\n"
            "  Stage_Position != 0\n\n"
            "Use == to compare values. Invalid conditions block Apply and Run."
        )
        return editor

    def setEditorData(self, editor, index):
        editor.setText(index.data() or "")

    def setModelData(self, editor, model, index):
        model.setData(index, editor.text())


class _IntSpinDelegate(QStyledItemDelegate):
    def __init__(self, minimum: int = 1, maximum: int = 100000, parent=None):
        super().__init__(parent)
        self._minimum = int(minimum)
        self._maximum = int(maximum)

    def createEditor(self, parent, option, index):
        editor = QSpinBox(parent)
        editor.setRange(self._minimum, self._maximum)
        editor.setFrame(False)
        editor.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        editor.setAlignment(Qt.AlignmentFlag.AlignCenter)
        editor.setKeyboardTracking(False)
        return editor

    def setEditorData(self, editor, index):
        try:
            value = int(float(index.data() or self._minimum))
        except Exception:
            value = self._minimum
        editor.setValue(max(self._minimum, min(self._maximum, value)))
        editor.lineEdit().selectAll()

    def setModelData(self, editor, model, index):
        model.setData(index, str(editor.value()))


class _OptionalFloatDelegate(QStyledItemDelegate):
    def __init__(self, minimum: float = -1e6, maximum: float = 1e6, decimals: int = 4, parent=None):
        super().__init__(parent)
        self._minimum = float(minimum)
        self._maximum = float(maximum)
        self._decimals = int(decimals)

    def createEditor(self, parent, option, index):
        editor = QDoubleSpinBox(parent)
        editor.setRange(self._minimum, self._maximum)
        editor.setDecimals(self._decimals)
        editor.setSingleStep(0.1)
        editor.setFrame(False)
        editor.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        editor.setAlignment(Qt.AlignmentFlag.AlignCenter)
        editor.setKeyboardTracking(False)
        editor.setSpecialValueText("")
        return editor

    def setEditorData(self, editor, index):
        text = str(index.data() or "").strip()
        if text == "":
            editor.lineEdit().selectAll()
            return
        try:
            editor.setValue(float(text))
        except Exception:
            editor.lineEdit().selectAll()
            return
        editor.lineEdit().selectAll()

    def setModelData(self, editor, model, index):
        text = editor.text().strip()
        if text == "":
            model.setData(index, "")
            return
        model.setData(index, f"{editor.value():g}")


class _SafeDoubleSpinBox(QDoubleSpinBox):
    """Calculator spin box without arrow buttons or accidental wheel changes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.setKeyboardTracking(False)

    def wheelEvent(self, event):
        event.ignore()


class _SafeSpinBox(QSpinBox):
    """Integer counterpart used for the calculator frame count."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.setKeyboardTracking(False)

    def wheelEvent(self, event):
        event.ignore()


class _SweepLineCalculator(QGroupBox):
    add_rows_requested = Signal(list)
    expanded_changed = Signal(bool)

    def __init__(self, smu_ctrl=None, safe_jump_spin=None, parent=None):
        super().__init__(parent)
        self._smu = smu_ctrl
        self._safe_jump_spin = safe_jump_spin
        self._label_auto = True
        self._body_visible = False
        self._calculated_rows: List[dict] = []
        self._calc_timer = QTimer(self)
        self._calc_timer.setSingleShot(True)
        self._calc_timer.setInterval(50)
        self._calc_timer.timeout.connect(self._recalculate)
        self._build()
        self._wire()
        self._recalculate()

    def _build(self):
        self.setObjectName("SweepLineCalculator")
        self.setStyleSheet(
            "QGroupBox#SweepLineCalculator {"
            "  background: #f6f8fb; border: 1px solid #d8dee8;"
            "  border-radius: 9px; }"
            "QFrame#SweepCalcCard {"
            "  background: #ffffff; border: 1px solid #e1e6ee;"
            "  border-radius: 7px; }"
            "QLabel#SweepCalcSectionTitle {"
            "  color: #26364a; font-weight: 600; font-size: 12px; border: none; }"
            "QDoubleSpinBox, QSpinBox, QComboBox, QLineEdit {"
            "  min-height: 24px; background: #ffffff; color: #202936;"
            "  border: 1px solid #cbd3df; border-radius: 5px; padding: 0 5px; }"
            "QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus, QLineEdit:focus {"
            "  border: 1px solid #2878d0; }"
            "QPushButton#SweepCalcPrimary {"
            "  min-height: 30px; color: white; background: #1769c2;"
            "  border: 1px solid #1769c2; border-radius: 6px;"
            "  padding: 2px 14px; font-weight: 600; }"
            "QPushButton#SweepCalcPrimary:hover { background: #0f5bab; }"
            "QPushButton#SweepCalcPrimary:pressed { background: #0b4d93; }"
            "QPushButton#SweepCalcPrimary:disabled {"
            "  color: #8994a3; background: #e8ecf1; border-color: #d6dce5; }"
            "QToolButton { border: none; border-radius: 5px; background: #e5edf7; }"
            "QToolButton:hover { background: #d7e5f5; }"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 7, 8, 8)
        root.setSpacing(6)

        hdr = QHBoxLayout()
        self._toggle = QToolButton()
        self._toggle.setCheckable(True)
        self._toggle.setChecked(False)
        self._toggle.setArrowType(Qt.RightArrow)
        self._toggle.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self._toggle.setFixedSize(24, 22)
        hdr.addWidget(self._toggle)
        title = QLabel("Sweep Line Calculator")
        title.setStyleSheet("font-weight: 600;")
        hdr.addWidget(title)
        hdr.addStretch()
        root.addLayout(hdr)

        self._body = QWidget()
        self._body.setVisible(False)
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        columns = QGridLayout()
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setHorizontalSpacing(8)
        columns.setVerticalSpacing(0)
        body.addLayout(columns)

        left_card = QFrame()
        left_card.setObjectName("SweepCalcCard")
        left_card.setMinimumWidth(500)
        left_layout = QVBoxLayout(left_card)
        left_layout.setContentsMargins(10, 8, 10, 9)
        left_layout.setSpacing(5)
        left_title = QLabel("Sweep definition")
        left_title.setObjectName("SweepCalcSectionTitle")
        left_layout.addWidget(left_title)

        right_card = QFrame()
        right_card.setObjectName("SweepCalcCard")
        right_card.setMinimumWidth(330)
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(10, 8, 10, 9)
        right_layout.setSpacing(5)

        columns.addWidget(left_card, 0, 0)
        columns.addWidget(right_card, 0, 1)
        columns.setColumnStretch(0, 3)
        columns.setColumnStretch(1, 2)

        eq = QGridLayout()
        eq.setContentsMargins(0, 0, 0, 0)
        eq.setHorizontalSpacing(5)
        eq.setVerticalSpacing(1)
        cond_lbl = QLabel("Condition:")
        cond_lbl.setFixedWidth(64)
        tg_lbl = QLabel("TG")
        tg_lbl.setStyleSheet("font-weight: 600;")
        self._op_combo = QComboBox()
        self._op_combo.addItems(["−", "+"])
        self._op_combo.setMinimumWidth(48)
        self._op_combo.setMaximumWidth(58)
        self._ratio_spin = _SafeDoubleSpinBox()
        self._ratio_spin.setRange(0.0, 100.0)
        self._ratio_spin.setDecimals(4)
        self._ratio_spin.setValue(0.9)
        self._ratio_spin.setMinimumWidth(96)
        self._ratio_spin.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        bg_lbl = QLabel("× BG =")
        bg_lbl.setStyleSheet("font-weight: 600;")
        self._constant_edit = QLineEdit("0")
        self._constant_edit.setPlaceholderText("0  or  [-4, 0, 4]  or  -4:2:4")
        self._constant_edit.setMinimumWidth(150)
        self._constant_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._constant_edit.setToolTip(
            "Enter one or more condition constants:\n"
            "  Single value:  4\n"
            "  Array:  [-4, -2, 0, 2, 4]\n"
            "  Comma list:  -4, -2, 0, 2, 4\n"
            "  Range:  -4:2:4  (start:step:stop)\n"
            "Ranges include the stop when the step lands on it. Lists and ranges "
            "can be mixed, for example: -4:2:0, 3, 5."
        )
        eq.addWidget(cond_lbl, 0, 0)
        eq.addWidget(tg_lbl, 0, 1)
        eq.addWidget(self._op_combo, 0, 2)
        eq.addWidget(self._ratio_spin, 0, 3)
        eq.addWidget(bg_lbl, 0, 4)
        eq.addWidget(self._constant_edit, 0, 5)
        eq.setColumnStretch(3, 1)
        eq.setColumnStretch(5, 1)

        hint = QLabel("D = TG + r·BG     F = TG − r·BG")
        hint.setToolTip(
            "Doping D = TG + r·BG\n"
            "E-field F = TG − r·BG\n"
            "The condition row holds either D or F constant."
        )
        hint.setStyleSheet("color: #777777; font-size: 10px;")
        eq.addWidget(hint, 1, 1, 1, 5)
        left_layout.addLayout(eq)

        step_row = QHBoxLayout()
        step_row.setSpacing(5)
        step_lbl = QLabel("Vbg step:")
        step_lbl.setFixedWidth(78)
        self._vbg_step_spin = _SafeDoubleSpinBox()
        self._vbg_step_spin.setRange(0.001, 10.0)
        self._vbg_step_spin.setDecimals(4)
        self._vbg_step_spin.setValue(0.1)
        self._vbg_step_spin.setMinimumWidth(104)
        self._vbg_step_spin.setMaximumWidth(150)
        self._vbg_step_spin.setSuffix(" V")
        self._vbg_step_spin.setToolTip("Spacing between consecutive Vbg values. Sets the frames count. The Vtg step is derived: Vtg step = ratio × Vbg step.")
        step_row.addWidget(step_lbl)
        step_row.addWidget(self._vbg_step_spin)
        step_row.addStretch()
        left_layout.addLayout(step_row)

        limits_grid = QGridLayout()
        limits_grid.setContentsMargins(0, 0, 0, 0)
        limits_grid.setHorizontalSpacing(5)
        limits_grid.setVerticalSpacing(2)
        voltage_title = QLabel("Voltage limits  (min → max)")
        physical_title = QLabel("Physical limits  (min → max)")
        for label in (voltage_title, physical_title):
            label.setStyleSheet("color: #666666; font-size: 10px; font-weight: 600;")
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        limits_grid.addWidget(voltage_title, 0, 0, 1, 3)
        limits_grid.addWidget(physical_title, 0, 4, 1, 3)

        def _limit_spin(value: float, tooltip: str):
            spin = _SafeDoubleSpinBox()
            spin.setRange(-200.0, 200.0)
            spin.setDecimals(2)
            spin.setValue(value)
            spin.setMinimumWidth(98)
            spin.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )
            spin.setSuffix(" V")
            spin.setToolTip(tooltip)
            return spin

        self._vtg_min_spin = _limit_spin(-10.0, "Minimum Vtg")
        self._vtg_max_spin = _limit_spin(10.0, "Maximum Vtg")
        self._vbg_min_spin = _limit_spin(-10.0, "Minimum Vbg")
        self._vbg_max_spin = _limit_spin(10.0, "Maximum Vbg")
        self._doping_min_spin = _limit_spin(-20.0, "Minimum doping D")
        self._doping_max_spin = _limit_spin(20.0, "Maximum doping D")
        self._efield_min_spin = _limit_spin(-20.0, "Minimum E-field F")
        self._efield_max_spin = _limit_spin(20.0, "Maximum E-field F")

        for row, left_name, left_min, left_max, right_name, right_min, right_max in (
            (1, "Vtg", self._vtg_min_spin, self._vtg_max_spin,
             "D", self._doping_min_spin, self._doping_max_spin),
            (2, "Vbg", self._vbg_min_spin, self._vbg_max_spin,
             "F", self._efield_min_spin, self._efield_max_spin),
        ):
            limits_grid.addWidget(QLabel(left_name), row, 0)
            limits_grid.addWidget(left_min, row, 1)
            limits_grid.addWidget(left_max, row, 2)
            limits_grid.addWidget(QLabel(right_name), row, 4)
            limits_grid.addWidget(right_min, row, 5)
            limits_grid.addWidget(right_max, row, 6)
        limits_grid.setColumnMinimumWidth(3, 8)
        limits_grid.setColumnStretch(7, 1)
        left_layout.addLayout(limits_grid)

        vb_row = QHBoxLayout()
        vb_row.setSpacing(5)
        vb_lbl = QLabel("Fixed Vbias:")
        vb_lbl.setFixedWidth(78)
        self._vbias_spin = _SafeDoubleSpinBox()
        self._vbias_spin.setRange(-200.0, 200.0)
        self._vbias_spin.setDecimals(4)
        self._vbias_spin.setValue(0.0)
        self._vbias_spin.setMinimumWidth(104)
        self._vbias_spin.setMaximumWidth(150)
        self._vbias_spin.setSuffix(" V")
        self._include_vbias_chk = QCheckBox("Include")
        self._include_vbias_chk.setToolTip("Include this fixed source-drain bias in the batch row.")
        self._vbias_badge = QLabel("Vbias unavailable")
        self._vbias_badge.setToolTip("No usable Vbias Keithley channel is connected.")
        self._vbias_badge.setStyleSheet("color: #b86300; font-size: 10px;")
        vb_row.addWidget(vb_lbl)
        vb_row.addWidget(self._vbias_spin)
        vb_row.addWidget(self._include_vbias_chk)
        vb_row.addWidget(self._vbias_badge)
        vb_row.addStretch()
        left_layout.addLayout(vb_row)
        left_layout.addStretch(1)

        calc_hdr = QLabel("Calculated sweep")
        calc_hdr.setObjectName("SweepCalcSectionTitle")
        right_layout.addWidget(calc_hdr)

        def _res_spin():
            s = _SafeDoubleSpinBox()
            s.setRange(-200.0, 200.0)
            s.setDecimals(4)
            s.setReadOnly(True)
            s.setToolTip(
                "Calculated endpoint. Change the condition or limits to update this value."
            )
            s.setMinimumWidth(118)
            s.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )
            return s

        results_grid = QGridLayout()
        results_grid.setContentsMargins(0, 0, 0, 0)
        results_grid.setHorizontalSpacing(8)
        results_grid.setVerticalSpacing(2)
        start_title = QLabel("Start")
        stop_title = QLabel("Stop")
        for label in (start_title, stop_title):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet("color: #777777; font-size: 10px;")
        results_grid.addWidget(start_title, 0, 1)
        results_grid.addWidget(stop_title, 0, 2)

        results_grid.addWidget(QLabel("Vtg"), 1, 0)
        self._vtg_start_spin = _res_spin()
        results_grid.addWidget(self._vtg_start_spin, 1, 1)
        self._vtg_stop_spin = _res_spin()
        results_grid.addWidget(self._vtg_stop_spin, 1, 2)

        results_grid.addWidget(QLabel("Vbg"), 2, 0)
        self._vbg_start_spin = _res_spin()
        results_grid.addWidget(self._vbg_start_spin, 2, 1)
        self._vbg_stop_spin = _res_spin()
        results_grid.addWidget(self._vbg_stop_spin, 2, 2)
        results_grid.setColumnStretch(3, 1)
        results_grid.setColumnStretch(1, 1)
        results_grid.setColumnStretch(2, 1)
        right_layout.addLayout(results_grid)

        self._derived_range_lbl = QLabel("D  —    F  —")
        self._derived_range_lbl.setToolTip("Derived doping D and E-field F at the start and stop points.")
        self._derived_range_lbl.setStyleSheet("color: #666666; font-size: 10px;")
        self._derived_range_lbl.setWordWrap(True)
        self._derived_range_lbl.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        right_layout.addWidget(self._derived_range_lbl)

        self._multi_preview = QTableWidget(0, 4)
        self._multi_preview.setHorizontalHeaderLabels(
            ["Condition", "Vtg start → stop", "Vbg start → stop", "Frames"]
        )
        self._multi_preview.verticalHeader().setVisible(False)
        self._multi_preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._multi_preview.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._multi_preview.setAlternatingRowColors(True)
        self._multi_preview.setMaximumHeight(150)
        self._multi_preview.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        preview_header = self._multi_preview.horizontalHeader()
        for column in range(3):
            preview_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch
            )
        preview_header.setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        preview_header.setMinimumSectionSize(42)
        for column, tooltip in enumerate(
            (
                "Generated condition label.",
                "Top-gate voltage from sweep start to stop.",
                "Back-gate voltage from sweep start to stop.",
                "Number of sweep points.",
            )
        ):
            header_item = self._multi_preview.horizontalHeaderItem(column)
            if header_item is not None:
                header_item.setToolTip(tooltip)
        self._multi_preview.setToolTip(
            "Preview of every batch row generated from the constant array."
        )
        self._multi_preview.hide()
        right_layout.addWidget(self._multi_preview)

        row_c = QHBoxLayout()
        row_c.setSpacing(6)
        row_c.addWidget(QLabel("Frames"))
        self._frames_spin = _SafeSpinBox()
        self._frames_spin.setRange(2, 1_000_000)
        self._frames_spin.setFixedWidth(72)
        row_c.addWidget(self._frames_spin)
        self._vtg_step_lbl = QLabel("ΔVtg 0.0000 V")
        self._vbg_step_lbl = QLabel("ΔVbg 0.0000 V")
        self._vtg_step_lbl.setStyleSheet("color: #555555; font-size: 10px;")
        self._vbg_step_lbl.setStyleSheet("color: #555555; font-size: 10px;")
        row_c.addWidget(self._vtg_step_lbl)
        row_c.addWidget(self._vbg_step_lbl)
        row_c.addStretch()
        right_layout.addLayout(row_c)

        row_d = QHBoxLayout()
        row_d.setSpacing(6)
        row_d.addWidget(QLabel("Condition label"))
        self._condition_edit = QLineEdit()
        row_d.addWidget(self._condition_edit, stretch=1)
        right_layout.addLayout(row_d)

        self._status_lbl = QLabel("")
        self._status_lbl.setWordWrap(True)
        right_layout.addWidget(self._status_lbl)
        right_layout.addStretch(1)

        self._add_btn = QPushButton("Add to Batch Table  ▸")
        self._add_btn.setObjectName("SweepCalcPrimary")
        right_layout.addWidget(self._add_btn)

        root.addWidget(self._body)

    def _wire(self):
        self._toggle.toggled.connect(self._on_toggle)
        for w in (
            self._op_combo,
            self._ratio_spin,
            self._vbg_step_spin,
            self._vtg_min_spin,
            self._vtg_max_spin,
            self._vbg_min_spin,
            self._vbg_max_spin,
            self._doping_min_spin,
            self._doping_max_spin,
            self._efield_min_spin,
            self._efield_max_spin,
        ):
            if hasattr(w, "valueChanged"):
                w.valueChanged.connect(self._schedule_recalc)
            else:
                w.currentTextChanged.connect(self._on_equation_changed)
        self._op_combo.currentTextChanged.connect(self._on_equation_changed)
        self._ratio_spin.valueChanged.connect(self._on_equation_changed)
        self._constant_edit.textChanged.connect(self._on_equation_changed)
        self._condition_edit.textEdited.connect(self._on_label_edited)
        self._frames_spin.valueChanged.connect(self._update_result_step_labels)
        for w in (self._vtg_start_spin, self._vtg_stop_spin, self._vbg_start_spin, self._vbg_stop_spin):
            w.valueChanged.connect(self._on_result_edited)
        self._include_vbias_chk.toggled.connect(self._recalculate)
        self._vbias_spin.valueChanged.connect(self._recalculate)
        self._add_btn.clicked.connect(self._on_add_clicked)
        self.set_vbias_available(self._vbias_available())

    def _on_toggle(self, checked: bool):
        self._body.setVisible(checked)
        self._toggle.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
        self.updateGeometry()
        self.expanded_changed.emit(bool(checked))

    def _schedule_recalc(self, *_args):
        self._add_btn.setEnabled(False)
        self._calc_timer.start()

    def _on_equation_changed(self, *_args):
        self._label_auto = True
        self._schedule_recalc()

    def _on_label_edited(self, *_args):
        self._label_auto = False
        self._schedule_recalc()

    def _set_status(self, text: str = "", color: str = "#666666"):
        self._status_lbl.setText(text)
        self._status_lbl.setStyleSheet(f"color: {color};")
        self._status_lbl.setVisible(bool(text))

    def _update_result(self, result_tuple, frames: int):
        vbg_start, vbg_stop, vtg_start, vtg_stop = result_tuple
        for spin, value in (
            (self._vtg_start_spin, vtg_start),
            (self._vtg_stop_spin, vtg_stop),
            (self._vbg_start_spin, vbg_start),
            (self._vbg_stop_spin, vbg_stop),
        ):
            spin.blockSignals(True)
            spin.setValue(float(value))
            spin.blockSignals(False)
        if not self._frames_spin.hasFocus():
            self._frames_spin.blockSignals(True)
            self._frames_spin.setValue(max(2, int(frames)))
            self._frames_spin.blockSignals(False)
        self._update_result_step_labels()

    def _update_result_step_labels(self):
        frames = max(int(self._frames_spin.value()), 2)
        vtg_step = abs(self._vtg_stop_spin.value() - self._vtg_start_spin.value()) / max(frames - 1, 1)
        vbg_step = abs(self._vbg_stop_spin.value() - self._vbg_start_spin.value()) / max(frames - 1, 1)
        self._vtg_step_lbl.setText(f"ΔVtg {vtg_step:.4f} V")
        self._vbg_step_lbl.setText(f"ΔVbg {vbg_step:.4f} V")
        ratio = float(self._ratio_spin.value())
        d0 = self._vtg_start_spin.value() + ratio * self._vbg_start_spin.value()
        d1 = self._vtg_stop_spin.value() + ratio * self._vbg_stop_spin.value()
        f0 = self._vtg_start_spin.value() - ratio * self._vbg_start_spin.value()
        f1 = self._vtg_stop_spin.value() - ratio * self._vbg_stop_spin.value()
        self._derived_range_lbl.setText(
            f"D  {d0:.4f} → {d1:.4f} V     "
            f"F  {f0:.4f} → {f1:.4f} V"
        )

    def _calculated_row_error(self) -> Optional[str]:
        return self._endpoint_error(
            (
                self._vbg_start_spin.value(),
                self._vbg_stop_spin.value(),
                self._vtg_start_spin.value(),
                self._vtg_stop_spin.value(),
            )
        )

    def _endpoint_error(self, result_tuple) -> Optional[str]:
        ratio = float(self._ratio_spin.value())
        vtg_tolerance = 0.5 * (10.0 ** -self._vtg_start_spin.decimals()) + 1e-12
        vbg_tolerance = 0.5 * (10.0 ** -self._vbg_start_spin.decimals()) + 1e-12
        physical_tolerance = vtg_tolerance + abs(ratio) * vbg_tolerance
        vbg_start, vbg_stop, vtg_start, vtg_stop = result_tuple
        endpoints = (
            (vtg_start, vbg_start),
            (vtg_stop, vbg_stop),
        )
        limits = (
            ("Vtg", self._vtg_min_spin.value(), self._vtg_max_spin.value(), vtg_tolerance),
            ("Vbg", self._vbg_min_spin.value(), self._vbg_max_spin.value(), vbg_tolerance),
            (
                "Doping",
                self._doping_min_spin.value(),
                self._doping_max_spin.value(),
                physical_tolerance,
            ),
            (
                "E-field",
                self._efield_min_spin.value(),
                self._efield_max_spin.value(),
                physical_tolerance,
            ),
        )
        for index, (vtg, vbg) in enumerate(endpoints, start=1):
            values = (vtg, vbg, vtg + ratio * vbg, vtg - ratio * vbg)
            if not all(np.isfinite(value) for value in values):
                return "Calculated row contains a non-finite value."
            for value, (name, lower, upper, tolerance) in zip(values, limits):
                if value < lower - tolerance or value > upper + tolerance:
                    return (
                        f"Endpoint {index} has {name} = {value:.4f} V, outside "
                        f"the {lower:.4f} to {upper:.4f} V limits."
                    )
        return None

    def _on_result_edited(self, *_args):
        self._update_result_step_labels()
        error = self._calculated_row_error()
        if error:
            self._set_status(f"✗ {error}", "#b42318")
            self._add_btn.setEnabled(False)
        else:
            self._set_status("")
            self._add_btn.setEnabled(True)

    def set_vbias_available(self, available: bool):
        self._vbias_badge.setVisible(not available)
        self._recalculate()

    def _vbias_available(self) -> bool:
        if self._smu is None:
            return False
        if hasattr(self._smu, "has_vbias"):
            return bool(getattr(self._smu, "has_vbias"))
        return bool(getattr(self._smu, "is_connected", False))

    def _recalculate(self):
        self._calculated_rows = []
        self._multi_preview.hide()
        self._add_btn.setText("Add to Batch Table  ▸")
        op = self._op_combo.currentText()
        ratio = float(self._ratio_spin.value())
        try:
            constants = _parse_sweep_constants(self._constant_edit.text())
        except ValueError as exc:
            self._calculated_rows = []
            self._multi_preview.hide()
            self._set_status(f"✗ {exc}", "#b42318")
            self._add_btn.setText("Add to Batch Table")
            self._add_btn.setEnabled(False)
            return
        constant = constants[0]
        vbg_step = float(self._vbg_step_spin.value())
        vtg_min = float(self._vtg_min_spin.value())
        vtg_max = float(self._vtg_max_spin.value())
        vbg_min = float(self._vbg_min_spin.value())
        vbg_max = float(self._vbg_max_spin.value())
        doping_min = float(self._doping_min_spin.value())
        doping_max = float(self._doping_max_spin.value())
        efield_min = float(self._efield_min_spin.value())
        efield_max = float(self._efield_max_spin.value())

        if vtg_min >= vtg_max:
            self._set_status("✗ Vtg limit: min must be less than max.", "#b42318")
            self._add_btn.setEnabled(False)
            return
        if vbg_min >= vbg_max:
            self._set_status("✗ Vbg limit: min must be less than max.", "#b42318")
            self._add_btn.setEnabled(False)
            return
        if doping_min >= doping_max:
            self._set_status("✗ Doping limit: min must be less than max.", "#b42318")
            self._add_btn.setEnabled(False)
            return
        if efield_min >= efield_max:
            self._set_status("✗ E-field limit: min must be less than max.", "#b42318")
            self._add_btn.setEnabled(False)
            return

        if len(constants) > 1:
            self._recalculate_multiple(
                constants,
                op=op,
                ratio=ratio,
                vbg_step=vbg_step,
                vtg_min=vtg_min,
                vtg_max=vtg_max,
                vbg_min=vbg_min,
                vbg_max=vbg_max,
                doping_min=doping_min,
                doping_max=doping_max,
                efield_min=efield_min,
                efield_max=efield_max,
            )
            return

        result = _solve_condition_line(
            op,
            ratio,
            constant,
            vtg_min,
            vtg_max,
            vbg_min,
            vbg_max,
            doping_min,
            doping_max,
            efield_min,
            efield_max,
        )
        if result is None:
            if ratio < 1e-9 and not (vtg_min <= constant <= vtg_max):
                self._set_status(f"✗ Vtg = {constant:.4f} V is outside the Vtg limits.", "#b42318")
            else:
                self._set_status(
                    "✗ No valid segment satisfies the Vtg, Vbg, doping, and E-field limits.",
                    "#b42318",
                )
            self._add_btn.setEnabled(False)
            return

        frames = _compute_frames_from_step(result[0], result[1], vbg_step)
        if frames < 2:
            self._set_status("✗ Segment too short for one step: reduce Vbg step or widen limits.", "#b42318")
            self._add_btn.setEnabled(False)
            return

        self._update_result(result, frames)
        endpoint_error = self._calculated_row_error()
        if endpoint_error:
            self._set_status(f"✗ {endpoint_error}", "#b42318")
            self._add_btn.setEnabled(False)
            return
        if self._label_auto:
            self._condition_edit.blockSignals(True)
            self._condition_edit.setText(_format_condition_label(op, ratio, constant))
            self._condition_edit.blockSignals(False)

        frames_now = max(int(self._frames_spin.value()), 2)
        vtg_step = abs(self._vtg_stop_spin.value() - self._vtg_start_spin.value()) / max(frames_now - 1, 1)
        vbg_step_actual = abs(self._vbg_stop_spin.value() - self._vbg_start_spin.value()) / max(frames_now - 1, 1)
        safe_jump = float(self._safe_jump_spin.value()) if self._safe_jump_spin is not None else float("inf")
        msg = ""
        color = "#666666"
        if ratio < 1e-9:
            msg = f"ℹ Ratio ≈ 0: sweeping Vbg across limits at fixed Vtg = {constant:.4f} V."
        elif not np.isfinite(vtg_step) or not np.isfinite(vbg_step_actual):
            msg = "✗ Calculation produced invalid values — check inputs."
            color = "#b42318"
            self._add_btn.setEnabled(False)
            self._set_status(msg, color)
            return
        elif vtg_step > safe_jump + 1e-12:
            max_vbg = safe_jump / max(ratio, 1e-12)
            msg = f"⚠ Vtg step ≈ {vtg_step:.4f} V exceeds safe jump {safe_jump:.3f} V. Reduce Vbg step to ≤ {max_vbg:.4f} V."
            color = "#c26a00"
        elif vbg_step_actual > safe_jump + 1e-12:
            needed = int(np.ceil(abs(self._vbg_stop_spin.value() - self._vbg_start_spin.value()) / safe_jump)) + 1
            msg = f"⚠ Vbg step ≈ {vbg_step_actual:.4f} V exceeds safe jump {safe_jump:.3f} V. Increase frames to ≥ {needed}."
            color = "#c26a00"
        elif frames_now > 10000:
            msg = f"⚠ Large sweep: {frames_now} points."
            color = "#c26a00"
        elif abs(self._vbg_stop_spin.value() - self._vbg_start_spin.value()) < 2.0 * vbg_step:
            msg = f"⚠ Only {frames_now} point(s) will be sampled. Reduce step size."
            color = "#c26a00"
        elif self._include_vbias_chk.isChecked() and not self._vbias_available():
            msg = f"⚠ Vbias set to {self._vbias_spin.value():.4f} V, but its Keithley channel is unavailable."
            color = "#c26a00"
        self._set_status(msg, color)
        displayed_result = (
            self._vbg_start_spin.value(),
            self._vbg_stop_spin.value(),
            self._vtg_start_spin.value(),
            self._vtg_stop_spin.value(),
        )
        self._calculated_rows = [
            self._batch_row_for_result(constant, displayed_result, frames_now, 1)
        ]
        self._add_btn.setEnabled(True)

    def _batch_row_for_result(
        self,
        constant: float,
        result_tuple,
        frames: int,
        total: int,
    ) -> dict:
        vbg_start, vbg_stop, vtg_start, vtg_stop = result_tuple
        auto_label = _format_condition_label(
            self._op_combo.currentText(), self._ratio_spin.value(), constant
        )
        custom_label = self._condition_edit.text().strip()
        if self._label_auto or not custom_label:
            label = auto_label
        elif total == 1:
            label = custom_label
        else:
            constant_token = auto_label.rsplit("=", 1)[-1]
            label = f"{custom_label}_{constant_token}"
        return {
            "Run": True,
            "When": "",
            "MeasurePower": False,
            "condition_label": label,
            "repeat": 1,
            "frames": int(frames),
            "Vbg_start": float(vbg_start),
            "Vbg_stop": float(vbg_stop),
            "Vtg_start": float(vtg_start),
            "Vtg_stop": float(vtg_stop),
            "Vbias_start": float(self._vbias_spin.value()) if self._include_vbias_chk.isChecked() else "",
            "Vbias_stop": float(self._vbias_spin.value()) if self._include_vbias_chk.isChecked() else "",
        }

    def _show_multi_preview(self, entries: Sequence[Tuple[float, Optional[dict], str]]):
        table = self._multi_preview
        table.setRowCount(len(entries))
        for row_index, (constant, row, error) in enumerate(entries):
            if row is None:
                values = [
                    _format_condition_label(
                        self._op_combo.currentText(), self._ratio_spin.value(), constant
                    ),
                    "Invalid", "", "",
                ]
            else:
                values = [
                    row["condition_label"],
                    f"{row['Vtg_start']:.4g} → {row['Vtg_stop']:.4g}",
                    f"{row['Vbg_start']:.4g} → {row['Vbg_stop']:.4g}",
                    str(row["frames"]),
                ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if error:
                    item.setToolTip(error)
                    item.setBackground(QColor("#fde8e7"))
                table.setItem(row_index, column, item)
        table.setVisible(True)

    def _recalculate_multiple(
        self,
        constants: Sequence[float],
        *,
        op: str,
        ratio: float,
        vbg_step: float,
        vtg_min: float,
        vtg_max: float,
        vbg_min: float,
        vbg_max: float,
        doping_min: float,
        doping_max: float,
        efield_min: float,
        efield_max: float,
    ):
        entries: List[Tuple[float, Optional[dict], str]] = []
        rows: List[dict] = []
        warnings: List[str] = []
        total = len(constants)
        safe_jump = (
            float(self._safe_jump_spin.value())
            if self._safe_jump_spin is not None
            else float("inf")
        )

        for constant in constants:
            result = _solve_condition_line(
                op,
                ratio,
                constant,
                vtg_min,
                vtg_max,
                vbg_min,
                vbg_max,
                doping_min,
                doping_max,
                efield_min,
                efield_max,
            )
            if result is None:
                error = "No valid segment satisfies all voltage and physical limits."
                entries.append((constant, None, error))
                continue
            frames = _compute_frames_from_step(result[0], result[1], vbg_step)
            if frames < 2:
                error = "Segment too short for one step."
                entries.append((constant, None, error))
                continue
            displayed_result = tuple(round(float(value), 4) for value in result)
            error = self._endpoint_error(displayed_result)
            if error:
                entries.append((constant, None, error))
                continue
            row = self._batch_row_for_result(
                constant, displayed_result, frames, total
            )
            rows.append(row)
            entries.append((constant, row, ""))
            vtg_step = abs(row["Vtg_stop"] - row["Vtg_start"]) / max(frames - 1, 1)
            vbg_step_actual = abs(row["Vbg_stop"] - row["Vbg_start"]) / max(frames - 1, 1)
            if max(vtg_step, vbg_step_actual) > safe_jump + 1e-12:
                warnings.append(row["condition_label"])

        self._show_multi_preview(entries)
        first_valid = next((entry[1] for entry in entries if entry[1] is not None), None)
        if first_valid is not None:
            self._update_result(
                (
                    first_valid["Vbg_start"],
                    first_valid["Vbg_stop"],
                    first_valid["Vtg_start"],
                    first_valid["Vtg_stop"],
                ),
                first_valid["frames"],
            )
            if self._label_auto:
                self._condition_edit.blockSignals(True)
                self._condition_edit.setText(first_valid["condition_label"])
                self._condition_edit.blockSignals(False)

        invalid_count = total - len(rows)
        if invalid_count:
            first_error = next(error for _constant, _row, error in entries if error)
            self._calculated_rows = []
            self._set_status(
                f"✗ {invalid_count} of {total} constants are invalid. {first_error}",
                "#b42318",
            )
            self._add_btn.setText(f"Add {total} Rows to Batch Table")
            self._add_btn.setEnabled(False)
            return

        self._calculated_rows = rows
        self._add_btn.setText(f"Add All {total} Rows to Batch Table  ▸")
        if warnings:
            self._set_status(
                f"⚠ Calculated {total} rows; {len(warnings)} exceed the configured safe jump.",
                "#c26a00",
            )
        elif self._include_vbias_chk.isChecked() and not self._vbias_available():
            self._set_status(
                f"⚠ Calculated {total} rows with Vbias, but its Keithley channel is unavailable.",
                "#c26a00",
            )
        else:
            self._set_status(f"✓ Calculated {total} sweep rows.", "#23642c")
        self._add_btn.setEnabled(True)

    def _on_add_clicked(self):
        if not self._calculated_rows:
            return
        if len(self._calculated_rows) == 1:
            constant = _parse_sweep_constants(self._constant_edit.text())[0]
            displayed_result = (
                self._vbg_start_spin.value(),
                self._vbg_stop_spin.value(),
                self._vtg_start_spin.value(),
                self._vtg_stop_spin.value(),
            )
            error = self._endpoint_error(displayed_result)
            if error:
                self._set_status(f"✗ {error}", "#b42318")
                self._add_btn.setEnabled(False)
                return
            self._calculated_rows = [
                self._batch_row_for_result(
                    constant,
                    displayed_result,
                    int(self._frames_spin.value()),
                    1,
                )
            ]
        if self._include_vbias_chk.isChecked() and not self._vbias_available():
            QMessageBox.information(
                self,
                "Vbias Keithley unavailable",
                f"The generated rows include Vbias = {self._vbias_spin.value():.4f} V. Run will remain blocked until a healthy Vbias Keithley channel is connected.",
            )
        self.add_rows_requested.emit(
            [dict(row) for row in self._calculated_rows]
        )


# ── Run worker ────────────────────────────────────────────────────────────────

class _RunWorker(QObject):
    log            = Signal(str)
    progress       = Signal(int, int)          # (done_files, total_files) — drives tree
    frame_progress = Signal(int, int)          # (done_frames, total_frames) — drives progress bar
    active_frame   = Signal(int, str, int, int, int)
    tree_update    = Signal(int, str, int)
    incident       = Signal(object)
    acquisition_ready = Signal(object)
    finished       = Signal(bool, str)
    error          = Signal(str)

    def __init__(
        self,
        final_sequence: List[dict],
        df_batch: pd.DataFrame,
        *,
        lf6_ctrl=None, smu_ctrl=None,
        rotation_ctrl=None, stage_ctrl=None, pm_ctrl=None,
        out_dir: Path,
        run_meta: Dict[str, Any],
        filename_parts: List[str],
        stop_event: threading.Event,
        preview_event: Optional[threading.Event] = None,
    ) -> None:
        super().__init__()
        self._seq      = final_sequence
        self._batch    = df_batch
        self._lf6      = lf6_ctrl
        self._smu      = smu_ctrl
        self._rot      = rotation_ctrl
        self._stage    = stage_ctrl
        self._pm       = pm_ctrl
        self._out_dir  = out_dir
        self._meta     = dict(run_meta)
        self._parts    = list(filename_parts)
        self._stop     = stop_event
        self._preview_event = preview_event
        self._active_run_context: Dict[str, Any] = {}
        self._required_smu_roles = _required_smu_roles(self._seq, self._batch)

    def _require_smu_ready(self):
        issues = _smu_readiness_issues(self._smu, self._required_smu_roles)
        if issues:
            raise _RunFlowError("SMU safety", issues[0])
        return self._smu.device

    @Slot()
    def run(self) -> None:
        from app.engine.csv_writer import CSVWriter

        total_acq = sum(
            max(int(r.get("repeat", 1)), 1)
            for ctx in self._seq
            for _, r in self._batch.iterrows()
            if _when_ok(r.get("When", ""), _outer_ctx(ctx))
        )
        total_points = _count_total_points(self._seq, self._batch)
        done = 0
        done_frames = 0
        failed = False
        summary = "Run complete."
        hardware_error = None
        hardware_stage = "smu_io"
        hardware_traceback = ""
        cleanup_report: Dict[str, Any] = {
            "attempted": False,
            "roles": {},
        }

        try:
            self._require_smu_ready()
            self.log.emit(
                f"Resolved run plan: {len(self._seq)} sequence(s), "
                f"{total_acq} acquisition file(s), {total_points} sweep point(s)."
            )
            self.log.emit(
                f"Direct-jump mode active. Safe jump limit={float(cfg.ramp.safe_jump_V):g} V. "
                f"Ramp-to-zero runs after measurement only and at 2x slower speed."
            )
            for seq_i, ctx in enumerate(self._seq):
                if self._stop.is_set():
                    summary = "Run stopped by user."
                    break

                center   = float(ctx.get("Center Wavelength (nm)", cfg.lf6.center_nm))
                exp_ms   = float(ctx.get("Exposure Time (ms)",     cfg.lf6.exposure_ms))
                accum    = int(ctx.get("Accumulations (EPF)",      cfg.lf6.accumulations))
                val_rot1  = ctx.get("Rotation1 Angle (deg)")
                val_rot2  = ctx.get("Rotation2 Angle (deg)")
                val_stage = ctx.get("Stage Position")

                ctx_bits = []
                for key in ("Center Wavelength (nm)", "Exposure Time (ms)", "Accumulations (EPF)", "Rotation1 Angle (deg)", "Rotation2 Angle (deg)", "Stage Position"):
                    if key in ctx and ctx.get(key) is not None:
                        ctx_bits.append(f"{key}={ctx.get(key)}")
                self.log.emit(
                    f"Sequence {seq_i+1}/{len(self._seq)} resolved: "
                    + (", ".join(ctx_bits) if ctx_bits else "(defaults only)")
                )

                if self._lf6 and self._lf6.is_connected:
                    self._lf6.apply_settings(exp_ms, center, accum)
                    time.sleep(0.15)

                if val_rot1 is not None and self._rot and self._rot.is_connected("rot1"):
                    self._rot.move_to("rot1", float(val_rot1))
                if val_rot2 is not None and self._rot and self._rot.is_connected("rot2"):
                    self._rot.move_to("rot2", float(val_rot2))
                if val_stage is not None and self._stage and self._stage.is_connected:
                    stage_target = _validate_stage_position_value(val_stage)
                    stage_profile = _active_stage_profile()
                    stage_axis = getattr(self._stage.adapter, "axis", None)
                    axis_text = f", axis {stage_axis}" if stage_axis is not None else ""
                    self.log.emit(
                        f"Linear stage ({stage_profile.display_name}{axis_text}) -> {stage_target:g} "
                        f"{stage_profile.position_unit}"
                    )
                    self._stage.move_to(stage_target)

                outer = _outer_ctx(ctx)
                for _, row in self._batch.iterrows():
                    if not _when_ok(row.get("When", ""), outer):
                        continue
                    if self._stop.is_set():
                        summary = "Run stopped by user."
                        break

                    row_dict = row.to_dict()
                    cond_label = build_condition_display_label(
                        row_dict.get("condition_label", ""),
                        row_dict.get("Vbias_start"),
                        row_dict.get("Vbias_stop"),
                    ) or clean_condition_label(row_dict.get("condition_label", "")) or "condition"
                    n_rep    = max(int(row.get("repeat", 1)), 1)
                    try:
                        sweep = _resolve_sweep_vectors(row_dict)
                    except Exception as e:
                        raise _RunFlowError("planning", f"Sweep parse error for '{cond_label}': {e}") from e

                    n_points = int(sweep["frames"])
                    point_count = int(sweep["point_count"])
                    vbg_s = float(sweep["vbg_start"]); vbg_e = float(sweep["vbg_stop"])
                    vtg_s = float(sweep["vtg_start"]); vtg_e = float(sweep["vtg_stop"])
                    vbias_s = sweep["vbias_start"]
                    vbias_e = sweep["vbias_stop"]
                    vbg_step = sweep["vbg_step"]
                    vtg_step = sweep["vtg_step"]
                    vbias_step = sweep["vbias_step"]
                    vbg_points = sweep["vbg_points"]
                    vtg_points = sweep["vtg_points"]
                    vbias_points = sweep["vbias_points"]

                    self.log.emit(
                        f"  Sweep plan | {cond_label}: "
                        f"Vbg {vbg_s:g}->{vbg_e:g} V, "
                        f"Vtg {vtg_s:g}->{vtg_e:g} V, "
                        + (
                            f"Vbias {vbias_s:g}->{vbias_e:g} V, "
                            if vbias_s is not None and vbias_e is not None else ""
                        )
                        + f"frames={n_points}, points={point_count}, "
                        + f"step(Vbg)={float(vbg_step):g} V, step(Vtg)={float(vtg_step):g} V, "
                        + (f"step(Vbias)={float(vbias_step):g} V, " if vbias_step is not None else "")
                        + "mode=direct-jump, zero-ramp=post-run only, "
                        + f"repeat={n_rep}"
                    )

                    self.tree_update.emit(seq_i, cond_label, 0)

                    for r_i in range(n_rep):
                        if self._stop.is_set():
                            summary = "Run stopped by user."
                            break

                        self.tree_update.emit(seq_i, cond_label, r_i)
                        self.log.emit(
                            f"Seq {seq_i+1}/{len(self._seq)} | {cond_label} rep {r_i+1}/{n_rep}"
                        )

                        measured_power_uw = None
                        if _to_bool(row.get("MeasurePower", False)):
                            if self._pm and self._pm.is_connected:
                                try:
                                    p_w = self._pm.adapter.get_power()
                                    measured_power_uw = float(p_w) * 1e6
                                    corrected, _source = resolve_power_uw(
                                        _filename_context_from_row(
                                            self._meta,
                                            ctx,
                                            row_dict,
                                            measured_power_uw=measured_power_uw,
                                        )
                                    )
                                    if corrected is not None:
                                        self.log.emit(f"  Measured power: {corrected:g} uW")
                                except Exception as e:
                                    raise _RunFlowError("power", f"Power read failed: {e}") from e

                        rep_suffix = f"_rep{r_i+1:02d}" if n_rep > 1 else ""

                        try:
                            stem_base, _resolved_ctx, _tokens = _build_run_filename_base(
                                self._meta,
                                ctx,
                                row_dict,
                                measured_power_uw=measured_power_uw,
                                enabled_parts=self._parts,
                            )
                            stem_final = make_unique_stem(self._out_dir, stem_base + rep_suffix)
                            self.log.emit(f"  -> {stem_final}.csv")
                        except Exception as e:
                            raise _RunFlowError("metadata", f"Filename error: {e}") from e

                        writer = None
                        try:
                            csv_path = self._out_dir / f"{stem_final}.csv"
                            writer = CSVWriter(
                                out_dir=str(csv_path.parent),
                                file_base=csv_path.stem,
                                wavelength_headers=[],
                                scalar_fields_order=[
                                    "Vbg_set", "Vbg_meas",
                                    "Vtg_set", "Vtg_meas",
                                    "Vbias_set", "Vbias_meas",
                                    "Ibg", "Itg", "Ibias",
                                ],
                            )
                            for frame_i, (vbg_set, vtg_set) in enumerate(zip(vbg_points, vtg_points), start=1):
                                if self._stop.is_set():
                                    summary = "Run stopped by user."
                                    break

                                vbias_set = vbias_points[frame_i - 1] if vbias_points is not None else None
                                is_start_point = (frame_i == 1)
                                self._active_run_context = {
                                    "sequence": seq_i + 1,
                                    "sequence_total": len(self._seq),
                                    "condition": cond_label,
                                    "repetition": r_i + 1,
                                    "repetition_total": n_rep,
                                    "frame": frame_i,
                                    "frame_total": point_count,
                                    "csv_path": str(csv_path),
                                    "Vbg_set_V": float(vbg_set),
                                    "Vtg_set_V": float(vtg_set),
                                    "Vbias_set_V": (
                                        float(vbias_set) if vbias_set is not None else None
                                    ),
                                }
                                dev = self._require_smu_ready()
                                dev.set_operation_context(
                                    **self._active_run_context
                                )
                                self.active_frame.emit(seq_i, cond_label, r_i, frame_i, point_count)
                                self.log.emit(
                                    f"    Point {frame_i}/{point_count}: "
                                    f"Vbg={float(vbg_set):g} V, Vtg={float(vtg_set):g} V"
                                    + (f", Vbias={float(vbias_set):g} V" if vbias_set is not None else "")
                                    + (" | ramp to sweep start" if is_start_point else " | direct setpoint jump")
                                )

                                try:
                                    if is_start_point:
                                        try:
                                            vbg_now, vtg_now = dev.read_current_gates()
                                        except Exception:
                                            vbg_now = vtg_now = float("nan")
                                        vbias_now = None
                                        if vbias_set is not None and hasattr(dev, "read_current_bias"):
                                            try:
                                                vbias_now = dev.read_current_bias()
                                            except Exception:
                                                vbias_now = None

                                        def _fmt_v(value):
                                            try:
                                                x = float(value)
                                                if np.isfinite(x):
                                                    return f"{x:.3f}"
                                            except Exception:
                                                pass
                                            return "n/a"

                                        self.log.emit(
                                            "    Pre-ramp readback: "
                                            f"Vbg={_fmt_v(vbg_now)} V, "
                                            f"Vtg={_fmt_v(vtg_now)} V"
                                            + (
                                                f", Vbias={_fmt_v(vbias_now)} V"
                                                if vbias_set is not None else
                                                ", Vbias=skipped"
                                            )
                                        )
                                        self.log.emit(
                                            "    Ramping to sweep start: "
                                            f"Vbg {_fmt_v(vbg_now)} -> {float(vbg_set):.3f} V, "
                                            f"Vtg {_fmt_v(vtg_now)} -> {float(vtg_set):.3f} V"
                                            + (
                                                f", Vbias {_fmt_v(vbias_now)} -> {float(vbias_set):.3f} V"
                                                if vbias_set is not None else
                                                ", Vbias skipped"
                                            )
                                        )
                                    dev.set_gates(
                                        Vbg=float(vbg_set), Vtg=float(vtg_set),
                                        ramp_step=(cfg.ramp.step_V if is_start_point else 0.0),
                                        delay_s=(cfg.ramp.delay_s if is_start_point else 0.0),
                                        stop_cb=self._stop.is_set,
                                        stop_exc=_StopRequested,
                                    )
                                    if vbias_set is not None:
                                        dev.set_bias(
                                            Vbias=float(vbias_set),
                                            ramp_step=(cfg.ramp.vbias_step_V if is_start_point else 0.0),
                                            delay_s=(cfg.ramp.delay_s if is_start_point else 0.0),
                                            stop_cb=self._stop.is_set,
                                            stop_exc=_StopRequested,
                                        )
                                except _StopRequested:
                                    raise
                                except Exception as e:
                                    raise _RunFlowError("hardware", f"Gate set error: {e}") from e
                                time.sleep(cfg.ramp.settle_s)

                                if self._stop.is_set():
                                    summary = "Run stopped by user."
                                    break

                                self._require_smu_ready()
                                wl = np.array([]); cts = np.array([])
                                if self._lf6 and self._lf6.is_connected:
                                    try:
                                        wl, cts = self._lf6.adapter.acquire()
                                    except Exception as e:
                                        raise _RunFlowError("acquisition", f"Acquire error: {e}") from e
                                else:
                                    raise _RunFlowError("acquisition", "LF6 is not connected.")

                                if self._stop.is_set():
                                    raise _StopRequested()

                                Ibg = Itg = Ib = None
                                Vbg_meas = Vtg_meas = float("nan")
                                Vbias_meas = float(vbias_set) if vbias_set is not None else None
                                dev = self._require_smu_ready()
                                try:
                                    Ibg, Itg, Ib = dev.read_currents(strict=True)
                                    Vbg_meas, Vtg_meas = dev.read_current_gates(strict=True)
                                    if vbias_set is not None and hasattr(dev, "read_current_bias"):
                                        vb_read = dev.read_current_bias(strict=True)
                                        if vb_read is not None:
                                            Vbias_meas = float(vb_read)
                                except Exception as exc:
                                    if (
                                        self._stop.is_set()
                                        and _find_smu_communication_error(exc) is None
                                    ):
                                        raise _StopRequested() from exc
                                    raise _RunFlowError(
                                        "readback", f"SMU read failed: {exc}"
                                    ) from exc
                                self.log.emit(
                                    "    "
                                    + _format_current_readback(
                                        Ibg,
                                        Itg,
                                        Ib,
                                        include_bias=vbias_set is not None,
                                    )
                                )

                                if wl.size == 0:
                                    raise _RunFlowError("acquisition", "No wavelength headers were returned by the spectrometer.")
                                acquired = np.asarray(cts, dtype=float)
                                try:
                                    if acquired.ndim == 2:
                                        wl, acquired = align_wavelengths_to_image(wl, acquired)
                                        is_full_sensor = True
                                    else:
                                        wl, acquired = align_wavelengths_to_intensities(wl, acquired)
                                        is_full_sensor = False
                                except ValueError as exc:
                                    raise _RunFlowError(
                                        "acquisition",
                                        f"LightField data/axis mismatch: {exc}",
                                    ) from exc
                                if getattr(writer, "_data_rows_written", 0) == 0 and hasattr(writer, "set_wavelength_headers"):
                                    writer.set_wavelength_headers(wl.tolist())

                                row_data = {
                                    "Vbg_set": float(vbg_set), "Vbg_meas": Vbg_meas,
                                    "Vtg_set": float(vtg_set), "Vtg_meas": Vtg_meas,
                                    "Ibg": Ibg, "Itg": Itg, "Ibias": Ib,
                                }
                                if vbias_set is not None:
                                    row_data["Vbias_set"] = float(vbias_set)
                                    row_data["Vbias_meas"] = Vbias_meas
                                if is_full_sensor:
                                    writer.write_matrix(
                                        row_data,
                                        acquired,
                                        point_index=frame_i - 1,
                                        y_pixels=list(range(acquired.shape[0])),
                                    )
                                else:
                                    writer.write_row(row_data, acquired.tolist())
                                preview_payload = {
                                    "csv_path": str(csv_path),
                                    "mode": "full_sensor" if is_full_sensor else "spectrum",
                                    "point_index": frame_i - 1,
                                    "point_number": frame_i,
                                    "point_total": point_count,
                                    **row_data,
                                }
                                if self._preview_event is not None and self._preview_event.is_set():
                                    preview_payload["wavelengths"] = wl.copy()
                                    preview_payload["data"] = acquired.copy()
                                    if is_full_sensor:
                                        preview_payload["y_pixels"] = np.arange(acquired.shape[0], dtype=float)
                                self.acquisition_ready.emit(preview_payload)
                                done_frames += 1
                                self.frame_progress.emit(done_frames, total_points)
                        except (_RunFlowError, _StopRequested):
                            raise
                        except Exception as e:
                            raise _RunFlowError("save", f"CSV write error: {e}") from e
                        finally:
                            if writer is not None:
                                writer.close()

                        if self._stop.is_set():
                            summary = "Run stopped by user."
                            break

                        done += 1
                        self.progress.emit(done, total_acq)

                    if failed or self._stop.is_set():
                        break

                if failed or self._stop.is_set():
                    break

        except _StopRequested:
            summary = "Run stopped by user."
            self.log.emit(summary)

        except _RunFlowError as exc:
            failed = True
            smu_error = _find_smu_communication_error(exc)
            if smu_error is not None:
                hardware_error = smu_error
                hardware_traceback = traceback.format_exc()
                diagnosis = smu_error.diagnosis.get("summary")
                summary = (
                    f"Hardware incident: {diagnosis}"
                    if diagnosis
                    else f"Hardware incident: {smu_error}"
                )
            else:
                summary = f"{exc.stage.capitalize()} failed: {exc.message}"
            self.log.emit(summary)
            self.error.emit(summary)

        except Exception as exc:
            failed = True
            smu_error = _find_smu_communication_error(exc)
            if smu_error is not None:
                hardware_error = smu_error
                hardware_traceback = traceback.format_exc()
                diagnosis = smu_error.diagnosis.get("summary")
                summary = (
                    f"Hardware incident: {diagnosis}"
                    if diagnosis
                    else f"Hardware incident: {smu_error}"
                )
            else:
                summary = f"Unexpected failure: {exc}"
            self.log.emit(summary)
            self.error.emit(summary)
        finally:
            if self._smu and self._smu.is_connected:
                self.log.emit("Ramping all channels to zero (2x slower than sweep settings)...")
                cleanup_report["attempted"] = True
                try:
                    device = self._smu.device
                    if hasattr(device, "ramp_all_to_zero_report"):
                        cleanup_roles = device.ramp_all_to_zero_report(
                            ramp_step=max(float(cfg.ramp.step_V) * 0.5, 1e-6),
                            delay_s=float(cfg.ramp.delay_s) * 2.0,
                        )
                        cleanup_report["roles"] = cleanup_roles
                        cleanup_errors = [
                            f"{role}: {result.get('error', result.get('status'))}"
                            for role, result in cleanup_roles.items()
                            if result.get("status") != "reached_zero"
                        ]
                    else:
                        cleanup_errors = device.ramp_all_to_zero(
                            ramp_step=max(float(cfg.ramp.step_V) * 0.5, 1e-6),
                            delay_s=float(cfg.ramp.delay_s) * 2.0,
                        )
                        cleanup_report["errors"] = list(cleanup_errors or [])
                    if cleanup_errors:
                        for error in cleanup_errors:
                            self.log.emit(f"Cleanup warning: {error}")
                    else:
                        self.log.emit("All available SMU channels reached 0 V.")
                except Exception as e:
                    cleanup_report["error"] = str(e)
                    self.log.emit(f"Cleanup warning: ramp-to-zero error: {e}")

                if hardware_error is None:
                    cleanup_error = getattr(
                        self._smu.device, "last_communication_error", None
                    )
                    if cleanup_error is not None:
                        hardware_error = cleanup_error
                        hardware_stage = "smu_cleanup"
                        failed = True
                        diagnosis = cleanup_error.diagnosis.get("summary")
                        summary = (
                            f"Hardware incident during cleanup: {diagnosis}"
                            if diagnosis
                            else f"Hardware incident during cleanup: {cleanup_error}"
                        )

            if hardware_error is not None:
                incident = build_hardware_incident(
                    hardware_error,
                    stage=hardware_stage,
                    run_context=self._active_run_context,
                    cleanup=cleanup_report,
                    traceback_text=hardware_traceback,
                )
                try:
                    recorder = HardwareIncidentRecorder(self._out_dir)
                    incident["report_path"] = str(recorder.path)
                    report_path = recorder.write(incident)
                    self.log.emit("HARDWARE INCIDENT: " + incident_display_text(incident))
                    self.log.emit(f"Incident report saved: {report_path}")
                except Exception as report_exc:
                    incident["report_write_error"] = str(report_exc)
                    self.log.emit(f"Incident report could not be saved: {report_exc}")
                self.log.emit(
                    "Run will not resume automatically. Disconnect/reconnect the "
                    "SMUs before starting another run."
                )
                self.incident.emit(incident)

            if self._smu and self._smu.is_connected:
                try:
                    self._smu.device.clear_operation_context()
                except Exception:
                    pass

            if failed:
                self.log.emit("Run failed.")
            elif self._stop.is_set():
                self.log.emit("Run stopped.")
            else:
                self.log.emit("Run complete.")
            self.finished.emit(not failed and not self._stop.is_set(), summary)


# ── Loop table read / write ────────────────────────────────────────────────────

def _populate_loop_table(table: QTableWidget, df: pd.DataFrame) -> None:
    """Fill the loop table from a DataFrame, using cell widgets."""
    table.setRowCount(0)
    for r, row in df.iterrows():
        table.insertRow(r)
        table.setCellWidget(r, 0, _make_check_cell(_to_bool(row.get("Enable", False))))
        table.setCellWidget(r, 1, _make_param_combo(str(row.get("Parameter", LOOP_PARAMS[0]))))
        table.setItem(r, 2, QTableWidgetItem(str(row.get("Values", ""))))
        table.setItem(r, 3, QTableWidgetItem(str(int(row.get("Group", 1)))))


def _read_loop_table(table: QTableWidget) -> pd.DataFrame:
    rows = []
    for r in range(table.rowCount()):
        enabled = _cell_checked(table.cellWidget(r, 0))
        combo   = table.cellWidget(r, 1)
        param   = combo.currentText() if combo else LOOP_PARAMS[0]
        v_item  = table.item(r, 2)
        values  = v_item.text() if v_item else ""
        g_item  = table.item(r, 3)
        try:
            group = int(g_item.text()) if g_item and g_item.text().strip() else 1
        except ValueError:
            group = 1
        rows.append({"Enable": enabled, "Parameter": param, "Values": values, "Group": group})
    return pd.DataFrame(rows, columns=LOOP_SCHEMA) if rows else pd.DataFrame(columns=LOOP_SCHEMA)


# ── Batch table read / write ───────────────────────────────────────────────────

def _populate_batch_table(table: QTableWidget, df: pd.DataFrame) -> None:
    signals_were_blocked = table.blockSignals(True)
    updates_were_enabled = table.updatesEnabled()
    table.setUpdatesEnabled(False)
    try:
        table.setRowCount(len(df))
        table.setColumnCount(len(BATCH_SCHEMA))
        table.setHorizontalHeaderLabels(BATCH_SCHEMA)
        for r, row in df.iterrows():
            for c, col in enumerate(BATCH_SCHEMA):
                table.setItem(r, c, _make_batch_table_item(col, row[col]))
        _configure_batch_table_columns(table)
    finally:
        table.setUpdatesEnabled(updates_were_enabled)
        table.blockSignals(signals_were_blocked)
    if updates_were_enabled:
        table.viewport().update()


def _configure_batch_table_columns(table: QTableWidget) -> None:
    """Use predictable widths for dense numeric editing and only stretch low-risk text."""
    hdr = table.horizontalHeader()
    hdr.setStretchLastSection(False)
    for c, col in enumerate(BATCH_SCHEMA):
        hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
        hdr.resizeSection(c, _BATCH_COL_WIDTHS.get(col, 80))
    for col_name in _BATCH_STRETCH_COLUMNS:
        hdr.setSectionResizeMode(BATCH_SCHEMA.index(col_name), QHeaderView.ResizeMode.Stretch)


def _read_batch_table(table: QTableWidget) -> pd.DataFrame:
    rows = []
    for r in range(table.rowCount()):
        row = {}
        for c, col in enumerate(BATCH_SCHEMA):
            item = table.item(r, c)
            if col in _BATCH_BOOL_COLUMNS:
                row[col] = bool(item and item.checkState() == Qt.CheckState.Checked)
            else:
                row[col] = item.text() if item else ""
        rows.append(row)
    return pd.DataFrame(rows, columns=BATCH_SCHEMA) if rows else pd.DataFrame(columns=BATCH_SCHEMA)


def _make_batch_table_item(col: str, val: Any) -> QTableWidgetItem:
    if col in _BATCH_BOOL_COLUMNS:
        item = QTableWidgetItem("")
        item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        item.setCheckState(Qt.CheckState.Checked if _to_bool(val) else Qt.CheckState.Unchecked)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return item

    text = "" if (isinstance(val, float) and pd.isna(val)) else str(val)
    item = QTableWidgetItem(text)
    if col in _BATCH_INT_COLUMNS or col in _BATCH_FLOAT_COLUMNS:
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    return item


def _is_checkable_batch_item(item: Optional[QTableWidgetItem]) -> bool:
    return bool(item and (item.flags() & Qt.ItemFlag.ItemIsUserCheckable))


# ── Main panel ────────────────────────────────────────────────────────────────

class PresetsPanel(QWidget):
    """
    Dual Gate sweep panel.

    Usage:
        panel = PresetsPanel(lf6_ctrl=lf6, smu_ctrl=smu, ...)
    """

    def __init__(
        self,
        lf6_ctrl=None, smu_ctrl=None,
        rotation_ctrl=None, stage_ctrl=None, pm_ctrl=None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._lf6   = lf6_ctrl
        self._smu   = smu_ctrl
        self._rot   = rotation_ctrl
        self._stage = stage_ctrl
        self._pm    = pm_ctrl

        self._loop_src  = _normalize_loop(_DEFAULT_LOOP)
        self._batch_src = _normalize_batch(_DEFAULT_BATCH)
        self._applied_mode = "Synchronize"
        self._tables_dirty = False
        self._last_power_uw: Optional[float] = None
        self._batch_row_clipboard: List[Dict[str, Any]] = []
        self._batch_history: List[pd.DataFrame] = []
        self._batch_history_index = -1
        self._batch_history_restoring = False

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(120)
        self._preview_timer.timeout.connect(self._refresh_filename_preview)

        self._run_thread: Optional[QThread]      = None
        self._run_worker: Optional[_RunWorker]   = None
        self._stop_event = threading.Event()
        self._preview_event = threading.Event()
        self._spectrum_viewer: Optional[DualGateSpectrumViewer] = None
        self._last_acquisition_ref: Optional[dict[str, Any]] = None
        self._hardware_incident_active = False

        self._final_seq:   List[dict] = []
        self._df_batch:    pd.DataFrame = self._batch_src.copy()
        self._total_acq:   int = 0
        self._total_points: int = 0
        self._done_acq: int = 0
        self._done_frames: int = 0
        self._current_seq_i: int = -1
        self._current_label: str = ""
        self._current_rep_i: int = 0
        self._current_frame_i: int = 0
        self._current_frame_total: int = 0
        self._manual_filename_parts = set(_enabled_filename_parts())

        self._build()
        self._refresh_tables()
        self._update_plan()
        self._refresh_draft_state()

    # ── build UI ──────────────────────────────────────────────────────────────

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ── top meta row ───────────────────────────────────────────────────
        meta = QHBoxLayout()
        meta.setSpacing(6)
        self._sample_edit    = QLineEdit(); self._sample_edit.setPlaceholderText("Sample ID")
        self._sample_edit.setToolTip("Sample ID — included at the start of saved filenames.")
        self._sample_edit.setMaximumWidth(120)
        self._point_edit     = QLineEdit(); self._point_edit.setPlaceholderText("p1")
        self._point_edit.setToolTip("Measurement location (e.g. p1, center, edge) — included in filenames.")
        self._point_edit.setFixedWidth(72)
        self._tag_edit       = QLineEdit(); self._tag_edit.setPlaceholderText("")
        self._tag_label = QLabel("Run note:")
        self._tag_edit.setToolTip("Short run tag or note — appended to filenames.")
        self._laser_edit     = QLineEdit(); self._laser_edit.setPlaceholderText("Laser nm")
        self._laser_edit.setFixedWidth(76)
        self._laser_edit.setToolTip("Excitation laser wavelength in nm — recorded in filename.")
        self._power_edit     = QLineEdit(); self._power_edit.setPlaceholderText("Power µW")
        self._power_edit.setFixedWidth(80)
        self._power_edit.setToolTip(
            "Laser power in µW — recorded in filename.\n"
            "Overwritten by a live PM100D reading when MeasurePower is enabled."
        )
        self._subfolder_edit = QLineEdit(); self._subfolder_edit.setPlaceholderText("Initial Data")
        self._subfolder_edit.setToolTip("Optional subfolder created under the base output directory.")
        self._subfolder_edit.setMaximumWidth(130)
        meta.addWidget(QLabel("Sample ID:")); meta.addWidget(self._sample_edit)
        meta.addWidget(QLabel("Point:"));     meta.addWidget(self._point_edit)
        meta.addWidget(self._tag_label);      meta.addWidget(self._tag_edit)
        meta.addWidget(QLabel("Laser:"));  meta.addWidget(self._laser_edit)
        meta.addWidget(QLabel("Power:"));  meta.addWidget(self._power_edit)
        meta.addWidget(QLabel("Subfolder:")); meta.addWidget(self._subfolder_edit)
        self._temp_edit = QLineEdit()
        self._temp_edit.setPlaceholderText("Temp (K)")
        self._temp_edit.setFixedWidth(66)
        self._temp_edit.setText(str(cfg.filename.temperature))
        self._temp_edit.setToolTip("Temperature token used in filenames, for example 6 or 1.8.")
        self._mode_combo_name = QComboBox()
        self._mode_combo_name.addItems(["PL", "Ref"])
        self._mode_combo_name.setCurrentText(str(cfg.filename.measurement_mode or "PL"))
        self._mode_combo_name.setFixedWidth(60)
        self._mode_combo_name.setToolTip("Measurement mode token used in filenames.")
        self._power_coeff_edit = QLineEdit()
        self._power_coeff_edit.setFixedWidth(66)
        self._power_coeff_edit.setText(f"{float(cfg.filename.power_coefficient):g}")
        self._power_coeff_edit.setToolTip(
            "Multiplier applied to measured power before it is written into filenames."
        )
        self._tag_edit.hide()
        self._tag_label.hide()
        self._subfolder_edit.setText("Initial Data")
        meta.insertWidget(4, QLabel("Temp:"))
        meta.insertWidget(5, self._temp_edit)
        meta.insertWidget(6, QLabel("Mode:"))
        meta.insertWidget(7, self._mode_combo_name)
        meta.insertWidget(meta.count() - 2, QLabel("Coeff:"))
        meta.insertWidget(meta.count() - 2, self._power_coeff_edit)

        # Wrap meta row in a styled frame
        meta_frame = QFrame()
        meta_frame.setFrameShape(QFrame.Shape.NoFrame)
        meta_frame.setStyleSheet(
            "QFrame { background: #f5f5f7; border: 1px solid #d8d8d8;"
            " border-radius: 4px; padding: 2px; }"
        )
        meta_frame.setLayout(meta)
        root.addWidget(meta_frame)

        # ── main splitter ──────────────────────────────────────────────────
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter = self._splitter
        root.addWidget(splitter, stretch=1)

        # ── left: tables + apply/discard ──────────────────────────────────
        left = QWidget()
        lay_left = QVBoxLayout(left)
        lay_left.setContentsMargins(0, 0, 0, 0)
        lay_left.setSpacing(6)
        self._workflow_content = left
        self._workflow_layout = lay_left

        # Loop table group
        loop_grp = QGroupBox("Loop table")
        loop_lay = QVBoxLayout(loop_grp)
        loop_lay.setContentsMargins(6, 8, 6, 6)
        loop_lay.setSpacing(4)

        # Mode selector row
        mode_row = QHBoxLayout()
        _mode_lbl = QLabel("Loop mode:")
        _mode_lbl.setToolTip(
            "Controls how enabled loop rows are combined:\n"
            "  Synchronize — nested Cartesian product (row 1 = outermost)\n"
            "  Zip         — all rows stepped together in lockstep\n"
            "  Customized  — manual Group assignment; see Group column"
        )
        mode_row.addWidget(_mode_lbl)
        self._mode_combo = QComboBox()
        for mode, tip in LOOP_MODES.items():
            self._mode_combo.addItem(mode)
            self._mode_combo.setItemData(
                self._mode_combo.count() - 1, tip, Qt.ItemDataRole.ToolTipRole
            )
        self._mode_combo.setToolTip(LOOP_MODES["Synchronize"])
        self._mode_hint = QLabel("")
        self._mode_hint.setStyleSheet("color: gray; font-size: 10px;")
        self._mode_hint.setWordWrap(True)
        mode_row.addWidget(self._mode_combo)
        mode_row.addStretch()
        loop_lay.addLayout(mode_row)
        loop_lay.addWidget(self._mode_hint)

        # Loop table itself
        self._loop_table = QTableWidget(0, len(LOOP_SCHEMA))
        self._loop_table.setMinimumHeight(80)
        self._loop_table.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._loop_table.setHorizontalHeaderLabels(LOOP_SCHEMA)
        self._loop_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._loop_table.verticalHeader().setVisible(False)
        # Column header tooltips
        _loop_hdr_tips = {
            "Enable":    "Check to include this row in the sweep.",
            "Parameter": "Instrument parameter to sweep over.",
            "Values":    (
                "Values to step through — comma-separated, e.g.  830, 860, 890\n"
                "Linspace shorthand: (start, stop, n)  e.g.  (830, 890, 7)\n"
                "  → generates n evenly-spaced values (linspace only supported\n"
                "    for Stage Position). Example: (0,50,51) or linspace(0,50,51)"
            ),
            "Group":     (
                "Customized mode only.\n"
                "Rows sharing the same Group number are zipped (stepped together).\n"
                "Different Group numbers are Cartesian-producted."
            ),
        }
        for i, col in enumerate(LOOP_SCHEMA):
            item = self._loop_table.horizontalHeaderItem(i)
            if item and col in _loop_hdr_tips:
                item.setToolTip(_loop_hdr_tips[col])
        # Set column widths
        self._loop_table.horizontalHeader().resizeSection(0, 55)   # Enable
        self._loop_table.horizontalHeader().resizeSection(1, 180)  # Parameter
        self._loop_table.horizontalHeader().resizeSection(2, 120)  # Values
        self._loop_table.horizontalHeader().resizeSection(3, 55)   # Group
        self._loop_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        loop_btn_row = QHBoxLayout()
        loop_btn_row.setSpacing(4)
        self._loop_add_btn = QPushButton("+ Row"); self._loop_add_btn.setFixedWidth(64)
        self._loop_add_btn.setToolTip("Append a new empty row to the loop table.")
        self._loop_del_btn = QPushButton("− Row"); self._loop_del_btn.setFixedWidth(64)
        self._loop_del_btn.setToolTip("Delete the selected row(s) from the loop table.")
        loop_btn_row.addWidget(self._loop_add_btn)
        loop_btn_row.addWidget(self._loop_del_btn)
        loop_btn_row.addStretch()
        loop_lay.addWidget(self._loop_table)
        loop_lay.addLayout(loop_btn_row)
        lay_left.addWidget(loop_grp)

        # Batch table group
        batch_grp = QGroupBox("Batch table")
        batch_lay = QVBoxLayout(batch_grp)
        batch_lay.setContentsMargins(6, 8, 6, 6)
        batch_lay.setSpacing(4)
        self._batch_table = QTableWidget(0, len(BATCH_SCHEMA))
        self._batch_table.setHorizontalHeaderLabels(BATCH_SCHEMA)
        self._batch_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._batch_table.verticalHeader().setVisible(False)
        self._batch_table.verticalHeader().setDefaultSectionSize(30)
        self._batch_table.setMinimumHeight(100)
        self._batch_table.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._batch_table.setStyleSheet(
            "QTableWidget::item { padding: 2px 4px; }"
        )
        # Batch column header tooltips
        _batch_hdr_tips = {
            "Run":             "Check to include this row in the batch.",
            "When":            (
                "Optional safe condition — row runs only when this evaluates to True.\n"
                "Leave blank (or 'always') to run unconditionally.\n"
                "Use == to compare values. Example: Center_Wavelength == 860\n"
                "Invalid conditions are highlighted and block Apply and Run."
            ),
            "MeasurePower":    (
                "If checked, the PM100D reads laser power just before acquisition.\n"
                "The measured value overwrites the manual Power field in the filename."
            ),
            "condition_label": "Short label appended to the filename to identify this Dual Gate condition.",
            "repeat":          "Number of times to repeat this file acquisition.",
            "frames":          "Number of measured sweep points. Example: 0->1 V with frames=11 measures 11 points spaced by 0.1 V.",
            "Vbg_start":       "Back-gate voltage at the first Dual Gate sweep point (V).",
            "Vbg_stop":        "Back-gate voltage at the final Dual Gate sweep point (V).",
            "Vtg_start":       "Top-gate voltage at the first Dual Gate sweep point (V).",
            "Vtg_stop":        "Top-gate voltage at the final Dual Gate sweep point (V).",
            "Vbias_start":     "Source-drain bias at the first sweep point (V). Leave blank to skip.",
            "Vbias_stop":      "Source-drain bias at the final sweep point (V). Leave blank to skip.",
        }
        for i, col in enumerate(BATCH_SCHEMA):
            item = self._batch_table.horizontalHeaderItem(i)
            if item and col in _batch_hdr_tips:
                item.setToolTip(_batch_hdr_tips[col])
        # When-column delegate — autocomplete from loop table parameter names
        self._when_delegate = _WhenDelegate(self._loop_table, self._batch_table)
        _when_col = BATCH_SCHEMA.index("When")
        self._batch_table.setItemDelegateForColumn(_when_col, self._when_delegate)
        self._batch_int_delegate = _IntSpinDelegate(parent=self._batch_table)
        self._batch_float_delegate = _OptionalFloatDelegate(parent=self._batch_table)
        for col_name in _BATCH_INT_COLUMNS:
            self._batch_table.setItemDelegateForColumn(BATCH_SCHEMA.index(col_name), self._batch_int_delegate)
        for col_name in _BATCH_FLOAT_COLUMNS:
            self._batch_table.setItemDelegateForColumn(BATCH_SCHEMA.index(col_name), self._batch_float_delegate)
        self._batch_table.setAlternatingRowColors(True)
        self._batch_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        _configure_batch_table_columns(self._batch_table)

        batch_btn_row = QHBoxLayout()
        batch_btn_row.setSpacing(4)
        self._batch_add_btn = QPushButton("+ Row"); self._batch_add_btn.setFixedWidth(64)
        self._batch_add_btn.setToolTip(
            "Insert a new empty row below the selected row. Appends when no row is selected."
        )
        self._batch_duplicate_btn = QPushButton("Duplicate")
        self._batch_duplicate_btn.setFixedWidth(82)
        self._batch_duplicate_btn.setToolTip(
            "Copy the selected batch row and insert the duplicate directly below it."
        )
        self._batch_rev_btn = QPushButton("Rev")
        self._batch_rev_btn.setFixedWidth(58)
        self._batch_rev_btn.setToolTip(
            "Create a reverse sweep below the selected row by swapping every "
            "start/stop voltage and appending _Rev to the label."
        )
        self._batch_del_btn = QPushButton("− Row"); self._batch_del_btn.setFixedWidth(64)
        self._batch_del_btn.setToolTip("Delete the selected row(s) from the batch table.")
        self._batch_up_btn = QPushButton("↑ Up"); self._batch_up_btn.setFixedWidth(64)
        self._batch_up_btn.setToolTip("Move the selected batch row up one position.")
        self._batch_down_btn = QPushButton("↓ Down"); self._batch_down_btn.setFixedWidth(72)
        self._batch_down_btn.setToolTip("Move the selected batch row down one position.")
        self._batch_actions_btn = QToolButton()
        self._batch_actions_btn.setText("Actions")
        self._batch_actions_btn.setToolTip(
            "More batch tools: run-state controls, copy/paste, arrangement, "
            "auto-frames, validation, and undo/redo."
        )
        self._batch_actions_btn.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly
        )
        self._batch_actions_btn.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self._batch_actions_btn.setMinimumWidth(76)
        self._build_batch_actions()
        batch_btn_row.addWidget(self._batch_add_btn)
        batch_btn_row.addWidget(self._batch_duplicate_btn)
        batch_btn_row.addWidget(self._batch_rev_btn)
        batch_btn_row.addWidget(self._batch_del_btn)
        batch_btn_row.addSpacing(8)
        batch_btn_row.addWidget(self._batch_up_btn)
        batch_btn_row.addWidget(self._batch_down_btn)
        batch_btn_row.addWidget(self._batch_actions_btn)
        batch_btn_row.addStretch()
        batch_lay.addWidget(self._batch_table)
        batch_lay.addLayout(batch_btn_row)
        lay_left.addWidget(batch_grp, stretch=1)

        self._sweep_calc = _SweepLineCalculator(
            smu_ctrl=self._smu,
            safe_jump_spin=self._safe_jump_spin if hasattr(self, "_safe_jump_spin") else None,
        )
        self._sweep_calc.add_rows_requested.connect(self._on_calculator_add_rows)
        self._sweep_calc.expanded_changed.connect(self._on_calculator_expanded)
        lay_left.addWidget(self._sweep_calc)

        apply_row = QHBoxLayout()
        apply_row.setSpacing(6)
        self._apply_btn   = QPushButton("Apply")
        self._apply_btn.setMinimumHeight(26)
        self._apply_btn.setMinimumWidth(80)
        self._apply_btn.setToolTip(
            "Commit the current table edits and rebuild the run plan preview."
        )
        self._apply_btn.setStyleSheet(
            "QPushButton { font-weight: 600; border-color: #90a8c0; }"
            "QPushButton:hover { border-color: #5a82a8; }"
        )
        self._discard_btn = QPushButton("Discard")
        self._discard_btn.setMinimumHeight(26)
        self._discard_btn.setToolTip("Revert the tables to the last applied state.")
        apply_row.addWidget(self._apply_btn)
        apply_row.addWidget(self._discard_btn)
        self._draft_badge = QLabel("Plan applied")
        self._draft_badge.setObjectName("DualGateDraftBadge")
        self._draft_badge.setStyleSheet(
            "padding: 3px 8px; border-radius: 8px; color: #23642c; "
            "background: #e6f4e8; border: 1px solid #b9ddbe;"
        )
        apply_row.addWidget(self._draft_badge)
        apply_row.addStretch()
        lay_left.addLayout(apply_row)

        self._workflow_scroll = QScrollArea()
        self._workflow_scroll.setObjectName("DualGateWorkflowScroll")
        self._workflow_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._workflow_scroll.setWidgetResizable(True)
        self._workflow_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._workflow_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._workflow_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._workflow_scroll.setWidget(left)
        splitter.addWidget(self._workflow_scroll)

        # ── right: tree + progress + run/stop + log ───────────────────────
        right = QWidget()
        lay_right = QVBoxLayout(right)
        lay_right.setContentsMargins(4, 0, 0, 0)
        lay_right.setSpacing(6)

        self._summary_lbl = QLabel("Apply tables to update the Dual Gate plan.")
        self._summary_lbl.setWordWrap(True)
        lay_right.addWidget(self._summary_lbl)

        self._readiness_lbl = QLabel("")
        self._readiness_lbl.setWordWrap(True)
        self._readiness_lbl.setObjectName("DualGateReadiness")
        lay_right.addWidget(self._readiness_lbl)

        safety_grp = QGroupBox("Dual Gate Safety")
        safety_form = QFormLayout(safety_grp)
        self._safe_jump_spin = QDoubleSpinBox()
        self._safe_jump_spin.setRange(0.01, 100.0)
        self._safe_jump_spin.setDecimals(3)
        self._safe_jump_spin.setSingleStep(0.1)
        self._safe_jump_spin.setValue(float(cfg.ramp.safe_jump_V))
        self._safe_jump_spin.setSuffix(" V")
        self._safe_jump_spin.setToolTip(
            "Maximum allowed direct voltage jump between Dual Gate sweep points. "
            "Runs are blocked if any resolved Vtg, Vbg, or Vbias jump exceeds this limit."
        )
        safety_form.addRow("Max jump / step:", self._safe_jump_spin)
        lay_right.addWidget(safety_grp)

        file_grp = QGroupBox("Filename preview")
        file_lay = QVBoxLayout(file_grp)
        self._filename_parts_table = QTableWidget(len(PART_SPECS), 3)
        self._filename_parts_table.setHorizontalHeaderLabels(["Use", "Part", "Preview"])
        self._filename_parts_table.verticalHeader().setVisible(False)
        self._filename_parts_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._filename_parts_table.horizontalHeader().resizeSection(0, 40)
        self._filename_parts_table.horizontalHeader().resizeSection(1, 108)
        self._filename_parts_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._filename_parts_table.setMaximumHeight(200)
        self._filename_parts_table.setAlternatingRowColors(True)
        self._filename_parts_table.hide()
        self._filename_preview_lbl = QLabel("Filename: -")
        self._filename_preview_lbl.setWordWrap(True)
        self._filename_preview_lbl.setStyleSheet("font-family: monospace;")
        self._save_path_preview_lbl = QLabel("Folder: -")
        self._save_path_preview_lbl.setWordWrap(True)
        self._save_path_preview_lbl.setStyleSheet("color: gray;")
        self._preview_note_lbl = QLabel("")
        self._preview_note_lbl.setWordWrap(True)
        self._preview_note_lbl.setStyleSheet("color: gray; font-size: 10px;")
        self._upcoming_preview = QTextEdit()
        self._upcoming_preview.setReadOnly(True)
        self._upcoming_preview.setMaximumHeight(72)
        self._upcoming_preview.setStyleSheet("font-family: monospace; font-size: 11px;")
        file_lay.addWidget(self._filename_preview_lbl)
        file_lay.addWidget(self._save_path_preview_lbl)
        file_lay.addWidget(self._preview_note_lbl)
        file_lay.addWidget(self._upcoming_preview)
        lay_right.addWidget(file_grp)

        self._tree = RunPlanTree()
        self._tree.setMinimumHeight(150)
        lay_right.addWidget(self._tree, stretch=1)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFormat("%v/%m frames")
        lay_right.addWidget(self._progress)

        self._status_lbl = QLabel("Idle")
        self._status_lbl.setStyleSheet("color: #707070; font-size: 11px;")
        lay_right.addWidget(self._status_lbl)

        run_row = QHBoxLayout()
        run_row.setSpacing(8)
        self._run_btn  = QPushButton("▶  Run")
        self._run_btn.setMinimumHeight(32)
        self._run_btn.setMinimumWidth(110)
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
        self._run_btn.setToolTip(
            "Start the sweep.\n"
            "Click Apply first to lock in any table edits."
        )
        self._stop_btn = QPushButton("■  Stop")
        self._stop_btn.setMinimumHeight(32)
        self._stop_btn.setMinimumWidth(90)
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
        self._stop_btn.setToolTip(
            "Request a graceful stop after the current acquisition finishes.\n"
            "Voltages are ramped back to zero before the run exits."
        )
        self._stop_btn.setEnabled(False)
        self._spectrum_btn = QPushButton("Show Spectrum")
        self._spectrum_btn.setCheckable(True)
        self._spectrum_btn.setMinimumHeight(32)
        self._spectrum_btn.setToolTip(
            "Show the most recent completed LightField acquisition in a separate window."
        )
        run_row.addWidget(self._run_btn)
        run_row.addWidget(self._stop_btn)
        run_row.addWidget(self._spectrum_btn)
        run_row.addStretch()
        lay_right.addLayout(run_row)

        log_grp = QGroupBox("Log")
        log_lay = QVBoxLayout(log_grp)
        self._log_text = QTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setMaximumHeight(190)
        self._log_text.setMinimumHeight(80)
        self._log_text.setStyleSheet(
            "QTextEdit { font-family: 'Consolas', 'Courier New', monospace;"
            " font-size: 11px; background: #fafafa; border: 1px solid #d0d0d0;"
            " border-radius: 3px; }"
        )
        clear_log_btn = QPushButton("Clear")
        clear_log_btn.setFixedWidth(55)
        clear_log_btn.clicked.connect(self._log_text.clear)
        log_hdr = QHBoxLayout()
        log_hdr.addWidget(QLabel("Run log"))
        log_hdr.addStretch()
        log_hdr.addWidget(clear_log_btn)
        log_lay.addLayout(log_hdr)
        log_lay.addWidget(self._log_text)
        lay_right.addWidget(log_grp)

        self._results_content = right
        self._results_scroll = QScrollArea()
        self._results_scroll.setObjectName("DualGateResultsScroll")
        self._results_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._results_scroll.setWidgetResizable(True)
        self._results_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._results_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._results_scroll.setMinimumWidth(430)
        self._results_scroll.setWidget(right)
        splitter.addWidget(self._results_scroll)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 11)
        splitter.setStretchFactor(1, 7)
        splitter.setSizes([880, 500])

        # ── wire ──────────────────────────────────────────────────────────
        self._mode_combo.currentTextChanged.connect(self._on_mode_changed)
        self._apply_btn.clicked.connect(self._on_apply)
        self._discard_btn.clicked.connect(self._on_discard)
        self._loop_add_btn.clicked.connect(self._add_loop_row)
        self._loop_del_btn.clicked.connect(self._del_loop_row)
        self._batch_add_btn.clicked.connect(self._add_batch_row)
        self._batch_duplicate_btn.clicked.connect(self._duplicate_batch_row)
        self._batch_rev_btn.clicked.connect(self._create_rev_batch_row)
        self._batch_del_btn.clicked.connect(self._del_batch_row)
        self._batch_up_btn.clicked.connect(self._move_batch_row_up)
        self._batch_down_btn.clicked.connect(self._move_batch_row_down)
        self._run_btn.clicked.connect(self._on_run)
        self._stop_btn.clicked.connect(self._on_stop)
        self._spectrum_btn.clicked.connect(self._toggle_spectrum_viewer)
        self._loop_table.itemChanged.connect(self._on_draft_edited)
        self._batch_table.itemChanged.connect(self._on_batch_item_changed)
        self._batch_table.itemSelectionChanged.connect(self._update_filename_preview)
        self._batch_table.itemSelectionChanged.connect(self._update_batch_row_buttons)
        self._loop_table.itemSelectionChanged.connect(self._update_filename_preview)
        self._mode_combo.currentTextChanged.connect(lambda _mode: self._update_filename_preview())
        self._mode_combo_name.currentTextChanged.connect(self._update_filename_preview)
        self._safe_jump_spin.valueChanged.connect(self._on_safety_changed)
        for widget in (
            self._sample_edit,
            self._point_edit,
            self._tag_edit,
            self._temp_edit,
            self._laser_edit,
            self._power_edit,
            self._power_coeff_edit,
            self._subfolder_edit,
        ):
            widget.textChanged.connect(self._on_metadata_edited)

        # Initialise mode UI (sets hint label + Group column visibility)
        self._populate_filename_parts()
        self._on_mode_changed(self._mode_combo.currentText())
        self._sweep_calc._safe_jump_spin = self._safe_jump_spin
        self._sweep_calc._recalculate()
        self._update_batch_row_buttons()
        if self._smu is not None:
            self._smu.connected.connect(
                lambda *_: self._sweep_calc.set_vbias_available(self._smu.has_vbias)
            )
            self._smu.connected.connect(self._on_smu_reconnected)
            self._smu.disconnected.connect(lambda: self._sweep_calc.set_vbias_available(False))
            self._smu.connected.connect(self._refresh_readiness)
            self._smu.disconnected.connect(self._refresh_readiness)
        if self._lf6 is not None:
            self._lf6.connected.connect(self._refresh_readiness)
            self._lf6.disconnected.connect(self._refresh_readiness)
        if self._pm is not None:
            self._pm.connected.connect(self._refresh_readiness)
            self._pm.disconnected.connect(self._refresh_readiness)
            self._pm.power_ready.connect(self._cache_preview_power)

    @staticmethod
    def _session_records(df: pd.DataFrame) -> List[dict]:
        """Convert a table to JSON-native records (including NaN -> null)."""
        return json.loads(df.to_json(orient="records"))

    def capture_session_state(self) -> dict:
        """Capture both the edited draft and last applied Dual Gate recipe."""
        calc = self._sweep_calc
        return {
            "metadata": {
                "sample_id": self._sample_edit.text(),
                "point": self._point_edit.text(),
                "tag": self._tag_edit.text(),
                "temperature": self._temp_edit.text(),
                "measurement_mode": self._mode_combo_name.currentText(),
                "laser_nm": self._laser_edit.text(),
                "power_uw": self._power_edit.text(),
                "power_coefficient": self._power_coeff_edit.text(),
                "subfolder": self._subfolder_edit.text(),
            },
            "loop_mode": self._mode_combo.currentText(),
            "applied_loop_mode": self._applied_mode,
            "draft_loop": self._session_records(_read_loop_table(self._loop_table)),
            "draft_batch": self._session_records(_read_batch_table(self._batch_table)),
            "applied_loop": self._session_records(self._loop_src),
            "applied_batch": self._session_records(self._batch_src),
            "safe_jump_v": float(self._safe_jump_spin.value()),
            "filename_parts": [
                key for key, _label in PART_SPECS
                if key in self._manual_filename_parts
            ],
            "calculator": {
                "open": bool(calc._toggle.isChecked()),
                "operator": calc._op_combo.currentText(),
                "ratio": float(calc._ratio_spin.value()),
                "constant": calc._constant_edit.text(),
                "vbg_step": float(calc._vbg_step_spin.value()),
                "vtg_min": float(calc._vtg_min_spin.value()),
                "vtg_max": float(calc._vtg_max_spin.value()),
                "vbg_min": float(calc._vbg_min_spin.value()),
                "vbg_max": float(calc._vbg_max_spin.value()),
                "doping_min": float(calc._doping_min_spin.value()),
                "doping_max": float(calc._doping_max_spin.value()),
                "efield_min": float(calc._efield_min_spin.value()),
                "efield_max": float(calc._efield_max_spin.value()),
                "vbias": float(calc._vbias_spin.value()),
                "include_vbias": bool(calc._include_vbias_chk.isChecked()),
                "condition_label": calc._condition_edit.text(),
            },
            "splitter_sizes": [int(v) for v in self._splitter.sizes()],
        }

    def restore_session_state(self, state: dict) -> None:
        if not isinstance(state, dict):
            return
        metadata = state.get("metadata")
        if isinstance(metadata, dict):
            for key, edit in (
                ("sample_id", self._sample_edit),
                ("point", self._point_edit),
                ("tag", self._tag_edit),
                ("temperature", self._temp_edit),
                ("laser_nm", self._laser_edit),
                ("power_uw", self._power_edit),
                ("power_coefficient", self._power_coeff_edit),
                ("subfolder", self._subfolder_edit),
            ):
                value = metadata.get(key)
                if isinstance(value, str):
                    edit.setText(value)
            mode_name = metadata.get("measurement_mode")
            if (
                isinstance(mode_name, str)
                and self._mode_combo_name.findText(mode_name) >= 0
            ):
                self._mode_combo_name.setCurrentText(mode_name)

        loop_mode = state.get("loop_mode")
        if isinstance(loop_mode, str) and self._mode_combo.findText(loop_mode) >= 0:
            self._mode_combo.setCurrentText(loop_mode)
        applied_loop_mode = state.get("applied_loop_mode", loop_mode)
        if (
            isinstance(applied_loop_mode, str)
            and self._mode_combo.findText(applied_loop_mode) >= 0
        ):
            self._applied_mode = applied_loop_mode
        try:
            self._safe_jump_spin.setValue(float(state["safe_jump_v"]))
        except (KeyError, TypeError, ValueError):
            pass

        parts = state.get("filename_parts")
        if isinstance(parts, list):
            allowed = {key for key, _label in PART_SPECS}
            self._manual_filename_parts = {
                str(key) for key in parts if str(key) in allowed
            }
            self._populate_filename_parts()

        def records_frame(key: str, normalizer, fallback: pd.DataFrame) -> pd.DataFrame:
            records = state.get(key)
            if not isinstance(records, list):
                return fallback.copy()
            try:
                return normalizer(pd.DataFrame(records))
            except Exception:
                return fallback.copy()

        self._loop_src = records_frame(
            "applied_loop", _normalize_loop, self._loop_src
        )
        self._batch_src = records_frame(
            "applied_batch", _normalize_batch, self._batch_src
        )
        draft_loop = records_frame("draft_loop", _normalize_loop, self._loop_src)
        draft_batch = records_frame("draft_batch", _normalize_batch, self._batch_src)
        _populate_loop_table(self._loop_table, draft_loop)
        _populate_batch_table(self._batch_table, draft_batch)
        self._connect_loop_param_signals()
        self._on_mode_changed(self._mode_combo.currentText())

        calculator = state.get("calculator")
        if isinstance(calculator, dict):
            operator = calculator.get("operator")
            if isinstance(operator, str) and self._sweep_calc._op_combo.findText(operator) >= 0:
                self._sweep_calc._op_combo.setCurrentText(operator)
            constant = calculator.get("constant")
            if isinstance(constant, (str, int, float)):
                self._sweep_calc._constant_edit.setText(str(constant))
            for key, spin in (
                ("ratio", self._sweep_calc._ratio_spin),
                ("vbg_step", self._sweep_calc._vbg_step_spin),
                ("vtg_min", self._sweep_calc._vtg_min_spin),
                ("vtg_max", self._sweep_calc._vtg_max_spin),
                ("vbg_min", self._sweep_calc._vbg_min_spin),
                ("vbg_max", self._sweep_calc._vbg_max_spin),
                ("doping_min", self._sweep_calc._doping_min_spin),
                ("doping_max", self._sweep_calc._doping_max_spin),
                ("efield_min", self._sweep_calc._efield_min_spin),
                ("efield_max", self._sweep_calc._efield_max_spin),
                ("vbias", self._sweep_calc._vbias_spin),
            ):
                try:
                    spin.setValue(float(calculator[key]))
                except (KeyError, TypeError, ValueError):
                    pass
            if "include_vbias" in calculator:
                self._sweep_calc._include_vbias_chk.setChecked(
                    bool(calculator["include_vbias"])
                )
            condition = calculator.get("condition_label")
            if isinstance(condition, str):
                self._sweep_calc._condition_edit.setText(condition)
            if "open" in calculator:
                self._sweep_calc._toggle.setChecked(bool(calculator["open"]))
            self._sweep_calc._recalculate()

        sizes = state.get("splitter_sizes")
        if isinstance(sizes, list) and len(sizes) == 2:
            try:
                self._splitter.setSizes([max(0, int(v)) for v in sizes])
            except (TypeError, ValueError):
                pass
        self._update_plan()
        self._refresh_draft_state()
        self._refresh_filename_preview()
        self._reset_batch_history()

    # ── mode ──────────────────────────────────────────────────────────────────

    @Slot(str)
    def _on_mode_changed(self, mode: str):
        # Update tooltip on the combo
        self._mode_combo.setToolTip(LOOP_MODES.get(mode, ""))
        # Update hint label
        hints = {
            "Synchronize": "Outer → inner nesting by row order.  Cartesian product.",
            "Zip":          "All enabled rows stepped together.  Must have equal value counts.",
            "Customized":   "Rows sharing the same Group number are zipped; groups are producted.",
        }
        self._mode_hint.setText(hints.get(mode, ""))
        # Show/hide the Group column
        show_group = (mode == "Customized")
        self._loop_table.setColumnHidden(3, not show_group)
        if hasattr(self, "_draft_badge"):
            self._on_draft_edited()

    # ── table helpers ─────────────────────────────────────────────────────────

    @Slot(bool)
    def _on_calculator_expanded(self, expanded: bool):
        """Reflow inside the workflow scroller without resizing the window."""
        self._workflow_layout.invalidate()
        self._workflow_content.updateGeometry()
        self._workflow_scroll.viewport().update()
        QTimer.singleShot(
            0,
            lambda expanded=bool(expanded): self._finish_workflow_reflow(expanded),
        )

    def _finish_workflow_reflow(self, expanded: bool):
        self._workflow_layout.activate()
        self._workflow_content.updateGeometry()
        target = self._apply_btn if expanded else self._batch_table
        self._workflow_scroll.ensureWidgetVisible(target, 0, 10)

    def _build_batch_actions(self):
        menu = QMenu(self._batch_actions_btn)
        menu.setToolTipsVisible(True)

        def add_action(
            label: str,
            callback,
            *,
            tooltip: str = "",
            shortcut=None,
            target_menu: Optional[QMenu] = None,
        ) -> QAction:
            action = QAction(label, self)
            action.triggered.connect(callback)
            if tooltip:
                action.setToolTip(tooltip)
                action.setStatusTip(tooltip)
            if shortcut is not None:
                action.setShortcuts(shortcut)
                action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            (target_menu or menu).addAction(action)
            return action

        self._batch_undo_action = add_action(
            "Undo Batch Edit",
            self._undo_batch_edit,
            tooltip="Restore the previous batch-table state.",
            shortcut=QKeySequence.StandardKey.Undo,
        )
        self._batch_redo_action = add_action(
            "Redo Batch Edit",
            self._redo_batch_edit,
            tooltip="Reapply the most recently undone batch-table edit.",
            shortcut=QKeySequence.StandardKey.Redo,
        )
        menu.addSeparator()
        self._batch_copy_action = add_action(
            "Copy Selected Rows",
            self._copy_batch_rows,
            tooltip="Copy all selected rows to the internal row clipboard.",
            shortcut=QKeySequence.StandardKey.Copy,
        )
        self._batch_paste_action = add_action(
            "Paste Rows Below",
            self._paste_batch_rows,
            tooltip="Paste copied rows below the selection, or append them.",
            shortcut=QKeySequence.StandardKey.Paste,
        )
        self._batch_duplicate_action = add_action(
            "Duplicate Selected Row",
            self._duplicate_batch_row,
            tooltip="Insert an exact copy below the selected row.",
        )
        self._batch_rev_action = add_action(
            "Create Rev Sweep",
            self._create_rev_batch_row,
            tooltip="Duplicate the row, swap start/stop values, and append _Rev.",
        )
        menu.addSeparator()
        run_menu = menu.addMenu("Run State")
        self._batch_enable_selected_action = add_action(
            "Enable Selected Rows",
            lambda: self._set_selected_rows_run_state(True),
            target_menu=run_menu,
        )
        self._batch_disable_selected_action = add_action(
            "Disable Selected Rows",
            lambda: self._set_selected_rows_run_state(False),
            target_menu=run_menu,
        )
        self._batch_only_selected_action = add_action(
            "Run Only Selected Rows",
            self._run_only_selected_rows,
            tooltip="Enable selected rows and disable every other row.",
            target_menu=run_menu,
        )
        self._batch_enable_all_action = add_action(
            "Enable All Rows",
            lambda: self._set_all_rows_run_state(True),
            target_menu=run_menu,
        )
        self._batch_disable_all_action = add_action(
            "Disable All Rows",
            lambda: self._set_all_rows_run_state(False),
            target_menu=run_menu,
        )
        arrange_menu = menu.addMenu("Arrange and Clean Up")
        self._batch_move_top_action = add_action(
            "Move Selected Row to Top",
            lambda: self._move_batch_row_to_edge(top=True),
            target_menu=arrange_menu,
        )
        self._batch_move_bottom_action = add_action(
            "Move Selected Row to Bottom",
            lambda: self._move_batch_row_to_edge(top=False),
            target_menu=arrange_menu,
        )
        self._batch_delete_disabled_action = add_action(
            "Delete Disabled Rows",
            self._delete_disabled_batch_rows,
            target_menu=arrange_menu,
        )
        tools_menu = menu.addMenu("Sweep Tools")
        self._batch_auto_frames_action = add_action(
            "Auto Frames for Selected",
            self._auto_frames_for_selected_rows,
            tooltip="Set the minimum frame count that satisfies the max jump/step.",
            target_menu=tools_menu,
        )
        self._batch_validate_action = add_action(
            "Validate All Rows",
            self._show_batch_validation,
            tooltip="Check conditions, labels, numeric values, frames, and safe jumps.",
            target_menu=tools_menu,
        )

        self._batch_actions_menu = menu
        self._batch_actions_btn.setMenu(menu)
        self._batch_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.ActionsContextMenu
        )
        self._batch_table.addActions(menu.actions())

    def _refresh_tables(self):
        _populate_loop_table(self._loop_table, self._loop_src)
        _populate_batch_table(self._batch_table, self._batch_src)
        # Reapply mode (column visibility may have changed)
        self._on_mode_changed(self._mode_combo.currentText())
        self._connect_loop_param_signals()
        self._reset_batch_history()
        self._update_filename_preview()

    def _batch_snapshot(self) -> pd.DataFrame:
        return _read_batch_table(self._batch_table).copy(deep=True)

    def _reset_batch_history(self):
        if not hasattr(self, "_batch_table"):
            return
        self._batch_history = [self._batch_snapshot()]
        self._batch_history_index = 0
        self._update_batch_action_states()

    def _record_batch_history(self):
        if self._batch_history_restoring:
            return
        snapshot = self._batch_snapshot()
        if (
            0 <= self._batch_history_index < len(self._batch_history)
            and snapshot.equals(self._batch_history[self._batch_history_index])
        ):
            self._update_batch_action_states()
            return
        self._batch_history = self._batch_history[: self._batch_history_index + 1]
        self._batch_history.append(snapshot)
        if len(self._batch_history) > 50:
            self._batch_history.pop(0)
        self._batch_history_index = len(self._batch_history) - 1
        self._update_batch_action_states()

    def _restore_batch_history(self, index: int):
        if not (0 <= index < len(self._batch_history)):
            return
        selected = self._selected_batch_rows()
        preferred_row = selected[0] if selected else 0
        self._batch_history_restoring = True
        try:
            _populate_batch_table(
                self._batch_table,
                self._batch_history[index].copy(deep=True),
            )
            self._batch_history_index = index
            if self._batch_table.rowCount():
                self._batch_table.selectRow(
                    min(preferred_row, self._batch_table.rowCount() - 1)
                )
        finally:
            self._batch_history_restoring = False
        self._update_batch_row_buttons()
        self._on_draft_edited()

    @Slot()
    def _undo_batch_edit(self):
        self._restore_batch_history(self._batch_history_index - 1)

    @Slot()
    def _redo_batch_edit(self):
        self._restore_batch_history(self._batch_history_index + 1)

    def _commit_batch_change(self):
        self._record_batch_history()
        self._on_draft_edited()

    def _when_names_for_loop(self, loop_df: pd.DataFrame) -> set[str]:
        names: set[str] = set()
        active = _normalize_loop(loop_df)
        active = active[active["Enable"]]
        for param in active["Parameter"].tolist():
            full, short = _param_to_expr_name(str(param))
            names.update(name for name in (full, short) if name)
        return names

    def _validate_when_rows(
        self,
        loop_df: pd.DataFrame,
        batch_df: pd.DataFrame,
        *,
        mark_cells: bool = False,
    ) -> List[Tuple[int, str]]:
        names = self._when_names_for_loop(loop_df)
        errors: List[Tuple[int, str]] = []
        when_column = BATCH_SCHEMA.index("When")
        normalized = _normalize_batch(batch_df)
        signals_were_blocked = self._batch_table.blockSignals(True) if mark_cells else False
        try:
            for row_index, row in normalized.iterrows():
                error = None
                if _to_bool(row.get("Run", True)):
                    error = validate_when_expression(row.get("When", ""), names)
                if error:
                    errors.append((int(row_index), error))
                if mark_cells and row_index < self._batch_table.rowCount():
                    item = self._batch_table.item(int(row_index), when_column)
                    if item is not None:
                        if error:
                            item.setBackground(QColor("#fde8e7"))
                            item.setToolTip(f"Invalid When condition: {error}")
                        else:
                            item.setData(Qt.ItemDataRole.BackgroundRole, None)
                            item.setToolTip(
                                "Optional condition. Use == for comparison; blank means always."
                            )
        finally:
            if mark_cells:
                self._batch_table.blockSignals(signals_were_blocked)
        return errors

    def _draft_frames(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        return (
            _normalize_loop(_read_loop_table(self._loop_table)),
            _normalize_batch(_read_batch_table(self._batch_table)),
        )

    def _draft_is_different(self) -> bool:
        try:
            loop_df, batch_df = self._draft_frames()
            return (
                not loop_df.equals(_normalize_loop(self._loop_src))
                or not batch_df.equals(_normalize_batch(self._batch_src))
                or self._mode_combo.currentText() != self._applied_mode
            )
        except Exception:
            return True

    @Slot()
    def _on_draft_edited(self, *_args):
        self._refresh_draft_state()
        self._update_filename_preview()

    @Slot()
    def _on_metadata_edited(self, *_args):
        self._update_filename_preview()
        self._refresh_readiness()

    @Slot(float)
    def _cache_preview_power(self, power_w: float):
        try:
            self._last_power_uw = float(power_w) * 1e6
        except (TypeError, ValueError):
            self._last_power_uw = None
        self._update_filename_preview()

    @Slot()
    def _on_discard(self):
        self._mode_combo.blockSignals(True)
        self._mode_combo.setCurrentText(self._applied_mode)
        self._mode_combo.blockSignals(False)
        self._refresh_tables()
        self._on_mode_changed(self._applied_mode)
        self._refresh_draft_state()
        self._refresh_filename_preview()

    def _refresh_draft_state(self):
        if not hasattr(self, "_draft_badge"):
            return
        self._tables_dirty = self._draft_is_different()
        try:
            loop_df, batch_df = self._draft_frames()
            when_errors = self._validate_when_rows(
                loop_df, batch_df, mark_cells=True
            )
        except Exception as exc:
            when_errors = [(-1, str(exc))]

        if when_errors:
            self._draft_badge.setText("Invalid draft")
            self._draft_badge.setStyleSheet(
                "padding: 3px 8px; border-radius: 8px; color: #9b1c15; "
                "background: #fde8e7; border: 1px solid #efb7b3;"
            )
        elif self._tables_dirty:
            self._draft_badge.setText("Unapplied changes")
            self._draft_badge.setStyleSheet(
                "padding: 3px 8px; border-radius: 8px; color: #8a5200; "
                "background: #fff3d6; border: 1px solid #ead097;"
            )
        else:
            self._draft_badge.setText("Plan applied")
            self._draft_badge.setStyleSheet(
                "padding: 3px 8px; border-radius: 8px; color: #23642c; "
                "background: #e6f4e8; border: 1px solid #b9ddbe;"
            )
        self._apply_btn.setEnabled(self._tables_dirty and not when_errors)
        self._discard_btn.setEnabled(self._tables_dirty)
        self._refresh_readiness(when_errors=when_errors)

    def _readiness_issues(
        self,
        when_errors: Optional[List[Tuple[int, str]]] = None,
    ) -> List[str]:
        issues: List[str] = []
        if when_errors is None:
            when_errors = self._validate_when_rows(
                self._loop_src, self._batch_src, mark_cells=not self._tables_dirty
            )
        if when_errors:
            row, error = when_errors[0]
            prefix = f"Batch row {row + 1}: " if row >= 0 else ""
            issues.append(prefix + error)
        if self._tables_dirty:
            issues.append("Apply or discard the table changes before running.")
        if self._tables_dirty:
            return issues
        run_meta = self._current_run_meta()
        if not run_meta["device_id"].strip():
            issues.append("Sample ID is required.")
        if not run_meta["temperature"].strip():
            issues.append("Temperature is required.")
        if not self._selected_filename_parts():
            issues.append("At least one filename part is required.")
        if not self._lf6 or not self._lf6.is_connected:
            issues.append("LF6 is not connected or in mock mode.")
        required_smu_roles = _required_smu_roles(self._final_seq, self._df_batch)
        issues.extend(_smu_readiness_issues(self._smu, required_smu_roles))
        if self._hardware_incident_active:
            issues.append("Reconnect the SMUs after the hardware fault.")
        if not self._final_seq or self._df_batch.empty or self._total_acq <= 0:
            issues.append("The applied plan has no runnable acquisitions.")
        if any(
            _to_bool(row.get("MeasurePower", False))
            for _, row in self._df_batch.iterrows()
        ) and (not self._pm or not self._pm.is_connected):
            issues.append("MeasurePower requires a connected PM100D.")
        jump_issues = _validate_safe_jumps(
            self._final_seq, self._df_batch, float(self._safe_jump_spin.value())
        )
        if jump_issues:
            issues.append(jump_issues[0])
        return issues

    @Slot()
    def _refresh_readiness(self, *_args, when_errors=None):
        if not hasattr(self, "_readiness_lbl"):
            return
        running = bool(self._run_thread and self._run_thread.isRunning())
        if running:
            self._readiness_lbl.setText("Running the applied plan.")
            self._readiness_lbl.setStyleSheet(
                "padding: 6px 8px; color: #765000; background: #fff5d9; "
                "border: 1px solid #ecd69c; border-radius: 6px;"
            )
            self._run_btn.setEnabled(False)
            return
        issues = self._readiness_issues(when_errors=when_errors)
        if issues:
            extra = f"  (+{len(issues) - 1} more)" if len(issues) > 1 else ""
            self._readiness_lbl.setText(f"Not ready: {issues[0]}{extra}")
            self._readiness_lbl.setToolTip("\n".join(issues))
            self._run_btn.setToolTip(
                "Cannot start the sweep:\n"
                + "\n".join(f"• {issue}" for issue in issues)
            )
            self._readiness_lbl.setStyleSheet(
                "padding: 6px 8px; color: #8f2019; background: #fff0ef; "
                "border: 1px solid #efc1bd; border-radius: 6px;"
            )
            self._run_btn.setEnabled(False)
        else:
            required_roles = _required_smu_roles(self._final_seq, self._df_batch)
            role_status = "/".join(required_roles)
            if "Vbias" not in required_roles:
                role_status = f"{role_status}; Vbias optional"
            self._readiness_lbl.setText(
                f"Ready to run · {self._total_acq} file(s) · "
                f"{self._total_points} sweep point(s) · Keithley {role_status}"
            )
            self._readiness_lbl.setToolTip("")
            self._run_btn.setToolTip(
                "Start the applied sweep plan.\n"
                f"Keithley safety check passed: {role_status}."
            )
            self._readiness_lbl.setStyleSheet(
                "padding: 6px 8px; color: #24652d; background: #edf8ee; "
                "border: 1px solid #bedfc2; border-radius: 6px;"
            )
            self._run_btn.setEnabled(True)

    def _connect_loop_param_signals(self):
        for r in range(self._loop_table.rowCount()):
            check_widget = self._loop_table.cellWidget(r, 0)
            checkbox = check_widget.findChild(QCheckBox) if check_widget is not None else None
            if checkbox is not None and not checkbox.property("draft_connected"):
                checkbox.toggled.connect(self._on_draft_edited)
                checkbox.setProperty("draft_connected", True)
            combo = self._loop_table.cellWidget(r, 1)
            if combo is not None and not combo.property("draft_connected"):
                combo.currentTextChanged.connect(self._on_draft_edited)
                combo.setProperty("draft_connected", True)

    def _populate_filename_parts(self):
        enabled = set(self._manual_filename_parts)
        self._filename_parts_table.setRowCount(len(PART_SPECS))
        for r, (key, label) in enumerate(PART_SPECS):
            self._filename_parts_table.setCellWidget(r, 0, _make_check_cell(key in enabled))
            self._filename_parts_table.setItem(r, 1, QTableWidgetItem(label))
            self._filename_parts_table.setItem(r, 2, QTableWidgetItem(""))
            cb = self._filename_parts_table.cellWidget(r, 0).findChild(QCheckBox)
            if cb and not cb.property("preview_connected"):
                cb.toggled.connect(self._on_filename_parts_changed)
                cb.setProperty("preview_connected", True)
        self._sync_filename_parts_from_loop_table()

    def _selected_filename_parts(self) -> List[str]:
        selected = self._manual_filename_parts | self._auto_filename_parts_from_loop_table()
        return [key for key, _label in PART_SPECS if key in selected]

    def _auto_filename_parts_from_loop_table(self) -> set[str]:
        auto_parts: set[str] = set()
        try:
            loop_df = _normalize_loop(_read_loop_table(self._loop_table))
        except Exception:
            return auto_parts
        if loop_df.empty:
            return auto_parts
        active = loop_df[loop_df["Enable"]]
        for param in active["Parameter"].tolist():
            key = _LOOP_PARAM_FILENAME_PARTS.get(str(param))
            if key:
                auto_parts.add(key)
        return auto_parts

    def _sync_filename_parts_from_loop_table(self):
        auto_parts = self._auto_filename_parts_from_loop_table()
        selected = self._manual_filename_parts | auto_parts
        for r, (key, _label) in enumerate(PART_SPECS):
            widget = self._filename_parts_table.cellWidget(r, 0)
            cb = widget.findChild(QCheckBox) if widget is not None else None
            if cb is None:
                continue
            desired = key in selected
            if cb.isChecked() != desired:
                cb.blockSignals(True)
                cb.setChecked(desired)
                cb.blockSignals(False)
            if key in auto_parts:
                cb.setToolTip("Auto-included because this parameter is enabled in the loop table.")
            else:
                cb.setToolTip("")

    def _on_filename_parts_changed(self, *_args):
        auto_parts = self._auto_filename_parts_from_loop_table()
        current_checked = {
            key
            for r, (key, _label) in enumerate(PART_SPECS)
            if _cell_checked(self._filename_parts_table.cellWidget(r, 0))
        }
        self._manual_filename_parts = current_checked - auto_parts
        cfg.filename.enabled_parts = [key for key, _label in PART_SPECS if key in self._manual_filename_parts]
        cfg.filename.temperature = self._temp_edit.text().strip() or cfg.filename.temperature
        cfg.filename.measurement_mode = self._mode_combo_name.currentText()
        coeff = _safe_float(self._power_coeff_edit.text())
        cfg.filename.power_coefficient = coeff if coeff is not None else 1.0
        self._sync_filename_parts_from_loop_table()
        self._update_filename_preview()
        self._refresh_readiness()

    def _on_safety_changed(self, value: float):
        cfg.ramp.safe_jump_V = float(value)
        if hasattr(self, "_sweep_calc"):
            self._sweep_calc._recalculate()
        self._update_plan()
        self._refresh_readiness()

    def _draft_loop_and_batch(self) -> Tuple[pd.DataFrame, pd.DataFrame, List[dict], pd.DataFrame]:
        loop_df = _normalize_loop(_read_loop_table(self._loop_table))
        batch_df = _normalize_batch(_read_batch_table(self._batch_table))
        seq, batch, _total = _build_plan(loop_df, batch_df, mode=self._mode_combo.currentText())
        return loop_df, batch_df, seq, batch

    def _current_run_meta(self) -> Dict[str, Any]:
        coeff = _safe_float(self._power_coeff_edit.text())
        return {
            "device_id": self._sample_edit.text().strip(),
            "point": self._point_edit.text().strip(),
            "tag": "",
            "temperature": self._temp_edit.text().strip(),
            "measurement_mode": self._mode_combo_name.currentText(),
            "laser_nm": self._laser_edit.text().strip(),
            "power_uw": self._power_edit.text().strip(),
            "power_coefficient": coeff if coeff is not None else 1.0,
            "subfolder": self._subfolder_edit.text().strip() or "Initial Data",
        }

    def _current_output_dir(self, run_meta: Dict[str, Any]) -> Path:
        device_id = run_meta["device_id"].strip() or "SampleID"
        subfolder = run_meta["subfolder"].strip() or "Initial Data"
        return cfg.base_out / device_id / subfolder

    def _selected_batch_row_dict(self, batch_df: pd.DataFrame) -> Dict[str, Any]:
        rows = self._batch_table.selectionModel().selectedRows() if self._batch_table.selectionModel() else []
        if rows:
            row_idx = rows[0].row()
            if 0 <= row_idx < len(batch_df):
                return batch_df.iloc[row_idx].to_dict()
        if len(batch_df):
            return batch_df.iloc[0].to_dict()
        return {c: "" for c in BATCH_SCHEMA}

    @Slot()
    def _update_filename_preview(self, *_args):
        """Coalesce rapid table edits into one preview rebuild."""
        self._preview_timer.start()

    @Slot()
    def _refresh_filename_preview(self):
        if not hasattr(self, "_filename_preview_lbl"):
            return
        self._sync_filename_parts_from_loop_table()

        try:
            loop_df, batch_df, seq, _batch = self._draft_loop_and_batch()
        except Exception as exc:
            self._filename_preview_lbl.setText(f"Filename: invalid draft ({exc})")
            self._save_path_preview_lbl.setText("Folder: -")
            self._preview_note_lbl.setText("Fix table errors to update the filename preview.")
            self._upcoming_preview.setPlainText("")
            return

        run_meta = self._current_run_meta()
        out_dir = self._current_output_dir(run_meta)
        selected_row = self._selected_batch_row_dict(batch_df)
        ctx = _first_applicable_seq_ctx(seq, selected_row)
        measured_preview_power = (
            self._last_power_uw
            if _to_bool(selected_row.get("MeasurePower", False))
            else None
        )

        try:
            base_name, fname_ctx, _tokens = _build_run_filename_base(
                run_meta,
                ctx,
                selected_row,
                measured_power_uw=measured_preview_power,
                enabled_parts=self._selected_filename_parts(),
            )
            self._filename_preview_lbl.setText(f"Filename: {base_name}.csv")
            self._save_path_preview_lbl.setText(f"Folder: {out_dir}")
            note = (
                "Files are saved under output_root / Sample ID / Subfolder. "
                "Numeric suffixes like _001 are added only when a name collision exists."
            )
            if _to_bool(selected_row.get("MeasurePower", False)):
                note = (
                    "MeasurePower is enabled. The power token uses corrected measured power when available; "
                    "preview uses the most recent asynchronous PM100D reading."
                )
            self._preview_note_lbl.setText(note)
            part_values = build_part_values(fname_ctx)
            for r, (key, _label) in enumerate(PART_SPECS):
                item = self._filename_parts_table.item(r, 2)
                if item is not None:
                    item.setText(part_values.get(key, ""))
        except Exception as exc:
            self._filename_preview_lbl.setText(f"Filename: invalid ({exc})")
            self._save_path_preview_lbl.setText("Folder: -")
            self._preview_note_lbl.setText("Fix the filename inputs before running.")

        upcoming: List[str] = []
        for seq_ctx in seq[:3]:
            for _, row in batch_df.iterrows():
                row_dict = row.to_dict()
                if not _to_bool(row_dict.get("Run", True)):
                    continue
                if not _when_ok(row_dict.get("When", ""), _outer_ctx(seq_ctx)):
                    continue
                try:
                    base_name, _fc, _tokens = _build_run_filename_base(
                        run_meta,
                        seq_ctx,
                        row_dict,
                        enabled_parts=self._selected_filename_parts(),
                    )
                    upcoming.append(base_name)
                except Exception:
                    continue
                if len(upcoming) >= 4:
                    break
            if len(upcoming) >= 4:
                break
        self._upcoming_preview.setPlainText("\n".join(upcoming) if upcoming else "No upcoming filenames yet.")

    def _validate_before_run(self) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        self._refresh_draft_state()
        if self._tables_dirty:
            return None, "Apply or discard the table changes before running."
        when_errors = self._validate_when_rows(self._loop_src, self._batch_src)
        if when_errors:
            row, error = when_errors[0]
            return None, f"Batch row {row + 1} has an invalid When condition: {error}"

        run_meta = self._current_run_meta()
        if not run_meta["device_id"].strip():
            return None, "Sample ID is required before running."
        if not run_meta["temperature"].strip():
            return None, "Temperature is required before running."
        if not self._selected_filename_parts():
            return None, "Enable at least one filename part."
        if not self._lf6 or not self._lf6.is_connected:
            return None, "LF6 must be connected or running in mock mode before a sweep can start."
        if self._df_batch.empty or not self._final_seq:
            return None, "No runnable plan is available."
        required_smu_roles = _required_smu_roles(self._final_seq, self._df_batch)
        smu_issues = _smu_readiness_issues(self._smu, required_smu_roles)
        if smu_issues:
            return None, smu_issues[0]
        if any(_to_bool(row.get("MeasurePower", False)) for _, row in self._df_batch.iterrows()):
            if not self._pm or not self._pm.is_connected:
                return None, "MeasurePower rows require a connected PM100D."
        jump_issues = _validate_safe_jumps(self._final_seq, self._df_batch, float(self._safe_jump_spin.value()))
        if jump_issues:
            for issue in jump_issues:
                self._log(issue)
            return None, jump_issues[0]
        out_dir = self._current_output_dir(run_meta)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return None, f"Save path is invalid: {exc}"
        try:
            first_row = self._df_batch.iloc[0].to_dict()
            _build_run_filename_base(
                run_meta,
                _first_applicable_seq_ctx(self._final_seq, first_row),
                first_row,
                enabled_parts=self._selected_filename_parts(),
            )
        except Exception as exc:
            return None, f"Filename metadata is incomplete: {exc}"
        return run_meta, None

    @Slot()
    def _add_loop_row(self):
        r = self._loop_table.rowCount()
        self._loop_table.insertRow(r)
        self._loop_table.setCellWidget(r, 0, _make_check_cell(False))
        self._loop_table.setCellWidget(r, 1, _make_param_combo(LOOP_PARAMS[0]))
        self._loop_table.setItem(r, 2, QTableWidgetItem(""))
        self._loop_table.setItem(r, 3, QTableWidgetItem("1"))
        self._connect_loop_param_signals()
        self._on_draft_edited()

    @Slot()
    def _del_loop_row(self):
        rows = {i.row() for i in self._loop_table.selectedIndexes()}
        for r in sorted(rows, reverse=True):
            self._loop_table.removeRow(r)
        self._on_draft_edited()

    @Slot()
    def _add_batch_row(self):
        selected = (
            self._batch_table.selectionModel().selectedRows()
            if self._batch_table.selectionModel()
            else []
        )
        r = (
            max(
                (index.row() for index in selected),
                default=self._batch_table.rowCount() - 1,
            )
            + 1
        )
        defaults = {
            "Run": True, "When": "", "MeasurePower": False,
            "condition_label": "baseline", "repeat": "1", "frames": "1",
            "Vbg_start": "0", "Vbg_stop": "0",
            "Vtg_start": "0", "Vtg_stop": "0",
            "Vbias_start": "", "Vbias_stop": "",
        }
        self._insert_batch_rows(r, [defaults])

    @Slot()
    def _del_batch_row(self):
        rows = {i.row() for i in self._batch_table.selectedIndexes()}
        signals_were_blocked = self._batch_table.blockSignals(True)
        try:
            for r in sorted(rows, reverse=True):
                self._batch_table.removeRow(r)
        finally:
            self._batch_table.blockSignals(signals_were_blocked)
        if rows and self._batch_table.rowCount():
            self._batch_table.selectRow(
                min(min(rows), self._batch_table.rowCount() - 1)
            )
        self._update_batch_row_buttons()
        self._commit_batch_change()

    def _selected_batch_row_index(self) -> int:
        rows = self._selected_batch_rows()
        if len(rows) != 1:
            return -1
        return rows[0]

    def _selected_batch_rows(self) -> List[int]:
        selection_model = self._batch_table.selectionModel()
        if selection_model is None:
            return []
        return sorted({int(index.row()) for index in selection_model.selectedRows()})

    def _batch_row_values(self, row: int) -> Dict[str, Any]:
        values: Dict[str, Any] = {}
        for column, name in enumerate(BATCH_SCHEMA):
            item = self._batch_table.item(row, column)
            if name in _BATCH_BOOL_COLUMNS:
                values[name] = bool(
                    item and item.checkState() == Qt.CheckState.Checked
                )
            else:
                values[name] = item.text() if item is not None else ""
        return values

    def _insert_batch_rows(
        self,
        target: int,
        rows: Sequence[Dict[str, Any]],
    ):
        if not rows:
            return
        table = self._batch_table
        target = min(max(int(target), 0), table.rowCount())
        current_column = max(0, table.currentColumn())
        signals_were_blocked = table.blockSignals(True)
        updates_were_enabled = table.updatesEnabled()
        table.setUpdatesEnabled(False)
        try:
            for offset, values in enumerate(rows):
                row_index = target + offset
                table.insertRow(row_index)
                for column, name in enumerate(BATCH_SCHEMA):
                    table.setItem(
                        row_index,
                        column,
                        _make_batch_table_item(name, values.get(name, "")),
                    )
            selected_row = target + len(rows) - 1
            table.setCurrentCell(selected_row, current_column)
            table.selectRow(selected_row)
        finally:
            table.setUpdatesEnabled(updates_were_enabled)
            table.blockSignals(signals_were_blocked)
        if updates_were_enabled:
            table.viewport().update()
        table.scrollTo(table.model().index(selected_row, current_column))
        self._update_batch_row_buttons()
        self._commit_batch_change()

    @Slot()
    def _update_batch_row_buttons(self):
        rows = self._selected_batch_rows()
        row = self._selected_batch_row_index()
        self._batch_duplicate_btn.setEnabled(row >= 0)
        self._batch_rev_btn.setEnabled(row >= 0)
        self._batch_up_btn.setEnabled(row > 0)
        self._batch_down_btn.setEnabled(0 <= row < self._batch_table.rowCount() - 1)
        self._update_batch_action_states(rows)

    def _update_batch_action_states(self, rows: Optional[List[int]] = None):
        if not hasattr(self, "_batch_undo_action"):
            return
        rows = self._selected_batch_rows() if rows is None else rows
        has_rows = bool(rows)
        single_row = rows[0] if len(rows) == 1 else -1
        row_count = self._batch_table.rowCount()
        self._batch_undo_action.setEnabled(self._batch_history_index > 0)
        self._batch_redo_action.setEnabled(
            0 <= self._batch_history_index < len(self._batch_history) - 1
        )
        self._batch_copy_action.setEnabled(has_rows)
        self._batch_paste_action.setEnabled(bool(self._batch_row_clipboard))
        self._batch_duplicate_action.setEnabled(single_row >= 0)
        self._batch_rev_action.setEnabled(single_row >= 0)
        self._batch_enable_selected_action.setEnabled(has_rows)
        self._batch_disable_selected_action.setEnabled(has_rows)
        self._batch_only_selected_action.setEnabled(has_rows)
        self._batch_enable_all_action.setEnabled(row_count > 0)
        self._batch_disable_all_action.setEnabled(row_count > 0)
        self._batch_auto_frames_action.setEnabled(has_rows)
        self._batch_move_top_action.setEnabled(single_row > 0)
        self._batch_move_bottom_action.setEnabled(
            0 <= single_row < row_count - 1
        )
        has_disabled = any(
            self._batch_table.item(row_index, BATCH_SCHEMA.index("Run"))
            and self._batch_table.item(
                row_index, BATCH_SCHEMA.index("Run")
            ).checkState() != Qt.CheckState.Checked
            for row_index in range(row_count)
        )
        self._batch_delete_disabled_action.setEnabled(has_disabled)

    @Slot()
    def _duplicate_batch_row(self):
        source = self._selected_batch_row_index()
        if source < 0:
            return
        self._insert_batch_rows(source + 1, [self._batch_row_values(source)])

    @Slot()
    def _create_rev_batch_row(self):
        source = self._selected_batch_row_index()
        if source < 0:
            return
        values = self._batch_row_values(source)
        for start_name, stop_name in (
            ("Vbg_start", "Vbg_stop"),
            ("Vtg_start", "Vtg_stop"),
            ("Vbias_start", "Vbias_stop"),
        ):
            values[start_name], values[stop_name] = (
                values.get(stop_name, ""),
                values.get(start_name, ""),
            )

        label = str(values.get("condition_label", "")).strip() or "Sweep"
        existing = {
            str(self._batch_row_values(row).get("condition_label", "")).strip()
            for row in range(self._batch_table.rowCount())
        }
        candidate = f"{label}_Rev"
        suffix = 2
        while candidate in existing:
            candidate = f"{label}_Rev{suffix}"
            suffix += 1
        values["condition_label"] = candidate
        self._insert_batch_rows(source + 1, [values])

    @Slot()
    def _copy_batch_rows(self):
        rows = self._selected_batch_rows()
        if not rows:
            return
        self._batch_row_clipboard = [
            dict(self._batch_row_values(row)) for row in rows
        ]
        self._update_batch_action_states(rows)
        self._log(
            f"Copied {len(self._batch_row_clipboard)} batch row(s)."
        )

    @Slot()
    def _paste_batch_rows(self):
        if not self._batch_row_clipboard:
            return
        rows = self._selected_batch_rows()
        target = max(rows) + 1 if rows else self._batch_table.rowCount()
        self._insert_batch_rows(
            target,
            [dict(row) for row in self._batch_row_clipboard],
        )

    def _set_rows_run_state(self, rows: Sequence[int], enabled: bool):
        run_column = BATCH_SCHEMA.index("Run")
        table = self._batch_table
        signals_were_blocked = table.blockSignals(True)
        try:
            for row in rows:
                if not (0 <= int(row) < table.rowCount()):
                    continue
                item = table.item(int(row), run_column)
                if item is not None:
                    item.setCheckState(
                        Qt.CheckState.Checked
                        if enabled
                        else Qt.CheckState.Unchecked
                    )
        finally:
            table.blockSignals(signals_were_blocked)
        self._commit_batch_change()

    def _set_selected_rows_run_state(self, enabled: bool):
        rows = self._selected_batch_rows()
        if rows:
            self._set_rows_run_state(rows, enabled)

    def _set_all_rows_run_state(self, enabled: bool):
        self._set_rows_run_state(
            range(self._batch_table.rowCount()),
            enabled,
        )

    @Slot()
    def _run_only_selected_rows(self):
        selected = set(self._selected_batch_rows())
        if not selected:
            return
        run_column = BATCH_SCHEMA.index("Run")
        table = self._batch_table
        signals_were_blocked = table.blockSignals(True)
        try:
            for row in range(table.rowCount()):
                item = table.item(row, run_column)
                if item is not None:
                    item.setCheckState(
                        Qt.CheckState.Checked
                        if row in selected
                        else Qt.CheckState.Unchecked
                    )
        finally:
            table.blockSignals(signals_were_blocked)
        self._commit_batch_change()

    def _move_batch_row_to_edge(self, *, top: bool):
        source = self._selected_batch_row_index()
        row_count = self._batch_table.rowCount()
        if source < 0 or row_count < 2:
            return
        target = 0 if top else row_count - 1
        if source == target:
            return
        frame = self._batch_snapshot()
        moving = frame.iloc[[source]].copy()
        remaining = frame.drop(frame.index[source]).reset_index(drop=True)
        updated = (
            pd.concat([moving, remaining], ignore_index=True)
            if top
            else pd.concat([remaining, moving], ignore_index=True)
        )
        _populate_batch_table(self._batch_table, updated)
        self._batch_table.selectRow(target)
        self._batch_table.scrollTo(
            self._batch_table.model().index(target, 0)
        )
        self._update_batch_row_buttons()
        self._commit_batch_change()

    @Slot()
    def _delete_disabled_batch_rows(self):
        run_column = BATCH_SCHEMA.index("Run")
        disabled = [
            row
            for row in range(self._batch_table.rowCount())
            if (
                self._batch_table.item(row, run_column) is None
                or self._batch_table.item(
                    row, run_column
                ).checkState() != Qt.CheckState.Checked
            )
        ]
        if not disabled:
            return
        table = self._batch_table
        signals_were_blocked = table.blockSignals(True)
        try:
            for row in reversed(disabled):
                table.removeRow(row)
        finally:
            table.blockSignals(signals_were_blocked)
        if table.rowCount():
            table.selectRow(min(disabled[0], table.rowCount() - 1))
        self._update_batch_row_buttons()
        self._commit_batch_change()

    @Slot()
    def _auto_frames_for_selected_rows(self):
        rows = self._selected_batch_rows()
        if not rows:
            return
        safe_jump = max(float(self._safe_jump_spin.value()), 1e-12)
        frames_column = BATCH_SCHEMA.index("frames")
        table = self._batch_table
        signals_were_blocked = table.blockSignals(True)
        try:
            for row in rows:
                values = self._batch_row_values(row)
                largest_range = 0.0
                for start_name, stop_name in (
                    ("Vbg_start", "Vbg_stop"),
                    ("Vtg_start", "Vtg_stop"),
                    ("Vbias_start", "Vbias_stop"),
                ):
                    start = _safe_float(values.get(start_name))
                    stop = _safe_float(values.get(stop_name))
                    if start is not None and stop is not None:
                        largest_range = max(
                            largest_range, abs(float(stop) - float(start))
                        )
                frames = (
                    1
                    if largest_range <= 1e-12
                    else int(np.ceil(largest_range / safe_jump)) + 1
                )
                item = table.item(row, frames_column)
                if item is not None:
                    item.setText(str(max(frames, 1)))
        finally:
            table.blockSignals(signals_were_blocked)
        self._commit_batch_change()

    def _batch_validation_issues(self) -> List[str]:
        issues: List[str] = []
        loop_df, batch_df = self._draft_frames()
        for row, error in self._validate_when_rows(loop_df, batch_df):
            issues.append(f"Row {row + 1} When: {error}")

        raw = _read_batch_table(self._batch_table)
        for row_index, row in raw.iterrows():
            row_number = int(row_index) + 1
            if _to_bool(row.get("Run", True)) and not str(
                row.get("condition_label", "")
            ).strip():
                issues.append(f"Row {row_number}: condition label is empty.")
            for name in ("repeat", "frames"):
                try:
                    value = int(str(row.get(name, "")).strip())
                    if value < 1:
                        raise ValueError
                except (TypeError, ValueError):
                    issues.append(
                        f"Row {row_number}: {name} must be a positive integer."
                    )
            for name in (
                "Vbg_start",
                "Vbg_stop",
                "Vtg_start",
                "Vtg_stop",
            ):
                if _safe_float(row.get(name)) is None:
                    issues.append(
                        f"Row {row_number}: {name} must be numeric."
                    )
            for name in ("Vbias_start", "Vbias_stop"):
                value = str(row.get(name, "")).strip()
                if value and _safe_float(value) is None:
                    issues.append(
                        f"Row {row_number}: {name} must be numeric or blank."
                    )

        if issues:
            return issues
        try:
            seq, enabled_batch, _total = _build_plan(
                loop_df,
                batch_df,
                mode=self._mode_combo.currentText(),
            )
        except ValueError as exc:
            return [f"Plan: {exc}"]
        issues.extend(
            _validate_safe_jumps(
                seq,
                enabled_batch,
                float(self._safe_jump_spin.value()),
            )
        )
        return issues

    @Slot()
    def _show_batch_validation(self):
        issues = self._batch_validation_issues()
        if issues:
            shown = "\n".join(f"• {issue}" for issue in issues[:12])
            if len(issues) > 12:
                shown += f"\n• …and {len(issues) - 12} more issue(s)."
            QMessageBox.warning(
                self,
                "Batch validation",
                shown,
            )
            return
        QMessageBox.information(
            self,
            "Batch validation",
            "All batch rows and safe-jump limits are valid.",
        )

    def _move_batch_row(self, offset: int):
        source = self._selected_batch_row_index()
        target = source + int(offset)
        if source < 0 or target < 0 or target >= self._batch_table.rowCount():
            return

        table = self._batch_table
        current_column = max(0, table.currentColumn())
        scroll_value = table.verticalScrollBar().value()
        signals_were_blocked = table.blockSignals(True)
        updates_were_enabled = table.updatesEnabled()
        table.setUpdatesEnabled(False)
        try:
            source_items = [
                table.takeItem(source, column)
                for column in range(table.columnCount())
            ]
            target_items = [
                table.takeItem(target, column)
                for column in range(table.columnCount())
            ]
            for column, item in enumerate(target_items):
                if item is not None:
                    table.setItem(source, column, item)
            for column, item in enumerate(source_items):
                if item is not None:
                    table.setItem(target, column, item)
            table.setCurrentCell(target, current_column)
            table.selectRow(target)
            table.verticalScrollBar().setValue(scroll_value)
        finally:
            table.setUpdatesEnabled(updates_were_enabled)
            table.blockSignals(signals_were_blocked)
        if updates_were_enabled:
            table.viewport().update()
        self._update_batch_row_buttons()
        self._commit_batch_change()

    @Slot()
    def _move_batch_row_up(self):
        self._move_batch_row(-1)

    @Slot()
    def _move_batch_row_down(self):
        self._move_batch_row(1)

    @Slot(QTableWidgetItem)
    def _on_batch_item_changed(self, item: Optional[QTableWidgetItem]):
        if item is None:
            self._commit_batch_change()
            return

        col_name = BATCH_SCHEMA[item.column()]
        if col_name in _BATCH_BOOL_COLUMNS and not _is_checkable_batch_item(item):
            normalized_item = _make_batch_table_item(col_name, item.text())
            self._batch_table.blockSignals(True)
            self._batch_table.setItem(item.row(), item.column(), normalized_item)
            self._batch_table.blockSignals(False)

        self._commit_batch_change()

    @Slot(list)
    def _on_calculator_add_rows(self, rows: list):
        if not rows:
            return
        selected = self._selected_batch_rows()
        target = max(selected) + 1 if selected else self._batch_table.rowCount()
        self._insert_batch_rows(target, rows)
        first_position = target + 1
        last_position = target + len(rows)
        position_text = (
            str(first_position)
            if first_position == last_position
            else f"{first_position}-{last_position}"
        )
        self._log(
            f"Added {len(rows)} row(s) at position {position_text} "
            "from Sweep Line Calculator."
        )

    # ── apply / plan ──────────────────────────────────────────────────────────

    @Slot()
    def _on_apply(self):
        loop_draft, batch_draft = self._draft_frames()
        when_errors = self._validate_when_rows(
            loop_draft, batch_draft, mark_cells=True
        )
        if when_errors:
            row, error = when_errors[0]
            self._summary_lbl.setText(
                f"Cannot apply: batch row {row + 1} has an invalid When condition."
            )
            self._summary_lbl.setStyleSheet("color: #b42318;")
            self._refresh_draft_state()
            QMessageBox.warning(
                self,
                "Invalid When condition",
                f"Batch row {row + 1}: {error}",
            )
            return
        mode = self._mode_combo.currentText()
        try:
            _build_plan(loop_draft, batch_draft, mode=mode)
        except ValueError as exc:
            self._summary_lbl.setText(f"Cannot apply: {exc}")
            self._summary_lbl.setStyleSheet("color: #b42318;")
            QMessageBox.warning(self, "Invalid sweep plan", str(exc))
            return

        self._loop_src = loop_draft
        self._batch_src = batch_draft
        self._applied_mode = mode
        cfg.filename.enabled_parts = [key for key, _label in PART_SPECS if key in self._manual_filename_parts]
        cfg.filename.temperature = self._temp_edit.text().strip() or cfg.filename.temperature
        cfg.filename.measurement_mode = self._mode_combo_name.currentText()
        coeff = _safe_float(self._power_coeff_edit.text())
        cfg.filename.power_coefficient = coeff if coeff is not None else 1.0
        self._update_plan()
        jump_issues = _validate_safe_jumps(self._final_seq, self._df_batch, float(self._safe_jump_spin.value()))
        if jump_issues:
            for issue in jump_issues:
                self._log(issue)
            QMessageBox.warning(self, "Unsafe Dual Gate sweep", jump_issues[0])
        self._refresh_draft_state()
        self._refresh_filename_preview()

    def _update_plan(self):
        mode = self._applied_mode
        try:
            seq, batch, total = _build_plan(self._loop_src, self._batch_src, mode=mode)
        except ValueError as exc:
            self._summary_lbl.setText(f"Plan error: {exc}")
            self._summary_lbl.setStyleSheet("color: red;")
            self._final_seq = []
            self._df_batch = _normalize_batch(pd.DataFrame())
            self._total_acq = 0
            self._total_points = 0
            self._progress.setMaximum(1)
            self._progress.setValue(0)
            self._refresh_readiness()
            return
        self._summary_lbl.setStyleSheet("")
        self._final_seq    = seq
        self._df_batch     = batch
        self._total_acq    = total
        total_points = _count_total_points(seq, batch)
        self._total_points = total_points
        self._done_acq = 0
        self._done_frames = 0
        self._current_seq_i = -1
        self._current_label = ""
        self._current_rep_i = 0
        self._current_frame_i = 0
        self._current_frame_total = 0
        self._progress.setMaximum(max(total_points, 1))
        self._progress.setValue(0)
        self._summary_lbl.setText(
            f"{len(seq)} sequence(s) x batch -> {total} file(s), "
            f"{total_points} sweep point(s)  [mode: {mode}]"
        )
        self._tree.update_plan(
            seq,
            batch,
            done=0,
            total_acq=total,
            param_order=self._tree_param_order(),
        )
        self._update_filename_preview()
        self._refresh_readiness()

    def _tree_param_order(self) -> List[str]:
        if hasattr(self, "_loop_src") and not self._loop_src.empty:
            active = self._loop_src[self._loop_src["Enable"]]
            ordered = [str(param) for param in active["Parameter"].tolist() if str(param).strip()]
            deduped: List[str] = []
            seen = set()
            for param in ordered:
                if param not in seen:
                    seen.add(param)
                    deduped.append(param)
            if deduped:
                return deduped
        return [
            "Center Wavelength (nm)",
            "Exposure Time (ms)",
            "Accumulations (EPF)",
            "Rotation1 Angle (deg)",
            "Rotation2 Angle (deg)",
            "Stage Position",
        ]

    # ── run / stop ────────────────────────────────────────────────────────────

    @Slot()
    def _on_run(self):
        if self._run_thread and self._run_thread.isRunning():
            return
        if not self._final_seq:
            self._log("No plan — click Apply first.")
            return

        run_meta, err = self._validate_before_run()
        if err:
            QMessageBox.warning(self, "Cannot start run", err)
            self._log(err)
            return
        out_dir = self._current_output_dir(run_meta)

        self._stop_event.clear()
        self._done_acq = 0
        self._done_frames = 0
        self._current_seq_i = -1
        self._current_label = ""
        self._current_rep_i = 0
        self._current_frame_i = 0
        self._current_frame_total = 0
        self._hardware_incident_active = False
        self._run_thread = QThread(self)
        self._run_worker = _RunWorker(
            self._final_seq, self._df_batch,
            lf6_ctrl=self._lf6, smu_ctrl=self._smu,
            rotation_ctrl=self._rot, stage_ctrl=self._stage, pm_ctrl=self._pm,
            out_dir=out_dir,
            run_meta=run_meta,
            filename_parts=self._selected_filename_parts(),
            stop_event=self._stop_event,
            preview_event=self._preview_event,
        )
        self._run_worker.moveToThread(self._run_thread)
        self._run_thread.started.connect(self._run_worker.run)
        self._run_worker.log.connect(self._log)
        self._run_worker.progress.connect(self._on_progress)
        self._run_worker.frame_progress.connect(self._on_frame_progress)
        self._run_worker.active_frame.connect(self._on_active_frame)
        self._run_worker.tree_update.connect(self._on_tree_update)
        self._run_worker.incident.connect(self._on_hardware_incident)
        self._run_worker.acquisition_ready.connect(self._on_acquisition_ready)
        self._run_worker.error.connect(lambda e: self._log(f"ERROR: {e}"))
        self._run_worker.finished.connect(self._on_finished)
        self._run_worker.finished.connect(self._run_thread.quit)

        self._run_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._status_lbl.setText("Running…")
        self._status_lbl.setStyleSheet("color: orange;")
        self._run_thread.start()

    @Slot(bool)
    def _toggle_spectrum_viewer(self, checked: bool):
        if not checked:
            if self._spectrum_viewer is not None:
                self._spectrum_viewer.hide()
            return
        if self._spectrum_viewer is None:
            self._spectrum_viewer = DualGateSpectrumViewer(self)
            self._spectrum_viewer.visibility_changed.connect(
                self._on_spectrum_viewer_visibility_changed
            )
        self._preview_event.set()
        self._spectrum_viewer.show()
        self._spectrum_viewer.raise_()
        self._spectrum_viewer.activateWindow()
        if self._last_acquisition_ref:
            try:
                loaded = load_last_dual_gate_acquisition(
                    self._last_acquisition_ref["csv_path"]
                )
                loaded.update(self._last_acquisition_ref)
                self._spectrum_viewer.set_acquisition(loaded)
            except Exception as exc:
                self._spectrum_viewer.show_message(
                    f"Could not load the last completed acquisition: {exc}"
                )

    @Slot(bool)
    def _on_spectrum_viewer_visibility_changed(self, visible: bool):
        if visible:
            self._preview_event.set()
        else:
            self._preview_event.clear()
        self._spectrum_btn.blockSignals(True)
        self._spectrum_btn.setChecked(bool(visible))
        self._spectrum_btn.setText("Hide Spectrum" if visible else "Show Spectrum")
        self._spectrum_btn.blockSignals(False)

    @Slot(object)
    def _on_acquisition_ready(self, payload: dict):
        # Retain only lightweight information while hidden.  The complete
        # acquisition remains recoverable from the flushed CSV on demand.
        self._last_acquisition_ref = {
            key: value
            for key, value in payload.items()
            if key not in ("wavelengths", "data", "y_pixels")
        }
        if (
            self._spectrum_viewer is not None
            and self._spectrum_viewer.isVisible()
            and "data" in payload
        ):
            self._spectrum_viewer.set_acquisition(payload)

    @Slot()
    def _on_stop(self):
        self._stop_event.set()
        self._stop_btn.setEnabled(False)
        self._status_lbl.setText("Stopping...")
        self._log("Stop requested.")
        timeout_s = max(0.25, float(getattr(cfg.smu, "visa_timeout_ms", 5000)) / 1000.0)
        self._log(
            f"Waiting for the current hardware call (up to ~{timeout_s:g} s per SMU I/O). "
            "The zero-ramp will begin immediately afterward."
        )

    @Slot(list)
    def _on_smu_reconnected(self, _addresses: list):
        if not self._hardware_incident_active:
            return
        self._hardware_incident_active = False
        self._status_lbl.setText("SMUs reconnected - ready")
        self._status_lbl.setStyleSheet("color: green;")
        self._log("SMUs reconnected and reinitialized; the hardware fault lock is cleared.")

    @Slot(int, int)
    def _on_progress(self, done: int, total: int):
        # Updates the tree only (file-level granularity).
        self._done_acq = int(done)
        self._total_acq = int(total)
        self._tree.update_plan(
            self._final_seq, self._df_batch,
            done=done, total_acq=total,
            current_seq_i=self._current_seq_i,
            current_label=self._current_label,
            current_rep_i=self._current_rep_i,
            current_frame_i=self._current_frame_i,
            current_frame_total=self._current_frame_total,
            param_order=self._tree_param_order(),
        )

    @Slot(int, int)
    def _on_frame_progress(self, done_frames: int, total_frames: int):
        # Updates the progress bar at frame (sweep-point) granularity.
        self._done_frames = int(done_frames)
        self._total_points = int(total_frames)
        self._progress.setMaximum(max(total_frames, 1))
        self._progress.setValue(done_frames)

    @Slot(int, str, int, int, int)
    def _on_active_frame(self, seq_i: int, label: str, rep_i: int, frame_i: int, frame_total: int):
        self._current_seq_i = int(seq_i)
        self._current_label = str(label)
        self._current_rep_i = int(rep_i)
        self._current_frame_i = int(frame_i)
        self._current_frame_total = int(frame_total)
        self._tree.update_plan(
            self._final_seq, self._df_batch,
            done=self._done_acq,
            total_acq=self._total_acq,
            current_seq_i=self._current_seq_i,
            current_label=self._current_label,
            current_rep_i=self._current_rep_i,
            current_frame_i=self._current_frame_i,
            current_frame_total=self._current_frame_total,
            param_order=self._tree_param_order(),
        )

    @Slot(int, str, int)
    def _on_tree_update(self, seq_i: int, label: str, rep_i: int):
        self._current_seq_i = int(seq_i)
        self._current_label = str(label)
        self._current_rep_i = int(rep_i)
        self._current_frame_i = 0
        self._current_frame_total = 0
        self._tree.update_plan(
            self._final_seq, self._df_batch,
            done=self._done_acq, total_acq=self._total_acq,
            current_seq_i=self._current_seq_i,
            current_label=self._current_label,
            current_rep_i=self._current_rep_i,
            current_frame_i=self._current_frame_i,
            current_frame_total=self._current_frame_total,
            param_order=self._tree_param_order(),
        )

    @Slot(bool, str)
    def _on_finished(self, success: bool, message: str):
        self._stop_btn.setEnabled(False)
        if self._hardware_incident_active:
            self._status_lbl.setText("Hardware fault - reconnect SMUs")
            self._status_lbl.setStyleSheet("color: red;")
        elif success:
            self._done_acq = self._total_acq
            self._done_frames = self._total_points
            self._progress.setMaximum(max(self._total_points, 1))
            self._progress.setValue(self._total_points)
            self._tree.update_plan(
                self._final_seq,
                self._df_batch,
                done=self._total_acq,
                total_acq=self._total_acq,
                param_order=self._tree_param_order(),
            )
            self._status_lbl.setText("Completed")
            self._status_lbl.setStyleSheet("color: green;")
        elif self._stop_event.is_set():
            self._status_lbl.setText("Stopped")
            self._status_lbl.setStyleSheet("color: gray;")
        else:
            self._status_lbl.setText("Failed")
            self._status_lbl.setStyleSheet("color: red;")
        QTimer.singleShot(50, self._refresh_readiness)

    @Slot(object)
    def _on_hardware_incident(self, incident: object):
        if not isinstance(incident, dict):
            return
        self._hardware_incident_active = True
        summary = incident_display_text(incident)
        report_path = incident.get("report_path")
        self._status_lbl.setText("Hardware fault - reconnect SMUs")
        self._status_lbl.setStyleSheet("color: red;")

        text = (
            f"{summary}\n\n"
            "The run has been stopped and will not resume automatically. "
            "Other reachable SMUs were ramped toward 0 V; inspect the cleanup "
            "results below. Disconnect and reconnect the SMUs before running again."
        )
        if report_path:
            text += f"\n\nIncident report:\n{report_path}"

        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Critical)
        dialog.setWindowTitle("SMU hardware incident")
        dialog.setText(text)
        dialog.setDetailedText(json.dumps(incident, indent=2, ensure_ascii=False))
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        dialog.exec()

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_text.append(f"[{ts}] {msg}")
