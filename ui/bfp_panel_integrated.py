from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pyqtgraph as pg
from matplotlib.figure import Figure
from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot, QRectF
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from utils.bfp_analysis import (
    compute_rc_contrast,
    compute_rc_division,
    compute_rc_subtract,
    limits_from_pair,
    moving_average_1d,
    rc_suffix_brc,
    rc_suffix_frc,
    repeated_gradient,
    validate_odd_window,
)
from utils.bfp_io import (
    FullImageData,
    detect_csv_mode,
    interpolate_1d_to_reference,
    interp_image_to_grid,
    load_binned_csv,
    load_full_image_csv,
    save_binned_csv,
    save_csv_atomic,
    save_full_image_csv,
)
from utils.config import cfg

pg.setConfigOption("background", "w")
pg.setConfigOption("foreground", "k")

MATERIAL_OPTIONS = ["tBN", "bBN", "biBN", "monoWS2", "monoWSe2", "WSe2/WS2", "Custom"]
EXPERIMENT_OPTIONS = ["PL", "Ref"]


def _sanitize(text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    text = re.sub(r'[\\/:*?"<>|]', "-", text)
    text = re.sub(r"\s+", "", text)
    return text


def _optional_float(text: str, field_name: str) -> Optional[float]:
    text = str(text).strip()
    if text == "":
        return None
    try:
        return float(text)
    except Exception as exc:
        raise ValueError(f"{field_name} must be a number or blank.") from exc


def _now_time() -> str:
    return datetime.now().strftime("%H%M%S")


def _get_wls(spec) -> np.ndarray:
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


def _do_raw_acquire(setup, roi_mode: str) -> np.ndarray:
    roi = (roi_mode or "").strip().lower()
    if roi.startswith("full") and hasattr(setup, "acquire_2d"):
        return np.asarray(setup.acquire_2d())
    return np.asarray(setup.acquire())


def _normalize_acquired_data(raw: np.ndarray, wl: np.ndarray, requested_mode: str) -> tuple[str, tuple]:
    arr = np.asarray(raw, dtype=float)
    wl = np.asarray(wl, dtype=float).ravel()
    if arr.ndim == 1 and wl.size == arr.size:
        return "binned", (wl, arr)
    if arr.ndim == 2:
        if wl.size == arr.shape[1]:
            return "full", (wl, np.arange(arr.shape[0], dtype=float), arr)
        if wl.size == arr.shape[0]:
            return "full", (wl, np.arange(arr.shape[1], dtype=float), arr.T)
    if requested_mode.startswith("bin"):
        x = wl if wl.size == arr.size else np.arange(arr.size, dtype=float)
        return "binned", (x, arr.ravel())
    if arr.ndim == 2:
        x = wl if wl.size == arr.shape[1] else np.arange(arr.shape[1], dtype=float)
        return "full", (x, np.arange(arr.shape[0], dtype=float), arr)
    x = wl if wl.size == arr.size else np.arange(arr.size, dtype=float)
    return "binned", (x, arr.ravel())


def _next_run_index(save_dir: Path, prefix_without_time_and_run: str) -> int:
    if not save_dir.is_dir():
        return 1
    patt = re.compile(re.escape(prefix_without_time_and_run) + r"_\d{6}_(\d{3})$")
    max_run = 0
    for file in save_dir.iterdir():
        if file.is_file():
            match = patt.fullmatch(file.stem)
            if match:
                max_run = max(max_run, int(match.group(1)))
    return max_run + 1


def _save_figure_atomic(path: Path, fig: Figure, dpi: int = 300) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fig.savefig(tmp, dpi=dpi, bbox_inches="tight")
    if not tmp.exists() or tmp.stat().st_size == 0:
        raise IOError(f"Failed to create image file: {path}")
    tmp.replace(path)


def _save_png(path: Path, data: np.ndarray, wls: np.ndarray, y_axis: Optional[np.ndarray] = None, scale: str = "Linear", cmap: str = "gray") -> None:
    arr = np.asarray(data, dtype=float)
    fig = Figure(figsize=(7.5, 4.2), dpi=140)
    ax = fig.add_subplot(111)
    if arr.ndim == 2:
        img_show = np.log1p(np.clip(arr, 0, None)) if scale.lower().startswith("log") else arr
        x = np.asarray(wls, dtype=float).ravel()
        y = np.asarray(y_axis, dtype=float).ravel() if y_axis is not None else np.arange(arr.shape[0], dtype=float)
        extent = [float(x.min()), float(x.max()), float(y.min()), float(y.max())] if x.size == arr.shape[1] else None
        im = ax.imshow(img_show, origin="lower", aspect="auto", extent=extent, cmap=cmap, interpolation="nearest")
        ax.set_xlabel("Wavelength (nm)" if extent else "X (px)")
        ax.set_ylabel("Pixel")
        fig.colorbar(im, ax=ax, fraction=0.04, pad=0.03)
    else:
        y = arr.ravel()
        x = np.asarray(wls, dtype=float).ravel()
        if x.size != y.size:
            x = np.arange(y.size, dtype=float)
        ax.plot(x, y, linewidth=1.1)
        ax.set_xlabel("Wavelength (nm)" if np.asarray(wls).size == y.size else "Pixel")
        ax.set_ylabel("Intensity")
        ax.grid(True, alpha=0.2)
    fig.tight_layout()
    _save_figure_atomic(path, fig, dpi=300)


def _axis_rect(x_axis: np.ndarray, y_axis: np.ndarray) -> QRectF:
    def _bounds(values: np.ndarray) -> tuple[float, float]:
        arr = np.asarray(values, dtype=float).ravel()
        if arr.size == 0:
            return 0.0, 1.0
        if arr.size == 1:
            return float(arr[0]) - 0.5, 1.0
        step = float(np.median(np.diff(arr)))
        if not np.isfinite(step) or abs(step) < 1e-12:
            step = 1.0
        start = float(arr[0] - step / 2.0)
        end = float(arr[-1] + step / 2.0)
        origin = min(start, end)
        span = abs(end - start)
        return origin, span if span > 0 else 1.0

    x0, width = _bounds(x_axis)
    y0, height = _bounds(y_axis)
    return QRectF(x0, y0, width, height)


def _set_image_axes(image_view: pg.ImageView, image: np.ndarray, x_axis: np.ndarray, y_axis: np.ndarray, *, auto_levels: bool = True, auto_range: bool = True) -> None:
    image_view.setImage(np.asarray(image, dtype=float).T, autoLevels=auto_levels, autoRange=auto_range)
    rect = _axis_rect(x_axis, y_axis)
    image_view.getImageItem().setRect(rect)
    if auto_range:
        x0 = float(rect.left())
        x1 = float(rect.left() + rect.width())
        y0 = float(rect.top())
        y1 = float(rect.top() + rect.height())
        if hasattr(image_view, "view") and image_view.view is not None:
            image_view.view.setXRange(x0, x1, padding=0.0)
            image_view.view.setYRange(y0, y1, padding=0.0)


def _auto_image_levels(image: np.ndarray) -> tuple[float, float]:
    arr = np.asarray(image, dtype=float)
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return 0.0, 1.0
    lo, hi = np.nanpercentile(finite, [1, 99])
    if not np.isfinite(lo):
        lo = float(np.nanmin(finite))
    if not np.isfinite(hi):
        hi = float(np.nanmax(finite))
    if hi <= lo:
        hi = lo + 1.0
    return float(lo), float(hi)


@dataclass
class _AcquiredRecord:
    data: np.ndarray
    wls: np.ndarray
    mode: str
    y_axis: Optional[np.ndarray] = None
    csv_path: Optional[Path] = None


class _AcquireWorker(QObject):
    result = Signal(object)
    error = Signal(str)
    status = Signal(str)
    finished = Signal()

    def __init__(self, lf6_ctrl, roi_mode: str, center_nm: float, exposure_ms: float, epf: int, repeat: int, auto_apply: bool, warmup: bool):
        super().__init__()
        self._ctrl = lf6_ctrl
        self._roi_mode = roi_mode
        self._center_nm = center_nm
        self._exposure_ms = exposure_ms
        self._epf = int(epf)
        self._repeat = max(1, int(repeat))
        self._auto_apply = auto_apply
        self._warmup = warmup

    @Slot()
    def run(self):
        try:
            spec = self._ctrl.adapter if self._ctrl and self._ctrl.is_connected else None
            if spec is None:
                raise RuntimeError("LF6 not connected.")
            setup = getattr(spec, "setup", spec)
            if self._auto_apply:
                self.status.emit("Applying settings...")
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
                if roi.startswith("full") and hasattr(setup, "change_roi_FullSensor"):
                    setup.change_roi_FullSensor()
                elif ("bin" in roi or "line" in roi) and hasattr(setup, "change_roi_LineSensor"):
                    setup.change_roi_LineSensor()
            if self._warmup:
                self.status.emit("Warm-up acquisition...")
                try:
                    _do_raw_acquire(setup, self._roi_mode)
                except Exception:
                    pass
            stack = []
            for idx in range(self._repeat):
                self.status.emit(f"Acquiring frame {idx + 1}/{self._repeat}...")
                stack.append(np.asarray(_do_raw_acquire(setup, self._roi_mode), dtype=float))
            raw = np.mean(np.stack(stack, axis=0), axis=0)
            wls = _get_wls(spec)
            mode, payload = _normalize_acquired_data(raw, wls, self._roi_mode.lower())
            if mode == "binned":
                wl_plot, spectrum = payload
                record = _AcquiredRecord(np.asarray(spectrum, dtype=float), np.asarray(wl_plot, dtype=float), mode="binned")
            else:
                wl_plot, y_axis, image = payload
                record = _AcquiredRecord(np.asarray(image, dtype=float), np.asarray(wl_plot, dtype=float), mode="full", y_axis=np.asarray(y_axis, dtype=float))
            self.result.emit(record)
            self.status.emit("Done.")
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class _DisplayWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._tabs = QTabWidget()
        lay.addWidget(self._tabs, stretch=1)
        self._imgview = pg.ImageView(view=pg.PlotItem())
        self._imgview.ui.roiBtn.hide()
        self._imgview.ui.menuBtn.hide()
        self._imgview.setMinimumHeight(520)
        self._imgview.getView().setAspectLocked(False)
        self._imgview.view.setLabel("bottom", "Wavelength", units="nm")
        self._imgview.view.setLabel("left", "Y Pixel")
        self._imgview.view.showGrid(x=True, y=True, alpha=0.2)
        self._tabs.addTab(self._imgview, "2D Frame")
        self._pw = pg.PlotWidget()
        self._pw.setLabel("bottom", "Wavelength", units="nm")
        self._pw.setLabel("left", "Intensity")
        self._pw.showGrid(x=True, y=True, alpha=0.3)
        self._curve = self._pw.plot(pen=pg.mkPen("#1565C0", width=1.5))
        self._tabs.addTab(self._pw, "1D Spectrum")
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Scale:"))
        self._scale_combo = QComboBox()
        self._scale_combo.addItems(["Linear", "Log"])
        ctrl.addWidget(self._scale_combo)
        ctrl.addWidget(QLabel("Cmap:"))
        self._cmap_combo = QComboBox()
        self._cmap_combo.addItems(["gray", "viridis", "plasma", "magma", "inferno"])
        ctrl.addWidget(self._cmap_combo)
        self._auto_color_chk = QCheckBox("Auto Color")
        self._auto_color_chk.setChecked(True)
        ctrl.addWidget(self._auto_color_chk)
        ctrl.addStretch()
        self._shape_lbl = QLabel("No data")
        ctrl.addWidget(self._shape_lbl)
        lay.addLayout(ctrl)
        self._data: Optional[np.ndarray] = None
        self._wls = np.array([])
        self._y_axis: Optional[np.ndarray] = None
        self._cmap_combo.currentTextChanged.connect(self._apply_cmap)
        self._auto_color_chk.toggled.connect(self._refresh_image_levels)
        self._apply_cmap("gray")

    def _apply_cmap(self, name: str):
        try:
            self._imgview.setColorMap(pg.colormap.get(name))
        except Exception:
            pass

    def update_data(self, data: np.ndarray, wls: np.ndarray, y_axis: Optional[np.ndarray] = None):
        self._data = np.asarray(data, dtype=float)
        self._wls = np.asarray(wls, dtype=float).ravel()
        self._y_axis = None if y_axis is None else np.asarray(y_axis, dtype=float).ravel()
        if self._data.ndim == 2:
            y_map = self._y_axis if self._y_axis is not None and self._y_axis.size == self._data.shape[0] else np.arange(self._data.shape[0], dtype=float)
            x_map = self._wls if self._wls.size == self._data.shape[1] else np.arange(self._data.shape[1], dtype=float)
            _set_image_axes(self._imgview, self._data, x_map, y_map, auto_levels=self._auto_color_chk.isChecked(), auto_range=True)
            self._apply_cmap(self._cmap_combo.currentText())
            self._refresh_image_levels()
            self._shape_lbl.setText(f"Shape: {self._data.shape[0]}x{self._data.shape[1]}")
            self._tabs.setCurrentIndex(0)
        else:
            y = self._data.ravel()
            x = self._wls if self._wls.size == y.size else np.arange(y.size, dtype=float)
            self._curve.setData(x, y)
            self._shape_lbl.setText(f"Points: {y.size}")
            self._tabs.setCurrentIndex(1)

    def clear(self):
        self._data = None
        self._wls = np.array([])
        self._y_axis = None
        self._curve.setData([], [])
        self._imgview.setImage(np.zeros((1, 1)))
        self._shape_lbl.setText("No data")

    def _refresh_image_levels(self):
        if self._data is None or self._data.ndim != 2:
            return
        if self._auto_color_chk.isChecked():
            self._imgview.setLevels(*_auto_image_levels(self._data))


class _BgPanel(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("Background Subtraction (R-R0)/R0", parent)
        lay = QFormLayout(self)
        self._enable_chk = QCheckBox("Enable")
        lay.addRow("", self._enable_chk)
        row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setReadOnly(True)
        self._browse_btn = QPushButton("Browse...")
        row.addWidget(self._path_edit)
        row.addWidget(self._browse_btn)
        lay.addRow("BG file:", row)
        self._tol_spin = QDoubleSpinBox()
        self._tol_spin.setRange(0, 100)
        self._tol_spin.setDecimals(6)
        self._tol_spin.setValue(0.001)
        self._tol_spin.setSuffix(" nm")
        lay.addRow("Lambda tol:", self._tol_spin)
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: gray; font-size: 10px;")
        lay.addRow("", self._status_lbl)
        self._bg_data: Optional[np.ndarray] = None
        self._bg_wls = np.array([])
        self._browse_btn.clicked.connect(self._on_browse)

    @Slot()
    def _on_browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select background CSV", str(cfg.base_out), "CSV files (*.csv)")
        if path:
            self._path_edit.setText(path)
            try:
                mode = detect_csv_mode(path)
                if mode == "binned":
                    wl, y = load_binned_csv(path)
                    self._bg_data = y
                    self._bg_wls = wl
                else:
                    full = load_full_image_csv(path)
                    self._bg_data = full.image
                    self._bg_wls = full.wl
                self._status_lbl.setText(f"Loaded: {Path(path).name}")
                self._status_lbl.setStyleSheet("color: green; font-size: 10px;")
            except Exception as exc:
                self._bg_data = None
                self._bg_wls = np.array([])
                self._status_lbl.setText(f"Error: {exc}")
                self._status_lbl.setStyleSheet("color: red; font-size: 10px;")

    def apply_to(self, data: np.ndarray, wls: np.ndarray) -> tuple[bool, np.ndarray, str]:
        if not self._enable_chk.isChecked() or self._bg_data is None:
            return False, np.asarray(data, dtype=float), "Background not applied."
        meas = np.asarray(data, dtype=float)
        bg = np.asarray(self._bg_data, dtype=float)
        if meas.shape != bg.shape or meas.ndim != bg.ndim:
            return False, meas, f"shape mismatch {meas.shape} vs {bg.shape}"
        mw = np.asarray(wls, dtype=float).ravel()
        bw = np.asarray(self._bg_wls, dtype=float).ravel()
        if mw.size and bw.size and mw.size == bw.size:
            delta = float(np.nanmax(np.abs(mw - bw)))
            if delta > self._tol_spin.value():
                return False, meas, f"lambda mismatch: max |dLambda|={delta:.4g} nm"
        out = np.where(np.abs(bg) > 1e-12, (meas - bg) / bg, np.nan)
        return True, out, "(R-R0)/R0 applied"


class _BRCWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._wl: Optional[np.ndarray] = None
        self._sample: Optional[np.ndarray] = None
        self._bg_scaled: Optional[np.ndarray] = None
        self._result: Optional[np.ndarray] = None
        self._build()
        self._wire()
        self.load_config()

    def _build(self):
        root = QVBoxLayout(self)
        controls = QGridLayout()
        self._sample_edit = QLineEdit()
        self._bg_edit = QLineEdit()
        self._browse_sample_btn = QPushButton("Browse...")
        self._browse_bg_btn = QPushButton("Browse...")
        controls.addWidget(QLabel("Sample CSV:"), 0, 0)
        controls.addWidget(self._sample_edit, 0, 1)
        controls.addWidget(self._browse_sample_btn, 0, 2)
        controls.addWidget(QLabel("Background CSV:"), 1, 0)
        controls.addWidget(self._bg_edit, 1, 1)
        controls.addWidget(self._browse_bg_btn, 1, 2)
        self._scale_spin = QDoubleSpinBox()
        self._scale_spin.setRange(0.0, 3.0)
        self._scale_spin.setDecimals(2)
        self._scale_spin.setSingleStep(0.01)
        self._mode_combo = QComboBox()
        self._mode_combo.addItems(["contrast", "subtract", "division", "derivative"])
        self._smooth_spin = QSpinBox()
        self._smooth_spin.setRange(1, 9999)
        self._order_combo = QComboBox()
        self._order_combo.addItems(["1", "2", "3"])
        controls.addWidget(QLabel("Scale c:"), 2, 0)
        controls.addWidget(self._scale_spin, 2, 1)
        controls.addWidget(QLabel("Mode:"), 2, 2)
        controls.addWidget(self._mode_combo, 2, 3)
        controls.addWidget(QLabel("Smooth:"), 3, 0)
        controls.addWidget(self._smooth_spin, 3, 1)
        controls.addWidget(QLabel("Order:"), 3, 2)
        controls.addWidget(self._order_combo, 3, 3)
        self._xmin_edit = QLineEdit()
        self._xmax_edit = QLineEdit()
        self._ymin_edit = QLineEdit()
        self._ymax_edit = QLineEdit()
        controls.addWidget(QLabel("X min:"), 4, 0)
        controls.addWidget(self._xmin_edit, 4, 1)
        controls.addWidget(QLabel("X max:"), 4, 2)
        controls.addWidget(self._xmax_edit, 4, 3)
        controls.addWidget(QLabel("Y min:"), 5, 0)
        controls.addWidget(self._ymin_edit, 5, 1)
        controls.addWidget(QLabel("Y max:"), 5, 2)
        controls.addWidget(self._ymax_edit, 5, 3)
        self._compute_btn = QPushButton("Compute")
        self._save_csv_btn = QPushButton("Save CSV")
        self._save_png_btn = QPushButton("Save PNG")
        self._auto_color_chk = QCheckBox("Auto Color")
        self._auto_color_chk.setChecked(True)
        self._save_csv_btn.setEnabled(False)
        self._save_png_btn.setEnabled(False)
        controls.addWidget(self._compute_btn, 6, 2)
        controls.addWidget(self._save_csv_btn, 6, 3)
        controls.addWidget(self._save_png_btn, 6, 4)
        controls.addWidget(self._auto_color_chk, 6, 1)
        root.addLayout(controls)
        plots = QHBoxLayout()
        self._left_plot = pg.PlotWidget()
        self._left_plot.showGrid(x=True, y=True, alpha=0.25)
        self._left_plot.addLegend()
        self._sample_curve = self._left_plot.plot(pen=pg.mkPen("#1565C0", width=1.5), name="Sample")
        self._bg_curve = self._left_plot.plot(pen=pg.mkPen("#C62828", width=1.5), name="Scaled BG")
        self._right_plot = pg.PlotWidget()
        self._right_plot.showGrid(x=True, y=True, alpha=0.25)
        self._result_curve = self._right_plot.plot(pen=pg.mkPen("#2E7D32", width=1.5))
        plots.addWidget(self._left_plot, 1)
        plots.addWidget(self._right_plot, 1)
        root.addLayout(plots, stretch=1)
        self._info_lbl = QLabel("")
        root.addWidget(self._info_lbl)

    def _wire(self):
        self._browse_sample_btn.clicked.connect(lambda: self._browse_target(self._sample_edit))
        self._browse_bg_btn.clicked.connect(lambda: self._browse_target(self._bg_edit))
        self._compute_btn.clicked.connect(self.compute)
        self._save_csv_btn.clicked.connect(self.save_csv)
        self._save_png_btn.clicked.connect(self.save_png)
        for widget in (self._sample_edit, self._bg_edit, self._xmin_edit, self._xmax_edit, self._ymin_edit, self._ymax_edit):
            widget.editingFinished.connect(self.compute)
        self._mode_combo.currentTextChanged.connect(self.compute)
        self._scale_spin.valueChanged.connect(self.compute)
        self._smooth_spin.valueChanged.connect(self.compute)
        self._order_combo.currentTextChanged.connect(self.compute)

    def _browse_target(self, target: QLineEdit):
        path, _ = QFileDialog.getOpenFileName(self, "Select binned CSV", str(cfg.base_out), "CSV files (*.csv)")
        if path:
            target.setText(path)
            self.compute()

    def load_config(self):
        rc = cfg.bfp_rc
        self._sample_edit.setText(rc.brc_sample)
        self._bg_edit.setText(rc.brc_bg)
        self._scale_spin.setValue(float(rc.brc_scale))
        self._mode_combo.setCurrentText(rc.brc_calc)
        self._smooth_spin.setValue(int(rc.brc_smooth_window))
        self._order_combo.setCurrentText(str(rc.brc_diff_order))
        self._xmin_edit.setText(rc.brc_xmin)
        self._xmax_edit.setText(rc.brc_xmax)
        self._ymin_edit.setText(rc.brc_ymin)
        self._ymax_edit.setText(rc.brc_ymax)

    def save_config(self):
        rc = cfg.bfp_rc
        rc.brc_sample = self._sample_edit.text().strip()
        rc.brc_bg = self._bg_edit.text().strip()
        rc.brc_scale = float(self._scale_spin.value())
        rc.brc_calc = self._mode_combo.currentText()
        rc.brc_smooth_window = int(self._smooth_spin.value())
        rc.brc_diff_order = int(self._order_combo.currentText())
        rc.brc_xmin = self._xmin_edit.text().strip()
        rc.brc_xmax = self._xmax_edit.text().strip()
        rc.brc_ymin = self._ymin_edit.text().strip()
        rc.brc_ymax = self._ymax_edit.text().strip()

    def set_sample_path(self, path: Path):
        self._sample_edit.setText(str(path))
        self.compute()

    def set_background_path(self, path: Path):
        self._bg_edit.setText(str(path))
        self.compute()

    @Slot()
    def compute(self):
        self.save_config()
        sample_path = self._sample_edit.text().strip()
        bg_path = self._bg_edit.text().strip()
        if not sample_path or not bg_path:
            return
        try:
            if detect_csv_mode(sample_path) != "binned" or detect_csv_mode(bg_path) != "binned":
                raise ValueError("Binned RC tab only accepts binned CSV files.")
            wl_s, s = load_binned_csv(sample_path)
            wl_b, b = load_binned_csv(bg_path)
            wl_common, b_i = interpolate_1d_to_reference(wl_s, wl_b, b)
            s_i = np.interp(wl_common, wl_s, s)
            scale = self._scale_spin.value()
            b_scaled = scale * b_i
            mode = self._mode_combo.currentText()
            if mode == "contrast":
                result = compute_rc_contrast(s_i, b_i, scale=scale)
            elif mode == "subtract":
                result = compute_rc_subtract(s_i, b_i, scale=scale)
            elif mode == "division":
                result = compute_rc_division(s_i, b_i, scale=scale)
            else:
                smooth_window = validate_odd_window(self._smooth_spin.value(), wl_common.size)
                rc_curve = compute_rc_contrast(s_i, b_i, scale=scale)
                result = repeated_gradient(wl_common, moving_average_1d(rc_curve, smooth_window), int(self._order_combo.currentText()))
            x0 = _optional_float(self._xmin_edit.text(), "x min")
            x1 = _optional_float(self._xmax_edit.text(), "x max")
            y0 = _optional_float(self._ymin_edit.text(), "y min")
            y1 = _optional_float(self._ymax_edit.text(), "y max")
            x_lo, x_hi = limits_from_pair(wl_common, x0, x1)
            mask = (wl_common >= x_lo) & (wl_common <= x_hi)
            if mask.sum() < 2:
                raise ValueError("Selected x range does not overlap the data.")
            wl_show = wl_common[mask]
            result_show = result[mask]
            sample_show = s_i[mask]
            bg_show = b_scaled[mask]
            self._sample_curve.setData(wl_show, sample_show)
            self._bg_curve.setData(wl_show, bg_show)
            self._result_curve.setData(wl_show, result_show)
            if y0 is not None or y1 is not None:
                y_lo = float(np.nanmin(result_show)) if y0 is None else y0
                y_hi = float(np.nanmax(result_show)) if y1 is None else y1
                self._right_plot.setYRange(y_lo, y_hi, padding=0.0)
            self._wl = wl_show
            self._sample = sample_show
            self._bg_scaled = bg_show
            self._result = result_show
            self._save_csv_btn.setEnabled(True)
            self._save_png_btn.setEnabled(True)
            self._info_lbl.setText(f"Binned result ready | x = {wl_show.min():.3f} to {wl_show.max():.3f} nm")
        except Exception as exc:
            self._info_lbl.setText(f"Binned RC failed: {exc}")

    def save_csv(self):
        if self._wl is None or self._result is None:
            return
        try:
            sample_path = Path(self._sample_edit.text().strip())
            suffix = rc_suffix_brc(self._mode_combo.currentText(), int(self._order_combo.currentText()))
            out = sample_path.with_name(f"{sample_path.stem}_{suffix}_c-{self._scale_spin.value():.2f}.csv")
            save_csv_atomic(out, save_binned_csv, self._wl, self._result)
            self._info_lbl.setText(f"Saved {out.name}")
        except Exception as exc:
            self._info_lbl.setText(f"Save failed: {exc}")

    def save_png(self):
        if self._wl is None or self._result is None or self._sample is None or self._bg_scaled is None:
            return
        try:
            sample_path = Path(self._sample_edit.text().strip())
            suffix = rc_suffix_brc(self._mode_combo.currentText(), int(self._order_combo.currentText()))
            out = sample_path.with_name(f"{sample_path.stem}_{suffix}_c-{self._scale_spin.value():.2f}.png")
            fig = Figure(figsize=(12, 5), dpi=120)
            ax_left = fig.add_subplot(121)
            ax_right = fig.add_subplot(122)
            ax_left.plot(self._wl, self._bg_scaled, linewidth=1.5, label=f"Background x {self._scale_spin.value():.2f}")
            ax_left.plot(self._wl, self._sample, linewidth=1.5, label="Sample")
            ax_left.set_xlabel("Wavelength (nm)")
            ax_left.set_ylabel("Intensity")
            ax_left.grid(True, alpha=0.25)
            ax_left.legend(loc="best")
            ax_right.plot(self._wl, self._result, linewidth=1.5)
            ax_right.set_xlabel("Wavelength (nm)")
            ax_right.grid(True, alpha=0.25)
            fig.tight_layout()
            _save_figure_atomic(out, fig, dpi=300)
            self._info_lbl.setText(f"Saved {out.name}")
        except Exception as exc:
            self._info_lbl.setText(f"Save failed: {exc}")


class _FRCWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._sample_data: Optional[FullImageData] = None
        self._bg_data: Optional[FullImageData] = None
        self._display_img: Optional[np.ndarray] = None
        self._display_wl: Optional[np.ndarray] = None
        self._display_y: Optional[np.ndarray] = None
        self._display_key: str = "contrast"
        self._build()
        self._wire()
        self.load_config()

    def _build(self):
        root = QVBoxLayout(self)
        controls = QGridLayout()
        self._sample_edit = QLineEdit()
        self._bg_edit = QLineEdit()
        self._browse_sample_btn = QPushButton("Browse...")
        self._browse_bg_btn = QPushButton("Browse...")
        controls.addWidget(QLabel("Sample CSV:"), 0, 0)
        controls.addWidget(self._sample_edit, 0, 1)
        controls.addWidget(self._browse_sample_btn, 0, 2)
        controls.addWidget(QLabel("Background CSV:"), 1, 0)
        controls.addWidget(self._bg_edit, 1, 1)
        controls.addWidget(self._browse_bg_btn, 1, 2)
        self._display_combo = QComboBox()
        self._display_combo.addItems(["result", "sample", "background"])
        self._calc_combo = QComboBox()
        self._calc_combo.addItems(["contrast", "subtract", "division"])
        controls.addWidget(QLabel("Show:"), 2, 0)
        controls.addWidget(self._display_combo, 2, 1)
        controls.addWidget(QLabel("Calc:"), 2, 2)
        controls.addWidget(self._calc_combo, 2, 3)
        self._xmin_edit = QLineEdit()
        self._xmax_edit = QLineEdit()
        self._ymin_edit = QLineEdit()
        self._ymax_edit = QLineEdit()
        self._zmin_edit = QLineEdit()
        self._zmax_edit = QLineEdit()
        controls.addWidget(QLabel("X min:"), 3, 0)
        controls.addWidget(self._xmin_edit, 3, 1)
        controls.addWidget(QLabel("X max:"), 3, 2)
        controls.addWidget(self._xmax_edit, 3, 3)
        controls.addWidget(QLabel("Y min:"), 4, 0)
        controls.addWidget(self._ymin_edit, 4, 1)
        controls.addWidget(QLabel("Y max:"), 4, 2)
        controls.addWidget(self._ymax_edit, 4, 3)
        controls.addWidget(QLabel("Z min:"), 5, 0)
        controls.addWidget(self._zmin_edit, 5, 1)
        controls.addWidget(QLabel("Z max:"), 5, 2)
        controls.addWidget(self._zmax_edit, 5, 3)
        self._compute_btn = QPushButton("Compute")
        self._save_csv_btn = QPushButton("Save CSV")
        self._save_png_btn = QPushButton("Save PNG")
        self._auto_color_chk = QCheckBox("Auto Color")
        self._auto_color_chk.setChecked(True)
        self._save_csv_btn.setEnabled(False)
        self._save_png_btn.setEnabled(False)
        controls.addWidget(self._auto_color_chk, 6, 1)
        controls.addWidget(self._compute_btn, 6, 2)
        controls.addWidget(self._save_csv_btn, 6, 3)
        controls.addWidget(self._save_png_btn, 6, 4)
        root.addLayout(controls)
        self._image = pg.ImageView(view=pg.PlotItem())
        self._image.ui.roiBtn.hide()
        self._image.ui.menuBtn.hide()
        self._image.setMinimumHeight(520)
        self._image.getView().setAspectLocked(False)
        self._image.view.setLabel("bottom", "Wavelength", units="nm")
        self._image.view.setLabel("left", "Y Pixel")
        self._image.view.showGrid(x=True, y=True, alpha=0.2)
        try:
            self._image.setColorMap(pg.colormap.get("gray"))
        except Exception:
            pass
        root.addWidget(self._image, stretch=1)
        self._info_lbl = QLabel("")
        root.addWidget(self._info_lbl)

    def _wire(self):
        self._browse_sample_btn.clicked.connect(lambda: self._browse_target(self._sample_edit))
        self._browse_bg_btn.clicked.connect(lambda: self._browse_target(self._bg_edit))
        self._compute_btn.clicked.connect(self.compute)
        self._save_csv_btn.clicked.connect(self.save_csv)
        self._save_png_btn.clicked.connect(self.save_png)
        self._auto_color_chk.toggled.connect(self.compute)
        for widget in (self._sample_edit, self._bg_edit, self._xmin_edit, self._xmax_edit, self._ymin_edit, self._ymax_edit, self._zmin_edit, self._zmax_edit):
            widget.editingFinished.connect(self.compute)
        self._display_combo.currentTextChanged.connect(self.compute)
        self._calc_combo.currentTextChanged.connect(self.compute)

    def _browse_target(self, target: QLineEdit):
        path, _ = QFileDialog.getOpenFileName(self, "Select full-sensor CSV", str(cfg.base_out), "CSV files (*.csv)")
        if path:
            target.setText(path)
            self.compute()

    def load_config(self):
        rc = cfg.bfp_rc
        self._sample_edit.setText(rc.frc_sample)
        self._bg_edit.setText(rc.frc_bg)
        self._display_combo.setCurrentText(rc.frc_display)
        self._calc_combo.setCurrentText(rc.frc_calc)
        self._auto_color_chk.setChecked(bool(rc.frc_auto_color_scale))
        self._xmin_edit.setText(rc.frc_xmin)
        self._xmax_edit.setText(rc.frc_xmax)
        self._ymin_edit.setText(rc.frc_ymin)
        self._ymax_edit.setText(rc.frc_ymax)
        self._zmin_edit.setText(rc.frc_zmin)
        self._zmax_edit.setText(rc.frc_zmax)

    def save_config(self):
        rc = cfg.bfp_rc
        rc.frc_sample = self._sample_edit.text().strip()
        rc.frc_bg = self._bg_edit.text().strip()
        rc.frc_display = self._display_combo.currentText()
        rc.frc_calc = self._calc_combo.currentText()
        rc.frc_auto_color_scale = self._auto_color_chk.isChecked()
        rc.frc_xmin = self._xmin_edit.text().strip()
        rc.frc_xmax = self._xmax_edit.text().strip()
        rc.frc_ymin = self._ymin_edit.text().strip()
        rc.frc_ymax = self._ymax_edit.text().strip()
        rc.frc_zmin = self._zmin_edit.text().strip()
        rc.frc_zmax = self._zmax_edit.text().strip()

    def set_sample_path(self, path: Path):
        self._sample_edit.setText(str(path))
        self.compute()

    def set_background_path(self, path: Path):
        self._bg_edit.setText(str(path))
        self.compute()

    def _current_image(self) -> tuple[np.ndarray, str]:
        if self._sample_data is None or self._bg_data is None:
            raise ValueError("No full-sensor result is ready.")
        display = self._display_combo.currentText()
        calc = self._calc_combo.currentText()
        if display == "sample":
            return self._sample_data.image, "sample"
        if display == "background":
            return self._bg_data.image, "background"
        if calc == "subtract":
            return compute_rc_subtract(self._sample_data.image, self._bg_data.image), "subtract"
        if calc == "division":
            return compute_rc_division(self._sample_data.image, self._bg_data.image), "division"
        return compute_rc_contrast(self._sample_data.image, self._bg_data.image), "contrast"

    @Slot()
    def compute(self):
        self.save_config()
        sample_path = self._sample_edit.text().strip()
        bg_path = self._bg_edit.text().strip()
        if not sample_path or not bg_path:
            return
        try:
            if detect_csv_mode(sample_path) != "full" or detect_csv_mode(bg_path) != "full":
                raise ValueError("Full-sensor RC tab only accepts full-sensor CSV files.")
            sample = load_full_image_csv(sample_path)
            bg = load_full_image_csv(bg_path)
            wl_min = max(sample.wl.min(), bg.wl.min())
            wl_max = min(sample.wl.max(), bg.wl.max())
            mask = (sample.wl >= wl_min) & (sample.wl <= wl_max)
            if mask.sum() < 2:
                raise ValueError("Sample and background do not overlap in wavelength.")
            wl_tgt = sample.wl[mask]
            y_tgt = sample.y
            s_img = sample.image[:, mask]
            b_img = interp_image_to_grid(bg.wl, bg.y, bg.image, wl_tgt, y_tgt)
            self._sample_data = FullImageData(wl=wl_tgt, y=y_tgt, image=s_img, orientation="row_wl_col_y")
            self._bg_data = FullImageData(wl=wl_tgt, y=y_tgt, image=b_img, orientation="row_wl_col_y")
            img_show, key = self._current_image()
            x0 = _optional_float(self._xmin_edit.text(), "x min")
            x1 = _optional_float(self._xmax_edit.text(), "x max")
            y0 = _optional_float(self._ymin_edit.text(), "y min")
            y1 = _optional_float(self._ymax_edit.text(), "y max")
            z0 = _optional_float(self._zmin_edit.text(), "z min")
            z1 = _optional_float(self._zmax_edit.text(), "z max")
            x_lo, x_hi = limits_from_pair(wl_tgt, x0, x1)
            y_lo, y_hi = limits_from_pair(y_tgt, y0, y1)
            mask_x = (wl_tgt >= x_lo) & (wl_tgt <= x_hi)
            mask_y = (y_tgt >= y_lo) & (y_tgt <= y_hi)
            if mask_x.sum() < 1 or mask_y.sum() < 1:
                raise ValueError("Selected x/y range does not overlap the data.")
            wl_crop = wl_tgt[mask_x]
            y_crop = y_tgt[mask_y]
            img_crop = img_show[np.ix_(mask_y, mask_x)]
            _set_image_axes(
                self._image,
                img_crop,
                wl_crop,
                y_crop,
                auto_levels=self._auto_color_chk.isChecked(),
                auto_range=True,
            )
            if self._auto_color_chk.isChecked():
                self._image.setLevels(*_auto_image_levels(img_crop))
            elif z0 is not None or z1 is not None:
                z_lo = float(np.nanmin(img_crop)) if z0 is None else z0
                z_hi = float(np.nanmax(img_crop)) if z1 is None else z1
                self._image.setLevels(z_lo, z_hi)
            self._display_img = img_crop
            self._display_wl = wl_crop
            self._display_y = y_crop
            self._display_key = key
            self._save_csv_btn.setEnabled(True)
            self._save_png_btn.setEnabled(True)
            self._info_lbl.setText(f"Full-sensor view ready | x = {wl_crop.min():.3f} to {wl_crop.max():.3f} nm")
        except Exception as exc:
            self._info_lbl.setText(f"Full-sensor RC failed: {exc}")

    def save_csv(self):
        if self._display_img is None or self._display_wl is None or self._display_y is None:
            return
        try:
            sample_path = Path(self._sample_edit.text().strip())
            suffix = rc_suffix_frc(self._calc_combo.currentText(), self._display_key)
            out = sample_path.with_name(f"{sample_path.stem}_{suffix}.csv")
            save_csv_atomic(out, save_full_image_csv, self._display_wl, self._display_y, self._display_img)
            self._info_lbl.setText(f"Saved {out.name}")
        except Exception as exc:
            self._info_lbl.setText(f"Save failed: {exc}")

    def save_png(self):
        if self._display_img is None or self._display_wl is None or self._display_y is None:
            return
        try:
            sample_path = Path(self._sample_edit.text().strip())
            suffix = rc_suffix_frc(self._calc_combo.currentText(), self._display_key)
            out = sample_path.with_name(f"{sample_path.stem}_{suffix}.png")
            _save_png(out, self._display_img, self._display_wl, y_axis=self._display_y)
            self._info_lbl.setText(f"Saved {out.name}")
        except Exception as exc:
            self._info_lbl.setText(f"Save failed: {exc}")


class BFPPanel(QWidget):
    def __init__(self, lf6_ctrl=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._ctrl = lf6_ctrl
        self._thread: Optional[QThread] = None
        self._worker: Optional[_AcquireWorker] = None
        self._record: Optional[_AcquiredRecord] = None
        self._build()
        self._wire()
        self._sync_from_config()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, stretch=1)
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left.setMaximumWidth(360)
        splitter.addWidget(left)

        name_grp = QGroupBox("Save Name")
        name_form = QFormLayout(name_grp)
        self._dev_edit = QLineEdit()
        self._dev_edit.setPlaceholderText("Sample ID")
        self._material_combo = QComboBox()
        self._material_combo.addItems(MATERIAL_OPTIONS)
        self._material_custom_edit = QLineEdit()
        material_row = QHBoxLayout()
        material_row.addWidget(self._material_combo)
        material_row.addWidget(self._material_custom_edit)
        self._point_edit = QLineEdit()
        self._exp_type_combo = QComboBox()
        self._exp_type_combo.addItems(EXPERIMENT_OPTIONS)
        self._power_edit = QLineEdit()
        self._note_edit = QLineEdit()
        self._suffix_edit = QLineEdit()
        self._run_idx_spin = QSpinBox()
        self._run_idx_spin.setRange(1, 999)
        self._auto_inc_chk = QCheckBox("Auto +1")
        self._auto_inc_chk.setChecked(True)
        name_form.addRow("Sample ID:", self._dev_edit)
        name_form.addRow("Material:", material_row)
        name_form.addRow("Point:", self._point_edit)
        name_form.addRow("Experiment:", self._exp_type_combo)
        name_form.addRow("Power (mW):", self._power_edit)
        name_form.addRow("Note:", self._note_edit)
        name_form.addRow("Suffix:", self._suffix_edit)
        name_form.addRow("Run #:", self._run_idx_spin)
        name_form.addRow("", self._auto_inc_chk)
        self._stem_lbl = QLabel("")
        self._stem_lbl.setStyleSheet("color: gray; font-size: 10px;")
        name_form.addRow("Preview:", self._stem_lbl)
        left_lay.addWidget(name_grp)

        acq_grp = QGroupBox("Acquisition Settings")
        acq_form = QFormLayout(acq_grp)
        self._roi_combo = QComboBox()
        self._roi_combo.addItems(["Full sensor", "Bin all"])
        self._center_spin = QDoubleSpinBox()
        self._center_spin.setRange(200, 2000)
        self._center_spin.setSuffix(" nm")
        self._exp_spin = QDoubleSpinBox()
        self._exp_spin.setRange(1, 600000)
        self._exp_spin.setSuffix(" ms")
        self._epf_spin = QSpinBox()
        self._epf_spin.setRange(1, 100000)
        self._repeat_spin = QSpinBox()
        self._repeat_spin.setRange(1, 100)
        self._auto_save_csv_chk = QCheckBox("Auto-save CSV after acquire")
        self._auto_save_csv_chk.setChecked(True)
        self._auto_apply_chk = QCheckBox("Auto-apply before acquire")
        self._auto_apply_chk.setChecked(True)
        self._warmup_chk = QCheckBox("Warm-up shot before acquire")
        self._warmup_chk.setChecked(True)
        self._est_lbl = QLabel("")
        self._est_lbl.setStyleSheet("color: gray; font-size: 10px;")
        acq_form.addRow("ROI mode:", self._roi_combo)
        acq_form.addRow("Center Lambda:", self._center_spin)
        acq_form.addRow("Exposure:", self._exp_spin)
        acq_form.addRow("Frames/EPF:", self._epf_spin)
        acq_form.addRow("Repeat avg:", self._repeat_spin)
        acq_form.addRow("", self._auto_save_csv_chk)
        acq_form.addRow("", self._auto_apply_chk)
        acq_form.addRow("", self._warmup_chk)
        acq_form.addRow("Est. time:", self._est_lbl)
        left_lay.addWidget(acq_grp)

        btn_row = QHBoxLayout()
        self._acquire_btn = QPushButton("Acquire")
        self._acquire_btn.setEnabled(False)
        self._clear_btn = QPushButton("Clear")
        btn_row.addWidget(self._acquire_btn)
        btn_row.addWidget(self._clear_btn)
        left_lay.addLayout(btn_row)

        save_row = QHBoxLayout()
        self._save_csv_btn = QPushButton("Save CSV")
        self._save_png_btn = QPushButton("Save PNG")
        self._save_csv_btn.setEnabled(False)
        self._save_png_btn.setEnabled(False)
        save_row.addWidget(self._save_csv_btn)
        save_row.addWidget(self._save_png_btn)
        left_lay.addLayout(save_row)

        load_row = QHBoxLayout()
        self._use_sample_btn = QPushButton("Use as RC Sample")
        self._use_bg_btn = QPushButton("Use as RC BG")
        self._use_sample_btn.setEnabled(False)
        self._use_bg_btn.setEnabled(False)
        load_row.addWidget(self._use_sample_btn)
        load_row.addWidget(self._use_bg_btn)
        left_lay.addLayout(load_row)

        self._status_lbl = QLabel("Disconnected")
        self._status_lbl.setStyleSheet("color: gray;")
        left_lay.addWidget(self._status_lbl)

        self._bg_panel = _BgPanel()
        left_lay.addWidget(self._bg_panel)
        self._apply_bg_btn = QPushButton("Apply / Revert Background")
        self._apply_bg_btn.setEnabled(False)
        left_lay.addWidget(self._apply_bg_btn)
        left_lay.addStretch()

        self._tabs = QTabWidget()
        self._display = _DisplayWidget()
        self._brc = _BRCWidget()
        self._frc = _FRCWidget()
        self._tabs.addTab(self._display, "Acquired Data")
        self._tabs.addTab(self._brc, "Binned RC")
        self._tabs.addTab(self._frc, "Full-Sensor RC")
        splitter.addWidget(self._tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

    def _wire(self):
        self._acquire_btn.clicked.connect(self._on_acquire)
        self._clear_btn.clicked.connect(self._on_clear)
        self._save_csv_btn.clicked.connect(self._on_save_csv)
        self._save_png_btn.clicked.connect(self._on_save_png)
        self._apply_bg_btn.clicked.connect(self._on_apply_bg)
        self._use_sample_btn.clicked.connect(self._use_as_sample)
        self._use_bg_btn.clicked.connect(self._use_as_bg)
        self._display._auto_color_chk.toggled.connect(self._persist_ui_config)
        for widget in (self._dev_edit, self._material_custom_edit, self._point_edit, self._power_edit, self._note_edit, self._suffix_edit):
            widget.textChanged.connect(self._refresh_preview)
        self._material_combo.currentTextChanged.connect(self._refresh_preview)
        self._exp_type_combo.currentTextChanged.connect(self._refresh_preview)
        self._run_idx_spin.valueChanged.connect(self._refresh_preview)
        for widget in (self._exp_spin, self._epf_spin, self._repeat_spin):
            widget.valueChanged.connect(self._update_est)
        self._auto_save_csv_chk.toggled.connect(self._persist_ui_config)
        if self._ctrl is not None:
            self._ctrl.connected.connect(self._on_lf6_connected)
            self._ctrl.disconnected.connect(self._on_lf6_disconnected)

    def _sync_from_config(self):
        self._center_spin.setValue(cfg.lf6.center_nm)
        self._exp_spin.setValue(cfg.lf6.exposure_ms)
        self._epf_spin.setValue(cfg.lf6.accumulations)
        naming = cfg.bfp_naming
        self._dev_edit.setText(naming.dev)
        idx = self._material_combo.findText(naming.material)
        self._material_combo.setCurrentIndex(idx if idx >= 0 else self._material_combo.findText("Custom"))
        self._material_custom_edit.setText(naming.material_custom)
        self._point_edit.setText(naming.point)
        self._exp_type_combo.setCurrentText(naming.exp_type)
        self._power_edit.setText(naming.power)
        self._note_edit.setText(naming.note)
        self._suffix_edit.setText(naming.suffix)
        self._repeat_spin.setValue(max(1, int(naming.repeat)))
        self._display._auto_color_chk.setChecked(bool(naming.auto_color_scale))
        self._auto_save_csv_chk.setChecked(bool(naming.auto_save_csv))
        self._refresh_preview()
        self._update_est()

    def _current_material_text(self) -> str:
        if self._material_combo.currentText() == "Custom":
            return self._material_custom_edit.text().strip()
        return self._material_combo.currentText().strip()

    def _build_prefix_without_time_and_run(self) -> str:
        parts = []
        dev = _sanitize(self._dev_edit.text())
        if dev:
            parts.append(dev)
        material = _sanitize(self._current_material_text())
        if material:
            parts.append(material)
        point = _sanitize(self._point_edit.text())
        if point:
            parts.append(f"P{point}")
        exp_type = _sanitize(self._exp_type_combo.currentText())
        if exp_type:
            parts.append(exp_type)
        power = _sanitize(self._power_edit.text())
        if power:
            parts.append(f"{power}mW")
        note = _sanitize(self._note_edit.text())
        if note:
            parts.append(note)
        parts.append(f"{self._center_spin.value():g}nm")
        parts.append(f"{self._exp_spin.value():g}ms")
        parts.append(f"{self._repeat_spin.value()}x")
        suffix = _sanitize(self._suffix_edit.text())
        if suffix:
            parts.append(suffix)
        return "_".join(part for part in parts if part) or "measurement"

    def _out_dir(self) -> Path:
        sample_hint = _sanitize(self._dev_edit.text()) or "SampleID"
        return Path(cfg.base_out) / sample_hint / "bfp"

    def _refresh_preview(self):
        self._persist_ui_config()
        save_dir = self._out_dir()
        run_idx = _next_run_index(save_dir, self._build_prefix_without_time_and_run())
        self._run_idx_spin.blockSignals(True)
        self._run_idx_spin.setValue(run_idx)
        self._run_idx_spin.blockSignals(False)
        self._stem_lbl.setText(f"{self._build_prefix_without_time_and_run()}_{_now_time()}_{run_idx:03d}.csv")

    def _update_est(self):
        est = (self._exp_spin.value() / 1000.0) * self._epf_spin.value() * self._repeat_spin.value()
        mult = 2 if self._warmup_chk.isChecked() else 1
        self._est_lbl.setText(f"~ {est * mult:.1f}s total")
        self._persist_ui_config()

    def _persist_ui_config(self):
        naming = cfg.bfp_naming
        naming.dev = self._dev_edit.text().strip()
        naming.material = self._material_combo.currentText()
        naming.material_custom = self._material_custom_edit.text().strip()
        naming.point = self._point_edit.text().strip()
        naming.exp_type = self._exp_type_combo.currentText()
        naming.power = self._power_edit.text().strip()
        naming.note = self._note_edit.text().strip()
        naming.suffix = self._suffix_edit.text().strip()
        naming.repeat = int(self._repeat_spin.value())
        naming.auto_color_scale = self._display._auto_color_chk.isChecked()
        naming.auto_save_csv = self._auto_save_csv_chk.isChecked()
        cfg.lf6.center_nm = float(self._center_spin.value())
        cfg.lf6.exposure_ms = float(self._exp_spin.value())
        cfg.lf6.accumulations = int(self._epf_spin.value())
        self._brc.save_config()
        self._frc.save_config()
        cfg.save()

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

    @Slot()
    def _on_acquire(self):
        self._persist_ui_config()
        self._acquire_btn.setEnabled(False)
        self._status_lbl.setText("Acquiring...")
        self._status_lbl.setStyleSheet("color: orange;")
        self._worker = _AcquireWorker(
            lf6_ctrl=self._ctrl,
            roi_mode=self._roi_combo.currentText(),
            center_nm=self._center_spin.value(),
            exposure_ms=self._exp_spin.value(),
            epf=self._epf_spin.value(),
            repeat=self._repeat_spin.value(),
            auto_apply=self._auto_apply_chk.isChecked(),
            warmup=self._warmup_chk.isChecked(),
        )
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.result.connect(self._on_result)
        self._worker.status.connect(self._status_lbl.setText)
        self._worker.error.connect(lambda msg: (self._status_lbl.setText(f"Error: {msg[:80]}"), self._status_lbl.setStyleSheet("color: red;")))
        self._worker.finished.connect(self._on_acquire_done)
        self._thread.start()

    @Slot(object)
    def _on_result(self, record: _AcquiredRecord):
        self._record = record
        self._display.update_data(record.data, record.wls, y_axis=record.y_axis)
        for btn in (self._save_csv_btn, self._save_png_btn, self._apply_bg_btn, self._use_sample_btn, self._use_bg_btn):
            btn.setEnabled(True)
        if self._auto_save_csv_chk.isChecked():
            try:
                csv_path = self._save_record_csv(record)
                self._status_lbl.setText(f"Acquired and auto-saved -> {csv_path.name}")
                self._status_lbl.setStyleSheet("color: green;")
            except Exception as exc:
                self._status_lbl.setText(f"Auto-save error: {exc}")
                self._status_lbl.setStyleSheet("color: red;")
        else:
            self._status_lbl.setText("Acquisition complete")
            self._status_lbl.setStyleSheet("color: green;")
        self._tabs.setCurrentIndex(0)

    @Slot()
    def _on_acquire_done(self):
        self._acquire_btn.setEnabled(self._ctrl is not None and self._ctrl.is_connected)
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
            self._thread = None
            self._worker = None

    @Slot()
    def _on_clear(self):
        self._record = None
        self._display.clear()
        for btn in (self._save_csv_btn, self._save_png_btn, self._apply_bg_btn, self._use_sample_btn, self._use_bg_btn):
            btn.setEnabled(False)
        self._status_lbl.setText("Cleared")
        self._status_lbl.setStyleSheet("color: gray;")

    @Slot()
    def _on_apply_bg(self):
        if self._record is None:
            return
        ok, out, msg = self._bg_panel.apply_to(self._record.data, self._record.wls)
        if ok:
            self._display.update_data(out, self._record.wls, y_axis=self._record.y_axis)
            self._status_lbl.setStyleSheet("color: green;")
        else:
            self._display.update_data(self._record.data, self._record.wls, y_axis=self._record.y_axis)
            self._status_lbl.setStyleSheet("color: orange;")
        self._status_lbl.setText(msg)

    def _next_base_path(self) -> Path:
        out_dir = self._out_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        run_idx = _next_run_index(out_dir, self._build_prefix_without_time_and_run())
        self._run_idx_spin.setValue(run_idx)
        return out_dir / f"{self._build_prefix_without_time_and_run()}_{_now_time()}_{run_idx:03d}"

    def _save_record_csv(self, record: _AcquiredRecord) -> Path:
        base = self._next_base_path()
        csv_path = base.with_suffix(".csv")
        if record.mode == "binned":
            save_csv_atomic(csv_path, save_binned_csv, record.wls, record.data)
        else:
            save_csv_atomic(csv_path, save_full_image_csv, record.wls, record.y_axis, record.data)
        record.csv_path = csv_path
        self._refresh_preview()
        return csv_path

    @Slot()
    def _on_save_csv(self):
        if self._record is None:
            return
        try:
            csv_path = self._save_record_csv(self._record)
            self._status_lbl.setText(f"Saved -> {csv_path.name}")
            self._status_lbl.setStyleSheet("color: green;")
        except Exception as exc:
            self._status_lbl.setText(f"Save error: {exc}")
            self._status_lbl.setStyleSheet("color: red;")

    @Slot()
    def _on_save_png(self):
        if self._record is None:
            return
        try:
            base = self._record.csv_path.with_suffix("") if self._record.csv_path else self._next_base_path()
            png_path = base.with_suffix(".png")
            _save_png(png_path, self._record.data, self._record.wls, y_axis=self._record.y_axis, scale=self._display._scale_combo.currentText(), cmap=self._display._cmap_combo.currentText())
            self._status_lbl.setText(f"Saved -> {png_path.name}")
            self._status_lbl.setStyleSheet("color: green;")
        except Exception as exc:
            self._status_lbl.setText(f"PNG error: {exc}")
            self._status_lbl.setStyleSheet("color: red;")

    def _use_as_sample(self):
        if self._record is None:
            return
        if self._record.csv_path is None:
            self._on_save_csv()
        if self._record and self._record.csv_path:
            if self._record.mode == "binned":
                self._brc.set_sample_path(self._record.csv_path)
                self._tabs.setCurrentWidget(self._brc)
            else:
                self._frc.set_sample_path(self._record.csv_path)
                self._tabs.setCurrentWidget(self._frc)

    def _use_as_bg(self):
        if self._record is None:
            return
        if self._record.csv_path is None:
            self._on_save_csv()
        if self._record and self._record.csv_path:
            if self._record.mode == "binned":
                self._brc.set_background_path(self._record.csv_path)
                self._tabs.setCurrentWidget(self._brc)
            else:
                self._frc.set_background_path(self._record.csv_path)
                self._tabs.setCurrentWidget(self._frc)
