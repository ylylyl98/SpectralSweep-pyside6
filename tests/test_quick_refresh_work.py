"""Work-count regressions: idle and one-cell updates must stay bounded."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFormLayout, QLabel,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.quick_parameter_forms import install_parameter_forms
from ui.quick_table_editor import QuickTableEditor
from tests.test_quick_parameter_forms import find_item


class CountedCombo(QComboBox):
    def __init__(self):
        super().__init__()
        self.option_reads = 0

    def itemText(self, index):
        self.option_reads += 1
        return super().itemText(index)


@pytest.fixture
def table_view():
    quick_application()
    source = QTableWidget(200, 4)
    choice = CountedCombo()
    choice.addItems(["First", "Second", "Third"])
    number = QDoubleSpinBox()
    number.setRange(-100, 100)
    number.setValue(10)
    source.setCellWidget(0, 0, choice)
    source.setCellWidget(0, 1, number)
    source.setItem(0, 2, QTableWidgetItem("old"))
    view = QuickTableEditor(source)
    view.resize(700, 420)
    view.show()
    QTest.qWait(30)
    view.model.refresh()
    yield source, view, choice, number
    view.shutdown()
    view.close()
    view.deleteLater()
    source.deleteLater()
    drain_qt_deletions()


def test_noop_reconciliation_emits_no_ui_notifications(table_view):
    source, view, _, _ = table_view
    model = view.model
    general = QSignalSpy(model.changed)
    schema = QSignalSpy(model.schemaChanged)
    cells = QSignalSpy(model.cellChanged)
    data = QSignalSpy(model.dataChanged)
    for _ in range(12):
        model.refresh()
    assert general.count() == schema.count() == cells.count() == data.count() == 0
    assert len(model._cells) < source.rowCount() * source.columnCount() // 2
    assert not view.qml_warnings


def test_one_control_change_does_not_rebuild_unrelated_options(table_view):
    _, view, choice, number = table_view
    model = view.model
    cells = QSignalSpy(model.cellChanged)
    choice.option_reads = 0
    number.setValue(11)
    model._flush_dirty()
    assert [cells.at(i) for i in range(cells.count())] == [[0, 1]]
    assert choice.option_reads == 0
    assert model.cellAt(0, 1)["value"] == "11.0"
    cells = QSignalSpy(model.cellChanged)
    choice.model().item(1).setEnabled(False)
    model._flush_dirty()
    assert [cells.at(i) for i in range(cells.count())] == [[0, 0]]
    assert model.cellAt(0, 0)["optionEnabled"] == [True, False, True]


def test_fallback_keeps_blocked_values_flags_and_readonly_state(table_view):
    _, view, choice, number = table_view
    model = view.model
    blocked = number.blockSignals(True)
    number.setValue(42.5)
    number.blockSignals(blocked)
    blocked = choice.model().blockSignals(True)
    choice.model().item(2).setEnabled(False)
    choice.model().blockSignals(blocked)
    view._poll.timeout.emit()
    assert model.cellAt(0, 1)["value"] == "42.5"
    assert model.cellAt(0, 0)["optionEnabled"] == [True, True, False]
    number.setReadOnly(True)
    model._flush_dirty()
    assert not model.cellAt(0, 1)["editable"]
    assert not model.editCell(0, 1, 88)


def test_hidden_models_buffer_changes_and_refresh_once_on_show(table_view):
    _, view, choice, _ = table_view
    view.hide()
    cells = QSignalSpy(view.model.cellChanged)
    choice.setCurrentIndex(2)
    QTest.qWait(10)
    assert not view._poll.isActive() and not view.model._timer.isActive()
    assert cells.count() == 0
    view.show()
    QTest.qWait(10)
    assert view.model.cellAt(0, 0)["currentIndex"] == 2


def test_pooled_record_reloads_all_fields_after_offscreen_change(table_view):
    source, view, _, _ = table_view
    root = view.quick_widget.rootObject()
    records = root.findChild(QObject, "QuickEditableTable")
    # Qt otherwise retains its default currentItem 0 even outside the viewport.
    records.setProperty("currentIndex", -1)
    records.setProperty("contentY", max(0, records.property("contentHeight") - records.property("height")))
    QTest.qWait(30)
    assert 0 not in view.model._active_rows
    source.item(0, 2).setText("changed while pooled")
    records.setProperty("contentY", 0)
    QTest.qWait(30)
    editor = find_item(root, "tableCell_0_2")
    assert editor is not None
    assert editor.property("text") == "changed while pooled"
    assert not view.qml_warnings


def test_scalar_dirty_work_and_signal_free_readbacks(monkeypatch):
    quick_application()
    panel = QWidget()
    form = QFormLayout(panel)
    readback = QLabel("Readback 0")
    form.addRow(readback)
    choices = []
    for index in range(12):
        choice = CountedCombo()
        choice.addItems(["One", "Two", "Three"])
        form.addRow("Choice " + str(index), choice)
        choices.append(choice)
    number = QDoubleSpinBox()
    form.addRow("Number", number)
    coverage = install_parameter_forms(panel, "settings")
    surface = coverage["forms"][0]
    panel.resize(600, 500)
    panel.show()
    QTest.qWait(30)
    fields = {id(source): field for field, source in surface.adapter._fields.items()}
    for choice in choices:
        choice.option_reads = 0
    number.setValue(12.5)
    surface.adapter.refresh_dirty()
    assert fields[id(number)].text == "12.50"
    assert sum(choice.option_reads for choice in choices) == 0
    readback.setText("Readback 1")
    QTest.qWait(200)
    assert fields[id(readback)].text == "Readback 1"
    assert sum(choice.option_reads for choice in choices) == 0
    blocked = number.blockSignals(True)
    number.setValue(37.5)
    number.blockSignals(blocked)
    surface._refresh.timeout.emit()
    assert fields[id(number)].text == "37.50"
    panel.hide()
    number.setValue(50)
    assert not surface.adapter._timer.isActive()
    surface.shutdown()
    panel.deleteLater()
    drain_qt_deletions()
