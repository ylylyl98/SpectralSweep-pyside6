"""Station 2100 manual paired acquisition through both Spectrum presentations."""
import csv
import json
from copy import deepcopy
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QDeadlineTimer, QPoint, Qt, Signal
from PySide6.QtGui import QCloseEvent, QFont, QFontMetricsF
from PySide6.QtTest import QTest

from tests.quick_helpers import drain_qt_deletions, quick_application
from tests.test_quick_parameter_forms import find_item
from ui.bfp_panel_integrated import BFPPanel
from ui.main_window import MainWindow
from ui.plot_factory import create_spectrum_plot
from ui.quick_controls import install_spectrum_controls, install_workflow_controls
from ui.quick_parameter_forms import install_parameter_forms
from ui.quick_reference_list import install_reference_list
from ui.quick_workspace_layouts import configure_workspace_layout
from ui.spectrum_panel import SpectrumPanel
from ui.spectrum_preview import PreviewSpectrumController
from ui.spectrum_reload_state import capture_spectrum_state, restore_spectrum_state
from utils.config import cfg


class PairPreview(PreviewSpectrumController):
    station_changed = Signal(str)
    connected_backends = ("lightfield", "andor_ingaas")

    def __init__(self):
        super().__init__()
        self.release_pair = Event()
        self.requests = []

    def acquire_station_pair(self, backend, recipes, stop):
        self.requests.append((backend, recipes, stop))
        if not self.release_pair.wait(5):
            raise RuntimeError("Test did not release pair")
        pixis = {"wavelengths": [500., 502.], "counts": [16., 19.],
                 "axis_unit": "nm", "settings": recipes["lightfield"]}
        andor = {"wavelengths": [0., 1., 2.], "counts": [4., 3., 1.],
                 "axis_unit": "pixel", "settings": recipes[backend]}
        if stop.is_set():
            andor = {"error": "Andor stopped", "settings": recipes[backend]}
        return {"lightfield": pixis, backend: andor}


class BinnedPairPreview(PairPreview):
    connected_backends = ("lightfield", "andor_si")

    def __init__(self):
        super().__init__()
        self.release_pair.set()
        self.lease = []

    def acquire_station_pair(self, backend, recipes, stop, *, owner):
        assert owner == "bfp"
        result = super().acquire_station_pair(backend, recipes, stop)
        for record in result.values():
            record["counts"] = np.asarray(record["counts"]) * len(self.requests)
        return result

    def set_temperature_monitor_paused(self, source, paused):
        self.lease.append((source, paused))


class RunningPairPreview(PairPreview):
    def __init__(self):
        super().__init__()
        self.points = (Event(), Event())
        self.lease = []

    def acquire_station_pair(self, backend, recipes, stop, *, owner):
        assert owner == "spectrum"
        self.requests.append((backend, recipes, stop))
        index = len(self.requests) - 1
        if not self.points[index].wait(timeout=5):
            raise RuntimeError("Test did not release pair point")
        pixis = {"wavelengths": [500., 502.], "counts": [10., float(index)],
                 "settings": recipes["lightfield"]}
        andor = {"wavelengths": [601., 602.], "counts": [4., 3.], "settings": recipes[backend]}
        if index == 1:
            andor = {"error": "Andor stopped" if stop.is_set() else "Andor acquisition failed",
                     "settings": recipes[backend]}
        return {"lightfield": pixis, backend: andor}

    def set_temperature_monitor_paused(self, source, paused):
        self.lease.append((source, paused))


@pytest.mark.parametrize("quick", (False, True))
def test_spectrograph_centers_can_be_applied_without_exposure(quick, tmp_path, monkeypatch):
    app = quick_application()
    old_quick = app.property("spectralQuick")
    app.setProperty("spectralQuick", quick)
    monkeypatch.setattr(cfg.lf6, "station_id", "attodry2100")
    monkeypatch.setattr(cfg.filename, "base_out", str(tmp_path))
    controller = PairPreview()
    center_requests = []
    def apply_centers(backend, centers, stop):
        center_requests.append((backend, centers))
        if not controller.release_pair.wait(5):
            raise RuntimeError("Timed out waiting for simulated spectrograph")
        return {key: {"center_nm": val} for key, val in centers.items()}
    controller.apply_station_pair_centers = apply_centers
    panel = SpectrumPanel(controller)
    forms = []
    command = None
    try:
        if quick:
            command = install_spectrum_controls(panel)
            configure_workspace_layout(panel, "spectrum")
            forms = install_parameter_forms(panel, "spectrum")["forms"]
        panel.resize(1300, 900)
        panel.show()
        controller.set_connected(True)
        panel._pair_controls.mode.setCurrentIndex(1)
        editors = (panel._pair_controls._pixis_editors[0], panel._pair_controls._andor_editors[0])
        apply = panel._apply_pair_centers_btn
        if quick:
            fields = {source: (form, field) for form in forms for field, source in form.adapter._fields.items()}
            for source, value in zip(editors, ("710.5", "1130")):
                form, field = fields[source]
                _wait_until(lambda: field.visible and form.isVisibleTo(panel))
                assert field.commit(value)
            form, field = fields[apply]
            _wait_until(lambda: field.visible and field.enabled and form.isVisibleTo(panel))
            assert field.trigger()
        else:
            for source, value in zip(editors, (710.5, 1130)):
                assert source.isVisibleTo(panel)
                source.setValue(value)
            QTest.mouseClick(apply, Qt.MouseButton.LeftButton)
        _wait_until(lambda: bool(center_requests))
        assert center_requests == [("andor_ingaas", {"lightfield": 710.5, "andor_ingaas": 1130})]
        assert panel.paired_acquisition_pending and not apply.isEnabled()
        assert not panel._acquire_btn.isEnabled()
        controller.release_pair.set()
        _wait_until(lambda: not panel.paired_acquisition_pending)
        assert panel._status_lbl.text() == "PIXIS 710.5 nm · Andor InGaAs 1130 nm"
        assert not controller.requests and not list(tmp_path.rglob("*.csv"))
        panel._pair_controls.mode.setCurrentIndex(0)
        assert not apply.isVisibleTo(panel)
        if quick:
            form, field = fields[panel._center]
            _wait_until(lambda: form.isVisibleTo(panel) and field.visible and field.enabled)
        else:
            assert panel._center.isVisibleTo(panel) and panel._center.isEnabled()
    finally:
        controller.release_pair.set()
        _wait_until(lambda: not panel.paired_acquisition_pending)
        if command is not None:
            command.shutdown()
        for form in forms:
            form.shutdown()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()
        app.setProperty("spectralQuick", old_quick)


def _wait_until(condition):
    deadline = QDeadlineTimer(5000)
    while not condition() and not deadline.hasExpired():
        QTest.qWait(10)
    assert condition()


@pytest.mark.parametrize("quick", (False, True))
def test_overlay_uses_both_native_axes_and_switches_without_reacquisition(quick, monkeypatch):
    app = quick_application()
    old_quick = app.property("spectralQuick")
    app.setProperty("spectralQuick", quick)
    monkeypatch.setattr(cfg.lf6, "station_id", "attodry2100")
    controller = PairPreview()
    panel = SpectrumPanel(controller)
    try:
        panel._pair_controls.mode.setCurrentIndex(1)
        panel.resize(1300, 880)
        panel.show()
        display = panel._pair_display
        records = {"lightfield": {"wavelengths": [700, 701, 702], "counts": [1, 4, 2]},
                   "andor_ingaas": {"wavelengths": [1010, 1014], "counts": [16, 32]}}
        display.show_result("andor_ingaas", records, {}, None)
        split_plot = display._entries[0][2]
        split_plot._autoscale_chk.setChecked(False)
        assert display.layout_choice.currentText() == "Side by side"
        display.layout_choice.setCurrentText("Overlay")
        overlay = display.overlay_plot
        assert overlay.isVisibleTo(panel)
        assert not display._splitter.isVisibleTo(panel)
        np.testing.assert_array_equal(overlay._wl, [700, 701, 702])
        secondary = overlay._reference_curves[overlay._secondary_live_id]
        np.testing.assert_array_equal(secondary.xData, [1010, 1014])
        np.testing.assert_array_equal(secondary.yData, [16, 32])
        overlay._autoscale_chk.setChecked(False)
        overlay.fit("live")
        assert display.fold_gaps.isVisibleTo(panel) and display.fold_gaps.isChecked()
        assert overlay._segments == [(700, 702), (1010, 1014)]
        marker = overlay._break_widgets[0]
        assert marker.text() == "//"
        assert "702–1010 nm" in marker.toolTip() and marker.accessibleName() == marker.toolTip()
        first, second = overlay.segment_plots
        assert first.viewRange()[0][1] < 1010 and second.viewRange()[0][0] > 702
        np.testing.assert_allclose(first.viewRange()[1], second.viewRange()[1])
        assert second.viewRange()[1][1] >= 32
        display.fold_gaps.click()
        assert overlay._segments == [(700, 1014)]
        display.fold_gaps.click()
        assert not overlay._autoscale_chk.isChecked()
        bridge = panel.add_reference([702, 800, 950, 1010], [0]*4, auto_save=False, reference_backend="lightfield")
        assert overlay._segments == [(700, 1014)]
        panel.remove_reference(bridge["id"])
        assert overlay._segments == [(700, 702), (1010, 1014)]
        ref = panel.add_reference([900, 905], [1, 2], name="Andor ref", auto_save=False,
                                  reference_backend="andor_ingaas")
        assert overlay._reference_curves[ref["id"]].isVisible()
        panel.remove_reference(ref["id"])
        assert ref["id"] not in overlay._reference_curves
        display.layout_choice.setCurrentIndex(0)
        assert display._splitter.isVisibleTo(panel) and not split_plot._autoscale_chk.isChecked()
        display.layout_choice.setCurrentIndex(1)
        records["andor_ingaas"] = {"axis_unit": "pixel", "wavelengths": [0, 1], "counts": [5, 6]}
        display.show_result("andor_ingaas", records, {}, None)
        assert not display.showing_overlay and display._splitter.isVisibleTo(panel)
        assert "pixels" in display._layout_note.text()
        assert not display.fold_gaps.isVisibleTo(panel)
        assert not controller.requests
    finally:
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()
        app.setProperty("spectralQuick", old_quick)


@pytest.mark.parametrize("quick", (False, True))
def test_folded_axis_compacts_800_to_950_gap_without_distorting_scale(quick):
    app = quick_application()
    old_quick = app.property("spectralQuick")
    app.setProperty("spectralQuick", quick)
    plot = create_spectrum_plot()
    try:
        plot.set_fold_overlay_gaps(True)
        plot.update_spectrum(np.linspace(650, 800, 31), np.linspace(1, 3, 31))
        plot.update_secondary_spectrum(np.linspace(950, 1050, 21), np.linspace(3, 1, 21), "red")
        plot.show()
        plot.fit("live")
        for width in (660, 940):
            plot.resize(width, 440)
            plot.grab()
            left, right = plot.segment_plots
            def widths_match_span():
                left_width, right_width = (
                    segment.getViewBox().sceneBoundingRect().width() for segment in (left, right)
                )
                return right_width > 0 and np.isclose(left_width / right_width, 1.5, rtol=.04)
            _wait_until(widths_match_span)
            assert plot._segments == [(650, 800), (950, 1050)]
            left_box, right_box = (segment.getViewBox().sceneBoundingRect() for segment in (left, right))
            left_edge = left.mapTo(plot, QPoint()).x() + left_box.right()
            right_edge = right.mapTo(plot, QPoint()).x() + right_box.left()
            metrics = QFontMetricsF(plot.font())
            marker = plot._break_widgets[0]
            assert marker.width() < metrics.horizontalAdvance("800–950 nm")
            assert 0 < right_edge - left_edge < marker.width() + metrics.height() * 2
            assert "800–950 nm" in marker.toolTip()
            assert np.isclose(left_box.width() / right_box.width(), 1.5, rtol=.04)
            assert left.viewRange()[0][1] < right.viewRange()[0][0]
            assert left.viewRange()[1] == right.viewRange()[1]
            if quick:
                from ui.quick_scientific_plot import ScientificPlotItem
                tick_metrics = QFontMetricsF(QFont("Segoe UI", 9))
                left_renderer, right_renderer = (
                    segment.quick_widget.rootObject().findChild(ScientificPlotItem)
                    for segment in (left, right)
                )
                assert left_renderer._horizontal_tick_rect(
                    "1.54980", left_box.right(), 0, 16, tick_metrics).right() <= left_renderer.width()
                assert right_renderer._horizontal_tick_rect(
                    "1.30510", right_box.left(), 0, 16, tick_metrics).left() >= 0
        plot.set_fold_overlay_gaps(False)
        QTest.qWait(30)
        assert not plot._break_widgets
        if quick:
            assert plot._plot.model.break_edges == (False, False)
    finally:
        plot.close()
        plot.deleteLater()
        drain_qt_deletions()
        app.setProperty("spectralQuick", old_quick)


@pytest.mark.parametrize("panel_name", ("_spectrum", "_bfp"))
def test_main_window_waits_until_paired_spectra_are_saved(panel_name):
    messages = []
    window = SimpleNamespace(_status=SimpleNamespace(showMessage=lambda message, _: messages.append(message)))
    setattr(window, panel_name, SimpleNamespace(paired_acquisition_pending=True))
    event = QCloseEvent()
    MainWindow.closeEvent(window, event)
    assert not event.isAccepted()
    assert "finish and save" in messages[0]


@pytest.mark.parametrize("quick", (False, True))
def test_spectrum_pair_keeps_separate_recipes_plots_and_files(quick, tmp_path, monkeypatch):
    app = quick_application()
    old_quick = app.property("spectralQuick")
    app.setProperty("spectralQuick", quick)
    monkeypatch.setattr(cfg.lf6, "station_id", "default", raising=False)
    monkeypatch.setattr(cfg.filename, "base_out", str(tmp_path))
    monkeypatch.setattr(cfg.session, "sample_id", "pairtest")
    controller = PairPreview()
    panel = SpectrumPanel(controller)
    command = forms = reference_view = None
    try:
        if quick:
            command = install_spectrum_controls(panel)
            configure_workspace_layout(panel, "spectrum")
            reference_view = install_reference_list(panel)
            forms = install_parameter_forms(panel, "spectrum")["forms"]
        panel.resize(1300, 900)
        panel.show()
        controller.set_connected(True)
        controls = panel._pair_controls
        assert controls.isHidden()
        controller.station_changed.emit("attodry2100")
        assert controls.isVisibleTo(panel)
        assert not controls.andor_backend
        assert "PIXIS" in controls._active_detector.text()
        if quick:
            form = next(form for form in forms if controls.mode in form.adapter._fields.values())
            _wait_until(lambda: form.rootObject() is not None)
            mode = next(field for field, source in form.adapter._fields.items() if source is controls.mode)
            assert mode.visible and mode.select(1)
        else:
            controls.mode.setCurrentIndex(1)
        assert controls.andor_backend == "andor_ingaas"
        controls._pixis_editors[0].setValue(725)
        controls._andor_editors[0].setValue(0)
        controls._pixis_editors[1].setValue(50)
        controls._andor_editors[1].setValue(80)
        controls._pixis_editors[2].setValue(2)
        controls._andor_editors[2].setValue(3)
        assert not panel._acquire_2d_btn.isEnabled()
        if quick:
            command.adapter.refresh()
            QTest.qWait(30)
            acquire = find_item(command.rootObject(), "command_acquire")
            _wait_until(lambda: acquire.property("text") == "Acquire pair" and acquire.isEnabled())
            point = acquire.mapToScene(acquire.boundingRect().center()).toPoint()
            QTest.mouseClick(command, Qt.MouseButton.LeftButton, pos=point)
        else:
            panel._acquire_btn.click()
        _wait_until(lambda: bool(controller.requests))
        assert panel.paired_acquisition_pending
        assert not controls.mode.isEnabled()
        backend, recipes, stop = controller.requests[0]
        assert backend == "andor_ingaas"
        assert recipes == {
            "lightfield": {"center_nm": 725., "exposure_ms": 50., "frames": 2},
            "andor_ingaas": {"center_nm": 0., "exposure_ms": 80., "frames": 3},
        }
        if not quick:
            panel._abort_btn.click()
            assert stop.is_set() and panel.paired_acquisition_pending
        controller.release_pair.set()
        _wait_until(lambda: not panel.paired_acquisition_pending)
        _, records, paths, manifest = panel._pair_result
        assert bool(records["andor_ingaas"].get("error")) is not quick
        assert len(paths) == (2 if quick else 1)
        point = json.loads(manifest.read_text(encoding="utf-8").splitlines()[-1])
        assert point["status"] == ("complete" if quick else "incomplete")
        for path in paths.values():
            with path.open(newline="", encoding="utf-8") as stream:
                row = next(csv.DictReader(stream))
            assert row["run_id"] == point["run_id"]
            assert row["point_id"] == point["point_id"] == "1"
        display = panel._pair_display
        np.testing.assert_array_equal(display._entries[0][2]._wl, [500., 502.])
        if quick:
            assert display._entries[1][2].axis_unit == "pixel"
            np.testing.assert_array_equal(display._entries[1][2]._wl, [0., 1., 2.])
        assert controls.mode.isEnabled()

        def click_source(source):
            if not quick:
                QTest.mouseClick(source, Qt.MouseButton.LeftButton)
                return
            form = next(view for view in forms if source in view.adapter._fields.values())
            field = next(field for field, widget in form.adapter._fields.items() if widget is source)
            _wait_until(lambda: field.visible and field.enabled and form.rootObject() is not None)
            item = find_item(form.rootObject(), "parameter_" + field.fieldId)
            point = item.mapToScene(item.boundingRect().center()).toPoint()
            QTest.mouseClick(form, Qt.MouseButton.LeftButton, pos=point)

        assert panel._side_panel.isVisibleTo(panel)
        assert panel._pair_reference_target.currentData() == "lightfield"
        click_source(panel._add_spectrum_btn)
        assert len(panel.references) == 1
        kept = panel.references[0]
        assert kept["setup_key"] == "lightfield"
        pixis, andor = (entry[2] for entry in panel._pair_display._entries)
        assert kept["id"] in pixis._reference_curves and kept["id"] not in andor._reference_curves
        panel._pair_reference_target.setCurrentIndex(1)
        assert panel._reference_list.item(0).isHidden()
        imported = tmp_path / "andor_reference.csv"
        imported.write_text("pixel,intensity_counts\n0,5\n1,7\n")
        monkeypatch.setattr("ui.spectrum_panel.QFileDialog.getOpenFileNames", lambda *a: ([str(imported)], ""))
        click_source(panel._load_spectra_btn)
        assert len(panel.references) == 2
        loaded = panel.references[-1]
        assert loaded["reference_backend"] == "andor_ingaas"
        assert loaded["id"] not in pixis._reference_curves and loaded["id"] in andor._reference_curves
        fitted = []
        monkeypatch.setattr(andor, "fit", fitted.append)
        click_source(panel._pair_fit_btn)
        assert fitted == ["visible"]
        if quick:
            reference_view.model.refresh()
            QTest.qWait(30)
            checkbox = find_item(reference_view.rootObject(), "referenceVisibility_" + loaded["id"])
            point = checkbox.mapToScene(checkbox.boundingRect().center()).toPoint()
            QTest.mouseClick(reference_view, Qt.MouseButton.LeftButton, pos=point)
        else:
            QTest.mouseClick(loaded["_row_checkbox"], Qt.MouseButton.LeftButton)
        assert not loaded["visible"] and not andor._reference_curves[loaded["id"]].isVisible()
        panel._reference_list.setCurrentRow(1)
        remove = next(action for action, source in panel._reference_menu_actions
                      if source is panel._remove_reference_btn)
        remove.trigger()
        assert panel.references == [kept]
        assert loaded["id"] not in andor._reference_curves

        if quick:
            andor._autoscale_chk.setChecked(False)
            restored = SpectrumPanel(controller, bind_controller=False)
            try:
                restored._pair_controls.set_station("attodry2100")
                restore_spectrum_state(restored, capture_spectrum_state(panel))
                assert restored._pair_reference_target.currentData() == "andor_ingaas"
                restored_pixis, restored_andor = (entry[2] for entry in restored._pair_display._entries)
                assert restored_pixis._autoscale_chk.isChecked()
                assert not restored_andor._autoscale_chk.isChecked()
            finally:
                restored.close()
                restored.deleteLater()

        controller.station_changed.emit("default")
        assert controls.isHidden()
        assert panel._acquire_btn.text() == "Acquire 1D"
        assert panel._acquire_2d_btn.isEnabled()
        assert panel._side_panel.isVisibleTo(panel)
        assert not panel._reference_list.item(0).isHidden()
    finally:
        controller.release_pair.set()
        _wait_until(lambda: not panel.paired_acquisition_pending)
        if command:
            command.shutdown()
        if reference_view is not None:
            reference_view.shutdown()
        for form in forms or []:
            form.shutdown()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()
        app.setProperty("spectralQuick", old_quick)


@pytest.mark.parametrize("quick", (False, True))
def test_bfp_dual_binned_repeat_average_stays_per_detector(quick, tmp_path, monkeypatch):
    app = quick_application()
    old_quick = app.property("spectralQuick")
    app.setProperty("spectralQuick", quick)
    monkeypatch.setattr(cfg.lf6, "station_id", "attodry2100", raising=False)
    monkeypatch.setattr(cfg.filename, "base_out", str(tmp_path))
    monkeypatch.setattr(cfg, "bfp_naming", deepcopy(cfg.bfp_naming))
    controller = BinnedPairPreview()
    panel = BFPPanel(controller)
    command = forms = None
    try:
        if quick:
            command = install_workflow_controls(panel, "BFP acquisition", {"acquire": panel._acquire_btn})
            configure_workspace_layout(panel, "bfp")
            forms = install_parameter_forms(panel, "bfp")["forms"]
        panel.resize(1400, 950)
        panel.show()
        controller.set_connected(True)
        panel._dev_edit.setText("pairtest")
        panel._repeat_spin.setValue(2)
        assert panel._roi_combo.currentText() == "Full sensor"
        controls = panel._pair_controls
        assert not controls.andor_backend
        controls.mode.setCurrentIndex(1)
        assert controls.andor_backend == "andor_ingaas" and not panel._acquire_btn.isEnabled()
        controls.andor_detector.setCurrentIndex(1)
        assert controls.andor_backend == "andor_si" and panel._acquire_btn.isEnabled()
        assert panel._roi_combo.isHidden()
        if quick:
            acquire = find_item(command.rootObject(), "command_acquire")
            _wait_until(lambda: acquire.property("text") == "Acquire pair" and acquire.isEnabled())
            point = acquire.mapToScene(acquire.boundingRect().center()).toPoint()
            QTest.mouseClick(command, Qt.MouseButton.LeftButton, pos=point)
        else:
            panel._acquire_btn.click()
        _wait_until(lambda: panel._pair_result is not None)
        assert len(controller.requests) == 2
        assert controller.lease == [("bfp", True), ("bfp", False)]
        _, records, paths, manifest = panel._pair_result
        assert set(paths) == {"lightfield", "andor_si"}
        assert manifest.parent == tmp_path / "pairtest" / "bfp"
        np.testing.assert_array_equal(records["lightfield"]["counts"], [24., 28.5])
        np.testing.assert_array_equal(records["andor_si"]["counts"], [6., 4.5, 1.5])
        assert records["lightfield"]["settings"]["repeats_averaged"] == 2
        assert records["andor_si"]["settings"]["repeats_requested"] == 2
        controls.mode.setCurrentIndex(0)
        assert panel._roi_combo.currentText() == "Full sensor" and not panel._roi_combo.isHidden()
    finally:
        _wait_until(lambda: not panel.paired_acquisition_pending)
        if command:
            command.shutdown()
        for form in forms or []:
            form.shutdown()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()
        app.setProperty("spectralQuick", old_quick)


@pytest.mark.parametrize("quick", (False, True))
def test_spectrum_pair_run_saves_points_until_stop_or_partial(quick, tmp_path, monkeypatch):
    app = quick_application()
    old_quick = app.property("spectralQuick")
    app.setProperty("spectralQuick", quick)
    monkeypatch.setattr(cfg.lf6, "station_id", "attodry2100", raising=False)
    monkeypatch.setattr(cfg.filename, "base_out", str(tmp_path))
    monkeypatch.setattr(cfg.session, "sample_id", "pair-run")
    preflights = []
    def preflight(_panel, controller, **options):
        preflights.append((controller, options))
        return True
    monkeypatch.setattr("ui.sensor_readout_preflight.ensure_sensor_readout", preflight)
    controller = RunningPairPreview()
    panel = SpectrumPanel(controller)
    command = forms = None

    def click(action, source):
        if not quick:
            QTest.mouseClick(source, Qt.MouseButton.LeftButton)
            return
        command.adapter.refresh()
        QTest.qWait(30)
        item = find_item(command.rootObject(), "command_" + action)
        assert item.isVisible() and item.isEnabled()
        point = item.mapToScene(item.boundingRect().center()).toPoint()
        QTest.mouseClick(command, Qt.MouseButton.LeftButton, pos=point)

    try:
        if quick:
            command = install_spectrum_controls(panel)
            configure_workspace_layout(panel, "spectrum")
            forms = install_parameter_forms(panel, "spectrum")["forms"]
        panel.resize(1300, 900)
        panel.show()
        controller.set_connected(True)
        panel._pair_controls.mode.setCurrentIndex(1)
        assert panel._run_1d_btn.text() == "Run pair"
        assert panel._spectrum_command_pane.isVisibleTo(panel)
        pane = panel._spectrum_command_pane
        assert pane.parentWidget().objectName() == "SpectrumStatusAndCommands"
        assert pane.parentWidget().geometry().bottom() < panel._content_splitter.y()
        assert panel._content_splitter.geometry().bottom() >= panel.height() - 10
        if not quick:
            actions = (panel._acquire_btn, panel._run_1d_btn, panel._stop_btn, panel._abort_btn)
            assert {button.y() for button in actions} == {actions[0].y()}
            assert [button.x() for button in actions] == sorted(button.x() for button in actions)
            assert max(button.width() - button.sizeHint().width() for button in actions) <= 2
        click("run", panel._run_1d_btn)
        _wait_until(lambda: len(controller.requests) == 1)
        assert panel.paired_acquisition_pending
        assert panel._stop_btn.isEnabled() and panel._abort_btn.isEnabled()
        assert not panel._run_1d_btn.isEnabled() and not panel._acquire_btn.isEnabled()
        assert panel._pair_result is None
        assert preflights == [(controller, {"mode": "1d", "backends": ("lightfield",)})]
        controller.points[0].set()
        _wait_until(lambda: len(controller.requests) == 2 and panel._pair_result_point == 1)
        first = panel._pair_result
        assert len(first[2]) == 2
        assert panel._rate_lbl.text().startswith("1 pair ·")
        if quick:
            click("stop", panel._stop_btn)
            assert controller.requests[1][2].is_set()
            assert not panel._stop_btn.isEnabled() and not panel._abort_btn.isEnabled()
            assert panel.paired_acquisition_pending
        controller.points[1].set()
        _wait_until(lambda: not panel.paired_acquisition_pending)
        assert len(controller.requests) == 2
        assert panel._pair_result_point == 2
        assert panel._run_1d_btn.isEnabled() and not panel._stop_btn.isEnabled()
        assert controller.lease[-2:] == [("spectrum", True), ("spectrum", False)]
        assert len(preflights) == 1
        manifest = first[-1]
        terminal = [entry for entry in
                    map(json.loads, manifest.read_text(encoding="utf-8").splitlines())
                    if entry["status"] != "writing"]
        assert [(entry["point_id"], entry["status"]) for entry in terminal] == [
            ("1", "complete"), ("2", "incomplete"),
        ]
        assert len({entry["run_id"] for entry in terminal}) == 1
        assert panel._pair_result[2] == {"lightfield": first[2]["lightfield"]}
        for backend, path in first[2].items():
            with path.open(newline="", encoding="utf-8") as stream:
                points = [row["point_id"] for row in csv.DictReader(stream)]
            assert points == (["1", "2"] if backend == "lightfield" else ["1"])
    finally:
        if panel.paired_acquisition_pending:
            panel._pair_thread.stop.set()
        for point in controller.points:
            point.set()
        _wait_until(lambda: not panel.paired_acquisition_pending)
        if command is not None:
            command.shutdown()
        for form in forms or []:
            form.shutdown()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()
        app.setProperty("spectralQuick", old_quick)


def test_dual_detector_recipes_are_remembered_separately_across_sessions(monkeypatch):
    quick_application()
    monkeypatch.setattr(cfg.lf6, "station_id", "attodry2100")
    panel, restored, fresh = (SpectrumPanel(PairPreview(), bind_controller=False) for _ in range(3))
    try:
        for widget in (panel, restored, fresh):
            widget._pair_controls.set_station("attodry2100")
        controls = panel._pair_controls
        controls.mode.setCurrentIndex(1)
        for editor, value in zip((*controls._pixis_editors, *controls._andor_editors), (650, 50, 1, 1100, 1000, 4)):
            editor.setValue(value)
        restored.restore_session_state(panel.capture_session_state())
        assert restored._pair_controls.recipes() == {
            "lightfield": {"center_nm": 650.0, "exposure_ms": 50.0, "frames": 1},
            "andor_ingaas": {"center_nm": 1100.0, "exposure_ms": 1000.0, "frames": 4}}
        # Before dual mode was ever saved, the Andor recipe starts from the Andor's own settings.
        fresh.restore_session_state({"backend_profiles": {
            "andor_ingaas": {"center_nm": 1100.0, "exposure_ms": 500.0, "accumulations": 2}}})
        assert [editor.value() for editor in fresh._pair_controls._andor_editors] == [1100.0, 500.0, 2]
    finally:
        for widget in (panel, restored, fresh):
            widget.close()
            widget.deleteLater()
