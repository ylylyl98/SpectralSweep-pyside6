"""Acquisition controls remain in their semantic sections after migration."""
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QBoxLayout, QWidget

from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.quick_acquisition_sections import configure_acquisition_sections
from ui.quick_parameter_forms import install_parameter_forms
from ui.quick_table_editor import install_table_editors
from ui.quick_workspace_layouts import configure_workspace_layout
from utils.config import cfg


@pytest.fixture
def quick_app(monkeypatch):
    app = quick_application()
    # Windows' offscreen plugin otherwise measures missing-glyph boxes.
    if not QFontDatabase.families() and os.name == "nt":
        QFontDatabase.addApplicationFont(str(Path(os.environ["WINDIR"]) / "Fonts" / "segoeui.ttf"))
    previous = app.property("spectralQuick")
    app.setProperty("spectralQuick", True)
    monkeypatch.setattr(cfg, "save", lambda *args, **kwargs: None)
    yield app
    app.setProperty("spectralQuick", previous)
    drain_qt_deletions()


def _close(panel):
    for form in getattr(panel, "_quick_parameter_forms", {}).get("forms", []):
        form.shutdown()
    for editor in getattr(panel, "_quick_table_editors", []):
        editor.shutdown()
    if hasattr(panel, "_parse_timer"):
        panel._parse_timer.stop()
    if hasattr(panel, "shutdown"):
        panel.shutdown(timeout_ms=0)
    panel.close()
    panel.deleteLater()


def _forms_by_source(coverage):
    return {id(source): form for form in coverage["forms"]
            for source in form.adapter._fields.values()}


def test_spectrum_optics_routing_and_display_keep_separate_forms(quick_app):
    from ui.quick_controls import install_spectrum_controls
    from ui.spectrum_panel import SpectrumPanel
    panel = SpectrumPanel()
    commands = install_spectrum_controls(panel)
    sources = (panel._center, panel._energy, panel._apply_optics_btn,
               panel._grating, panel._output_port, panel._comparison_mode,
               panel._normalize_references_chk, panel._references_toggle)
    try:
        configure_workspace_layout(panel, "spectrum")
        sections = configure_acquisition_sections(panel, "spectrum")
        assert configure_acquisition_sections(panel, "spectrum") is sections
        coverage = install_parameter_forms(panel, "spectrum")
        panel.resize(1600, 850)
        panel.show()
        QTest.qWait(100)
        forms = _forms_by_source(coverage)
        assert forms[id(panel._center)] is forms[id(panel._apply_optics_btn)]
        assert forms[id(panel._center)] is not forms[id(panel._grating)]
        assert forms[id(panel._grating)] is not forms[id(panel._comparison_mode)]
        assert commands.adapter.fields == {}
        for source in (panel._center, panel._exposure, panel._accumulations):
            assert sections["optics"].isAncestorOf(forms[id(source)])
            assert not sections["display"].isAncestorOf(forms[id(source)])
        assert forms[id(panel._exposure)] is forms[id(panel._accumulations)]
        assert sections["display"].isAncestorOf(forms[id(panel._comparison_mode)])
        assert sections["readback"].isAncestorOf(panel._optics_readback)
        field = next(field for form in coverage["forms"]
                     for field, source in form.adapter._fields.items()
                     if source is panel._references_toggle)
        assert field.trigger()
        assert not panel._side_panel.isVisible()
        assert field.trigger()
        assert panel._side_panel.isVisible()
        assert sources == (panel._center, panel._energy, panel._apply_optics_btn,
                           panel._grating, panel._output_port, panel._comparison_mode,
                           panel._normalize_references_chk, panel._references_toggle)
    finally:
        commands.shutdown()
        _close(panel)


def test_spectrum_sensor_reminder_survives_quick_command_replacement(quick_app):
    from PySide6.QtCore import Signal
    from tests.test_quick_parameter_forms import find_item
    from tests.test_spectrum_panel_controls import _FakeSpectrumController
    from ui.quick_controls import install_spectrum_controls
    from ui.spectrum_panel import SpectrumPanel

    class SensorController(_FakeSpectrumController):
        acquisition_settings_readback = Signal(object)

    controller = SensorController()
    controller.identity = {"backend": "lightfield"}
    panel = SpectrumPanel(controller)
    commands = install_spectrum_controls(panel)
    try:
        configure_workspace_layout(panel, "spectrum")
        configure_acquisition_sections(panel, "spectrum")
        coverage = install_parameter_forms(panel, "spectrum")
        panel.resize(1600, 850)
        panel.show()
        controller.connected.emit([])
        QTest.qWait(200)
        fields = {source: field for form in coverage["forms"]
                  for field, source in form.adapter._fields.items()}
        assert fields[panel._exposure].commit("127.5")
        assert fields[panel._accumulations].commit("3")
        assert panel._exposure.value() == 127.5
        assert panel._accumulations.value() == 3
        assert commands.adapter.presenter.fields == []
        form, field = next((form, field) for form in coverage["forms"]
                           for field, source in form.adapter._fields.items()
                           if source is panel._sensor_mode_hint)
        rendered = find_item(form.rootObject(), "parameter_label_" + field.fieldId)
        assert panel._acquisition_group.isHidden()
        assert form.isVisible() and field.visible and rendered.isVisible()
        assert "1D → Bin All" in rendered.property("text")
        assert "2D → Full sensor" in rendered.property("text")

        controller.acquisition_settings_readback.emit({"lightfield_sensor_mode": {
            "requested": "FullSensor", "actual": "FullSensor", "changed": True}})
        QTest.qWait(200)
        assert "switched to FullSensor" in rendered.property("text")
        controller.disconnected.emit()
        QTest.qWait(200)
        assert not field.visible and not rendered.isVisible()
    finally:
        commands.shutdown()
        _close(panel)


def test_motion_pairs_and_gate_options_preserve_source_enabled_state(quick_app):
    from ui.power_sweep_panel import PowerSweepPanel
    panel = PowerSweepPanel()
    sources = (panel._center_spin, panel._pm_wl_spin, panel._exp_spin,
               panel._frames_spin, panel._apply_gates_chk, panel._devid_edit)
    ranges = [(widget.minimum(), widget.maximum()) for widget in sources[:4]]
    try:
        install_table_editors(panel)
        configure_workspace_layout(panel, "power_sweep")
        sections = configure_acquisition_sections(panel, "power_sweep")
        coverage = install_parameter_forms(panel, "power_sweep")
        panel.resize(1600, 850)
        panel.show()
        QTest.qWait(100)
        forms = _forms_by_source(coverage)
        assert forms[id(panel._center_spin)] is forms[id(panel._pm_wl_spin)]
        assert forms[id(panel._center_spin)] is not forms[id(panel._exp_spin)]
        assert forms[id(panel._exp_spin)] is forms[id(panel._frames_spin)]
        assert forms[id(panel._apply_gates_chk)] is not forms[id(panel._devid_edit)]
        assert panel._apply_gates_chk.isEnabled()
        assert not panel._gate_grp.isEnabled()
        assert sections["gate_options"].isAncestorOf(forms[id(panel._apply_gates_chk)])
        assert ranges == [(widget.minimum(), widget.maximum()) for widget in sources[:4]]
        assert sources == (panel._center_spin, panel._pm_wl_spin, panel._exp_spin,
                           panel._frames_spin, panel._apply_gates_chk, panel._devid_edit)
    finally:
        _close(panel)


@pytest.mark.parametrize("page_id", ["mcd", "mcd2100"])
def test_mcd_status_and_display_actions_remain_separate_and_log_signal_works(quick_app, page_id):
    if page_id == "mcd":
        from ui.mcd_panel import MCDPanel
        from ui.quick_workspaces_preview import DisconnectedPreviewMagnet
        panel = MCDPanel(DisconnectedPreviewMagnet(), None, None, None)
        field = panel._current_field
    else:
        from ui.mcd2100_panel import MCD2100Panel
        panel = MCD2100Panel(SimpleNamespace(state=SimpleNamespace(name="DISCONNECTED"), generation=0))
        field = panel.current_field
    sources = (panel._show_log_chk, panel._clear_log_btn, panel._plot_log_splitter, field)
    try:
        install_table_editors(panel)
        configure_workspace_layout(panel, page_id)
        sections = configure_acquisition_sections(panel, page_id)
        coverage = install_parameter_forms(panel, page_id)
        panel.resize(1600, 850)
        panel.show()
        QTest.qWait(100)
        forms = _forms_by_source(coverage)
        assert sections["display_options"].isAncestorOf(forms[id(panel._show_log_chk)])
        assert not sections["display_options"].isAncestorOf(field)
        assert sections["telemetry"].isAncestorOf(field)
        assert sections["header_row"].direction() == QBoxLayout.Direction.LeftToRight
        assert sections["status_body"].direction() == QBoxLayout.Direction.TopToBottom
        toggle = next(field for form in coverage["forms"]
                      for field, source in form.adapter._fields.items()
                      if source is panel._show_log_chk)
        assert toggle.trigger()
        assert panel._log.isVisibleTo(panel)
        panel._log.setPlainText("Test event")
        clear = next(field for form in coverage["forms"]
                     for field, source in form.adapter._fields.items()
                     if source is panel._clear_log_btn)
        assert clear.trigger()
        assert panel._log.toPlainText() == ""
        panel.resize(1100, 850)
        QTest.qWait(50)
        assert sections["header_row"].direction() == QBoxLayout.Direction.TopToBottom
        assert sources == (panel._show_log_chk, panel._clear_log_btn, panel._plot_log_splitter, field)
    finally:
        _close(panel)


@pytest.mark.parametrize("page_id", ["mcd", "mcd2100"])
def test_narrow_mcd_header_keeps_progress_readbacks_and_actions_compact(quick_app, page_id):
    from tests.test_quick_parameter_forms import find_item
    previous_font = quick_app.font()
    quick_app.setFont(QFont("Segoe UI", 9))
    if page_id == "mcd":
        from ui.mcd_panel import MCDPanel
        from ui.quick_workspaces_preview import DisconnectedPreviewMagnet
        panel = MCDPanel(DisconnectedPreviewMagnet(), None, None, None)
        readback, progress = panel._current_field, panel._progress
    else:
        from ui.mcd2100_panel import MCD2100Panel
        panel = MCD2100Panel(SimpleNamespace(state=SimpleNamespace(name="DISCONNECTED"), generation=0))
        readback, progress = panel.current_field, panel.progress_bar
    try:
        configure_workspace_layout(panel, page_id)
        sections = configure_acquisition_sections(panel, page_id)
        coverage = install_parameter_forms(panel, page_id)
        # Match the monitoring width available beside the parameter pane in
        # the 1024px workbench, independent of saved splitter preferences.
        panel._splitter.widget(1).setMaximumWidth(350)
        readback.setText("+1.23456 T")
        progress.setValue(round(progress.maximum() * .63))
        panel.resize(1024, 768)
        panel.show()
        panel._splitter.setSizes([650, 350])
        QTest.qWait(150)
        assert sections["header_row"].direction() == QBoxLayout.Direction.TopToBottom
        assert sections["status_body"].direction() == QBoxLayout.Direction.LeftToRight
        assert sections["progress"].geometry().right() < sections["telemetry"].geometry().left()
        assert readback.text() == "+1.23456 T"
        assert readback.width() >= readback.minimumSizeHint().width()
        assert sections["status"].height() < 120
        forms = _forms_by_source(coverage)
        progress_field = next(field for field, source in forms[id(progress)].adapter._fields.items()
                              if source is progress)
        assert progress_field.step == progress.value()
        controls = [panel._show_log_chk, panel._clear_log_btn]
        if page_id == "mcd":
            controls.insert(0, panel._show_spectrum_chk)
        action_form = forms[id(panel._show_log_chk)]
        items = [find_item(action_form.rootObject(), "parameter_" + field.fieldId)
                 for control in controls
                 for field, source in action_form.adapter._fields.items() if source is control]
        assert len(items) == len(controls)
        assert len({item.mapToScene(QPointF()).y() for item in items}) == 1
        assert all(item.height() >= 28 and item.isVisible() for item in items)
        assert all(0 <= item.mapToScene(QPointF()).x()
                   and item.mapToScene(QPointF()).x() + item.width() <= action_form.width() + 1
                   for item in items)
        if page_id == "mcd2100":
            # A longer gate count must reclaim the progress row. This is
            # driven by label geometry updates, without a window resize.
            panel.progress.setText("Gate 999999/1000000 · 73.5%")
            QTest.qWait(100)
            assert sections["status_body"].direction() == QBoxLayout.Direction.TopToBottom, (
                panel._splitter.widget(1).width(), sections["telemetry"].minimumSizeHint().width())
            assert panel.progress.width() >= panel.progress.minimumSizeHint().width()
            assert readback.width() >= readback.minimumSizeHint().width()
            panel.progress.setText("0 spectra")
            QTest.qWait(100)
            assert sections["status_body"].direction() == QBoxLayout.Direction.LeftToRight
    finally:
        _close(panel)
        quick_app.setFont(previous_font)


def test_fake_workbench_pages_are_not_assumed_to_have_acquisition_widgets(quick_app):
    for page_id in ("spectrum", "power_sweep", "mcd", "mcd2100"):
        panel = QWidget()
        assert configure_acquisition_sections(panel, page_id) == {}
        panel.deleteLater()
