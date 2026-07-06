# ui/power_sweep_panel.py
# ──────────────────────────────────────────────────────────────────────────────
# Power-dependent measurement panel.
#
# Moves the linear stage through user-defined positions. At each position:
#   1. Moves stage to target position
#   2. Reads optical power from PM100D (stored in µW)
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
import math
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QPushButton, QLineEdit, QDoubleSpinBox, QSpinBox,
    QCheckBox, QSplitter, QScrollArea, QProgressBar,
    QTextEdit, QMessageBox, QFrame,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pyqtgraph as pg
from utils.config import cfg
from utils.filename_builder import (
    FilenameContext, build_base_filename, sanitize_token, format_compact_number,
)

pg.setConfigOption("background", "w")
pg.setConfigOption("foreground", "k")

# ── constants ──────────────────────────────────────────────────────────────────
NAN = float("nan")


# ── stop exception ─────────────────────────────────────────────────────────────
class _StopRequested(Exception):
    pass


# ── position parsing ───────────────────────────────────────────────────────────
def _parse_stage_positions(text: str) -> np.ndarray:
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


def _describe_positions(pos: np.ndarray, max_show: int = 5) -> str:
    if pos.size == 0:
        return "[]"
    if pos.size <= max_show * 2:
        return str([round(x, 4) for x in pos.tolist()])
    head = ", ".join(f"{x:.4g}" for x in pos[:max_show])
    tail = ", ".join(f"{x:.4g}" for x in pos[-2:])
    return f"[{head}, ..., {tail}]"


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


# ── CSV helpers ────────────────────────────────────────────────────────────────
_SMU_COLUMNS = [
    "Vbg_set", "Vbg_meas",
    "Vtg_set", "Vtg_meas",
    "Vbias_set", "Vbias_meas",
    "Ibg", "Itg", "Ibias",
]


def _scalar_column_names(smu_available: bool) -> list[str]:
    cols = ["Power_uW", "stage_pos"]
    if smu_available:
        cols.extend(_SMU_COLUMNS)
    return cols


def _read_scalar_row(iv, power_uw: float, pos: float, smu_available: bool,
                    Vbg_set: float = NAN, Vtg_set: float = NAN,
                    Vbias_set: float = NAN) -> list:
    values = [power_uw, pos]
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
    """Runs the power-dependent measurement loop in a background QThread."""

    log = Signal(str)
    progress = Signal(int, int)
    spectrum = Signal(object, object)  # wl, cts
    finished = Signal()
    error = Signal(str)

    def __init__(self, params: dict, stage_ctrl, pm_ctrl, lf6_ctrl, smu_ctrl):
        super().__init__()
        self._p = params
        self._stg = stage_ctrl
        self._pm = pm_ctrl
        self._lf6 = lf6_ctrl
        self._smu = smu_ctrl
        self._stop = threading.Event()

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
        stage = self._stg.adapter
        pm = self._pm.adapter
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
                setup.change_spectra_center(f"{p['center_nm']:.0f}")
                time.sleep(0.15)
                setup.change_expose_time(float(p["exp_ms"]))
                time.sleep(0.10)
                for fn in ("set_accumulations", "set_frames",
                           "change_frame_to_combine"):
                    if hasattr(setup, fn):
                        getattr(setup, fn)(int(p["frames"]))
                        break
            except Exception as exc:
                self.log.emit(f"[{_ts()}] LF6 settings warning: {exc}")

        # ── wavelength calibration ───────────────────────────────────────────
        self.log.emit(f"[{_ts()}] Acquiring wavelength calibration...")
        wls = np.array([])
        if spec is not None:
            try:
                wls = spec.calibration_wavelengths(force=True)
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
        try:
            pm.configure_wavelength(float(p["pm_wl_nm"]))
            self.log.emit(
                f"[{_ts()}] PM wavelength set to {p['pm_wl_nm']:.1f} nm"
            )
        except Exception as exc:
            self.log.emit(f"[{_ts()}] PM wavelength warning: {exc}")

        # ── apply gate voltages ──────────────────────────────────────────────
        if p.get("apply_gates") and iv is not None:
            self.log.emit(f"[{_ts()}] Ramping gates...")
            ramp = p["ramp_step_V"]
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
                        ramp_step=ramp,
                        delay_s=delay,
                        stop_cb=self._stop.is_set,
                        stop_exc=_StopRequested,
                    )
                time.sleep(p["settle_s"])
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

        # ── create output file ───────────────────────────────────────────────
        out_path = Path(p["out_path"])
        out_path.mkdir(parents=True, exist_ok=True)
        fp = out_path / f"{p['base_name']}.csv"
        k = 2
        while fp.exists():
            fp = out_path / f"{p['base_name']}_{k:03d}.csv"
            k += 1

        cols = _scalar_column_names(smu_ok)
        wl_headers = [f"{float(w):.4f}" for w in wls]

        with open(fp, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(cols + wl_headers)
        self.log.emit(f"[{_ts()}] Writing to {fp.name}")

        # ── main loop ────────────────────────────────────────────────────────
        positions = p["positions"]
        total = len(positions)
        nwls = len(wls)

        try:
            with open(fp, "a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                for done, pos in enumerate(positions, start=1):
                    if self._stop.is_set():
                        raise _StopRequested()

                    self.log.emit(
                        f"[{_ts()}] {done}/{total}: moving stage to {pos:.3f}"
                    )

                    # move stage
                    move_ok = True
                    try:
                        stage.move_to(pos)
                        time.sleep(0.3)
                        arrived = float(stage.get_position())
                        self.log.emit(
                            f"[{_ts()}]   arrived at {arrived:.3f}"
                        )
                    except Exception as exc:
                        self.log.emit(f"[{_ts()}]   stage error: {exc}")
                        move_ok = False

                    # read power
                    power_uw = NAN
                    if move_ok:
                        try:
                            power_w = float(pm.get_power())
                            power_uw = power_w * 1e6
                            self.log.emit(
                                f"[{_ts()}]   power: {power_uw:.3f} µW"
                            )
                        except Exception as exc:
                            self.log.emit(
                                f"[{_ts()}]   power error: {exc}"
                            )

                    # acquire spectrum
                    if move_ok:
                        try:
                            wl, y = spec.acquire()
                            self.spectrum.emit(wl, y)
                        except Exception as exc:
                            self.log.emit(
                                f"[{_ts()}]   acquire error: {exc}"
                            )
                            y = np.full(nwls, NAN, dtype=float)
                    else:
                        y = np.full(nwls, NAN, dtype=float)

                    # write row
                    scalar_vals = _read_scalar_row(
                        iv, power_uw, pos, smu_ok,
                        Vbg_set=p.get("Vbg_target", NAN),
                        Vtg_set=p.get("Vtg_target", NAN),
                        Vbias_set=p.get("Vbias_target", NAN),
                    )
                    row = [_csv_cell(v) for v in scalar_vals]
                    row.extend(_csv_cell(float(v)) for v in y)
                    writer.writerow(row)
                    fh.flush()

                    self.progress.emit(done, total)

        except _StopRequested:
            self.log.emit(f"[{_ts()}] Stopped by user.")

        finally:
            # return gates to zero
            if p.get("return_to_zero", True) and iv is not None:
                try:
                    iv.ramp_all_to_zero(
                        ramp_step=p.get("ramp_step_V", 0.1),
                        delay_s=0.02,
                    )
                    self.log.emit(
                        f"[{_ts()}] Gates returned to 0 V."
                    )
                except Exception as exc:
                    self.log.emit(
                        f"[{_ts()}] Gate return-to-zero failed: {exc}"
                    )
            # return stage to minimum position
            try:
                stage.move_to(stage.minimum_position)
                self.log.emit(
                    f"[{_ts()}] Stage returned to minimum position."
                )
            except Exception as exc:
                self.log.emit(f"[{_ts()}] Stage return failed: {exc}")

        if self._stop.is_set():
            self.log.emit(
                f"[{_ts()}] Stopped. Partial data saved → {fp.name}"
            )
        else:
            self.log.emit(f"[{_ts()}] Done. Saved → {fp.name}")


# ── panel ──────────────────────────────────────────────────────────────────────
class PowerSweepPanel(QWidget):
    """Power-dependent measurement tab.

    Usage:
        panel = PowerSweepPanel(
            lf6_ctrl=lf6, stage_ctrl=stg, pm_ctrl=pm, smu_ctrl=smu
        )
    """

    def __init__(
        self,
        lf6_ctrl=None,
        stage_ctrl=None,
        pm_ctrl=None,
        smu_ctrl=None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._lf6 = lf6_ctrl
        self._stg = stage_ctrl
        self._pm = pm_ctrl
        self._smu = smu_ctrl
        self._worker: Optional[_PowerSweepWorker] = None
        self._thread: Optional[QThread] = None
        self._positions: np.ndarray = np.array([], dtype=float)
        self._parse_timer = QTimer(self)
        self._parse_timer.setSingleShot(True)
        self._parse_timer.setInterval(120)
        self._parse_timer.timeout.connect(self._update_position_preview)
        self._build()
        self._wire()
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
        scroll.setMinimumWidth(350)
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(4, 4, 4, 4)
        left_lay.setSpacing(3)
        scroll.setWidget(left)
        splitter.addWidget(scroll)

        # Stage positions
        pos_grp = QGroupBox("Stage Positions")
        pos_form = QFormLayout(pos_grp)
        pos_form.setContentsMargins(6, 4, 6, 4)
        pos_form.setSpacing(3)
        self._pos_input = QLineEdit("(0, 50, 51)")
        self._pos_input.setToolTip(
            "Tuple (start, stop, count) → linspace\n"
            "List [v1, v2, ...] → exact positions\n"
            "Single number → one position"
        )
        pos_form.addRow("Input:", self._pos_input)
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
        left_lay.addWidget(pos_grp)

        # Optical settings
        opt_grp = QGroupBox("Optical Settings")
        opt_form = QFormLayout(opt_grp)
        opt_form.setContentsMargins(6, 4, 6, 4)
        opt_form.setSpacing(3)
        self._center_spin = QDoubleSpinBox()
        self._center_spin.setRange(200, 2000)
        self._center_spin.setDecimals(2)
        self._center_spin.setValue(cfg.lf6.center_nm)
        self._center_spin.setSuffix(" nm")
        opt_form.addRow("Center λ:", self._center_spin)
        self._exp_spin = QDoubleSpinBox()
        self._exp_spin.setRange(1, 600_000)
        self._exp_spin.setDecimals(1)
        self._exp_spin.setValue(cfg.lf6.exposure_ms)
        self._exp_spin.setSuffix(" ms")
        opt_form.addRow("Exposure:", self._exp_spin)
        self._frames_spin = QSpinBox()
        self._frames_spin.setRange(1, 100000)
        self._frames_spin.setValue(cfg.lf6.accumulations)
        opt_form.addRow("Frames/EPF:", self._frames_spin)
        self._pm_wl_spin = QDoubleSpinBox()
        self._pm_wl_spin.setRange(200, 1100)
        self._pm_wl_spin.setDecimals(1)
        self._pm_wl_spin.setValue(730.0)
        self._pm_wl_spin.setSuffix(" nm")
        opt_form.addRow("PM λ:", self._pm_wl_spin)
        left_lay.addWidget(opt_grp)

        # Gate settings (SMU)
        self._gate_grp = QGroupBox("Gate Settings")
        gate_form = QFormLayout(self._gate_grp)
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
        self._ramp_step_spin = QDoubleSpinBox()
        self._ramp_step_spin.setRange(0.001, 10.0)
        self._ramp_step_spin.setDecimals(3)
        self._ramp_step_spin.setValue(cfg.ramp.step_V)
        self._ramp_step_spin.setSuffix(" V")
        gate_form.addRow("Ramp step:", self._ramp_step_spin)
        self._settle_spin = QDoubleSpinBox()
        self._settle_spin.setRange(0.0, 60.0)
        self._settle_spin.setDecimals(3)
        self._settle_spin.setValue(cfg.ramp.settle_s)
        self._settle_spin.setSuffix(" s")
        gate_form.addRow("Settle:", self._settle_spin)
        self._apply_gates_chk = QCheckBox("Apply gate voltages before sweep")
        self._apply_gates_chk.setChecked(True)
        gate_form.addRow("", self._apply_gates_chk)
        self._return_zero_chk = QCheckBox("Return gates to 0 V after sweep")
        self._return_zero_chk.setChecked(True)
        gate_form.addRow("", self._return_zero_chk)
        self._gate_grp.setEnabled(False)
        left_lay.addWidget(self._gate_grp)

        # File / metadata
        meta_grp = QGroupBox("File / Metadata")
        meta_form = QFormLayout(meta_grp)
        meta_form.setContentsMargins(6, 4, 6, 4)
        meta_form.setSpacing(3)
        self._devid_edit = QLineEdit()
        self._devid_edit.setPlaceholderText("Sample ID")
        meta_form.addRow("Sample ID:", self._devid_edit)
        self._point_edit = QLineEdit()
        self._point_edit.setPlaceholderText("optional")
        meta_form.addRow("Point:", self._point_edit)
        self._laser_edit = QLineEdit("730")
        meta_form.addRow("Laser (nm):", self._laser_edit)
        self._subfolder_edit = QLineEdit("power_sweep")
        meta_form.addRow("Subfolder:", self._subfolder_edit)
        self._filename_lbl = QLabel("")
        self._filename_lbl.setStyleSheet(
            "color: #555; font-size: 10px;"
        )
        self._filename_lbl.setWordWrap(True)
        meta_form.addRow("Preview:", self._filename_lbl)
        self._est_lbl = QLabel("")
        self._est_lbl.setStyleSheet("color: gray; font-size: 10px;")
        meta_form.addRow("", self._est_lbl)
        left_lay.addWidget(meta_grp)

        # Hardware status
        hw_grp = QGroupBox("Hardware Status")
        hw_form = QFormLayout(hw_grp)
        hw_form.setContentsMargins(6, 4, 6, 4)
        hw_form.setSpacing(2)
        self._stage_status = QLabel("○ Not connected")
        self._stage_status.setStyleSheet("color: gray; font-weight: bold;")
        hw_form.addRow("Stage:", self._stage_status)
        self._pm_status = QLabel("○ Not connected")
        self._pm_status.setStyleSheet("color: gray; font-weight: bold;")
        hw_form.addRow("PM:", self._pm_status)
        self._lf6_status = QLabel("○ Not connected")
        self._lf6_status.setStyleSheet("color: gray; font-weight: bold;")
        hw_form.addRow("LF6:", self._lf6_status)
        self._smu_status = QLabel("○ Not connected")
        self._smu_status.setStyleSheet("color: gray; font-weight: bold;")
        hw_form.addRow("SMU:", self._smu_status)
        left_lay.addWidget(hw_grp)

        left_lay.addStretch(1)

        # ── right: output ────────────────────────────────────────────────────
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(6, 4, 4, 4)
        right_lay.setSpacing(6)
        splitter.addWidget(right)
        splitter.setSizes([400, 600])

        # Log
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(200)
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

        # Controls
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(8)
        self._run_btn = QPushButton("▶  Run Power Sweep")
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

        # Controller signals → status lamps
        if self._stg is not None:
            self._stg.connected.connect(self._on_stage_connected)
            self._stg.disconnected.connect(self._on_stage_disconnected)
            if self._stg.is_connected:
                self._on_stage_connected(
                    getattr(self._stg, "backend_key", "")
                )
        if self._pm is not None:
            self._pm.connected.connect(self._on_pm_connected)
            self._pm.disconnected.connect(self._on_pm_disconnected)
            if self._pm.is_connected:
                self._on_pm_connected()
        if self._lf6 is not None:
            self._lf6.connected.connect(self._on_lf6_connected)
            self._lf6.disconnected.connect(self._on_lf6_disconnected)
            if self._lf6.is_connected:
                self._on_lf6_connected([])

        if self._smu is not None:
            self._smu.connected.connect(self._on_smu_connected)
            self._smu.disconnected.connect(self._on_smu_disconnected)
            if self._smu.is_connected:
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
            "center_nm": float(self._center_spin.value()),
            "exposure_ms": float(self._exp_spin.value()),
            "frames": int(self._frames_spin.value()),
            "pm_wavelength_nm": float(self._pm_wl_spin.value()),
            "vbg": float(self._vbg_spin.value()),
            "vtg": float(self._vtg_spin.value()),
            "vbias": float(self._vbias_spin.value()),
            "ramp_step": float(self._ramp_step_spin.value()),
            "settle_s": float(self._settle_spin.value()),
            "apply_gates": bool(self._apply_gates_chk.isChecked()),
            "return_zero": bool(self._return_zero_chk.isChecked()),
            "sample_id": self._devid_edit.text(),
            "point": self._point_edit.text(),
            "laser_nm": self._laser_edit.text(),
            "subfolder": self._subfolder_edit.text(),
            "splitter_sizes": [int(v) for v in self._splitter.sizes()],
        }

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
        }
        for key, widget in text_fields.items():
            value = state.get(key)
            if isinstance(value, str):
                widget.setText(value)
        for key, widget in (
            ("center_nm", self._center_spin),
            ("exposure_ms", self._exp_spin),
            ("frames", self._frames_spin),
            ("pm_wavelength_nm", self._pm_wl_spin),
            ("vbg", self._vbg_spin),
            ("vtg", self._vtg_spin),
            ("vbias", self._vbias_spin),
            ("ramp_step", self._ramp_step_spin),
            ("settle_s", self._settle_spin),
        ):
            set_number(widget, key)
        if "apply_gates" in state:
            self._apply_gates_chk.setChecked(bool(state["apply_gates"]))
        if "return_zero" in state:
            self._return_zero_chk.setChecked(bool(state["return_zero"]))
        sizes = state.get("splitter_sizes")
        if isinstance(sizes, list) and len(sizes) == 2:
            try:
                self._splitter.setSizes([max(0, int(v)) for v in sizes])
            except (TypeError, ValueError):
                pass
        self._update_position_preview()
        self._update_filename_preview()

    # ── position preview ──────────────────────────────────────────────────────

    @Slot()
    def _update_position_preview(self):
        text = self._pos_input.text()
        try:
            self._positions = _parse_stage_positions(text)
        except Exception as exc:
            self._pos_preview_lbl.setText(f"⚠ {exc}")
            self._pos_preview_lbl.setStyleSheet(
                "color: #b42318; font-size: 10px;"
            )
            self._pos_count_lbl.setText("")
            self._est_lbl.setText("")
            return

        self._pos_preview_lbl.setText(
            _describe_positions(self._positions)
        )
        self._pos_preview_lbl.setStyleSheet(
            "color: #444; font-size: 10px;"
        )
        n = len(self._positions)
        self._pos_count_lbl.setText(
            f"{n} position{'s' if n != 1 else ''}"
        )
        self._update_est()
        self._update_filename_preview()

    def _update_est(self):
        if self._positions.size == 0:
            self._est_lbl.setText("")
            return
        n = len(self._positions)
        exp_s = self._exp_spin.value() / 1000.0 * self._frames_spin.value()
        per_pt = 1.5 + exp_s
        total_s = n * per_pt
        if total_s < 60:
            self._est_lbl.setText(f"Est: ~{total_s:.0f} s ({n} pos)")
        elif total_s < 3600:
            m = total_s // 60
            s = int(total_s % 60)
            self._est_lbl.setText(f"Est: ~{m:.0f} min {s} s ({n} pos)")
        else:
            h = total_s // 3600
            m = int((total_s % 3600) // 60)
            self._est_lbl.setText(
                f"Est: ~{h:.0f} h {m} min ({n} pos)"
            )

    # ── filename preview ──────────────────────────────────────────────────────

    @Slot()
    def _update_filename_preview(self):
        try:
            devid = self._devid_edit.text().strip()
            sub = self._subfolder_edit.text().strip() or "power_sweep"
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
                condition_label="power_sweep",
            )
            enabled = ["laser_power", "center", "exposure", "condition"]
            base = build_base_filename(ctx, enabled)
            # append gate token from readback
            if self._apply_gates_chk.isChecked() and \
               self._smu is not None and self._smu.is_connected:
                vbg, vtg, vbias = self._read_gate_snapshot()
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

    @Slot()
    def _on_stage_disconnected(self):
        self._stage_status.setText("○ Not connected")
        self._stage_status.setStyleSheet(
            "color: gray; font-weight: bold;"
        )

    @Slot()
    def _on_pm_connected(self):
        self._pm_status.setText("● Connected")
        self._pm_status.setStyleSheet(
            "color: green; font-weight: bold;"
        )

    @Slot()
    def _on_pm_disconnected(self):
        self._pm_status.setText("○ Not connected")
        self._pm_status.setStyleSheet(
            "color: gray; font-weight: bold;"
        )

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
        if self._smu is None or not self._smu.is_connected:
            return 0.0, 0.0, 0.0
        iv = self._smu.device
        vbg, vtg = _read_gates(iv)
        vbias = _read_bias(iv)
        return vbg, vtg, vbias

    # ── validation ────────────────────────────────────────────────────────────

    def _validate(self) -> bool:
        if self._positions.size == 0:
            QMessageBox.critical(
                self, "No positions",
                "Enter at least one stage position."
            )
            return False

        if self._stg is None or not self._stg.is_connected:
            QMessageBox.critical(
                self, "Stage not connected",
                "Connect the linear stage before running a power sweep."
            )
            return False

        if self._pm is None or not self._pm.is_connected:
            QMessageBox.critical(
                self, "Power meter not connected",
                "Connect the PM100D before running a power sweep."
            )
            return False

        if self._lf6 is None or not self._lf6.is_connected:
            QMessageBox.critical(
                self, "LF6 not connected",
                "Connect the spectrometer before running a power sweep."
            )
            return False

        if self._apply_gates_chk.isChecked():
            if self._smu is None or not self._smu.is_connected:
                QMessageBox.critical(
                    self, "SMU not connected",
                    "Gate voltages are requested but SMU is not connected.\n"
                    "Connect the SMU or uncheck \"Apply gate voltages\"."
                )
                return False

        # Check positions against stage limits
        adapter = self._stg.adapter
        lo, hi = adapter.minimum_position, adapter.maximum_position
        unit = getattr(adapter, "position_unit", "units")
        for i, pos in enumerate(self._positions):
            if pos < lo or pos > hi:
                QMessageBox.critical(
                    self, "Position out of range",
                    f"Position {pos:.3f} (index {i}) is outside the\n"
                    f"stage range [{lo:g}, {hi:g}] {unit}."
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
        if not self._validate():
            return

        devid = self._devid_edit.text().strip() or "SampleID"
        sub = self._subfolder_edit.text().strip() or "power_sweep"
        out_path = str(Path(cfg.filename.base_out) / devid / sub)

        # read gate snapshot for filename token + cache setpoints for CSV
        apply_gates = self._apply_gates_chk.isChecked()
        vbg_set = self._vbg_spin.value()
        vtg_set = self._vtg_spin.value()
        vbias_set = self._vbias_spin.value()
        vbg_snap, vtg_snap, vbias_snap = self._read_gate_snapshot()

        try:
            ctx = FilenameContext(
                device_id=devid,
                tag="",
                temperature="",
                mode="",
                laser_nm=self._laser_edit.text().strip(),
                nominal_power_uw=None,
                center_nm=self._center_spin.value(),
                exposure_ms=self._exp_spin.value(),
                accumulations=self._frames_spin.value(),
                condition_label="power_sweep",
                point=self._point_edit.text().strip(),
            )
            enabled = ["laser_power", "center", "exposure", "condition"]
            base_name = build_base_filename(ctx, enabled)
            if apply_gates and \
               self._smu is not None and self._smu.is_connected:
                gt = _build_gate_token(vbg_snap, vtg_snap, vbias_snap)
                if gt:
                    base_name = f"{base_name}_{gt}"
        except Exception as exc:
            QMessageBox.critical(
                self, "Filename error",
                f"Could not build filename: {exc}"
            )
            return

        params = {
            "positions": self._positions,
            "center_nm": self._center_spin.value(),
            "exp_ms": self._exp_spin.value(),
            "frames": self._frames_spin.value(),
            "pm_wl_nm": self._pm_wl_spin.value(),
            "out_path": out_path,
            "base_name": base_name,
            # gate settings
            "apply_gates": apply_gates,
            "Vbg_target": vbg_set,
            "Vtg_target": vtg_set,
            "Vbias_target": vbias_set,
            "ramp_step_V": self._ramp_step_spin.value(),
            "settle_s": self._settle_spin.value(),
            "return_to_zero": self._return_zero_chk.isChecked(),
        }

        self._run_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._progress.setValue(0)
        self._log.clear()
        self._status_lbl.setText("Running…")
        self._status_lbl.setStyleSheet("color: #b26a00; font-size: 11px;")

        self._worker = _PowerSweepWorker(
            params, self._stg, self._pm, self._lf6, self._smu,
        )
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.log.connect(self._on_log)
        self._worker.progress.connect(self._on_progress)
        self._worker.spectrum.connect(self._on_spectrum)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._thread.start()

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

    @Slot()
    def _on_finished(self):
        self._run_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        if self._status_lbl.text() not in ("Error", "Stopping…"):
            self._status_lbl.setText("Ready")
            self._status_lbl.setStyleSheet(
                "color: #707070; font-size: 11px;"
            )
        if self._thread:
            self._thread.quit()
            self._thread.wait()
            self._thread = None
        self._worker = None
