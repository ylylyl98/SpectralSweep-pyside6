"""Numerical state for native Quick scientific views; acquisition stays outside.

Arrays are copied once at the presentation boundary, never converted to QML
lists. The renderer may reduce dense *monotone* curves to a per-pixel envelope;
the immutable originals remain available for analysis/export.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

import numpy as np
from PySide6.QtCore import QObject, Property, QRectF, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QImage, QPen


def finite_runs(x, y, connect="finite"):
    """Return original index runs, breaking on NaN and explicit edge masks."""
    finite = np.isfinite(x) & np.isfinite(y)
    indices = np.flatnonzero(finite)
    if not indices.size:
        return []
    breaks = np.diff(indices) != 1
    if not isinstance(connect, str):
        edges = np.asarray(connect, dtype=bool)
        if edges.size not in {max(0, len(x) - 1), len(x)}:
            raise ValueError("Connection mask must have N or N-1 entries")
        if indices.size > 1:
            breaks |= ~edges[indices[:-1]]
    return np.split(indices, np.flatnonzero(breaks) + 1)


def envelope_indices(x, y, run, limits, width):
    """Bound dense monotone drawing while retaining both extrema in each bin.

    Nonmonotone paths are left intact: decimating them could invent a route.
    One point outside each edge is retained for correct clipping.
    """
    if run.size < max(8, width * 4):
        return run
    values = x[run]
    differences = np.diff(values)
    ascending = bool(np.all(differences >= 0))
    descending = bool(np.all(differences <= 0))
    if not (ascending or descending):
        return run
    if descending:
        return envelope_indices(x, y, run[::-1], limits, width)[::-1]
    first = max(0, int(np.searchsorted(values, limits[0], side="left")) - 1)
    last = min(run.size, int(np.searchsorted(values, limits[1], side="right")) + 1)
    visible = run[first:last]
    if visible.size < width * 4:
        return visible
    bins = np.clip(((x[visible] - limits[0]) * width / (limits[1] - limits[0])).astype(int), 0, width - 1)
    boundaries = np.r_[0, np.flatnonzero(np.diff(bins)) + 1, visible.size]
    selected = []
    for start, stop in zip(boundaries[:-1], boundaries[1:]):
        part = visible[start:stop]
        selected.extend((part[0], part[np.argmin(y[part])], part[np.argmax(y[part])], part[-1]))
    # Preserve traversal order, including a reversed run.
    selected = np.unique(selected)
    return selected if run[0] <= run[-1] else selected[::-1]


def nice_ticks(lo, hi, target=6):
    if not np.isfinite([lo, hi]).all() or hi <= lo:
        return []
    raw = (hi - lo) / max(2, target)
    power = 10. ** math.floor(math.log10(raw))
    step = next((factor * power for factor in (1, 2, 2.5, 5, 10) if factor * power >= raw), 10 * power)
    return [float(value) for value in np.arange(math.ceil(lo / step) * step, hi + step * 1e-8, step)]


@dataclass
class ScientificCurve:
    x: np.ndarray
    y: np.ndarray
    pen: QPen = field(default_factory=lambda: QPen(QColor("#1565c0"), 1.8))
    brush: QBrush = field(default_factory=QBrush)
    visible: bool = True
    symbol: str = ""
    size: float = 6.
    name: str = ""
    z: float = 0.
    line_visible: bool = True
    symbol_pen: object = None
    symbol_brush: object = None
    connect: object = "finite"
    runs: list = field(default_factory=list)


class ScientificPlotModel(QObject):
    changed = Signal()
    rangeChanged = Signal()
    manualRangeChanged = Signal(bool, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.curves: dict[str, ScientificCurve] = {}
        self.annotations = {}
        self.x_range = [0., 1.]
        self.y_range = [0., 1.]
        self.x_label = ""
        self.y_label = ""
        self.energy_axis = False
        self.reverse_x = False
        self.reverse_y = False
        self.grid = True
        self.legend = False
        self.legend_offset = (8., 8.)
        self.legend_size = None
        self.extra_margins = (0., 0., 0., 0.)
        self.log_x = self.log_y = False
        self.auto_x = self.auto_y = True
        self.image = None
        self.image_rect = QRectF()
        self.image_levels = (0., 1.)
        self.color_map = None
        self.raster = QImage()
        self.empty_text = "No data yet"
        self.fit_handler = None

    hasData = Property(bool, lambda self: self.image is not None or any(c.visible and c.runs for c in self.curves.values()), notify=changed)
    hasImage = Property(bool, lambda self: self.image is not None, notify=changed)
    levelMinimum = Property(float, lambda self: self.image_levels[0], notify=changed)
    levelMaximum = Property(float, lambda self: self.image_levels[1], notify=changed)

    def set_curve(self, key, x, y, **style):
        x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
        if x.ndim != 1 or y.ndim != 1 or x.size != y.size:
            raise ValueError("Scientific curves require equal-length one-dimensional x/y arrays")
        if "connect" in style and not isinstance(style["connect"], str):
            style["connect"] = np.asarray(style["connect"], dtype=bool)
        previous = self.curves.get(str(key))
        for name in style:
            if not hasattr(ScientificCurve, name) and name not in ScientificCurve.__dataclass_fields__:
                raise TypeError(f"Unsupported curve property: {name}")
        def unchanged_array(values, original):
            # Owned arrays are immutable. Read-only inputs from another band
            # are compared against our snapshot: their backing buffer may still
            # be writable, so aliasing them would corrupt scientific exports.
            return values is original or (not values.flags.writeable and
                                           np.array_equal(values, original, equal_nan=True))
        def same_style(left, right):
            if isinstance(left, np.ndarray) or isinstance(right, np.ndarray):
                return np.array_equal(left, right)
            return left == right
        x_changed = previous is None or not unchanged_array(x, previous.x)
        y_changed = previous is None or not unchanged_array(y, previous.y)
        style_changed = previous is None or any(not same_style(getattr(previous, name), value)
                                               for name, value in style.items())
        if not x_changed and not y_changed and not style_changed:
            return previous
        if previous is None:
            previous = ScientificCurve(x.copy(), y.copy())
            self.curves[str(key)] = previous
        else:
            if x_changed:
                previous.x = x.copy()
            if y_changed:
                previous.y = y.copy()
        previous.x.setflags(write=False)
        previous.y.setflags(write=False)
        for name, value in style.items():
            setattr(previous, name, value.copy() if isinstance(value, np.ndarray) else value)
        previous.runs = finite_runs(*self.display_arrays(previous.x, previous.y), previous.connect)
        if self.auto_x or self.auto_y:
            self.fit(self.auto_x, self.auto_y)
        self.changed.emit()
        return previous

    def display_arrays(self, x, y):
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.log10(x) if self.log_x else x, np.log10(y) if self.log_y else y

    def set_log_mode(self, x=False, y=False):
        if (self.log_x, self.log_y) == (bool(x), bool(y)):
            return
        self.log_x, self.log_y = bool(x), bool(y)
        for curve in self.curves.values():
            curve.runs = finite_runs(*self.display_arrays(curve.x, curve.y), curve.connect)
        self.fit()
        self.changed.emit()

    def remove_curve(self, key):
        self.curves.pop(str(key), None)
        self.changed.emit()

    def export_arrays(self):
        """Return independent numerical originals, without view downsampling."""
        return {key: (curve.x.copy(), curve.y.copy()) for key, curve in self.curves.items()}

    def set_image(self, rows, *, rect=None, auto_levels=True, fit_ranges=True):
        arr = np.asarray(rows, dtype=float)
        if arr.ndim != 2 or not arr.size:
            raise ValueError("Scientific images require a nonempty rows-by-columns array")
        self.image = arr.copy()
        self.image.setflags(write=False)
        self.image_rect = QRectF(rect) if rect is not None else QRectF(0, 0, arr.shape[1], arr.shape[0])
        if auto_levels:
            good = arr[np.isfinite(arr)]
            lo, hi = (float(good.min()), float(good.max())) if good.size else (0., 1.)
            self.image_levels = (lo, hi if hi > lo else lo + 1.)
        self.rebuild_raster()
        if fit_ranges and (self.auto_x or self.auto_y):
            self.fit(self.auto_x, self.auto_y)
        self.changed.emit()

    def rebuild_raster(self):
        if self.image is None:
            self.raster = QImage()
            return
        lo, hi = self.image_levels
        values = np.clip(np.nan_to_num((self.image - lo) / (hi - lo), nan=0., posinf=1., neginf=0.), 0., 1.)
        if self.color_map is not None:
            rgba = np.asarray(self.color_map.map(values, mode="byte"), dtype=np.uint8)
        else:
            gray = (values * 255).astype(np.uint8)
            rgba = np.stack((gray, gray, gray, np.full_like(gray, 255)), axis=-1)
        rgba = np.ascontiguousarray(rgba)
        rgba[~np.isfinite(self.image), 3] = 0
        self.raster = QImage(rgba.data, rgba.shape[1], rgba.shape[0], rgba.strides[0], QImage.Format.Format_RGBA8888).copy()

    def set_levels(self, lo, hi):
        if not np.isfinite([lo, hi]).all() or hi <= lo:
            raise ValueError("Image levels must be finite and increasing")
        levels = (float(lo), float(hi))
        if levels == self.image_levels:
            return
        self.image_levels = levels
        self.rebuild_raster()
        self.changed.emit()

    @Slot(float, float, result=bool)
    def setImageLevels(self, lo, hi):
        if not np.isfinite([lo, hi]).all() or hi <= lo:
            return False
        self.set_levels(lo, hi)
        return True

    @Slot()
    def autoImageLevels(self):
        if self.image is None:
            return
        finite = self.image[np.isfinite(self.image)]
        lo, hi = (float(finite.min()), float(finite.max())) if finite.size else (0., 1.)
        self.set_levels(lo, hi if hi > lo else lo + 1.)

    def set_ranges(self, x=None, y=None, *, manual=False):
        for values in (x, y):
            if values is not None and (len(values) != 2 or not np.isfinite(values).all() or values[0] >= values[1]):
                raise ValueError("View ranges must be finite and increasing")
        changed = ((x is not None and list(map(float, x)) != self.x_range) or
                   (y is not None and list(map(float, y)) != self.y_range))
        if x is not None:
            self.x_range = list(map(float, x))
        if y is not None:
            self.y_range = list(map(float, y))
        if manual:
            if x is not None:
                self.auto_x = False
            if y is not None:
                self.auto_y = False
            self.manualRangeChanged.emit(x is not None, y is not None)
        if changed:
            self.rangeChanged.emit()
            self.changed.emit()

    @Slot()
    def fitAll(self):
        if self.fit_handler is not None:
            self.fit_handler()
            return
        self.auto_x = self.auto_y = True
        self.fit()

    def fit(self, x=True, y=True):
        xs, ys = [], []
        for curve in self.curves.values():
            if not curve.visible:
                continue
            displayed_x, displayed_y = self.display_arrays(curve.x, curve.y)
            finite = np.isfinite(displayed_x) & np.isfinite(displayed_y)
            if finite.any():
                xs.extend((float(displayed_x[finite].min()), float(displayed_x[finite].max())))
                ys.extend((float(displayed_y[finite].min()), float(displayed_y[finite].max())))
        for annotation in self.annotations.values():
            if annotation.get("points"):
                px, py = zip(*annotation["points"])
                xs.extend(px)
                ys.extend(py)
        if self.image is not None:
            xs.extend((self.image_rect.left(), self.image_rect.right()))
            ys.extend((self.image_rect.top(), self.image_rect.bottom()))
        def padded(values):
            lo, hi = min(values), max(values)
            pad = max((hi - lo) * .025, abs(hi) * .001, 1e-9)
            return [lo - pad, hi + pad]
        self.set_ranges(padded(xs) if x and xs else None, padded(ys) if y and ys else None)

    @Slot(float, float, float, bool, bool)
    def zoom(self, factor, x_fraction, y_fraction, x_axis=True, y_axis=True):
        if not np.isfinite(factor) or factor <= 0:
            return
        def zoomed(bounds, fraction):
            anchor = bounds[0] + fraction * (bounds[1] - bounds[0])
            return [anchor + (value - anchor) * factor for value in bounds]
        self.set_ranges(zoomed(self.x_range, x_fraction) if x_axis else None,
                        zoomed(self.y_range, y_fraction) if y_axis else None, manual=True)

    @Slot(float, float)
    def pan(self, dx_fraction, dy_fraction):
        def shifted(bounds, fraction):
            delta = (bounds[1] - bounds[0]) * fraction
            return [value + delta for value in bounds]
        self.set_ranges(shifted(self.x_range, dx_fraction), shifted(self.y_range, dy_fraction), manual=True)
