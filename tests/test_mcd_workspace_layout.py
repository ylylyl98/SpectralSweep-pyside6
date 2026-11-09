"""MCD sections retain their ownership through dragging, folding and reset."""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QBoxLayout

from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.quick_parameter_forms import install_parameter_forms
from ui.quick_table_editor import install_table_editors
from ui.quick_workspace_layouts import configure_workspace_layout
from utils.config import cfg


@pytest.fixture(params=[("mcd", False), ("mcd", True), ("mcd2100", False), ("mcd2100", True)])
def workspace(request, monkeypatch, tmp_path):
    app = quick_application()
    previous = app.property("spectralQuick")
    page_id, quick = request.param
    app.setProperty("spectralQuick", quick)
    monkeypatch.setattr(cfg, "save", lambda *a, **k: pytest.fail("No production config writes"))
    monkeypatch.setattr(cfg.filename, "base_out", str(tmp_path))
    if page_id == "mcd":
        from ui.mcd_panel import MCDPanel
        from ui.quick_workspaces_preview import DisconnectedPreviewMagnet
        panel = MCDPanel(DisconnectedPreviewMagnet(), None, None, None)
    else:
        from ui.mcd2100_panel import MCD2100Panel
        panel = MCD2100Panel(SimpleNamespace(state=SimpleNamespace(name="DISCONNECTED"), generation=0))
    sections, coverage = {}, {"forms": []}
    if quick:
        install_table_editors(panel)
        sections = configure_workspace_layout(panel, page_id)
        coverage = install_parameter_forms(panel, page_id)
    panel.resize(1600, 900)
    yield panel, page_id, quick, sections, coverage
    for form in coverage["forms"]:
        form.shutdown()
    for table in getattr(panel, "_quick_table_editors", []):
        table.shutdown()
    panel.shutdown(timeout_ms=0)
    panel.close()
    panel.deleteLater()
    drain_qt_deletions()
    app.setProperty("spectralQuick", previous)


def section_containers(panel, sections):
    return [sections.get("Temperature") if name == "Temperature" and sections.get("Temperature")
            else sections.get("Filename") if name == "Output" and sections.get("Filename")
            else group for name, group in panel._mcd_settings_groups.items()]


def test_gate_entry_captions_follow_their_fields_and_mode(workspace):
    panel, _page_id, quick, _sections, coverage = workspace
    a, b = panel._gate_entry_a_label, panel._gate_entry_b_label
    assert a.buddy() is panel._gate_entry_a
    assert b.buddy() is panel._gate_entry_b
    assert panel._gate_entry_expansion_label.buddy() is panel._gate_entry_expansion
    for mode, captions in (("direct", ("Vtg values", "Vbg values")),
                           ("doping_efield", ("Doping values", "E-field values"))):
        panel._gate_entry_mode.setCurrentIndex(panel._gate_entry_mode.findData(mode))
        assert (a.text(), b.text()) == captions
    panel._gate_entry_a.setText("1,2")
    panel._gate_entry_b.setText("3,4")
    assert not panel._gate_entry_expansion_label.isHidden()
    assert not panel._gate_entry_expansion.isHidden()
    panel._gate_entry_b.setText("3")
    assert panel._gate_entry_expansion_label.isHidden()
    assert panel._gate_entry_expansion.isHidden()
    if quick:
        owners = {source: form for form in coverage["forms"]
                  for source in form.adapter._fields.values()}
        assert owners[a] is owners[panel._gate_entry_a]
        assert owners[b] is owners[panel._gate_entry_b]
        assert owners[panel._gate_entry_expansion_label] is owners[panel._gate_entry_expansion]


@pytest.mark.parametrize("spec_a,spec_b,expansion,expected", [
    ("1,2", "3", "paired", [(1, 3), (2, 3)]),
    ("(1,3,1)", "4", "paired", [(1, 4), (2, 4), (3, 4)]),
    ("1,2", "3,4", "grid", [(1, 3), (1, 4), (2, 3), (2, 4)]),
])
def test_batch_gate_entry_is_visible_and_adds_multiple_conditions(
        workspace, spec_a, spec_b, expansion, expected):
    from tests.test_quick_parameter_forms import find_item

    panel, page_id, quick, sections, coverage = workspace
    panel._seed_condition_table([dict(enabled=True, mode="direct", input_a=0,
                                     input_b=0, vtg_v=0, vbg_v=0, vbias_v=0)])
    panel._gate_entry_mode.setCurrentIndex(panel._gate_entry_mode.findData("direct"))
    panel._gate_entry_expansion.setCurrentIndex(
        panel._gate_entry_expansion.findData(expansion))
    panel.show()
    QTest.qWait(80)
    scroll = getattr(panel, "_settings_scroll", getattr(panel, "_scroll", None))
    if quick:
        assert sections["New gate rows"].toggle.isChecked()
        forms = {source: (form, field) for form in coverage["forms"]
                 for field, source in form.adapter._fields.items()}

    def control(source):
        if not quick:
            scroll.ensureWidgetVisible(source)
            QTest.qWait(20)
            assert source.isVisible()
            return source, source.rect().center()
        form, field = forms[source]
        scroll.ensureWidgetVisible(form)
        QTest.qWait(30)
        item = find_item(form.rootObject(), "parameter_" + field.fieldId)
        assert form.isVisible() and field.visible and item.isVisible()
        return form, item.mapToScene(item.boundingRect().center()).toPoint()

    for source, text in ((panel._gate_entry_a, spec_a), (panel._gate_entry_b, spec_b)):
        widget, position = control(source)
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=position)
        QTest.keyClick(widget, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClicks(widget, text)
        QTest.keyClick(widget, Qt.Key.Key_Tab)
        assert source.text() == text
    QTest.qWait(50)
    assert panel._gate_entry_add.isEnabled()
    assert panel._gate_entry_add.text() == f"Add {len(expected)} rows"
    widget, position = control(panel._gate_entry_add)
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=position)
    QTest.qWait(50)
    rows = panel._condition_rows()
    assert [(row["vtg_v"], row["vbg_v"]) for row in rows] == expected


def test_pairing_fields_are_editable_and_restore_in_both_shells(workspace):
    panel, page_id, quick, sections, coverage = workspace
    if page_id == "mcd":
        step, tolerance, hold = panel._point_step_mt, panel._point_tolerance_mt, panel._point_hold_s
        panel._acquisition_mode.setCurrentIndex(1)
    else:
        step, tolerance, hold = panel.point_step, panel.point_tolerance, panel.point_stable
        panel.point_pairing.setChecked(True)
    assert tolerance.value() == 10
    for widget, value in ((step, 75), (tolerance, 4), (hold, 2)):
        widget.setValue(value)
    snapshot = panel.capture_session_state()
    step.setValue(50)
    tolerance.setValue(10)
    hold.setValue(1)
    panel.restore_session_state(snapshot)
    assert (step.value(), tolerance.value(), hold.value()) == (75, 4, 2)
    if quick:
        fields = {source for form in coverage["forms"] for source in form.adapter._fields.values()}
        assert {step, tolerance, hold} <= fields


def test_dragging_keeps_full_width_sections_in_the_same_order(workspace):
    panel, page_id, quick, sections, coverage = workspace
    panel.show()
    QTest.qWait(80)
    scroll = getattr(panel, "_settings_scroll", getattr(panel, "_scroll", None))
    owners = section_containers(panel, sections)
    for width in (460, 760, 510, 900, 620):
        total = sum(panel._splitter.sizes())
        panel._splitter.setSizes([width, total - width])
        panel._splitter.splitterMoved.emit(panel._splitter.sizes()[0], 1)
        QTest.qWait(40)
        # Both Qt layout requests and Quick implicit-height updates are queued.
        # Observe the settled section geometry rather than a fixed 40 ms frame.
        for _ in range(100):
            positions = [owner.mapTo(scroll.widget(), QPoint()).y() for owner in owners]
            if all(second >= first + owner.height()
                   for first, second, owner in zip(positions, positions[1:], owners)):
                break
            QTest.qWait(10)
        assert all(second >= first + owner.height()
                   for first, second, owner in zip(positions, positions[1:], owners))
        assert len({owner.width() for owner in owners[:3]}) <= 2
        rows = (panel._row_a,) if page_id == "mcd" else (panel._field_rotation_row, panel._temperature_lightfield_row)
        assert all(row.direction() == QBoxLayout.Direction.TopToBottom for row in rows)
    if quick:
        forms = {source: form for form in coverage["forms"] for source in form.adapter._fields.values()}
        start = panel._start_t if page_id == "mcd" else panel.start_field
        angle = panel._angle_a if page_id == "mcd" else panel.angles
        optical = panel._center if page_id == "mcd" else panel.lf_center
        assert len({forms[start], forms[angle], forms[optical]}) == 3
        for name, source in (("FieldSweep", start), ("Rotation", angle), ("LightField", optical)):
            assert panel._mcd_settings_groups[name].isAncestorOf(forms[source])
    else:
        assert panel.layout().indexOf(panel._run_footer) < panel.layout().indexOf(panel._splitter)


def test_reset_overrides_restored_width_without_changing_measurement_values(workspace):
    panel, page_id, quick, sections, coverage = workspace
    panel.restore_session_state({"splitter_sizes": [950, 550]})
    panel.show()
    QTest.qWait(80)
    assert panel._splitter.sizes()[0] > panel._mcd_pane_width.preferred + 100
    scroll = getattr(panel, "_settings_scroll", getattr(panel, "_scroll", None))
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
    before = panel.capture_session_state()
    if quick:
        field = next(field for form in coverage["forms"] for field, source in form.adapter._fields.items()
                     if source is panel._reset_layout_btn)
        assert field.trigger()
    else:
        panel._reset_layout_btn.click()
    QTest.qWait(60)
    assert abs(panel._splitter.sizes()[0] - panel._mcd_pane_width.preferred) <= 2
    assert scroll.verticalScrollBar().value() == 0
    after = panel.capture_session_state()
    for key in before.keys() - {"splitter_sizes", "plot_log_sizes"}:
        assert after[key] == before[key], key
    panel.resize(1800, 900)
    QTest.qWait(40)
    assert abs(panel._splitter.sizes()[0] - panel._mcd_pane_width.preferred) <= 2
    panel.restore_session_state(before)
    QTest.qWait(40)
    # Settings keep the user's pixel width; extra window width belongs to plots.
    assert abs(panel._splitter.sizes()[0] - before["splitter_sizes"][0]) <= 2


def test_reset_before_show_and_invalid_drafts_remain_presentation_only(workspace):
    panel, page_id, quick, sections, coverage = workspace
    panel.restore_session_state({"splitter_sizes": [1000, 400]})
    panel._reset_layout_btn.click()
    panel.show()
    QTest.qWait(80)
    assert abs(panel._splitter.sizes()[0] - panel._mcd_pane_width.preferred) <= 2
    if quick:
        from tests.test_quick_parameter_forms import find_item
        source = panel._center if page_id == "mcd" else panel.lf_center
        fields = {source: field for form in coverage["forms"] for field, source in form.adapter._fields.items()}
        forms = {source: form for form in coverage["forms"] for source in form.adapter._fields.values()}
        value = source.value()
        scroll = getattr(panel, "_settings_scroll", getattr(panel, "_scroll", None))
        scroll.ensureWidgetVisible(forms[source])
        QTest.qWait(20)
        editor = find_item(forms[source].rootObject(), "parameter_" + fields[source].fieldId)
        QTest.mouseClick(forms[source], Qt.MouseButton.LeftButton,
                         pos=editor.mapToScene(editor.boundingRect().center()).toPoint())
        assert editor.hasActiveFocus()
        QTest.keyClick(forms[source], Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClicks(forms[source], "not a number")
        assert fields[source].invalid
        reset = panel._reset_layout_btn
        button = find_item(forms[reset].rootObject(), "parameter_" + fields[reset].fieldId)
        QTest.mouseClick(forms[reset], Qt.MouseButton.LeftButton,
                         pos=button.mapToScene(button.boundingRect().center()).toPoint())
        QTest.qWait(20)
        assert fields[source].invalid
        assert fields[source].text == "not a number"
        assert source.value() == value
        assert scroll.verticalScrollBar().value() == 0


def test_gate_weighting_and_settling_keep_equal_columns_at_every_pane_width(workspace):
    from tests.test_quick_parameter_forms import find_item

    panel, page_id, quick, sections, coverage = workspace
    first = panel._initial_voltage_settle if page_id == "mcd" else panel.initial_voltage_settle
    later = panel._voltage_settle if page_id == "mcd" else panel.voltage_settle
    controls = (panel.gate_vtg_factor, panel.gate_vbg_factor, first, later)
    panel.show()
    QTest.qWait(80)

    def rectangle(source):
        if not quick:
            return QRectF(source.mapTo(panel, QPoint()), source.size())
        form, field = next((form, field) for form in coverage["forms"]
                           for field, widget in form.adapter._fields.items() if widget is source)
        item = find_item(form.rootObject(), "parameter_" + field.fieldId)
        top = item.mapToScene(QPointF()) + QPointF(form.mapTo(panel, QPoint()))
        return QRectF(top.x(), top.y(), item.width(), item.height())

    for width in (460, 620, 900):
        total = sum(panel._splitter.sizes())
        panel._splitter.setSizes([width, total - width])
        panel._splitter.splitterMoved.emit(panel._splitter.sizes()[0], 1)
        QTest.qWait(40)
        # Quick forms report their content height asynchronously. The ratio
        # stays a native label (its labels-only row is not migrated), so wait
        # until the parent layout has incorporated those Quick height hints.
        # Compare only visible coordinates in panel space after each resize.
        for _ in range(100):
            tg, bg, first_rect, later_rect = map(rectangle, controls)
            ratio_y = panel.gate_ratio_value.mapTo(panel, QPoint()).y()
            if tg.bottom() < ratio_y < first_rect.top():
                break
            QTest.qWait(10)
        assert abs(tg.y() - bg.y()) <= 1
        assert abs(first_rect.y() - later_rect.y()) <= 1
        assert abs(tg.width() - bg.width()) <= 1
        assert abs(first_rect.width() - later_rect.width()) <= 1
        assert abs(tg.x() - first_rect.x()) <= 1
        assert abs(bg.x() - later_rect.x()) <= 1
        assert tg.right() < bg.left()
        assert tg.width() >= 100
        assert tg.bottom() < ratio_y < first_rect.top()
