# ui/megasweep_panel.py

from __future__ import annotations

import math
import sys
import threading
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, QPointF, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QFont, QPolygonF
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pyqtgraph as pg
from utils.config import cfg
from utils.filename_builder import format_compact_number, format_decimal_token

pg.setConfigOption("background", "w")
pg.setConfigOption("foreground", "k")

NAN = float("nan")
EPS = 1e-9
_SAFETY_RAMP_STEP_V = 0.1
_SAFETY_RAMP_DELAY_S = 0.02


class _MegaSweepStopRequested(Exception):
    """Internal control-flow exception used to unwind to the safety ramp."""
    pass


class CoordSystem(Enum):
    RAW = "Raw Voltages"
    PHYSICAL = "Physical Coordinates"


class RawAxis(Enum):
    VTG = "Vtg"
    VBG = "Vbg"
    VBIAS = "Vbias"


class PhysAxis(Enum):
    DOPING = "Doping"
    EFIELD = "E-field"
    VBIAS = "Vbias"


RAW_AXIS_ORDER = [RawAxis.VTG.value, RawAxis.VBG.value, RawAxis.VBIAS.value]
PHYS_AXIS_ORDER = [PhysAxis.DOPING.value, PhysAxis.EFIELD.value, PhysAxis.VBIAS.value]
AXIS_UNITS = {
    "Vtg": "V",
    "Vbg": "V",
    "Vbias": "V",
    "Doping": "V",
    "E-field": "V",
}
AXIS_CODES = {
    "Vtg": "tg",
    "Vbg": "bg",
    "Vbias": "Vb",
    "Doping": "D",
    "E-field": "E",
}


def _get_linear_array(start: float, stop: float, param: float, mode: str) -> np.ndarray:
    if mode == "Total Points":
        return np.linspace(start, stop, max(2, int(round(param))))
    step = abs(float(param))
    if step <= 0:
        return np.array([start], dtype=float)
    step = step if stop >= start else -step
    n = int(np.floor((stop - start) / step + 1e-9)) + 1
    vals = start + np.arange(max(1, n), dtype=float) * step
    if vals.size == 0:
        vals = np.array([start], dtype=float)
    if (step > 0 and vals[-1] < stop - 1e-12) or (step < 0 and vals[-1] > stop + 1e-12):
        vals = np.append(vals, stop)
    return vals.astype(float)


def _physics_to_raw(D: float, F: float, r: float) -> tuple[float, float]:
    if abs(r) < EPS:
        raise ValueError("Ratio cannot be zero.")
    vtg = 0.5 * (D + F)
    vbg = 0.5 * (D - F) / r
    return float(vtg), float(vbg)


def _raw_to_physics(Vtg: float, Vbg: float, r: float) -> tuple[float, float]:
    D = Vtg + r * Vbg
    F = Vtg - r * Vbg
    return float(D), float(F)


def _fmt_uA(I: float) -> str:
    try:
        I = float(I)
        return f"{I * 1e6:.3f}" if math.isfinite(I) else "nan"
    except Exception:
        return "nan"


def _read_gates(iv) -> tuple[float, float]:
    if iv is None or not hasattr(iv, "read_current_gates"):
        return NAN, NAN
    try:
        bg, tg = iv.read_current_gates()
        return float(bg) if bg is not None else NAN, float(tg) if tg is not None else NAN
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


def _wait_lambda(lf6, target_nm: float, tol_nm: float = 1.0,
                 timeout_s: float = 25.0, poll_s: float = 0.3) -> np.ndarray:
    deadline = time.time() + timeout_s
    ok = 0
    while time.time() < deadline:
        try:
            w = np.asarray(lf6.get_wavelength_calibration(), dtype=float).ravel()
        except Exception:
            w = np.array([])
        if w.size > 2:
            mid = 0.5 * (w[0] + w[-1])
            if abs(mid - target_nm) <= tol_nm:
                ok += 1
                if ok >= 2:
                    return w
            else:
                ok = 0
        time.sleep(poll_s)
    raise TimeoutError(f"Lambda not converged to {target_nm} nm in {timeout_s}s")


def _get_wavelengths(spec, lf6, center_nm: float, tol_nm: float) -> np.ndarray:
    if lf6 is not None:
        try:
            w = _wait_lambda(lf6, center_nm, tol_nm)
            if w.size > 2:
                return w
        except Exception:
            pass
        try:
            w = np.asarray(lf6.get_wavelength_calibration(), dtype=float).ravel()
            if w.size > 2:
                return w
        except Exception:
            pass
    if spec is not None and hasattr(spec, "calibration_wavelengths"):
        try:
            w = np.asarray(list(spec.calibration_wavelengths()), dtype=float).ravel()
            if w.size > 2:
                return w
        except Exception:
            pass
    if spec is not None:
        try:
            sp = spec.acquire()
            if isinstance(sp, tuple) and len(sp) >= 2:
                w = np.asarray(sp[0], dtype=float).ravel()
                if w.size > 2:
                    return w
        except Exception:
            pass
    return np.array([])


def _read_intensity(spec, expected_len: int) -> np.ndarray:
    try:
        sp = spec.acquire()
        if isinstance(sp, tuple) and len(sp) >= 2:
            y = np.asarray(sp[1]).ravel()
        elif isinstance(sp, dict):
            for key in ("intensity", "y", "counts", "data"):
                if key in sp:
                    y = np.asarray(sp[key]).ravel()
                    break
            else:
                y = np.asarray(sp).ravel()
        else:
            y = np.asarray(sp).ravel()
    except Exception:
        return np.full(expected_len, NAN, dtype=float)
    if y.size > expected_len:
        return y[:expected_len].astype(float)
    if y.size < expected_len:
        return np.pad(y.astype(float), (0, expected_len - y.size), constant_values=NAN)
    return y.astype(float)


def _is_in_bounds(vtg: float, vbg: float, vbias: float, safety: dict) -> bool:
    return (
        safety["vtg_min"] <= vtg <= safety["vtg_max"]
        and safety["vbg_min"] <= vbg <= safety["vbg_max"]
        and safety["vbias_min"] <= vbias <= safety["vbias_max"]
    )


def _format_exposure_seconds_token(exp_ms: float) -> str:
    return f"{int(exp_ms // 1000)}" if exp_ms % 1000 == 0 else format_decimal_token(exp_ms / 1000.0, decimals=3)


def _describe_array(arr: np.ndarray, mode: str, param: float) -> dict:
    vals = np.asarray(arr, dtype=float)
    start = float(vals[0]) if vals.size else 0.0
    stop = float(vals[-1]) if vals.size else 0.0
    if mode == "Step Size" and vals.size >= 2:
        step = float(abs(vals[1] - vals[0]))
    elif mode == "Step Size":
        step = float(param)
    else:
        step = float(param)
    return {
        "start": start,
        "stop": stop,
        "step": step,
        "mode": mode,
        "points": int(vals.size),
    }


def build_megasweep_filename(params: dict) -> str:
    sample = params.get("sample", "Sample") or "Sample"
    tag = params.get("tag", "2DSweep") or "2DSweep"
    axis_a = params["axis_a"]
    axis_b = params["axis_b"]
    coord = params["coord"]
    ratio = float(params.get("ratio", 1.0))
    vbias_available = bool(params.get("vbias_available", True))
    fixed = params.get("fixed", {})

    sweep_token = f"{AXIS_CODES[axis_a]}-{AXIS_CODES[axis_b]}"
    if coord == CoordSystem.PHYSICAL:
        sweep_token = f"{sweep_token}_r{format_decimal_token(ratio, decimals=2)}"

    fixed_tokens: list[str] = []
    for key in sorted(fixed.keys(), key=lambda name: ("Vbias" not in name, name)):
        if key == "Vbias" and not vbias_available:
            fixed_tokens.append("VbNC")
        else:
            fixed_tokens.append(f"{AXIS_CODES[key]}{format_compact_number(fixed[key], keep_sign=True, decimals=3)}")
    fixed_token = "_".join(fixed_tokens) if fixed_tokens else "None"

    exp_s = _format_exposure_seconds_token(float(params.get("exp_ms", 0.0)))
    optical_token = f"{float(params.get('center_nm', 0.0)):.0f}nm_{exp_s}sx{int(params.get('frames', 1))}"
    return f"{sample}~{sweep_token}~{fixed_token}~{optical_token}~{tag}"


def _build_csv_metadata_text(params: dict, wls: np.ndarray) -> str:
    axis_a_desc = params["axis_a_desc"]
    axis_b_desc = params["axis_b_desc"]
    ramp_rate = params["ramp_step"] / max(params["step_delay_s"], EPS)
    fixed = params.get("fixed", {})
    vbias_available = bool(params.get("vbias_available", True))
    smu_connected = bool(params.get("smu_connected", False))
    lf6_connected = bool(params.get("lf6_connected", False))

    lines = [
        "# MegaSweep Data File",
        f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "#",
        "# === Sweep Configuration ===",
        f"# CoordinateSystem: {'Raw' if params['coord'] == CoordSystem.RAW else 'Physical'}",
        f"# AxisA_Name: {params['axis_a']}",
        f"# AxisA_Start: {axis_a_desc['start']:.4f}",
        f"# AxisA_Stop: {axis_a_desc['stop']:.4f}",
        f"# AxisA_Step: {axis_a_desc['step']:.4f}",
        f"# AxisA_StepMode: {axis_a_desc['mode']}",
        f"# AxisA_Points: {axis_a_desc['points']}",
        f"# AxisB_Name: {params['axis_b']}",
        f"# AxisB_Start: {axis_b_desc['start']:.4f}",
        f"# AxisB_Stop: {axis_b_desc['stop']:.4f}",
        f"# AxisB_Step: {axis_b_desc['step']:.4f}",
        f"# AxisB_StepMode: {axis_b_desc['mode']}",
        f"# AxisB_Points: {axis_b_desc['points']}",
        f"# Snake: {params['snake']}",
        f"# TotalPlanned: {len(params['all_points'])}",
        f"# InBounds: {len(params['valid_points'])}",
        f"# Skipped: {len(params['all_points']) - len(params['valid_points'])}",
        "#",
        "# === Coordinate Transform ===",
        f"# GateRatio_r: {float(params['ratio']):.4f}",
        "# Doping_definition: D = Vtg + r*Vbg",
        "# Efield_definition: F = Vtg - r*Vbg",
        "#",
        "# === Fixed Parameters ===",
    ]
    if fixed:
        for key, value in fixed.items():
            value_txt = "N/A" if key == "Vbias" and not vbias_available else f"{float(value):.4f}"
            lines.append(f"# Fixed_{key}: {value_txt}")
    else:
        lines.append("# Fixed_None: N/A")
    lines.extend([
        "#",
        "# === Safety Limits ===",
        f"# SafetyLimit_Vtg_min: {params['safety']['vtg_min']:.4f}",
        f"# SafetyLimit_Vtg_max: {params['safety']['vtg_max']:.4f}",
        f"# SafetyLimit_Vbg_min: {params['safety']['vbg_min']:.4f}",
        f"# SafetyLimit_Vbg_max: {params['safety']['vbg_max']:.4f}",
        f"# SafetyLimit_Vbias_min: {params['safety']['vbias_min']:.4f}",
        f"# SafetyLimit_Vbias_max: {params['safety']['vbias_max']:.4f}",
        "#",
        "# === Timing ===",
        f"# SettleTime_s: {params['settle']:.4f}",
        f"# RampStep_V: {params['ramp_step']:.4f}",
        f"# StepDelay_s: {params['step_delay_s']:.4f}",
        f"# RampRate_Vs: {ramp_rate:.2f}",
        "#",
        "# === Optical ===",
        f"# Center_nm: {params['center_nm']:.2f}",
        f"# Exposure_ms: {params['exp_ms']}",
        f"# Frames: {params['frames']}",
        f"# Wavelength_start_nm: {float(wls[0]):.2f}",
        f"# Wavelength_end_nm: {float(wls[-1]):.2f}",
        f"# Wavelength_pixels: {int(wls.size)}",
        "#",
        "# === Hardware ===",
        f"# SMU_Connected: {smu_connected}",
        f"# Vbias_Available: {vbias_available}",
        f"# LF6_Connected: {lf6_connected}",
        "#",
        "# === Sample ===",
        f"# Sample: {params.get('sample', 'Sample')}",
        f"# Tag: {params.get('tag', '2DSweep')}",
        f"# Laser_nm: {params.get('laser_nm', 'N/A') or 'N/A'}",
        f"# Power_uW: {params.get('power_uw', 'N/A') or 'N/A'}",
        "#",
        "# === Columns ===",
        f"# {params['axis_a']}_axis_a_set: {params['axis_a']} axis setpoint",
        f"# {params['axis_b']}_axis_b_set: {params['axis_b']} axis setpoint",
        "# Vtg_set: Vtg hardware setpoint (V)",
        "# Vbg_set: Vbg hardware setpoint (V)",
        "# Vbias_set: Vbias hardware setpoint (V)",
        "# Doping_set: Doping D = Vtg + r*Vbg (V)",
        "# Efield_set: E-field F = Vtg - r*Vbg (V)",
        "# Vbg_meas: Vbg measured readback (V)",
        "# Vtg_meas: Vtg measured readback (V)",
        "# Vbias_meas: Vbias measured readback (V)",
        "# Ibg_A: Back-gate current (A)",
        "# Itg_A: Top-gate current (A)",
        "# Ibias_A: Bias current (A)",
        "# [wavelength columns follow: values in nm]",
    ])
    return "\n".join(lines) + "\n"


def build_sweep_points(
    coord: CoordSystem,
    axis_a: str,
    axis_a_vals: np.ndarray,
    axis_b: str,
    axis_b_vals: np.ndarray,
    fixed: dict,
    ratio: float,
    safety: dict,
    snake: bool,
) -> tuple[list[dict], list[dict]]:
    all_points: list[dict] = []
    valid_points: list[dict] = []
    for i, a_val in enumerate(np.asarray(axis_a_vals, dtype=float)):
        b_seq = axis_b_vals[::-1] if (snake and i % 2 == 1) else axis_b_vals
        for b_val in np.asarray(b_seq, dtype=float):
            axis_values = dict(fixed)
            axis_values[axis_a] = float(a_val)
            axis_values[axis_b] = float(b_val)
            if coord == CoordSystem.RAW:
                vtg = float(axis_values.get("Vtg", 0.0))
                vbg = float(axis_values.get("Vbg", 0.0))
                vbias = float(axis_values.get("Vbias", 0.0))
            else:
                D = float(axis_values.get("Doping", 0.0))
                F = float(axis_values.get("E-field", 0.0))
                vbias = float(axis_values.get("Vbias", 0.0))
                vtg, vbg = _physics_to_raw(D, F, ratio)
            point = {
                "axis_a": float(a_val),
                "axis_b": float(b_val),
                "axis_values": axis_values,
                "raw": (float(vtg), float(vbg), float(vbias)),
                "in_bounds": _is_in_bounds(vtg, vbg, vbias, safety),
            }
            all_points.append(point)
            if point["in_bounds"]:
                valid_points.append(point)
    return all_points, valid_points


class _CoordSystemWidget(QGroupBox):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__("Coordinate System  Gate Ratio", parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(3)
        self._group = QButtonGroup(self)
        self._raw = QRadioButton("Raw Voltages  — sweep Vtg / Vbg directly")
        self._physical = QRadioButton("Physical Coordinates  — sweep Doping / E-field")
        self._raw.setChecked(True)
        self._group.addButton(self._raw)
        self._group.addButton(self._physical)
        lay.addWidget(self._raw)
        lay.addWidget(self._physical)

        note = QLabel(
            "Raw: step sizes are actual Vtg/Vbg steps; second preview tab shows the resulting D/F map.\n"
            "Physical: step sizes are Doping/E-field steps; Vtg/Vbg positions are derived via ratio."
        )
        note.setStyleSheet("color: #6a6a6a; font-style: italic; font-size: 10px;")
        note.setWordWrap(True)
        lay.addWidget(note)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._ratio_label = QLabel("Gate ratio r")
        self._ratio_spin = QDoubleSpinBox()
        self._ratio_spin.setRange(-1000.0, 1000.0)
        self._ratio_spin.setDecimals(4)
        self._ratio_spin.setValue(1.0)
        self._ratio_spin.setToolTip(
            "Gate efficiency ratio r.\n"
            "Doping D = Vtg + r·Vbg\n"
            "E-field F = Vtg − r·Vbg\n"
            "Hardware: Vtg = (D+F)/2,  Vbg = (D−F)/(2r)\n\n"
            "Changing r changes the actual Vtg/Vbg positions swept."
        )
        row.addWidget(self._ratio_label)
        row.addWidget(self._ratio_spin, 1)
        lay.addLayout(row)

        self._formula = QLabel("D = Vtg + r·Vbg    F = Vtg − r·Vbg\nVtg = (D+F)/2    Vbg = (D−F)/(2r)")
        self._formula.setStyleSheet("color: #6a6a6a; font-size: 10px;")
        lay.addWidget(self._formula)

        self._raw.toggled.connect(self.changed)
        self._physical.toggled.connect(self.changed)
        self._ratio_spin.valueChanged.connect(self.changed)

    def coord_system(self) -> CoordSystem:
        return CoordSystem.PHYSICAL if self._physical.isChecked() else CoordSystem.RAW

    def ratio(self) -> float:
        return float(self._ratio_spin.value())

    def set_ratio_invalid(self, invalid: bool):
        self._ratio_spin.setStyleSheet("border: 1px solid red;" if invalid else "")


class _AxisSelectorWidget(QGroupBox):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__("Axis Selection", parent)
        vlay = QVBoxLayout(self)
        vlay.setContentsMargins(6, 4, 6, 4)
        vlay.setSpacing(3)
        self._available: list[str] = []
        self._outer = QComboBox()
        self._inner = QComboBox()
        self._snake = QCheckBox("Reverse inner direction on alternating outer steps")
        self._snake.setChecked(True)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("Outer (slow) axis"))
        row.addWidget(self._outer)
        row.addSpacing(16)
        row.addWidget(QLabel("Inner (fast) axis"))
        row.addWidget(self._inner)
        row.addStretch()
        vlay.addLayout(row)
        vlay.addWidget(self._snake)
        self._outer.currentTextChanged.connect(self._sync_inner)
        self._inner.currentTextChanged.connect(self._sync_outer)
        self._outer.currentTextChanged.connect(self.changed)
        self._inner.currentTextChanged.connect(self.changed)
        self._snake.toggled.connect(self.changed)

    def set_available_axes(self, axes: list[str]):
        prev_outer = self.outer()
        prev_inner = self.inner()
        self._available = list(axes)
        outer = prev_outer if prev_outer in axes else (axes[0] if axes else "")
        inner_choices = [a for a in axes if a != outer]
        inner = prev_inner if prev_inner in inner_choices else (inner_choices[0] if inner_choices else "")
        self._set_items(self._outer, axes, outer)
        self._set_items(self._inner, inner_choices, inner)
        self.changed.emit()

    def _set_items(self, combo: QComboBox, items: list[str], selected: str):
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(items)
        if selected in items:
            combo.setCurrentText(selected)
        combo.blockSignals(False)

    def _sync_inner(self):
        outer = self.outer()
        items = [a for a in self._available if a != outer]
        selected = self.inner() if self.inner() in items else (items[0] if items else "")
        self._set_items(self._inner, items, selected)

    def _sync_outer(self):
        inner = self.inner()
        items = [a for a in self._available if a != inner]
        selected = self.outer() if self.outer() in items else (items[0] if items else "")
        self._set_items(self._outer, items, selected)

    def outer(self) -> str:
        return self._outer.currentText()

    def inner(self) -> str:
        return self._inner.currentText()

    def snake(self) -> bool:
        return self._snake.isChecked()

    def available_axes(self) -> list[str]:
        return list(self._available)


class _AxisConfigWidget(QGroupBox):
    changed = Signal()

    def __init__(self, title: str, default_start: float, default_stop: float, parent=None):
        super().__init__(title, parent)
        self._axis_name = title
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(3)

        self._start = QDoubleSpinBox()
        self._stop = QDoubleSpinBox()
        self._step = QDoubleSpinBox()
        for spin in (self._start, self._stop):
            spin.setRange(-200.0, 200.0)
            spin.setDecimals(4)
        self._start.setValue(default_start)
        self._stop.setValue(default_stop)
        self._step.setRange(1e-9, 100000.0)
        self._step.setDecimals(4)
        self._step.setValue(1.0)
        self._mode = QComboBox()
        self._mode.addItems(["Step Size", "Total Points"])
        self._mode.setFixedWidth(100)

        range_row = QHBoxLayout()
        range_row.setSpacing(4)
        lbl_start = QLabel("Start")
        lbl_start.setFixedWidth(30)
        lbl_stop = QLabel("Stop")
        lbl_stop.setFixedWidth(28)
        range_row.addWidget(lbl_start)
        range_row.addWidget(self._start, 1)
        range_row.addWidget(lbl_stop)
        range_row.addWidget(self._stop, 1)
        lay.addLayout(range_row)

        step_row = QHBoxLayout()
        step_row.setSpacing(4)
        lbl_step = QLabel("Step")
        lbl_step.setFixedWidth(30)
        step_row.addWidget(lbl_step)
        step_row.addWidget(self._step, 1)
        step_row.addWidget(self._mode)
        lay.addLayout(step_row)

        self._start.valueChanged.connect(self.changed)
        self._stop.valueChanged.connect(self.changed)
        self._step.valueChanged.connect(self.changed)
        self._mode.currentTextChanged.connect(self._update_suffix)
        self._mode.currentTextChanged.connect(self.changed)
        self._update_suffix()

    def _update_suffix(self):
        if self._mode.currentText() == "Total Points":
            self._step.setDecimals(0)
            self._step.setSingleStep(1.0)
            self._step.setSuffix(" pts")
        else:
            self._step.setDecimals(4)
            self._step.setSingleStep(0.1)
            self._step.setSuffix(f" {AXIS_UNITS.get(self._axis_name, 'V')}/step")

    def set_axis_label(self, name: str, units: str):
        self._axis_name = name
        prefix = self.title().split(":")[0]
        self.setTitle(f"{prefix}: {name}")
        self._update_suffix()
        if name == "Vbias" and abs(self._stop.value() - 5.0) < EPS:
            self._stop.setValue(0.050)
        elif name != "Vbias" and abs(self._stop.value() - 0.050) < EPS:
            self._stop.setValue(5.0)

    def get_array(self) -> np.ndarray:
        return _get_linear_array(
            self._start.value(),
            self._stop.value(),
            self._step.value(),
            "Total Points" if self._mode.currentText() == "Total Points" else "Step Size",
        )

    def describe(self) -> dict:
        arr = self.get_array()
        return {
            "start": float(self._start.value()),
            "stop": float(self._stop.value()),
            "param": float(self._step.value()),
            "mode": self._mode.currentText(),
            "points": int(arr.size),
        }


class _FixedParamsWidget(QGroupBox):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__("Fixed Parameters", parent)
        self._layout = QFormLayout(self)
        self._layout.setContentsMargins(6, 4, 6, 4)
        self._layout.setSpacing(3)
        self._spins: dict[str, QDoubleSpinBox] = {}

    def set_fixed_axes(self, axes: list[str], vbias_available: bool):
        while self._layout.rowCount():
            self._layout.removeRow(0)
        self._spins.clear()
        display_axes = list(axes)
        if "Vbias" not in display_axes and not vbias_available:
            display_axes.append("Vbias")
        self.setVisible(bool(display_axes))
        for axis in display_axes:
            spin = QDoubleSpinBox()
            spin.setRange(-200.0, 200.0)
            spin.setDecimals(4)
            spin.setValue(0.0)
            if axis == "Vbias" and not vbias_available:
                spin.setEnabled(False)
                row = QWidget()
                row_lay = QHBoxLayout(row)
                row_lay.setContentsMargins(0, 0, 0, 0)
                badge = QLabel("SMU not connected")
                badge.setStyleSheet("color: #ad6700;")
                row_lay.addWidget(spin)
                row_lay.addWidget(badge)
                self._layout.addRow(axis, row)
            else:
                spin.valueChanged.connect(self.changed)
                self._layout.addRow(axis, spin)
            self._spins[axis] = spin

    def get_values(self) -> dict[str, float]:
        return {name: float(spin.value()) for name, spin in self._spins.items()}


class _SafetyWidget(QGroupBox):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__("Safety Limits", parent)
        form = QFormLayout(self)
        form.setContentsMargins(6, 4, 6, 4)
        form.setSpacing(3)
        self._spins: dict[str, QDoubleSpinBox] = {}
        defaults = {
            "vtg_min": -10.0, "vtg_max": 10.0,
            "vbg_min": -10.0, "vbg_max": 10.0,
            "vbias_min": -1.0, "vbias_max": 1.0,
        }
        for prefix, row_label in (("vtg", "Vtg"), ("vbg", "Vbg"), ("vbias", "Vbias")):
            container = QWidget()
            hlay = QHBoxLayout(container)
            hlay.setContentsMargins(0, 0, 0, 0)
            hlay.setSpacing(4)
            for suffix, short in (("min", "min"), ("max", "max")):
                key = f"{prefix}_{suffix}"
                spin = QDoubleSpinBox()
                spin.setRange(-200.0, 200.0)
                spin.setDecimals(4)
                spin.setValue(defaults[key])
                spin.setSuffix(" V")
                spin.valueChanged.connect(self.changed)
                self._spins[key] = spin
                lbl = QLabel(short)
                lbl.setFixedWidth(24)
                hlay.addWidget(lbl)
                hlay.addWidget(spin, 1)
            form.addRow(row_label, container)

    def set_vbias_available(self, available: bool):
        self._spins["vbias_min"].setEnabled(available)
        self._spins["vbias_max"].setEnabled(available)

    def get_limits(self) -> dict:
        return {name: float(spin.value()) for name, spin in self._spins.items()}


class _TimingWidget(QGroupBox):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__("Timing", parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(3)
        form = QFormLayout()
        form.setSpacing(3)
        lay.addLayout(form)
        self._settle = QDoubleSpinBox()
        self._settle.setRange(0.0, 60.0)
        self._settle.setDecimals(4)
        self._settle.setValue(cfg.ramp.settle_s)
        self._settle.setSuffix(" s")
        form.addRow("Settle time (s)", self._settle)
        self._toggle = QToolButton()
        self._toggle.setText("Advanced Timing")
        self._toggle.setCheckable(True)
        self._toggle.setChecked(False)
        self._toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._toggle.setArrowType(Qt.RightArrow)
        lay.addWidget(self._toggle)
        self._advanced = QFrame()
        self._advanced.setVisible(False)
        adv_form = QFormLayout(self._advanced)
        self._ramp_step = QDoubleSpinBox()
        self._ramp_step.setRange(0.001, 10.0)
        self._ramp_step.setDecimals(4)
        self._ramp_step.setValue(cfg.ramp.step_V)
        self._ramp_step.setSuffix(" V")
        self._step_delay_ms = QDoubleSpinBox()
        self._step_delay_ms.setRange(1.0, 10000.0)
        self._step_delay_ms.setDecimals(1)
        self._step_delay_ms.setValue(cfg.ramp.delay_s * 1000.0)
        self._step_delay_ms.setSuffix(" ms")
        self._rate = QLabel("")
        self._rate.setStyleSheet("color: #6a6a6a;")
        adv_form.addRow("Voltage increment (V)", self._ramp_step)
        adv_form.addRow("Step delay (ms)", self._step_delay_ms)
        adv_form.addRow("Ramp rate", self._rate)
        lay.addWidget(self._advanced)
        self._toggle.toggled.connect(self._on_toggle)
        self._settle.valueChanged.connect(self.changed)
        self._ramp_step.valueChanged.connect(self._update_rate)
        self._step_delay_ms.valueChanged.connect(self._update_rate)
        self._ramp_step.valueChanged.connect(self.changed)
        self._step_delay_ms.valueChanged.connect(self.changed)
        self._update_rate()

    def _on_toggle(self, checked: bool):
        self._advanced.setVisible(checked)
        self._toggle.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)

    def _update_rate(self):
        rate = self.ramp_step() / max(self.step_delay_s(), EPS)
        self._rate.setText(f"≈ {rate:.2f} V/s")

    def settle(self) -> float:
        return float(self._settle.value())

    def ramp_step(self) -> float:
        return float(self._ramp_step.value())

    def step_delay_s(self) -> float:
        return float(self._step_delay_ms.value()) / 1000.0


class _PreviewPlot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._tabs = QTabWidget()
        lay.addWidget(self._tabs)

        self._pw_axis = pg.PlotWidget()
        self._pw_axis.showGrid(x=True, y=True, alpha=0.25)
        self._tabs.addTab(self._pw_axis, "Axis-space")

        self._pw_v = pg.PlotWidget()
        self._pw_v.setLabel("bottom", "Vtg", units="V")
        self._pw_v.setLabel("left", "Vbg", units="V")
        self._pw_v.showGrid(x=True, y=True, alpha=0.25)
        self._tabs.addTab(self._pw_v, "Voltage-space (Vtg/Vbg)")

        self._axis_items: list = []
        self._voltage_items: list = []
        self._safety_polygon: Optional[QGraphicsPolygonItem] = None
        self._safety_rect: Optional[QGraphicsRectItem] = None
        self._valid_points: list[dict] = []
        self._all_points: list[dict] = []
        self._axis_a_vals = np.array([])
        self._axis_b_vals = np.array([])
        self._snake = True
        self._axis_stripes: dict[int, object] = {}
        self._voltage_stripes: dict[int, object] = {}
        self._ratio: float = 1.0
        self._coord: CoordSystem = CoordSystem.PHYSICAL

        self._planned_axis = pg.ScatterPlotItem(size=5, pen=pg.mkPen(None), brush=pg.mkBrush(90, 110, 130, 85))
        self._planned_v = pg.ScatterPlotItem(size=5, pen=pg.mkPen(None), brush=pg.mkBrush(90, 110, 130, 85))
        self._oob_axis = pg.ScatterPlotItem(size=5, symbol="x", pen=pg.mkPen(210, 70, 70, 100, width=0.8), brush=pg.mkBrush(None))
        self._oob_v = pg.ScatterPlotItem(size=5, symbol="x", pen=pg.mkPen(210, 70, 70, 100, width=0.8), brush=pg.mkBrush(None))
        self._completed_axis_pts = pg.ScatterPlotItem(size=6, pen=pg.mkPen("#27c2d9", width=0.8), brush=pg.mkBrush(39, 194, 217, 170))
        self._completed_v_pts = pg.ScatterPlotItem(size=6, pen=pg.mkPen("#27c2d9", width=0.8), brush=pg.mkBrush(39, 194, 217, 170))
        self._done_axis = pg.PlotDataItem(pen=pg.mkPen("#27c2d9", width=2))
        self._done_v = pg.PlotDataItem(pen=pg.mkPen("#27c2d9", width=2))
        self._current_axis_stripe = pg.PlotDataItem(pen=pg.mkPen("#2b7fff", width=2))
        self._current_v_stripe = pg.PlotDataItem(pen=pg.mkPen("#2b7fff", width=2))
        self._cur_axis = pg.ScatterPlotItem(size=14, pen=pg.mkPen("r", width=2), brush=pg.mkBrush(None))
        self._cur_v = pg.ScatterPlotItem(size=14, pen=pg.mkPen("r", width=2), brush=pg.mkBrush(None))
        self._pw_axis.addItem(self._planned_axis)
        self._pw_axis.addItem(self._oob_axis)
        self._pw_axis.addItem(self._completed_axis_pts)
        self._pw_axis.addItem(self._done_axis)
        self._pw_axis.addItem(self._current_axis_stripe)
        self._pw_axis.addItem(self._cur_axis)
        self._pw_v.addItem(self._planned_v)
        self._pw_v.addItem(self._oob_v)
        self._pw_v.addItem(self._completed_v_pts)
        self._pw_v.addItem(self._done_v)
        self._pw_v.addItem(self._current_v_stripe)
        self._pw_v.addItem(self._cur_v)

    def set_axis_labels(self, x_label: str, y_label: str):
        self._pw_axis.setLabel("bottom", x_label, units=AXIS_UNITS.get(x_label, "V"))
        self._pw_axis.setLabel("left", y_label, units=AXIS_UNITS.get(y_label, "V"))

    def clear(self):
        for item in self._axis_items:
            self._pw_axis.removeItem(item)
        for item in self._voltage_items:
            self._pw_v.removeItem(item)
        self._axis_items.clear()
        self._voltage_items.clear()
        if self._safety_polygon is not None:
            self._pw_axis.removeItem(self._safety_polygon)
            self._safety_polygon = None
        if self._safety_rect is not None:
            self._pw_v.removeItem(self._safety_rect)
            self._safety_rect = None
        self._axis_stripes.clear()
        self._voltage_stripes.clear()
        self.clear_progress()

    def draw_safety_polygon(self, corners: list[tuple[float, float]]):
        if self._safety_polygon is not None:
            self._pw_axis.removeItem(self._safety_polygon)
            self._safety_polygon = None
        if not corners:
            return
        points = [QPointF(x, y) for x, y in corners]
        polygon = QPolygonF(points + [points[0]])
        item = QGraphicsPolygonItem(polygon)
        item.setPen(pg.mkPen("r", width=1.2, style=Qt.DashLine))
        item.setBrush(pg.mkBrush(None))
        self._pw_axis.addItem(item)
        self._safety_polygon = item

    def _add_rect(self, plot_widget, item_list, x0: float, y0: float, x1: float, y1: float, pen):
        rect = QGraphicsRectItem(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
        rect.setPen(pen)
        rect.setBrush(pg.mkBrush(None))
        plot_widget.addItem(rect)
        item_list.append(rect)

    def _add_limit_lines(self, plot_widget, item_list, orientation: str, lo: float, hi: float, span_lo: float, span_hi: float):
        pen = pg.mkPen("r", width=1.2, style=Qt.DashLine)
        if orientation == "h":
            for y in (lo, hi):
                item = plot_widget.plot([span_lo, span_hi], [y, y], pen=pen)
                item_list.append(item)
        else:
            for x in (lo, hi):
                item = plot_widget.plot([x, x], [span_lo, span_hi], pen=pen)
                item_list.append(item)

    def _raw_to_df(self, vtg: float, vbg: float) -> tuple[float, float]:
        r = self._ratio
        return (vtg + r * vbg, vtg - r * vbg)

    def _set_point_layers(self, all_points: list[dict], valid_points: list[dict]):
        self._all_points = list(all_points)
        total_points = max(1, len(all_points))
        if total_points <= 400:
            point_size = 6
            alpha = 95
        elif total_points <= 2500:
            point_size = 4
            alpha = 75
        else:
            point_size = 3
            alpha = 55
        self._planned_axis.setSize(point_size)
        self._planned_v.setSize(point_size)
        self._planned_axis.setBrush(pg.mkBrush(90, 110, 130, alpha))
        self._planned_v.setBrush(pg.mkBrush(90, 110, 130, alpha))
        self._oob_axis.setSize(max(3, point_size))
        self._oob_v.setSize(max(3, point_size))
        axis_valid_x = [p["axis_a"] for p in valid_points]
        axis_valid_y = [p["axis_b"] for p in valid_points]
        raw_valid_x = [p["raw"][0] for p in valid_points]
        raw_valid_y = [p["raw"][1] for p in valid_points]
        self._planned_axis.setData(x=axis_valid_x, y=axis_valid_y)
        if self._coord == CoordSystem.RAW:
            df_pairs = [self._raw_to_df(vtg, vbg) for vtg, vbg in zip(raw_valid_x, raw_valid_y)]
            self._planned_v.setData(x=[d for d, _ in df_pairs], y=[f for _, f in df_pairs])
        else:
            self._planned_v.setData(x=raw_valid_x, y=raw_valid_y)

        show_oob = total_points <= 2500
        if show_oob:
            oob_points = [p for p in all_points if not p["in_bounds"]]
            self._oob_axis.setData(x=[p["axis_a"] for p in oob_points], y=[p["axis_b"] for p in oob_points])
            if self._coord == CoordSystem.RAW:
                oob_df = [self._raw_to_df(p["raw"][0], p["raw"][1]) for p in oob_points]
                self._oob_v.setData(x=[d for d, _ in oob_df], y=[f for _, f in oob_df])
            else:
                self._oob_v.setData(x=[p["raw"][0] for p in oob_points], y=[p["raw"][1] for p in oob_points])
        else:
            self._oob_axis.setData([], [])
            self._oob_v.setData([], [])

    def _stripe_raw_endpoints(self, outer_index: int, axis_a_name: str, axis_b_name: str, axis_b_vals: np.ndarray, all_points: list[dict]) -> tuple[tuple[float, float], tuple[float, float], bool]:
        reversed_stripe = self._snake and (outer_index % 2 == 1)
        start_b = float(axis_b_vals[-1] if reversed_stripe else axis_b_vals[0])
        end_b = float(axis_b_vals[0] if reversed_stripe else axis_b_vals[-1])
        point_map = {}
        for point in all_points:
            if abs(point["axis_a"] - self._axis_a_vals[outer_index]) < EPS:
                point_map[round(point["axis_b"], 12)] = point
        p0 = point_map.get(round(start_b, 12))
        p1 = point_map.get(round(end_b, 12))
        if p0 is None or p1 is None:
            a = float(self._axis_a_vals[outer_index])
            if axis_a_name == "Vtg":
                vtg0 = vtg1 = a
            elif axis_a_name == "Vbg":
                vtg0 = float(self._axis_b_vals[0]) if axis_b_name == "Vtg" else 0.0
                vtg1 = float(self._axis_b_vals[-1]) if axis_b_name == "Vtg" else 0.0
            else:
                vtg0 = vtg1 = 0.0
            return (vtg0, 0.0), (vtg1, 0.0), reversed_stripe
        return (p0["raw"][0], p0["raw"][1]), (p1["raw"][0], p1["raw"][1]), reversed_stripe

    def _build_stripe_schematic(
        self,
        axis_a_vals: np.ndarray,
        axis_b_vals: np.ndarray,
        snake: bool,
        all_points: list[dict],
        valid_points: list[dict],
        safety: dict,
        plot_widget,
        item_list,
        stripe_store: dict[int, object],
        coord_mode: str,
        axis_a_name: str,
        axis_b_name: str,
        draw_polygon: list[tuple[float, float]] | None = None,
    ):
        m = len(axis_a_vals)
        if m == 0 or len(axis_b_vals) == 0:
            return
        shown = list(range(m)) if m <= 4 else [0, 1, 2, m - 1]
        extent_pen = pg.mkPen("#4aa8d8", width=1, style=Qt.DashLine)

        if coord_mode == "axis":
            self._add_rect(plot_widget, item_list, float(np.min(axis_a_vals)), float(np.min(axis_b_vals)), float(np.max(axis_a_vals)), float(np.max(axis_b_vals)), extent_pen)
            if draw_polygon:
                self.draw_safety_polygon(draw_polygon)
            elif axis_a_name in ("Vtg", "Vbg") and axis_b_name in ("Vtg", "Vbg"):
                if {axis_a_name, axis_b_name} == {"Vtg", "Vbg"}:
                    x0, x1 = safety["vtg_min"], safety["vtg_max"]
                    y0, y1 = safety["vbg_min"], safety["vbg_max"]
                    if axis_a_name == "Vbg":
                        x0, x1, y0, y1 = y0, y1, x0, x1
                    self._add_rect(plot_widget, item_list, x0, y0, x1, y1, pg.mkPen("r", width=1.2, style=Qt.DashLine))
            else:
                if axis_a_name == "Vbias":
                    self._add_limit_lines(plot_widget, item_list, "v", safety["vbias_min"], safety["vbias_max"], float(np.min(axis_b_vals)), float(np.max(axis_b_vals)))
                elif axis_a_name == "Vtg":
                    self._add_limit_lines(plot_widget, item_list, "v", safety["vtg_min"], safety["vtg_max"], float(np.min(axis_b_vals)), float(np.max(axis_b_vals)))
                elif axis_a_name == "Vbg":
                    self._add_limit_lines(plot_widget, item_list, "v", safety["vbg_min"], safety["vbg_max"], float(np.min(axis_b_vals)), float(np.max(axis_b_vals)))
                if axis_b_name == "Vbias":
                    self._add_limit_lines(plot_widget, item_list, "h", safety["vbias_min"], safety["vbias_max"], float(np.min(axis_a_vals)), float(np.max(axis_a_vals)))
                elif axis_b_name == "Vtg":
                    self._add_limit_lines(plot_widget, item_list, "h", safety["vtg_min"], safety["vtg_max"], float(np.min(axis_a_vals)), float(np.max(axis_a_vals)))
                elif axis_b_name == "Vbg":
                    self._add_limit_lines(plot_widget, item_list, "h", safety["vbg_min"], safety["vbg_max"], float(np.min(axis_a_vals)), float(np.max(axis_a_vals)))
        elif coord_mode == "df":
            # Second tab in raw mode: show D/F derived space.
            # Extent from actual D/F ranges of all points.
            if all_points:
                r = self._ratio
                d_vals = [p["raw"][0] + r * p["raw"][1] for p in all_points]
                f_vals = [p["raw"][0] - r * p["raw"][1] for p in all_points]
                self._add_rect(plot_widget, item_list, min(d_vals), min(f_vals), max(d_vals), max(f_vals), extent_pen)
            # Safety polygon in D/F space (Vtg/Vbg limits mapped via ratio).
            if draw_polygon:
                pts = [QPointF(x, y) for x, y in draw_polygon]
                poly = QPolygonF(pts + [pts[0]])
                poly_item = QGraphicsPolygonItem(poly)
                poly_item.setPen(pg.mkPen("r", width=1.2, style=Qt.DashLine))
                poly_item.setBrush(pg.mkBrush(None))
                plot_widget.addItem(poly_item)
                item_list.append(poly_item)
        else:  # "voltage" — physical mode second tab (Vtg/Vbg)
            self._add_rect(plot_widget, item_list, float(np.min(axis_a_vals)), float(np.min(axis_b_vals)), float(np.max(axis_a_vals)), float(np.max(axis_b_vals)), extent_pen)
            rect = QGraphicsRectItem(safety["vtg_min"], safety["vbg_min"], safety["vtg_max"] - safety["vtg_min"], safety["vbg_max"] - safety["vbg_min"])
            rect.setPen(pg.mkPen("r", width=1.2, style=Qt.DashLine))
            rect.setBrush(pg.mkBrush(None))
            plot_widget.addItem(rect)
            item_list.append(rect)

        for outer_index in shown:
            a_val = float(axis_a_vals[outer_index])
            reversed_stripe = snake and (outer_index % 2 == 1)
            start_b = float(axis_b_vals[-1] if reversed_stripe else axis_b_vals[0])
            end_b = float(axis_b_vals[0] if reversed_stripe else axis_b_vals[-1])
            if coord_mode == "axis":
                start = (a_val, start_b)
                end = (a_val, end_b)
                stripe_points = [p for p in all_points if abs(p["axis_a"] - a_val) < EPS]
            elif coord_mode == "df":
                # Stripes in D/F space: transform raw endpoints.
                start_raw, end_raw, _ = self._stripe_raw_endpoints(outer_index, axis_a_name, axis_b_name, axis_b_vals, all_points)
                r = self._ratio
                start = (start_raw[0] + r * start_raw[1], start_raw[0] - r * start_raw[1])
                end = (end_raw[0] + r * end_raw[1], end_raw[0] - r * end_raw[1])
                stripe_points = [p for p in all_points if abs(p["axis_a"] - a_val) < EPS]
            else:
                start, end, _ = self._stripe_raw_endpoints(outer_index, axis_a_name, axis_b_name, axis_b_vals, all_points)
                stripe_points = [p for p in all_points if abs(p["axis_a"] - a_val) < EPS]
            valid_count = sum(1 for p in stripe_points if p["in_bounds"])
            if valid_count == 0:
                pen = pg.mkPen("#cc3b3b", width=1.5, style=Qt.DashLine)
            elif valid_count < len(stripe_points):
                pen = pg.mkPen("#b7b7b7", width=1.5, style=Qt.DashLine)
            else:
                pen = pg.mkPen("#6f6f6f", width=1.5)
            stripe = plot_widget.plot([start[0], end[0]], [start[1], end[1]], pen=pen)
            item_list.append(stripe)
            stripe_store[outer_index] = stripe
            # Arrow direction for axis/voltage: based on inner axis sweep direction.
            # For df mode: based on D/F end coordinates.
            angle = 90 if end[1] >= start[1] else 270
            arrow = pg.ArrowItem(pos=(end[0], end[1]), angle=angle, headLen=14, tipAngle=28, baseAngle=22, brush=pg.mkBrush("#7a7a7a"), pen=pg.mkPen("#7a7a7a"))
            plot_widget.addItem(arrow)
            item_list.append(arrow)

        if m > 4:
            gap_x = float((axis_a_vals[2] + axis_a_vals[-1]) / 2.0)
            gap_y = float((axis_b_vals[0] + axis_b_vals[-1]) / 2.0)
            if coord_mode == "df" and all_points:
                r = self._ratio
                gap_x = (axis_a_vals[2] + r * float(np.mean(axis_b_vals)) + axis_a_vals[-1] + r * float(np.mean(axis_b_vals))) / 2.0
                gap_y = (axis_a_vals[2] - r * float(np.mean(axis_b_vals)) + axis_a_vals[-1] - r * float(np.mean(axis_b_vals))) / 2.0
            dots = pg.TextItem("· · ·", color="#6f6f6f", anchor=(0.5, 0.5))
            dots.setPos(gap_x, gap_y)
            plot_widget.addItem(dots)
            item_list.append(dots)

        start_axis = (float(axis_a_vals[0]), float(axis_b_vals[0]))
        end_axis = (float(axis_a_vals[-1]), float(axis_b_vals[0] if (snake and ((m - 1) % 2 == 1)) else axis_b_vals[-1]))
        start_raw_pt = all_points[0]["raw"][:2] if all_points else (0.0, 0.0)
        end_raw_pt = all_points[-1]["raw"][:2] if all_points else (0.0, 0.0)
        if coord_mode == "axis":
            start_xy = start_axis
            end_xy = end_axis
        elif coord_mode == "df":
            r = self._ratio
            start_xy = (start_raw_pt[0] + r * start_raw_pt[1], start_raw_pt[0] - r * start_raw_pt[1])
            end_xy = (end_raw_pt[0] + r * end_raw_pt[1], end_raw_pt[0] - r * end_raw_pt[1])
        else:
            start_xy = start_raw_pt
            end_xy = end_raw_pt
        start_marker = pg.ScatterPlotItem(x=[start_xy[0]], y=[start_xy[1]], symbol="o", size=16, pen=pg.mkPen("#1f9d55", width=1.5), brush=pg.mkBrush("#1f9d55"))
        end_marker = pg.ScatterPlotItem(x=[end_xy[0]], y=[end_xy[1]], symbol="s", size=16, pen=pg.mkPen("#d92d20", width=1.5), brush=pg.mkBrush("#d92d20"))
        plot_widget.addItem(start_marker)
        plot_widget.addItem(end_marker)
        item_list.extend([start_marker, end_marker])

    def update_plan(
        self,
        all_points: list[dict],
        valid_points: list[dict],
        safety: dict,
        axis_a_name: str,
        axis_b_name: str,
        axis_a_vals: np.ndarray,
        axis_b_vals: np.ndarray,
        snake: bool,
        draw_polygon: list[tuple[float, float]] | None = None,
        coord: CoordSystem = CoordSystem.PHYSICAL,
        ratio: float = 1.0,
        df_polygon: list[tuple[float, float]] | None = None,
    ):
        self._coord = coord
        self._ratio = ratio
        self.clear()
        self._valid_points = list(valid_points)
        self._set_point_layers(all_points, valid_points)
        self._axis_a_vals = np.asarray(axis_a_vals, dtype=float)
        self._axis_b_vals = np.asarray(axis_b_vals, dtype=float)
        self._snake = bool(snake)
        self.set_axis_labels(axis_a_name, axis_b_name)

        if coord == CoordSystem.RAW:
            self._tabs.setTabText(0, "Raw (Vtg/Vbg)")
            self._tabs.setTabText(1, "Derived (Doping / E-field)")
            self._pw_v.setLabel("bottom", "Doping  D = Vtg + r·Vbg", units="V")
            self._pw_v.setLabel("left", "E-field  F = Vtg − r·Vbg", units="V")
            v_coord_mode = "df"
            v_draw = df_polygon
        else:
            self._tabs.setTabText(0, "Physical (D/F)")
            self._tabs.setTabText(1, "Raw (Vtg/Vbg)")
            self._pw_v.setLabel("bottom", "Vtg", units="V")
            self._pw_v.setLabel("left", "Vbg", units="V")
            v_coord_mode = "voltage"
            v_draw = None

        self._build_stripe_schematic(self._axis_a_vals, self._axis_b_vals, snake, all_points, valid_points, safety, self._pw_axis, self._axis_items, self._axis_stripes, "axis", axis_a_name, axis_b_name, draw_polygon)
        self._build_stripe_schematic(self._axis_a_vals, self._axis_b_vals, snake, all_points, valid_points, safety, self._pw_v, self._voltage_items, self._voltage_stripes, v_coord_mode, axis_a_name, axis_b_name, v_draw)

        self._pw_axis.autoRange()
        self._pw_v.autoRange()

    def update_progress(self, done: int, inner_count: int):
        if done <= 0 or not self._valid_points:
            self.clear_progress()
            return
        upto = min(done, len(self._valid_points))
        pts = self._valid_points[:upto]
        cur = pts[-1]
        def _v_coords(p: dict) -> tuple[float, float]:
            vtg, vbg = p["raw"][0], p["raw"][1]
            if self._coord == CoordSystem.RAW:
                return self._raw_to_df(vtg, vbg)
            return vtg, vbg

        self._completed_axis_pts.setData(x=[p["axis_a"] for p in pts], y=[p["axis_b"] for p in pts])
        self._completed_v_pts.setData(x=[_v_coords(p)[0] for p in pts], y=[_v_coords(p)[1] for p in pts])
        inner_count = max(1, inner_count)
        current_stripe = min(len(self._axis_a_vals) - 1, max(0, (done - 1) // inner_count)) if len(self._axis_a_vals) else 0
        completed = [float(self._axis_a_vals[i]) for i in range(current_stripe) if i < len(self._axis_a_vals)]
        if completed:
            axis_x, axis_y = [], []
            v_x, v_y = [], []
            for idx in range(current_stripe):
                a_val = float(self._axis_a_vals[idx])
                stripe_pts = [p for p in self._valid_points if abs(p["axis_a"] - a_val) < EPS]
                if not stripe_pts:
                    continue
                axis_x.extend([stripe_pts[0]["axis_a"], stripe_pts[-1]["axis_a"], np.nan])
                axis_y.extend([stripe_pts[0]["axis_b"], stripe_pts[-1]["axis_b"], np.nan])
                v0 = _v_coords(stripe_pts[0])
                v1 = _v_coords(stripe_pts[-1])
                v_x.extend([v0[0], v1[0], np.nan])
                v_y.extend([v0[1], v1[1], np.nan])
            self._done_axis.setData(x=axis_x, y=axis_y)
            self._done_v.setData(x=v_x, y=v_y)
        else:
            self._done_axis.setData([], [])
            self._done_v.setData([], [])
        current_pts = [p for p in self._valid_points if abs(p["axis_a"] - cur["axis_a"]) < EPS]
        if current_pts:
            self._current_axis_stripe.setData(x=[current_pts[0]["axis_a"], current_pts[-1]["axis_a"]], y=[current_pts[0]["axis_b"], current_pts[-1]["axis_b"]])
            cv0 = _v_coords(current_pts[0])
            cv1 = _v_coords(current_pts[-1])
            self._current_v_stripe.setData(x=[cv0[0], cv1[0]], y=[cv0[1], cv1[1]])
        else:
            self._current_axis_stripe.setData([], [])
            self._current_v_stripe.setData([], [])
        cv_cur = _v_coords(cur)
        self._cur_axis.setData(x=[cur["axis_a"]], y=[cur["axis_b"]])
        self._cur_v.setData(x=[cv_cur[0]], y=[cv_cur[1]])

    def clear_progress(self):
        self._completed_axis_pts.setData([], [])
        self._completed_v_pts.setData([], [])
        self._done_axis.setData([], [])
        self._done_v.setData([], [])
        self._current_axis_stripe.setData([], [])
        self._current_v_stripe.setData([], [])
        self._cur_axis.setData([], [])
        self._cur_v.setData([], [])


class _MegaSweepWorker(QObject):
    log = Signal(str)
    progress = Signal(int, int)
    point_done = Signal(int)
    finished = Signal()
    error = Signal(str)

    def __init__(self, params: dict, smu_ctrl=None, lf6_ctrl=None):
        super().__init__()
        self._p = params
        self._smu = smu_ctrl
        self._lf6 = lf6_ctrl
        self._stop = threading.Event()
        self._stopping = False

    def request_stop(self):
        if self._stopping:
            return
        self._stopping = True
        self._stop.set()
        self._emit_log("Stop requested - ramping to 0 V after the current step.")

    def _ts(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _emit_log(self, msg: str):
        self.log.emit(f"[{self._ts()}] {msg}")

    @Slot()
    def run(self):
        try:
            self._run_sweep(self._p)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()

    def _run_sweep(self, p: dict):
        iv = self._smu.device if self._smu and self._smu.is_connected else None
        spec = self._lf6.adapter if self._lf6 and self._lf6.is_connected else None
        lf6 = self._lf6.setup if self._lf6 and self._lf6.is_connected else None
        out_path: Path = p["out_path"]
        out_path.mkdir(parents=True, exist_ok=True)
        if lf6 is not None:
            try:
                if hasattr(lf6, "change_spectra_center"):
                    lf6.change_spectra_center(f"{p['center_nm']:.0f}")
                    time.sleep(0.15)
                if hasattr(lf6, "change_expose_time"):
                    lf6.change_expose_time(float(p["exp_ms"]))
                    time.sleep(0.10)
                for fn in ("set_accumulations", "set_frames", "change_frame_to_combine"):
                    if hasattr(lf6, fn):
                        getattr(lf6, fn)(int(p["frames"]))
                        break
            except Exception as exc:
                self._emit_log(f"LF6 settings warning: {exc}")

        self._emit_log("Acquiring wavelength calibration...")
        wls = _get_wavelengths(spec, lf6, float(p["center_nm"]), tol_nm=1.0)
        if wls.size <= 2:
            raise RuntimeError("Could not obtain wavelength calibration. Aborting.")

        fp = out_path / f"{p['base_name']}.csv"
        k = 2
        while fp.exists():
            fp = out_path / f"{p['base_name']}_{k:03d}.csv"
            k += 1

        points = p["valid_points"]
        if not points:
            raise RuntimeError("No sweep points within safety limits.")

        cols = [
            f"{p['axis_a']}_axis_a_set", f"{p['axis_b']}_axis_b_set",
            "Vtg_set", "Vbg_set", "Vbias_set", "Doping_set", "Efield_set",
            "Vbg_meas", "Vtg_meas", "Vbias_meas", "Ibg_A", "Itg_A", "Ibias_A",
        ]
        wl_str = np.array([f"{x:g}" for x in wls], dtype="U")
        header = np.concatenate((np.array(cols, dtype="U"), wl_str)).reshape(1, -1)
        meta_fp = fp.with_suffix(".meta.txt")
        with open(meta_fp, "w", encoding="utf-8", newline="") as meta_fh:
            meta_fh.write(_build_csv_metadata_text(p, wls))
        with open(fp, "w", newline="") as fh:
            np.savetxt(fh, header, fmt="%s", delimiter=",")
        self._emit_log(f"Writing data to {fp}")
        self._emit_log(f"Writing metadata to {meta_fp.name}")

        try:
            if iv is not None:
                vtg0, vbg0, vb0 = points[0]["raw"]
                iv.set_gates(
                    Vtg=vtg0,
                    Vbg=vbg0,
                    delay_s=p["step_delay_s"],
                    ramp_step=p["ramp_step"],
                    stop_cb=self._stop.is_set,
                    stop_exc=_MegaSweepStopRequested,
                )
                if hasattr(iv, "set_bias"):
                    iv.set_bias(
                        Vbias=vb0,
                        delay_s=p["step_delay_s"],
                        ramp_step=p["ramp_step"],
                        stop_cb=self._stop.is_set,
                        stop_exc=_MegaSweepStopRequested,
                    )
                time.sleep(p["settle"])

            total = len(points)
            prev = None
            with open(fp, "a", newline="") as fh:
                for done, point in enumerate(points, start=1):
                    if self._stop.is_set():
                        raise _MegaSweepStopRequested()
                    vtg, vbg, vbias = point["raw"]
                    if iv is not None:
                        if prev is None or abs(prev[0] - vtg) > EPS or abs(prev[1] - vbg) > EPS:
                            iv.set_gates(
                                Vtg=vtg,
                                Vbg=vbg,
                                delay_s=p["step_delay_s"],
                                ramp_step=p["ramp_step"],
                                stop_cb=self._stop.is_set,
                                stop_exc=_MegaSweepStopRequested,
                            )
                        if hasattr(iv, "set_bias") and (prev is None or abs(prev[2] - vbias) > EPS):
                            iv.set_bias(
                                Vbias=vbias,
                                delay_s=p["step_delay_s"],
                                ramp_step=p["ramp_step"],
                                stop_cb=self._stop.is_set,
                                stop_exc=_MegaSweepStopRequested,
                            )
                        prev = point["raw"]
                        time.sleep(p["settle"])

                    vbg_m, vtg_m = _read_gates(iv)
                    vbias_m = _read_bias(iv)
                    Ibg, Itg, Ib = _read_currents(iv)
                    y = _read_intensity(spec, int(wls.size)) if spec is not None else np.full(wls.size, NAN, dtype=float)
                    axis_vals = point["axis_values"]
                    prefix = np.array([
                        point["axis_a"], point["axis_b"],
                        vtg, vbg, vbias,
                        axis_vals.get("Doping", NAN), axis_vals.get("E-field", NAN),
                        vbg_m, vtg_m, vbias_m, Ibg, Itg, Ib,
                    ], dtype=np.float64)
                    row = np.concatenate((prefix, y)).reshape(1, -1)
                    np.savetxt(fh, row, fmt="%.6e", delimiter=",")
                    fh.flush()
                    self.progress.emit(done, total)
                    self.point_done.emit(done)
                    self._emit_log(
                        f"{done}/{total}: {p['axis_a']}={point['axis_a']:.4f}, {p['axis_b']}={point['axis_b']:.4f} | "
                        f"raw Vtg={vtg:.4f}, Vbg={vbg:.4f}, Vb={vbias:.4f} | "
                        f"Itg={_fmt_uA(Itg)} uA, Ibg={_fmt_uA(Ibg)} uA, Ib={_fmt_uA(Ib)} uA"
                    )
        except _MegaSweepStopRequested:
            self._emit_log("Stopped by user.")
        finally:
            self._safe_ramp_to_zero(iv)

        if self._stop.is_set():
            self._emit_log(f"Stopped. Partial data saved -> {fp.name}")
        else:
            self._emit_log(f"Done. Saved -> {fp.name}")

    def _safe_ramp_to_zero(self, iv):
        if iv is None:
            return
        try:
            self._emit_log("Ramping all connected channels to 0 V...")
            iv.ramp_all_to_zero(
                ramp_step=_SAFETY_RAMP_STEP_V,
                delay_s=_SAFETY_RAMP_DELAY_S,
            )
            self._emit_log("All connected channels are at 0 V.")
        except Exception as exc:
            self._emit_log(f"Return-to-zero failed: {exc}")


class MegaSweepPanel(QWidget):
    def __init__(self, smu_ctrl=None, lf6_ctrl=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._smu = smu_ctrl
        self._lf6 = lf6_ctrl
        self._worker: Optional[_MegaSweepWorker] = None
        self._thread: Optional[QThread] = None
        self._run_inner_count = 1
        self._last_preview: dict = {"all_points": [], "valid_points": []}
        self._run_failed = False
        self._step_linking = False
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(80)
        self._preview_timer.timeout.connect(self._refresh_preview)
        self._build()
        self._wire()
        self._sync_vbias_availability()
        self._update_raw_link_label()
        self._schedule_preview()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, stretch=1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(380)
        left = QWidget()
        self._left_lay = QVBoxLayout(left)
        self._left_lay.setContentsMargins(4, 4, 4, 4)
        self._left_lay.setSpacing(3)
        scroll.setWidget(left)
        splitter.addWidget(scroll)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(6, 4, 4, 4)
        right_lay.setSpacing(6)
        splitter.addWidget(right)
        splitter.setSizes([380, 620])

        self._coord_widget = _CoordSystemWidget()
        self._axis_selector = _AxisSelectorWidget()
        self._axis_a = _AxisConfigWidget("Outer Axis", -5.0, 5.0)
        self._axis_b = _AxisConfigWidget("Inner Axis", -5.0, 5.0)
        self._fixed_widget = _FixedParamsWidget()
        self._safety_widget = _SafetyWidget()
        self._timing_widget = _TimingWidget()

        optical = QGroupBox("Optical Settings")
        optical_form = QFormLayout(optical)
        optical_form.setContentsMargins(6, 4, 6, 4)
        optical_form.setSpacing(3)
        self._exp_spin = QDoubleSpinBox()
        self._exp_spin.setRange(1, 600000)
        self._exp_spin.setDecimals(1)
        self._exp_spin.setValue(cfg.lf6.exposure_ms)
        self._exp_spin.setSuffix(" ms")
        self._center_spin = QDoubleSpinBox()
        self._center_spin.setRange(200, 2000)
        self._center_spin.setDecimals(2)
        self._center_spin.setValue(cfg.lf6.center_nm)
        self._center_spin.setSuffix(" nm")
        self._frames_spin = QSpinBox()
        self._frames_spin.setRange(1, 1000)
        self._frames_spin.setValue(cfg.lf6.accumulations)
        optical_form.addRow("Exposure", self._exp_spin)
        optical_form.addRow("Center λ", self._center_spin)
        optical_form.addRow("Frames", self._frames_spin)

        meta = QGroupBox("File / Metadata")
        meta_form = QFormLayout(meta)
        meta_form.setContentsMargins(6, 4, 6, 4)
        meta_form.setSpacing(3)
        self._sample_edit = QLineEdit()
        self._sample_edit.setPlaceholderText("Sample")
        self._tag_edit = QLineEdit("2DSweep")
        self._laser_edit = QLineEdit("730")
        self._power_edit = QLineEdit("1")
        meta_form.addRow("Sample", self._sample_edit)
        meta_form.addRow("Tag", self._tag_edit)
        meta_form.addRow("Laser (nm)", self._laser_edit)
        meta_form.addRow("Power (µW)", self._power_edit)

        for widget in (self._coord_widget, self._axis_selector, self._axis_a, self._axis_b):
            self._left_lay.addWidget(widget)

        self._raw_link_label = QLabel()
        self._raw_link_label.setStyleSheet("color: #6a6a6a; font-style: italic; font-size: 10px; padding: 0px 6px 1px 6px;")
        self._raw_link_label.setWordWrap(True)
        self._left_lay.addWidget(self._raw_link_label)

        for widget in (self._fixed_widget, self._safety_widget, self._timing_widget, optical, meta):
            self._left_lay.addWidget(widget)
        self._left_lay.addStretch(1)

        self._preview = _PreviewPlot()
        right_lay.addWidget(self._preview, stretch=1)
        self._summary = QLabel("")
        font = QFont("Consolas", 9)
        font.setStyleHint(QFont.Monospace)
        self._summary.setFont(font)
        self._summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._summary.setStyleSheet(
            "color: #444444; background: #f5f5f7; border: 1px solid #dedede;"
            " border-radius: 3px; padding: 4px 6px;"
        )
        right_lay.addWidget(self._summary)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self._run_btn = QPushButton("▶  Run Sweep")
        self._run_btn.setMinimumHeight(32)
        self._run_btn.setMinimumWidth(120)
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
        controls.addWidget(self._run_btn)
        controls.addWidget(self._stop_btn)
        controls.addWidget(self._progress, stretch=1)
        controls.addWidget(self._status_lbl)
        right_lay.addLayout(controls)

        self._log_edit = QTextEdit()
        self._log_edit.setReadOnly(True)
        self._log_edit.setMaximumHeight(160)
        self._log_edit.setMinimumHeight(70)
        self._log_edit.setStyleSheet(
            "QTextEdit { font-family: 'Consolas', 'Courier New', monospace;"
            " font-size: 11px; background: #fafafa; border: 1px solid #d0d0d0;"
            " border-radius: 3px; }"
        )
        right_lay.addWidget(self._log_edit)

    def _wire(self):
        self._coord_widget.changed.connect(self._on_coord_system_changed)
        self._axis_selector.changed.connect(self._on_axis_selection_changed)
        self._axis_a.changed.connect(self._schedule_preview)
        self._axis_b.changed.connect(self._schedule_preview)
        self._axis_a._step.valueChanged.connect(lambda v: self._on_raw_step_changed("a", v))
        self._axis_b._step.valueChanged.connect(lambda v: self._on_raw_step_changed("b", v))
        self._coord_widget._ratio_spin.valueChanged.connect(self._on_ratio_changed_for_step_link)
        self._fixed_widget.changed.connect(self._schedule_preview)
        self._safety_widget.changed.connect(self._schedule_preview)
        self._timing_widget.changed.connect(self._schedule_preview)
        for w in (
            self._exp_spin, self._center_spin, self._frames_spin,
        ):
            w.valueChanged.connect(self._schedule_preview)
        for w in (self._sample_edit, self._tag_edit, self._laser_edit, self._power_edit):
            w.textChanged.connect(self._schedule_preview)
        self._run_btn.clicked.connect(self._on_run)
        self._stop_btn.clicked.connect(self._on_stop)
        if self._smu is not None:
            self._smu.connected.connect(lambda *_: self._sync_vbias_availability())
            self._smu.disconnected.connect(self._sync_vbias_availability)

    def _schedule_preview(self):
        self._preview_timer.start()

    def _vbias_available(self) -> bool:
        return bool(self._smu is not None and self._smu.is_connected and getattr(self._smu, "has_vbias", True))

    def _current_axis_pool(self) -> list[str]:
        axes = list(RAW_AXIS_ORDER if self._coord_widget.coord_system() == CoordSystem.RAW else PHYS_AXIS_ORDER)
        if not self._vbias_available() and "Vbias" in axes:
            axes.remove("Vbias")
        return axes

    @Slot()
    def _sync_vbias_availability(self):
        self._safety_widget.set_vbias_available(self._vbias_available())
        self._axis_selector.set_available_axes(self._current_axis_pool())
        self._on_axis_selection_changed()

    @Slot()
    def _on_coord_system_changed(self):
        self._sync_vbias_availability()
        self._update_ratio_validation()
        self._update_raw_link_label()
        self._schedule_preview()

    @Slot()
    def _on_axis_selection_changed(self):
        axes = self._axis_selector.available_axes()
        outer = self._axis_selector.outer()
        inner = self._axis_selector.inner()
        remaining = [axis for axis in axes if axis not in (outer, inner)]
        if not self._vbias_available() and "Vbias" not in remaining:
            remaining.append("Vbias")
        self._fixed_widget.set_fixed_axes(remaining, self._vbias_available())
        if outer:
            self._axis_a.set_axis_label(outer, AXIS_UNITS.get(outer, "V"))
        if inner:
            self._axis_b.set_axis_label(inner, AXIS_UNITS.get(inner, "V"))
        self._update_raw_link_label()
        self._schedule_preview()

    def _is_raw_vtg_vbg_mode(self) -> bool:
        if self._coord_widget.coord_system() != CoordSystem.RAW:
            return False
        return {self._axis_selector.outer(), self._axis_selector.inner()} == {"Vtg", "Vbg"}

    def _on_raw_step_changed(self, source: str, value: float):
        if self._step_linking or not self._is_raw_vtg_vbg_mode():
            return
        r = self._coord_widget.ratio()
        if abs(r) < EPS:
            return
        if self._axis_a._mode.currentText() != "Step Size" or self._axis_b._mode.currentText() != "Step Size":
            return
        outer_is_vtg = self._axis_selector.outer() == "Vtg"
        self._step_linking = True
        try:
            if source == "a":
                # axis_a step changed → update axis_b
                # Δvtg = r · Δvbg always
                self._axis_b._step.setValue(value / r if outer_is_vtg else value * r)
            else:
                # axis_b step changed → update axis_a
                self._axis_a._step.setValue(value * r if outer_is_vtg else value / r)
        finally:
            self._step_linking = False
        self._update_raw_link_label()

    def _on_ratio_changed_for_step_link(self):
        if self._step_linking or not self._is_raw_vtg_vbg_mode():
            self._update_raw_link_label()
            return
        r = self._coord_widget.ratio()
        if abs(r) < EPS:
            self._update_raw_link_label()
            return
        if self._axis_a._mode.currentText() != "Step Size" or self._axis_b._mode.currentText() != "Step Size":
            self._update_raw_link_label()
            return
        # Recompute axis_b from axis_a (Vtg drives Vbg)
        outer_is_vtg = self._axis_selector.outer() == "Vtg"
        step_a = self._axis_a._step.value()
        self._step_linking = True
        try:
            self._axis_b._step.setValue(step_a / r if outer_is_vtg else step_a * r)
        finally:
            self._step_linking = False
        self._update_raw_link_label()

    def _update_raw_link_label(self):
        if not self._is_raw_vtg_vbg_mode():
            self._raw_link_label.setText("")
            return
        r = self._coord_widget.ratio()
        outer = self._axis_selector.outer()
        step_vtg = self._axis_a._step.value() if outer == "Vtg" else self._axis_b._step.value()
        step_vbg = self._axis_b._step.value() if outer == "Vtg" else self._axis_a._step.value()
        self._raw_link_label.setText(
            f"Steps linked via ratio: Δvtg = r·Δvbg  "
            f"({step_vtg:.4g} V = {r:.4g}·{step_vbg:.4g} V)"
        )

    def _update_ratio_validation(self):
        invalid = self._coord_widget.coord_system() == CoordSystem.PHYSICAL and abs(self._coord_widget.ratio()) < EPS
        self._coord_widget.set_ratio_invalid(invalid)
        self._run_btn.setEnabled(not invalid and self._thread is None)
        self._run_btn.setToolTip("Ratio cannot be zero - doping/efield transform is undefined." if invalid else "")

    def _resolve_preview_data(self) -> dict:
        coord = self._coord_widget.coord_system()
        axis_a = self._axis_selector.outer()
        axis_b = self._axis_selector.inner()
        safety = self._safety_widget.get_limits()
        ratio = self._coord_widget.ratio()
        fixed = self._fixed_widget.get_values()
        if not axis_a or not axis_b:
            return {"all_points": [], "valid_points": [], "coord": coord, "axis_a": axis_a, "axis_b": axis_b, "safety": safety, "ratio": ratio, "fixed": fixed}
        if coord == CoordSystem.PHYSICAL and abs(ratio) < EPS:
            raise ValueError("Ratio cannot be zero.")
        axis_a_vals = self._axis_a.get_array()
        axis_b_vals = self._axis_b.get_array()
        all_points, valid_points = build_sweep_points(
            coord=coord,
            axis_a=axis_a,
            axis_a_vals=axis_a_vals,
            axis_b=axis_b,
            axis_b_vals=axis_b_vals,
            fixed=fixed,
            ratio=ratio,
            safety=safety,
            snake=self._axis_selector.snake(),
        )
        return {
            "all_points": all_points,
            "valid_points": valid_points,
            "coord": coord,
            "axis_a": axis_a,
            "axis_b": axis_b,
            "axis_a_vals": axis_a_vals,
            "axis_b_vals": axis_b_vals,
            "axis_a_desc": _describe_array(axis_a_vals, self._axis_a._mode.currentText(), self._axis_a._step.value()),
            "axis_b_desc": _describe_array(axis_b_vals, self._axis_b._mode.currentText(), self._axis_b._step.value()),
            "safety": safety,
            "ratio": ratio,
            "fixed": fixed,
        }

    def _format_axis_summary(self, axis_name: str, desc: dict) -> str:
        if desc["mode"] == "Total Points":
            step_txt = f"{int(desc['param'])} pts"
        else:
            step_txt = f"step {desc['param']:.4f} {AXIS_UNITS.get(axis_name.split('(')[-1].rstrip(')'), 'V')}"
        return (
            f"{axis_name:<18} {desc['start']:>8.4f} -> {desc['stop']:<8.4f} "
            f"{step_txt:<18} -> {desc['points']:>4d} pts"
        )

    def _estimate_duration_s(self, valid_points: list[dict]) -> float:
        exp_s = float(self._exp_spin.value()) / 1000.0 * int(self._frames_spin.value())
        ramp_rate = self._timing_widget.ramp_step() / max(self._timing_widget.step_delay_s(), EPS)
        avg_ramp_time = self._timing_widget.ramp_step() / max(ramp_rate, EPS)
        return len(valid_points) * (self._timing_widget.settle() + avg_ramp_time + exp_s)

    def _format_duration(self, total_s: float) -> str:
        hours = int(total_s // 3600)
        mins = int((total_s % 3600) // 60)
        secs = int(round(total_s % 60))
        if hours:
            return f"~{hours} h {mins} min"
        if mins:
            return f"~{mins} min {secs} sec"
        return f"~{secs} sec"

    def _safety_polygon_for_preview(self, axis_a: str, axis_b: str, ratio: float, safety: dict) -> list[tuple[float, float]]:
        if self._coord_widget.coord_system() != CoordSystem.PHYSICAL:
            return []
        if {axis_a, axis_b} != {"Doping", "E-field"}:
            return []
        raw_corners = [
            (safety["vtg_min"], safety["vbg_min"]),
            (safety["vtg_min"], safety["vbg_max"]),
            (safety["vtg_max"], safety["vbg_max"]),
            (safety["vtg_max"], safety["vbg_min"]),
        ]
        mapped = [_raw_to_physics(vtg, vbg, ratio) for vtg, vbg in raw_corners]
        return mapped if axis_a == "Doping" else [(F, D) for D, F in mapped]

    @Slot()
    def _refresh_preview(self):
        self._update_ratio_validation()
        try:
            data = self._resolve_preview_data()
        except Exception as exc:
            self._summary.setStyleSheet("color: #b42318;")
            self._summary.setText(f"Warning: {exc}")
            self._preview.clear()
            return

        self._last_preview = data
        all_points = data["all_points"]
        valid_points = data["valid_points"]
        skipped = len(all_points) - len(valid_points)
        coord = data["coord"]
        ratio = data["ratio"]
        df_polygon: list[tuple[float, float]] | None = None
        if coord == CoordSystem.RAW and abs(ratio) > EPS:
            raw_corners = [
                (data["safety"]["vtg_min"], data["safety"]["vbg_min"]),
                (data["safety"]["vtg_min"], data["safety"]["vbg_max"]),
                (data["safety"]["vtg_max"], data["safety"]["vbg_max"]),
                (data["safety"]["vtg_max"], data["safety"]["vbg_min"]),
            ]
            df_polygon = [_raw_to_physics(vtg, vbg, ratio) for vtg, vbg in raw_corners]
        self._preview.update_plan(
            all_points=all_points,
            valid_points=valid_points,
            safety=data["safety"],
            axis_a_name=data["axis_a"],
            axis_b_name=data["axis_b"],
            axis_a_vals=data["axis_a_vals"],
            axis_b_vals=data["axis_b_vals"],
            snake=self._axis_selector.snake(),
            draw_polygon=self._safety_polygon_for_preview(data["axis_a"], data["axis_b"], ratio, data["safety"]),
            coord=coord,
            ratio=ratio,
            df_polygon=df_polygon,
        )

        fixed_txt = ", ".join(f"{k} = {v:.4f} V" for k, v in data["fixed"].items()) if data["fixed"] else "None"
        est_s = self._estimate_duration_s(valid_points)
        filename_preview = build_megasweep_filename({
            "coord": data["coord"],
            "axis_a": data["axis_a"],
            "axis_b": data["axis_b"],
            "fixed": data["fixed"],
            "ratio": data["ratio"],
            "center_nm": float(self._center_spin.value()),
            "exp_ms": float(self._exp_spin.value()),
            "frames": int(self._frames_spin.value()),
            "tag": self._tag_edit.text().strip() or "2DSweep",
            "sample": self._sample_edit.text().strip() or "Sample",
            "vbias_available": self._vbias_available(),
        })
        exp_s_per_pt = float(self._exp_spin.value()) / 1000.0 * int(self._frames_spin.value())
        lines = [
            self._format_axis_summary(f"Outer ({data['axis_a']})", self._axis_a.describe()),
            self._format_axis_summary(f"Inner ({data['axis_b']})", self._axis_b.describe()),
            f"Fixed:            {fixed_txt}",
            f"Filename:         {filename_preview}.csv",
            f"Total planned:    {len(all_points)}    In-bounds: {len(valid_points)}    Skipped: {skipped}",
            f"Est. duration:    {self._format_duration(est_s)}   "
            f"(settle {self._timing_widget.settle():.3f} s + exposure {exp_s_per_pt:.3f} s + ramp ~{self._timing_widget.step_delay_s():.3f} s, per pt)",
        ]
        color = "#5a5a5a"
        if len(valid_points) == 0:
            color = "#b42318"
            lines.append("Warning: No points within the safety limits.")
        elif len(all_points) > 0 and len(valid_points) < max(1, int(0.1 * len(all_points))):
            color = "#ad6700"
            lines.append(f"Warning: {skipped} of {len(all_points)} planned points are out of bounds.")
        elif est_s > 3600:
            color = "#ad6700"
            lines.append(f"Warning: Estimated sweep time is {self._format_duration(est_s)}.")
        self._summary.setStyleSheet(f"color: {color};")
        self._summary.setText("\n".join(lines))

    def _collect_params(self) -> dict:
        data = dict(self._last_preview)
        sample = self._sample_edit.text().strip() or "Sample"
        tag = self._tag_edit.text().strip() or "2DSweep"
        exp_ms = float(self._exp_spin.value())
        frames = int(self._frames_spin.value())
        center_nm = float(self._center_spin.value())
        exp_s = f"{int(exp_ms // 1000)}" if exp_ms % 1000 == 0 else f"{exp_ms / 1000:.3g}"
        data.update({
            "exp_ms": exp_ms,
            "center_nm": center_nm,
            "frames": frames,
            "sample": sample,
            "tag": tag,
            "snake": self._axis_selector.snake(),
            "laser_nm": self._laser_edit.text().strip(),
            "power_uw": self._power_edit.text().strip(),
            "settle": self._timing_widget.settle(),
            "ramp_step": self._timing_widget.ramp_step(),
            "step_delay_s": self._timing_widget.step_delay_s(),
            "vbias_available": self._vbias_available(),
            "smu_connected": bool(self._smu is not None and self._smu.is_connected),
            "lf6_connected": bool(self._lf6 is not None and self._lf6.is_connected),
            "out_path": Path(cfg.filename.base_out) / sample / "megasweep",
        })
        data["base_name"] = build_megasweep_filename(data)
        return data

    def _validate(self, params: dict) -> bool:
        if params["coord"] == CoordSystem.PHYSICAL and abs(params["ratio"]) < EPS:
            QMessageBox.critical(self, "Invalid ratio", "Ratio r = 0 is invalid - the doping/efield transform is undefined.")
            return False
        for lo, hi, label in (
            ("vtg_min", "vtg_max", "Vtg"),
            ("vbg_min", "vbg_max", "Vbg"),
            ("vbias_min", "vbias_max", "Vbias"),
        ):
            if params["safety"][lo] >= params["safety"][hi]:
                QMessageBox.critical(self, "Invalid safety limits", f"{label} min must be smaller than max.")
                return False
        total = len(params["all_points"])
        valid = len(params["valid_points"])
        if valid == 0:
            QMessageBox.critical(self, "No valid points", "No sweep points fall within the safety limits.")
            return False
        if valid < 2:
            QMessageBox.critical(self, "Too few points", "At least 2 in-bounds points are required to run.")
            return False
        if total > 0 and valid < max(1, int(total * 0.1)):
            reply = QMessageBox.warning(self, "Many points will be skipped", f"Only {valid} of {total} planned points are within the safety limits.\nContinue anyway?", QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Cancel)
            if reply != QMessageBox.Ok:
                return False
        if self._estimate_duration_s(params["valid_points"]) > 3600:
            reply = QMessageBox.warning(self, "Long sweep", f"Estimated sweep time is {self._format_duration(self._estimate_duration_s(params['valid_points']))}.\nContinue anyway?", QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Cancel)
            if reply != QMessageBox.Ok:
                return False
        if not self._vbias_available() and abs(params["fixed"].get("Vbias", 0.0)) > EPS:
            QMessageBox.warning(self, "Vbias unavailable", f"Fixed Vbias is set to {params['fixed'].get('Vbias', 0.0):.4f} V but the SMU is not connected. Vbias will not be applied.")
        if not self._vbias_available() and "Vbias" in params["fixed"]:
            if max(abs(params["safety"]["vbias_min"]), abs(params["safety"]["vbias_max"])) > 10.0:
                QMessageBox.warning(self, "Inactive Vbias limits", "Vbias safety limits are wide, but Vbias is not active because the SMU is not connected.")
        if not params.get("vbias_available", True) and not params.get("lf6_connected", False):
            QMessageBox.warning(self, "No hardware connected", "Neither SMU nor spectrometer is connected - this run will produce empty data.")
        if self._smu is None or not self._smu.is_connected:
            reply = QMessageBox.question(self, "SMU not connected", "SMU is not connected. Continue with a mock/preview-only acquisition path?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return False
        try:
            params["out_path"].mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            QMessageBox.critical(self, "Output path error", f"Cannot create output directory:\n{exc}")
            return False
        return True

    @Slot()
    def _on_run(self):
        self._refresh_preview()
        params = self._collect_params()
        if not self._validate(params):
            return
        self._preview.clear_progress()
        self._progress.setValue(0)
        self._log_edit.clear()
        self._run_failed = False
        self._run_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._set_status("Running...", "#b26a00")
        self._run_inner_count = max(1, len(params.get("axis_b_vals", [])))
        self._worker = _MegaSweepWorker(params, self._smu, self._lf6)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.log.connect(self._on_log)
        self._worker.progress.connect(self._on_progress)
        self._worker.point_done.connect(self._on_point_done)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._thread.start()

    @Slot()
    def _on_stop(self):
        if self._worker is not None:
            self._worker.request_stop()
        self._stop_btn.setEnabled(False)
        self._set_status("Stopping...", "#b26a00")

    @Slot(str)
    def _on_log(self, msg: str):
        self._log_edit.append(msg)
        sb = self._log_edit.verticalScrollBar()
        sb.setValue(sb.maximum())

    @Slot(str)
    def _on_error(self, msg: str):
        self._run_failed = True
        self._on_log(f"ERROR: {msg}")

    @Slot(int, int)
    def _on_progress(self, done: int, total: int):
        self._progress.setValue(int(100 * done / total) if total > 0 else 0)

    @Slot(int)
    def _on_point_done(self, done: int):
        self._preview.update_progress(done, self._run_inner_count)

    @Slot()
    def _on_finished(self):
        if self._thread:
            if self._thread.isRunning():
                self._thread.quit()
                self._thread.wait()
            self._thread = None
        worker = self._worker
        self._worker = None
        self._run_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        if self._run_failed:
            self._set_status("Error", "#b42318")
        elif worker is not None and worker._stop.is_set():
            self._set_status("Stopped", "#707070")
        else:
            self._set_status("Done", "#1f7a1f")
        self._update_ratio_validation()
        self._schedule_preview()

    def _set_status(self, text: str, color: str):
        self._status_lbl.setText(text)
        self._status_lbl.setStyleSheet(f"color: {color}; font-size: 11px;")
