"""Real calibration page, Quick plot/table edits and scientific evidence path."""
import copy
import json

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton, QWidget

from app.wavelength_calibration import calibrated_axis
from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.quick_dialogs import install_dialog_migration
from ui.quick_scientific_plot import QuickScientificPlot, ScientificPlotItem
from ui.wavelength_calibration_dialog import WavelengthCalibrationDialog


CONTEXT = {"profile": "isolated-bench", "grating": "300", "center_nm": 1320.,
           "output_port": "SideExit", "detector": "synthetic-camera", "geometry": [512, 1], "spectrometer": "synthetic-sp"}


def visual(item, name):
    if item.objectName() == name:
        return item
    for child in item.childItems():
        found = visual(child, name)
        if found is not None:
            return found


@pytest.fixture
def calibration(tmp_path, monkeypatch):
    app = quick_application()
    previous_quick = app.property("spectralQuick")
    app.setProperty("spectralQuick", True)
    from utils import config
    monkeypatch.setattr(config, "_CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config.cfg, "lf6", config.LF6Config())
    monkeypatch.setattr(config.cfg, "save", lambda *a, **k: (_ for _ in ()).throw(AssertionError("No config writes")))
    window = QWidget()
    manager = install_dialog_migration(window)
    dialog = WavelengthCalibrationDialog(window)
    dialog.advanced_toggle.setChecked(True)
    dialog.steps.setCurrentIndex(1)
    dialog.resize(1200, 1000)
    dialog.show()
    QTest.qWait(40)
    yield dialog, manager._entries[id(dialog)], tmp_path
    manager.shutdown()
    dialog.deleteLater()
    window.deleteLater()
    drain_qt_deletions()
    app.setProperty("spectralQuick", previous_quick)


def frame():
    pixels = np.arange(1., 513.)
    counts = np.zeros(512)
    for center in (20.25, 200.2, 300.1, 490.3):
        counts += 1000 * np.exp(-.5 * ((pixels - center) / 1.5) ** 2)
    return {"counts": counts.tolist(), "context": copy.deepcopy(CONTEXT), "captured_utc": "2026-10-26T00:00:00Z"}


def click_source(entry, source):
    view, field = next((view, field) for view in entry["forms"]["forms"]
                       for field, widget in view.adapter._fields.items() if widget is source)
    view.adapter.refresh()
    QTest.qWait(60)
    item = visual(view.rootObject(), "parameter_" + field.fieldId)
    assert item is not None
    assert view.height() >= item.mapToScene(item.boundingRect().center()).y(), (source.text(), view.size(), view.rootObject().implicitHeight())
    QTest.mouseClick(view, Qt.MouseButton.LeftButton,
                     pos=item.mapToScene(item.boundingRect().center()).toPoint())


def test_native_calibration_plot_keeps_one_based_pixels_and_detected_peak_locations(calibration):
    dialog, entry, _ = calibration
    payload = frame()
    original = copy.deepcopy(payload)
    dialog.set_frame(payload)
    QTest.qWait(30)
    assert isinstance(dialog.plot, QuickScientificPlot)
    assert dialog.table.rowCount() == 4
    detected = [float(dialog.table.item(row, 1).text()) for row in range(4)]
    np.testing.assert_allclose(detected, [20.25, 200.2, 300.1, 490.3], atol=.035)
    assert all(dialog.table.cellWidget(row, 0).currentText() == "Ignore" for row in range(4))
    assert all(dialog.table.cellWidget(row, 2).currentText() == "" for row in range(4))
    x, y = next(iter(dialog.plot.export_arrays().values()))
    np.testing.assert_array_equal(x, np.arange(1, 513))
    np.testing.assert_array_equal(y, payload["counts"])
    dialog.plot.setXRange(250, 350)
    renderer = dialog.plot.quick_widget.rootObject().findChild(ScientificPlotItem)
    point = renderer.map_point(300.1, 100.)
    assert "x = 300.1" in renderer.coordinatesAt(point.x(), point.y())
    assert payload == original
    assert not dialog.plot.qml_warnings


def test_manual_peak_button_and_editable_reference_wavelength_reach_source(calibration):
    dialog, entry, _ = calibration
    dialog.set_frame(frame())
    QTest.qWait(20)
    button = next(button for button in dialog.findChildren(QPushButton) if button.text() == "Add peak manually")
    before = dialog.table.rowCount()
    click_source(entry, button)
    assert dialog.table.rowCount() == before + 1
    assert dialog.table.item(before, 1).text() == "1.0000"
    view = dialog.table._quick_editor
    model = view.model
    assert model.editCell(0, model._columns.index(1), "20.2500")
    assert model.editCell(0, model._columns.index(2), "1202.025")
    assert dialog.table.cellWidget(0, 2).currentText() == "1202.025"
    QTest.qWait(30)
    cell = visual(view.quick_widget.rootObject(), "tableCell_0_" + str(model._columns.index(2)))
    assert cell is not None and cell.property("editable") is True
    assert not view.qml_warnings


def test_quick_reference_assignment_fit_independent_check_signal_and_raw_export(calibration):
    dialog, entry, tmp_path = calibration
    payload = frame()
    dialog.set_frame(payload)
    QTest.qWait(30)
    model = dialog.table._quick_editor.model
    for row in range(dialog.table.rowCount()):
        pixel = float(dialog.table.item(row, 1).text())
        assert model.editCell(row, model._columns.index(0), 2 if row == 2 else 1)
        assert model.editCell(row, model._columns.index(2), f"{1200 + .1 * pixel:.8f}")
    dialog.source_confirm.setChecked(True)
    validate = next(button for button in dialog.findChildren(QPushButton) if button.text() == "Validate calibration")
    click_source(entry, validate)
    assert dialog.record is not None, dialog.result.text()
    assert dialog.save.isEnabled()
    np.testing.assert_allclose(dialog.record["coefficients"], [.1, 1200], atol=1e-8)
    axis, mask = calibrated_axis(dialog.record, CONTEXT)
    assert mask.sum() < 512  # No extrapolation outside the confirmed fit lines.
    assert np.all(np.diff(axis) > 0)
    np.testing.assert_array_equal(dialog.record["raw_counts"], payload["counts"])
    emitted = []
    dialog.calibration_saved.connect(emitted.append)
    click_source(entry, dialog.save)
    assert len(emitted) == 1
    emitted[0]["raw_counts"][0] = -999
    assert dialog.frame["counts"][0] != -999
    dialog.save_raw_frame()
    folder = tmp_path / "winspec-calibration-captures"
    metadata = next(folder.glob("*.json"))
    assert json.loads(metadata.read_text(encoding="utf-8"))["context"] == CONTEXT
    raw_csv = np.loadtxt(metadata.with_suffix(".csv"), delimiter=",", skiprows=1)
    np.testing.assert_array_equal(raw_csv[:, 0], np.arange(1, 513))
    np.testing.assert_array_equal(raw_csv[:, 1], payload["counts"])
