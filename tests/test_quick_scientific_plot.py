"""Scientific coordinate, image orientation and native Quick interaction checks."""
import numpy as np
import pytest
from PySide6.QtCore import QObject, QPointF, QRectF, Qt
from PySide6.QtGui import QPen
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QTest

from app.scientific_plot_model import ScientificPlotModel, envelope_indices, finite_runs
from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.quick_scientific_plot import QuickImageView, QuickScientificPlot, ScientificPlotItem, quick_spectrum_plot_class


@pytest.fixture
def plot():
    quick_application()
    widget = QuickScientificPlot()
    widget.resize(800, 480)
    widget.show()
    QTest.qWait(30)
    yield widget
    widget.close()
    widget.deleteLater()
    drain_qt_deletions()


def test_original_arrays_are_independent_immutable_and_exported_at_full_resolution():
    model = ScientificPlotModel()
    x = np.arange(10000., dtype=float)
    y = np.sin(x)
    curve = model.set_curve("live", x, y)
    x[:] = -1
    y[:] = -2
    assert curve.x[123] == 123
    assert curve.y[123] == pytest.approx(np.sin(123))
    assert not curve.x.flags.writeable
    previous = curve.x
    model.set_curve("live", curve.x, curve.y)
    assert curve.x is previous  # Style/refresh does not duplicate numerical data.
    exported = model.export_arrays()["live"]
    assert len(exported[0]) == 10000
    exported[1][:] = 0
    assert curve.y[123] == pytest.approx(np.sin(123))


def test_hidden_views_keep_model_without_loading_qml():
    quick_application()
    view = QuickImageView()
    try:
        view.setImage(np.arange(12).reshape(4, 3))
        view.setLevels(1, 10)
        view.set_theme("dark")
        assert view._quick_widget is None
        assert view.getView().getViewBox() is view.getViewBox()
        assert view.getLevels() == (1, 10)
    finally:
        view.deleteLater()
        drain_qt_deletions()


def test_finite_runs_respect_nan_and_explicit_edges():
    x = np.arange(9.)
    y = np.array([1, 2, np.nan, 3, 4, 5, 6, np.inf, 7])
    connections = np.ones(9, bool)
    connections[4] = False
    assert [run.tolist() for run in finite_runs(x, y, connections)] == [[0, 1], [3, 4], [5, 6], [8]]
    with pytest.raises(ValueError):
        finite_runs(x, y, [True])


@pytest.mark.parametrize("descending", [False, True])
def test_million_point_envelope_keeps_peak_dip_edges_and_traversal(descending):
    x = np.linspace(650, 750, 1_000_000)
    y = np.zeros(x.size)
    y[123456], y[456789] = 1e9, -1e9
    if descending:
        x = x[::-1].copy()
    selected = envelope_indices(x, y, np.arange(x.size), [650, 750], 800)
    assert 123456 in selected and 456789 in selected
    assert selected[0] == 0 and selected[-1] == x.size - 1
    assert len(selected) <= 800 * 4
    assert np.all(np.diff(selected) > 0)


def test_nonmonotone_scientific_routes_are_not_decimated():
    x = np.tile([0., 1., 0.], 1000)
    y = np.arange(x.size, dtype=float)
    run = np.arange(x.size)
    np.testing.assert_array_equal(envelope_indices(x, y, run, [0, 1], 20), run)


def test_model_zoom_keeps_anchor_and_pauses_following():
    model = ScientificPlotModel()
    model.set_ranges([650, 750], [0, 100])
    model.zoom(.5, .25, .75, True, False)
    np.testing.assert_allclose(model.x_range, [662.5, 712.5])
    np.testing.assert_allclose(model.y_range, [0, 100])
    assert not model.auto_x and model.auto_y
    model.pan(.1, -.2)
    np.testing.assert_allclose(model.x_range, [667.5, 717.5])
    np.testing.assert_allclose(model.y_range, [-20, 80])
    assert not model.auto_y


def test_native_quick_surface_has_real_coordinate_zoom_keyboard_and_no_warnings(plot, tmp_path):
    plot.plot([650, 700, 750], [0, 10, 0])
    plot.set_labels("Wavelength (nm)", "Intensity (counts)")
    plot.setRange(xRange=[650, 750], yRange=[0, 10])
    renderer = plot.quick_widget.rootObject().findChild(ScientificPlotItem)
    rect = renderer.data_rect()
    center = rect.center()
    assert "700" in renderer.coordinatesAt(center.x(), center.y())
    renderer.zoomAt(center.x(), center.y(), .5, True, False)
    np.testing.assert_allclose(plot.viewRange()[0], [675, 725])
    np.testing.assert_allclose(plot.viewRange()[1], [0, 10])
    plot.quick_widget.setFocus()
    plot.quick_widget.rootObject().forceActiveFocus()
    QTest.keyClick(plot.quick_widget, Qt.Key.Key_F)
    QTest.qWait(20)
    assert plot.viewRange()[0][0] < 650 < 750 < plot.viewRange()[0][1]
    assert plot.quick_widget.status() == QQuickWidget.Status.Ready
    assert not plot.qml_warnings
    assert plot.export_png(tmp_path / "plot.png")
    assert (tmp_path / "plot.png").stat().st_size > 1000


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_image_rows_orientation_calibration_transparency_and_levels(mode):
    quick_application()
    view = QuickImageView()
    try:
        view.resize(700, 480)
        view.set_theme(mode)
        rows = np.array([[0., 1., 2.], [3., np.nan, 5.]])
        view.set_image(rows, rect=QRectF(650, 10, 30, 2))
        view.setRange(xRange=[650, 680], yRange=[10, 12])
        view.show()
        QTest.qWait(30)
        np.testing.assert_array_equal(view.model.image, rows)
        np.testing.assert_array_equal(view.image, rows.T)
        assert view.model.image_rect == QRectF(650, 10, 30, 2)
        renderer = view.quick_widget.rootObject().findChild(ScientificPlotItem)
        # Row zero is above row one under ImageView's inverted-Y convention.
        top = renderer.map_point(655, 10.5)
        bottom = renderer.map_point(655, 11.5)
        assert top.y() < bottom.y()
        image = view.quick_widget.grabFramebuffer()
        ratio = image.devicePixelRatio()
        sample = lambda p: image.pixelColor(round(p.x() * ratio), round(p.y() * ratio)).red()
        assert sample(top) < sample(bottom)
        assert view.model.raster.pixelColor(1, 1).alpha() == 0
        raw = view.model.image.copy()
        view.setLevels(1., 4.)
        np.testing.assert_array_equal(view.model.image, raw)
        assert view.model.raster.pixelColor(0, 0).red() == 0
        assert view.model.raster.pixelColor(2, 1).red() == 255
        assert not view.model.setImageLevels(5., 1.)
        assert not view.qml_warnings
    finally:
        view.close()
        view.deleteLater()
        drain_qt_deletions()


def test_signed_image_calibration_preserves_descending_axis():
    model = ScientificPlotModel()
    model.set_image([[0, 1], [2, 3]], rect=QRectF(750, 20, -100, -10))
    assert model.image_rect.width() == -100
    assert model.x_range[0] < 650 < 750 < model.x_range[1]
    assert model.y_range[0] < 10 < 20 < model.y_range[1]


@pytest.mark.parametrize("width", [180, 320, 700])
def test_image_level_controls_wrap_without_collapsing_or_horizontal_overflow(width):
    quick_application()
    view = QuickImageView()
    try:
        view.set_image(np.arange(12).reshape(3, 4))
        view.resize(width, 450)
        view.show()
        QTest.qWait(30)
        root = view.quick_widget.rootObject()
        for name in ("ScientificImageLevelMinimum", "ScientificImageLevelMaximum",
                     "ScientificImageApplyLevels", "ScientificImageAutoLevels"):
            item = root.findChild(QObject, name)
            assert item.width() >= 64, (name, item.width())
            left = item.mapToScene(QPointF(0, 0)).x()
            assert left >= 0 and left + item.width() <= root.width() + 1
        assert not view.qml_warnings
    finally:
        view.close()
        view.deleteLater()
        drain_qt_deletions()


def test_spectrum_bands_references_follow_and_manual_shared_y():
    quick_application()
    view = quick_spectrum_plot_class()()
    try:
        view.resize(1200, 600)
        wl = np.arange(1100., 1301.)
        view.update_spectrum(wl, np.ones(wl.size) * 10)
        view.set_reference("si", np.arange(650., 751.), np.ones(101) * 100, "#d13438")
        assert view._plot.viewRange()[0][0] > 1000
        view.set_comparison(True, True)
        view.show()
        QTest.qWait(30)
        assert len(view.segment_plots) == 2
        assert all(segment.layout().count() == 1 for segment in view.segment_plots)
        left, right = view.segment_plots
        assert left.viewRange()[0][1] < 800 and right.viewRange()[0][0] > 1000
        np.testing.assert_allclose(left.viewRange()[1], right.viewRange()[1])
        np.testing.assert_array_equal(view._reference_curves["si"].xData, np.arange(650., 751.))
        right.setYRange(2, 4)
        view.update_spectrum(wl, np.ones(wl.size) * 1000)
        for segment in view.segment_plots:
            np.testing.assert_allclose(segment.viewRange()[1], [2, 4])
        state = view.capture_view()
        view.fit("visible")
        view.restore_view(state)
        for segment in view.segment_plots:
            np.testing.assert_allclose(segment.viewRange()[1], [2, 4])
        # Native F/Fit must use the spectrum's shared band policy, rather than
        # fitting every reference into one segment's viewport.
        view.segment_plots[0].model.fitAll()
        assert len(view.segment_plots) == 2
        assert view.segment_plots[0].viewRange()[0][1] < 800
        assert view.segment_plots[1].viewRange()[0][0] > 1000
        view.set_axis_unit("pixel", reverse_pixels=True)
        assert len(view.segment_plots) == 1
        assert view._plot.model.reverse_x
        assert not view._plot.model.energy_axis
        assert not view._reference_curves["si"].isVisible()
    finally:
        view.close()
        view.deleteLater()
        drain_qt_deletions()


def test_native_sweep_layers_keep_geometry_style_and_progress_updates(plot):
    import pyqtgraph as pg
    from PySide6.QtWidgets import QGraphicsPolygonItem, QGraphicsRectItem
    from PySide6.QtGui import QPolygonF
    scatter = pg.ScatterPlotItem(x=[1, 2], y=[3, 4], symbol="x", pen=pg.mkPen("r"), brush=pg.mkBrush(None))
    plot.addItem(scatter)
    scatter.setData(x=[5, 6, 7], y=[8, 9, 10])
    curve = plot.model.curves[str(id(scatter))]
    np.testing.assert_array_equal(curve.x, [5, 6, 7])
    assert curve.symbol == "x"
    line = pg.PlotDataItem([1, 2, np.nan, 4], [2, 3, np.nan, 5], pen=pg.mkPen("r", style=Qt.PenStyle.DashLine))
    plot.addItem(line)
    assert plot.model.curves[str(id(line))].pen.style() == Qt.PenStyle.DashLine
    assert len(plot.model.curves[str(id(line))].runs) == 2
    boundary = QGraphicsRectItem(-1, -2, 4, 6)
    plot.addItem(boundary)
    polygon = QGraphicsPolygonItem(QPolygonF([QPointF(0, 0), QPointF(2, 1), QPointF(1, 3)]))
    plot.addItem(polygon)
    arrow = pg.ArrowItem(pos=(2, 4), angle=90)
    plot.addItem(arrow)
    label = pg.TextItem("omitted stripes")
    label.setPos(2, 3)
    plot.addItem(label)
    assert plot.model.annotations[str(id(boundary))]["points"] == [(-1, -2), (3, -2), (3, 4), (-1, 4)]
    assert plot.model.annotations[str(id(arrow))]["position"] == (2, 4)
    assert plot.model.annotations[str(id(label))]["text"] == "omitted stripes"
    label.setText("Updated acquisition status")
    label.setPos(4, 6)
    assert plot.model.annotations[str(id(label))]["text"] == "Updated acquisition status"
    assert plot.model.annotations[str(id(label))]["position"] == (4, 6)
    plot.removeItem(label)
    plot.removeItem(scatter)
    scatter.setData([100], [100])
    assert str(id(scatter)) not in plot.model.curves
    with pytest.raises(TypeError):
        plot.addItem(object())


def test_power_calibration_markers_legend_and_log_coordinates_preserve_raw_data(plot):
    curve = plot.plot([1, 2, 3, 4], [1, 10, 0, 1000], pen=QPen(Qt.PenStyle.NoPen), symbol="x",
                      symbolBrush="#e31a1c", symbolPen="#e31a1c", symbolSize=10, name="Planned points")
    plot.addLegend(offset=(8, 8), size=(200, 40))
    plot.setLogMode(False, True)
    state = plot.model.curves[curve.key]
    assert state.symbol == "x" and state.size == 10
    assert state.symbol_pen.color().name() == "#e31a1c"
    assert plot.model.legend_offset == (8, 8) and plot.model.legend_size == (200, 40)
    assert [run.tolist() for run in state.runs] == [[0, 1], [3]]
    np.testing.assert_array_equal(curve.yData, [1, 10, 0, 1000])
    plot.setRange(xRange=[1, 4], yRange=[0, 3])
    renderer = plot.quick_widget.rootObject().findChild(ScientificPlotItem)
    point = renderer.map_point(2, 10)
    assert "y = 10" in renderer.coordinatesAt(point.x(), point.y())
    assert renderer.map_point(2, 10).y() > renderer.map_point(2, 100).y()
    plot.setLogMode(False, False)
    np.testing.assert_array_equal(curve.yData, [1, 10, 0, 1000])
    curve.clear()
    assert not curve.xData.size
