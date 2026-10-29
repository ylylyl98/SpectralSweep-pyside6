"""Quick assembly exposes sequence review while preserving recipe controls."""
import os
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, Qt
from PySide6.QtTest import QTest

from tests.quick_helpers import quick_application, drain_qt_deletions
from tests.test_dual_gate_sequence_model import nested_plan
from ui.presets_panel import PresetsPanel
from ui.megasweep_panel import MegaSweepPanel
from ui.quick_table_editor import install_table_editors
from ui.quick_parameter_forms import install_parameter_forms
from ui.quick_workspace_layouts import configure_workspace_layout, remember_workspace_layout
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


def test_dual_gate_sequence_is_initially_visible_and_source_controls_survive(app):
    from ui.quick_controls import install_workflow_controls

    panel = PresetsPanel()
    command = install_workflow_controls(panel, "Dual Gate", {
        "apply": panel._apply_btn, "run": panel._run_btn, "stop": panel._stop_btn})
    source = (panel._safe_jump_spin, panel._initial_voltage_settle_spin,
              panel._voltage_settle_spin, panel._execution_order_table)
    original_plan = panel._acquisition_schedule
    try:
        install_table_editors(panel)
        assembly = configure_workspace_layout(panel, "dual_gate")
        assert configure_workspace_layout(panel, "dual_gate") is assembly
        forms = install_parameter_forms(panel, "dual_gate")
        panel.layout().removeWidget(command)
        panel._results_pane.layout().addWidget(command)
        command.rootObject().setProperty("compact", True)
        command.show()
        panel.setProperty("quickEditorsInstalled", True)
        panel.resize(1136, 850)
        panel.show()
        QTest.qWait(250)
        assert panel._splitter.orientation() == Qt.Orientation.Horizontal
        assert panel._batch_group.parentWidget() is panel._workflow_content
        assert panel._batch_table.property("quickSingleLineRows")
        assert not assembly["safety"].toggle.isChecked()
        assert not assembly["execution"].toggle.isChecked()
        assert panel._preview_splitter.widget(0) is assembly["execution"]
        top = panel._tree.mapTo(panel._results_scroll.viewport(), QPoint(0, 0))
        visible_height = min(top.y() + panel._tree.height(), panel._results_scroll.viewport().height()) - max(0, top.y())
        # A short plan owns its measured content height, including the detailed
        # loop path and navigation. A desktop viewport shows the entire card;
        # shorter windows deliberately scroll the preview above fixed actions.
        assert visible_height == panel._tree.height() > 0, (
            top, panel._tree.size(), panel._results_scroll.viewport().size())

        panel.resize(1136, 700)
        QTest.qWait(150)
        scroll = panel._results_scroll
        viewport = scroll.viewport()
        top = panel._tree.mapTo(viewport, QPoint())
        assert 0 <= top.y() < viewport.height()
        if top.y() + panel._tree.height() > viewport.height():
            assert scroll.verticalScrollBar().maximum() > 0
            fixed_actions = command.mapTo(panel, QPoint())
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
            QTest.qWait(30)
            bottom = panel._tree.mapTo(viewport, QPoint(0, panel._tree.height()))
            assert 0 < bottom.y() <= viewport.height()
            assert command.mapTo(panel, QPoint()) == fixed_actions
        assert command.isVisibleTo(panel) and command.height() > 0
        def quick_items(item):
            yield item
            for child in item.childItems():
                yield from quick_items(child)

        buttons = {item.objectName(): item for item in quick_items(command.rootObject())}
        for action in ("apply", "run", "stop"):
            button = buttons["command_" + action]
            assert button.isVisible() and button.height() > 0
        assert command.y() >= scroll.y() + scroll.height()
        assert command.y() + command.height() <= panel._results_pane.height()
        scroll.verticalScrollBar().setValue(0)

        safety_field = next(field for form in forms["forms"]
                            for field, source_widget in form.adapter._fields.items()
                            if source_widget is assembly["safety"].toggle)
        assert safety_field.trigger()
        assert assembly["safety"].toggle.isChecked()
        source[0].setValue(.75)
        source[1].setValue(.25)
        source[2].setValue(.125)
        state = panel.capture_session_state()
        assert (state["safe_jump_v"], state["initial_voltage_settle_s"], state["voltage_settle_s"]) == (.75, .25, .125)
        assert panel._acquisition_schedule is original_plan
        assert source == (panel._safe_jump_spin, panel._initial_voltage_settle_spin,
                          panel._voltage_settle_spin, panel._execution_order_table)
        panel.restore_session_state(state)
        QTest.qWait(100)
        restored = panel.capture_session_state()
        assert (restored["safe_jump_v"], restored["initial_voltage_settle_s"], restored["voltage_settle_s"]) == (.75, .25, .125)
        assert panel._preview_splitter.count() == 2
        panel.resize(1010, 580)
        QTest.qWait(150)
        assert panel._splitter.orientation() == Qt.Orientation.Horizontal
        assert panel.minimumSizeHint().width() <= 1010
        panel.resize(896, 580)
        QTest.qWait(150)
        assert panel._splitter.orientation() == Qt.Orientation.Horizontal
        assert panel.minimumSizeHint().width() <= 896
        sizes = []
        for count in (0, 1, 50):
            panel._tree.update_plan([], [], acquisition_schedule=nested_plan(count))
            QTest.qWait(80)
            sizes.append((panel._tree.height(), panel._preview_splitter.height()))
        assert sizes[0][0] < sizes[1][0] < sizes[2][0]
        # The containing splitter must return the reclaimed card space too;
        # shrinking only QML would leave a blank area above Safety and settling.
        assert sizes[2][1] - sizes[1][1] == sizes[2][0] - sizes[1][0]
        collapsed_height = panel._preview_splitter.height()
        with patch.object(panel._tree.sequence_model, "update_plan", wraps=panel._tree.sequence_model.update_plan) as update:
            assembly["execution"].set_expanded(True)
            QTest.qWait(60)
            assert panel._preview_splitter.height() > collapsed_height
            assert assembly["execution"].content.isVisible()
            assembly["execution"].set_expanded(False)
            QTest.qWait(60)
            assert panel._preview_splitter.height() == collapsed_height
            assert update.call_count == 0
    finally:
        command.shutdown()
        coverage = getattr(panel, "_quick_parameter_forms", {})
        for form in coverage.get("forms", []):
            form.shutdown()
        for table in getattr(panel, "_quick_table_editors", []):
            table.shutdown()
        panel.dispose_reloaded_page()
        panel.close()
        panel.deleteLater()


@pytest.mark.parametrize("width,height", [(1700, 850), (1136, 700), (896, 580)])
def test_dual_gate_live_progress_stays_above_right_pane_actions(app, width, height):
    from PySide6.QtCore import QPointF
    from ui.quick_controls import install_workflow_controls

    panel = PresetsPanel()
    command = install_workflow_controls(panel, "Dual Gate", {
        "apply": panel._apply_btn, "run": panel._run_btn, "stop": panel._stop_btn})
    try:
        install_table_editors(panel)
        assembly = configure_workspace_layout(panel, "dual_gate")
        coverage = install_parameter_forms(panel, "dual_gate")
        # Match QuickWorkbench's existing dock, without a global command bar.
        panel.layout().removeWidget(command)
        panel._results_pane.layout().addWidget(command)
        command.rootObject().setProperty("compact", True)
        command.show()
        panel.setProperty("quickEditorsInstalled", True)
        panel.resize(width, height)
        panel.show()
        QTest.qWait(150)

        status = assembly["run_status"]
        assert status.parentWidget() is panel._results_pane
        assert status.property("quickSectionBoundary")
        assert not assembly["run_details"].toggle.isChecked()
        fields = {source: (form, field) for form in coverage["forms"]
                  for field, source in form.adapter._fields.items()}
        progress_form, progress_field = fields[panel._progress]
        assert status.isAncestorOf(progress_form)
        assert progress_form.isVisibleTo(panel)
        assert progress_field.visible
        assert progress_field.kind == "progress"
        for name in ("_status_lbl", "_run_time_lbl"):
            source = getattr(panel, name, None)
            if source is not None:
                form, field = fields[source]
                assert status.isAncestorOf(form)
                assert form.isVisibleTo(panel) and field.visible

        panel._on_frame_progress(3, 12)
        panel._status_lbl.setText("Acquiring frame 4 of 12")
        # QLabel uses the existing bounded readback timer (150 ms).
        QTest.qWait(200)
        assert (progress_field.minimum, progress_field.maximum, progress_field.step) == (0, 12, 3)
        assert progress_field.text == panel._progress.text()
        assert "3/12" in progress_field.text
        assert fields[panel._status_lbl][1].text == "Acquiring frame 4 of 12"

        def quick_items(item):
            yield item
            for child in item.childItems():
                yield from quick_items(child)

        rendered = next(item for item in quick_items(progress_form.rootObject())
                        if item.metaObject().indexOfProperty("from") >= 0
                        and item.metaObject().indexOfProperty("to") >= 0)
        assert rendered.isVisible()
        assert (rendered.property("from"), rendered.property("to"), rendered.property("value")) == (0, 12, 3)
        position = rendered.mapToScene(QPointF())
        assert position.x() >= 0
        assert position.x() + rendered.width() <= progress_form.width() + 1

        pane = panel._results_pane
        scroll = panel._results_scroll
        assert status.y() >= scroll.y() + scroll.height()
        assert command.y() >= status.y() + status.height()
        assert command.y() + command.height() <= pane.height()
        assert 0 <= status.x() <= pane.width() - status.width()
        assert 0 <= command.x() <= pane.width() - command.width()
        assert panel.width() == width
        assert scroll.horizontalScrollBar().maximum() == 0
        # Even a long preview only scrolls its content; progress and Stop stay.
        panel._results_content.setMinimumHeight(1600)
        QTest.qWait(50)
        before = (status.mapTo(panel, QPoint()), command.mapTo(panel, QPoint()))
        assert scroll.verticalScrollBar().maximum() > 0
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        QTest.qWait(30)
        assert (status.mapTo(panel, QPoint()), command.mapTo(panel, QPoint())) == before
        assert progress_form.isVisibleTo(panel)
        assert not assembly["run_details"].toggle.isChecked()
    finally:
        command.shutdown()
        for form in getattr(panel, "_quick_parameter_forms", {}).get("forms", []):
            form.shutdown()
        for editor in getattr(panel, "_quick_table_editors", []):
            editor.shutdown()
        panel.dispose_reloaded_page()
        panel.close()
        panel.deleteLater()


def test_mega_ratio_hint_belongs_to_coordinate_section_and_widgets_skip(app):
    panel = MegaSweepPanel()
    try:
        hint = panel._raw_link_label
        configure_workspace_layout(panel, "mega_sweep")
        assert hint.parentWidget() is panel._coord_widget
        assert panel._coord_widget.layout().indexOf(hint) >= 0
        assert panel._settings_grid.indexOf(hint) < 0
        assert "Steps linked" in hint.text()
        panel._coord_widget._ratio_spin.setValue(2)
        assert "2" in hint.text()
        app.setProperty("spectralQuick", False)
        another = MegaSweepPanel()
        try:
            assert configure_workspace_layout(another, "mega_sweep") == {}
            assert another._settings_grid.indexOf(another._raw_link_label) >= 0
        finally:
            QTest.qWait(20)
            another.close()
            another.deleteLater()
    finally:
        QTest.qWait(20)
        panel.close()
        panel.deleteLater()


def test_optional_bfp_details_keep_primary_acquisition_and_active_background(app):
    from ui.bfp_panel_integrated import BFPPanel
    panel = BFPPanel()
    try:
        center, exposure, sample = panel._center_spin, panel._exp_spin, panel._dev_edit
        panel._bg_panel._enable_chk.setChecked(True)
        sections = configure_workspace_layout(panel, "bfp")
        assert sections["Background"].toggle.isChecked()
        assert not sections["Naming"].toggle.isChecked()
        assert not sections["Acquisition options"].toggle.isChecked()
        assert sections["Naming"].content.isAncestorOf(panel._material_combo)
        assert sections["Acquisition options"].content.isAncestorOf(panel._warmup_chk)
        assert panel._center_spin is center and panel._exp_spin is exposure and panel._dev_edit is sample
        assert not sections["Naming"].content.isAncestorOf(sample)
        assert not sections["Acquisition options"].content.isAncestorOf(center)
        sections["Naming"].set_expanded(True)
        panel._suffix_edit.setText("layout-test")
        assert panel._suffix_edit.text() == "layout-test"
    finally:
        QTest.qWait(20)
        panel.close()
        panel.deleteLater()


def test_mcd2100_active_temperature_and_scan_controls_survive_disclosures(app):
    from ui.mcd2100_panel import MCD2100Panel
    controller = SimpleNamespace(state=SimpleNamespace(name="DISCONNECTED"), generation=0)
    panel = MCD2100Panel(controller)
    try:
        table = panel._condition_table
        count = table.rowCount()
        panel.temperature_control_enabled.setChecked(True)
        sections = configure_workspace_layout(panel, "mcd2100")
        assert sections["Temperature"].toggle.isChecked()
        assert not sections["New gate rows"].toggle.isChecked()
        assert not sections["Filename"].toggle.isChecked()
        assert table is panel._condition_table and table.rowCount() == count
        assert not sections["New gate rows"].content.isAncestorOf(table)
        assert panel._sweep_group.title() == "Field Sweep"
        assert panel._lightfield_group.title() == "LightField"
        assert panel.shutdown(timeout_ms=0)
    finally:
        QTest.qWait(20)
        panel.close()
        panel.deleteLater()


def test_mega_late_layout_callback_is_safe_after_native_page_deletion(app):
    panel = MegaSweepPanel()
    callback = panel._refresh_settings_scroll_range
    panel.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    callback()


def test_mcd2100_collapsed_temperature_returns_full_width_to_optical_fields(app):
    from math import ceil
    from PySide6.QtWidgets import QBoxLayout
    from ui.mcd2100_panel import MCD2100Panel
    controller = SimpleNamespace(state=SimpleNamespace(name="DISCONNECTED"), generation=0)
    panel = MCD2100Panel(controller)
    try:
        install_table_editors(panel)
        sections = configure_workspace_layout(panel, "mcd2100")
        coverage = install_parameter_forms(panel, "mcd2100")

        def settle_geometry():
            # Quick implicit heights and the native scroll viewport settle in
            # separate event-loop passes. Measure the actual collapsed layout,
            # not a transient width before the vertical scrollbar appears.
            previous, stable = None, 0
            for _ in range(80):
                for form in coverage["forms"]:
                    form.adapter.refresh()
                QTest.qWait(25)
                panel._apply_responsive_layout()
                heights = tuple((form.height(), max(1, ceil(form.rootObject().implicitHeight())))
                                for form in coverage["forms"] if form.isVisibleTo(panel))
                scroll = panel._settings_scroll
                geometry = (heights, scroll.viewport().width(), scroll.widget().height(),
                            scroll.verticalScrollBar().maximum(), panel._lightfield_group.width())
                stable = stable + 1 if geometry == previous else 0
                if stable >= 8 and all(actual == expected for actual, expected in heights):
                    return
                previous = geometry
            pytest.fail(f"MCD 2100 geometry did not settle: {previous}")

        panel.resize(1100, 850)
        panel.show()
        settle_geometry()
        row = panel._temperature_lightfield_row
        section = sections["Temperature"]
        field = next(field for form in coverage["forms"]
                     for field, source in form.adapter._fields.items()
                     if source is section.toggle)
        assert row.direction() == QBoxLayout.Direction.TopToBottom
        assert row.property("quickSectionColumns")
        assert panel._lightfield_group.isVisibleTo(panel)
        full_width = panel._lightfield_group.width()
        assert full_width > 500
        assert field.trigger()
        settle_geometry()
        assert row.direction() == QBoxLayout.Direction.LeftToRight
        assert section.content.isVisibleTo(panel)
        assert panel._lightfield_group.width() < full_width
        assert field.trigger()
        settle_geometry()
        assert row.direction() == QBoxLayout.Direction.TopToBottom
        assert panel._lightfield_group.width() == full_width
        # Expansion changes the layout, not whether temperature is controlled.
        assert not panel.temperature_control_enabled.isChecked()
    finally:
        for form in getattr(panel, "_quick_parameter_forms", {}).get("forms", []):
            form.shutdown()
        for editor in getattr(panel, "_quick_table_editors", []):
            editor.shutdown()
        panel.shutdown(timeout_ms=0)
        panel.close()
        panel.deleteLater()


def test_desktop_pane_default_waits_for_viewport_and_preserves_user_resize(app):
    from PySide6.QtWidgets import QSplitter, QVBoxLayout, QWidget
    from ui.quick_workspace_layouts import _readable_pane

    panel = QWidget()
    layout = QVBoxLayout(panel)
    splitter = QSplitter(Qt.Orientation.Horizontal)
    splitter.addWidget(QWidget())
    splitter.addWidget(QWidget())
    layout.addWidget(splitter)
    policy = _readable_pane(panel, splitter, 620, state_key=None)
    try:
        assert not policy.initialized
        panel.resize(1920, 850)
        panel.show()
        QTest.qWait(30)
        assert splitter.sizes()[0] == 620
        panel.resize(2100, 850)
        QTest.qWait(30)
        assert splitter.sizes()[0] == 620
        splitter.moveSplitter(735, 1)
        dragged_width = splitter.sizes()[0]
        splitter.widget(0).setMinimumWidth(400)
        splitter.widget(1).setMinimumWidth(420)
        panel.resize(950, 850)
        QTest.qWait(30)
        assert splitter.sizes()[0] < dragged_width
        panel.resize(1840, 850)
        QTest.qWait(30)
        assert splitter.sizes()[0] == dragged_width
    finally:
        panel.close()
        panel.deleteLater()


def test_responsive_preview_preserves_table_space_until_user_drags(app):
    from PySide6.QtWidgets import QSplitter, QVBoxLayout, QWidget
    from ui.quick_workspace_layouts import _readable_pane

    panel = QWidget()
    layout = QVBoxLayout(panel)
    splitter = QSplitter(Qt.Orientation.Horizontal)
    splitter.addWidget(QWidget())
    splitter.addWidget(QWidget())
    layout.addWidget(splitter)
    policy = _readable_pane(panel, splitter,
                           lambda available: min(420, max(350, available * .36)),
                           index=1, state_key=None, responsive=True)
    try:
        panel.resize(1450, 850)
        panel.show()
        QTest.qWait(40)
        assert splitter.sizes()[1] == 420
        panel.resize(1024, 768)
        QTest.qWait(40)
        assert panel.width() == 1024
        assert 350 <= splitter.sizes()[1] <= 380
        assert splitter.sizes()[0] >= 620
        panel.resize(1450, 850)
        QTest.qWait(40)
        assert splitter.sizes()[1] == 420
        splitter.moveSplitter(splitter.width() - splitter.handleWidth() - 450, 1)
        chosen = splitter.sizes()[1]
        assert not policy.responsive
        panel.resize(1024, 768)
        QTest.qWait(40)
        assert splitter.sizes()[1] == chosen
    finally:
        panel.close()
        panel.deleteLater()


def test_dual_gate_saved_width_before_lazy_migration_wins_over_desktop_default(app):
    panel = PresetsPanel()
    state = {"splitter_sizes": [740, 620], "splitter_orientation": "Horizontal"}
    try:
        # Production MainWindow restores panels before QuickWorkbench exists.
        panel.restore_session_state(state)
        remember_workspace_layout(panel, state)
        install_table_editors(panel)
        configure_workspace_layout(panel, "dual_gate")
        install_parameter_forms(panel, "dual_gate")
        panel.resize(1700, 850)
        panel.show()
        QTest.qWait(100)
        widths = panel._splitter.sizes()
        assert widths[0] / sum(widths) == pytest.approx(740 / 1360, abs=.002)
        assert widths[0] != 980
    finally:
        for form in getattr(panel, "_quick_parameter_forms", {}).get("forms", []):
            form.shutdown()
        for editor in getattr(panel, "_quick_table_editors", []):
            editor.shutdown()
        panel.dispose_reloaded_page()
        panel.close()
        panel.deleteLater()


def test_mega_saved_ratio_before_lazy_migration_wins_over_desktop_default(app):
    panel = MegaSweepPanel()
    state = {"splitter_sizes": [650, 850], "splitter_ratio": .48}
    try:
        panel.restore_session_state(state)
        remember_workspace_layout(panel, state)
        configure_workspace_layout(panel, "mega_sweep")
        panel.resize(1700, 850)
        panel.show()
        QTest.qWait(100)
        widths = panel._splitter.sizes()
        assert widths[0] / sum(widths) == pytest.approx(.48, abs=.002)
        assert panel.capture_session_state()["splitter_ratio"] == pytest.approx(.48, abs=.002)
        assert widths[0] != 620
    finally:
        panel.close()
        panel.deleteLater()


def test_mega_optical_recipe_stays_with_parameters_and_keeps_dynamic_rows(app):
    panel = MegaSweepPanel()
    try:
        table = panel._optical_widget._table
        install_table_editors(panel)
        configure_workspace_layout(panel, "mega_sweep")
        install_parameter_forms(panel, "mega_sweep")
        panel.resize(1390, 850)
        panel.show()
        QTest.qWait(150)
        assert panel._optical_scroll.parentWidget() is panel._settings_content
        recipe = panel.capture_session_state()["optical_sequence"]

        panel.resize(1700, 850)
        QTest.qWait(150)
        assert panel._optical_scroll.parentWidget() is panel._settings_content
        assert not panel._right_panel.isAncestorOf(panel._optical_widget)
        assert panel._optical_scroll.isVisibleTo(panel)
        assert panel._optical_widget._table is table
        assert panel.capture_session_state()["optical_sequence"] == recipe
        one_row_height = panel._optical_scroll.height()
        panel._optical_widget._add_condition()
        QTest.qWait(150)
        assert table.rowCount() == 2
        assert panel._optical_scroll.maximumHeight() > one_row_height
        # Real Segoe metrics grow this section for the second row. A font
        # fallback can make captions taller and constrain it instead; that
        # case must expose ordinary vertical scrolling for the extra content.
        assert (panel._optical_scroll.height() > one_row_height
                or panel._optical_scroll.verticalScrollBar().maximum() > 0)
        assert panel._preview.height() >= 220
        two_rows = panel.capture_session_state()["optical_sequence"]
        panel._optical_group.set_expanded(False)
        QTest.qWait(100)
        assert panel._optical_scroll.height() < one_row_height
        panel._optical_group.set_expanded(True)

        panel.resize(1100, 850)
        QTest.qWait(150)
        for width, height in ((1920, 1080), (1536, 864), (1100, 850)):
            panel.resize(width, height)
            QTest.qWait(150)
            assert panel.width() == width
            assert panel._optical_scroll.parentWidget() is panel._settings_content
            assert panel._optical_scroll.isVisibleTo(panel)
            assert panel.capture_session_state()["optical_sequence"] == two_rows
            assert panel._optical_scroll.horizontalScrollBar().maximum() == 0
            assert panel._settings_scroll.horizontalScrollBar().maximum() == 0
            records = table._quick_editor.quick_widget.rootObject().findChild(QObject, "QuickEditableTable")
            assert records.property("contentWidth") <= records.property("width") + 1
    finally:
        for form in getattr(panel, "_quick_parameter_forms", {}).get("forms", []):
            form.shutdown()
        for editor in getattr(panel, "_quick_table_editors", []):
            editor.shutdown()
        panel.close()
        panel.deleteLater()


def test_mega_short_viewport_summarizes_optics_and_explicit_edit_survives_resize(app):
    panel = MegaSweepPanel()
    try:
        install_table_editors(panel)
        configure_workspace_layout(panel, "mega_sweep")
        coverage = install_parameter_forms(panel, "mega_sweep")
        panel.resize(1024, 768)
        panel.show()
        QTest.qWait(250)
        section = panel._optical_group
        header = next((form, field) for form in coverage["forms"]
                      for field, source in form.adapter._fields.items()
                      if source is section._header)
        form, field = header
        assert not section.is_expanded()
        assert field.kind == "disclosure"
        assert "1/1 active" in field.text and "nm" in field.text and "ms" in field.text
        assert form.isVisibleTo(panel) and field.visible
        assert panel._optical_scroll.height() < 90
        assert panel._optical_scroll.verticalScrollBar().maximum() == 0
        assert panel._optical_scroll.horizontalScrollBar().maximum() == 0
        collapsed_settings_height = panel._settings_content.minimumHeight()
        # The untouched default follows the available height in both directions.
        panel.resize(1700, 850)
        QTest.qWait(200)
        assert section.is_expanded()
        panel.resize(1024, 768)
        QTest.qWait(200)
        assert not section.is_expanded()
        assert field.trigger()
        QTest.qWait(200)
        assert section.is_expanded() and panel._optical_widget.isVisibleTo(panel)
        assert panel._settings_content.minimumHeight() > collapsed_settings_height
        table = panel._optical_widget._table
        model = table._quick_editor.model
        assert model.editCell(0, model._columns.index(2), "765.5")
        QTest.qWait(200)
        assert panel.capture_session_state()["optical_sequence"][0]["center_nm"] == 765.5
        for width, height in ((1700, 850), (1024, 768), (1024, 650)):
            panel.resize(width, height)
            QTest.qWait(150)
            assert section.is_expanded()
            assert panel.width() == width
            assert panel._optical_scroll.horizontalScrollBar().maximum() == 0
            assert panel._settings_scroll.horizontalScrollBar().maximum() == 0
        assert field.trigger()
        QTest.qWait(200)
        assert "765.5 nm" in field.text
        assert panel._optical_scroll.height() < 90
        panel.resize(1700, 850)
        QTest.qWait(150)
        assert not section.is_expanded()
    finally:
        for form in getattr(panel, "_quick_parameter_forms", {}).get("forms", []):
            form.shutdown()
        for editor in getattr(panel, "_quick_table_editors", []):
            editor.shutdown()
        panel.close()
        panel.deleteLater()


@pytest.mark.parametrize("expanded", [True, False])
def test_mega_restored_optical_choice_takes_precedence_over_height(app, expanded):
    panel = MegaSweepPanel()
    state = {"sections": {"optical": expanded}}
    try:
        panel.restore_session_state(state)
        remember_workspace_layout(panel, state)
        configure_workspace_layout(panel, "mega_sweep")
        panel.resize(1024, 768)
        panel.show()
        QTest.qWait(100)
        assert panel._optical_group.is_expanded() is expanded
        panel.resize(1700, 850)
        QTest.qWait(100)
        assert panel._optical_group.is_expanded() is expanded
        # An explicit restore after migration also counts when its state is
        # already current, so it cannot depend on receiving a toggled signal.
        panel.restore_session_state(state)
        panel.resize(1024, 768)
        QTest.qWait(100)
        assert panel._optical_group.is_expanded() is expanded
    finally:
        panel.close()
        panel.deleteLater()


def test_mega_desktop_uses_readable_settings_and_restores_explicit_layout(app):
    panel = MegaSweepPanel()
    try:
        configure_workspace_layout(panel, "mega_sweep")
        panel.resize(1700, 850)
        panel.show()
        QTest.qWait(80)
        assert panel._splitter.sizes()[0] == 620
        panel.resize(1900, 850)
        QTest.qWait(50)
        assert panel._splitter.sizes()[0] == 620
        panel._splitter.moveSplitter(720, 1)
        dragged_width = panel._splitter.sizes()[0]
        panel.resize(1800, 850)
        QTest.qWait(50)
        assert panel._splitter.sizes()[0] == dragged_width
        state = panel.capture_session_state()
        state["splitter_ratio"] = .45
        panel.restore_session_state(state)
        QTest.qWait(80)
        widths = panel._splitter.sizes()
        assert widths[0] / sum(widths) == pytest.approx(.45, abs=.002)
        restored_width = widths[0]
        panel.resize(1920, 850)
        QTest.qWait(50)
        assert panel._splitter.sizes()[0] == restored_width
    finally:
        panel.close()
        panel.deleteLater()
