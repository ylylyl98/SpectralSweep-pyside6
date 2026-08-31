# ui/megasweep_panel.py

from __future__ import annotations

import math
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import QEvent, QObject, QPointF, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QFont, QPolygonF
from PySide6.QtWidgets import (
    QButtonGroup,
    QAbstractSpinBox,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
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
from app.experiment_metadata import ExperimentMetadataService
from utils.filename_builder import format_compact_number, format_decimal_token, format_power_uw_decimal, sanitize_token
from utils.mcd_common import (
    mcd_coordinates as _mcd_coordinates,
    vtg_vbg_from_doping_efield as _vtg_vbg_from_doping_efield,
    resolve_condition_line as _resolve_mcd_condition_line,
)


def resolve_mcd_gate_condition(condition, ratio):
    """Compatibility facade for the shared D/F gate equations."""
    return _resolve_mcd_condition_line(condition, ratio)

pg.setConfigOption("background", "w")
pg.setConfigOption("foreground", "k")

NAN = float("nan")
EPS = 1e-9
_SAFETY_RAMP_STEP_V = 0.1
_SAFETY_RAMP_DELAY_S = 0.02
_DEFAULT_EXTRA_OVERHEAD_S = 1.0
_FLUSH_EVERY_POINTS = 50
_PLOT_UPDATE_EVERY_POINTS = 5
_UI_LOG_EVERY_POINTS = 10
_SETTINGS_MIN_WIDTH = 420
_PREVIEW_MIN_WIDTH = 420
_RESPONSIVE_BREAKPOINT = 960
_SETTINGS_TWO_COLUMN_BREAKPOINT = 570
_SPLITTER_HANDLE_WIDTH = 8
_COMPACT_FIELD_MIN_WIDTH = 86
_COMPACT_FIELD_MAX_WIDTH = 132
_OPTICAL_TABLE_MIN_HEIGHT = 126
_OPTICAL_TABLE_MAX_HEIGHT = 214


class _NoWheelDoubleSpinBox(QDoubleSpinBox):
    """Numeric editor that can never be changed by a mouse-wheel gesture."""

    def wheelEvent(self, event):
        event.ignore()


class _NoWheelSpinBox(QSpinBox):
    """Integer editor that can never be changed by a mouse-wheel gesture."""

    def wheelEvent(self, event):
        event.ignore()


class _NoWheelComboBox(QComboBox):
    """Selector that ignores wheel input unless its popup is being used."""

    def wheelEvent(self, event):
        event.ignore()


def _set_compact_editor(
    widget: QWidget,
    *,
    minimum: int = _COMPACT_FIELD_MIN_WIDTH,
    maximum: int = _COMPACT_FIELD_MAX_WIDTH,
) -> None:
    """Keep form fields readable without allowing them to consume a whole card."""

    widget.setMinimumWidth(minimum)
    widget.setMaximumWidth(maximum)
    widget.setSizePolicy(
        QSizePolicy.Policy.Preferred,
        QSizePolicy.Policy.Fixed,
    )


def _configure_compact_form(form: QFormLayout) -> None:
    form.setContentsMargins(7, 5, 7, 6)
    form.setHorizontalSpacing(8)
    form.setVerticalSpacing(5)
    form.setFieldGrowthPolicy(
        QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint
    )
    form.setFormAlignment(
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
    )
    form.setLabelAlignment(
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    )


class _OpticalSequenceWidget(QWidget):
    """Editable ordered list of optical recipes, one full map per row."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 5, 7, 6)
        layout.setSpacing(5)

        caption = QLabel("One complete 2D map and output file per enabled row.")
        caption.setWordWrap(True)
        caption.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        caption.setStyleSheet("color: #5f6b78; font-size: 10px;")
        layout.addWidget(caption)

        self._table = QTableWidget(0, 5)
        headers = ["Run", "Name", "Center λ", "Exposure", "Frames/EPF"]
        self._table.setHorizontalHeaderLabels(headers)
        for column, title in enumerate(headers):
            self._table.horizontalHeaderItem(column).setToolTip(title)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self._table.setMinimumWidth(0)
        self._table.setMinimumHeight(_OPTICAL_TABLE_MIN_HEIGHT)
        self._table.setMaximumHeight(_OPTICAL_TABLE_MAX_HEIGHT)
        header = self._table.horizontalHeader()
        header.setMinimumSectionSize(24)
        for column in range(5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((26, 38, 60, 66, 60)):
            self._table.setColumnWidth(column, width)
        self._table.cellChanged.connect(self._on_cell_changed)
        layout.addWidget(self._table)

        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        self._add_btn = QPushButton("Add")
        self._duplicate_btn = QPushButton("Duplicate")
        self._remove_btn = QPushButton("Remove")
        self._up_btn = QPushButton("↑")
        self._down_btn = QPushButton("↓")
        for button, width in (
            (self._add_btn, 42),
            (self._duplicate_btn, 64),
            (self._remove_btn, 56),
            (self._up_btn, 28),
            (self._down_btn, 28),
        ):
            button.setFixedWidth(width)
        for button in (
            self._add_btn,
            self._duplicate_btn,
            self._remove_btn,
            self._up_btn,
            self._down_btn,
        ):
            button.setMinimumHeight(24)
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )

        self._add_btn.clicked.connect(self._add_condition)
        self._duplicate_btn.clicked.connect(self._duplicate_selected)
        self._remove_btn.clicked.connect(self._remove_selected)
        self._up_btn.clicked.connect(lambda: self._move_selected(-1))
        self._down_btn.clicked.connect(lambda: self._move_selected(1))

        self.set_conditions([
            OpticalCondition(
                enabled=True,
                name="C1",
                center_nm=float(cfg.lf6.center_nm),
                exposure_ms=float(cfg.lf6.exposure_ms),
                frames=int(cfg.lf6.accumulations),
            )
        ])

    def _make_center_spin(self, value: float) -> _NoWheelDoubleSpinBox:
        spin = _NoWheelDoubleSpinBox()
        spin.setRange(200, 2000)
        spin.setDecimals(2)
        spin.setValue(float(value))
        spin.setSuffix(" nm")
        _set_compact_editor(spin, minimum=56, maximum=74)
        spin.valueChanged.connect(self.changed)
        return spin

    def _make_exposure_spin(self, value: float) -> _NoWheelDoubleSpinBox:
        spin = _NoWheelDoubleSpinBox()
        spin.setRange(1, 600000)
        spin.setDecimals(1)
        spin.setValue(float(value))
        spin.setSuffix(" ms")
        _set_compact_editor(spin, minimum=60, maximum=78)
        spin.valueChanged.connect(self.changed)
        return spin

    def _make_frames_spin(self, value: int) -> _NoWheelSpinBox:
        spin = _NoWheelSpinBox()
        spin.setRange(1, 1000)
        spin.setValue(int(value))
        _set_compact_editor(spin, minimum=48, maximum=62)
        spin.valueChanged.connect(self.changed)
        return spin

    def _append_row(self, condition: OpticalCondition) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        enabled_item = QTableWidgetItem()
        enabled_item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        enabled_item.setCheckState(
            Qt.CheckState.Checked
            if condition.enabled
            else Qt.CheckState.Unchecked
        )
        self._table.setItem(row, 0, enabled_item)
        self._table.setItem(row, 1, QTableWidgetItem(condition.name))
        self._table.setCellWidget(row, 2, self._make_center_spin(condition.center_nm))
        self._table.setCellWidget(row, 3, self._make_exposure_spin(condition.exposure_ms))
        self._table.setCellWidget(row, 4, self._make_frames_spin(condition.frames))
        self._set_row_enabled(row, condition.enabled)
        self._table.setRowHeight(row, 28)

    def _set_row_enabled(self, row: int, enabled: bool) -> None:
        for column in (1, 2, 3, 4):
            widget = self._table.cellWidget(row, column)
            if widget is not None:
                widget.setEnabled(enabled)
        name_item = self._table.item(row, 1)
        if name_item is not None:
            flags = name_item.flags()
            if enabled:
                flags |= Qt.ItemFlag.ItemIsEditable
            else:
                flags &= ~Qt.ItemFlag.ItemIsEditable
            name_item.setFlags(flags)

    def _on_cell_changed(self, row: int, column: int) -> None:
        if column == 0:
            item = self._table.item(row, 0)
            if item is not None:
                self._set_row_enabled(
                    row, item.checkState() == Qt.CheckState.Checked
                )
        self.changed.emit()

    def conditions(self, *, enabled_only: bool = False) -> list[OpticalCondition]:
        result: list[OpticalCondition] = []
        for row in range(self._table.rowCount()):
            enabled_item = self._table.item(row, 0)
            name_item = self._table.item(row, 1)
            center = self._table.cellWidget(row, 2)
            exposure = self._table.cellWidget(row, 3)
            frames = self._table.cellWidget(row, 4)
            if not isinstance(center, QDoubleSpinBox):
                continue
            if not isinstance(exposure, QDoubleSpinBox):
                continue
            if not isinstance(frames, QSpinBox):
                continue
            condition = OpticalCondition(
                enabled=(
                    enabled_item is not None
                    and enabled_item.checkState() == Qt.CheckState.Checked
                ),
                name=(name_item.text().strip() if name_item else "") or f"C{row + 1}",
                center_nm=float(center.value()),
                exposure_ms=float(exposure.value()),
                frames=int(frames.value()),
            )
            if condition.enabled or not enabled_only:
                result.append(condition)
        return result

    def set_conditions(self, conditions: list[OpticalCondition | dict]) -> None:
        parsed: list[OpticalCondition] = []
        for index, condition in enumerate(conditions):
            if isinstance(condition, OpticalCondition):
                parsed.append(condition)
                continue
            if not isinstance(condition, dict):
                continue
            try:
                parsed.append(OpticalCondition(
                    enabled=bool(condition.get("enabled", True)),
                    name=str(condition.get("name", f"C{index + 1}")),
                    center_nm=float(condition["center_nm"]),
                    exposure_ms=float(
                        condition.get("exposure_ms", condition.get("exp_ms"))
                    ),
                    frames=int(condition["frames"]),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        if not parsed:
            parsed = [OpticalCondition(True, "C1", 720.0, 30.0, 1)]
        self._table.blockSignals(True)
        self._table.setRowCount(0)
        for condition in parsed:
            self._append_row(condition)
        self._table.blockSignals(False)
        self._table.selectRow(0)
        self.changed.emit()

    def first_editors(self) -> tuple[QDoubleSpinBox, QDoubleSpinBox, QSpinBox]:
        return (
            self._table.cellWidget(0, 3),
            self._table.cellWidget(0, 2),
            self._table.cellWidget(0, 4),
        )

    def _selected_row(self) -> int:
        row = self._table.currentRow()
        return row if row >= 0 else max(0, self._table.rowCount() - 1)

    def _add_condition(self) -> None:
        conditions = self.conditions()
        previous = conditions[-1]
        conditions.append(OpticalCondition(
            True,
            f"C{len(conditions) + 1}",
            previous.center_nm,
            previous.exposure_ms,
            previous.frames,
        ))
        self.set_conditions(conditions)
        self._table.selectRow(len(conditions) - 1)

    def _duplicate_selected(self) -> None:
        conditions = self.conditions()
        row = self._selected_row()
        source = conditions[row]
        conditions.insert(row + 1, OpticalCondition(
            source.enabled,
            f"{source.name}_copy",
            source.center_nm,
            source.exposure_ms,
            source.frames,
        ))
        self.set_conditions(conditions)
        self._table.selectRow(row + 1)

    def _remove_selected(self) -> None:
        if self._table.rowCount() <= 1:
            return
        conditions = self.conditions()
        row = self._selected_row()
        conditions.pop(row)
        self.set_conditions(conditions)
        self._table.selectRow(min(row, len(conditions) - 1))

    def _move_selected(self, delta: int) -> None:
        conditions = self.conditions()
        row = self._selected_row()
        target = row + int(delta)
        if not 0 <= target < len(conditions):
            return
        conditions[row], conditions[target] = conditions[target], conditions[row]
        self.set_conditions(conditions)
        self._table.selectRow(target)


class _CollapsibleSection(QFrame):
    """Lightweight card used for secondary 2D-sweep settings."""

    expanded_changed = Signal(bool)

    def __init__(
        self,
        title: str,
        content: QWidget,
        *,
        expanded: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("MegaSweepDisclosure")
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Maximum,
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._header = QToolButton()
        self._header.setObjectName("MegaSweepDisclosureHeader")
        self._header.setText(title)
        self._header.setCheckable(True)
        self._header.setChecked(expanded)
        self._header.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self._header.setArrowType(
            Qt.ArrowType.DownArrow
            if expanded
            else Qt.ArrowType.RightArrow
        )
        self._header.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )

        self._content = content
        self._content.setVisible(expanded)
        layout.addWidget(self._header)
        layout.addWidget(self._content)
        self._header.toggled.connect(self._on_toggled)

        self.setStyleSheet(
            "QFrame#MegaSweepDisclosure {"
            " background: #ffffff;"
            " border: 1px solid #d9e0e9;"
            " border-radius: 7px;"
            "}"
            "QToolButton#MegaSweepDisclosureHeader {"
            " min-height: 24px;"
            " border: none;"
            " border-radius: 6px;"
            " padding: 4px 7px;"
            " color: #29384c;"
            " font-weight: 600;"
            " text-align: left;"
            " background: transparent;"
            "}"
            "QToolButton#MegaSweepDisclosureHeader:hover {"
            " background: #f0f5fb;"
            "}"
        )

    @Slot(bool)
    def _on_toggled(self, expanded: bool) -> None:
        self._content.setVisible(expanded)
        self._header.setArrowType(
            Qt.ArrowType.DownArrow
            if expanded
            else Qt.ArrowType.RightArrow
        )
        self.updateGeometry()
        self.expanded_changed.emit(expanded)

    def is_expanded(self) -> bool:
        return self._header.isChecked()

    def set_expanded(self, expanded: bool) -> None:
        self._header.setChecked(bool(expanded))


class _MegaSweepStopRequested(Exception):
    """Internal control-flow exception used to unwind to the safety ramp."""
    pass


@dataclass(frozen=True)
class OpticalCondition:
    """One complete 2D-map acquisition recipe."""

    enabled: bool
    name: str
    center_nm: float
    exposure_ms: float
    frames: int

    def as_dict(self) -> dict:
        return {
            "enabled": bool(self.enabled),
            "name": str(self.name),
            "center_nm": float(self.center_nm),
            "exposure_ms": float(self.exposure_ms),
            "frames": int(self.frames),
        }


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


def _format_axis_range_token(axis_name: str, desc: dict) -> str:
    code = AXIS_CODES.get(axis_name, sanitize_token(axis_name) or "Axis")
    start = format_compact_number(desc.get("start", 0.0), keep_sign=True, decimals=3)
    stop = format_compact_number(desc.get("stop", 0.0), keep_sign=True, decimals=3)
    points = int(desc.get("points", 0) or 0)
    return f"{code}{start}to{stop}_{points}pts"


def _format_laser_power_token(laser_nm, power_uw) -> str:
    parts: list[str] = []
    laser_txt = sanitize_token(laser_nm)
    if laser_txt:
        parts.append(f"{laser_txt}nm")
    try:
        power_txt = format_power_uw_decimal(float(power_uw)) if str(power_uw).strip() else ""
    except Exception:
        power_txt = ""
    if power_txt:
        parts.append(power_txt)
    return "".join(parts)


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
    sample = params.get("sample", "SampleID") or "SampleID"
    tag = params.get("tag", "2DSweep") or "2DSweep"
    axis_a = params["axis_a"]
    axis_b = params["axis_b"]
    coord = params["coord"]
    ratio = float(params.get("ratio", 1.0))
    vbias_available = bool(params.get("vbias_available", True))
    fixed = params.get("fixed", {})

    axis_a_desc = params.get("axis_a_desc")
    axis_b_desc = params.get("axis_b_desc")
    if isinstance(axis_a_desc, dict) and isinstance(axis_b_desc, dict):
        sweep_token = "_".join([
            _format_axis_range_token(axis_a, axis_a_desc),
            _format_axis_range_token(axis_b, axis_b_desc),
        ])
    else:
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
    laser_power_token = _format_laser_power_token(params.get("laser_nm", ""), params.get("power_uw", ""))
    parts = [sample, sweep_token, fixed_token]
    if laser_power_token:
        parts.append(laser_power_token)
    condition_index = params.get("condition_index")
    if condition_index is not None:
        index_value = int(condition_index)
        condition_name = sanitize_token(params.get("condition_name", ""))[:24]
        condition_token = f"C{index_value:02d}"
        default_names = {f"c{index_value}", f"c{index_value:02d}"}
        if condition_name and condition_name.lower() not in default_names:
            condition_token = f"{condition_token}_{condition_name}"
        parts.append(condition_token)
    parts.extend([optical_token, tag])
    return "~".join(parts)


def _build_csv_metadata_text(
    params: dict,
    wls: np.ndarray,
    *,
    status: str = "Running",
    completed_points: int = 0,
) -> str:
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
        f"# Status: {status}",
        f"# CompletedPoints: {int(completed_points)}",
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
        f"# ExtraOverheadPerPoint_s: {float(params.get('extra_overhead_s', 0.0)):.4f}",
        f"# RampStep_V: {params['ramp_step']:.4f}",
        f"# StepDelay_s: {params['step_delay_s']:.4f}",
        f"# CSVFlushEvery_points: {_FLUSH_EVERY_POINTS}",
        f"# PlotUpdateEvery_points: {_PLOT_UPDATE_EVERY_POINTS}",
        f"# UILogEvery_points: {_UI_LOG_EVERY_POINTS}",
        f"# RampRate_Vs: {ramp_rate:.2f}",
        "#",
        "# === Optical ===",
        f"# Condition_Index: {int(params.get('condition_index', 1))}",
        f"# Condition_Count: {int(params.get('condition_count', 1))}",
        f"# Condition_Name: {params.get('condition_name', 'C1')}",
        f"# Center_nm: {params['center_nm']:.2f}",
        f"# Exposure_ms: {params['exp_ms']}",
        f"# Frames_EPF: {params['frames']}",
        f"# Wavelength_start_nm: {float(wls[0]):.2f}",
        f"# Wavelength_end_nm: {float(wls[-1]):.2f}",
        f"# Wavelength_pixels: {int(wls.size)}",
        "#",
        "# === Hardware ===",
        f"# SMU_Connected: {smu_connected}",
        f"# Vbias_Available: {vbias_available}",
        f"# LF6_Connected: {lf6_connected}",
        "#",
        "# === Sample ID ===",
        f"# Sample_ID: {params.get('sample', 'SampleID')}",
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
        super().__init__("Sweep Coordinates", parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(7, 5, 7, 6)
        lay.setSpacing(5)
        self._group = QButtonGroup(self)
        self._raw = QRadioButton("Raw Vtg/Vbg")
        self._physical = QRadioButton("Physical D/F")
        self._raw.setToolTip("Sweep the Vtg and Vbg voltages directly.")
        self._physical.setToolTip(
            "Sweep Doping and E-field coordinates; derive Vtg and Vbg using the gate ratio."
        )
        self._raw.setChecked(True)
        self._group.addButton(self._raw)
        self._group.addButton(self._physical)
        coord_row = QVBoxLayout()
        coord_row.setContentsMargins(0, 0, 0, 0)
        coord_row.setSpacing(3)
        coord_row.addWidget(self._raw)
        coord_row.addWidget(self._physical)
        lay.addLayout(coord_row)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._ratio_label = QLabel("Ratio r")
        self._ratio_spin = _NoWheelDoubleSpinBox()
        self._ratio_spin.setRange(-1000.0, 1000.0)
        self._ratio_spin.setDecimals(4)
        self._ratio_spin.setValue(1.0)
        _set_compact_editor(self._ratio_spin, minimum=90, maximum=116)
        self._ratio_spin.setToolTip(
            "Gate efficiency ratio r.\n"
            "Doping D = Vtg + r·Vbg\n"
            "E-field F = Vtg − r·Vbg\n"
            "Hardware: Vtg = (D+F)/2,  Vbg = (D−F)/(2r)\n\n"
            "Changing r changes the actual Vtg/Vbg positions swept."
        )
        row.addWidget(self._ratio_label)
        row.addWidget(self._ratio_spin)
        row.addStretch(1)
        lay.addLayout(row)

        self._formula = QLabel("D = Vtg + r·Vbg    F = Vtg − r·Vbg\nVtg = (D+F)/2    Vbg = (D−F)/(2r)")
        self._formula.setStyleSheet("color: #6a6a6a; font-size: 10px;")
        self._formula.setWordWrap(True)
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
        super().__init__("Sweep Axes", parent)
        vlay = QVBoxLayout(self)
        vlay.setContentsMargins(7, 5, 7, 6)
        vlay.setSpacing(5)
        self._available: list[str] = []
        self._outer = _NoWheelComboBox()
        self._inner = _NoWheelComboBox()
        for combo in (self._outer, self._inner):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(0)
            _set_compact_editor(combo, minimum=104, maximum=144)
        self._snake = QCheckBox("Snake scan")
        self._snake.setToolTip(
            "Reverse the inner-axis direction on alternating outer-axis steps."
        )
        self._snake.setChecked(True)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(5)
        grid.addWidget(QLabel("Outer (slow)"), 0, 0)
        grid.addWidget(self._outer, 0, 1)
        grid.addWidget(QLabel("Inner (fast)"), 1, 0)
        grid.addWidget(self._inner, 1, 1)
        vlay.addLayout(grid)
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
        lay.setContentsMargins(7, 5, 7, 6)
        lay.setSpacing(5)

        self._start = _NoWheelDoubleSpinBox()
        self._stop = _NoWheelDoubleSpinBox()
        self._step = _NoWheelDoubleSpinBox()
        for spin in (self._start, self._stop):
            spin.setRange(-200.0, 200.0)
            spin.setDecimals(4)
        self._start.setValue(default_start)
        self._stop.setValue(default_stop)
        self._step.setRange(1e-9, 100000.0)
        self._step.setDecimals(4)
        self._step.setValue(1.0)
        self._mode = _NoWheelComboBox()
        self._mode.addItems(["Step Size", "Total Points"])
        _set_compact_editor(self._mode, minimum=92, maximum=112)
        for spin in (self._start, self._stop, self._step):
            _set_compact_editor(spin, minimum=82, maximum=112)

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

    def set_axis_label(self, name: str, units: str, display_name: str | None = None):
        self._axis_name = name
        prefix = self.title().split(":")[0]
        self.setTitle(f"{prefix}: {display_name or name}")
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
        _configure_compact_form(self._layout)
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
            spin = _NoWheelDoubleSpinBox()
            spin.setRange(-200.0, 200.0)
            spin.setDecimals(4)
            spin.setValue(0.0)
            _set_compact_editor(spin)
            if axis == "E-field":
                spin.setToolTip(
                    "Fixed electric field - held constant for all sweep points.\n"
                    "Gate voltages are derived from Doping + this value:\n"
                    "  Vtg = (Doping + E-field) / 2\n"
                    "  Vbg = (Doping - E-field) / (2r)"
                )
            if axis == "Vbias" and not vbias_available:
                spin.setEnabled(False)
                row = QWidget()
                row_lay = QHBoxLayout(row)
                row_lay.setContentsMargins(0, 0, 0, 0)
                row_lay.setSpacing(6)
                badge = QLabel("N/A")
                badge.setToolTip("No usable Vbias Keithley channel is connected.")
                badge.setStyleSheet("color: #ad6700;")
                row_lay.addWidget(spin)
                row_lay.addWidget(badge)
                row_lay.addStretch(1)
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
        grid = QGridLayout(self)
        grid.setContentsMargins(7, 5, 7, 6)
        grid.setHorizontalSpacing(7)
        grid.setVerticalSpacing(5)
        min_header = QLabel("Minimum")
        max_header = QLabel("Maximum")
        for header in (min_header, max_header):
            header.setStyleSheet("color: #6a6a6a; font-size: 10px;")
        grid.addWidget(min_header, 0, 1)
        grid.addWidget(max_header, 0, 2)
        self._spins: dict[str, QDoubleSpinBox] = {}
        defaults = {
            "vtg_min": -10.0, "vtg_max": 10.0,
            "vbg_min": -10.0, "vbg_max": 10.0,
            "vbias_min": -1.0, "vbias_max": 1.0,
        }
        for row, (prefix, row_label) in enumerate(
            (("vtg", "Vtg"), ("vbg", "Vbg"), ("vbias", "Vbias")),
            start=1,
        ):
            grid.addWidget(QLabel(row_label), row, 0)
            for column, suffix in enumerate(("min", "max"), start=1):
                key = f"{prefix}_{suffix}"
                spin = _NoWheelDoubleSpinBox()
                spin.setRange(-200.0, 200.0)
                spin.setDecimals(4)
                spin.setValue(defaults[key])
                spin.setSuffix(" V")
                _set_compact_editor(spin, minimum=88, maximum=116)
                spin.valueChanged.connect(self.changed)
                self._spins[key] = spin
                grid.addWidget(spin, row, column)
        grid.setColumnStretch(3, 1)

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
        lay.setContentsMargins(7, 5, 7, 6)
        lay.setSpacing(5)
        form = QFormLayout()
        _configure_compact_form(form)
        form.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(form)
        self._settle = _NoWheelDoubleSpinBox()
        self._settle.setRange(0.0, 60.0)
        self._settle.setDecimals(4)
        self._settle.setValue(cfg.ramp.settle_s)
        self._settle.setSuffix(" s")
        _set_compact_editor(self._settle)
        self._extra_overhead = _NoWheelDoubleSpinBox()
        self._extra_overhead.setRange(0.0, 30.0)
        self._extra_overhead.setDecimals(3)
        self._extra_overhead.setSingleStep(0.1)
        self._extra_overhead.setValue(_DEFAULT_EXTRA_OVERHEAD_S)
        self._extra_overhead.setSuffix(" s")
        _set_compact_editor(self._extra_overhead)
        self._extra_overhead.setToolTip(
            "Pre-run ETA allowance for per-point overhead not covered by exposure, "
            "settle, or ramp time: SMU readbacks, current reads, spectrometer "
            "readout overhead, CSV write, and UI/log work."
        )
        form.addRow("Settle time (s)", self._settle)
        form.addRow("Overhead / point", self._extra_overhead)
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
        _configure_compact_form(adv_form)
        adv_form.setContentsMargins(0, 0, 0, 0)
        self._ramp_step = _NoWheelDoubleSpinBox()
        self._ramp_step.setRange(0.001, 10.0)
        self._ramp_step.setDecimals(4)
        self._ramp_step.setValue(cfg.ramp.step_V)
        self._ramp_step.setSuffix(" V")
        _set_compact_editor(self._ramp_step)
        self._step_delay_ms = _NoWheelDoubleSpinBox()
        self._step_delay_ms.setRange(1.0, 10000.0)
        self._step_delay_ms.setDecimals(1)
        self._step_delay_ms.setValue(cfg.ramp.delay_s * 1000.0)
        self._step_delay_ms.setSuffix(" ms")
        _set_compact_editor(self._step_delay_ms)
        self._rate = QLabel("")
        self._rate.setStyleSheet("color: #6a6a6a;")
        adv_form.addRow("Voltage increment (V)", self._ramp_step)
        adv_form.addRow("Step delay (ms)", self._step_delay_ms)
        adv_form.addRow("Ramp rate", self._rate)
        lay.addWidget(self._advanced)
        self._toggle.toggled.connect(self._on_toggle)
        self._settle.valueChanged.connect(self.changed)
        self._extra_overhead.valueChanged.connect(self.changed)
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

    def extra_overhead_s(self) -> float:
        return float(self._extra_overhead.value())

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
        self._progress_axis_x = np.array([], dtype=float)
        self._progress_axis_y = np.array([], dtype=float)
        self._progress_v_x = np.array([], dtype=float)
        self._progress_v_y = np.array([], dtype=float)
        self._completed_axis_x = np.array([], dtype=float)
        self._completed_axis_y = np.array([], dtype=float)
        self._completed_v_x = np.array([], dtype=float)
        self._completed_v_y = np.array([], dtype=float)
        self._completed_segment_offsets: list[int] = [0]
        self._progress_stripe_indices = np.array([], dtype=int)
        self._stripe_endpoints: list[
            tuple[float, float, float, float, float, float, float, float] | None
        ] = []

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

    def _cache_progress_geometry(self) -> None:
        """Precompute immutable point and stripe geometry for cheap updates."""
        points = self._valid_points
        self._progress_axis_x = np.asarray(
            [point["axis_a"] for point in points], dtype=float
        )
        self._progress_axis_y = np.asarray(
            [point["axis_b"] for point in points], dtype=float
        )
        if self._coord == CoordSystem.RAW:
            voltage_points = [
                self._raw_to_df(point["raw"][0], point["raw"][1])
                for point in points
            ]
        else:
            voltage_points = [
                (float(point["raw"][0]), float(point["raw"][1]))
                for point in points
            ]
        self._progress_v_x = np.asarray(
            [point[0] for point in voltage_points], dtype=float
        )
        self._progress_v_y = np.asarray(
            [point[1] for point in voltage_points], dtype=float
        )

        axis_values = np.asarray(self._axis_a_vals, dtype=float)
        grouped: list[list[int]] = [[] for _ in axis_values]
        stripe_indices = np.full(len(points), -1, dtype=int)
        if axis_values.size:
            sorted_indices = np.argsort(axis_values)
            sorted_values = axis_values[sorted_indices]
            for point_index, point_axis in enumerate(self._progress_axis_x):
                insertion = int(np.searchsorted(sorted_values, point_axis))
                candidates = [
                    candidate
                    for candidate in (insertion - 1, insertion)
                    if 0 <= candidate < sorted_values.size
                ]
                if not candidates:
                    continue
                sorted_index = min(
                    candidates,
                    key=lambda candidate: abs(sorted_values[candidate] - point_axis),
                )
                if abs(sorted_values[sorted_index] - point_axis) < EPS:
                    stripe_index = int(sorted_indices[sorted_index])
                    grouped[stripe_index].append(point_index)
                    stripe_indices[point_index] = stripe_index

        axis_x: list[float] = []
        axis_y: list[float] = []
        voltage_x: list[float] = []
        voltage_y: list[float] = []
        offsets = [0]
        endpoints = []
        for indices in grouped:
            if indices:
                first, last = indices[0], indices[-1]
                endpoint = (
                    self._progress_axis_x[first], self._progress_axis_x[last],
                    self._progress_axis_y[first], self._progress_axis_y[last],
                    self._progress_v_x[first], self._progress_v_x[last],
                    self._progress_v_y[first], self._progress_v_y[last],
                )
                endpoints.append(endpoint)
                axis_x.extend((endpoint[0], endpoint[1], np.nan))
                axis_y.extend((endpoint[2], endpoint[3], np.nan))
                voltage_x.extend((endpoint[4], endpoint[5], np.nan))
                voltage_y.extend((endpoint[6], endpoint[7], np.nan))
            else:
                endpoints.append(None)
            offsets.append(len(axis_x))

        self._completed_axis_x = np.asarray(axis_x, dtype=float)
        self._completed_axis_y = np.asarray(axis_y, dtype=float)
        self._completed_v_x = np.asarray(voltage_x, dtype=float)
        self._completed_v_y = np.asarray(voltage_y, dtype=float)
        self._completed_segment_offsets = offsets
        self._progress_stripe_indices = stripe_indices
        self._stripe_endpoints = endpoints

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
        self._cache_progress_geometry()
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
        cur = self._valid_points[upto - 1]
        self._completed_axis_pts.setData(
            x=self._progress_axis_x[:upto], y=self._progress_axis_y[:upto]
        )
        self._completed_v_pts.setData(
            x=self._progress_v_x[:upto], y=self._progress_v_y[:upto]
        )
        inner_count = max(1, inner_count)
        current_stripe = min(len(self._axis_a_vals) - 1, max(0, (done - 1) // inner_count)) if len(self._axis_a_vals) else 0
        segment_end = self._completed_segment_offsets[
            min(current_stripe, len(self._completed_segment_offsets) - 1)
        ]
        if segment_end:
            self._done_axis.setData(
                x=self._completed_axis_x[:segment_end],
                y=self._completed_axis_y[:segment_end],
            )
            self._done_v.setData(
                x=self._completed_v_x[:segment_end],
                y=self._completed_v_y[:segment_end],
            )
        else:
            self._done_axis.setData([], [])
            self._done_v.setData([], [])
        stripe_index = int(self._progress_stripe_indices[upto - 1])
        endpoint = (
            self._stripe_endpoints[stripe_index]
            if 0 <= stripe_index < len(self._stripe_endpoints)
            else None
        )
        if endpoint is not None:
            self._current_axis_stripe.setData(
                x=[endpoint[0], endpoint[1]], y=[endpoint[2], endpoint[3]]
            )
            self._current_v_stripe.setData(
                x=[endpoint[4], endpoint[5]], y=[endpoint[6], endpoint[7]]
            )
        else:
            self._current_axis_stripe.setData([], [])
            self._current_v_stripe.setData([], [])
        self._cur_axis.setData(x=[cur["axis_a"]], y=[cur["axis_b"]])
        self._cur_v.setData(
            x=[self._progress_v_x[upto - 1]], y=[self._progress_v_y[upto - 1]]
        )

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
    map_started = Signal(int, int, str)
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
        points = p["valid_points"]
        if not points:
            raise RuntimeError("No sweep points within safety limits.")

        conditions = [
            dict(condition)
            for condition in p.get("optical_conditions", [])
            if bool(condition.get("enabled", True))
        ]
        if not conditions:
            conditions = [{
                "enabled": True,
                "name": "C1",
                "center_nm": float(p["center_nm"]),
                "exposure_ms": float(p["exp_ms"]),
                "frames": int(p["frames"]),
            }]

        map_count = len(conditions)
        total_acquisitions = len(points) * map_count
        self._emit_log(
            f"Starting optical sequence: {map_count} complete map(s), "
            f"{total_acquisitions} total acquisitions."
        )
        for map_offset, condition in enumerate(conditions):
            if self._stop.is_set():
                break
            map_index = map_offset + 1
            condition_name = str(condition.get("name", "")).strip() or f"C{map_index}"
            map_p = dict(p)
            map_p.update({
                "condition_index": map_index,
                "condition_count": map_count,
                "condition_name": condition_name,
                "center_nm": float(condition["center_nm"]),
                "exp_ms": float(
                    condition.get("exposure_ms", condition.get("exp_ms"))
                ),
                "frames": int(condition["frames"]),
            })
            map_p["base_name"] = build_megasweep_filename(map_p)
            description = (
                f"{condition_name}: {map_p['center_nm']:g} nm, "
                f"{map_p['exp_ms']:g} ms, {map_p['frames']} EPF"
            )
            self.map_started.emit(map_index, map_count, description)
            self._emit_log(f"Map {map_index}/{map_count} - {description}")
            ramp_ok = True
            map_failed = False
            try:
                self._run_map(
                    map_p,
                    iv=iv,
                    spec=spec,
                    lf6=lf6,
                    global_done_before=map_offset * len(points),
                    total_acquisitions=total_acquisitions,
                )
            except _MegaSweepStopRequested:
                break
            except Exception:
                map_failed = True
                raise
            finally:
                # Each output file is a fully independent gate map. Return to
                # the safe state before applying the next optical recipe.
                if map_failed:
                    self._emit_log(
                        "Map failed; preserving the last commanded SMU state. "
                        "No automatic return-to-zero commands were sent."
                    )
                else:
                    ramp_ok = self._safe_ramp_to_zero(iv)
            if not ramp_ok:
                raise RuntimeError(
                    "Return-to-zero failed; the optical sequence was aborted."
                )

        if self._stop.is_set():
            self._emit_log("Optical sequence stopped. Completed files were preserved.")
        else:
            self._emit_log("Optical sequence complete.")

    def _apply_optical_settings(self, p: dict, spec, lf6) -> None:
        target = spec if spec is not None else lf6
        if target is None:
            return
        try:
            prepare = getattr(self._lf6, "configure_for_acquisition", None)
            if not callable(prepare):
                raise AttributeError("LF6 acquisition preparation surface is unavailable")
            prepare(center_nm=float(p["center_nm"]), exposure_ms=float(p["exp_ms"]), frames=int(p["frames"]))

        except Exception as exc:
            raise RuntimeError(
                f"Could not apply optical condition "
                f"{p.get('condition_index', 1)} ({p.get('condition_name', 'C1')}): {exc}"
            ) from exc

    def _run_map(
        self,
        p: dict,
        *,
        iv,
        spec,
        lf6,
        global_done_before: int,
        total_acquisitions: int,
    ) -> None:
        self._apply_optical_settings(p, spec, lf6)
        points = p["valid_points"]
        self._emit_log("Acquiring wavelength calibration...")
        wls = _get_wavelengths(spec, lf6, float(p["center_nm"]), tol_nm=1.0)
        if wls.size <= 2:
            raise RuntimeError(
                f"Could not obtain wavelength calibration for "
                f"{p['center_nm']:g} nm. Aborting."
            )

        out_path: Path = p["out_path"]
        fp = out_path / f"{p['base_name']}.csv"
        k = 2
        while fp.exists():
            fp = out_path / f"{p['base_name']}_{k:03d}.csv"
            k += 1

        cols = [
            f"{p['axis_a']}_axis_a_set", f"{p['axis_b']}_axis_b_set",
            "Vtg_set", "Vbg_set", "Vbias_set", "Doping_set", "Efield_set",
            "Vbg_meas", "Vtg_meas", "Vbias_meas", "Ibg_A", "Itg_A", "Ibias_A",
        ]
        wl_str = np.array([f"{x:g}" for x in wls], dtype="U")
        header = np.concatenate((np.array(cols, dtype="U"), wl_str)).reshape(1, -1)
        meta_fp = fp.with_suffix(".meta.txt")
        completed_points = 0
        status = "Running"
        with open(meta_fp, "w", encoding="utf-8", newline="") as meta_fh:
            meta_fh.write(_build_csv_metadata_text(
                p, wls, status=status, completed_points=completed_points
            ))
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
                    completed_points = done
                    if done % _FLUSH_EVERY_POINTS == 0 or done == total:
                        fh.flush()
                    self.progress.emit(
                        global_done_before + done,
                        total_acquisitions,
                    )
                    if done % _PLOT_UPDATE_EVERY_POINTS == 0 or done == total:
                        self.point_done.emit(done)
                    point_msg = (
                        f"{done}/{total}: {p['axis_a']}={point['axis_a']:.4f}, {p['axis_b']}={point['axis_b']:.4f} | "
                        f"raw Vtg={vtg:.4f}, Vbg={vbg:.4f}, Vb={vbias:.4f} | "
                        f"Itg={_fmt_uA(Itg)} uA, Ibg={_fmt_uA(Ibg)} uA, Ib={_fmt_uA(Ib)} uA"
                    )
                    if done == 1 or done % _UI_LOG_EVERY_POINTS == 0 or done == total:
                        self._emit_log(point_msg)
        except _MegaSweepStopRequested:
            status = "Stopped"
            self._emit_log("Stopped by user.")
            raise
        except Exception:
            status = "Failed"
            raise
        else:
            status = "Complete"
        finally:
            with open(meta_fp, "w", encoding="utf-8", newline="") as meta_fh:
                meta_fh.write(_build_csv_metadata_text(
                    p,
                    wls,
                    status=status,
                    completed_points=completed_points,
                ))

        self._emit_log(f"Map complete. Saved -> {fp.name}")

    def _safe_ramp_to_zero(self, iv) -> bool:
        if iv is None:
            return True
        try:
            self._emit_log("Ramping all connected channels to 0 V...")
            iv.ramp_all_to_zero(
                ramp_step=_SAFETY_RAMP_STEP_V,
                delay_s=_SAFETY_RAMP_DELAY_S,
            )
            self._emit_log("All connected channels are at 0 V.")
            return True
        except Exception as exc:
            self._emit_log(f"Return-to-zero failed: {exc}")
            return False


class MegaSweepPanel(QWidget):
    def __init__(self, smu_ctrl=None, lf6_ctrl=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._smu = smu_ctrl
        self._lf6 = lf6_ctrl
        self._worker: Optional[_MegaSweepWorker] = None
        self._thread: Optional[QThread] = None
        self._run_inner_count = 1
        self._current_map_index = 0
        self._map_count = 1
        self._last_preview: dict = {"all_points": [], "valid_points": []}
        self._run_failed = False
        self._step_linking = False
        self._horizontal_splitter_ratio = 0.40
        self._settings_two_column: Optional[bool] = None
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
        self._splitter = QSplitter(Qt.Horizontal)
        splitter = self._splitter
        root.addWidget(splitter, stretch=1)

        scroll = QScrollArea()
        self._settings_scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(_SETTINGS_MIN_WIDTH)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        scroll.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Expanding,
        )
        left = QWidget()
        left.setMinimumWidth(0)
        left.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self._settings_content = left
        self._left_lay = QVBoxLayout(left)
        self._left_lay.setContentsMargins(5, 4, 5, 6)
        self._left_lay.setSpacing(0)
        self._left_lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        left.setStyleSheet(
            "QGroupBox[compactCard=\"true\"] {"
            " margin-top: 11px;"
            " padding: 6px 5px 5px 5px;"
            " border-radius: 7px;"
            "}"
            "QGroupBox[compactCard=\"true\"]::title {"
            " left: 8px;"
            " padding: 0 4px;"
            "}"
            "QGroupBox#MegaSweepEmbeddedGroup {"
            " border: none;"
            " margin: 0;"
            " padding: 0;"
            " background: transparent;"
            "}"
        )
        scroll.setWidget(left)
        scroll.viewport().installEventFilter(self)
        splitter.addWidget(scroll)

        right = QWidget()
        self._right_panel = right
        right.setMinimumWidth(_PREVIEW_MIN_WIDTH)
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(6, 4, 4, 4)
        right_lay.setSpacing(6)
        splitter.addWidget(right)
        splitter.setHandleWidth(_SPLITTER_HANDLE_WIDTH)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStyleSheet(
            "QSplitter::handle {"
            " background: #d7e0ea;"
            " border-left: 1px solid #c7d2df;"
            " border-right: 1px solid #c7d2df;"
            "}"
            "QSplitter::handle:hover { background: #afc6dd; }"
        )
        splitter.setSizes([480, 720])
        splitter.splitterMoved.connect(self._on_splitter_moved)

        self._coord_widget = _CoordSystemWidget()
        self._axis_selector = _AxisSelectorWidget()
        self._axis_a = _AxisConfigWidget("Outer Axis", -5.0, 5.0)
        self._axis_b = _AxisConfigWidget("Inner Axis", -5.0, 5.0)
        self._fixed_widget = _FixedParamsWidget()
        self._safety_widget = _SafetyWidget()
        self._timing_widget = _TimingWidget()

        for card in (
            self._coord_widget,
            self._axis_selector,
            self._axis_a,
            self._axis_b,
            self._fixed_widget,
            self._safety_widget,
        ):
            card.setProperty("compactCard", True)

        self._timing_widget.setTitle("")
        self._timing_widget.setObjectName("MegaSweepEmbeddedGroup")
        self._timing_widget.setStyleSheet(
            "QGroupBox#MegaSweepEmbeddedGroup {"
            " border: none; margin: 0; padding: 0;"
            " background: transparent;"
            "}"
        )
        self._timing_section = _CollapsibleSection(
            "Timing",
            self._timing_widget,
            expanded=False,
        )

        self._optical_widget = _OpticalSequenceWidget()
        self._sync_primary_optical_aliases()
        self._optical_group = _CollapsibleSection(
            "Optical Sequence",
            self._optical_widget,
            expanded=True,
        )

        meta = QWidget()
        meta_grid = QGridLayout(meta)
        self._metadata_grid = meta_grid
        meta_grid.setContentsMargins(7, 5, 7, 6)
        meta_grid.setHorizontalSpacing(8)
        meta_grid.setVerticalSpacing(5)
        self._sample_edit = QLineEdit()
        self._sample_edit.setPlaceholderText("Sample ID")
        self._tag_edit = QLineEdit("2DSweep")
        self._laser_edit = QLineEdit("730")
        self._power_edit = QLineEdit("1")
        for edit in (
            self._sample_edit,
            self._tag_edit,
            self._laser_edit,
            self._power_edit,
        ):
            _set_compact_editor(edit, minimum=104, maximum=164)
        self._metadata_fields = (
            (QLabel("Sample ID"), self._sample_edit),
            (QLabel("Tag"), self._tag_edit),
            (QLabel("Laser (nm)"), self._laser_edit),
            (QLabel("Power (µW)"), self._power_edit),
        )
        self._metadata_two_column: Optional[bool] = None
        self._metadata_group = _CollapsibleSection(
            "File / Metadata",
            meta,
            expanded=True,
        )

        self._raw_link_label = QLabel()
        self._raw_link_label.setStyleSheet(
            "color: #6a6a6a; font-size: 10px;"
            " padding: 1px 6px 2px 6px;"
        )
        self._raw_link_label.setWordWrap(True)

        self._settings_grid = QGridLayout()
        self._settings_grid.setContentsMargins(0, 0, 0, 0)
        self._settings_grid.setHorizontalSpacing(8)
        self._settings_grid.setVerticalSpacing(7)
        self._left_lay.addLayout(self._settings_grid)
        self._left_lay.addSpacing(12)
        self._settings_cards = (
            self._coord_widget,
            self._axis_selector,
            self._axis_a,
            self._axis_b,
            self._raw_link_label,
            self._fixed_widget,
            self._safety_widget,
            self._timing_section,
            self._optical_group,
            self._metadata_group,
        )
        self._apply_settings_card_layout(force=True)

        self._preview = _PreviewPlot()
        right_lay.addWidget(self._preview, stretch=1)
        self._summary = QTextEdit()
        self._summary.setReadOnly(True)
        self._summary.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self._summary.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._summary.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._summary.setMinimumHeight(112)
        self._summary.setMaximumHeight(168)
        self._summary.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        font = QFont("Consolas", 9)
        font.setStyleHint(QFont.Monospace)
        self._summary.setFont(font)
        self._summary.setStyleSheet(
            "QTextEdit { color: #444444; background: #f5f5f7;"
            " border: 1px solid #dedede; border-radius: 3px;"
            " padding: 4px 6px; }"
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
        self._status_lbl.setMinimumWidth(84)
        self._status_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._status_lbl.setStyleSheet("color: #707070; font-size: 11px;")
        controls.addWidget(self._run_btn)
        controls.addWidget(self._stop_btn)
        controls.addStretch(1)
        controls.addWidget(self._status_lbl)
        right_lay.addLayout(controls)
        right_lay.addWidget(self._progress)

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
        self._install_settings_wheel_redirects()
        QTimer.singleShot(0, self._refresh_settings_scroll_range)

    def _apply_metadata_field_layout(
        self,
        two_column: bool,
        *,
        force: bool = False,
    ) -> bool:
        if not hasattr(self, "_metadata_grid"):
            return False
        if not force and self._metadata_two_column == two_column:
            return False
        while self._metadata_grid.count():
            self._metadata_grid.takeAt(0)
        for column in range(5):
            self._metadata_grid.setColumnStretch(column, 0)
        for index, (label, editor) in enumerate(self._metadata_fields):
            if two_column:
                row = index // 2
                label_column = (index % 2) * 2
            else:
                row = index
                label_column = 0
            self._metadata_grid.addWidget(label, row, label_column)
            self._metadata_grid.addWidget(editor, row, label_column + 1)
        self._metadata_grid.setColumnStretch(4 if two_column else 2, 1)
        self._metadata_two_column = two_column
        self._metadata_grid.invalidate()
        return True

    def _apply_settings_card_layout(self, *, force: bool = False) -> bool:
        """Reflow settings cards from one to two columns using pane width."""

        if not hasattr(self, "_settings_grid"):
            return False
        viewport_width = self._settings_scroll.viewport().width()
        two_column = viewport_width >= _SETTINGS_TWO_COLUMN_BREAKPOINT
        metadata_changed = self._apply_metadata_field_layout(
            two_column,
            force=force,
        )
        if not force and self._settings_two_column == two_column:
            return metadata_changed

        while self._settings_grid.count():
            self._settings_grid.takeAt(0)

        if two_column:
            placements = (
                (self._coord_widget, 0, 0, 1, 1),
                (self._axis_selector, 0, 1, 1, 1),
                (self._axis_a, 1, 0, 1, 1),
                (self._axis_b, 1, 1, 1, 1),
                (self._raw_link_label, 2, 0, 1, 2),
                (self._fixed_widget, 3, 0, 1, 1),
                (self._safety_widget, 3, 1, 1, 1),
                (self._timing_section, 4, 0, 1, 1),
                (self._optical_group, 4, 1, 1, 1),
                (self._metadata_group, 5, 0, 1, 2),
            )
            self._settings_grid.setColumnStretch(0, 1)
            self._settings_grid.setColumnStretch(1, 1)
        else:
            placements = tuple(
                (widget, row, 0, 1, 1)
                for row, widget in enumerate(self._settings_cards)
            )
            self._settings_grid.setColumnStretch(0, 1)
            self._settings_grid.setColumnStretch(1, 0)

        for widget, row, column, row_span, column_span in placements:
            self._settings_grid.addWidget(
                widget,
                row,
                column,
                row_span,
                column_span,
                Qt.AlignmentFlag.AlignTop,
            )
        self._settings_two_column = two_column
        self._settings_grid.invalidate()
        self._settings_grid.activate()
        self._settings_content.updateGeometry()
        return True

    def _on_splitter_moved(self, *_args):
        if self._splitter.orientation() != Qt.Orientation.Horizontal:
            return
        sizes = self._splitter.sizes()
        total = sum(sizes)
        if total > 0:
            self._horizontal_splitter_ratio = min(
                0.55,
                max(0.25, float(sizes[0]) / float(total)),
            )
        QTimer.singleShot(0, self._refresh_settings_scroll_range)

    def _apply_responsive_layout(self, *, force: bool = False):
        if not hasattr(self, "_splitter"):
            return
        narrow = self.width() < _RESPONSIVE_BREAKPOINT
        target = (
            Qt.Orientation.Vertical
            if narrow
            else Qt.Orientation.Horizontal
        )
        changed = self._splitter.orientation() != target
        if not changed and not force:
            if target == Qt.Orientation.Horizontal:
                self._apply_horizontal_splitter_sizes()
            return

        if self._splitter.orientation() == Qt.Orientation.Horizontal:
            self._on_splitter_moved()
        self._splitter.setOrientation(target)
        if target == Qt.Orientation.Vertical:
            self._settings_scroll.setMinimumWidth(0)
            self._right_panel.setMinimumWidth(0)
            self._settings_scroll.setMinimumHeight(140)
            self._right_panel.setMinimumHeight(260)
            self._splitter.setStretchFactor(0, 0)
            self._splitter.setStretchFactor(1, 1)
            available = max(
                0,
                self._splitter.height() - self._splitter.handleWidth(),
            )
            settings_height = max(140, int(round(available * 0.40)))
            settings_height = min(
                360,
                max(140, available - 260),
                settings_height,
            )
            self._splitter.setSizes(
                [settings_height, max(260, available - settings_height)]
            )
        else:
            self._settings_scroll.setMinimumHeight(0)
            self._right_panel.setMinimumHeight(0)
            self._settings_scroll.setMinimumWidth(_SETTINGS_MIN_WIDTH)
            self._right_panel.setMinimumWidth(_PREVIEW_MIN_WIDTH)
            self._splitter.setStretchFactor(0, 0)
            self._splitter.setStretchFactor(1, 1)
            self._apply_horizontal_splitter_sizes()
        QTimer.singleShot(0, self._refresh_settings_scroll_range)

    def _apply_horizontal_splitter_sizes(self):
        available = max(
            0,
            self._splitter.width() - self._splitter.handleWidth(),
        )
        if available <= 0:
            return
        max_left = max(
            _SETTINGS_MIN_WIDTH,
            available - _PREVIEW_MIN_WIDTH,
        )
        left_width = int(round(available * self._horizontal_splitter_ratio))
        left_width = min(max_left, max(_SETTINGS_MIN_WIDTH, left_width))
        self._splitter.setSizes(
            [left_width, max(_PREVIEW_MIN_WIDTH, available - left_width)]
        )

    def _refresh_settings_scroll_range(self):
        if not hasattr(self, "_settings_content"):
            return
        self._apply_settings_card_layout()
        self._install_settings_wheel_redirects()
        self._left_lay.invalidate()
        self._left_lay.activate()
        required_height = max(
            self._left_lay.minimumSize().height(),
            self._left_lay.sizeHint().height(),
        )
        self._settings_content.setMinimumHeight(max(0, required_height))
        self._settings_content.updateGeometry()
        self._settings_scroll.widget().updateGeometry()
        self._settings_scroll.viewport().update()

    def _install_settings_wheel_redirects(self):
        controls = [
            *self._settings_content.findChildren(QAbstractSpinBox),
            *self._settings_content.findChildren(QComboBox),
        ]
        for control in controls:
            if not control.property("settings_wheel_redirect"):
                control.installEventFilter(self)
                control.setProperty("settings_wheel_redirect", True)

    def eventFilter(self, watched, event):
        if (
            event.type() == QEvent.Type.Resize
            and hasattr(self, "_settings_scroll")
            and watched is self._settings_scroll.viewport()
        ):
            QTimer.singleShot(0, self._refresh_settings_scroll_range)
            return False
        if (
            event.type() == QEvent.Type.Wheel
            and isinstance(watched, (QAbstractSpinBox, QComboBox))
            and hasattr(self, "_settings_content")
            and self._settings_content.isAncestorOf(watched)
        ):
            scrollbar = self._settings_scroll.verticalScrollBar()
            pixel_delta = int(event.pixelDelta().y())
            if pixel_delta:
                scroll_delta = pixel_delta
            else:
                steps = float(event.angleDelta().y()) / 120.0
                scroll_delta = int(
                    round(steps * max(24, scrollbar.singleStep() * 3))
                )
            scrollbar.setValue(scrollbar.value() - scroll_delta)
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_responsive_layout()

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
        self._timing_widget._toggle.toggled.connect(
            lambda *_: QTimer.singleShot(
                0, self._refresh_settings_scroll_range
            )
        )
        for section in (
            self._timing_section,
            self._optical_group,
            self._metadata_group,
        ):
            section.expanded_changed.connect(
                lambda *_: QTimer.singleShot(
                    0, self._refresh_settings_scroll_range
                )
            )
        self._optical_widget.changed.connect(
            self._on_optical_sequence_changed
        )
        for w in (self._sample_edit, self._tag_edit, self._laser_edit, self._power_edit):
            w.textChanged.connect(self._schedule_preview)
        self._run_btn.clicked.connect(self._on_run)
        self._stop_btn.clicked.connect(self._on_stop)
        if self._smu is not None:
            self._smu.connected.connect(lambda *_: self._sync_vbias_availability())
            self._smu.disconnected.connect(self._sync_vbias_availability)

    def _sync_primary_optical_aliases(self) -> None:
        """Keep legacy first-row attributes available to callers and tests."""
        exposure, center, frames = self._optical_widget.first_editors()
        self._exp_spin = exposure
        self._center_spin = center
        self._frames_spin = frames

    @Slot()
    def _on_optical_sequence_changed(self) -> None:
        self._sync_primary_optical_aliases()
        self._install_settings_wheel_redirects()
        self._schedule_preview()

    def capture_session_state(self) -> dict:
        """Capture the editable sweep recipe, never controller/runtime state."""

        def axis_state(widget: _AxisConfigWidget) -> dict:
            return {
                "start": float(widget._start.value()),
                "stop": float(widget._stop.value()),
                "step": float(widget._step.value()),
                "mode": widget._mode.currentText(),
            }

        optical_conditions = self._optical_widget.conditions()
        primary = optical_conditions[0]
        return {
            "coordinate_system": self._coord_widget.coord_system().value,
            "ratio": float(self._coord_widget.ratio()),
            "outer_axis": self._axis_selector.outer(),
            "inner_axis": self._axis_selector.inner(),
            "snake": bool(self._axis_selector.snake()),
            "outer_config": axis_state(self._axis_a),
            "inner_config": axis_state(self._axis_b),
            "fixed": self._fixed_widget.get_values(),
            "safety": self._safety_widget.get_limits(),
            "timing": {
                "settle_s": float(self._timing_widget.settle()),
                "extra_overhead_s": float(self._timing_widget.extra_overhead_s()),
                "ramp_step": float(self._timing_widget.ramp_step()),
                "step_delay_s": float(self._timing_widget.step_delay_s()),
                "advanced_open": bool(self._timing_widget._toggle.isChecked()),
            },
            "optical": {
                "exposure_ms": float(primary.exposure_ms),
                "center_nm": float(primary.center_nm),
                "frames": int(primary.frames),
            },
            "optical_sequence": [
                condition.as_dict() for condition in optical_conditions
            ],
            "metadata": {
                "sample_id": self._sample_edit.text(),
                "tag": self._tag_edit.text(),
                "laser_nm": self._laser_edit.text(),
                "power_uw": self._power_edit.text(),
            },
            "sections": {
                "timing": self._timing_section.is_expanded(),
                "optical": self._optical_group.is_expanded(),
                "metadata": self._metadata_group.is_expanded(),
            },
            "splitter_sizes": [int(v) for v in self._splitter.sizes()],
            "splitter_ratio": float(self._horizontal_splitter_ratio),
        }

    def apply_saved_experiment_settings(self, settings: dict) -> dict:
        allowed = {
            "center_nm": lambda v: self._optical_widget.set_center(float(v)),
            "exp_ms": lambda v: self._optical_widget.set_exposure(float(v)),
            "frames": lambda v: self._optical_widget.set_frames(int(v)),
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

        coord = state.get("coordinate_system")
        if coord == CoordSystem.PHYSICAL.value:
            self._coord_widget._physical.setChecked(True)
        elif coord == CoordSystem.RAW.value:
            self._coord_widget._raw.setChecked(True)
        try:
            self._coord_widget._ratio_spin.setValue(float(state["ratio"]))
        except (KeyError, TypeError, ValueError):
            pass

        self._sync_vbias_availability()
        outer = state.get("outer_axis")
        if isinstance(outer, str) and self._axis_selector._outer.findText(outer) >= 0:
            self._axis_selector._outer.setCurrentText(outer)
        inner = state.get("inner_axis")
        if isinstance(inner, str) and self._axis_selector._inner.findText(inner) >= 0:
            self._axis_selector._inner.setCurrentText(inner)
        if "snake" in state:
            self._axis_selector._snake.setChecked(bool(state["snake"]))
        self._on_axis_selection_changed()

        def restore_axis(widget: _AxisConfigWidget, values: object) -> None:
            if not isinstance(values, dict):
                return
            mode = values.get("mode")
            if isinstance(mode, str) and widget._mode.findText(mode) >= 0:
                widget._mode.setCurrentText(mode)
            for key, spin in (
                ("start", widget._start),
                ("stop", widget._stop),
                ("step", widget._step),
            ):
                try:
                    spin.setValue(float(values[key]))
                except (KeyError, TypeError, ValueError):
                    pass

        restore_axis(self._axis_a, state.get("outer_config"))
        restore_axis(self._axis_b, state.get("inner_config"))

        fixed = state.get("fixed")
        if isinstance(fixed, dict):
            for key, value in fixed.items():
                spin = self._fixed_widget._spins.get(str(key))
                if spin is not None:
                    try:
                        spin.setValue(float(value))
                    except (TypeError, ValueError):
                        pass
        safety = state.get("safety")
        if isinstance(safety, dict):
            for key, value in safety.items():
                spin = self._safety_widget._spins.get(str(key))
                if spin is not None:
                    try:
                        spin.setValue(float(value))
                    except (TypeError, ValueError):
                        pass
        timing = state.get("timing")
        if isinstance(timing, dict):
            for key, spin, scale in (
                ("settle_s", self._timing_widget._settle, 1.0),
                ("extra_overhead_s", self._timing_widget._extra_overhead, 1.0),
                ("ramp_step", self._timing_widget._ramp_step, 1.0),
                ("step_delay_s", self._timing_widget._step_delay_ms, 1000.0),
            ):
                try:
                    spin.setValue(float(timing[key]) * scale)
                except (KeyError, TypeError, ValueError):
                    pass
            if "advanced_open" in timing:
                self._timing_widget._toggle.setChecked(bool(timing["advanced_open"]))

        optical_sequence = state.get("optical_sequence")
        if isinstance(optical_sequence, list) and optical_sequence:
            self._optical_widget.set_conditions(optical_sequence)
            self._sync_primary_optical_aliases()
        else:
            # Older sessions stored only one optical recipe.
            optical = state.get("optical")
            if isinstance(optical, dict):
                try:
                    self._optical_widget.set_conditions([{
                        "enabled": True,
                        "name": "C1",
                        "center_nm": float(optical["center_nm"]),
                        "exposure_ms": float(optical["exposure_ms"]),
                        "frames": int(optical["frames"]),
                    }])
                    self._sync_primary_optical_aliases()
                except (KeyError, TypeError, ValueError):
                    pass
        metadata = state.get("metadata")
        if isinstance(metadata, dict):
            for key, edit in (
                ("sample_id", self._sample_edit),
                ("tag", self._tag_edit),
                ("laser_nm", self._laser_edit),
                ("power_uw", self._power_edit),
            ):
                value = metadata.get(key)
                if isinstance(value, str):
                    edit.setText(value)
        sections = state.get("sections")
        if isinstance(sections, dict):
            for key, section in (
                ("timing", self._timing_section),
                ("optical", self._optical_group),
                ("metadata", self._metadata_group),
            ):
                if key in sections:
                    section.set_expanded(bool(sections[key]))
        ratio = state.get("splitter_ratio")
        try:
            restored_ratio = float(ratio)
        except (TypeError, ValueError):
            restored_ratio = float("nan")
        if not math.isfinite(restored_ratio):
            sizes = state.get("splitter_sizes")
            if isinstance(sizes, list) and len(sizes) == 2:
                try:
                    total = max(0, int(sizes[0])) + max(0, int(sizes[1]))
                    if total > 0:
                        restored_ratio = max(0, int(sizes[0])) / total
                except (TypeError, ValueError):
                    pass
        if math.isfinite(restored_ratio):
            self._horizontal_splitter_ratio = min(
                0.55,
                max(0.25, restored_ratio),
            )
        QTimer.singleShot(
            0,
            lambda: self._apply_responsive_layout(force=True),
        )
        QTimer.singleShot(0, self._refresh_settings_scroll_range)
        self._schedule_preview()

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
        coord = self._coord_widget.coord_system()
        remaining = [axis for axis in axes if axis not in (outer, inner)]
        if not self._vbias_available() and "Vbias" not in remaining:
            remaining.append("Vbias")
        self._fixed_widget.set_fixed_axes(remaining, self._vbias_available())
        self._install_settings_wheel_redirects()
        if outer:
            vds_label = "VDS (Vbias)" if (outer == "Vbias" and coord == CoordSystem.PHYSICAL) else None
            self._axis_a.set_axis_label(outer, AXIS_UNITS.get(outer, "V"), display_name=vds_label)
        if inner:
            vds_label = "VDS (Vbias)" if (inner == "Vbias" and coord == CoordSystem.PHYSICAL) else None
            self._axis_b.set_axis_label(inner, AXIS_UNITS.get(inner, "V"), display_name=vds_label)
        self._update_raw_link_label()
        QTimer.singleShot(0, self._refresh_settings_scroll_range)
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

    def _estimate_duration_s(
        self,
        valid_points: list[dict],
        conditions: list[OpticalCondition] | None = None,
    ) -> float:
        if conditions is None:
            conditions = self._optical_widget.conditions(enabled_only=True)
        ramp_rate = self._timing_widget.ramp_step() / max(self._timing_widget.step_delay_s(), EPS)
        avg_ramp_time = self._timing_widget.ramp_step() / max(ramp_rate, EPS)
        common_per_point = (
            self._timing_widget.settle()
            + avg_ramp_time
            + self._timing_widget.extra_overhead_s()
        )
        return sum(
            len(valid_points) * (
                common_per_point
                + condition.exposure_ms / 1000.0 * condition.frames
            )
            for condition in conditions
        )

    def _format_duration(self, total_s: float) -> str:
        hours = int(total_s // 3600)
        mins = int((total_s % 3600) // 60)
        secs = int(round(total_s % 60))
        if hours:
            return f"~{hours} h {mins} min"
        if mins:
            return f"~{mins} min {secs} sec"
        return f"~{secs} sec"

    def _set_summary_text(self, text: str, color: str = "#444444"):
        self._summary.setStyleSheet(
            "QTextEdit {"
            f" color: {color};"
            " background: #f5f5f7;"
            " border: 1px solid #dedede;"
            " border-radius: 3px;"
            " padding: 4px 6px;"
            "}"
        )
        self._summary.setPlainText(str(text))
        self._summary.setToolTip(str(text))

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
            self._set_summary_text(f"Warning: {exc}", "#b42318")
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
        conditions = self._optical_widget.conditions(enabled_only=True)
        est_s = self._estimate_duration_s(valid_points, conditions)
        preview_condition = conditions[0] if conditions else OpticalCondition(
            False, "None", self._center_spin.value(), self._exp_spin.value(), self._frames_spin.value()
        )
        filename_preview = build_megasweep_filename({
            "coord": data["coord"],
            "axis_a": data["axis_a"],
            "axis_b": data["axis_b"],
            "axis_a_desc": self._axis_a.describe(),
            "axis_b_desc": self._axis_b.describe(),
            "fixed": data["fixed"],
            "ratio": data["ratio"],
            "center_nm": float(preview_condition.center_nm),
            "exp_ms": float(preview_condition.exposure_ms),
            "frames": int(preview_condition.frames),
            "condition_index": 1,
            "condition_count": len(conditions),
            "condition_name": preview_condition.name,
            "laser_nm": self._laser_edit.text().strip(),
            "power_uw": self._power_edit.text().strip(),
            "tag": self._tag_edit.text().strip() or "2DSweep",
            "sample": self._sample_edit.text().strip() or "SampleID",
            "vbias_available": self._vbias_available(),
        })
        sample = self._sample_edit.text().strip()
        folder_preview = Path(cfg.filename.base_out) / sample / "megasweep"
        full_preview = folder_preview / f"{filename_preview}.csv"
        optical_txt = "; ".join(
            f"{index}: {condition.center_nm:g} nm/{condition.exposure_ms:g} ms/{condition.frames} EPF"
            for index, condition in enumerate(conditions, start=1)
        ) or "No enabled conditions"
        lines = [
            self._format_axis_summary(f"Outer ({data['axis_a']})", self._axis_a.describe()),
            self._format_axis_summary(f"Inner ({data['axis_b']})", self._axis_b.describe()),
            f"Fixed:            {fixed_txt}",
            f"Folder:           {folder_preview}",
            f"Filename:         {filename_preview}.csv",
            f"Full path:        {full_preview}",
            f"Optical maps:     {len(conditions)} ({optical_txt})",
            f"Total planned:    {len(all_points)}    In-bounds: {len(valid_points)}    Skipped: {skipped}",
            f"Total spectra:    {len(valid_points) * len(conditions)}",
            f"Est. duration:    {self._format_duration(est_s)}   "
            f"({len(conditions)} complete gate map(s), with a zero ramp between maps)",
            f"Run output cadence: flush every {_FLUSH_EVERY_POINTS} pts, plot every {_PLOT_UPDATE_EVERY_POINTS} pts, UI log every {_UI_LOG_EVERY_POINTS} pts",
        ]
        color = "#5a5a5a"
        if not conditions:
            color = "#b42318"
            lines.append("Warning: Enable at least one optical condition.")
        elif len(valid_points) == 0:
            color = "#b42318"
            lines.append("Warning: No points within the safety limits.")
        elif len(all_points) > 0 and len(valid_points) < max(1, int(0.1 * len(all_points))):
            color = "#ad6700"
            lines.append(f"Warning: {skipped} of {len(all_points)} planned points are out of bounds.")
        elif est_s > 3600:
            color = "#ad6700"
            lines.append(f"Warning: Estimated sweep time is {self._format_duration(est_s)}.")
        self._set_summary_text("\n".join(lines), color)

    def _collect_params(self) -> dict:
        data = dict(self._last_preview)
        sample = self._sample_edit.text().strip() or "SampleID"
        tag = self._tag_edit.text().strip() or "2DSweep"
        conditions = self._optical_widget.conditions()
        enabled_conditions = [c for c in conditions if c.enabled]
        primary = enabled_conditions[0] if enabled_conditions else conditions[0]
        data.update({
            "exp_ms": float(primary.exposure_ms),
            "center_nm": float(primary.center_nm),
            "frames": int(primary.frames),
            "optical_conditions": [
                condition.as_dict() for condition in conditions
            ],
            "sample": sample,
            "tag": tag,
            "snake": self._axis_selector.snake(),
            "laser_nm": self._laser_edit.text().strip(),
            "power_uw": self._power_edit.text().strip(),
            "settle": self._timing_widget.settle(),
            "extra_overhead_s": self._timing_widget.extra_overhead_s(),
            "ramp_step": self._timing_widget.ramp_step(),
            "step_delay_s": self._timing_widget.step_delay_s(),
            "vbias_available": self._vbias_available(),
            "smu_connected": bool(self._smu is not None and self._smu.is_connected),
            "lf6_connected": bool(self._lf6 is not None and self._lf6.is_connected),
            "out_path": Path(cfg.filename.base_out) / sample / "megasweep",
        })
        data.update({
            "condition_index": 1,
            "condition_count": len(enabled_conditions),
            "condition_name": primary.name,
        })
        data["base_name"] = build_megasweep_filename(data)
        return data

    def _validate(self, params: dict) -> bool:
        if not str(params.get("sample", "")).strip():
            QMessageBox.critical(self, "Sample ID required", "Enter a Sample ID before starting the sweep.")
            return False
        enabled_conditions = [
            condition
            for condition in params.get("optical_conditions", [])
            if bool(condition.get("enabled", True))
        ]
        if not enabled_conditions:
            QMessageBox.critical(
                self,
                "No optical conditions",
                "Enable at least one optical condition before running.",
            )
            return False
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
        estimate = self._estimate_duration_s(
            params["valid_points"],
            [
                OpticalCondition(
                    True,
                    str(condition.get("name", "")),
                    float(condition["center_nm"]),
                    float(condition["exposure_ms"]),
                    int(condition["frames"]),
                )
                for condition in enabled_conditions
            ],
        )
        if estimate > 3600:
            reply = QMessageBox.warning(self, "Long sweep", f"Estimated sequence time is {self._format_duration(estimate)}.\nContinue anyway?", QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Cancel)
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
        self._run_csv_before = set(Path(params["out_path"]).glob("*.csv"))
        self._run_files_before = set(Path(params["out_path"]).glob("*"))
        self._run_metadata_params = dict(params)
        try:
            self._experiment_run = ExperimentMetadataService(params["out_path"]).begin(
                "gate_map_2d", str(params.get("sample", "")).strip(),
                output_dir=params["out_path"], settings=params,
            )
        except Exception as exc:
            self._on_error(f"Metadata error; run blocked: {exc}")
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
        self._worker.map_started.connect(self._on_map_started)
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

    @Slot(int, int, str)
    def _on_map_started(
        self,
        map_index: int,
        map_count: int,
        description: str,
    ) -> None:
        self._current_map_index = int(map_index)
        self._map_count = max(1, int(map_count))
        self._preview.clear_progress()
        self._set_status(
            f"Map {map_index}/{map_count}",
            "#b26a00",
        )
        self._status_lbl.setToolTip(description)

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
        run = getattr(self, "_experiment_run", None)
        if run is not None:
            try:
                for data_file in Path(run.path.parent).glob("*"):
                    if data_file == run.path or data_file in getattr(self, "_run_files_before", set()) or data_file.suffix.lower() not in {".csv", ".txt", ".log", ".json"}:
                        continue
                    run.register_file(data_file, "raw" if data_file.suffix.lower() == ".csv" else "intermediate")
                if self._run_failed:
                    run.fail("gate map failed")
                elif worker is not None and worker._stop.is_set():
                    run.cancel("user stop")
                else:
                    run.complete()
            except Exception as exc:
                self._on_error(f"Metadata finalization error: {exc}")
        self._update_ratio_validation()
        self._schedule_preview()

    def _set_status(self, text: str, color: str):
        self._status_lbl.setText(text)
        self._status_lbl.setToolTip(text)
        self._status_lbl.setStyleSheet(f"color: {color}; font-size: 11px;")
