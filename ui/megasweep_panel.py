# ui/megasweep_panel.py
# ──────────────────────────────────────────────────────────────────────────────
# MegaSweep panel: 2-axis voltage gate sweep (Vtg/Vbg stripes or D/Vbias).
#
# Two sweep modes:
#   1. Vtg stripes & Vbg  – outer Vtg, inner Vbg, safety-clipped
#   2. D & Vbias (fixed F) – outer D = r*Vtg+Vbg, inner Vbias, constant F
#
# Preview: pyqtgraph scatter plot showing planned path, in/out-of-bounds,
#           run-order colour, live progress overlay.
#
# Run loop in QThread worker – stop via threading.Event.
# CSV written via numpy.savetxt (raw float rows, wavelength header).
#
# Rules:
#   - No instrument state stored here.
#   - importlib.reload() safe.
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import math
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QPushButton, QLineEdit, QDoubleSpinBox, QSpinBox,
    QCheckBox, QComboBox, QTextEdit, QProgressBar, QSplitter,
    QScrollArea, QSizePolicy, QTabWidget,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pyqtgraph as pg
from utils.config import cfg

pg.setConfigOption("background", "w")
pg.setConfigOption("foreground", "k")

NAN = float("nan")


# ── Pure maths helpers (no Qt) ────────────────────────────────────────────────

def _get_linear_array(start: float, stop: float, param: float, mode: str) -> np.ndarray:
    """Return array by total points or fixed step size (inclusive of stop)."""
    if mode == "Total Points":
        return np.linspace(start, stop, max(2, int(param)))
    step = float(param)
    if step <= 0:
        return np.array([start])
    step = abs(step) if stop >= start else -abs(step)
    n = int(np.floor((stop - start) / step) + 1.00001)
    vals = start + np.arange(n) * step
    if (step > 0 and vals[-1] < stop - 1e-12) or (step < 0 and vals[-1] > stop + 1e-12):
        vals = np.append(vals, stop)
    return vals


def _arange_inclusive(start: float, stop: float, step: float) -> np.ndarray:
    if step == 0:
        return np.array([start])
    n = int(np.floor((stop - start) / step + 1e-9)) + 1
    vals = start + np.arange(max(1, n)) * step
    if len(vals):
        if (step > 0 and vals[-1] < stop - 1e-12) or (step < 0 and vals[-1] > stop + 1e-12):
            vals = np.append(vals, stop)
    return vals


def _df_to_vtg_vbg(D: float, F: float, r: float) -> Tuple[float, float]:
    r = float(r)
    if abs(r) < 1e-12:
        raise ValueError("r ≈ 0")
    Vbg = 0.5 * (D - F)
    Vtg = 0.5 * (D + F) / r
    return Vtg, Vbg


def _build_sweep_points_vtgvbg(
    outer_vals: np.ndarray, inner_vals: np.ndarray,
    lim_vtg_min: float, lim_vtg_max: float,
    lim_vbg_min: float, lim_vbg_max: float,
    snake: bool,
) -> List[Tuple[float, float]]:
    pts = []
    for i, vtg in enumerate(outer_vals):
        seq = inner_vals[::-1] if (snake and i % 2 == 1) else inner_vals
        for vbg in seq:
            if (lim_vtg_min <= vtg <= lim_vtg_max) and (lim_vbg_min <= vbg <= lim_vbg_max):
                pts.append((float(vtg), float(vbg)))
    return pts


def _build_full_path_vtgvbg(
    outer_vals: np.ndarray, inner_vals: np.ndarray, snake: bool
) -> List[Tuple[float, float]]:
    pts = []
    for i, vtg in enumerate(outer_vals):
        seq = inner_vals[::-1] if (snake and i % 2 == 1) else inner_vals
        for vbg in seq:
            pts.append((float(vtg), float(vbg)))
    return pts


def _build_dvbias_valid(
    D_vals: np.ndarray, F_fixed: float, r: float,
    lim_vtg_min: float, lim_vtg_max: float,
    lim_vbg_min: float, lim_vbg_max: float,
) -> Tuple[List[float], List[float], List[float]]:
    valid_D, valid_vtg, valid_vbg = [], [], []
    for D in D_vals:
        try:
            vtg, vbg = _df_to_vtg_vbg(D, F_fixed, r)
        except Exception:
            continue
        if (lim_vtg_min <= vtg <= lim_vtg_max) and (lim_vbg_min <= vbg <= lim_vbg_max):
            valid_D.append(float(D))
            valid_vtg.append(float(vtg))
            valid_vbg.append(float(vbg))
    return valid_D, valid_vtg, valid_vbg


def _fmt_uA(I: float) -> str:
    try:
        I = float(I)
        return f"{I * 1e6:.3f}" if math.isfinite(I) else "nan"
    except Exception:
        return "nan"


def _read_gates(iv) -> Tuple[float, float]:
    if iv is None or not hasattr(iv, "read_current_gates"):
        return NAN, NAN
    try:
        bg, tg = iv.read_current_gates()
        return (float(bg) if bg is not None else NAN,
                float(tg) if tg is not None else NAN)
    except Exception:
        return NAN, NAN


def _read_bias(iv) -> float:
    if iv is None:
        return NAN
    if hasattr(iv, "read_current_bias"):
        try:
            return float(iv.read_current_bias())
        except Exception:
            return NAN
    return NAN


def _read_currents(iv) -> Tuple[float, float, float]:
    if iv is None or not hasattr(iv, "read_currents"):
        return NAN, NAN, NAN
    try:
        Ibg, Itg, Ib = iv.read_currents()
        def _f(x):
            try:
                v = float(x)
                return v if math.isfinite(v) else NAN
            except Exception:
                return NAN
        return _f(Ibg), _f(Itg), _f(Ib)
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
    raise TimeoutError(f"λ not converged to {target_nm} nm in {timeout_s}s")


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
    sp = spec.acquire()
    if isinstance(sp, tuple) and len(sp) >= 2:
        y = np.asarray(sp[1]).ravel()
    elif isinstance(sp, dict):
        for k in ("intensity", "y", "counts", "data"):
            if k in sp:
                y = np.asarray(sp[k]).ravel()
                break
        else:
            y = np.asarray(sp).ravel()
    else:
        arr = np.asarray(sp).ravel()
        y = arr
    # pad / trim
    if y.size > expected_len:
        return y[:expected_len].astype(float)
    if y.size < expected_len:
        return np.pad(y, (0, expected_len - y.size)).astype(float)
    return y.astype(float)


# ── Sweep worker ──────────────────────────────────────────────────────────────

class _MegaSweepWorker(QObject):
    log      = Signal(str)
    progress = Signal(int, int)   # done, total
    point_xy = Signal(float, float, int)  # vtg, vbg, done_count  (for live overlay)
    finished = Signal()
    error    = Signal(str)

    def __init__(
        self,
        params: dict,           # all sweep params frozen at run-start
        smu_ctrl=None,
        lf6_ctrl=None,
    ):
        super().__init__()
        self._p      = params
        self._smu    = smu_ctrl
        self._lf6    = lf6_ctrl
        self._stop   = threading.Event()

    def request_stop(self):
        self._stop.set()

    def _ts(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _emit_log(self, msg: str):
        self.log.emit(f"[{self._ts()}] {msg}")

    @Slot()
    def run(self):
        p = self._p
        try:
            self._run_sweep(p)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()

    def _run_sweep(self, p: dict):
        iv   = self._smu.device   if self._smu and self._smu.is_connected else None
        spec = self._lf6.adapter  if self._lf6 and self._lf6.is_connected else None
        lf6  = self._lf6.setup    if self._lf6 and self._lf6.is_connected else None

        mode  = p["mode"]
        snake = p["snake"]
        go_step    = p["go_step"]
        go_delay   = p["go_delay"]
        settle     = p["settle"]
        lim_vtg_min = p["lim_vtg_min"]
        lim_vtg_max = p["lim_vtg_max"]
        lim_vbg_min = p["lim_vbg_min"]
        lim_vbg_max = p["lim_vbg_max"]

        out_path: Path = p["out_path"]
        out_path.mkdir(parents=True, exist_ok=True)

        # ── Apply LF6 settings ────────────────────────────────────────────────
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
            except Exception as e:
                self._emit_log(f"LF6 settings warning: {e}")

        # ── Wavelength calibration ────────────────────────────────────────────
        self._emit_log("Acquiring wavelength calibration…")
        wls = _get_wavelengths(spec, lf6, float(p["center_nm"]), tol_nm=1.0)
        if wls.size <= 2:
            raise RuntimeError("Could not obtain wavelength calibration. Aborting.")
        self._emit_log(f"λ range: {wls[0]:.2f}–{wls[-1]:.2f} nm  ({wls.size} pixels)")

        if self._stop.is_set():
            self._emit_log("Stopped before start.")
            return

        # ── Build file path ───────────────────────────────────────────────────
        stem = p["base_name"]
        # simple uniqueness: append counter if file exists
        fp = out_path / f"{stem}.csv"
        k = 2
        while fp.exists():
            fp = out_path / f"{stem}_{k:03d}.csv"
            k += 1

        # ── Mode dispatch ─────────────────────────────────────────────────────
        if mode == "D & Vbias":
            self._run_dvbias(p, iv, spec, wls, fp, go_step, go_delay, settle,
                             lim_vtg_min, lim_vtg_max, lim_vbg_min, lim_vbg_max, snake)
        else:
            self._run_vtgvbg(p, iv, spec, wls, fp, go_step, go_delay, settle,
                             lim_vtg_min, lim_vtg_max, lim_vbg_min, lim_vbg_max, snake)

    # ── Vtg/Vbg stripe sweep ──────────────────────────────────────────────────

    def _run_vtgvbg(self, p, iv, spec, wls, fp, go_step, go_delay, settle,
                    vtg_min, vtg_max, vbg_min, vbg_max, snake):
        outer_vals = p["outer_vals"]
        inner_vals = p["inner_vals"]
        enable_vbias = p["enable_vbias"]
        vbias_set    = float(p["vbias_set"])
        vbias_step   = float(p["vbias_ramp_step"])

        pts = _build_sweep_points_vtgvbg(
            outer_vals, inner_vals, vtg_min, vtg_max, vbg_min, vbg_max, snake
        )
        if not pts:
            raise RuntimeError("No sweep points within safety limits.")

        total = len(pts)
        self._emit_log(f"Vtg/Vbg sweep: {total} points.")

        # header
        cols = ["Vbg_meas", "Vtg_meas", "Vbias_meas", "Ibg_A", "Itg_A", "Ibias_A"]
        wl_str = np.array([f"{x:g}" for x in wls], dtype="U")
        h_row = np.concatenate((np.array(cols, dtype="U"), wl_str)).reshape(1, -1)
        with open(fp, "w", newline="") as f:
            np.savetxt(f, h_row, fmt="%s", delimiter=",")

        if iv is not None:
            if enable_vbias:
                iv.set_bias(Vbias=vbias_set, delay_s=go_delay, ramp_step=vbias_step)
            vtg0, vbg0 = pts[0]
            iv.set_gates(Vtg=vtg0, delay_s=go_delay, ramp_step=go_step)
            iv.set_gates(Vbg=vbg0, delay_s=go_delay, ramp_step=go_step)
            time.sleep(settle)

        done = 0
        current_vtg = None
        with open(fp, "a", newline="") as f:
            for vtg, vbg in pts:
                if self._stop.is_set():
                    self._emit_log("Stopped by user.")
                    break

                if iv is not None:
                    if current_vtg is None or vtg != current_vtg:
                        iv.set_gates(Vtg=vtg, delay_s=go_delay, ramp_step=go_step)
                        current_vtg = vtg
                    iv.set_gates(Vbg=vbg, delay_s=go_delay, ramp_step=go_step)
                    time.sleep(settle)

                vbg_m, vtg_m = _read_gates(iv)
                vbias_m = _read_bias(iv) if enable_vbias else NAN
                Ibg, Itg, Ib = _read_currents(iv)
                y = _read_intensity(spec, int(wls.size)) if spec else np.zeros(wls.size)

                prefix = np.array([vbg_m, vtg_m, vbias_m, Ibg, Itg, Ib], dtype=np.float64)
                row = np.concatenate((prefix, y)).reshape(1, -1)
                np.savetxt(f, row, fmt="%.6e", delimiter=",")
                f.flush()

                done += 1
                self.progress.emit(done, total)
                self.point_xy.emit(vtg, vbg, done)
                self._emit_log(
                    f"{done}/{total}: Vtg={vtg:.3f}, Vbg={vbg:.3f} | "
                    f"meas Vtg={vtg_m:.3f}, Vbg={vbg_m:.3f}, Vb={vbias_m:.3f} | "
                    f"Itg={_fmt_uA(Itg)} µA, Ibg={_fmt_uA(Ibg)} µA, Ib={_fmt_uA(Ib)} µA"
                )

        self._ramp_to_zero(iv, go_step, go_delay, vbias_step)
        self._emit_log(f"Done. Saved → {fp.name}")

    # ── D & Vbias sweep ───────────────────────────────────────────────────────

    def _run_dvbias(self, p, iv, spec, wls, fp, go_step, go_delay, settle,
                    vtg_min, vtg_max, vbg_min, vbg_max, snake):
        r       = float(p["ratio"])
        F_fixed = float(p["F_fixed"])
        Vb_vals = p["Vb_vals"]
        vbias_step = float(p["vbias_ramp_step"])

        valid_D, valid_vtg, valid_vbg = _build_dvbias_valid(
            p["D_vals"], F_fixed, r, vtg_min, vtg_max, vbg_min, vbg_max
        )
        if not valid_D:
            raise RuntimeError("No D points within safety limits.")
        vb_base = [float(x) for x in Vb_vals]
        if not vb_base:
            raise RuntimeError("Vbias grid is empty.")

        total = len(valid_D) * len(vb_base)
        self._emit_log(f"D&Vbias sweep: {len(valid_D)} D stripes × {len(vb_base)} Vbias = {total} pts (F={F_fixed:.3f})")

        cols = ["D_set", "F_set", "Vbias_set",
                "Vbg_meas", "Vtg_meas", "Vbias_meas",
                "Ibg_A", "Itg_A", "Ibias_A"]
        wl_str = np.array([f"{x:g}" for x in wls], dtype="U")
        h_row = np.concatenate((np.array(cols, dtype="U"), wl_str)).reshape(1, -1)
        with open(fp, "w", newline="") as f:
            np.savetxt(f, h_row, fmt="%s", delimiter=",")

        if iv is not None:
            iv.set_gates(Vtg=valid_vtg[0], Vbg=valid_vbg[0], delay_s=go_delay, ramp_step=go_step)
            iv.set_bias(Vbias=vb_base[0], delay_s=go_delay, ramp_step=vbias_step)
            time.sleep(settle)

        done = 0
        with open(fp, "a", newline="") as f:
            for i, (D, vtg, vbg) in enumerate(zip(valid_D, valid_vtg, valid_vbg)):
                if self._stop.is_set():
                    self._emit_log("Stopped by user.")
                    break

                if iv is not None:
                    iv.set_gates(Vtg=vtg, Vbg=vbg, delay_s=go_delay, ramp_step=go_step)

                vb_seq = list(reversed(vb_base)) if (snake and i % 2 == 1) else vb_base
                for vb in vb_seq:
                    if self._stop.is_set():
                        break

                    if iv is not None:
                        iv.set_bias(Vbias=vb, delay_s=go_delay, ramp_step=vbias_step)
                        time.sleep(settle)

                    vbg_m, vtg_m = _read_gates(iv)
                    vbias_m = _read_bias(iv)
                    Ibg, Itg, Ib = _read_currents(iv)
                    y = _read_intensity(spec, int(wls.size)) if spec else np.zeros(wls.size)

                    prefix = np.array(
                        [D, F_fixed, vb, vbg_m, vtg_m, vbias_m, Ibg, Itg, Ib],
                        dtype=np.float64
                    )
                    row = np.concatenate((prefix, y)).reshape(1, -1)
                    np.savetxt(f, row, fmt="%.6e", delimiter=",")
                    f.flush()

                    done += 1
                    self.progress.emit(done, total)
                    self.point_xy.emit(vtg, vbg, done)
                    self._emit_log(
                        f"{done}/{total}: D={D:.3f}, Vb={vb:.3f} | "
                        f"meas Vtg={vtg_m:.3f}, Vbg={vbg_m:.3f}, Vb={vbias_m:.3f} | "
                        f"Itg={_fmt_uA(Itg)} µA, Ibg={_fmt_uA(Ibg)} µA, Ib={_fmt_uA(Ib)} µA"
                    )

        self._ramp_to_zero(iv, go_step, go_delay, float(p["vbias_ramp_step"]))
        self._emit_log(f"Done. Saved → {fp.name}")

    def _ramp_to_zero(self, iv, go_step, go_delay, vbias_step):
        if iv is None:
            return
        try:
            self._emit_log("Ramping to 0 V…")
            if hasattr(iv, "set_bias"):
                iv.set_bias(Vbias=0.0, delay_s=go_delay, ramp_step=vbias_step)
            iv.set_gates(Vtg=0.0, Vbg=0.0, delay_s=go_delay, ramp_step=go_step)
        except Exception as e:
            self._emit_log(f"Return-to-zero failed: {e}")


# ── Preview plot (pyqtgraph) ──────────────────────────────────────────────────

class _PreviewPlot(QWidget):
    """Scatter preview for voltage-space and physics-space."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self._tabs = QTabWidget()
        lay.addWidget(self._tabs)

        # Voltage tab
        self._pw_v = pg.PlotWidget()
        self._pw_v.setLabel("bottom", "Vtg", units="V")
        self._pw_v.setLabel("left",   "Vbg", units="V")
        self._pw_v.showGrid(x=True, y=True, alpha=0.3)
        self._pw_v.addLegend()
        self._tabs.addTab(self._pw_v, "V-grid")

        # Physics tab (D vs F)
        self._pw_p = pg.PlotWidget()
        self._pw_p.setLabel("bottom", "D = r·Vtg+Vbg")
        self._pw_p.setLabel("left",   "F = r·Vtg−Vbg")
        self._pw_p.showGrid(x=True, y=True, alpha=0.3)
        self._tabs.addTab(self._pw_p, "D/F")

        # status line
        self._info = QLabel("")
        self._info.setStyleSheet("color: gray; font-size: 10px;")
        lay.addWidget(self._info)

        # items we'll repopulate
        self._items_v: List[pg.PlotDataItem] = []
        self._items_p: List[pg.PlotDataItem] = []

        # live progress dots
        self._done_scatter_v = pg.ScatterPlotItem(size=8, pen=pg.mkPen("k", width=0.5))
        self._done_scatter_p = pg.ScatterPlotItem(size=8, pen=pg.mkPen("k", width=0.5))
        self._cur_scatter_v  = pg.ScatterPlotItem(size=14, pen=pg.mkPen("r", width=2), brush=pg.mkBrush(None))
        self._cur_scatter_p  = pg.ScatterPlotItem(size=14, pen=pg.mkPen("r", width=2), brush=pg.mkBrush(None))
        self._pw_v.addItem(self._done_scatter_v)
        self._pw_v.addItem(self._cur_scatter_v)
        self._pw_p.addItem(self._done_scatter_p)
        self._pw_p.addItem(self._cur_scatter_p)

        self._all_pts: List[Tuple[float, float]] = []  # (vtg, vbg) executed points in order
        self._ratio = 1.0

    def update_plan(
        self,
        all_path: List[Tuple[float, float]],   # full planned path (incl out-of-bounds)
        run_pts:  List[Tuple[float, float]],    # in-bounds executed points
        safety: Tuple[float, float, float, float],
        ratio: float,
        info_text: str = "",
    ):
        self._all_pts = list(run_pts)
        self._ratio   = ratio

        for item in self._items_v:
            self._pw_v.removeItem(item)
        for item in self._items_p:
            self._pw_p.removeItem(item)
        self._items_v.clear()
        self._items_p.clear()
        self._done_scatter_v.setData([], [])
        self._done_scatter_p.setData([], [])
        self._cur_scatter_v.setData([], [])
        self._cur_scatter_p.setData([], [])

        vtg_min, vtg_max, vbg_min, vbg_max = safety

        # safety box (voltage space)
        box = pg.QtWidgets.QGraphicsRectItem(
            vtg_min, vbg_min, vtg_max - vtg_min, vbg_max - vbg_min
        )
        box.setPen(pg.mkPen("r", width=1, style=Qt.DashLine))
        box.setBrush(pg.mkBrush(None))
        self._pw_v.addItem(box)
        self._items_v.append(box)

        if all_path:
            x_all = np.array([p[0] for p in all_path])
            y_all = np.array([p[1] for p in all_path])
            in_mask = (
                (x_all >= vtg_min) & (x_all <= vtg_max) &
                (y_all >= vbg_min) & (y_all <= vbg_max)
            )
            x_out = x_all[~in_mask]
            y_out = y_all[~in_mask]

            # full path (light grey)
            path_item_v = self._pw_v.plot(x_all, y_all, pen=pg.mkPen("#cccccc", width=1))
            self._items_v.append(path_item_v)

            # out-of-bounds red X
            if len(x_out):
                oob = pg.ScatterPlotItem(
                    x=x_out, y=y_out, symbol="x", size=8,
                    pen=pg.mkPen("r", width=1.5), brush=pg.mkBrush(None)
                )
                self._pw_v.addItem(oob)
                self._items_v.append(oob)

        if run_pts:
            n = len(run_pts)
            x_in = np.array([p[0] for p in run_pts])
            y_in = np.array([p[1] for p in run_pts])

            # colour by run order: viridis-like blue→yellow
            colours = [
                pg.intColor(int(255 * i / max(1, n - 1)), hues=256) for i in range(n)
            ]

            # hollow circles for all
            scatter_v = pg.ScatterPlotItem(
                x=x_in, y=y_in,
                size=7, pen=[pg.mkPen(c, width=1.2) for c in colours],
                brush=[pg.mkBrush(None)] * n,
                symbol="o",
            )
            self._pw_v.addItem(scatter_v)
            self._items_v.append(scatter_v)

            # in-bounds path
            path_in_v = self._pw_v.plot(x_in, y_in, pen=pg.mkPen("#555", width=1, alpha=150))
            self._items_v.append(path_in_v)

            # START/END markers
            s_v = pg.ScatterPlotItem(
                x=[x_in[0], x_in[-1]], y=[y_in[0], y_in[-1]],
                symbol=["t", "s"], size=[10, 10],
                pen=pg.mkPen("k", width=1), brush=pg.mkBrush("k"),
            )
            self._pw_v.addItem(s_v)
            self._items_v.append(s_v)

            # Physics tab
            D_in = ratio * x_in + y_in
            F_in = ratio * x_in - y_in
            scatter_p = pg.ScatterPlotItem(
                x=D_in, y=F_in, size=7,
                pen=[pg.mkPen(c, width=1.2) for c in colours],
                brush=[pg.mkBrush(None)] * n,
            )
            self._pw_p.addItem(scatter_p)
            self._items_p.append(scatter_p)

        self._pw_v.autoRange()
        self._pw_p.autoRange()
        self._info.setText(info_text)

    def update_progress(self, done: int, vtg_now: float, vbg_now: float):
        """Colour done points and highlight current."""
        if done <= 0 or not self._all_pts:
            return
        n_done = min(done, len(self._all_pts))
        done_pts = self._all_pts[:n_done]
        x_d = np.array([p[0] for p in done_pts])
        y_d = np.array([p[1] for p in done_pts])
        self._done_scatter_v.setData(x=x_d, y=y_d)
        self._cur_scatter_v.setData(x=[vtg_now], y=[vbg_now])

        r = self._ratio
        D_d = r * x_d + y_d
        F_d = r * x_d - y_d
        self._done_scatter_p.setData(x=D_d, y=F_d)
        self._cur_scatter_p.setData(x=[r * vtg_now + vbg_now], y=[r * vtg_now - vbg_now])

    def clear_progress(self):
        self._done_scatter_v.setData([], [])
        self._done_scatter_p.setData([], [])
        self._cur_scatter_v.setData([], [])
        self._cur_scatter_p.setData([], [])


# ── Grid input helper ─────────────────────────────────────────────────────────

class _GridInput(QWidget):
    """Start / Stop / (Param) + mode combo for one axis."""

    def __init__(self, label: str, default_start: float, default_stop: float,
                 default_step: float = 1.0, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        lay.addWidget(QLabel(label))

        self._start = QDoubleSpinBox()
        self._start.setRange(-200, 200)
        self._start.setDecimals(3)
        self._start.setValue(default_start)
        self._start.setPrefix("start ")
        lay.addWidget(self._start)

        self._stop = QDoubleSpinBox()
        self._stop.setRange(-200, 200)
        self._stop.setDecimals(3)
        self._stop.setValue(default_stop)
        self._stop.setPrefix("stop ")
        lay.addWidget(self._stop)

        self._param = QDoubleSpinBox()
        self._param.setRange(1e-9, 1000)
        self._param.setDecimals(3)
        self._param.setValue(default_step)
        lay.addWidget(self._param)

        self._mode = QComboBox()
        self._mode.addItems(["Step Size", "Total Points"])
        self._mode.currentTextChanged.connect(self._on_mode)
        lay.addWidget(self._mode)

        self._on_mode(self._mode.currentText())

    def _on_mode(self, text: str):
        if text == "Step Size":
            self._param.setSuffix(" V/step")
        else:
            self._param.setSuffix(" pts")
            self._param.setDecimals(0)

    def get_array(self) -> np.ndarray:
        mode = "Total Points" if self._mode.currentText() == "Total Points" else "Step"
        return _get_linear_array(
            self._start.value(), self._stop.value(),
            self._param.value(),
            "Total Points" if mode == "Total Points" else "Step Size (Grid)"
        )

    def values(self) -> Tuple[float, float, float, str]:
        return self._start.value(), self._stop.value(), self._param.value(), self._mode.currentText()


# ── Main panel ────────────────────────────────────────────────────────────────

class MegaSweepPanel(QWidget):
    """
    Inject smu_ctrl and lf6_ctrl to enable run.

    Usage:
        panel = MegaSweepPanel(smu_ctrl=smu, lf6_ctrl=lf6)
    """

    def __init__(self, smu_ctrl=None, lf6_ctrl=None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._smu  = smu_ctrl
        self._lf6  = lf6_ctrl
        self._worker: Optional[_MegaSweepWorker] = None
        self._thread: Optional[QThread] = None
        self._build()
        self._wire()
        self._refresh_preview()

    # ── build ─────────────────────────────────────────────────────────────────

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # ── meta row ──────────────────────────────────────────────────────────
        meta = QHBoxLayout()
        self._sample_edit = QLineEdit(); self._sample_edit.setPlaceholderText("Sample")
        self._tag_edit    = QLineEdit("Megasweep")
        self._laser_edit  = QLineEdit("730")
        self._power_edit  = QLineEdit("1")
        for w, lbl in [(self._sample_edit, "Sample:"),
                       (self._tag_edit,    "Tag:"),
                       (self._laser_edit,  "Laser (nm):"),
                       (self._power_edit,  "Power (µW):")]:
            meta.addWidget(QLabel(lbl))
            meta.addWidget(w)
        meta.addStretch()
        root.addLayout(meta)

        # ── splitter ──────────────────────────────────────────────────────────
        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, stretch=1)

        # left: config (in scroll area)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(380)
        cfg_widget = QWidget()
        cfg_lay = QVBoxLayout(cfg_widget)
        cfg_lay.setSpacing(6)
        scroll.setWidget(cfg_widget)
        splitter.addWidget(scroll)

        # right: preview + controls
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(4, 4, 4, 4)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        # ── Optical settings group ────────────────────────────────────────────
        opt_grp = QGroupBox("Optical Settings")
        opt_form = QFormLayout(opt_grp)

        self._exp_spin = QDoubleSpinBox()
        self._exp_spin.setRange(1, 600_000)
        self._exp_spin.setSuffix(" ms")
        self._exp_spin.setValue(cfg.lf6.exposure_ms)
        opt_form.addRow("Exposure:", self._exp_spin)

        self._center_spin = QDoubleSpinBox()
        self._center_spin.setRange(200, 2000)
        self._center_spin.setSuffix(" nm")
        self._center_spin.setValue(cfg.lf6.center_nm)
        opt_form.addRow("Center λ:", self._center_spin)

        self._frames_spin = QSpinBox()
        self._frames_spin.setRange(1, 1000)
        self._frames_spin.setValue(cfg.lf6.accumulations)
        opt_form.addRow("Frames/Accum:", self._frames_spin)

        cfg_lay.addWidget(opt_grp)

        # ── Ramp settings ─────────────────────────────────────────────────────
        ramp_grp = QGroupBox("Ramp Settings")
        ramp_form = QFormLayout(ramp_grp)

        self._go_step_spin = QDoubleSpinBox()
        self._go_step_spin.setRange(0.001, 10); self._go_step_spin.setDecimals(3)
        self._go_step_spin.setValue(cfg.ramp.step_V)
        ramp_form.addRow("Ramp step (V):", self._go_step_spin)

        self._go_delay_spin = QDoubleSpinBox()
        self._go_delay_spin.setRange(0, 10); self._go_delay_spin.setDecimals(3)
        self._go_delay_spin.setValue(cfg.ramp.delay_s)
        ramp_form.addRow("Ramp delay (s):", self._go_delay_spin)

        self._settle_spin = QDoubleSpinBox()
        self._settle_spin.setRange(0, 60); self._settle_spin.setDecimals(3)
        self._settle_spin.setValue(cfg.ramp.settle_s)
        ramp_form.addRow("Settle (s):", self._settle_spin)

        cfg_lay.addWidget(ramp_grp)

        # ── Vbias (optional constant) ─────────────────────────────────────────
        vb_grp = QGroupBox("Vbias (optional, constant for Vtg/Vbg mode)")
        vb_form = QFormLayout(vb_grp)

        self._enable_vbias_chk = QCheckBox("Enable")
        vb_form.addRow("", self._enable_vbias_chk)

        self._vbias_set_spin = QDoubleSpinBox()
        self._vbias_set_spin.setRange(-200, 200); self._vbias_set_spin.setDecimals(4)
        self._vbias_set_spin.setValue(0.0); self._vbias_set_spin.setEnabled(False)
        vb_form.addRow("Vbias (V):", self._vbias_set_spin)

        self._vbias_step_spin = QDoubleSpinBox()
        self._vbias_step_spin.setRange(0.001, 10); self._vbias_step_spin.setDecimals(3)
        self._vbias_step_spin.setValue(cfg.ramp.step_V); self._vbias_step_spin.setEnabled(False)
        vb_form.addRow("Vbias ramp step:", self._vbias_step_spin)

        cfg_lay.addWidget(vb_grp)

        # ── Safety limits ─────────────────────────────────────────────────────
        lim_grp = QGroupBox("Absolute Safety Limits")
        lim_form = QFormLayout(lim_grp)

        def _lim_spin(default):
            s = QDoubleSpinBox(); s.setRange(-200, 200); s.setDecimals(3)
            s.setValue(default); return s

        self._vtg_min = _lim_spin(-10.0)
        self._vtg_max = _lim_spin( 10.0)
        self._vbg_min = _lim_spin(-10.0)
        self._vbg_max = _lim_spin( 10.0)
        lim_form.addRow("Vtg min (V):", self._vtg_min)
        lim_form.addRow("Vtg max (V):", self._vtg_max)
        lim_form.addRow("Vbg min (V):", self._vbg_min)
        lim_form.addRow("Vbg max (V):", self._vbg_max)

        cfg_lay.addWidget(lim_grp)

        # ── Mode + grid ───────────────────────────────────────────────────────
        mode_grp = QGroupBox("Sweep Mode & Grid")
        mode_lay = QVBoxLayout(mode_grp)

        self._mode_combo = QComboBox()
        self._mode_combo.addItems(["Vtg stripes & Vbg", "D & Vbias (fixed F)"])
        mode_lay.addWidget(self._mode_combo)

        self._snake_chk = QCheckBox("Snake pattern")
        self._snake_chk.setChecked(True)
        mode_lay.addWidget(self._snake_chk)

        self._ratio_spin = QDoubleSpinBox()
        self._ratio_spin.setRange(-1000, 1000); self._ratio_spin.setDecimals(4)
        self._ratio_spin.setValue(1.0)
        ratio_row = QHBoxLayout()
        ratio_row.addWidget(QLabel("r  (D=r·Vtg+Vbg, F=r·Vtg−Vbg):"))
        ratio_row.addWidget(self._ratio_spin)
        mode_lay.addLayout(ratio_row)

        # Vtg/Vbg grid (mode 1)
        self._vtg_grid = _GridInput("Vtg outer:", -5.0, 5.0, 1.0)
        self._vbg_grid = _GridInput("Vbg inner:", -5.0, 5.0, 1.0)
        self._ratio_step_spin = QDoubleSpinBox()
        self._ratio_step_spin.setRange(0, 100); self._ratio_step_spin.setDecimals(4)
        self._ratio_step_spin.setValue(1.0)
        ratio_step_row = QHBoxLayout()
        ratio_step_row.addWidget(QLabel("Vbg step = r_step × Vtg step:"))
        ratio_step_row.addWidget(self._ratio_step_spin)

        self._vtgvbg_widget = QWidget()
        vvlay = QVBoxLayout(self._vtgvbg_widget); vvlay.setContentsMargins(0,0,0,0)
        vvlay.addWidget(self._vtg_grid)
        vvlay.addLayout(ratio_step_row)
        self._derived_lbl = QLabel("")
        self._derived_lbl.setStyleSheet("color: gray; font-size: 10px;")
        vvlay.addWidget(self._derived_lbl)
        mode_lay.addWidget(self._vtgvbg_widget)

        # D/Vbias grid (mode 2)
        self._D_grid  = _GridInput("D outer:",      -5.0, 5.0, 1.0)
        self._Vb_grid = _GridInput("Vbias inner:", -0.5, 0.5, 0.05)
        self._F_fixed_spin = QDoubleSpinBox()
        self._F_fixed_spin.setRange(-200, 200); self._F_fixed_spin.setDecimals(4)
        self._F_fixed_spin.setValue(0.0)

        self._dvbias_widget = QWidget()
        dvlay = QVBoxLayout(self._dvbias_widget); dvlay.setContentsMargins(0,0,0,0)
        F_row = QHBoxLayout()
        F_row.addWidget(QLabel("F fixed (V):"))
        F_row.addWidget(self._F_fixed_spin)
        dvlay.addLayout(F_row)
        dvlay.addWidget(self._D_grid)
        dvlay.addWidget(self._Vb_grid)
        mode_lay.addWidget(self._dvbias_widget)

        cfg_lay.addWidget(mode_grp)
        cfg_lay.addStretch()

        # ── Preview ───────────────────────────────────────────────────────────
        self._preview = _PreviewPlot()
        right_lay.addWidget(self._preview, stretch=1)

        # ── Run controls ──────────────────────────────────────────────────────
        ctrl = QHBoxLayout()
        self._run_btn  = QPushButton("Run Sweep")
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setEnabled(False)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        ctrl.addWidget(self._run_btn)
        ctrl.addWidget(self._stop_btn)
        ctrl.addWidget(self._progress, stretch=1)
        right_lay.addLayout(ctrl)

        # log
        self._log_edit = QTextEdit()
        self._log_edit.setReadOnly(True)
        self._log_edit.setMaximumHeight(160)
        self._log_edit.setFont(
            self._log_edit.font().__class__("Consolas") if False else self._log_edit.font()
        )
        right_lay.addWidget(self._log_edit)

    # ── wire ──────────────────────────────────────────────────────────────────

    def _wire(self):
        self._enable_vbias_chk.toggled.connect(self._vbias_set_spin.setEnabled)
        self._enable_vbias_chk.toggled.connect(self._vbias_step_spin.setEnabled)

        self._mode_combo.currentTextChanged.connect(self._on_mode_change)
        self._on_mode_change(self._mode_combo.currentText())

        # preview updates
        for w in (self._vtg_min, self._vtg_max, self._vbg_min, self._vbg_max,
                  self._ratio_spin, self._ratio_step_spin, self._snake_chk,
                  self._F_fixed_spin):
            if hasattr(w, "valueChanged"):
                w.valueChanged.connect(self._refresh_preview)
            elif hasattr(w, "toggled"):
                w.toggled.connect(self._refresh_preview)
        self._mode_combo.currentTextChanged.connect(self._refresh_preview)

        for grid in (self._vtg_grid, self._vbg_grid, self._D_grid, self._Vb_grid):
            grid._start.valueChanged.connect(self._refresh_preview)
            grid._stop.valueChanged.connect(self._refresh_preview)
            grid._param.valueChanged.connect(self._refresh_preview)
            grid._mode.currentTextChanged.connect(self._refresh_preview)

        self._run_btn.clicked.connect(self._on_run)
        self._stop_btn.clicked.connect(self._on_stop)

    @Slot(str)
    def _on_mode_change(self, text: str):
        is_vtgvbg = (text == "Vtg stripes & Vbg")
        self._vtgvbg_widget.setVisible(is_vtgvbg)
        self._dvbias_widget.setVisible(not is_vtgvbg)

    # ── preview ───────────────────────────────────────────────────────────────

    def _get_sweep_pts(self):
        """Return (all_path, run_pts) based on current UI state."""
        mode   = self._mode_combo.currentText()
        snake  = self._snake_chk.isChecked()
        vtg_mn = self._vtg_min.value()
        vtg_mx = self._vtg_max.value()
        vbg_mn = self._vbg_min.value()
        vbg_mx = self._vbg_max.value()

        if mode == "Vtg stripes & Vbg":
            outer = self._vtg_grid.get_array()
            # derive inner from ratio_step
            vtg_step_eff = abs(float(outer[1] - outer[0])) if len(outer) >= 2 else 1.0
            vbg_step_mag = max(1e-9, abs(self._ratio_step_spin.value()) * vtg_step_eff)
            start, stop, _, _ = self._vbg_grid.values()
            vbg_step = vbg_step_mag if stop >= start else -vbg_step_mag
            inner = _arange_inclusive(start, stop, vbg_step)

            all_path = _build_full_path_vtgvbg(outer, inner, snake)
            run_pts  = _build_sweep_points_vtgvbg(
                outer, inner, vtg_mn, vtg_mx, vbg_mn, vbg_mx, snake
            )
        else:
            D_vals = self._D_grid.get_array()
            Vb_vals = self._Vb_grid.get_array()
            F = self._F_fixed_spin.value()
            r = self._ratio_spin.value()
            _, valid_vtg, valid_vbg = _build_dvbias_valid(
                D_vals, F, r, vtg_mn, vtg_mx, vbg_mn, vbg_mx
            )
            n_vb = len(Vb_vals)
            run_pts  = [(vtg, vbg) for vtg, vbg in zip(valid_vtg, valid_vbg) for _ in range(n_vb)]
            # full path same (D mode: gates don't change per vbias)
            all_path = [(vtg, vbg) for vtg, vbg in zip(valid_vtg, valid_vbg)] * n_vb

        return all_path, run_pts

    @Slot()
    def _refresh_preview(self):
        try:
            all_path, run_pts = self._get_sweep_pts()
        except Exception:
            return

        safety = (
            self._vtg_min.value(), self._vtg_max.value(),
            self._vbg_min.value(), self._vbg_max.value(),
        )
        n_plan = len(all_path)
        n_run  = len(run_pts)
        info = (f"Planned: {n_plan}  |  In safety (run): {n_run}  |  "
                f"Skipped: {n_plan - n_run}")

        # update derived label for Vtg/Vbg mode
        if self._mode_combo.currentText() == "Vtg stripes & Vbg":
            outer = self._vtg_grid.get_array()
            vtg_eff = abs(float(outer[1] - outer[0])) if len(outer) >= 2 else 1.0
            vbg_eff = abs(self._ratio_step_spin.value()) * vtg_eff
            self._derived_lbl.setText(
                f"Derived: Vtg step ≈ {vtg_eff:.4g} V  |  Vbg step ≈ {vbg_eff:.4g} V"
            )

        self._preview.update_plan(all_path, run_pts, safety,
                                  self._ratio_spin.value(), info)

    # ── run / stop ────────────────────────────────────────────────────────────

    def _collect_params(self) -> dict:
        mode   = self._mode_combo.currentText()
        snake  = self._snake_chk.isChecked()
        outer  = self._vtg_grid.get_array()
        vtg_step_eff = abs(float(outer[1] - outer[0])) if len(outer) >= 2 else 1.0
        vbg_step_mag = max(1e-9, abs(self._ratio_step_spin.value()) * vtg_step_eff)
        start_vbg, stop_vbg, _, _ = self._vbg_grid.values()
        vbg_signed = vbg_step_mag if stop_vbg >= start_vbg else -vbg_step_mag
        inner = _arange_inclusive(start_vbg, stop_vbg, vbg_signed)

        sample = self._sample_edit.text().strip() or "Sample"
        tag    = self._tag_edit.text().strip() or "Megasweep"
        exp_ms = self._exp_spin.value()
        center = self._center_spin.value()
        frames = self._frames_spin.value()
        exp_s  = f"{int(exp_ms // 1000)}" if exp_ms % 1000 == 0 else f"{exp_ms / 1000:.3g}"

        base_name = f"{sample}~{tag}~{center:.0f}nm~{exp_s}sx{int(frames)}"

        out_path = Path(cfg.filename.base_out) / sample / "megasweep"

        return dict(
            mode          = "D & Vbias" if "D" in mode else "Vtg stripes",
            snake         = snake,
            outer_vals    = outer,
            inner_vals    = inner,
            D_vals        = self._D_grid.get_array(),
            Vb_vals       = self._Vb_grid.get_array(),
            ratio         = self._ratio_spin.value(),
            F_fixed       = self._F_fixed_spin.value(),
            lim_vtg_min   = self._vtg_min.value(),
            lim_vtg_max   = self._vtg_max.value(),
            lim_vbg_min   = self._vbg_min.value(),
            lim_vbg_max   = self._vbg_max.value(),
            exp_ms        = exp_ms,
            center_nm     = center,
            frames        = frames,
            go_step       = self._go_step_spin.value(),
            go_delay      = self._go_delay_spin.value(),
            settle        = self._settle_spin.value(),
            enable_vbias  = self._enable_vbias_chk.isChecked(),
            vbias_set     = self._vbias_set_spin.value(),
            vbias_ramp_step = self._vbias_step_spin.value(),
            base_name     = base_name,
            out_path      = out_path,
        )

    @Slot()
    def _on_run(self):
        params = self._collect_params()

        self._preview.clear_progress()
        self._progress.setValue(0)
        self._log_edit.clear()
        self._run_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)

        self._worker = _MegaSweepWorker(params, self._smu, self._lf6)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.log.connect(self._on_log)
        self._worker.progress.connect(self._on_progress)
        self._worker.point_xy.connect(self._on_point)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(lambda msg: self._on_log(f"ERROR: {msg}"))
        self._worker.error.connect(self._on_finished)

        self._thread.start()

    @Slot()
    def _on_stop(self):
        if self._worker:
            self._worker.request_stop()
        self._stop_btn.setEnabled(False)

    @Slot(str)
    def _on_log(self, msg: str):
        self._log_edit.append(msg)
        sb = self._log_edit.verticalScrollBar()
        sb.setValue(sb.maximum())

    @Slot(int, int)
    def _on_progress(self, done: int, total: int):
        pct = int(100 * done / total) if total > 0 else 0
        self._progress.setValue(pct)

    @Slot(float, float, int)
    def _on_point(self, vtg: float, vbg: float, done: int):
        self._preview.update_progress(done, vtg, vbg)

    @Slot()
    def _on_finished(self):
        self._run_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        if self._thread:
            self._thread.quit()
            self._thread.wait()
