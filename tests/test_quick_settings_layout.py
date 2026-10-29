"""Settings desktop sections keep original control and configuration routes."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractButton, QBoxLayout, QComboBox, QDoubleSpinBox, QLineEdit, QSpinBox,
)

from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.quick_parameter_forms import install_parameter_forms
from ui.quick_settings_layout import configure_quick_settings
from ui.settings_panel import SettingsPanel
from utils.config import cfg


@pytest.fixture
def settings(monkeypatch):
    app = quick_application()
    previous = app.property("spectralQuick")
    app.setProperty("spectralQuick", True)
    monkeypatch.setattr(cfg, "save", lambda: None)
    panel = SettingsPanel()
    state = panel.capture_session_state()
    yield panel
    panel.restore_session_state(state)
    for form in getattr(panel, "_quick_parameter_forms", {}).get("forms", []):
        form.shutdown()
    panel.close()
    panel.deleteLater()
    drain_qt_deletions()
    app.setProperty("spectralQuick", previous)


def test_settings_semantic_sections_preserve_source_controls_and_camera_ownership(settings):
    panel = settings
    original = (panel._andor_si_temperature, panel._andor_ingaas_temperature,
                panel._exposure, panel._andor_sdk_dir)
    controls = tuple(value for name, value in vars(panel).items()
                     if name != "_power_coeff" and isinstance(value, (
                         QAbstractButton, QComboBox, QDoubleSpinBox, QLineEdit, QSpinBox)))
    state = panel.capture_session_state()
    assembly = configure_quick_settings(panel)
    assert configure_quick_settings(panel) is assembly
    coverage = install_parameter_forms(panel, "settings")
    panel.resize(1700, 850)
    panel.show()
    QTest.qWait(150)
    assert original == (panel._andor_si_temperature, panel._andor_ingaas_temperature,
                        panel._exposure, panel._andor_sdk_dir)
    assert panel.capture_session_state() == state
    by_source = {id(source): form for form in coverage["forms"]
                 for source in form.adapter._fields.values()}
    assert by_source[id(original[0])] is not by_source[id(original[1])]
    assert assembly["profiles"][0].isAncestorOf(by_source[id(original[0])])
    assert assembly["profiles"][1].isAncestorOf(by_source[id(original[1])])
    panel._andor_si_temperature.setValue(-61)
    assert cfg.lf6.andor_si_temperature_c == -61
    assert all(row.property("quickSectionColumns") for row in assembly["rows"])
    assert all(row.direction() == QBoxLayout.Direction.LeftToRight for row in assembly["rows"])
    scroll = panel._categories.widget(0)
    assert scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert scroll.widget().width() <= scroll.viewport().width()
    # All source fields remain represented once, including every utility tab.
    sources = [source for form in coverage["forms"] for source in form.adapter._fields.values()]
    for source in (*controls, panel._mff101_position1_path, panel._andor_si_serial,
                   panel._andor_ingaas_serial, panel._base_out_edit,
                   panel._notifications.name_edit, panel._notifications.url_edit):
        assert sources.count(source) == 1


def test_settings_columns_reflow_without_horizontal_overflow(settings):
    assembly = configure_quick_settings(settings)
    install_parameter_forms(settings, "settings")
    settings.resize(900, 700)
    settings.show()
    QTest.qWait(150)
    outer, cameras, lower = assembly["rows"]
    assert outer.direction() == QBoxLayout.Direction.TopToBottom
    assert cameras.direction() == QBoxLayout.Direction.LeftToRight
    assert lower.direction() == QBoxLayout.Direction.TopToBottom
    scroll = settings._categories.widget(0)
    assert scroll.widget().width() <= scroll.viewport().width()
    for index in (1, 2):
        settings._categories.setCurrentIndex(index)
        QTest.qWait(60)
        utility = settings._categories.widget(index)
        assert utility.widget().width() <= utility.viewport().width()


def test_widgets_settings_layout_remains_unchanged(settings):
    app = quick_application()
    app.setProperty("spectralQuick", False)
    body = settings._categories.widget(0).widget()
    original = body.layout().itemAt(0).widget()
    assert configure_quick_settings(settings) == {}
    assert body.layout().itemAt(0).widget() is original
    assert original.title() == "Spectrometer defaults & device setup"
