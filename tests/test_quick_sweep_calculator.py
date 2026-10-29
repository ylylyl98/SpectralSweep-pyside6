"""Semantic calculator layout keeps Quick cells aligned and source behavior intact."""
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pandas as pd
import pytest
from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest

from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.presets_panel import PresetsPanel, _required_smu_roles, _smu_readiness_issues
from ui.quick_parameter_forms import install_parameter_forms
from ui.quick_table_editor import install_table_editors
from ui.quick_workspace_layouts import configure_workspace_layout
from utils.config import cfg


@pytest.fixture
def calculator(monkeypatch):
    app = quick_application()
    previous_quick, previous_style = app.property("spectralQuick"), app.styleSheet()
    app.setProperty("spectralQuick", True)
    app.setStyleSheet((Path(__file__).resolve().parents[1] / "ui/styles/quick_application.qss").read_text())
    monkeypatch.setattr(cfg, "save", lambda *args, **kwargs: None)
    panel = PresetsPanel()
    calc = panel._sweep_calc
    originals = {name: value for name, value in vars(calc).items()
                 if name.endswith(("_spin", "_edit", "_combo", "_chk", "_btn", "_toggle"))}
    install_table_editors(panel)
    configure_workspace_layout(panel, "dual_gate")
    coverage = install_parameter_forms(panel, "dual_gate")
    fields = {source: (form, field) for form in coverage["forms"]
              for field, source in form.adapter._fields.items()}
    # Mount just this already assembled workflow to measure actual viewport
    # widths independently of the surrounding workbench and preview splitter.
    scroll = panel._workflow_scroll
    scroll.setParent(None)
    scroll.resize(1024, 900)
    scroll.show()
    header = fields[calc._toggle][0]
    # The QML implicit-height notification and the parent scroll layout settle
    # on separate event-loop turns, especially beside a native rendering probe.
    for _ in range(60):
        QTest.qWait(25)
        if calc.height() == header.height() == 28 and not header._height_timer.isActive():
            break
    try:
        yield panel, calc, fields, originals
    finally:
        scroll.hide()
        scroll.setParent(panel)
        for form in coverage["forms"]:
            form.shutdown()
        for editor in getattr(panel, "_quick_table_editors", []):
            editor.shutdown()
        panel.dispose_reloaded_page()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()
        app.setProperty("spectralQuick", previous_quick)
        app.setStyleSheet(previous_style)


def _rect_in_body(form, calc):
    point = form.mapTo(calc._body, QPoint())
    return point.x(), point.y(), form.width(), form.height()


def test_calculator_cells_keep_sources_and_voltage_columns_aligned(calculator):
    panel, calc, fields, originals = calculator
    assert all(getattr(calc, name) is source for name, source in originals.items())
    assert not calc._toggle.isChecked()
    assert not calc._body.isVisible()
    header_form, header_field = fields[calc._toggle]
    assert calc.height() == header_form.height() == 28
    assert header_field.trigger()
    QTest.qWait(100)
    assert calc._body.isVisible()
    bias_form, bias_field = fields[calc._include_vbias_chk]
    assert bias_field.visible and bias_form.isVisibleTo(calc)
    assert bias_form.width() >= 96
    layout = panel._quick_calculator_layout
    sections = layout.sections
    assert layout.columns == 3
    assert len({section.y() for section in sections}) == 1
    assert sections[0].x() < sections[1].x() < sections[2].x()

    for rows in (
        [(calc._vtg_min_spin, calc._vtg_max_spin),
         (calc._vbg_min_spin, calc._vbg_max_spin),
         (calc._doping_min_spin, calc._doping_max_spin),
         (calc._efield_min_spin, calc._efield_max_spin)],
        [(calc._vtg_start_spin, calc._vtg_stop_spin),
         (calc._vbg_start_spin, calc._vbg_stop_spin)],
    ):
        positions = [[_rect_in_body(fields[source][0], calc) for source in row] for row in rows]
        assert len({row[0][0] for row in positions}) == 1
        assert len({row[1][0] for row in positions}) == 1
        for low, high in positions:
            assert low[1] == high[1]
            assert low[0] + low[2] <= high[0]
            assert low[2] >= 100 and high[2] >= 100
    assert header_field.trigger()
    QTest.qWait(100)
    assert not calc._body.isVisible()
    assert calc.height() == header_form.height()


def test_calculator_reflows_without_horizontal_overflow(calculator):
    panel, calc, fields, _ = calculator
    fields[calc._toggle][1].trigger()
    for width, columns in ((1024, 3), (724, 2), (464, 1)):
        panel._workflow_scroll.resize(width, 900)
        QTest.qWait(120)
        viewport = panel._workflow_scroll.viewport()
        assert panel._quick_calculator_layout.columns == columns
        assert panel._workflow_scroll.horizontalScrollBar().maximum() == 0
        assert calc.width() <= viewport.width()
        for section in panel._quick_calculator_layout.sections:
            assert section.x() >= 0
            assert section.x() + section.width() <= calc._body.width()
        for source in (calc._vtg_min_spin, calc._vtg_max_spin, calc._vtg_start_spin, calc._vtg_stop_spin):
            form = fields[source][0]
            assert form.isVisibleTo(calc)
            x, _, cell_width, _ = _rect_in_body(form, calc)
            assert x >= 0 and x + cell_width <= calc._body.width()
            assert not form.errors()


def test_calculator_quick_actions_preserve_rows_session_and_vbias_interlock(calculator, monkeypatch):
    panel, calc, fields, _ = calculator
    fields[calc._toggle][1].trigger()
    calc._constant_edit.setText("-1:1:1")
    calc._ratio_spin.setValue(.5)
    calc._repeat_spin.setValue(3)
    calc._doping_min_spin.setValue(-7.5)
    calc._efield_max_spin.setValue(3.25)
    calc._recalculate()
    QTest.qWait(100)
    preview = panel._quick_calculator_layout.preview
    assert preview.source_table is calc._multi_preview
    assert preview.isVisibleTo(calc)
    assert preview.width() > panel._quick_calculator_layout.sections[0].width()
    for source in (calc._vtg_start_spin, calc._vtg_stop_spin, calc._vbg_start_spin, calc._vbg_stop_spin):
        assert source.isReadOnly()
        assert fields[source][1].readOnly
    before = panel._batch_table.rowCount()
    assert calc._add_btn.isEnabled()
    assert fields[calc._add_btn][1].trigger()
    assert panel._batch_table.rowCount() == before + 3
    state = panel.capture_session_state()
    calc._constant_edit.setText("0")
    calc._repeat_spin.setValue(1)
    calc._doping_min_spin.setValue(-1)
    calc._toggle.setChecked(False)
    panel.restore_session_state(state)
    assert calc._constant_edit.text() == "-1:1:1"
    assert calc._repeat_spin.value() == 3
    assert calc._doping_min_spin.value() == -7.5
    assert calc._efield_max_spin.value() == 3.25
    assert calc._toggle.isChecked() and calc._body.isVisible()
    calc._include_vbias_chk.setChecked(True)
    calc._recalculate()
    QTest.qWait(100)
    assert not calc._vbias_available()
    assert not calc._vbias_badge.isHidden()
    # Drafting with unavailable bias remains allowed; the original warning
    # explains the channel requirement and run admission still requires it.
    notices = []
    monkeypatch.setattr("ui.presets_panel.QMessageBox.information", lambda *args: notices.append(args))
    assert fields[calc._add_btn][1].trigger()
    assert len(notices) == 1 and "Vbias Keithley unavailable" in notices[0][1]
    assert panel._batch_table.rowCount() == before + 6
    rows = pd.DataFrame(panel.capture_session_state()["draft_batch"])
    roles = _required_smu_roles([{}], rows)
    assert "Vbias" in roles
    assert any("Vbias" in issue for issue in _smu_readiness_issues(None, roles))
