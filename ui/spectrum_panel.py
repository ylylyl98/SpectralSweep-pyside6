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
import csv
import json
import re
import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTabWidget, QComboBox, QCheckBox, QSizePolicy, QDoubleSpinBox,
    QSpinBox,
    QToolButton,
    QFileDialog,
    QListWidget,
    QListWidgetItem,
    QLineEdit,
    QColorDialog,
    QSplitter,
)
from PySide6.QtGui import QColor

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pyqtgraph as pg
from utils.config import cfg
from ui.andor_controls_widget import AndorControlsWidget
from app.experiment_metadata import instrument_inventory
from app.power_reading import power_correction_factor

# Use a dark-on-white look that reads well in lab conditions
pg.setConfigOption("background", "w")
pg.setConfigOption("foreground", "k")

LIVE_SPECTRUM_COLOR = "#1565C0"
LIVE_SPECTRUM_WIDTH = 2.4
REFERENCE_SPECTRUM_WIDTH = 1.8
REFERENCE_COLORS = (
    "#D13438", "#107C10", "#8764B8", "#CA5010", "#038387",
    "#5C2D91", "#8E562E", "#C239B3",
)


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
            pen=pg.mkPen(
                color=LIVE_SPECTRUM_COLOR,
                width=LIVE_SPECTRUM_WIDTH,
                style=Qt.PenStyle.SolidLine,
            )
        )
        self._curve.setZValue(10)
        self._reference_curves: dict[str, object] = {}
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

    def set_reference(self, reference_id: str, wl: np.ndarray, cts: np.ndarray,
                      color: str, visible: bool = True) -> None:
        curve = self._reference_curves.get(str(reference_id))
        if curve is None:
            curve = self._plot.plot(
                pen=pg.mkPen(
                    color=color,
                    width=REFERENCE_SPECTRUM_WIDTH,
                    style=Qt.PenStyle.SolidLine,
                )
            )
            curve.setZValue(0)
            self._reference_curves[str(reference_id)] = curve
        curve.setPen(pg.mkPen(
            color=color,
            width=REFERENCE_SPECTRUM_WIDTH,
            style=Qt.PenStyle.SolidLine,
        ))
        curve.setData(np.asarray(wl, dtype=float), np.asarray(cts, dtype=float))
        curve.setVisible(bool(visible))

    def set_reference_visible(self, reference_id: str, visible: bool) -> None:
        curve = self._reference_curves.get(str(reference_id))
        if curve is not None:
            curve.setVisible(bool(visible))

    def remove_reference(self, reference_id: str) -> None:
        curve = self._reference_curves.pop(str(reference_id), None)
        if curve is not None:
            self._plot.removeItem(curve)

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
        self._last_data_kind: Optional[str] = None
        self._last_data: Optional[np.ndarray] = None
        self._last_wavelength: Optional[np.ndarray] = None
        self._acquisition_settings_snapshot: Optional[dict] = None
        self._pending_acquisition_snapshot: Optional[dict] = None
        self._last_acquisition_snapshot: Optional[dict] = None
        self._references: list[dict] = []
        self._metadata_context: dict = {}
        self._sidebar_snapshot_provider = None
        self._observed_readbacks: dict[str, dict] = {}
        self._add_pending = False
        self._reference_colors = list(REFERENCE_COLORS)
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
        self._save_btn = QPushButton("Save current…")
        self._save_btn.setToolTip("Deliberately record the currently displayed acquisition with its saved setup.")
        self._add_spectrum_btn = QPushButton("Add spectrum")
        self._add_spectrum_btn.setToolTip(
            "Acquire a spectrum and add it as a reference. During Run, freeze the latest completed frame."
        )
        self._load_spectra_btn = QPushButton("Load spectra…")
        self._status_lbl     = QLabel("Ready")
        self._status_lbl.setStyleSheet("color: gray;")
        btn_row.addWidget(self._acquire_btn)
        btn_row.addWidget(self._acquire_2d_btn)
        btn_row.addWidget(self._abort_btn)
        btn_row.addSpacing(10)
        btn_row.addWidget(self._run_1d_btn)
        btn_row.addWidget(self._run_2d_btn)
        btn_row.addWidget(self._stop_btn)
        btn_row.addWidget(self._save_btn)
        btn_row.addWidget(self._add_spectrum_btn)
        btn_row.addWidget(self._load_spectra_btn)
        btn_row.addStretch()
        self._rate_lbl = QLabel("0 frames · 0.0 fps")
        btn_row.addWidget(self._rate_lbl)
        btn_row.addWidget(self._status_lbl)
        root.addLayout(btn_row)

        save_row = QHBoxLayout()
        save_row.addWidget(QLabel("Spectrum save folder:"))
        self._save_root_lbl = QLabel()
        self._save_root_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        save_row.addWidget(self._save_root_lbl, 1)
        self._change_save_root_btn = QPushButton("Change…")
        self._open_save_root_btn = QPushButton("Open")
        save_row.addWidget(self._change_save_root_btn)
        save_row.addWidget(self._open_save_root_btn)
        root.addLayout(save_row)

        reference_panel = QWidget()
        reference_layout = QVBoxLayout(reference_panel)
        reference_layout.setContentsMargins(4, 4, 4, 4)
        reference_header = QHBoxLayout()
        reference_header.addWidget(QLabel("Spectra"))
        self._normalize_references_chk = QCheckBox("Normalize")
        self._normalize_references = False
        self._normalize_references_chk.toggled.connect(self._on_normalize_references)
        reference_header.addWidget(self._normalize_references_chk)
        self._show_all_btn = QPushButton("Show all")
        self._hide_all_btn = QPushButton("Hide all")
        reference_header.addWidget(self._show_all_btn)
        reference_header.addWidget(self._hide_all_btn)
        reference_layout.addLayout(reference_header)
        self._reference_list = QListWidget()
        self._reference_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._reference_list.setMinimumHeight(48)
        reference_layout.addWidget(self._reference_list, 1)
        reference_actions = QVBoxLayout()
        self._rename_reference_btn = QPushButton("Rename")
        self._color_reference_btn = QPushButton("Color")
        self._details_reference_btn = QPushButton("Details")
        self._remove_reference_btn = QPushButton("Remove")
        self._retry_reference_btn = QPushButton("Retry save")
        for button in (self._rename_reference_btn, self._color_reference_btn,
                       self._details_reference_btn,
                       self._remove_reference_btn, self._retry_reference_btn):
            reference_actions.addWidget(button)
        reference_actions.addStretch()
        reference_layout.addLayout(reference_actions)

        # Tab widget: 1D | 2D
        self._tabs = QTabWidget()
        self._spec_plot  = _SpectrumPlot()
        self._frame_plot = _FramePlot()
        self._tabs.addTab(self._spec_plot,  "1D Spectrum")
        self._tabs.addTab(self._frame_plot, "2D Frame")
        self._content_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._content_splitter.addWidget(self._tabs)
        self._content_splitter.addWidget(reference_panel)
        self._content_splitter.setStretchFactor(0, 4)
        self._content_splitter.setStretchFactor(1, 1)
        self._content_splitter.setSizes([900, 300])
        root.addWidget(self._content_splitter, 1)

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
        self._save_btn.setEnabled(False)
        self._add_spectrum_btn.setEnabled(False)
        self._load_spectra_btn.setEnabled(True)
        self._retry_reference_btn.setEnabled(False)
        self._save_root_lbl.setText(str(self._spectrum_output_root()))

    def _wire(self):
        self._acquire_btn.clicked.connect(self._on_acquire)
        self._acquire_2d_btn.clicked.connect(self._on_acquire_2d)
        self._apply_btn.clicked.connect(self._on_apply)
        self._abort_btn.clicked.connect(self._on_abort)
        self._run_1d_btn.clicked.connect(lambda: self._start_continuous("1d"))
        self._run_2d_btn.clicked.connect(lambda: self._start_continuous("2d"))
        self._stop_btn.clicked.connect(self._stop_continuous)
        self._save_btn.clicked.connect(self._save_current_dialog)
        self._add_spectrum_btn.clicked.connect(self._on_add_spectrum)
        self._load_spectra_btn.clicked.connect(self._load_spectra_dialog)
        self._change_save_root_btn.clicked.connect(self._change_save_root)
        self._open_save_root_btn.clicked.connect(self._open_save_root)
        self._reference_list.itemChanged.connect(self._on_reference_item_changed)
        self._show_all_btn.clicked.connect(lambda: self._set_all_references_visible(True))
        self._hide_all_btn.clicked.connect(lambda: self._set_all_references_visible(False))
        self._rename_reference_btn.clicked.connect(self._rename_selected_reference)
        self._color_reference_btn.clicked.connect(self._color_selected_reference)
        self._details_reference_btn.clicked.connect(self._show_selected_reference_details)
        self._remove_reference_btn.clicked.connect(self._remove_selected_reference)
        self._retry_reference_btn.clicked.connect(self._retry_selected_reference)
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
            for signal_name, source in (("temperature_ready", "detector_temperature"),
                                        ("temperature_snapshot_ready", "detector_temperature_snapshot"),
                                        ("andor_status_ready", "andor_status"),
                                        ("state_changed", "lightfield_state")):
                signal = getattr(self._ctrl, signal_name, None)
                if signal is not None and hasattr(signal, "connect"):
                    signal.connect(lambda value, source=source: self._cache_readback(source, value))

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
            "andor": self._andor_controls.capture_session_state(),
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
        andor = state.get("andor")
        if isinstance(andor, dict):
            self._andor_controls.restore_session_state(andor)
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

    @property
    def references(self) -> list[dict]:
        """Reference records in display order (arrays remain unmodified)."""
        return list(self._references)

    def set_metadata_context(self, context: Optional[dict]) -> None:
        """Attach sidebar/sample context supplied by the owning window."""
        self._metadata_context = dict(context or {})

    def set_sidebar_snapshot_provider(self, provider) -> None:
        """Set the owning instrument sidebar's frame-time readback provider."""
        self._sidebar_snapshot_provider = provider if callable(provider) else None

    def _owner_window(self):
        try:
            return self.window()
        except RuntimeError:
            # A few integrations construct a lightweight instance with
            # ``__new__`` for direct save tests; it has no Qt base state.
            return None

    def _available_metadata_context(self) -> dict:
        context = dict(getattr(self, "_metadata_context", {}) or {})
        provider = getattr(self, "_sidebar_snapshot_provider", None)
        if callable(provider):
            try:
                context["sidebar_readbacks"] = provider()
            except Exception as exc:
                context["sidebar_readbacks"] = {
                    "available": False, "reason": f"Sidebar readback snapshot failed: {exc}"
                }
        host = self._owner_window()
        # A configured provider already captures the sidebar controls and
        # callback caches at frame time.  Avoid invoking MainWindow's broad
        # session capture here, which may mutate session persistence state.
        if callable(provider):
            context.setdefault("sample_id", str(getattr(cfg.session, "sample_id", "") or "") or None)
            return context
        capture = getattr(host, "_capture_session", None)
        if callable(capture):
            try:
                session = capture()
                if isinstance(session, dict):
                    context.setdefault("session", session)
                    context.setdefault("sidebar", session.get("panels", {}))
                    context.setdefault("sample_id", session.get("sample_id"))
            except Exception as exc:
                context.setdefault("sidebar", {"available": False, "reason": f"Sidebar snapshot failed: {exc}"})
        return context

    def _instrument_controllers(self) -> dict:
        controllers = {"lightfield": self._ctrl}
        host = self._owner_window()
        for role, attribute in (("smu", "_smu"), ("rotation", "_rot"),
                                ("stage", "_stg"), ("imaging_stage", "_imaging_stg"),
                                ("power_meter", "_pm"), ("magnet", "_magnet"),
                                ("magnet_2100", "_magnet2100")):
            candidate = getattr(host, attribute, None) if host is not None else None
            if candidate is not None:
                controllers[role] = candidate
        return controllers

    def _capture_frame_provenance(self) -> dict:
        identity = copy.deepcopy(getattr(self._ctrl, "identity", {}) or {})
        try:
            power_factor = power_correction_factor()
        except Exception as exc:
            power_factor = {"available": False, "reason": f"Power correction unavailable: {exc}"}
        try:
            inventory = instrument_inventory(**self._instrument_controllers())
        except Exception as exc:
            inventory = [{"available": False, "reason": f"Instrument inventory unavailable: {exc}"}]
        return {
            "instrument_identity": identity,
            "power_correction_factor": copy.deepcopy(power_factor),
            "instrument_inventory": copy.deepcopy(inventory),
        }

    def _on_add_spectrum(self) -> None:
        # A running acquisition owns the controller.  Capture the most recent
        # completed frame immediately and leave the run's next request alone.
        if self._continuous_mode is not None:
            if (self._continuous_frames > 0 and self._last_data_kind == "spectrum_1d"
                    and self._last_data is not None):
                self.add_reference(
                    self._last_wavelength, self._last_data,
                    settings_snapshot=self._last_acquisition_snapshot,
                )
            else:
                self._status_lbl.setText("No completed live spectrum to add")
            return
        if self._connected and self._pending_acquisition is None:
            self._add_pending = True
            self._apply_settings_then("add_1d")

    def _on_normalize_references(self, enabled: bool) -> None:
        self._normalize_references = bool(enabled)
        self._refresh_reference_curves()
        if self._last_data_kind == "spectrum_1d" and self._last_wavelength is not None and self._last_data is not None:
            self._spec_plot.update_spectrum(self._last_wavelength, self._display_live_counts())

    def _display_live_counts(self) -> np.ndarray:
        counts = np.asarray(self._last_data, dtype=float)
        if not getattr(self, "_normalize_references", False):
            return counts
        peak = float(np.nanmax(np.abs(counts))) if counts.size else 0.0
        return counts / peak if peak > 0 else counts

    def _spectrum_output_root(self) -> Path:
        return Path(getattr(cfg.filename, "base_out", "" ) or Path.cwd()).expanduser().resolve()

    def _change_save_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose spectrum save folder", str(self._spectrum_output_root())
        )
        if folder:
            cfg.filename.base_out = str(Path(folder).expanduser().resolve())
            self._save_root_lbl.setText(str(self._spectrum_output_root()))

    def _open_save_root(self) -> None:
        folder = self._spectrum_output_root()
        folder.mkdir(parents=True, exist_ok=True)
        try:
            import os
            os.startfile(str(folder))
        except (AttributeError, OSError):
            pass

    def _load_spectra_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Load spectra", "", "CSV files (*.csv);;All files (*)"
        )
        if paths:
            try:
                self.load_spectra(paths)
                self._status_lbl.setText(f"Loaded {len(paths)} reference spectra")
            except Exception as exc:
                self._status_lbl.setText(f"Load failed: {str(exc)[:80]}")

    def _on_reference_item_changed(self, item: QListWidgetItem) -> None:
        reference_id = item.data(Qt.ItemDataRole.UserRole)
        ref = self._reference_by_id(reference_id)
        if ref is None:
            return
        edited_name = item.text().strip()
        if edited_name and edited_name != ref["name"]:
            ref["name"] = edited_name
        ref["visible"] = bool(item.data(Qt.ItemDataRole.UserRole + 1))
        self._spec_plot.set_reference_visible(reference_id, ref["visible"])
        row_checkbox = ref.get("_row_checkbox")
        if row_checkbox is not None and row_checkbox.isChecked() != ref["visible"]:
            row_checkbox.setChecked(ref["visible"])

    def _set_reference_visible(self, reference_id: str, visible: bool) -> None:
        ref = self._reference_by_id(reference_id)
        if ref is None:
            return
        ref["visible"] = bool(visible)
        self._spec_plot.set_reference_visible(reference_id, ref["visible"])
        item = next((self._reference_list.item(index) for index in range(self._reference_list.count())
                     if self._reference_list.item(index).data(Qt.ItemDataRole.UserRole) == str(reference_id)), None)
        if item is not None:
            item.setData(Qt.ItemDataRole.UserRole + 1, bool(visible))

    def _set_all_references_visible(self, visible: bool) -> None:
        for ref in self._references:
            self._set_reference_visible(ref["id"], visible)

    def _selected_reference(self) -> Optional[dict]:
        item = self._reference_list.currentItem()
        return self._reference_by_id(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _reference_by_id(self, reference_id: object) -> Optional[dict]:
        return next((ref for ref in self._references if ref["id"] == str(reference_id)), None)

    def _rename_selected_reference(self) -> None:
        ref = self._selected_reference()
        if ref is not None and ref.get("_row_name") is not None:
            ref["_row_name"].setFocus()
            ref["_row_name"].selectAll()

    def _color_selected_reference(self) -> None:
        ref = self._selected_reference()
        if ref is None:
            return
        color = QColorDialog.getColor(QColor(ref["color"]), self, "Reference color")
        if color.isValid():
            self.set_reference_color(ref["id"], color.name())

    def _remove_selected_reference(self) -> None:
        ref = self._selected_reference()
        if ref is not None:
            self.remove_reference(ref["id"])

    def _show_selected_reference_details(self) -> None:
        ref = self._selected_reference()
        if ref is not None:
            self._status_lbl.setToolTip(json.dumps(self.reference_details(ref["id"]), default=str, indent=2))
            self._status_lbl.setText(f"{ref['name']} details available in tooltip")

    def _retry_selected_reference(self) -> None:
        ref = self._selected_reference()
        if ref is not None:
            self.retry_reference_save(ref["id"])

    def _refresh_reference_item(self, ref: dict) -> None:
        for index in range(self._reference_list.count()):
            item = self._reference_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == ref["id"]:
                # The visible row is a custom widget; keeping DisplayRole
                # empty avoids a second text label behind the row controls.
                item.setText("")
                item.setData(Qt.ItemDataRole.UserRole + 2, ref["name"])
                item.setData(Qt.ItemDataRole.UserRole + 1, bool(ref["visible"]))
                state = "saved" if ref["saved"] else "unsaved"
                item.setToolTip(f'{state}: ' + str(ref.get("save_error") or ref.get("path") or ref.get("source") or ""))
                name_edit = ref.get("_row_name")
                if name_edit is not None and name_edit.text() != ref["name"]:
                    name_edit.setText(ref["name"])
                status_label = ref.get("_row_status")
                if status_label is not None:
                    status_label.setText("saved" if ref["saved"] else "unsaved")
                swatch = ref.get("_row_swatch")
                if swatch is not None:
                    swatch.setStyleSheet(f"background-color: {ref['color']}; border: 1px solid #777;")
                break

    def rename_reference(self, reference_id: str, name: str) -> bool:
        ref = self._reference_by_id(reference_id)
        value = str(name).strip()
        if ref is None or not value:
            return False
        ref["name"] = value
        self._refresh_reference_item(ref)
        return True

    def set_reference_color(self, reference_id: str, color: str) -> bool:
        ref = self._reference_by_id(reference_id)
        if ref is None or not QColor(str(color)).isValid():
            return False
        ref["color"] = QColor(str(color)).name()
        self._spec_plot.set_reference(reference_id, ref["wavelength"],
                                      self._display_counts(ref), ref["color"], ref["visible"])
        self._refresh_reference_item(ref)
        return True

    def remove_reference(self, reference_id: str) -> bool:
        ref = self._reference_by_id(reference_id)
        if ref is None:
            return False
        self._references.remove(ref)
        self._spec_plot.remove_reference(reference_id)
        for index in range(self._reference_list.count() - 1, -1, -1):
            item = self._reference_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == str(reference_id):
                self._reference_list.takeItem(index)
                break
        self._retry_reference_btn.setEnabled(any(not r["saved"] for r in self._references))
        return True

    def reference_details(self, reference_id: str) -> dict:
        ref = self._reference_by_id(reference_id)
        if ref is None:
            return {}
        return {
            "id": ref["id"], "name": ref["name"], "source": ref["source"],
            "path": ref.get("path"), "metadata_path": ref.get("metadata_path"),
            "saved": bool(ref["saved"]), "save_error": ref.get("save_error"),
            "points": int(np.asarray(ref["counts"]).size),
            "wavelength_range_nm": [float(ref["wavelength"][0]), float(ref["wavelength"][-1])],
            "settings_snapshot": dict(ref.get("settings_snapshot") or {}),
            "metadata": dict(ref.get("metadata") or {}),
        }

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
        # Freeze the request at dispatch time.  The controls remain editable
        # while the asynchronous controller applies settings; reading them in
        # the completion callback can otherwise attribute a later UI edit to
        # the spectrum that was acquired with the old request.
        requested_at = datetime.now(timezone.utc).isoformat()
        self._pending_acquisition_snapshot = {
            "center_nm": float(self._center.value()),
            "exposure_ms": float(self._exposure.value()),
            "accumulations": int(self._accumulations.value()),
            "requested_epoch_s": time.time(),
            "requested_utc": requested_at,
            "requested": {
                "center_nm": float(self._center.value()),
                "exposure_ms": float(self._exposure.value()),
                "accumulations": int(self._accumulations.value()),
                "timestamp_utc": requested_at,
            },
        }
        self._set_action_controls_enabled(False)
        self._status_lbl.setText("Applying settings…")
        self._ctrl.apply_settings(
            exposure_ms=self._pending_acquisition_snapshot["exposure_ms"],
            center_nm=self._pending_acquisition_snapshot["center_nm"],
            accumulations=self._pending_acquisition_snapshot["accumulations"],
        )

    @Slot()
    def _on_settings_applied(self) -> None:
        pending = self._pending_acquisition
        self._pending_acquisition = None
        snapshot = dict(self._pending_acquisition_snapshot or {})
        applied_at = datetime.now(timezone.utc).isoformat()
        snapshot["applied_epoch_s"] = time.time()
        snapshot["applied_utc"] = applied_at
        snapshot["applied"] = {
            "center_nm": snapshot.get("center_nm"),
            "exposure_ms": snapshot.get("exposure_ms"),
            "accumulations": snapshot.get("accumulations"),
            "timestamp_utc": applied_at,
        }
        self._acquisition_settings_snapshot = snapshot
        self._pending_acquisition_snapshot = dict(snapshot)
        if pending in {"1d", "run_1d", "add_1d"}:
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
        self._pending_acquisition_snapshot = None
        self._continuous_mode = None
        self._add_pending = False
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
        pause = getattr(self._ctrl, "set_temperature_monitor_paused", None)
        if callable(pause):
            pause("spectrum", self._connected and not enabled)
        self._apply_btn.setEnabled(enabled)
        self._acquire_btn.setEnabled(enabled)
        self._acquire_2d_btn.setEnabled(enabled and self._supports_2d)
        self._run_1d_btn.setEnabled(enabled)
        self._run_2d_btn.setEnabled(enabled and self._supports_2d)
        self._center.setEnabled(enabled)
        self._exposure.setEnabled(enabled)
        self._accumulations.setEnabled(enabled)
        self._add_spectrum_btn.setEnabled(
            bool(enabled) or (self._continuous_mode is not None and self._continuous_frames > 0)
        )

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
        self._add_pending = False
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
        self._last_acquisition_snapshot = copy.deepcopy(
            dict(
            self._pending_acquisition_snapshot or self._acquisition_settings_snapshot or {}
            )
        )
        self._pending_acquisition_snapshot = None
        self._last_acquisition_snapshot["actual_readback"] = self._capture_readbacks()
        self._last_acquisition_snapshot.update(self._capture_frame_provenance())
        frame_time = datetime.now(timezone.utc)
        self._last_acquisition_snapshot["completed_utc"] = frame_time.isoformat()
        self._last_acquisition_snapshot["metadata_context"] = copy.deepcopy(
            self._available_metadata_context()
        )
        self._last_acquisition_snapshot["sample_id"] = str(getattr(cfg.session, "sample_id", "") or "")
        self._last_acquisition_snapshot["save_root"] = str(self._spectrum_output_root())
        self._last_data_kind = "spectrum_1d"
        self._last_wavelength = np.asarray(wl, dtype=float).copy()
        self._last_data = np.asarray(cts, dtype=float).copy()
        self._save_btn.setEnabled(True)
        self._spec_plot.update_spectrum(wl, self._display_live_counts())
        self._tabs.setCurrentIndex(0)
        self._status_lbl.setText("Ready")
        if self._add_pending:
            self._add_pending = False
            self.add_reference(self._last_wavelength, self._last_data,
                               settings_snapshot=self._last_acquisition_snapshot)
        self._queue_next_continuous("1d")
        if self._continuous_mode == "1d" and self._continuous_frames > 0:
            self._add_spectrum_btn.setEnabled(True)

    @Slot(object)
    def _on_frame_ready(self, img: np.ndarray):
        self._last_acquisition_snapshot = dict(
            self._pending_acquisition_snapshot or self._acquisition_settings_snapshot or {}
        )
        self._pending_acquisition_snapshot = None
        self._last_data_kind = "frame_2d"
        self._last_wavelength = None
        self._last_data = np.asarray(img, dtype=float).copy()
        self._save_btn.setEnabled(True)
        self._frame_plot.update_frame(img)
        self._tabs.setCurrentIndex(1)
        self._status_lbl.setText("Ready")
        self._queue_next_continuous("2d")

    # ── direct update (called by sweep loop without going through controller) ─

    def push_spectrum(self, wl: np.ndarray, cts: np.ndarray,
                      settings_snapshot: Optional[dict] = None) -> None:
        """Update 1D plot directly (e.g. from a sweep step callback)."""
        self._last_data_kind = "spectrum_1d"
        self._last_wavelength = np.asarray(wl, dtype=float).copy()
        self._last_data = np.asarray(cts, dtype=float).copy()
        self._spec_plot.update_spectrum(wl, self._display_live_counts())
        self._last_acquisition_snapshot = dict(settings_snapshot or {
            "available": False, "source": "external_push",
        })
        self._save_btn.setEnabled(True)

    def push_frame(self, img: np.ndarray,
                   settings_snapshot: Optional[dict] = None) -> None:
        """Update 2D plot directly."""
        self._frame_plot.update_frame(img)
        self._last_data_kind = "frame_2d"
        self._last_wavelength = None
        self._last_data = np.asarray(img, dtype=float).copy()
        self._last_acquisition_snapshot = dict(settings_snapshot or {
            "available": False, "source": "external_push",
        })
        self._save_btn.setEnabled(True)

    def _display_counts(self, ref: dict) -> np.ndarray:
        counts = np.asarray(ref["counts"], dtype=float)
        if getattr(self, "_normalize_references", False):
            peak = float(np.nanmax(np.abs(counts))) if counts.size else 0.0
            if peak > 0:
                return counts / peak
        return counts

    def _add_reference_item(self, ref: dict) -> None:
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, ref["id"])
        # Visibility is represented by the row checkbox widget; the item
        # check state is retained as an internal model value for compatibility.
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        item.setData(Qt.ItemDataRole.UserRole + 1, True)
        self._reference_list.addItem(item)
        row = QWidget()
        row.setMinimumHeight(28)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(2, 0, 2, 0)
        row_layout.setSpacing(5)
        checkbox = QCheckBox()
        checkbox.setChecked(True)
        checkbox.setToolTip("Show or hide this reference")
        checkbox.toggled.connect(lambda checked, reference_id=ref["id"]:
                                  self._set_reference_visible(reference_id, checked))
        swatch = QPushButton()
        swatch.setFixedSize(18, 18)
        swatch.setToolTip("Change reference color")
        swatch.clicked.connect(lambda _checked=False, reference_id=ref["id"]:
                               self._choose_reference_color(reference_id))
        name_edit = QLineEdit(ref["name"])
        name_edit.setFrame(False)
        name_edit.setPlaceholderText("Reference name")
        name_edit.editingFinished.connect(lambda reference_id=ref["id"], edit=name_edit:
                                          self.rename_reference(reference_id, edit.text()))
        status = QLabel()
        status.setMinimumWidth(52)
        row_layout.addWidget(checkbox)
        row_layout.addWidget(swatch)
        row_layout.addWidget(name_edit, 1)
        row_layout.addWidget(status)
        self._reference_list.setItemWidget(item, row)
        item.setSizeHint(row.sizeHint())
        ref["_row_checkbox"], ref["_row_swatch"] = checkbox, swatch
        ref["_row_name"], ref["_row_status"] = name_edit, status
        self._refresh_reference_item(ref)

    def _choose_reference_color(self, reference_id: str) -> None:
        ref = self._reference_by_id(reference_id)
        if ref is None:
            return
        color = QColorDialog.getColor(QColor(ref["color"]), self, "Reference color")
        if color.isValid():
            self.set_reference_color(reference_id, color.name())

    def add_reference(self, wavelength: np.ndarray, counts: np.ndarray,
                      *, name: Optional[str] = None, color: Optional[str] = None,
                      source: str = "acquired", path: Optional[str | Path] = None,
                      settings_snapshot: Optional[dict] = None,
                      metadata: Optional[dict] = None, auto_save: bool = True) -> dict:
        wl = np.asarray(wavelength, dtype=float).copy()
        raw = np.asarray(counts, dtype=float).copy()
        if wl.ndim != 1 or raw.ndim != 1 or wl.size != raw.size or not wl.size:
            raise ValueError("Reference wavelength and intensity axes must be matching non-empty vectors")
        reference_id = f"reference-{time.time_ns()}"
        ordinal = len(self._references) + 1
        default_name = (
            Path(path).stem if path is not None else
            f"Reference {ordinal:02d} · {datetime.now().strftime('%H:%M:%S')}"
        )
        frozen_settings = (settings_snapshot if settings_snapshot is not None
                           else ({} if source == "loaded" else (self._last_acquisition_snapshot or {})))
        ref = {
            "id": reference_id,
            "name": str(name or default_name),
            "wavelength": wl,
            "counts": raw,
            "original_counts": raw.copy(),
            "color": str(color or self._reference_colors[(ordinal - 1) % len(self._reference_colors)]),
            "visible": True,
            "source": str(source),
            "path": str(Path(path).resolve()) if path is not None else None,
            "metadata": dict(metadata or (settings_snapshot or {}).get("metadata_context", {})),
            "settings_snapshot": copy.deepcopy(frozen_settings),
            "sample_id": str((settings_snapshot or {}).get("sample_id") or getattr(cfg.session, "sample_id", "") or ""),
            "save_root": str((settings_snapshot or {}).get("save_root") or self._spectrum_output_root()),
            "observed_readback": (dict(frozen_settings.get("actual_readback", {}))
                                   if isinstance(frozen_settings.get("actual_readback"), dict) else None),
            "saved": bool(source == "loaded" and path is not None and Path(path).exists()),
            "metadata_path": None,
            "save_error": None,
        }
        self._references.append(ref)
        self._spec_plot.set_reference(reference_id, wl, self._display_counts(ref), ref["color"], True)
        self._add_reference_item(ref)
        if auto_save and source == "acquired":
            self._auto_save_reference(ref)
        self._retry_reference_btn.setEnabled(any(not r["saved"] for r in self._references))
        return ref

    def _auto_save_path(self, ref: Optional[dict] = None) -> Path:
        root = Path(str((ref or {}).get("save_root") or self._spectrum_output_root())).expanduser().resolve()
        sample = str((ref or {}).get("sample_id") or getattr(cfg.session, "sample_id", "") or "").strip()
        sample = re.sub(r'[<>:"/\\|?*]+', "_", sample).strip(" .")
        snapshot = (ref or {}).get("settings_snapshot") or {}
        frame_timestamp = str(snapshot.get("completed_utc") or snapshot.get("frame_timestamp_utc") or "")
        try:
            frame_dt = datetime.fromisoformat(frame_timestamp.replace("Z", "+00:00"))
        except ValueError:
            frame_dt = datetime.now(timezone.utc)
        frame_dt = frame_dt.astimezone()
        folder = root / sample if sample else root
        folder = folder / "Spectrum" / frame_dt.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        stamp = frame_dt.strftime("%Y%m%d_%H%M%S_%f")[:-3]
        stem = f"spectrum_{stamp}"
        candidate = folder / f"{stem}.csv"
        sequence = 1
        while candidate.exists() or candidate.with_name(f"{candidate.stem}.experiment.metadata.json").exists():
            candidate = folder / f"{stem}_{sequence:02d}.csv"
            sequence += 1
        return candidate

    def _auto_save_reference(self, ref: dict) -> bool:
        try:
            output = self._auto_save_path(ref)
            metadata_path = self._save_spectrum_data(
                ref["wavelength"], ref["counts"], output,
                settings_snapshot=ref.get("settings_snapshot"),
                source=ref.get("source", "acquired"),
                reference_name=ref.get("name"),
                sample_id=ref.get("sample_id"),
                metadata_context=ref.get("metadata") or None,
                observed_readback=ref.get("observed_readback"),
            )
            ref["path"] = str(output)
            ref["metadata_path"] = str(metadata_path)
            ref["saved"] = True
            ref["save_error"] = None
            self._refresh_reference_item(ref)
            return True
        except Exception as exc:
            ref["saved"] = False
            ref["save_error"] = f"{type(exc).__name__}: {exc}"
            self._refresh_reference_item(ref)
            self._status_lbl.setText("Reference unsaved; use Retry save")
            return False

    def retry_reference_save(self, reference_id: str) -> bool:
        ref = self._reference_by_id(reference_id)
        return bool(ref is not None and self._auto_save_reference(ref))

    def load_spectra(self, paths: list[str | Path], *, normalize: Optional[bool] = None) -> list[dict]:
        loaded = []
        for source_path in paths:
            path = Path(source_path).expanduser().resolve()
            wavelengths, counts = self._read_spectrum_csv(path)
            loaded.append(self.add_reference(
                wavelengths, counts, source="loaded", path=path,
                metadata=self._load_adjacent_metadata(path),
                auto_save=False,
            ))
        if normalize is not None:
            self._normalize_references = bool(normalize)
            self._refresh_reference_curves()
        return loaded

    @staticmethod
    def _read_spectrum_csv(path: Path) -> tuple[np.ndarray, np.ndarray]:
        with path.open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.reader(stream))
        if not rows:
            raise ValueError(f"Empty spectrum file: {path.name}")
        header = [str(value).strip().lower() for value in rows[0]]
        if any(value in {"point_index", "y_pixel"} for value in header):
            raise ValueError(f"Matrix/sweep CSV is not a 1D spectrum: {path.name}")
        wl_idx = next((index for index, value in enumerate(header)
                       if "wavelength" in value or value in {"wl", "x"}), None)
        count_idx = next((index for index, value in enumerate(header)
                          if "intensity" in value or "count" in value or value in {"cts", "y"}), None)
        if wl_idx is None or count_idx is None:
            raise ValueError(f"CSV must contain wavelength and intensity columns: {path.name}")
        wavelengths, counts = [], []
        for row in rows[1:] if len(rows) > 1 else []:
            try:
                wavelengths.append(float(row[wl_idx]))
                counts.append(float(row[count_idx]))
            except (ValueError, IndexError):
                continue
        if not wavelengths:
            raise ValueError(f"No wavelength/intensity rows found in {path.name}")
        return np.asarray(wavelengths), np.asarray(counts)

    @staticmethod
    def _load_adjacent_metadata(path: Path) -> dict:
        """Load a matching experiment sidecar when one records this CSV."""
        try:
            for sidecar in path.parent.glob("*.experiment.metadata.json"):
                with sidecar.open(encoding="utf-8") as stream:
                    payload = json.load(stream)
                files = payload.get("files", []) if isinstance(payload, dict) else []
                if any(Path(str(item.get("path", ""))).name == path.name for item in files if isinstance(item, dict)):
                    return {"metadata_path": str(sidecar), "experiment": payload}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        return {}

    def _refresh_reference_curves(self) -> None:
        for ref in self._references:
            self._spec_plot.set_reference(
                ref["id"], ref["wavelength"], self._display_counts(ref),
                ref["color"], ref["visible"],
            )

    def _save_spectrum_data(self, wavelength: np.ndarray, counts: np.ndarray,
                            output: Path, *, settings_snapshot: Optional[dict],
                            source: str, reference_name: Optional[str] = None,
                            sample_id: Optional[str] = None,
                            metadata_context: Optional[dict] = None,
                            observed_readback: Optional[dict] = None) -> Path:
        output = Path(output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(("wavelength_nm", "intensity_counts"))
            writer.writerows(zip(np.asarray(wavelength, dtype=float).tolist(),
                                 np.asarray(counts, dtype=float).tolist()))
        from app.experiment_metadata import ExperimentMetadataService
        snapshot = dict(settings_snapshot or {"available": False, "source": "unknown"})
        frozen_provenance = bool(settings_snapshot is not None and (
            "instrument_identity" in snapshot or "instrument_inventory" in snapshot
            or "power_correction_factor" in snapshot
        ))
        identity = (copy.deepcopy(snapshot.get("instrument_identity", {}))
                    if frozen_provenance else (getattr(self._ctrl, "identity", {}) or {}))
        device_id = str(identity.get("serial_number") or identity.get("model") or "spectrum")
        readback = (copy.deepcopy(observed_readback) if isinstance(observed_readback, dict)
                    else copy.deepcopy(snapshot.get("actual_readback", {
                        "available": False,
                        "reason": "No frame-time readback snapshot supplied",
                    })))
        power_factor = (copy.deepcopy(snapshot.get("power_correction_factor"))
                        if frozen_provenance else power_correction_factor())
        settings = {
            "display_kind": "spectrum_1d",
            "source": str(source),
            "reference_name": reference_name,
            "acquisition_time_snapshot": snapshot,
            "requested": snapshot.get("requested", snapshot),
            "applied": snapshot.get("applied", {"available": False, "reason": "No applied readback supplied"}),
            "observed": readback,
            "actual_readback": readback,
            "power_correction_factor": power_factor,
            "calibration": {"wavelength_axis_nm": np.asarray(wavelength, dtype=float).tolist(),
                            "source": "acquired_data_axis"},
        }
        controllers = self._instrument_controllers()
        inventory = (copy.deepcopy(snapshot.get("instrument_inventory", []))
                     if frozen_provenance else instrument_inventory(**controllers))
        settings["instrument_identity"] = identity
        settings["instrument_inventory"] = inventory
        effective_sample_id = (sample_id if sample_id is not None else
                               (snapshot.get("sample_id") if frozen_provenance else
                                str(getattr(cfg.session, "sample_id", "") or "")))
        run = ExperimentMetadataService(output.parent).begin(
            "spectrum_preview", device_id, output_dir=output.parent,
            settings=settings,
            instruments=inventory,
            sample_id=str(effective_sample_id or "") or None,
        )
        run.register_file(output, role="raw", kind="spectrum_1d",
                          details={"shape": [int(np.asarray(counts).size)], "source": source})
        context = (copy.deepcopy(snapshot.get("metadata_context", {}))
                   if frozen_provenance and metadata_context is None
                   else (self._available_metadata_context() if metadata_context is None
                         else dict(metadata_context)))
        context.update(dict(metadata_context or {}))
        context.setdefault("sample_id", str(effective_sample_id or "") or None)
        context.setdefault("sidebar", {"available": False, "reason": "No sidebar snapshot supplied"})
        run.metadata["context"] = context
        run.record_export({"path": output.name, "kind": "spectrum_1d"},
                          output={"file": output.name})
        run.complete({"shape": [int(np.asarray(counts).size)], "source": source})
        return run.path

    def _capture_readbacks(self) -> dict:
        controller = self._ctrl
        candidate = getattr(controller, "readback", None) if controller is not None else None
        if callable(candidate):
            try:
                candidate = candidate()
            except Exception as exc:
                return {"available": False, "reason": f"readback failed: {exc}"}
        if isinstance(candidate, dict):
            return {"available": True, "values": dict(candidate),
                    "timestamp_utc": datetime.now(timezone.utc).isoformat()}
        observed = getattr(self, "_observed_readbacks", {})
        if observed:
            return {"available": True, "values": {
                        source: dict(entry.get("value", {}))
                        for source, entry in observed.items()
                    }, "timestamps_utc": {
                        source: entry.get("timestamp_utc")
                        for source, entry in observed.items()
                    }, "source": "controller_signals"}
        return {"available": False, "reason": "Controller did not expose actual readback values"}

    def _cache_readback(self, source: str, value: object) -> None:
        """Keep the latest controller signal with its real arrival time."""
        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], dict):
            value = value[1]
        if isinstance(value, dict):
            payload = dict(value)
        elif source == "detector_temperature":
            payload = {"temperature_c": value}
        else:
            payload = {"value": value}
        self._observed_readbacks[source] = {
            "value": payload,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }

    def _save_current_dialog(self) -> None:
        if self._last_data is None:
            return
        frozen_snapshot = copy.deepcopy(self._last_acquisition_snapshot)
        frozen_data = np.asarray(self._last_data, dtype=float).copy()
        frozen_wavelength = (None if self._last_wavelength is None
                             else np.asarray(self._last_wavelength, dtype=float).copy())
        frozen_kind = self._last_data_kind
        path, _ = QFileDialog.getSaveFileName(self, "Save current acquisition", "", "CSV (*.csv)")
        if path:
            try:
                self.save_current(path, settings_snapshot=frozen_snapshot,
                                  data=frozen_data, wavelength=frozen_wavelength,
                                  data_kind=frozen_kind)
                self._status_lbl.setText("Saved current acquisition")
            except Exception as exc:
                self._status_lbl.setText(f"Save failed: {str(exc)[:80]}")

    def save_current(self, output_path: str | Path, *, settings_snapshot: Optional[dict] = None,
                     data: Optional[np.ndarray] = None,
                     wavelength: Optional[np.ndarray] = None,
                     data_kind: Optional[str] = None) -> Path:
        """Deliberately save the currently displayed data and sidecar."""
        last_data = self._last_data if data is None else np.asarray(data, dtype=float)
        last_wavelength = self._last_wavelength if wavelength is None else wavelength
        last_kind = self._last_data_kind if data_kind is None else data_kind
        if last_data is None or last_kind is None:
            raise RuntimeError("No acquired spectrum or frame is available")
        output = Path(output_path).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        if last_kind == "spectrum_1d":
            if last_wavelength is None or last_wavelength.size != last_data.size:
                raise ValueError("Spectrum wavelength and intensity axes do not match")
            self._save_spectrum_data(
                last_wavelength, last_data, output,
                settings_snapshot=(settings_snapshot if settings_snapshot is not None
                                   else self._last_acquisition_snapshot),
                source="current", reference_name=None,
            )
        else:
            np.savetxt(output, last_data, delimiter=",")
            # Keep 2D behavior and existing output format unchanged.
            from app.experiment_metadata import ExperimentMetadataService
            settings = {"display_kind": last_kind,
                        "acquisition_time_snapshot": (settings_snapshot if settings_snapshot is not None
                                                       else self._last_acquisition_snapshot),
                        "power_correction_factor": power_correction_factor(),
                        "calibration": {"wavelength_axis_nm": None,
                                        "source": "acquired_data_axis"}}
            identity = getattr(self._ctrl, "identity", {}) or {}
            device_id = str(identity.get("serial_number") or identity.get("model") or "spectrum")
            run = ExperimentMetadataService(output.parent).begin(
                "spectrum_preview", device_id, output_dir=output.parent,
                settings=settings,
                instruments=instrument_inventory(lightfield=self._ctrl),
            )
            run.register_file(output, role="raw", kind=last_kind,
                              details={"shape": list(last_data.shape)})
            run.record_export({"path": output.name, "kind": last_kind},
                              output={"file": output.name})
            run.complete({"shape": list(last_data.shape)})
        return output
