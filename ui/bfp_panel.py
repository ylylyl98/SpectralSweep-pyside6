# ui/bfp_panel.py
# ──────────────────────────────────────────────────────────────────────────────
# Back Focal Plane (BFP) measurement panel.
#
# Acquires a single frame (1D or 2D) from the LF6 spectrometer.
# Displays in a pyqtgraph ImageView (2D) or PlotWidget (1D).
# Supports optional background subtraction: (R − R0) / R0.
# Saves CSV with wavelength header and optional PNG snapshot.
#
# Rules:
#   - No instrument state stored here.
#   - importlib.reload() safe.
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QPushButton, QLineEdit, QDoubleSpinBox, QSpinBox,
    QCheckBox, QComboBox, QSplitter, QFileDialog, QSizePolicy,
    QTabWidget,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pyqtgraph as pg
from utils.config import cfg

pg.setConfigOption("background", "w")
pg.setConfigOption("foreground", "k")


# ── Pure helpers ──────────────────────────────────────────────────────────────

def _sanitize(s: str) -> str:
    s = str(s or "").strip()
    s = re.sub(r"[^\w\-.]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "unnamed"


def _now_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _build_stem(sample: str, note: str, suffix: str, ts: str, run_idx: int) -> str:
    parts = [p for p in [_sanitize(sample), _sanitize(note), _sanitize(suffix), ts, f"{int(run_idx):03d}"] if p]
    return "_".join(parts) or "bfp"


def _orient_wl_columns(arr: np.ndarray, wls: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Ensure wavelength axis is columns (axis=1) if possible."""
    img = np.asarray(arr)
    wls = np.asarray(wls, dtype=float).ravel() if wls is not None else np.array([])
    if img.ndim != 2 or wls.size == 0:
        return img, np.array([])
    if wls.size == img.shape[1]:
        return img, wls
    if wls.size == img.shape[0] and wls.size != img.shape[1]:
        return img.T, wls
    return img, np.array([])


def _apply_background(meas: np.ndarray, meas_wls: np.ndarray,
                       bg: np.ndarray, bg_wls: np.ndarray,
                       tol_nm: float) -> Tuple[bool, np.ndarray, str]:
    meas = np.asarray(meas, dtype=float)
    bg   = np.asarray(bg,   dtype=float)
    if meas.ndim != bg.ndim:
        return False, meas, f"ndim mismatch ({meas.ndim}D vs {bg.ndim}D)"
    if meas.shape != bg.shape:
        return False, meas, f"shape mismatch {meas.shape} vs {bg.shape}"
    mw = np.asarray(meas_wls, dtype=float).ravel()
    bw = np.asarray(bg_wls,   dtype=float).ravel()
    if mw.size > 0 and bw.size > 0:
        if mw.size != bw.size:
            return False, meas, f"wavelength length mismatch {mw.size} vs {bw.size}"
        d = float(np.nanmax(np.abs(mw - bw)))
        if d > tol_nm:
            return False, meas, f"λ mismatch: max|Δλ|={d:.4g} nm > tol {tol_nm:g}"
    out = np.where(np.abs(bg) > 1e-12, (meas - bg) / bg, np.nan)
    return True, out, "(R-R0)/R0 applied"


def _get_wls(spec) -> np.ndarray:
    """Retrieve wavelength calibration from the adapter."""
    if spec is None:
        return np.array([])
    try:
        if hasattr(spec, "calibration_wavelengths"):
            return np.asarray(list(spec.calibration_wavelengths()), dtype=float).ravel()
    except Exception:
        pass
    setup = getattr(spec, "setup", None)
    if setup is not None:
        try:
            return np.asarray(setup.get_wavelength_calibration(), dtype=float).ravel()
        except Exception:
            pass
    return np.array([])


def _save_csv(path: Path, data: np.ndarray, wls: np.ndarray) -> None:
    import pandas as pd
    arr = np.asarray(data)
    wls = np.asarray(wls, dtype=float).ravel() if wls is not None else np.array([])

    if arr.ndim == 1:
        y = arr.astype(float).ravel()
        if wls.size == y.size:
            pd.DataFrame({"wavelength_nm": wls, "intensity": y}).to_csv(path, index=False)
        else:
            pd.DataFrame({"pixel": np.arange(y.size, dtype=int), "intensity": y}).to_csv(path, index=False)
        return

    if arr.ndim == 2:
        img, wls_x = _orient_wl_columns(arr, wls)
        H, W = img.shape
        x_col  = wls_x if wls_x.size == W else np.arange(W, dtype=float)
        x_name = "wavelength_nm" if wls_x.size == W else "x_px"
        mat = img.T   # (W,H) – each row = one wavelength, cols = y positions
        df = pd.DataFrame(mat, columns=[str(i) for i in range(H)])
        df.insert(0, x_name, x_col)
        df.to_csv(path, index=False)
        return

    raise ValueError(f"Unsupported shape: {arr.shape}")


# ── Acquire worker ────────────────────────────────────────────────────────────

class _AcquireWorker(QObject):
    result  = Signal(object, object)   # (data: np.ndarray, wls: np.ndarray)
    error   = Signal(str)
    status  = Signal(str)
    finished = Signal()

    def __init__(self, lf6_ctrl, roi_mode: str, center_nm: float,
                 exposure_ms: float, epf: int, auto_apply: bool, warmup: bool):
        super().__init__()
        self._ctrl       = lf6_ctrl
        self._roi_mode   = roi_mode
        self._center_nm  = center_nm
        self._exposure_ms = exposure_ms
        self._epf        = int(epf)
        self._auto_apply = auto_apply
        self._warmup     = warmup

    @Slot()
    def run(self):
        try:
            self._do_acquire()
        except Exception as e:
            self.error.emit(str(e))
        finally:
            self.finished.emit()

    def _do_acquire(self):
        spec = self._ctrl.adapter if self._ctrl and self._ctrl.is_connected else None
        if spec is None:
            raise RuntimeError("LF6 not connected.")

        setup = getattr(spec, "setup", spec)

        if self._auto_apply:
            self.status.emit("Applying settings…")
            try:
                if hasattr(setup, "change_spectra_center"):
                    setup.change_spectra_center(f"{self._center_nm:.0f}")
                    time.sleep(0.15)
                if hasattr(setup, "change_expose_time"):
                    setup.change_expose_time(float(self._exposure_ms))
                    time.sleep(0.10)
                for fn in ("set_accumulations", "change_frame_to_combine"):
                    if hasattr(setup, fn):
                        getattr(setup, fn)(self._epf)
                        break

                roi = (self._roi_mode or "").strip().lower()
                if roi.startswith("full"):
                    if hasattr(setup, "change_roi_FullSensor"):
                        setup.change_roi_FullSensor()
                elif "bin" in roi or "line" in roi:
                    if hasattr(setup, "change_roi_LineSensor"):
                        setup.change_roi_LineSensor()
            except Exception as e:
                self.status.emit(f"Settings warning: {e}")

        if self._warmup:
            self.status.emit("Warm-up acquisition…")
            try:
                _do_raw_acquire(setup, self._roi_mode)
            except Exception:
                pass

        self.status.emit("Acquiring…")
        raw = _do_raw_acquire(setup, self._roi_mode)
        wls = _get_wls(spec)
        self.result.emit(np.asarray(raw), wls)
        self.status.emit("Done.")


def _do_raw_acquire(setup, roi_mode: str) -> np.ndarray:
    roi = (roi_mode or "").strip().lower()
    if roi.startswith("full"):
        if hasattr(setup, "acquire_2d"):
            return np.asarray(setup.acquire_2d())
    return np.asarray(setup.acquire())


# ── Display widget ────────────────────────────────────────────────────────────

class _DisplayWidget(QWidget):
    """Shows 1D or 2D data with cmap / scale controls."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        # ── tab: 2D image | 1D spectrum ──────────────────────────────────────
        self._tabs = QTabWidget()
        lay.addWidget(self._tabs, stretch=1)

        # 2D tab
        self._imgview = pg.ImageView()
        self._imgview.ui.roiBtn.hide()
        self._imgview.ui.menuBtn.hide()
        self._tabs.addTab(self._imgview, "2D Frame")

        # 1D tab
        self._pw = pg.PlotWidget()
        self._pw.setLabel("bottom", "Wavelength", units="nm")
        self._pw.setLabel("left",   "Intensity")
        self._pw.showGrid(x=True, y=True, alpha=0.3)
        self._curve = self._pw.plot(pen=pg.mkPen("#1565C0", width=1.5))
        self._tabs.addTab(self._pw, "1D Spectrum")

        # ── display controls ─────────────────────────────────────────────────
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Scale:"))
        self._scale_combo = QComboBox()
        self._scale_combo.addItems(["Linear", "Log"])
        ctrl.addWidget(self._scale_combo)

        ctrl.addWidget(QLabel("Cmap:"))
        self._cmap_combo = QComboBox()
        self._cmap_combo.addItems(["gray", "viridis", "plasma", "magma", "inferno"])
        ctrl.addWidget(self._cmap_combo)

        self._autoscale_chk = QCheckBox("Auto 1–99%")
        self._autoscale_chk.setChecked(True)
        ctrl.addWidget(self._autoscale_chk)

        ctrl.addStretch()
        self._shape_lbl = QLabel("No data")
        ctrl.addWidget(self._shape_lbl)
        lay.addLayout(ctrl)

        self._cmap_combo.currentTextChanged.connect(self._apply_cmap)
        self._apply_cmap("gray")

        self._data: Optional[np.ndarray] = None
        self._wls:  np.ndarray = np.array([])

    def _apply_cmap(self, name: str):
        try:
            cmap = pg.colormap.get(name)
            self._imgview.setColorMap(cmap)
        except Exception:
            pass

    def update_data(self, data: np.ndarray, wls: np.ndarray):
        self._data = np.asarray(data, dtype=float)
        self._wls  = np.asarray(wls,  dtype=float).ravel()

        if self._data.ndim == 2:
            img = self._data
            # ImageView expects (x, y) → transpose (rows,cols) to (cols,rows)
            self._imgview.setImage(img.T, autoLevels=True, autoRange=True)
            self._apply_cmap(self._cmap_combo.currentText())
            self._shape_lbl.setText(f"Shape: {img.shape[0]}×{img.shape[1]}")
            self._tabs.setCurrentIndex(0)
        else:
            y = self._data.ravel()
            x = self._wls if self._wls.size == y.size else np.arange(y.size, dtype=float)
            self._curve.setData(x, y)
            if self._autoscale_chk.isChecked():
                self._pw.enableAutoRange()
            self._shape_lbl.setText(f"Points: {y.size}")
            self._tabs.setCurrentIndex(1)

    def get_data(self) -> Tuple[Optional[np.ndarray], np.ndarray]:
        return self._data, self._wls


# ── Background panel ──────────────────────────────────────────────────────────

class _BgPanel(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("Background Subtraction  (R-R0)/R0", parent)
        lay = QFormLayout(self)

        self._enable_chk = QCheckBox("Enable")
        lay.addRow("", self._enable_chk)

        row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("Select background CSV…")
        self._path_edit.setReadOnly(True)
        self._browse_btn = QPushButton("Browse…")
        self._browse_btn.setFixedWidth(72)
        row.addWidget(self._path_edit)
        row.addWidget(self._browse_btn)
        lay.addRow("BG file:", row)

        self._tol_spin = QDoubleSpinBox()
        self._tol_spin.setRange(0, 100)
        self._tol_spin.setDecimals(6)
        self._tol_spin.setValue(0.001)
        self._tol_spin.setSuffix(" nm")
        lay.addRow("λ tol:", self._tol_spin)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: gray; font-size: 10px;")
        lay.addRow("", self._status_lbl)

        self._bg_data: Optional[np.ndarray] = None
        self._bg_wls:  np.ndarray = np.array([])

        self._browse_btn.clicked.connect(self._on_browse)

    @Slot()
    def _on_browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select background CSV",
            str(Path(cfg.filename.base_out)),
            "CSV files (*.csv)"
        )
        if path:
            self._path_edit.setText(path)
            self._load_bg(Path(path))

    def _load_bg(self, p: Path):
        try:
            import pandas as pd
            df = pd.read_csv(p)
            cols_lower = {c.strip().lower(): c for c in df.columns}
            if "wavelength_nm" in cols_lower and "intensity" in cols_lower:
                wls = df[cols_lower["wavelength_nm"]].to_numpy(dtype=float)
                y   = df[cols_lower["intensity"]].to_numpy(dtype=float)
                self._bg_data = y
                self._bg_wls  = wls
            else:
                # Try to load as 2D wide format (wavelength_nm + y columns)
                if "wavelength_nm" in cols_lower:
                    wl_col = cols_lower["wavelength_nm"]
                    wls = df[wl_col].to_numpy(dtype=float)
                    y_cols = [c for c in df.columns if c != wl_col]
                    mat = df[y_cols].to_numpy(dtype=float).T  # (H,W)
                    self._bg_data = mat
                    self._bg_wls  = wls
                else:
                    self._bg_data = df.to_numpy(dtype=float)
                    self._bg_wls  = np.array([])
            self._status_lbl.setText(f"Loaded: {p.name}")
            self._status_lbl.setStyleSheet("color: green; font-size: 10px;")
        except Exception as e:
            self._bg_data = None
            self._bg_wls  = np.array([])
            self._status_lbl.setText(f"Error: {e}")
            self._status_lbl.setStyleSheet("color: red; font-size: 10px;")

    def apply_to(self, data: np.ndarray, wls: np.ndarray) -> Tuple[bool, np.ndarray, str]:
        if not self._enable_chk.isChecked() or self._bg_data is None:
            return False, data, "Background not applied."
        tol = self._tol_spin.value()

        # orient 2D
        if data.ndim == 2 and self._bg_data.ndim == 2:
            d_o, d_wls = _orient_wl_columns(data,          wls)
            b_o, b_wls = _orient_wl_columns(self._bg_data, self._bg_wls)
        else:
            d_o, d_wls = data,           wls
            b_o, b_wls = self._bg_data,  self._bg_wls

        ok, out, msg = _apply_background(d_o, d_wls, b_o, b_wls, tol)
        return ok, out, msg


# ── Main panel ────────────────────────────────────────────────────────────────

class BFPPanel(QWidget):
    """
    Back Focal Plane acquisition panel.
    Inject lf6_ctrl to enable acquisition.

    Usage:
        panel = BFPPanel(lf6_ctrl=lf6)
    """

    def __init__(self, lf6_ctrl=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._ctrl   = lf6_ctrl
        self._thread: Optional[QThread] = None
        self._worker: Optional[_AcquireWorker] = None
        self._raw_data: Optional[np.ndarray] = None
        self._raw_wls:  np.ndarray = np.array([])
        self._run_idx = 1
        self._build()
        self._wire()

    # ── build ─────────────────────────────────────────────────────────────────

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, stretch=1)

        # ── left: controls ────────────────────────────────────────────────────
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left.setMaximumWidth(320)
        splitter.addWidget(left)

        # File naming
        name_grp = QGroupBox("Save Name")
        name_form = QFormLayout(name_grp)
        self._sample_edit = QLineEdit()
        self._note_edit   = QLineEdit()
        self._suffix_edit = QLineEdit("BFP")
        self._run_idx_spin = QSpinBox()
        self._run_idx_spin.setRange(1, 99999)
        self._run_idx_spin.setValue(1)
        self._auto_inc_chk = QCheckBox("Auto +1")
        self._auto_inc_chk.setChecked(True)
        name_form.addRow("Sample:", self._sample_edit)
        name_form.addRow("Note:",   self._note_edit)
        name_form.addRow("Suffix:", self._suffix_edit)
        name_form.addRow("Run #:",  self._run_idx_spin)
        name_form.addRow("",        self._auto_inc_chk)
        self._stem_lbl = QLabel("")
        self._stem_lbl.setStyleSheet("color: gray; font-size: 10px;")
        name_form.addRow("Preview:", self._stem_lbl)
        left_lay.addWidget(name_grp)

        # Acquisition settings
        acq_grp = QGroupBox("Acquisition Settings")
        acq_form = QFormLayout(acq_grp)

        self._roi_combo = QComboBox()
        self._roi_combo.addItems(["Full sensor", "Bin all"])
        acq_form.addRow("ROI mode:", self._roi_combo)

        self._center_spin = QDoubleSpinBox()
        self._center_spin.setRange(200, 2000)
        self._center_spin.setSuffix(" nm")
        self._center_spin.setValue(cfg.lf6.center_nm)
        acq_form.addRow("Center λ:", self._center_spin)

        self._exp_spin = QDoubleSpinBox()
        self._exp_spin.setRange(1, 600_000)
        self._exp_spin.setSuffix(" ms")
        self._exp_spin.setValue(cfg.lf6.exposure_ms)
        acq_form.addRow("Exposure:", self._exp_spin)

        self._epf_spin = QSpinBox()
        self._epf_spin.setRange(1, 100000)
        self._epf_spin.setValue(cfg.lf6.accumulations)
        acq_form.addRow("Frames/EPF:", self._epf_spin)

        self._auto_apply_chk = QCheckBox("Auto-apply before acquire")
        self._auto_apply_chk.setChecked(True)
        acq_form.addRow("", self._auto_apply_chk)

        self._warmup_chk = QCheckBox("Warm-up shot before acquire")
        self._warmup_chk.setChecked(True)
        acq_form.addRow("", self._warmup_chk)

        self._est_lbl = QLabel("")
        self._est_lbl.setStyleSheet("color: gray; font-size: 10px;")
        acq_form.addRow("Est. time:", self._est_lbl)
        left_lay.addWidget(acq_grp)

        # Buttons
        btn_row = QHBoxLayout()
        self._acquire_btn = QPushButton("Acquire")
        self._acquire_btn.setEnabled(False)
        self._clear_btn   = QPushButton("Clear")
        btn_row.addWidget(self._acquire_btn)
        btn_row.addWidget(self._clear_btn)
        left_lay.addLayout(btn_row)

        # Save buttons
        save_row = QHBoxLayout()
        self._save_csv_btn = QPushButton("Save CSV")
        self._save_csv_btn.setEnabled(False)
        self._save_png_btn = QPushButton("Save PNG")
        self._save_png_btn.setEnabled(False)
        save_row.addWidget(self._save_csv_btn)
        save_row.addWidget(self._save_png_btn)
        left_lay.addLayout(save_row)

        # Status
        self._status_lbl = QLabel("Disconnected")
        self._status_lbl.setStyleSheet("color: gray;")
        left_lay.addWidget(self._status_lbl)

        # Background
        self._bg_panel = _BgPanel()
        left_lay.addWidget(self._bg_panel)

        # Apply BG button
        self._apply_bg_btn = QPushButton("Apply / Revert Background")
        self._apply_bg_btn.setEnabled(False)
        left_lay.addWidget(self._apply_bg_btn)

        left_lay.addStretch()

        # ── right: display ────────────────────────────────────────────────────
        self._display = _DisplayWidget()
        splitter.addWidget(self._display)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

    # ── wire ──────────────────────────────────────────────────────────────────

    def _wire(self):
        self._acquire_btn.clicked.connect(self._on_acquire)
        self._clear_btn.clicked.connect(self._on_clear)
        self._save_csv_btn.clicked.connect(self._on_save_csv)
        self._save_png_btn.clicked.connect(self._on_save_png)
        self._apply_bg_btn.clicked.connect(self._on_apply_bg)

        for w in (self._sample_edit, self._note_edit, self._suffix_edit):
            w.textChanged.connect(self._update_stem_preview)
        self._run_idx_spin.valueChanged.connect(self._update_stem_preview)
        self._update_stem_preview()

        for w in (self._exp_spin, self._epf_spin):
            w.valueChanged.connect(self._update_est)
        self._update_est()

        if self._ctrl is not None:
            self._ctrl.connected.connect(self._on_lf6_connected)
            self._ctrl.disconnected.connect(self._on_lf6_disconnected)

    def _update_stem_preview(self):
        ts = _now_ts()
        stem = _build_stem(
            self._sample_edit.text(), self._note_edit.text(),
            self._suffix_edit.text(), ts, self._run_idx_spin.value()
        )
        self._stem_lbl.setText(f"{stem}.csv")

    def _update_est(self):
        est = (self._exp_spin.value() / 1000.0) * self._epf_spin.value()
        mult = 2 if self._warmup_chk.isChecked() else 1
        self._est_lbl.setText(f"≈ {est * mult:.1f}s total")

    # ── LF6 connection slots ──────────────────────────────────────────────────

    @Slot(list)
    def _on_lf6_connected(self, _experiments):
        self._acquire_btn.setEnabled(True)
        self._status_lbl.setText("Connected")
        self._status_lbl.setStyleSheet("color: green;")

    @Slot()
    def _on_lf6_disconnected(self):
        self._acquire_btn.setEnabled(False)
        self._status_lbl.setText("Disconnected")
        self._status_lbl.setStyleSheet("color: gray;")

    # ── acquire ───────────────────────────────────────────────────────────────

    @Slot()
    def _on_acquire(self):
        self._acquire_btn.setEnabled(False)
        self._status_lbl.setText("Acquiring…")
        self._status_lbl.setStyleSheet("color: orange;")

        self._worker = _AcquireWorker(
            lf6_ctrl     = self._ctrl,
            roi_mode     = self._roi_combo.currentText(),
            center_nm    = self._center_spin.value(),
            exposure_ms  = self._exp_spin.value(),
            epf          = self._epf_spin.value(),
            auto_apply   = self._auto_apply_chk.isChecked(),
            warmup       = self._warmup_chk.isChecked(),
        )
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.result.connect(self._on_result)
        self._worker.status.connect(self._status_lbl.setText)
        self._worker.error.connect(
            lambda msg: (
                self._status_lbl.setText(f"Error: {msg[:80]}"),
                self._status_lbl.setStyleSheet("color: red;"),
            )
        )
        self._worker.finished.connect(self._on_acquire_done)
        self._thread.start()

    @Slot(object, object)
    def _on_result(self, data: np.ndarray, wls: np.ndarray):
        self._raw_data = data
        self._raw_wls  = wls
        self._display.update_data(data, wls)
        self._save_csv_btn.setEnabled(True)
        self._save_png_btn.setEnabled(True)
        self._apply_bg_btn.setEnabled(True)

    @Slot()
    def _on_acquire_done(self):
        self._acquire_btn.setEnabled(self._ctrl is not None and self._ctrl.is_connected)
        if self._thread:
            self._thread.quit()
            self._thread.wait()

    @Slot()
    def _on_clear(self):
        self._raw_data = None
        self._raw_wls  = np.array([])
        self._display._curve.setData([], [])
        self._display._imgview.setImage(np.zeros((1, 1)))
        self._save_csv_btn.setEnabled(False)
        self._save_png_btn.setEnabled(False)
        self._apply_bg_btn.setEnabled(False)
        self._status_lbl.setText("Cleared")
        self._status_lbl.setStyleSheet("color: gray;")

    # ── background ────────────────────────────────────────────────────────────

    @Slot()
    def _on_apply_bg(self):
        if self._raw_data is None:
            return
        ok, out, msg = self._bg_panel.apply_to(self._raw_data, self._raw_wls)
        if ok:
            self._display.update_data(out, self._raw_wls)
            self._status_lbl.setText(msg)
            self._status_lbl.setStyleSheet("color: green;")
        else:
            # revert to raw
            self._display.update_data(self._raw_data, self._raw_wls)
            self._status_lbl.setText(msg)
            self._status_lbl.setStyleSheet("color: orange;")

    # ── save ──────────────────────────────────────────────────────────────────

    def _out_path(self) -> Path:
        sample = _sanitize(self._sample_edit.text()) or "Sample"
        return Path(cfg.filename.base_out) / sample / "bfp"

    def _next_stem(self) -> str:
        ts = _now_ts()
        return _build_stem(
            self._sample_edit.text(), self._note_edit.text(),
            self._suffix_edit.text(), ts, self._run_idx_spin.value()
        )

    def _bump_run_idx(self):
        if self._auto_inc_chk.isChecked():
            self._run_idx_spin.setValue(self._run_idx_spin.value() + 1)

    @Slot()
    def _on_save_csv(self):
        data, wls = self._display.get_data()
        if data is None:
            return
        out = self._out_path()
        out.mkdir(parents=True, exist_ok=True)
        stem = self._next_stem()
        fp = out / f"{stem}.csv"
        try:
            _save_csv(fp, data, wls)
            self._status_lbl.setText(f"Saved → {fp.name}")
            self._status_lbl.setStyleSheet("color: green;")
            self._bump_run_idx()
        except Exception as e:
            self._status_lbl.setText(f"Save error: {e}")
            self._status_lbl.setStyleSheet("color: red;")

    @Slot()
    def _on_save_png(self):
        data, wls = self._display.get_data()
        if data is None:
            return
        out = self._out_path()
        out.mkdir(parents=True, exist_ok=True)
        stem = self._next_stem()
        fp = out / f"{stem}.png"
        try:
            _save_png(fp, data, wls,
                      scale=self._display._scale_combo.currentText(),
                      cmap=self._display._cmap_combo.currentText())
            self._status_lbl.setText(f"Saved → {fp.name}")
            self._status_lbl.setStyleSheet("color: green;")
        except Exception as e:
            self._status_lbl.setText(f"PNG error: {e}")
            self._status_lbl.setStyleSheet("color: red;")


# ── PNG save helper ───────────────────────────────────────────────────────────

def _save_png(path: Path, data: np.ndarray, wls: np.ndarray,
              scale: str = "Linear", cmap: str = "gray") -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        raise RuntimeError("matplotlib required for PNG save")

    arr = np.asarray(data, dtype=float)

    if arr.ndim == 2:
        img, wls_x = _orient_wl_columns(arr, wls)
        if scale.lower().startswith("log"):
            img_show = np.log1p(np.clip(img, 0, None))
        else:
            img_show = img
        lo, hi = np.nanpercentile(img_show, [1, 99])
        if not np.isfinite(lo): lo = 0.0
        if not np.isfinite(hi) or hi <= lo: hi = lo + 1.0

        fig, ax = plt.subplots(figsize=(7, 3.8), dpi=160)
        extent = None
        if wls_x.size == img_show.shape[1]:
            extent = (float(wls_x[0]), float(wls_x[-1]), 0, img_show.shape[0] - 1)
        ax.imshow(img_show, origin="lower", aspect="auto", extent=extent,
                  cmap=cmap, vmin=lo, vmax=hi, interpolation="nearest")
        ax.set_xlabel("Wavelength (nm)" if extent else "X (px)")
        ax.set_ylabel("Y (px)")
        fig.colorbar(ax.images[0], ax=ax, fraction=0.046, pad=0.04)
        fig.tight_layout()
        fig.savefig(path, dpi=200, bbox_inches="tight")
        plt.close(fig)

    elif arr.ndim == 1:
        y = arr.ravel()
        x = np.asarray(wls, dtype=float).ravel()
        if x.size != y.size:
            x = np.arange(y.size, dtype=float)
        fig, ax = plt.subplots(figsize=(7.5, 3.0), dpi=160)
        ax.plot(x, y, linewidth=1.0)
        ax.set_xlabel("Wavelength (nm)" if wls is not None and np.asarray(wls).size == y.size else "Pixel")
        ax.set_ylabel("Intensity")
        ax.grid(True, alpha=0.2)
        fig.tight_layout()
        fig.savefig(path, dpi=200, bbox_inches="tight")
        plt.close(fig)
    else:
        raise ValueError(f"Unsupported shape for PNG: {arr.shape}")
