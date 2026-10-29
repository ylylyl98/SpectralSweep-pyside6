"""Native Qt Quick numerical plots and narrow compatibility adapters.

QQuickPaintedItem paints numerical geometry directly at device resolution. It
does not embed a GraphicsView, capture a legacy plot, or transfer array lists
through QML. The small PlotWidget/ImageView adapters let existing controllers
retain their exact calculation, persistence and export paths during migration.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import uuid
from shiboken6 import isValid

import numpy as np
from PySide6.QtCore import QObject, QPointF, Property, QRectF, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPolygonF, QTransform
from PySide6.QtQml import qmlRegisterType
from PySide6.QtQuick import QQuickPaintedItem
from PySide6.QtQuickWidgets import QQuickWidget
from ui.quick_engine import QuickSurface
from PySide6.QtWidgets import QWidget, QVBoxLayout

from app.scientific_plot_model import ScientificPlotModel, envelope_indices, nice_ticks


class ScientificPlotItem(QQuickPaintedItem):
    modelChanged = Signal()
    colorsChanged = Signal()
    cursorChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = None
        self._colors = {"background": QColor("white"), "foreground": QColor("#202024"), "grid": QColor("#dedee3")}
        self.setAntialiasing(True)
        self._draw_cache = {}
        self._paint_rect = None

    @Property(QObject, notify=modelChanged)
    def plotModel(self):
        return self._model

    @plotModel.setter
    def plotModel(self, value):
        if self._model is value:
            return
        if self._model is not None and isValid(self._model):
            self._model.changed.disconnect(self._changed)
        self._model = value
        if value:
            value.changed.connect(self._changed)
        self._changed()
        self.modelChanged.emit()

    def _changed(self):
        self._draw_cache.clear()
        self.update()

    def _color_property(name, changed):
        def setter(self, value):
            if self._colors[name] != value:
                self._colors[name] = QColor(value)
                self.colorsChanged.emit()
                self.update()
        return Property(QColor, lambda self: self._colors[name], setter, notify=changed)

    backgroundColor = _color_property("background", colorsChanged)
    foregroundColor = _color_property("foreground", colorsChanged)
    gridColor = _color_property("grid", colorsChanged)

    def data_rect(self):
        # Font-relative margins scale on native high-DPI surfaces.
        font = QFont("Segoe UI", 9)
        metrics = QFontMetricsF(font)
        labels = [f"{10 ** v if self._model.log_y else v:.5g}" for v in nice_ticks(*self._model.y_range)] if self._model else ["0"]
        left = max(metrics.horizontalAdvance(text) for text in labels) + metrics.height() * 2.1
        ml, mt, mr, mb = self._model.extra_margins if self._model else (0, 0, 0, 0)
        return QRectF(left + ml, metrics.height() * (2.5 if self._model and self._model.energy_axis else .8) + mt,
                      max(1., self.width() - left - metrics.height() * 1.9 - ml - mr),
                      max(1., self.height() - metrics.height() * (5.8 if self._model and self._model.energy_axis else 4.1) - mt - mb))

    def map_point(self, x, y):
        model = self._model
        rect = self._paint_rect if self._paint_rect is not None else self.data_rect()
        if model.log_x:
            x = np.log10(x) if x > 0 else np.nan
        if model.log_y:
            y = np.log10(y) if y > 0 else np.nan
        return self.map_display_point(x, y, rect)

    def map_display_point(self, x, y, rect=None):
        model, rect = self._model, rect if rect is not None else self.data_rect()
        fx = (x - model.x_range[0]) / (model.x_range[1] - model.x_range[0])
        fy = (y - model.y_range[0]) / (model.y_range[1] - model.y_range[0])
        return QPointF(rect.right() - fx * rect.width() if model.reverse_x else rect.left() + fx * rect.width(),
                       rect.top() + fy * rect.height() if model.reverse_y else rect.bottom() - fy * rect.height())

    @Slot(float, float, float, bool, bool)
    def zoomAt(self, x, y, factor, horizontal=True, vertical=True):
        if not self._model:
            return
        rect = self.data_rect()
        if not rect.contains(x, y):
            return
        fx, fy = (x - rect.left()) / rect.width(), (rect.bottom() - y) / rect.height()
        self._model.zoom(factor, 1 - fx if self._model.reverse_x else fx,
                         1 - fy if self._model.reverse_y else fy, horizontal, vertical)

    @Slot(float, float)
    def panPixels(self, dx, dy):
        rect = self.data_rect()
        self._model.pan(dx / rect.width() * (1 if self._model.reverse_x else -1),
                        dy / rect.height() * (-1 if self._model.reverse_y else 1))

    @Slot(float, float, result=str)
    def coordinatesAt(self, x, y):
        if not self._model:
            return ""
        rect = self.data_rect()
        if not rect.contains(x, y):
            return ""
        fx, fy = (x - rect.left()) / rect.width(), (rect.bottom() - y) / rect.height()
        if self._model.reverse_x:
            fx = 1 - fx
        if self._model.reverse_y:
            fy = 1 - fy
        xr, yr = self._model.x_range, self._model.y_range
        x, y = xr[0] + fx * (xr[1] - xr[0]), yr[0] + fy * (yr[1] - yr[0])
        x = 10 ** x if self._model.log_x else x
        y = 10 ** y if self._model.log_y else y
        return f"x = {x:.7g}   y = {y:.7g}"

    def paint(self, painter):
        model = self._model
        painter.fillRect(self.boundingRect(), self._colors["background"])
        if model is None:
            return
        rect = self.data_rect()
        self._paint_rect = rect
        font = QFont("Segoe UI", 9)
        painter.setFont(font)
        metrics = QFontMetricsF(font)
        foreground, grid = self._colors["foreground"], self._colors["grid"]
        def visible_color(color):
            # Keep stored trace colors intact; lift their lightness only on a
            # dark display so reference identity stays recognizable and legible.
            result = QColor(color)
            if self._colors["background"].lightnessF() < .3 and result.lightnessF() < .64:
                hue, saturation, _lightness, alpha = result.getHslF()
                result.setHslF(hue, saturation, .64, alpha)
            return result
        def visible_pen(pen):
            result = QPen(pen)
            result.setColor(visible_color(result.color()))
            return result
        def visible_brush(brush):
            result = QBrush(brush)
            if result.style() == Qt.BrushStyle.SolidPattern:
                result.setColor(visible_color(result.color()))
            return result
        xticks = nice_ticks(*model.x_range, max(2, int(rect.width() / 100)))
        yticks = nice_ticks(*model.y_range, max(2, int(rect.height() / 55)))
        for value in xticks:
            px = self.map_display_point(value, model.y_range[0]).x()
            if model.grid:
                painter.setPen(QPen(grid, .7))
                painter.drawLine(QPointF(px, rect.top()), QPointF(px, rect.bottom()))
            painter.setPen(foreground)
            physical_value = 10 ** value if model.log_x else value
            painter.drawText(QRectF(px - 60, rect.bottom() + 4, 120, metrics.height() * 1.4), Qt.AlignmentFlag.AlignHCenter, f"{physical_value:.6g}")
            if model.energy_axis and value > 0:
                painter.drawText(QRectF(px - 60, rect.top() - metrics.height() * 1.4, 120, metrics.height()), Qt.AlignmentFlag.AlignHCenter, f"{1239.8419843320026 / value:.5g}")
        for value in yticks:
            py = self.map_display_point(model.x_range[0], value).y()
            if model.grid:
                painter.setPen(QPen(grid, .7))
                painter.drawLine(QPointF(rect.left(), py), QPointF(rect.right(), py))
            painter.setPen(foreground)
            painter.drawText(QRectF(metrics.height(), py - metrics.height() / 2, rect.left() - metrics.height() - 7, metrics.height()), Qt.AlignmentFlag.AlignRight, f"{10 ** value if model.log_y else value:.5g}")
        painter.setPen(foreground)
        painter.drawRect(rect)
        painter.drawText(QRectF(rect.left(), rect.bottom() + metrics.height() * 1.7, rect.width(), metrics.height() * 1.3), Qt.AlignmentFlag.AlignHCenter, model.x_label + (" · log scale" if model.log_x else ""))
        if model.energy_axis:
            painter.drawText(QRectF(rect.left(), 0, rect.width(), metrics.height()), Qt.AlignmentFlag.AlignHCenter, "Energy (eV)")
        painter.save()
        painter.translate(2, rect.bottom())
        painter.rotate(-90)
        painter.drawText(QRectF(0, 0, rect.height(), metrics.height()), Qt.AlignmentFlag.AlignHCenter, model.y_label + (" · log scale" if model.log_y else ""))
        painter.restore()
        painter.save()
        painter.setClipRect(rect)
        if model.image is not None and not model.raster.isNull():
            # Source row zero always maps to the rect's y0. Axis inversion is
            # explicit, and descending calibration keeps its signed rectangle.
            first = self.map_point(model.image_rect.x(), model.image_rect.y())
            last = self.map_point(model.image_rect.x() + model.image_rect.width(), model.image_rect.y() + model.image_rect.height())
            painter.save()
            painter.translate(first)
            painter.scale((last.x() - first.x()) / model.raster.width(), (last.y() - first.y()) / model.raster.height())
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            painter.drawImage(QPointF(0, 0), model.raster)
            painter.restore()
        for curve in sorted(model.curves.values(), key=lambda c: c.z):
            if not curve.visible:
                continue
            painter.setPen(visible_pen(curve.pen))
            painter.setBrush(visible_brush(curve.brush))
            displayed_x, displayed_y = model.display_arrays(curve.x, curve.y)
            for run_index, run in enumerate(curve.runs):
                if curve.symbol:
                    painter.setPen(visible_pen(curve.symbol_pen if curve.symbol_pen is not None else curve.pen))
                    painter.setBrush(visible_brush(curve.symbol_brush if curve.symbol_brush is not None else curve.brush))
                    # Symbols carry per-point meaning; do not decimate them.
                    for i in run:
                        point = self.map_point(curve.x[i], curve.y[i])
                        if not rect.adjusted(-curve.size, -curve.size, curve.size, curve.size).contains(point):
                            continue
                        radius = curve.size / 2
                        if curve.symbol == "x":
                            painter.drawLine(point + QPointF(-radius, -radius), point + QPointF(radius, radius))
                            painter.drawLine(point + QPointF(-radius, radius), point + QPointF(radius, -radius))
                        elif curve.symbol == "s":
                            painter.drawRect(QRectF(point.x() - radius, point.y() - radius, curve.size, curve.size))
                        else:
                            painter.drawEllipse(point, radius, radius)
                if curve.line_visible and curve.pen.style() != Qt.PenStyle.NoPen:
                    painter.setPen(visible_pen(curve.pen))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    cache_key = (id(curve), run_index, tuple(model.x_range), tuple(model.y_range),
                                 rect.width(), rect.height(), rect.left(), rect.top(), model.reverse_x, model.reverse_y)
                    points = self._draw_cache.get(cache_key)
                    if points is None:
                        indices = envelope_indices(displayed_x, displayed_y, run, model.x_range, max(1, int(rect.width())))
                        points = QPolygonF([self.map_point(curve.x[i], curve.y[i]) for i in indices])
                        self._draw_cache[cache_key] = points
                    if points.size() > 1:
                        painter.drawPolyline(points)
                    elif points.size():
                        painter.drawPoint(points[0])
        for annotation in model.annotations.values():
            if not annotation.get("visible", True):
                continue
            painter.save()
            painter.setPen(visible_pen(annotation.get("pen", foreground)))
            painter.setBrush(visible_brush(annotation.get("brush", Qt.BrushStyle.NoBrush)))
            if annotation["kind"] == "polygon":
                painter.drawPolygon(QPolygonF([self.map_point(x, y) for x, y in annotation["points"]]))
            elif annotation["kind"] == "path":
                painter.translate(self.map_point(*annotation["position"]))
                painter.drawPath(annotation["path"])
            elif annotation["kind"] == "text":
                point = self.map_point(*annotation["position"])
                painter.drawText(QRectF(point.x() - 50, point.y() - 12, 100, 24), Qt.AlignmentFlag.AlignCenter, annotation["text"])
            painter.restore()
        painter.restore()
        if model.legend:
            named = [c for c in model.curves.values() if c.name and c.visible]
            width = max([metrics.horizontalAdvance(c.name) + 48 for c in named] + [40])
            height = len(named) * metrics.height() * 1.4 + 12
            if model.legend_size is not None:
                width, height = model.legend_size
            offset_x, offset_y = model.legend_offset
            left = rect.left() + offset_x if offset_x >= 0 else rect.right() + offset_x - width
            top = rect.top() + offset_y if offset_y >= 0 else rect.bottom() + offset_y - height
            if named:
                painter.fillRect(QRectF(left, top, width, height), self._colors["background"])
                painter.setPen(grid)
                painter.drawRect(QRectF(left, top, width, height))
            row = 0
            for curve in model.curves.values():
                if curve.name and curve.visible:
                    y = top + 12 + row * metrics.height() * 1.4
                    painter.setPen(visible_pen(curve.pen))
                    painter.drawLine(QPointF(left + 6, y), QPointF(left + 31, y))
                    if curve.symbol:
                        painter.setPen(visible_pen(curve.symbol_pen if curve.symbol_pen is not None else curve.pen))
                        painter.setBrush(visible_brush(curve.symbol_brush if curve.symbol_brush is not None else curve.brush))
                        center = QPointF(left + 18, y)
                        if curve.symbol == "x":
                            painter.drawLine(center + QPointF(-4, -4), center + QPointF(4, 4))
                            painter.drawLine(center + QPointF(-4, 4), center + QPointF(4, -4))
                        elif curve.symbol == "s":
                            painter.drawRect(QRectF(center.x() - 4, center.y() - 4, 8, 8))
                        else:
                            painter.drawEllipse(center, 4, 4)
                    painter.setPen(foreground)
                    painter.drawText(QPointF(left + 38, y + 4), curve.name)
                    row += 1
        if not model.hasData and not any(a["kind"] == "text" and a.get("visible", True) and a.get("text") for a in model.annotations.values()):
            painter.setPen(foreground)
            painter.drawText(rect.adjusted(15, 15, -15, -15), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, model.empty_text)
        self._paint_rect = None


_REGISTERED = False


def register_scientific_plot():
    global _REGISTERED
    if not _REGISTERED:
        qmlRegisterType(ScientificPlotItem, "SpectralSweep.Scientific", 1, 0, "ScientificPlotItem")
        _REGISTERED = True


class QuickCurve:
    """Data-item adapter with immutable originals and familiar query methods."""
    def __init__(self, plot, x=(), y=(), **options):
        self.plot = plot
        self.key = uuid.uuid4().hex
        self.opts = {"pen": QPen(QColor("#1565c0"), 1.8), "connect": "finite", **options}
        self.setData(x, y)

    @property
    def xData(self):
        return self.plot.model.curves[self.key].x

    @property
    def yData(self):
        return self.plot.model.curves[self.key].y

    def setData(self, x=(), y=(), **options):
        if "connect" not in options and not isinstance(self.opts.get("connect", "finite"), str):
            # Existing SpectrumPlot publishes new arrays, then recomputes its
            # band-edge mask in _refresh. A mask from the previous acquisition
            # cannot describe a newly sized detector array.
            if len(self.opts["connect"]) not in {len(x), max(0, len(x) - 1)}:
                self.opts["connect"] = "finite"
        self.opts.update(options)
        opts = self.opts
        def pen(value):
            if value is None:
                return QPen(Qt.PenStyle.NoPen)
            return QPen(QColor(value)) if isinstance(value, str) else QPen(value)
        def brush(value):
            if value is None:
                return QBrush(Qt.BrushStyle.NoBrush)
            return QBrush(QColor(value)) if isinstance(value, str) else QBrush(value)
        style = {key: value for key, value in opts.items() if key in {"connect", "name", "visible", "z"}}
        style.update(pen=pen(opts.get("pen")), symbol=opts.get("symbol") or "",
                     size=float(opts.get("symbolSize", opts.get("size", 6))),
                     brush=brush(opts.get("brush")), symbol_pen=pen(opts.get("symbolPen", opts.get("pen"))),
                     symbol_brush=brush(opts.get("symbolBrush", opts.get("brush"))))
        self.plot.model.set_curve(self.key, x, y, **style)

    def clear(self):
        self.setData([], [])

    def setToolTip(self, text):
        self._tooltip = str(text)
        self.plot._refresh_tooltip()

    def toolTip(self):
        return getattr(self, "_tooltip", "")

    def getData(self):
        return self.xData, self.yData

    def setPen(self, pen):
        value = QPen(pen)
        self.opts["pen"] = value
        if self.plot.model.curves[self.key].pen == value:
            return
        self.plot.model.curves[self.key].pen = value
        self.plot.model.changed.emit()

    def setVisible(self, visible):
        if self.plot.model.curves[self.key].visible == bool(visible):
            return
        self.plot.model.curves[self.key].visible = bool(visible)
        self.plot.model.changed.emit()

    def isVisible(self):
        return self.plot.model.curves[self.key].visible

    def setZValue(self, value):
        if self.plot.model.curves[self.key].z == value:
            return
        self.plot.model.curves[self.key].z = value
        self.plot.model.changed.emit()

    def zValue(self):
        return self.plot.model.curves[self.key].z


class _Axis:
    def __init__(self, name=""):
        self.name = name
        self.labelText = "Energy" if name == "top" else ""
        self.labelUnits = "eV" if name == "top" else ""
        self.range = [0., 1.]

    def enableAutoSIPrefix(self, *_):
        pass  # Tick text always uses explicit scientific notation.

    def tickStrings(self, values, scale, spacing):
        if self.name == "top":
            return [f"{1239.8419843320026 / value:.6g}" if np.isfinite(value) and value > 0 else "" for value in values]
        return [f"{value:.6g}" for value in values]


class _ViewBox(QObject):
    sigRangeChangedManually = Signal(object)
    sigRangeChanged = Signal(object, object, object)
    sigYRangeChanged = Signal(object, object)
    sigResized = Signal()

    def __init__(self, plot):
        super().__init__(plot)
        self.plot = plot
        self._last_range = plot.viewRange()
        plot.model.rangeChanged.connect(self._range_changed)
        plot.model.manualRangeChanged.connect(lambda x, y: self.sigRangeChangedManually.emit([x, y]))

    def _range_changed(self):
        ranges = self.plot.viewRange()
        for name, axis in self.plot._axes.items():
            axis.range = list(ranges[0 if name in {"top", "bottom"} else 1])
        changed = [ranges[i] != self._last_range[i] for i in range(2)]
        self._last_range = ranges
        self.sigRangeChanged.emit(self, ranges, changed)
        if changed[1]:
            self.sigYRangeChanged.emit(self, ranges[1])

    def width(self):
        item = self.plot._quick_widget.rootObject() if self.plot._quick_widget is not None else None
        return item.findChild(ScientificPlotItem).data_rect().width() if item else self.plot.width() - 80

    def invertX(self, enabled=True):
        self.plot.model.reverse_x = bool(enabled)
        self.plot.model.changed.emit()

    def invertY(self, enabled=True):
        self.plot.model.reverse_y = bool(enabled)
        self.plot.model.changed.emit()

    def setAspectLocked(self, enabled=True):
        if enabled:
            raise NotImplementedError("Use calibrated independent scientific axes; aspect lock is not implemented")

    def getViewBox(self):
        return self

    def sceneBoundingRect(self):
        return self.plot.quick_widget.rootObject().findChild(ScientificPlotItem).data_rect()

    def mapViewToScene(self, point):
        return self.plot.quick_widget.rootObject().findChild(ScientificPlotItem).map_point(point.x(), point.y())

    def getState(self):
        return {"viewRange": self.plot.viewRange(), "xInverted": self.plot.model.reverse_x, "yInverted": self.plot.model.reverse_y}

    def setState(self, state):
        self.invertX(state.get("xInverted", False))
        self.invertY(state.get("yInverted", self.plot.model.reverse_y))
        ranges = state.get("viewRange")
        if ranges:
            self.plot.setRange(xRange=ranges[0], yRange=ranges[1])

    def __getattr__(self, name):
        if name in {"autoRange", "setXRange", "setYRange", "setRange", "setLabel", "showGrid", "viewRange"}:
            return getattr(self.plot, name)
        raise AttributeError(name)


class QuickScientificPlot(QWidget):
    """Reusable Quick view with a deliberately small PlotWidget adapter API."""
    def __init__(self, parent=None, **_options):
        super().__init__(parent)
        register_scientific_plot()
        self._quick_widget = None
        self._loading = False
        self._theme_mode = self.window().property("quickThemeMode") or self._application_theme()
        self._theme_explicit = False
        self._show_navigation_footer = True
        self.model = ScientificPlotModel(self)
        self.qml_warnings = []
        self._items = {}
        self._external_slots = {}
        self._axes = {name: _Axis(name) for name in ("left", "bottom", "top", "right")}
        self._viewbox = _ViewBox(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setMinimumSize(140, 100)

    @property
    def quick_widget(self):
        return self._ensure_loaded()

    def _ensure_loaded(self):
        # Constructing the first QQuickWidget can synchronously resend the
        # ancestor's show event while Qt creates its graphics infrastructure.
        if self._loading:
            return self._quick_widget
        if self._quick_widget is None:
            self._loading = True
            try:
                if not self._theme_explicit:
                    self._theme_mode = self._application_theme()
                widget = QuickSurface(self)
                self._quick_widget = widget
                self.model.setParent(widget)
                widget.setObjectName("QuickScientificPlotWidget")
                widget.setAccessibleName("Scientific plot; wheel to zoom, drag to pan, F to fit")
                widget.setToolTip(self.toolTip())
                widget.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
                widget.engine().warnings.connect(self._warnings)
                widget.setInitialProperties({"plotModel": self.model, "themeMode": self._theme_mode,
                                             "showNavigationFooter": self._show_navigation_footer})
                widget.setSource(QUrl.fromLocalFile(str(Path(__file__).with_name("qml") / "ScientificPlot.qml")))
                if widget.status() == QQuickWidget.Status.Error:
                    raise RuntimeError("\n".join(error.toString() for error in widget.errors()))
                renderer = widget.rootObject().findChild(ScientificPlotItem)
                renderer.widthChanged.connect(self._viewbox.sigResized.emit)
                renderer.heightChanged.connect(self._viewbox.sigResized.emit)
                self.layout().addWidget(widget)
                QTimer.singleShot(0, self, self._viewbox.sigResized.emit)
            finally:
                self._loading = False
        return self._quick_widget

    @staticmethod
    def _application_theme():
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        return (app.property("quickThemeMode") if app is not None else None) or "system"

    def _warnings(self, errors):
        self.qml_warnings.extend(error.toString() for error in errors)

    def showEvent(self, event):
        self._ensure_loaded()
        super().showEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._viewbox.sigResized.emit()

    def set_theme(self, mode):
        if mode not in {"system", "light", "dark"}:
            raise ValueError("Theme must be system, light or dark")
        self._theme_mode = mode
        self._theme_explicit = True
        if self._quick_widget is not None:
            self._quick_widget.rootObject().setProperty("themeMode", mode)

    def plot(self, x=(), y=(), **options):
        curve = QuickCurve(self, x, y, **options)
        self._items[curve.key] = curve
        return curve

    def set_curve(self, key, x, y, **options):
        return self.model.set_curve(key, x, y, **options)

    def set_labels(self, x, y):
        self.model.x_label, self.model.y_label = str(x), str(y)
        self.model.changed.emit()

    def _refresh_tooltip(self):
        text = "\n".join(item.toolTip() for item in self._items.values() if isinstance(item, QuickCurve) and item.toolTip())
        self.setToolTip(text)
        if self._quick_widget is not None:
            self._quick_widget.setToolTip(text)

    def setLabel(self, axis, text, units="", **_):
        self._axes[axis].labelText, self._axes[axis].labelUnits = str(text), str(units or "")
        label = f"{text} ({units})" if units else str(text)
        if axis == "bottom":
            self.model.x_label = label
        elif axis == "left":
            self.model.y_label = label
        self.model.changed.emit()

    def getAxis(self, name):
        return self._axes[name]

    def getViewBox(self):
        return self._viewbox

    def viewport(self):
        return self.quick_widget

    def getView(self):
        return self._viewbox

    def showAxis(self, axis, show=True):
        if axis == "top":
            self.model.energy_axis = bool(show)
            self.model.changed.emit()

    def showGrid(self, x=True, y=True, **_):
        self.model.grid = bool(x or y)
        self.model.changed.emit()

    def disableAutoRange(self):
        self.model.auto_x = self.model.auto_y = False

    def enableAutoRange(self, **_):
        self.model.fitAll()

    autoRange = enableAutoRange

    def setRange(self, *, xRange=None, yRange=None, padding=0):
        def padded(values):
            if values is None:
                return None
            lo, hi = map(float, values)
            if lo == hi:
                lo, hi = lo - .5, hi + .5
            pad = (hi - lo) * padding
            return [lo - pad, hi + pad]
        if xRange is not None:
            self.model.auto_x = False
        if yRange is not None:
            self.model.auto_y = False
        # Programmatic changes emit rangeChanged, but only pointer/keyboard
        # interactions emit sigRangeChangedManually (matching ViewBox).
        self.model.set_ranges(padded(xRange), padded(yRange))

    def setXRange(self, lo, hi, padding=0):
        self.setRange(xRange=(lo, hi), padding=padding)

    def setYRange(self, lo, hi, padding=0):
        self.setRange(yRange=(lo, hi), padding=padding)

    def viewRange(self):
        return [list(self.model.x_range), list(self.model.y_range)]

    def addLegend(self, size=None, offset=(8, 8)):
        self.model.legend = True
        self.model.legend_size = size
        self.model.legend_offset = (8, 8) if offset is None else tuple(offset)
        self.model.changed.emit()
        return self

    def setLogMode(self, x=False, y=False):
        self.model.set_log_mode(x, y)

    def getPlotItem(self):
        def set_margins(left, top, right, bottom):
            self.model.extra_margins = tuple(map(float, (left, top, right, bottom)))
            self.model.changed.emit()
        return SimpleNamespace(layout=SimpleNamespace(setContentsMargins=set_margins))

    def removeItem(self, item):
        key = item.key if isinstance(item, QuickCurve) else str(id(item))
        self._items.pop(key, None)
        if key in self._external_slots:
            for signal, slot in self._external_slots.pop(key):
                signal.disconnect(slot)
        self.model.remove_curve(key)
        self.model.annotations.pop(key, None)
        self.model.changed.emit()

    def addItem(self, item, ignoreBounds=False):
        """Read numerical pg data items and Qt geometry, never paint widgets.

        Only items used by the sweep preview are supported. Unsupported items
        raise immediately rather than disappearing from scientific displays.
        """
        key = str(id(item))
        self._items[key] = item
        if hasattr(item, "getData") and hasattr(item, "sigPlotChanged"):
            def update(*_):
                x, y = item.getData()
                options = item.opts
                is_scatter = type(item).__name__ == "ScatterPlotItem"
                self.model.set_curve(key, () if x is None else x, () if y is None else y,
                    pen=QPen(options.get("pen") or QPen(Qt.PenStyle.NoPen)),
                    brush=QBrush(options.get("brush") or QBrush(Qt.BrushStyle.NoBrush)),
                    symbol=str(options.get("symbol", "o")) if is_scatter else "",
                    size=float(options.get("size", 6)), connect=options.get("connect", "finite"), z=float(item.zValue()), line_visible=not is_scatter)
            item.sigPlotChanged.connect(update)
            self._external_slots[key] = [(item.sigPlotChanged, update)]
            update()
            return
        if hasattr(item, "polygon"):
            self.model.annotations[key] = {"kind": "polygon", "points": [(p.x(), p.y()) for p in item.polygon()], "pen": item.pen(), "brush": item.brush()}
        elif hasattr(item, "rect"):
            r = item.rect()
            self.model.annotations[key] = {"kind": "polygon", "points": [(r.left(), r.top()), (r.right(), r.top()), (r.right(), r.bottom()), (r.left(), r.bottom())], "pen": item.pen(), "brush": item.brush()}
        elif hasattr(item, "path"):
            path = item.path() if callable(item.path) else item.path
            self.model.annotations[key] = {"kind": "path", "position": (item.pos().x(), item.pos().y()), "path": path, "pen": item.pen(), "brush": item.brush()}
        elif hasattr(item, "textItem"):
            self.model.annotations[key] = {"kind": "text", "position": (item.pos().x(), item.pos().y()), "text": item.textItem.toPlainText(), "pen": item.textItem.defaultTextColor()}
            def update_text(*_):
                if key in self.model.annotations:
                    self.model.annotations[key].update(position=(item.pos().x(), item.pos().y()), text=item.textItem.toPlainText(), visible=item.isVisible())
                    self.model.changed.emit()
            bindings = []
            for signal in (item.textItem.document().contentsChanged, item.xChanged, item.yChanged, item.visibleChanged):
                signal.connect(update_text)
                bindings.append((signal, update_text))
            self._external_slots[key] = bindings
        else:
            self._items.pop(key)
            raise TypeError(f"Unsupported Quick scientific annotation: {type(item).__name__}")
        self.model.changed.emit()

    def clear(self):
        for item in list(self._items.values()):
            self.removeItem(item)

    def export_arrays(self):
        return self.model.export_arrays()

    def export_png(self, path):
        return self.quick_widget.grabFramebuffer().save(str(path), "PNG")


class QuickImageView(QuickScientificPlot):
    """ImageView boundary accepting legacy (columns, rows) data explicitly.

    New code should use set_image(rows, rect=...) to avoid transposition.
    ImageView historically inverts Y, so row zero appears at the top.
    """
    def __init__(self, parent=None, **options):
        super().__init__(parent, **options)
        self.view = self
        self.image = None
        self.model.reverse_y = True
        self.ui = SimpleNamespace(roiBtn=SimpleNamespace(hide=lambda: None), menuBtn=SimpleNamespace(hide=lambda: None))

    def set_image(self, rows, *, rect=None, auto_levels=True, auto_range=True):
        if auto_range:
            self.model.auto_x = self.model.auto_y = True
        self.model.set_image(rows, rect=rect, auto_levels=auto_levels, fit_ranges=auto_range)
        self.image = self.model.image.T

    def setImage(self, image, autoLevels=True, autoRange=True, **_):
        self.set_image(np.asarray(image).T, auto_levels=autoLevels, auto_range=autoRange)

    def getImageItem(self):
        return self

    def setRect(self, *args):
        rect = QRectF(args[0]) if len(args) == 1 else QRectF(*args)
        if not np.isfinite([rect.x(), rect.y(), rect.width(), rect.height()]).all() or not rect.width() or not rect.height():
            raise ValueError("Image calibration requires a finite nonzero rectangle")
        self.model.image_rect = rect
        self.model.changed.emit()
        if self.model.auto_x or self.model.auto_y:
            self.model.fit(self.model.auto_x, self.model.auto_y)

    def setLevels(self, lo, hi=None):
        if hi is None:
            lo, hi = lo
        self.model.set_levels(lo, hi)

    def getLevels(self):
        return tuple(self.model.image_levels)

    def setColorMap(self, color_map):
        self.model.color_map = color_map
        self.model.rebuild_raster()
        self.model.changed.emit()

    def clear(self):
        self.image = self.model.image = None
        self.model.rebuild_raster()
        self.model.changed.emit()


def quick_spectrum_plot_class():
    """Reuse validated spectrum band/range policy with a native Quick surface."""
    from ui.spectrum_plot import _SpectrumPlot

    class QuickSpectrumPlot(_SpectrumPlot):
        def __init__(self, parent=None):
            super().__init__(parent)
            # Scope migration to the inherited footer. Descending through the
            # plot would duplicate the numerical renderers and their sources.
            layout = self.layout()
            footer_item = layout.takeAt(layout.count() - 1)
            self._quick_footer = QWidget(self)
            self._quick_footer.setObjectName("SpectrumPlotFooter")
            self._quick_footer.setLayout(footer_item.layout())
            layout.addWidget(self._quick_footer)
            self._quick_footer_forms = None

        def showEvent(self, event):
            if self._quick_footer_forms is None:
                from ui.quick_parameter_forms import install_parameter_forms
                self._quick_footer_forms = install_parameter_forms(self._quick_footer, "spectrum")
            super().showEvent(event)

        def _make_plot(self):
            plot = QuickScientificPlot(self)
            plot._show_navigation_footer = False
            plot.disableAutoRange()
            plot.model.fit_handler = lambda: self.fit(self._range_mode)
            box = plot.getViewBox()
            box.sigRangeChangedManually.connect(self._manual_range)
            box.sigYRangeChanged.connect(self._sync_y_ranges)
            box.sigResized.connect(self._schedule_width_update)
            if not hasattr(self, "_plot"):
                box.sigRangeChanged.connect(self._range_changed)
            self._label_plot(plot)
            return plot

        def update_spectrum(self, wl, cts):
            # The inherited refresh publishes the live curve; avoid the
            # legacy initial setData followed by a second identical publish.
            self._wl = np.asarray(wl, dtype=float)
            self._cts = np.asarray(cts, dtype=float)
            self._refresh()
            self._update_info()

        def set_theme(self, mode):
            self._quick_theme = mode
            for plot in self.segment_plots:
                plot.set_theme(mode)
            if self._quick_footer_forms is not None:
                for form in self._quick_footer_forms["forms"]:
                    form.rootObject().setProperty("themeMode", mode)

        def _rebuild_segments(self, segments):
            super()._rebuild_segments(segments)
            for plot in self.segment_plots:
                if hasattr(self, "_quick_theme"):
                    plot.set_theme(self._quick_theme)

    return QuickSpectrumPlot
