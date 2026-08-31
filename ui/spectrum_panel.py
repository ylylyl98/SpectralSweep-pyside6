# ui/spectrum_panel.py
# ──────────────────────────────────────────────────────────────────────────────
# Live spectrum plot panel using pyqtgraph.
#
# Two display modes (switchable via tab):
#   1D  — line plot of wavelength vs intensity (one spectrum)
#   2D  — false-colour image of a CCD frame (acquire_2d output)
#
# Receives data via:
#   lf6_ctrl.spectrum_ready(wl, cts)  → updates 1D plot
#   lf6_ctrl.frame_ready(img)         → updates 2D plot
#
# Controls:
#   Acquire button   → calls lf6_ctrl.acquire_single()
#   Acquire 2D       → calls lf6_ctrl.acquire_2d()
#   Auto-scale       → toggle Y autoscale on 1D plot
#   Colormap picker  → 1D: unused; 2D: jet / viridis / hot
#
# Rules:
#   - No instrument state here.
#   - importlib.reload() safe.
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTabWidget, QComboBox, QCheckBox, QSizePolicy, QDoubleSpinBox,
    QSpinBox,
    QToolButton,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pyqtgraph as pg
from utils.config import cfg
from ui.andor_controls_widget import AndorControlsWidget

# Use a dark-on-white look that reads well in lab conditions
pg.setConfigOption("background", "w")
pg.setConfigOption("foreground", "k")


# ── 1D plot widget ────────────────────────────────────────────────────────────

class _SpectrumPlot(QWidget):
    """Single spectrum line plot."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self._plot = pg.PlotWidget()
        self._plot.setLabel("bottom", "Wavelength", units="nm")
        self._plot.setLabel("left",   "Intensity",  units="counts")
        self._plot.showGrid(x=True, y=True, alpha=0.3)

        self._curve = self._plot.plot(
            pen=pg.mkPen(color="#1565C0", width=1.5)
        )
        lay.addWidget(self._plot)

        # info bar
        info = QHBoxLayout()
        self._peak_lbl  = QLabel("Peak: —")
        self._range_lbl = QLabel("Range: —")
        self._autoscale_chk = QCheckBox("Auto Y")
        self._autoscale_chk.setChecked(True)
        info.addWidget(self._peak_lbl)
        info.addStretch()
        info.addWidget(self._range_lbl)
        info.addWidget(self._autoscale_chk)
        lay.addLayout(info)

        self._wl:  np.ndarray = np.array([])
        self._cts: np.ndarray = np.array([])

    def update_spectrum(self, wl: np.ndarray, cts: np.ndarray) -> None:
        self._wl  = np.asarray(wl,  dtype=float)
        self._cts = np.asarray(cts, dtype=float)
        self._curve.setData(self._wl, self._cts)

        if self._autoscale_chk.isChecked():
            self._plot.enableAutoRange()

        if self._cts.size:
            peak_idx = int(np.argmax(self._cts))
            self._peak_lbl.setText(
                f"Peak: {self._cts[peak_idx]:.0f} cts @ {self._wl[peak_idx]:.2f} nm"
            )
        if self._wl.size >= 2:
            self._range_lbl.setText(
                f"Range: {self._wl[0]:.1f} – {self._wl[-1]:.1f} nm"
            )


# ── 2D image widget ───────────────────────────────────────────────────────────

def _try_cmap(name: str) -> Optional[pg.ColorMap]:
    try:
        return pg.colormap.get(name)
    except Exception:
        return None

_CMAP_CANDIDATES = ["viridis", "plasma", "inferno", "magma", "CET-L1", "grays"]
_COLORMAPS: dict[str, pg.ColorMap] = {}
for _name in _CMAP_CANDIDATES:
    _c = _try_cmap(_name)
    if _c is not None:
        _COLORMAPS[_name] = _c
if not _COLORMAPS:
    # last resort: build a simple greyscale manually
    _COLORMAPS["gray"] = pg.ColorMap([0.0, 1.0], [(0, 0, 0, 255), (255, 255, 255, 255)])


class _FramePlot(QWidget):
    """2D CCD frame false-colour image."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self._view = pg.ImageView()
        self._view.ui.roiBtn.hide()
        self._view.ui.menuBtn.hide()
        lay.addWidget(self._view)

        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Colormap:"))
        self._cmap_combo = QComboBox()
        self._cmap_combo.addItems(list(_COLORMAPS.keys()))
        self._cmap_combo.currentTextChanged.connect(self._apply_cmap)
        ctrl.addWidget(self._cmap_combo)
        ctrl.addStretch()
        self._shape_lbl = QLabel("Shape: —")
        ctrl.addWidget(self._shape_lbl)
        lay.addLayout(ctrl)

        self._apply_cmap("viridis")

    def _apply_cmap(self, name: str) -> None:
        cmap = _COLORMAPS.get(name)
        if cmap is not None:
            self._view.setColorMap(cmap)

    def update_frame(self, img: np.ndarray) -> None:
        arr = np.asarray(img, dtype=float)
        # ImageView expects (x, y) or (x, y, channels)
        # CCD frames come in as (rows, cols) → transpose to (cols, rows)
        if arr.ndim == 2:
            arr = arr.T
        self._view.setImage(arr, autoLevels=True, autoRange=True)
        self._shape_lbl.setText(f"Shape: {img.shape[0]}×{img.shape[1]}")


# ── Combined panel ────────────────────────────────────────────────────────────

class SpectrumPanel(QWidget):
    """
    Live spectrum panel.  Inject lf6_ctrl to wire signals.

    Usage:
        panel = SpectrumPanel(lf6_ctrl=lf6)
    """

    def __init__(self, lf6_ctrl=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._ctrl = lf6_ctrl
        self._connected = False
        self._supports_2d = True
        self._pending_acquisition: Optional[str] = None
        self._continuous_mode: Optional[str] = None
        self._continuous_frames = 0
        self._continuous_started_at = 0.0
        self._build()
        self._wire()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # Acquisition settings.  These intentionally mirror the shared
        # Settings tab so a single spectrum can be configured where it is run.
        settings_row = QHBoxLayout()
        settings_row.addWidget(QLabel("Center wavelength:"))
        self._center = QDoubleSpinBox()
        self._center.setRange(200.0, 2000.0)
        self._center.setDecimals(1)
        self._center.setSingleStep(1.0)
        self._center.setSuffix(" nm")
        self._center.setValue(float(cfg.lf6.center_nm))
        settings_row.addWidget(self._center)

        settings_row.addWidget(QLabel("Exposure:"))
        self._exposure = QDoubleSpinBox()
        self._exposure.setRange(1.0, 600_000.0)
        self._exposure.setDecimals(1)
        self._exposure.setSingleStep(100.0)
        self._exposure.setSuffix(" ms")
        self._exposure.setValue(float(cfg.lf6.exposure_ms))
        settings_row.addWidget(self._exposure)

        settings_row.addWidget(QLabel("Accumulations:"))
        self._accumulations = QSpinBox()
        self._accumulations.setRange(1, 1000)
        self._accumulations.setSuffix(" frame(s)")
        self._accumulations.setValue(int(cfg.lf6.accumulations))
        settings_row.addWidget(self._accumulations)

        self._apply_btn = QPushButton("Apply")
        self._apply_btn.setToolTip(
            "Apply center wavelength, exposure, and accumulations without acquiring."
        )
        settings_row.addWidget(self._apply_btn)
        settings_row.addStretch()
        root.addLayout(settings_row)

        # Button row
        btn_row = QHBoxLayout()
        self._acquire_btn    = QPushButton("Acquire 1D")
        self._acquire_2d_btn = QPushButton("Acquire 2D")
        self._abort_btn = QPushButton("Abort")
        self._abort_btn.setEnabled(False)
        self._run_1d_btn = QPushButton("Run 1D")
        self._run_2d_btn = QPushButton("Run 2D")
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setEnabled(False)
        self._status_lbl     = QLabel("Ready")
        self._status_lbl.setStyleSheet("color: gray;")
        btn_row.addWidget(self._acquire_btn)
        btn_row.addWidget(self._acquire_2d_btn)
        btn_row.addWidget(self._abort_btn)
        btn_row.addSpacing(10)
        btn_row.addWidget(self._run_1d_btn)
        btn_row.addWidget(self._run_2d_btn)
        btn_row.addWidget(self._stop_btn)
        btn_row.addStretch()
        self._rate_lbl = QLabel("0 frames · 0.0 fps")
        btn_row.addWidget(self._rate_lbl)
        btn_row.addWidget(self._status_lbl)
        root.addLayout(btn_row)

        # Tab widget: 1D | 2D
        self._tabs = QTabWidget()
        self._spec_plot  = _SpectrumPlot()
        self._frame_plot = _FramePlot()
        self._tabs.addTab(self._spec_plot,  "1D Spectrum")
        self._tabs.addTab(self._frame_plot, "2D Frame")
        root.addWidget(self._tabs)

        self._andor_toggle = QToolButton()
        self._andor_toggle.setText("Andor controls")
        self._andor_toggle.setCheckable(True)
        self._andor_toggle.setChecked(False)
        self._andor_toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self._andor_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self._andor_toggle.setVisible(False)
        root.addWidget(self._andor_toggle)
        self._andor_controls = AndorControlsWidget(self._ctrl)
        self._andor_controls.setVisible(False)
        root.addWidget(self._andor_controls)

        # Disabled until controller connects
        self._acquire_btn.setEnabled(False)
        self._acquire_2d_btn.setEnabled(False)
        self._apply_btn.setEnabled(False)
        self._run_1d_btn.setEnabled(False)
        self._run_2d_btn.setEnabled(False)

    def _wire(self):
        self._acquire_btn.clicked.connect(self._on_acquire)
        self._acquire_2d_btn.clicked.connect(self._on_acquire_2d)
        self._apply_btn.clicked.connect(self._on_apply)
        self._abort_btn.clicked.connect(self._on_abort)
        self._run_1d_btn.clicked.connect(lambda: self._start_continuous("1d"))
        self._run_2d_btn.clicked.connect(lambda: self._start_continuous("2d"))
        self._stop_btn.clicked.connect(self._stop_continuous)
        self._andor_toggle.toggled.connect(self._toggle_andor_controls)
        self._andor_controls.status_changed.connect(self._on_andor_status_text)
        self._center.valueChanged.connect(
            lambda value: setattr(cfg.lf6, "center_nm", float(value))
        )
        self._center.valueChanged.connect(self._andor_controls.center.setValue)
        self._andor_controls.center.valueChanged.connect(self._center.setValue)
        self._exposure.valueChanged.connect(
            lambda value: setattr(cfg.lf6, "exposure_ms", float(value))
        )
        self._accumulations.valueChanged.connect(
            lambda value: setattr(cfg.lf6, "accumulations", int(value))
        )

        if self._ctrl is not None:
            self._ctrl.connected.connect(self._on_lf6_connected)
            self._ctrl.disconnected.connect(self._on_lf6_disconnected)
            self._ctrl.spectrum_ready.connect(self._on_spectrum_ready)
            self._ctrl.frame_ready.connect(self._on_frame_ready)
            self._ctrl.settings_applied.connect(self._on_settings_applied)
            self._ctrl.error.connect(self._on_error)

    # ── slots ─────────────────────────────────────────────────────────────────

    def capture_session_state(self) -> dict:
        """Return display preferences only; acquired data is intentionally omitted."""
        return {
            "view_tab": int(self._tabs.currentIndex()),
            "auto_y": bool(self._spec_plot._autoscale_chk.isChecked()),
            "colormap": self._frame_plot._cmap_combo.currentText(),
            "center_nm": float(self._center.value()),
            "exposure_ms": float(self._exposure.value()),
            "accumulations": int(self._accumulations.value()),
        }

    def restore_session_state(self, state: dict) -> None:
        if not isinstance(state, dict):
            return
        self._spec_plot._autoscale_chk.setChecked(bool(state.get("auto_y", True)))
        try:
            self._center.setValue(float(state.get("center_nm", self._center.value())))
            self._exposure.setValue(
                float(state.get("exposure_ms", self._exposure.value()))
            )
            self._accumulations.setValue(
                int(state.get("accumulations", self._accumulations.value()))
            )
        except (TypeError, ValueError):
            pass
        cmap = state.get("colormap")
        if isinstance(cmap, str) and self._frame_plot._cmap_combo.findText(cmap) >= 0:
            self._frame_plot._cmap_combo.setCurrentText(cmap)
        try:
            tab = int(state.get("view_tab", 0))
        except (TypeError, ValueError):
            tab = 0
        self._tabs.setCurrentIndex(min(max(tab, 0), self._tabs.count() - 1))

    @Slot()
    def _on_acquire(self):
        self._apply_settings_then("1d")

    @Slot()
    def _on_acquire_2d(self):
        self._apply_settings_then("2d")

    @Slot()
    def _on_apply(self):
        self._apply_settings_then(None)

    @Slot(bool)
    def _toggle_andor_controls(self, expanded: bool) -> None:
        self._andor_toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self._andor_controls.setVisible(expanded and self._andor_toggle.isVisible())

    @Slot(str)
    def _on_andor_status_text(self, message: str) -> None:
        if self._continuous_mode is None and self._pending_acquisition is None:
            self._status_lbl.setText(message)

    def _apply_settings_then(self, acquisition: Optional[str]) -> None:
        if self._ctrl is None or not self._connected:
            return
        self._pending_acquisition = acquisition
        self._set_action_controls_enabled(False)
        self._status_lbl.setText("Applying settings…")
        self._ctrl.apply_settings(
            exposure_ms=float(self._exposure.value()),
            center_nm=float(self._center.value()),
            accumulations=int(self._accumulations.value()),
        )

    @Slot()
    def _on_settings_applied(self) -> None:
        pending = self._pending_acquisition
        self._pending_acquisition = None
        if pending in {"1d", "run_1d"}:
            if pending == "run_1d":
                self._continuous_mode = "1d"
            self._status_lbl.setText("Acquiring…")
            self._abort_btn.setEnabled(True)
            self._ctrl.acquire_single()
        elif pending in {"2d", "run_2d"}:
            if pending == "run_2d":
                self._continuous_mode = "2d"
            self._status_lbl.setText("Acquiring 2D…")
            self._abort_btn.setEnabled(True)
            self._ctrl.acquire_2d()
        else:
            self._status_lbl.setText("Settings applied")
            self._set_action_controls_enabled(self._connected)

    @Slot(str)
    def _on_error(self, message: str) -> None:
        self._pending_acquisition = None
        self._continuous_mode = None
        full = str(message)
        self._status_lbl.setText(f"Error: {full.splitlines()[0][:80]}")
        self._status_lbl.setToolTip(full)
        self._abort_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._andor_controls.set_controls_locked(False)
        self._set_action_controls_enabled(self._connected)

    @Slot()
    def _on_abort(self) -> None:
        method = getattr(self._ctrl, "abort_acquisition", None)
        if callable(method) and method():
            self._status_lbl.setText("Cancelling acquisition…")
        self._abort_btn.setEnabled(False)

    def _start_continuous(self, mode: str) -> None:
        if not self._connected or self._continuous_mode is not None:
            return
        if mode == "2d" and not self._supports_2d:
            return
        self._continuous_mode = mode
        self._continuous_frames = 0
        self._continuous_started_at = time.perf_counter()
        self._rate_lbl.setText("0 frames · 0.0 fps")
        self._stop_btn.setEnabled(True)
        self._andor_controls.set_controls_locked(True)
        self._apply_settings_then(f"run_{mode}")

    @Slot()
    def _stop_continuous(self) -> None:
        was_running = self._continuous_mode is not None
        self._continuous_mode = None
        self._pending_acquisition = None
        method = getattr(self._ctrl, "abort_acquisition", None)
        if was_running and callable(method):
            method()
        self._abort_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._andor_controls.set_controls_locked(False)
        self._set_action_controls_enabled(self._connected)
        self._status_lbl.setText("Stopped" if was_running else "Ready")

    def _queue_next_continuous(self, completed_mode: str) -> None:
        if self._continuous_mode != completed_mode or not self._connected:
            self._finish_one_shot()
            return
        self._continuous_frames += 1
        elapsed = max(1e-9, time.perf_counter() - self._continuous_started_at)
        self._rate_lbl.setText(
            f"{self._continuous_frames} frames · "
            f"{self._continuous_frames / elapsed:.1f} fps"
        )
        self._status_lbl.setText(
            f"Running {'2D' if completed_mode == '2d' else '1D'}…"
        )
        QTimer.singleShot(0, self._acquire_next_continuous)

    @Slot()
    def _acquire_next_continuous(self) -> None:
        if self._continuous_mode == "1d":
            self._ctrl.acquire_single()
        elif self._continuous_mode == "2d":
            self._ctrl.acquire_2d()

    def _finish_one_shot(self) -> None:
        self._abort_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._andor_controls.set_controls_locked(False)
        self._set_action_controls_enabled(self._connected)

    def _set_action_controls_enabled(self, enabled: bool) -> None:
        self._apply_btn.setEnabled(enabled)
        self._acquire_btn.setEnabled(enabled)
        self._acquire_2d_btn.setEnabled(enabled and self._supports_2d)
        self._run_1d_btn.setEnabled(enabled)
        self._run_2d_btn.setEnabled(enabled and self._supports_2d)
        self._center.setEnabled(enabled)
        self._exposure.setEnabled(enabled)
        self._accumulations.setEnabled(enabled)

    @Slot(list)
    def _on_lf6_connected(self, _experiments):
        self._connected = True
        identity = getattr(self._ctrl, "identity", {}) or {}
        self._supports_2d = not (
            str(identity.get("backend", "")) == "andor_sdk2"
            and str(identity.get("camera_role", "")) == "ingaas"
        )
        self._acquire_2d_btn.setToolTip(
            "The connected InGaAs detector is a one-dimensional 512-pixel array."
            if not self._supports_2d
            else "Acquire a two-dimensional detector frame."
        )
        self._set_action_controls_enabled(True)
        is_andor = str(identity.get("backend", "")) == "andor_sdk2"
        self._andor_controls.set_backend_identity(identity)
        self._andor_toggle.setVisible(is_andor)
        self._andor_controls.setVisible(is_andor and self._andor_toggle.isChecked())
        self._status_lbl.setText("Connected")
        self._status_lbl.setStyleSheet("color: green;")

    @Slot()
    def _on_lf6_disconnected(self):
        self._connected = False
        self._pending_acquisition = None
        self._continuous_mode = None
        self._supports_2d = True
        self._abort_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._andor_toggle.setVisible(False)
        self._andor_controls.set_backend_identity({})
        self._set_action_controls_enabled(False)
        self._status_lbl.setText("Disconnected")
        self._status_lbl.setStyleSheet("color: gray;")

    @Slot(object, object)
    def _on_spectrum_ready(self, wl: np.ndarray, cts: np.ndarray):
        self._spec_plot.update_spectrum(wl, cts)
        self._tabs.setCurrentIndex(0)
        self._status_lbl.setText("Ready")
        self._queue_next_continuous("1d")

    @Slot(object)
    def _on_frame_ready(self, img: np.ndarray):
        self._frame_plot.update_frame(img)
        self._tabs.setCurrentIndex(1)
        self._status_lbl.setText("Ready")
        self._queue_next_continuous("2d")

    # ── direct update (called by sweep loop without going through controller) ─

    def push_spectrum(self, wl: np.ndarray, cts: np.ndarray) -> None:
        """Update 1D plot directly (e.g. from a sweep step callback)."""
        self._spec_plot.update_spectrum(wl, cts)

    def push_frame(self, img: np.ndarray) -> None:
        """Update 2D plot directly."""
        self._frame_plot.update_frame(img)
