"""Side-by-side spectra retain aligned data canvases and independent controls."""
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from tests.quick_helpers import drain_qt_deletions, quick_application
from ui.spectrum_pair import SpectrumPairDisplay


def _canvas_edges(display, plot):
    stack = plot._display_stack
    top = stack.mapTo(display, QPoint()).y()
    return top, top + stack.height()


def _data_edges(display, plot):
    top, _ = _canvas_edges(display, plot)
    bounds = plot._plot.getViewBox().sceneBoundingRect()
    return top + bounds.top(), top + bounds.bottom()


@pytest.mark.parametrize("quick", (False, True))
def test_side_by_side_aligns_split_panes_through_results_and_overlay(quick):
    app = quick_application()
    original_quick = app.property("spectralQuick")
    app.setProperty("spectralQuick", quick)
    display = SpectrumPairDisplay(orientation=Qt.Orientation.Horizontal, selectable_layout=True)
    display.resize(900, 550)
    display.show()
    backend = "andor_ingaas"
    pixis, andor = (plot for _, plot in display.plots(backend))
    try:
        min_width = display.minimumSizeHint().width()
        records = {
            "lightfield": {"wavelengths": [650, 680], "counts": [10, 30]},
            backend: {"wavelengths": [1000, 1300], "counts": [30000, 60000]},
        }
        path = Path("spectrum_pair_with_a_long_description_" * 5 + ".csv")
        paths = dict.fromkeys(records, path)
        display.show_result(backend, records, paths, Path("pair_manifest.csv"))
        QTest.qWait(75)
        assert display.minimumSizeHint().width() <= min_width

        for width in (900, 1200):
            display.resize(width, 550)
            display._splitter.setSizes([width // 3, 2 * width // 3])
            QTest.qWait(50)
            assert _canvas_edges(display, pixis) == _canvas_edges(display, andor)
            assert _data_edges(display, pixis) == pytest.approx(_data_edges(display, andor), abs=1)
            for plot in (pixis, andor):
                for control in (plot._fit_live_btn, plot._fit_visible_btn, plot._autoscale_chk):
                    assert control.isVisible() and control.isEnabled()
                    assert control.geometry().right() < plot.width()
            assert display._entries[1][1].toolTip() == f"Saved to {path.name}"

        QTest.mouseClick(andor._autoscale_chk, Qt.MouseButton.LeftButton)
        QTest.mouseClick(andor._fit_visible_btn, Qt.MouseButton.LeftButton)
        QTest.mouseClick(pixis._fit_live_btn, Qt.MouseButton.LeftButton)
        assert andor._range_mode == "visible" and pixis._range_mode == "live"
        assert not andor._autoscale_chk.isChecked() and pixis._autoscale_chk.isChecked()

        display.layout_choice.setCurrentText("Overlay")
        QTest.qWait(50)
        assert display.showing_overlay and not pixis.isVisible()
        display.layout_choice.setCurrentText("Side by side")
        QTest.qWait(50)
        assert pixis.isVisible() and not andor._autoscale_chk.isChecked()

        pixel_result = dict(records, **{backend: {**records[backend], "axis_unit": "pixel"}})
        display.show_result(backend, pixel_result, paths, None)
        display.layout_choice.setCurrentText("Overlay")
        QTest.qWait(50)
        assert not display.showing_overlay and andor.axis_unit == "pixel"
        assert _data_edges(display, pixis) == pytest.approx(_data_edges(display, andor), abs=1)

        partial = dict(records, **{backend: {"error": "Andor did not return data"}})
        display.layout_choice.setCurrentText("Side by side")
        display.show_result(backend, partial, {"lightfield": path}, None)
        QTest.qWait(50)
        assert _canvas_edges(display, pixis) == _canvas_edges(display, andor)
        assert andor._empty_label.isVisible()
        assert andor._empty_label.text() == partial[backend]["error"]
    finally:
        display.close()
        display.deleteLater()
        drain_qt_deletions()
        app.setProperty("spectralQuick", original_quick)
