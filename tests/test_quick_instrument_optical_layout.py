"""The detector's label and live source controls survive sidebar grouping."""
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel
from tests.quick_helpers import quick_application, drain_qt_deletions
from tests.test_quick_parameter_forms import find_item
from ui.instrument_panel import _LF6Section, _PM100DSection
from ui.quick_instrument_layouts import configure_instrument_layout
from ui.quick_parameter_forms import install_parameter_forms
from ui.spectrum_preview import PreviewSpectrumController
from utils.config import cfg


@pytest.mark.parametrize("width", [328, 438])
def test_detector_setup_label_stays_above_its_selector_and_disclosure_works(monkeypatch, width):
    quick_application()
    monkeypatch.setattr(cfg, "save", lambda: None)
    monkeypatch.setattr(cfg.lf6, "backend", "lightfield")
    controller = PreviewSpectrumController()
    section = _LF6Section(controller)
    panel = SimpleNamespace(_sections={"lf6": section})
    groups = configure_instrument_layout(panel)
    assert configure_instrument_layout(panel) is groups
    coverage = install_parameter_forms(section, "instruments")
    try:
        section.resize(width, 600)
        section.show()
        QTest.qWait(80)
        connection = groups["lf6"]["connection"]
        setup = next(label for label in connection.findChildren(QLabel)
                     if label.text() == "Measurement setup:")
        form = next(surface for surface in coverage["forms"]
                    if section._backend in surface.adapter._fields.values())
        fields = {source: field for field, source in form.adapter._fields.items()}
        label = find_item(form.rootObject(), "parameter_label_" + fields[setup].fieldId)
        combo = find_item(form.rootObject(), "parameter_" + fields[section._backend].fieldId)
        label_rect = label.mapRectToScene(label.boundingRect())
        combo_rect = combo.mapRectToScene(combo.boundingRect())
        assert label_rect.bottom() <= combo_rect.top()
        assert label_rect.left() == combo_rect.left()
        assert combo_rect.right() <= form.width()
        assert section._backend.currentData() == "lightfield"
        assert not section._disconnect_btn.isEnabled()
        advanced = section._advanced_expander
        field = next(field for surface in coverage["forms"]
                     for field, source in surface.adapter._fields.items() if source is advanced._btn)
        profile = next(field for surface in coverage["forms"]
                       for field, source in surface.adapter._fields.items() if source is section._optical_profile)
        assert field.trigger()
        assert not advanced._content.isHidden()
        assert profile.visible
        assert field.trigger()
        assert advanced._content.isHidden()
        assert not profile.visible
    finally:
        for form in coverage["forms"]:
            form.shutdown()
        section._status_refresh_timer.stop()
        section._warmup_timer.stop()
        section.close()
        section.deleteLater()
        controller.deleteLater()
        drain_qt_deletions()


class _PowerMeterController(QObject):
    devices_scanned = Signal(list)
    connected = Signal(str)
    disconnected = Signal()
    error = Signal(str)
    is_connected = False

    def scan_devices(self):
        raise AssertionError("Layout tests must not scan hardware")

    def disconnect_instrument(self):
        raise AssertionError("Layout tests must not operate hardware")


@pytest.mark.parametrize("width", [328, 438])
def test_power_meter_short_status_and_number_unit_stay_on_one_line(monkeypatch, width):
    quick_application()
    # Qt's Windows offscreen plugin otherwise falls back to inaccurate missing-
    # font metrics that make these captions artificially wide and hide wrapping.
    font_ids = []
    for name in ("segoeui.ttf", "segoeuib.ttf"):
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        if font.is_file():
            font_ids.append(QFontDatabase.addApplicationFont(str(font)))
    monkeypatch.setattr(cfg, "save", lambda: None)
    controller = _PowerMeterController()
    section = _PM100DSection(controller)
    configure_instrument_layout(SimpleNamespace(_sections={"pm100d": section}))
    coverage = install_parameter_forms(section, "instruments")

    def rendered(source):
        form, field = next((form, field) for form in coverage["forms"]
                           for field, widget in form.adapter._fields.items() if widget is source)
        return form, find_item(form.rootObject(), "parameter_label_" + field.fieldId)

    try:
        section.resize(width, 600)
        section.show()
        section._status.setText("Wavelength set to 685.0 nm")
        section._on_reading(455.3e-6)
        assert section._pwr_lbl.text() == "455.3 µW"
        QTest.qWait(200)
        for source in (section._status, section._pwr_lbl):
            form, label = rendered(source)
            assert form.isVisibleTo(section) and label.isVisible()
            assert label.property("lineCount") == 1, (source.text(), label.width())
            assert label.property("text") == source.text()
            assert label.mapRectToScene(label.boundingRect()).right() <= form.width() + 1
        section._status.setText("WL error: " + "The power meter did not accept the requested calibration wavelength. " * 5)
        QTest.qWait(200)
        form, label = rendered(section._status)
        assert label.property("lineCount") > 1
        assert label.property("text") == section._status.text()
        assert label.mapRectToScene(label.boundingRect()).right() <= form.width() + 1
        assert section.width() == width
    finally:
        for form in coverage["forms"]:
            form.shutdown()
        section._poll_timer.stop()
        section.close()
        section.deleteLater()
        controller.deleteLater()
        drain_qt_deletions()
        for font_id in font_ids:
            if font_id >= 0:
                QFontDatabase.removeApplicationFont(font_id)
