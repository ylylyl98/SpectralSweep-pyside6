"""Spectrum display with real-coordinate, shared-Y broken wavelength axes.

Only display arrays are consumed here. Acquisition and saved counts stay with
SpectrumPanel. Each segment uses an ordinary ViewBox: no wavelength remapping.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QSignalBlocker, QTimer
from PySide6.QtGui import QFontMetricsF
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QCheckBox, QPushButton, QStackedWidget, QSizePolicy

PHOTON_EV_NM = 1239.8419843320026
LIVE_SPECTRUM_COLOR = "#1565C0"
LIVE_SPECTRUM_WIDTH = 2.4
REFERENCE_SPECTRUM_WIDTH = 1.8
REFERENCE_COLORS = ("#D13438", "#107C10", "#8764B8", "#CA5010", "#038387", "#5C2D91", "#8E562E", "#C239B3")


def _wavelength_energy_text(value):
    try:
        wavelength = float(value)
    except (TypeError, ValueError):
        return "Not read"
    if not np.isfinite(wavelength) or wavelength < 0:
        return "Not read"
    if wavelength == 0:
        return "0 nm (zero order)"
    return f"{wavelength:g} nm / {PHOTON_EV_NM / wavelength:.4f} eV"


class _EnergyAxis(pg.AxisItem):
    def __init__(self):
        super().__init__(orientation="top")
        self.setLabel("Energy", units="eV")
        self.enableAutoSIPrefix(False)

    def tickStrings(self, values, scale, spacing):
        return [f"{PHOTON_EV_NM / value:.6g}" if np.isfinite(value) and value > 0 else "" for value in values]


def wavelength_segments(arrays):
    """Merge sampled bands; fold only gaps larger than 10 nm and 10% of a band.

Sparse arrays are still bands. Internal discontinuities must additionally be
larger than ten typical sample steps. Intensity is deliberately not inspected.
    """
    intervals = []
    for values in arrays:
        x = np.unique(np.asarray(values)[np.isfinite(values)])
        if not x.size:
            continue
        steps = np.diff(x)
        splits = np.flatnonzero(steps > max(10., 10 * float(np.median(steps)))) + 1 if steps.size else []
        intervals.extend((float(part[0]), float(part[-1])) for part in np.split(x, splits))
    merged = []
    for lo, hi in sorted(intervals):
        if merged:
            previous_lo, previous_hi = merged[-1]
            threshold = max(10., .1 * min(previous_hi - previous_lo, hi - lo))
            if lo - previous_hi <= threshold:
                merged[-1] = (previous_lo, max(previous_hi, hi))
                continue
        merged.append((lo, hi))
    return merged


class _SpectrumPlot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.axis_unit = "nm"
        self._reverse_pixels = False
        self._normalized = False
        self._compare = self._hide_gaps = False
        self._range_mode = "live"
        self._follow_x = self._follow_y = True
        self._setting_range = False
        self._wl = self._cts = np.array([])
        self._reference_curves = {}
        self._reference_units = {}
        self._reference_visible = {}
        self._segments = []
        self._extra_curves = []
        self._break_widgets = []
        self._width_update_pending = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._display_stack = QStackedWidget()
        self._empty_label = QLabel("No spectrum yet\n\nConnect a detector to acquire, or use Load spectra to compare saved data.")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        self._empty_label.setAccessibleName("Spectrum empty state")
        self._display_stack.addWidget(self._empty_label)
        self._plot_container = QWidget()
        self._plot_row = QHBoxLayout(self._plot_container)
        self._plot_row.setContentsMargins(0, 0, 0, 0)
        self._plot_row.setSpacing(0)
        self._display_stack.addWidget(self._plot_container)
        layout.addWidget(self._display_stack, 1)
        self._plot = self._make_plot()
        self._curve = self._make_live_curve(self._plot)
        self.segment_plots = [self._plot]
        self._plot_row.addWidget(self._plot, 1)
        info = QGridLayout()
        self._peak_lbl = QLabel("Peak: —")
        self._range_lbl = QLabel("Range: —")
        for label in (self._peak_lbl, self._range_lbl):
            label.setWordWrap(True)
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._autoscale_chk = QCheckBox("Auto Y")
        self._autoscale_chk.setChecked(True)
        self._autoscale_chk.setToolTip("Follow live intensity, or all visible spectra in Compare. Manual Y zoom pauses this.")
        self._fit_live_btn = QPushButton("Fit live")
        self._fit_visible_btn = QPushButton("Fit visible")
        info.addWidget(self._peak_lbl, 0, 0)
        info.addWidget(self._range_lbl, 0, 1)
        info.setColumnStretch(0, 1)
        info.setColumnStretch(1, 1)
        view_actions = QHBoxLayout()
        view_actions.addStretch()
        for widget in (self._fit_live_btn, self._fit_visible_btn, self._autoscale_chk):
            view_actions.addWidget(widget)
        info.addLayout(view_actions, 1, 0, 1, 2)
        layout.addLayout(info)
        self._autoscale_chk.toggled.connect(self._toggle_auto_y)
        self._fit_live_btn.clicked.connect(lambda: self.fit("live"))
        self._fit_visible_btn.clicked.connect(lambda: self.fit("visible"))
        self._refresh_empty_state()

    def _refresh_empty_state(self):
        def has_points(x, y):
            return x is not None and y is not None and bool((np.isfinite(x) & np.isfinite(y)).any())

        live = has_points(self._wl, self._cts)
        visible = live or any(
            self._reference_visible.get(key, False)
            and self._reference_units.get(key) == self.axis_unit
            and has_points(curve.xData, curve.yData)
            for key, curve in self._reference_curves.items()
        )
        self._display_stack.setCurrentWidget(self._plot_container if visible else self._empty_label)
        self._empty_label.setText(
            "No visible spectra\n\nShow a reference or change Spectrum view to Compare."
            if self._reference_curves else
            "No spectrum yet\n\nConnect a detector to acquire, or use Load spectra to compare saved data."
        )
        self._fit_live_btn.setEnabled(live)
        self._fit_visible_btn.setEnabled(visible)
        self._autoscale_chk.setEnabled(visible)

    def _make_plot(self):
        plot = pg.PlotWidget(axisItems={"top": _EnergyAxis()})
        # Keep the right edge and endpoint tick labels inside the canvas,
        # including when the reference pane narrows the plot. Font-relative
        # spacing scales with the native Qt font instead of changing data ranges.
        margin = int(QFontMetricsF(self.font()).horizontalAdvance("0000.0000") / 2) + 4
        plot.getPlotItem().layout.setContentsMargins(0, 0, margin, 0)
        plot.showAxis("right")
        plot.getAxis("right").setStyle(showValues=False, tickLength=0)
        plot.getAxis("right").setWidth(1)
        plot.showGrid(x=True, y=True, alpha=.3)
        plot.disableAutoRange()
        plot.getAxis("bottom").enableAutoSIPrefix(False)
        plot.getViewBox().sigRangeChangedManually.connect(self._manual_range)
        plot.getViewBox().sigYRangeChanged.connect(self._sync_y_ranges)
        plot.getViewBox().sigResized.connect(self._schedule_width_update)
        # Programmatic zooms also disable following; updates guarded below do not.
        if not hasattr(self, "_plot"):
            plot.getViewBox().sigRangeChanged.connect(self._range_changed)
        self._label_plot(plot)
        return plot

    def _label_plot(self, plot):
        pixel_label = "Pixel (reversed; uncalibrated)" if self._reverse_pixels else "Pixel (uncalibrated)"
        plot.setLabel("bottom", pixel_label if self.axis_unit == "pixel" else "Wavelength",
                      units="" if self.axis_unit == "pixel" else "nm")
        plot.showAxis("top", self.axis_unit == "nm")
        plot.setLabel("left", "Normalized intensity" if self._normalized else "Intensity",
                      units="" if self._normalized else "counts")
        plot.getViewBox().invertX(self._reverse_pixels)

    @staticmethod
    def _make_live_curve(plot):
        curve = plot.plot(pen=pg.mkPen(LIVE_SPECTRUM_COLOR, width=LIVE_SPECTRUM_WIDTH, style=Qt.PenStyle.SolidLine), connect="finite")
        curve.setZValue(10)
        return curve

    def _manual_range(self, axes):
        if axes[0]:
            self._follow_x = False
        if axes[1]:
            self._follow_y = False
            with QSignalBlocker(self._autoscale_chk):
                self._autoscale_chk.setChecked(False)

    def _range_changed(self, view, ranges, changed):
        if not self._setting_range:
            self._manual_range(changed)

    def _sync_y_ranges(self, view, limits):
        if self._setting_range:
            return
        self._manual_range([False, True])
        self._setting_range = True
        try:
            for plot in self.segment_plots:
                if plot.getViewBox() is not view:
                    plot.setYRange(*limits, padding=0)
        finally:
            self._setting_range = False

    def _toggle_auto_y(self, enabled):
        self._follow_y = bool(enabled)
        self._update_ranges()

    def set_normalized(self, enabled):
        if self._normalized != bool(enabled):
            self._follow_y = True
            with QSignalBlocker(self._autoscale_chk):
                self._autoscale_chk.setChecked(True)
        self._normalized = bool(enabled)
        for plot in self.segment_plots:
            self._label_plot(plot)
        self._update_info()

    def set_axis_unit(self, unit, reverse_pixels=False):
        unit = "pixel" if unit == "pixel" else "nm"
        reverse_pixels = bool(reverse_pixels and unit == "pixel")
        changed = self.axis_unit != unit
        if not changed and self._reverse_pixels == reverse_pixels:
            return
        self.axis_unit = unit
        self._reverse_pixels = reverse_pixels
        self._setting_range = True
        try:
            for plot in self.segment_plots:
                self._label_plot(plot)
        finally:
            self._setting_range = False
        self._refresh(force=changed)

    def set_comparison(self, compare, hide_gaps):
        changed = (self._compare, self._hide_gaps) != (bool(compare), bool(hide_gaps))
        if not changed:
            return
        self._compare, self._hide_gaps = bool(compare), bool(hide_gaps)
        if changed:
            self._range_mode = "visible" if compare else "live"
        self._refresh(force=changed)

    def set_reference(self, reference_id, wl, cts, color, visible=True, axis_unit="nm"):
        key = str(reference_id)
        curve = self._reference_curves.get(key)
        if curve is None:
            curve = self._plot.plot(connect="finite")
            self._reference_curves[key] = curve
        curve.setPen(pg.mkPen(color, width=REFERENCE_SPECTRUM_WIDTH, style=Qt.PenStyle.SolidLine))
        curve.setData(np.asarray(wl, dtype=float), np.asarray(cts, dtype=float))
        self._reference_units[key] = axis_unit
        self._reference_visible[key] = bool(visible)
        self._refresh()

    def set_reference_visible(self, reference_id, visible):
        key = str(reference_id)
        if self._reference_visible.get(key) == bool(visible):
            return
        self._reference_visible[key] = bool(visible)
        self._refresh()

    def remove_reference(self, reference_id):
        key = str(reference_id)
        curve = self._reference_curves.pop(key, None)
        self._reference_units.pop(key, None)
        self._reference_visible.pop(key, None)
        if curve is not None:
            self._plot.removeItem(curve)
        self._refresh()

    def update_spectrum(self, wl, cts):
        self._wl = np.asarray(wl, dtype=float)
        self._cts = np.asarray(cts, dtype=float)
        self._curve.setData(self._wl, self._cts)
        self._refresh()
        self._update_info()

    def _update_info(self):
        good = np.isfinite(self._cts) & np.isfinite(self._wl)
        self._peak_lbl.setText("Peak: —")
        self._range_lbl.setText("Range: —")
        if good.any():
            indices = np.flatnonzero(good)
            peak = indices[np.argmax(self._cts[good])]
            position = f"pixel {self._wl[peak]:g}" if self.axis_unit == "pixel" else _wavelength_energy_text(self._wl[peak])
            intensity = f"{self._cts[peak]:.3g}" + (" (normalized)" if self._normalized else " cts")
            self._peak_lbl.setText(f"Peak: {intensity} @ {position}")
            self._range_lbl.setText(f"Range: {self._wl[good].min():.1f} – {self._wl[good].max():.1f} {self.axis_unit}")

    def _visible_series(self):
        series = [(self._wl, self._cts)] if self._wl.size else []
        series.extend((curve.xData, curve.yData) for curve in self._reference_curves.values()
                      if curve.isVisible() and curve.xData is not None)
        return series

    def _range_series(self):
        if self._range_mode == "live" and self._wl.size:
            return [(self._wl, self._cts)]
        return self._visible_series()

    def _refresh(self, force=False):
        for key, curve in self._reference_curves.items():
            curve.setVisible(self._reference_visible.get(key, False) and self._reference_units.get(key) == self.axis_unit)
        self._refresh_empty_state()
        series = self._range_series()
        arrays = [x for x, _ in series]
        segments = wavelength_segments(arrays)
        if segments and not (self._compare and self._hide_gaps and self.axis_unit == "nm" and self._range_mode == "visible"):
            segments = [(segments[0][0], segments[-1][1])]
        geometry_changed = segments != self._segments
        self._setting_range = True
        try:
            if geometry_changed:
                self._rebuild_segments(segments)
            self._segments = segments
            self._curve.setData(self._wl, self._cts, connect=self._connections(self._wl))
            for curve in self._reference_curves.values():
                curve.setData(curve.xData, curve.yData, connect=self._connections(curve.xData))
            for index, (live, references) in enumerate(self._extra_curves):
                live.setData(self._wl, self._cts, connect=self._connections(self._wl))
                for key in list(references):
                    if key not in self._reference_curves:
                        self.segment_plots[index + 1].removeItem(references.pop(key))
                for key, source in self._reference_curves.items():
                    curve = references.get(key)
                    if curve is None:
                        plot = self.segment_plots[index + 1]
                        curve = plot.plot(connect="finite")
                        references[key] = curve
                    curve.setPen(source.opts['pen'])
                    curve.setData(source.xData, source.yData, connect=self._connections(source.xData))
                    curve.setVisible(source.isVisible())
            if force:
                self._follow_x = self._follow_y = True
                with QSignalBlocker(self._autoscale_chk):
                    self._autoscale_chk.setChecked(True)
            self._update_ranges(update_x=force or geometry_changed)
        finally:
            self._setting_range = False

    def _connections(self, wavelengths):
        if wavelengths is None or len(self._segments) < 2:
            return "finite"
        connect = np.isfinite(wavelengths)
        for left, right in zip(self._segments, self._segments[1:]):
            crossings = ((np.minimum(wavelengths[:-1], wavelengths[1:]) <= left[1])
                         & (np.maximum(wavelengths[:-1], wavelengths[1:]) >= right[0]))
            connect[:-1] &= ~crossings
        return connect

    def _rebuild_segments(self, segments):
        previous_ranges = dict(zip(self._segments, (plot.viewRange() for plot in self.segment_plots)))
        shared_y = self._plot.viewRange()[1]
        for plot in self.segment_plots[1:]:
            self._plot_row.removeWidget(plot)
            plot.hide()
            plot.deleteLater()
        for widget in self._break_widgets:
            self._plot_row.removeWidget(widget)
            widget.deleteLater()
        self._extra_curves = []
        self._break_widgets = []
        self.segment_plots = [self._plot]
        for index in range(1, len(segments)):
            gap = segments[index][0] - segments[index - 1][1]
            marker = QLabel(f"//\n{gap:g} nm\nomitted")
            marker.setAlignment(Qt.AlignmentFlag.AlignCenter)
            marker.setToolTip(f"No sampled wavelengths between {segments[index - 1][1]:g} and {segments[index][0]:g} nm")
            marker.setAccessibleName(marker.toolTip())
            self._plot_row.addWidget(marker)
            self._break_widgets.append(marker)
            plot = self._make_plot()
            # Share numerical Y limits. Native setYLink adjusts limits to screen
            # geometry while axes settle, which changes a user's requested zoom.
            self._plot_row.addWidget(plot)
            self.segment_plots.append(plot)
            self._extra_curves.append((self._make_live_curve(plot), {}))
        for index, (lo, hi) in enumerate(segments):
            plot = self.segment_plots[index]
            self._plot_row.setStretchFactor(plot, max(1, int(round((hi - lo) * 10))))
            previous = previous_ranges.get((lo, hi))
            pad = max((hi - lo) * .025, .5 if lo == hi else 0.)
            x_range = previous[0] if previous and not self._follow_x else (lo - pad, hi + pad)
            plot.setRange(xRange=x_range, yRange=shared_y, padding=0)
        self._schedule_width_update()

    def _schedule_width_update(self, *_args):
        if not self._width_update_pending:
            self._width_update_pending = True
            QTimer.singleShot(0, self._update_segment_widths)

    def _update_segment_widths(self):
        self._width_update_pending = False
        if len(self.segment_plots) < 2:
            return
        # Divide the data area proportionally, excluding axis labels/ticks.
        # Proportional widget widths alone distort nm/pixel in narrower bands.
        spans = [max(1e-9, hi - lo) for lo, hi in self._segments]
        overhead = [plot.width() - plot.getViewBox().width() for plot in self.segment_plots]
        available = sum(plot.width() for plot in self.segment_plots) - sum(overhead)
        if available <= 0:
            return
        for plot, span, margin in zip(self.segment_plots, spans, overhead):
            self._plot_row.setStretchFactor(plot, max(1, round(100 * (available * span / sum(spans) + margin))))

    def _update_ranges(self, update_x=False):
        previous_guard = self._setting_range
        self._setting_range = True
        try:
            if update_x and self._follow_x:
                for plot, (lo, hi) in zip(self.segment_plots, self._segments):
                    pad = max((hi - lo) * .025, .5 if lo == hi else 0.)
                    plot.setXRange(lo - pad, hi + pad, padding=0)
            if self._follow_y:
                finite = [y[np.isfinite(x) & np.isfinite(y)] for x, y in self._range_series()]
                finite = [y for y in finite if y.size]
                if finite:
                    lo, hi = min(float(y.min()) for y in finite), max(float(y.max()) for y in finite)
                    pad = max((hi - lo) * .06, abs(hi) * .01, 1e-9)
                    for plot in self.segment_plots:
                        plot.setYRange(lo - pad, hi + pad, padding=0)
        finally:
            self._setting_range = previous_guard

    def fit(self, mode):
        self._range_mode = mode
        self._refresh(force=True)

    def capture_view(self):
        return {"mode": self._range_mode, "follow_x": self._follow_x, "follow_y": self._follow_y,
                "compare": self._compare, "hide_gaps": self._hide_gaps,
                "normalized": self._normalized,
                "segments": list(self._segments), "ranges": [plot.viewRange() for plot in self.segment_plots]}

    def restore_view(self, state):
        if not state:
            self.fit("visible" if self._compare else "live")
            return
        same_mode = (state['compare'], state['hide_gaps']) == (self._compare, self._hide_gaps)
        self._range_mode = state['mode'] if same_mode else ("visible" if self._compare else "live")
        self._refresh(force=True)
        self._follow_x = state['follow_x']
        self._follow_y = state['follow_y'] if state['normalized'] == self._normalized else True
        with QSignalBlocker(self._autoscale_chk):
            self._autoscale_chk.setChecked(self._follow_y)
        self._setting_range = True
        try:
            saved_ranges = dict(zip(state['segments'], state['ranges'])) if same_mode else {}
            shared_y = state['ranges'][0][1] if not self._follow_y else self._plot.viewRange()[1]
            for plot, segment in zip(self.segment_plots, self._segments):
                ranges = saved_ranges.get(segment)
                plot.setRange(xRange=ranges[0] if ranges else plot.viewRange()[0], yRange=shared_y, padding=0)
        finally:
            self._setting_range = False
