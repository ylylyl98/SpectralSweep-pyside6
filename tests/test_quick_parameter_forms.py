"""Real Quick scalar editors against isolated state-only source controls."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QIntValidator
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
                               QGroupBox, QLineEdit, QPushButton, QSpinBox,
                               QHBoxLayout, QLabel, QMenu, QTableWidget, QToolButton, QVBoxLayout, QWidget)

from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.quick_parameter_forms import install_parameter_forms, parameters_valid, rebind_parameter_sources


def find_item(item, name):
    if item.objectName() == name:
        return item
    for child in item.childItems():
        found = find_item(child, name)
        if found is not None:
            return found
    return None


def test_source_action_roles_and_status_tones_reach_quick_without_changing_actions():
    from ui.style_roles import set_style_role

    quick_application()
    panel = QWidget()
    layout = QHBoxLayout(panel)
    run = QPushButton("Preview plan")
    apply = QPushButton("Apply")
    remove = QPushButton("− Row")
    ordinary = QPushButton("Run preview options")
    status = QLabel("Waiting for instrument")
    set_style_role(run, "runAction")
    set_style_role(status, tone="warning", strong=True)
    for widget in (run, apply, remove, ordinary, status):
        layout.addWidget(widget)
    calls = QSignalSpy(run.clicked)
    coverage = install_parameter_forms(panel, "settings")
    try:
        panel.resize(1000, 180)
        panel.show()
        QTest.qWait(30)
        surface = coverage["forms"][0]
        fields = {source: field for field, source in surface.adapter._fields.items()}
        assert [fields[source].appearance for source in (run, apply, remove, ordinary)] == [
            "primary", "accent", "danger", "standard"]
        rendered = find_item(surface.rootObject(), "parameter_" + fields[run].fieldId)
        label = find_item(surface.rootObject(), "parameter_label_" + fields[status].fieldId)
        assert rendered.property("appearance") == "primary"
        assert label.property("font").bold()
        warning_color = label.property("color")
        assert fields[run].trigger() and calls.count() == 1

        # Source changes still drive the same rendered objects and interlocks.
        set_style_role(run, "stopAction")
        set_style_role(status, tone="success")
        run.setEnabled(False)
        QTest.qWait(30)
        assert rendered.property("appearance") == "danger"
        assert not rendered.isEnabled() and not fields[run].trigger()
        assert calls.count() == 1
        assert fields[status].textTone == "success"
        assert label.property("color") != warning_color
        assert not label.property("font").bold()
    finally:
        for form in coverage["forms"]:
            form.shutdown()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()


def test_semantic_rows_keep_separate_forms_and_original_actions():
    quick_application()
    panel = QWidget()
    layout = QVBoxLayout(panel)
    layout.setAlignment(Qt.AlignmentFlag.AlignTop)
    rows = []
    for _ in range(2):
        row = QWidget()
        row.setProperty("quickSectionBoundary", True)
        inner = QHBoxLayout(row)
        inner.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(row)
        rows.append(row)
    value = QSpinBox()
    rows[0].layout().addWidget(QLabel("Target"))
    rows[0].layout().addWidget(value)
    action = QPushButton("Apply target")
    rows[1].layout().addWidget(action)
    calls = QSignalSpy(action.clicked)
    coverage = install_parameter_forms(panel, "settings")
    try:
        assert len(coverage["forms"]) == 2
        assert {form.parentWidget() for form in coverage["forms"]} == set(rows)
        panel.resize(900, 250)
        panel.show()
        QTest.qWait(30)
        numeric = next(field for form in coverage["forms"]
                       for field, source in form.adapter._fields.items() if source is value)
        button = next(field for form in coverage["forms"]
                      for field, source in form.adapter._fields.items() if source is action)
        assert numeric.commit("23")
        assert value.value() == 23
        assert button.trigger() and calls.count() == 1
        assert rows[1].y() >= rows[0].y() + rows[0].height()
    finally:
        for form in coverage["forms"]:
            form.shutdown()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()


def test_explicit_disclosure_has_its_own_row_without_reclassifying_enable_toggle():
    quick_application()
    panel = QWidget()
    layout = QVBoxLayout(panel)
    disclosure = QToolButton()
    disclosure.setText("Output settings")
    disclosure.setCheckable(True)
    disclosure.setProperty("quickDisclosure", True)
    enable = QToolButton()
    enable.setText("Enable output")
    enable.setCheckable(True)
    layout.addWidget(disclosure)
    layout.addWidget(enable)
    toggles = QSignalSpy(disclosure.toggled)
    coverage = install_parameter_forms(panel, "settings")
    try:
        surface = coverage["forms"][0]
        fields = {source: field for field, source in surface.adapter._fields.items()}
        assert fields[disclosure].kind == "disclosure"
        assert fields[enable].kind == "check"
        panel.resize(900, 250)
        panel.show()
        QTest.qWait(30)
        header = find_item(surface.rootObject(), "parameter_" + fields[disclosure].fieldId)
        body = find_item(surface.rootObject(), "parameter_" + fields[enable].fieldId)
        assert body.mapToScene(QPoint(0, 0)).y() >= header.height()
        assert fields[disclosure].trigger()
        assert disclosure.isChecked() and toggles.count() == 1
        assert not enable.isChecked()
    finally:
        for form in coverage["forms"]:
            form.shutdown()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()


@pytest.fixture
def form():
    quick_application()
    panel = QWidget()
    layout = QVBoxLayout(panel)
    group = QGroupBox("Parameters")
    fields = QFormLayout(group)
    panel.number = QDoubleSpinBox()
    panel.number.setRange(-10, 100)
    panel.number.setDecimals(2)
    panel.number.setValue(3.5)
    fields.addRow("Value", panel.number)
    panel.text = QLineEdit("sample")
    fields.addRow("Name", panel.text)
    panel.choice = QComboBox()
    panel.choice.addItems(["A", "B"])
    fields.addRow("Mode", panel.choice)
    panel.check = QCheckBox("Enabled")
    fields.addRow(panel.check)
    panel.button = QPushButton("Apply")
    fields.addRow(panel.button)
    layout.addWidget(group)
    coverage = install_parameter_forms(panel, "settings")
    widget = coverage["forms"][0]
    panel.resize(900, 500)
    panel.show()
    QTest.qWait(20)
    widget.adapter.refresh()
    field_map = {f.fieldId: f for f in widget.adapter._fields}
    yield panel, group, widget, field_map
    for surface in coverage["forms"]:
        surface.shutdown()
    panel.close()
    panel.deleteLater()
    drain_qt_deletions()


def test_real_qml_numeric_typing_keeps_draft_focus_and_original_signals(form):
    panel, _, widget, fields = form
    editor = find_item(widget.rootObject(), "parameter_number")
    assert editor is not None
    spy = QSignalSpy(panel.number.editingFinished)
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton,
                     pos=editor.mapToScene(editor.boundingRect().center()).toPoint())
    QTest.keyClick(widget, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(widget, "12.75")
    widget.adapter.refresh()
    assert editor.property("activeFocus")
    assert editor.property("text") == "12.75"
    assert panel.number.value() == 12.75
    QTest.keyClick(widget, Qt.Key.Key_Return)
    assert spy.count() == 1
    assert not fields["number"].invalid


def test_invalid_draft_never_clamps_source_or_allows_apply(form):
    panel, _, widget, fields = form
    spy = QSignalSpy(panel.button.clicked)
    assert not fields["number"].edit("1000")
    assert panel.number.value() == 3.5
    widget.adapter.refresh()
    assert fields["number"].text == "1000"
    assert fields["number"].invalid
    assert not parameters_valid(panel)
    assert not fields["button"].trigger()
    assert spy.count() == 0
    assert fields["number"].commit("12.5")
    assert fields["button"].trigger()
    assert spy.count() == 1


def test_external_updates_preserve_field_identity_and_replace_old_draft(form):
    panel, _, widget, fields = form
    editor = find_item(widget.rootObject(), "parameter_number")
    fields["number"].edit("bad")
    panel.number.setValue(28.25)
    widget.adapter.refresh()
    assert find_item(widget.rootObject(), "parameter_number") is editor
    assert fields["number"].text == "28.25"
    assert not fields["number"].invalid


def test_enabled_and_explicit_visibility_follow_hidden_state_host(form):
    panel, group, widget, fields = form
    # Hidden host does not make the source fields logically invisible.
    assert not panel.number.isVisible()
    assert fields["number"].visible
    panel.number.hide()
    widget.adapter.refresh()
    assert not fields["number"].visible
    panel.number.show()
    group.setEnabled(False)
    widget.adapter.refresh()
    assert not fields["number"].enabled
    assert not fields["number"].commit("10")
    group.setEnabled(True)
    widget.adapter.refresh()
    assert fields["number"].enabled
    assert fields["number"].visible


def test_combo_activation_and_check_dispatch_original_user_signals(form):
    panel, _, widget, fields = form
    activated = QSignalSpy(panel.choice.activated)
    assert fields["choice"].select(1)
    assert panel.choice.currentText() == "B"
    assert activated.count() == 1
    assert fields["check"].trigger()
    assert panel.check.isChecked()
    panel.choice.addItem("C")
    widget.adapter.refresh()
    assert fields["choice"].options == ["A", "B", "C"]


def test_disabled_combo_option_never_diverges_quick_display_from_source(form):
    panel, _, widget, fields = form
    panel.choice.model().item(1).setEnabled(False)
    widget.adapter.refresh()
    assert fields["choice"].optionEnabled == [True, False]
    combo = find_item(widget.rootObject(), "parameter_choice")
    # Reproduce the reviewed stale-display path even if a queued activation
    # arrives after the option became disabled.
    combo.setProperty("currentIndex", 1)
    assert not fields["choice"].select(1)
    assert combo.property("currentIndex") == 0
    assert panel.choice.currentIndex() == 0
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton,
                     pos=combo.mapToScene(combo.boundingRect().center()).toPoint())
    QTest.keyClick(widget, Qt.Key.Key_Down)
    QTest.keyClick(widget, Qt.Key.Key_Return)
    QTest.qWait(10)
    assert combo.property("currentIndex") == panel.choice.currentIndex() == 0


def test_line_edit_user_edit_and_finished_are_preserved(form):
    panel, _, _, fields = form
    edited = QSignalSpy(panel.text.textEdited)
    finished = QSignalSpy(panel.text.editingFinished)
    assert fields["text"].edit("new sample")
    assert panel.text.text() == "new sample"
    assert edited.count() == 1
    assert fields["text"].commit("new sample")
    assert finished.count() == 1
    panel.text.setValidator(QIntValidator(0, 100, panel.text))
    assert not fields["text"].commit("invalid")
    assert panel.text.text() == "new sample"


def test_numeric_keyboard_tracking_false_commits_only_on_finish(form):
    panel, _, _, fields = form
    panel.number.setKeyboardTracking(False)
    assert fields["number"].edit("12.25")
    assert panel.number.value() == 3.5
    assert fields["number"].text == "12.25"
    assert fields["number"].commit("12.25")
    assert panel.number.value() == 12.25


def test_numeric_user_signals_preserve_dirty_target_and_enter_move_paths(form):
    panel, _, widget, fields = form
    edited = QSignalSpy(panel.number.lineEdit().textEdited)
    returned = QSignalSpy(panel.number.lineEdit().returnPressed)
    editor = find_item(widget.rootObject(), "parameter_number")
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton,
                     pos=editor.mapToScene(editor.boundingRect().center()).toPoint())
    QTest.keyClick(widget, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(widget, "18.5")
    assert edited.count() > 0
    QTest.keyClick(widget, Qt.Key.Key_Return)
    assert returned.count() == 1
    assert panel.number.value() == 18.5


def test_invalid_draft_does_not_block_mode_toggle_or_cancellation(form):
    panel, _, widget, fields = form
    assert not fields["number"].edit("999")
    assert fields["check"].trigger()
    panel.button.setText("Stop / Cancel")
    widget.adapter.refresh()
    assert fields["button"].trigger()


def test_shutdown_blocks_mutation_and_timer_restart(form):
    panel, _, widget, fields = form
    widget.shutdown()
    assert not fields["number"].commit("25")
    panel.number.setEnabled(False)
    widget.hide()
    widget.show()
    QTest.qWait(10)
    assert not widget._refresh.isActive()
    assert not widget.adapter._timer.isActive()


def test_complex_table_is_preserved_and_scalar_sibling_migrates():
    quick_application()
    panel = QWidget()
    layout = QVBoxLayout(panel)
    row = QFormLayout()
    panel.value = QSpinBox()
    row.addRow("Scalar", panel.value)
    layout.addLayout(row)
    table = QTableWidget(2, 2)
    layout.addWidget(table)
    coverage = install_parameter_forms(panel, "mega_sweep")
    assert coverage["migrated_fields"] == 1
    assert "QTableWidget" in coverage["boundaries"]
    assert table.parentWidget() is panel
    for surface in coverage["forms"]:
        surface.shutdown()
    panel.deleteLater()
    drain_qt_deletions()


def test_spanning_form_row_has_one_editor_and_one_binding(form):
    _, _, widget, fields = form
    checks = [field for field in widget.adapter._fields if field.fieldId == "check"]
    assert len(checks) == 1


@pytest.mark.parametrize("caption", ["Discard", "Reset"])
def test_explicit_correction_discards_invalid_draft_and_allows_next_commit(form, caption):
    panel, _, widget, fields = form
    panel.button.setText(caption)
    panel.button.clicked.connect(lambda: panel.number.setValue(3.5))
    widget.adapter.refresh()
    assert not fields["number"].edit("999")
    assert not parameters_valid(panel)
    assert fields["button"].trigger()
    assert parameters_valid(panel)
    assert fields["number"].text == "3.50"
    assert fields["number"].commit("6.25")
    assert panel.number.value() == 6.25


def test_rebind_replaced_calibration_checkbox_keeps_qml_and_field_identity(form):
    panel, _, widget, fields = form
    previous = panel.check
    field = fields["check"]
    item = find_item(widget.rootObject(), "parameter_check")
    replacement = QCheckBox("New calibration checkbox", previous.parentWidget())
    previous.parentWidget().layout().addRow(replacement)
    replacement.setChecked(True)
    panel.check = replacement
    assert rebind_parameter_sources(panel, {previous: replacement}) == ["check"]
    previous.setChecked(True)
    previous.setChecked(False)
    widget.adapter.refresh()
    assert field.checked
    assert find_item(widget.rootObject(), "parameter_check") is item
    previous.deleteLater()
    drain_qt_deletions()
    widget.adapter.refresh()
    spy = QSignalSpy(replacement.clicked)
    assert field.trigger()
    assert not replacement.isChecked()
    assert spy.count() == 1


def test_measurement_mode_is_one_exclusive_choice_with_its_label():
    quick_application()
    from ui.presets_panel import _MeasurementModeSelector
    panel = QWidget()
    layout = QFormLayout(panel)
    panel.mode = _MeasurementModeSelector()
    layout.addRow("Mode", panel.mode)
    coverage = install_parameter_forms(panel, "dual_gate")
    surface = coverage["forms"][0]
    field = next(field for field, source in surface.adapter._fields.items() if source is panel.mode)
    assert field.kind == "choice" and field.options == ["PL", "Ref"]
    assert any(group.fields[-1] is field and len(group.fields) == 2 for group in surface.adapter.model.groups)
    changed = QSignalSpy(panel.mode.currentTextChanged)
    assert field.select(1)
    assert panel.mode.currentText() == "Ref"
    assert not panel.mode._group.button(0).isChecked()
    assert changed.count() == 1
    panel.mode.setCurrentText("PL")
    surface.adapter.refresh_dirty()
    assert field.currentIndex == 0
    surface.shutdown()
    panel.deleteLater()
    drain_qt_deletions()


def test_dense_form_packs_fields_without_horizontal_overflow(form):
    panel, _, widget, _ = form
    panel.resize(1000, 500)
    QTest.qWait(30)
    wide_height = widget.height()
    assert wide_height < 150
    panel.resize(300, 500)
    QTest.qWait(30)
    for key in ("number", "text", "choice", "check", "button"):
        item = find_item(widget.rootObject(), "parameter_" + key)
        rect = item.mapRectToScene(item.boundingRect())
        assert rect.x() >= -1
        assert rect.right() <= widget.width() + 1
        assert rect.bottom() <= widget.height() + 1
        assert item.height() >= 28
        if key in {"number", "text"}:
            assert item.property("contentWidth") <= item.width() - item.property("leftPadding") - item.property("rightPadding") + 1


def test_original_form_identity_and_dynamic_label_lookup_survive_migration():
    quick_application()
    panel = QWidget()
    source = QFormLayout(panel)
    panel.value = QSpinBox()
    source.addRow("Original label", panel.value)
    label = source.labelForField(panel.value)
    coverage = install_parameter_forms(panel, "power_sweep")
    assert source.labelForField(panel.value) is label
    label.setText("New label")
    source.setRowVisible(panel.value, False)
    surface = coverage["forms"][0]
    surface.adapter.refresh()
    assert not next(f for f in surface.adapter._fields if f.fieldId == "value").visible
    surface.shutdown()
    panel.deleteLater()
    drain_qt_deletions()


def test_checkable_group_stays_usable_and_preserves_child_interlock():
    quick_application()
    panel = QWidget()
    layout = QVBoxLayout(panel)
    group = QGroupBox("Optional settings")
    group.setCheckable(True)
    group.setChecked(False)
    panel.value = QSpinBox()
    fields = QFormLayout(group)
    fields.addRow("Value", panel.value)
    layout.addWidget(group)
    coverage = install_parameter_forms(panel, "power_sweep")
    surface = coverage["forms"][0]
    group_field = next(f for f in surface.adapter._fields if f.kind == "groupcheck")
    value = next(f for f in surface.adapter._fields if f.fieldId == "value")
    assert not value.enabled
    assert group_field.trigger()
    assert value.enabled
    assert group.isChecked()
    surface.shutdown()
    panel.deleteLater()
    drain_qt_deletions()


def test_qml_menu_state_routes_original_qactions_and_dynamic_submenus():
    quick_application()
    panel = QWidget()
    layout = QVBoxLayout(panel)
    panel.actions = QPushButton("Actions")
    menu = QMenu(panel.actions)
    checked = menu.addAction("Checked action")
    checked.setCheckable(True)
    submenu = menu.addMenu("Submenu")
    child = submenu.addAction("Child")
    menu.addSeparator()
    hidden = menu.addAction("Hidden")
    hidden.setVisible(False)
    panel.actions.setMenu(menu)
    layout.addWidget(panel.actions)
    coverage = install_parameter_forms(panel, "dual_gate")
    surface = coverage["forms"][0]
    field = next(f for f in surface.adapter._fields if f.kind == "menu")
    spy = QSignalSpy(checked.triggered)
    assert field.openMenu()
    assert field.actions[0].trigger()
    assert checked.isChecked()
    assert spy.count() == 1
    checked.setEnabled(False)
    assert not field.actions[0].trigger()
    assert not field.actions[-1].visible
    assert field.actions[1].openMenu()
    assert field.actions[1].actions[0].text == "Child"
    menu.aboutToShow.connect(lambda: menu.addAction("Dynamic"))
    assert field.openMenu()
    assert field.actions[-1].text == "Dynamic"
    surface.shutdown()
    assert not field.actions[1].actions[0].trigger()
    panel.deleteLater()
    drain_qt_deletions()


def test_parallel_groups_keep_the_original_responsive_layout_and_top_stretch():
    quick_application()
    panel = QWidget()
    outer = QVBoxLayout(panel)
    columns = QHBoxLayout()
    groups = []
    for title in ("Field Sweep", "Rotation"):
        group = QGroupBox(title)
        form = QFormLayout(group)
        form.addRow("Exposure:", QSpinBox())
        columns.addWidget(group, 1)
        groups.append(group)
    outer.addLayout(columns)
    outer.addStretch(1)
    coverage = install_parameter_forms(panel, "mcd")
    try:
        panel.resize(1000, 650)
        panel.show()
        QTest.qWait(40)
        assert columns.parent() is outer
        assert groups[0].isVisible() and groups[1].isVisible()
        assert groups[0].y() == groups[1].y()
        assert groups[1].x() > groups[0].geometry().right()
        assert groups[0].y() <= outer.contentsMargins().top() + 1
        assert len(coverage["forms"]) == 2
        for surface in coverage["forms"]:
            assert surface.y() < 40
        columns.setDirection(QHBoxLayout.Direction.TopToBottom)
        QTest.qWait(20)
        assert groups[1].y() > groups[0].geometry().bottom()
    finally:
        for surface in coverage["forms"]:
            surface.shutdown()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()


def test_disclosure_is_arrow_header_and_collapses_to_header_height():
    from ui.workflow_sections import DisclosureSection
    quick_application()
    panel = QWidget()
    outer = QVBoxLayout(panel)
    content = QWidget()
    content_layout = QFormLayout(content)
    panel.exposure = QSpinBox()
    content_layout.addRow("Exposure:", panel.exposure)
    section = DisclosureSection("Acquisition options", content, expanded=True)
    outer.addWidget(section)
    coverage = install_parameter_forms(panel, "settings")
    surface = coverage["forms"][0]
    try:
        panel.resize(650, 500)
        panel.show()
        QTest.qWait(40)
        field = next(f for f, w in surface.adapter._fields.items() if w is section.toggle)
        assert field.kind == "disclosure"
        header = find_item(surface.rootObject(), "parameter_" + field.fieldId)
        assert header.property("text").startswith("▾")
        expanded = surface.height()
        assert field.trigger()
        QTest.qWait(40)
        assert not section.toggle.isChecked()
        assert header.property("text").startswith("▸")
        assert surface.height() < expanded
        assert surface.height() <= header.height() + 2
        assert surface.y() <= panel.layout().contentsMargins().top() + 1
        assert field.trigger()
        for _ in range(20):
            QTest.qWait(10)
            if surface.height() == expanded:
                break
        assert surface.height() == expanded
    finally:
        for form in coverage["forms"]:
            form.shutdown()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()


def test_label_ceil_allowance_keeps_exposure_colon_and_stacks_when_narrow():
    quick_application()
    panel = QWidget()
    form = QFormLayout(panel)
    panel.exposure = QSpinBox()
    form.addRow("Exposure:", panel.exposure)
    coverage = install_parameter_forms(panel, "settings")
    surface = coverage["forms"][0]
    try:
        panel.resize(500, 300)
        panel.show()
        QTest.qWait(40)
        label_field = next(f for f in surface.adapter._fields if f.kind == "label")
        label = find_item(surface.rootObject(), "parameter_label_" + label_field.fieldId)
        editor = find_item(surface.rootObject(), "parameter_exposure")
        assert label.property("lineCount") == 1
        assert label.width() >= label.property("contentWidth")
        assert editor.mapToScene(editor.boundingRect().topLeft()).y() <= label.mapToScene(label.boundingRect().bottomRight()).y()
        panel.resize(170, 300)
        QTest.qWait(40)
        assert label.property("lineCount") == 1
        assert editor.mapToScene(editor.boundingRect().topLeft()).y() >= label.mapToScene(label.boundingRect().bottomRight()).y()
        assert editor.width() > 70
        assert editor.mapToScene(editor.boundingRect().bottomRight()).x() <= surface.width() + 1
    finally:
        surface.shutdown()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()


def test_real_settings_camera_profiles_keep_their_row_ownership(monkeypatch):
    from ui.settings_panel import SettingsPanel
    from utils.config import cfg
    quick_application()
    monkeypatch.setattr(cfg, "save", lambda *args: None)
    panel = SettingsPanel()
    coverage = install_parameter_forms(panel, "settings")
    try:
        panel.resize(760, 720)
        panel.show()
        QTest.qWait(70)
        by_source = {id(source): (surface, field) for surface in coverage["forms"]
                     for field, source in surface.adapter._fields.items()}
        surface, si = by_source[id(panel._andor_si_output_port)]
        _, ingaas = by_source[id(panel._andor_ingaas_output_port)]
        si_band = next(b for b in surface.adapter.model.bands if any(si in g.fields for g in b.groups))
        ingaas_band = next(b for b in surface.adapter.model.bands if any(ingaas in g.fields for g in b.groups))
        assert si_band is not ingaas_band
        assert any("Si operating profile" in f.text for g in si_band.groups for f in g.fields)
        assert any("InGaAs operating profile" in f.text for g in ingaas_band.groups for f in g.fields)
        for field in (si, ingaas):
            item = find_item(surface.rootObject(), "parameter_" + field.fieldId)
            rect = item.mapRectToScene(item.boundingRect())
            assert rect.x() >= 0 and rect.right() <= surface.width() + 1
        body = panel._categories.widget(0).widget()
        first_group = next(body.layout().itemAt(i).widget() for i in range(body.layout().count())
                           if body.layout().itemAt(i).widget() is not None)
        assert first_group.mapTo(body, QPoint(0, 0)).y() <= body.layout().contentsMargins().top() + 1
    finally:
        for form in coverage["forms"]:
            form.shutdown()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()


def test_checkable_group_with_opaque_editor_keeps_an_enabled_quick_header():
    quick_application()
    panel = QWidget()
    layout = QVBoxLayout(panel)
    group = QGroupBox("Across conditions")
    group.setCheckable(True)
    group.setChecked(False)
    group_layout = QVBoxLayout(group)
    table = QTableWidget(2, 2)
    group_layout.addWidget(table)
    layout.addWidget(group)
    coverage = install_parameter_forms(panel, "power_sweep")
    try:
        panel.resize(600, 400)
        panel.show()
        QTest.qWait(40)
        header = group._quick_group_header
        field = next(f for f, source in header.adapter._fields.items() if source is group)
        check = find_item(header.rootObject(), "parameter_" + field.fieldId)
        assert field.kind == "groupcheck"
        assert check.property("enabled")
        assert header.parentWidget().height() <= header.parentWidget().sizeHint().height() + 1
        assert not table.isEnabled()
        clicked = QSignalSpy(group.clicked)
        assert field.trigger()
        assert table.isEnabled() and group.isChecked()
        assert clicked.count() == 1
        assert field.trigger()
        assert not table.isEnabled() and not group.isChecked()
        assert check.property("enabled")
        assert group.title() == "Across conditions"
        assert table.parentWidget() is group
        group.hide()
        header.adapter.refresh_dirty()
        assert not field.visible
    finally:
        for surface in coverage["forms"]:
            surface.shutdown()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()


def test_explicit_above_labels_pack_parallel_editors_at_their_preferred_width():
    quick_application()
    panel = QWidget()
    form = QFormLayout(panel)
    panel.start = QSpinBox()
    panel.stop = QSpinBox()
    for label, editor in (("Start", panel.start), ("Stop", panel.stop)):
        editor.setProperty("quickLabelAbove", True)
        editor.setProperty("quickPreferredWidth", 140)
        form.addRow(label, editor)
    coverage = install_parameter_forms(panel, "mcd")
    surface = coverage["forms"][0]
    try:
        panel.resize(324, 400)
        panel.show()
        QTest.qWait(40)
        editors = [find_item(surface.rootObject(), "parameter_" + key) for key in ("start", "stop")]
        rects = [editor.mapRectToScene(editor.boundingRect()) for editor in editors]
        assert rects[0].y() == rects[1].y()
        assert rects[0].y() >= 12
        assert rects[1].x() > rects[0].right()
        assert rects[1].right() <= surface.width()
        assert surface.height() <= 50
        assert panel.start.value() == panel.stop.value() == 0
        assert all(f.labelAbove for f in surface.adapter._fields if f.kind == "number")
    finally:
        surface.shutdown()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()


def test_opt_in_unchecked_group_has_zero_body_and_reopens_from_quick_header():
    quick_application()
    panel = QWidget()
    layout = QVBoxLayout(panel)
    group = QGroupBox("Shared calibration")
    group.setCheckable(True)
    group.setChecked(False)
    group.setMaximumHeight(250)
    group.setProperty("quickCollapseWhenUnchecked", True)
    group_layout = QVBoxLayout(group)
    table = QTableWidget(2, 2)
    group_layout.addWidget(table)
    table.hide()
    group.toggled.connect(table.setVisible)
    layout.addWidget(group)
    coverage = install_parameter_forms(panel, "power_sweep")
    try:
        panel.resize(600, 500)
        panel.show()
        QTest.qWait(40)
        header = group._quick_group_header
        field = next(f for f, source in header.adapter._fields.items() if source is group)
        check = find_item(header.rootObject(), "parameter_" + field.fieldId)
        assert not group.isHidden()
        assert group.maximumHeight() == group.height() == 0
        assert check.isVisible() and check.property("enabled")
        assert header.height() >= 28
        assert field.trigger()
        QTest.qWait(40)
        assert group.isChecked() and table.isVisibleTo(panel)
        assert group.maximumHeight() == 250
        assert 0 < group.height() <= 250
        assert check.property("enabled")
        assert field.trigger()
        QTest.qWait(40)
        assert group.maximumHeight() == group.height() == 0
        assert check.isVisible() and field.visible
        assert not table.isVisibleTo(panel)
    finally:
        for surface in coverage["forms"]:
            surface.shutdown()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()
