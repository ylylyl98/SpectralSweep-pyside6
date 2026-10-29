"""Gate sections retain source ownership and meaningful row associations."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtTest import QSignalSpy, QTest

from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.megasweep_panel import MegaSweepPanel
from ui.presets_panel import PresetsPanel
from ui.quick_gate_sections import configure_gate_sections
from ui.quick_parameter_forms import install_parameter_forms
from ui.quick_table_editor import install_table_editors
from ui.quick_workspace_layouts import configure_workspace_layout
from utils.config import cfg


@pytest.fixture
def app(monkeypatch):
    application = quick_application()
    previous = application.property("spectralQuick")
    application.setProperty("spectralQuick", True)
    monkeypatch.setattr(cfg, "save", lambda: None)
    yield application
    application.setProperty("spectralQuick", previous)
    drain_qt_deletions()


def _dispose(panel):
    for form in getattr(panel, "_quick_parameter_forms", {}).get("forms", []):
        form.shutdown()
    for editor in getattr(panel, "_quick_table_editors", []):
        editor.shutdown()
    if hasattr(panel, "dispose_reloaded_page"):
        panel.dispose_reloaded_page()
    panel.close()
    panel.deleteLater()


def test_mega_voltage_bounds_keep_channel_rows_and_column_headers_after_migration(app):
    panel = MegaSweepPanel()
    sources = dict(panel._safety_widget._spins)
    limits = panel._safety_widget.get_limits()
    grid = panel._safety_widget.layout()
    try:
        install_table_editors(panel)
        configure_workspace_layout(panel, "mega_sweep")
        sections = configure_gate_sections(panel, "mega_sweep")
        assert configure_gate_sections(panel, "mega_sweep") is sections
        coverage = install_parameter_forms(panel, "mega_sweep")
        panel.resize(1700, 950)
        panel.show()
        QTest.qWait(120)
        assert panel._safety_widget.layout() is grid
        assert panel._safety_widget._spins == sources
        assert panel._safety_widget.get_limits() == limits
        assert grid.itemAtPosition(0, 1).widget().text() == "Minimum"
        assert grid.itemAtPosition(0, 2).widget().text() == "Maximum"
        form_for = {source: form for form in coverage["forms"] for source in form.adapter._fields.values()}
        for row, axis in enumerate(("vtg", "vbg", "vbias"), start=1):
            minimum, maximum = (form_for[sources[f"{axis}_{bound}"]] for bound in ("min", "max"))
            assert minimum is not maximum
            assert grid.itemAtPosition(row, 1).widget().isAncestorOf(minimum)
            assert grid.itemAtPosition(row, 2).widget().isAncestorOf(maximum)
            assert minimum.mapTo(sections["safety"], QPoint()).y() == maximum.mapTo(sections["safety"], QPoint()).y()
        sources["vtg_min"].setValue(-12)
        assert panel._safety_widget.get_limits()["vtg_min"] == -12
        panel._safety_widget.set_vbias_available(True)
        assert sources["vbias_min"].isEnabled() and sources["vbias_max"].isEnabled()
    finally:
        _dispose(panel)


def test_mega_disclosure_headings_stay_separate_from_metadata_fields(app):
    panel = MegaSweepPanel()
    try:
        install_table_editors(panel)
        configure_workspace_layout(panel, "mega_sweep")
        configure_gate_sections(panel, "mega_sweep")
        coverage = install_parameter_forms(panel, "mega_sweep")
        form_for = {source: (form, field) for form in coverage["forms"] for field, source in form.adapter._fields.items()}
        for section in (panel._timing_section, panel._metadata_group, panel._optical_group):
            assert form_for[section._header][1].kind == "disclosure"
        metadata_header = form_for[panel._metadata_group._header][0]
        assert metadata_header is not form_for[panel._sample_edit][0]
        assert panel._metadata_group.is_expanded()
        assert form_for[panel._metadata_group._header][1].trigger()
        assert not panel._metadata_group.is_expanded()
        assert form_for[panel._metadata_group._header][1].trigger()
        assert panel._metadata_group.is_expanded()
    finally:
        _dispose(panel)


def test_dual_metadata_has_named_groups_without_changing_source_values(app):
    panel = PresetsPanel()
    sources = (panel._sample_edit, panel._point_edit, panel._temp_edit,
               panel._mode_combo_name, panel._laser_edit, panel._power_edit, panel._subfolder_edit)
    changed = QSignalSpy(panel._sample_edit.textChanged)
    try:
        install_table_editors(panel)
        configure_workspace_layout(panel, "dual_gate")
        sections = configure_gate_sections(panel, "dual_gate")
        coverage = install_parameter_forms(panel, "dual_gate")
        assert changed.count() == 0
        assert [sections[key].title() for key in ("sample", "measurement", "output")] == [
            "Sample and location", "Measurement context", "Output"]
        form_for = {source: form for form in coverage["forms"] for source in form.adapter._fields.values()}
        assert all(source in form_for for source in sources)
        assert form_for[sources[0]] is not form_for[sources[3]]
        assert form_for[sources[3]] is not form_for[sources[6]]
        panel.resize(1700, 950)
        panel.show()
        QTest.qWait(120)
        assert sections["sample"].y() == sections["measurement"].y() == sections["output"].y()
        panel.resize(896, 780)
        QTest.qWait(120)
        assert sections["output"].y() > sections["sample"].y()
        assert sources == (panel._sample_edit, panel._point_edit, panel._temp_edit,
                           panel._mode_combo_name, panel._laser_edit, panel._power_edit, panel._subfolder_edit)
    finally:
        _dispose(panel)


def test_widgets_frontend_keeps_original_gate_layout(app):
    app.setProperty("spectralQuick", False)
    panel = MegaSweepPanel()
    try:
        assert configure_gate_sections(panel, "mega_sweep") == {}
        assert panel._safety_widget.layout().itemAtPosition(1, 1).widget() is panel._safety_widget._spins["vtg_min"]
    finally:
        _dispose(panel)
