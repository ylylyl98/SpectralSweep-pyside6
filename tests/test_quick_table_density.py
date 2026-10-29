"""Table density remains responsive without paging or hiding record fields."""
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtQml import QQmlEngine, QQmlExpression
from PySide6.QtGui import QFontDatabase, QFontMetricsF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QCheckBox, QComboBox, QTableWidget, QTableWidgetItem

from tests.quick_helpers import quick_application, drain_qt_deletions
from tests.test_quick_parameter_forms import find_item
from ui.presets_panel import BATCH_SCHEMA
from ui.quick_table_editor import QuickTableEditor


@pytest.fixture
def table_factory():
    quick_application()
    # The Windows offscreen plugin does not discover system fonts. Geometry
    # assertions need the same 12px Segoe UI used by the rendered application.
    if not QFontDatabase.families() and os.name == "nt":
        QFontDatabase.addApplicationFont(str(Path(os.environ["WINDIR"]) / "Fonts" / "segoeui.ttf"))
    tables = []

    def create(titles, width, count=2, *, single_line=False):
        source = QTableWidget(count, len(titles))
        source.setProperty("quickSingleLineRows", single_line)
        source.setHorizontalHeaderLabels(titles)
        for row in range(count):
            for column, title in enumerate(titles):
                if title in {"Enable", "Run", "MeasurePower", "Use"}:
                    source.setCellWidget(row, column, QCheckBox())
                elif title == "This run":
                    combo = QComboBox()
                    combo.addItems(["All values", "700"])
                    source.setCellWidget(row, column, combo)
                else:
                    source.setItem(row, column, QTableWidgetItem("700, 800" if title == "Values" else "1"))
        view = QuickTableEditor(source)
        tables.append((source, view))
        view.resize(width, 300)
        view.show()
        QTest.qWait(40)
        return source, view, view.quick_widget.rootObject()

    yield create
    for source, view in tables:
        view.shutdown()
        view.close()
        view.deleteLater()
        source.deleteLater()
    drain_qt_deletions()


def test_loop_schema_fits_shared_header_and_compact_control_rows(table_factory):
    _, view, root = table_factory(["Enable", "Parameter", "Values", "This run", "Group"], 620)
    assert root.property("sharedHeaders")
    assert find_item(root, "QuickTableSharedHeader").isVisible()
    record = find_item(root, "tableRecord_0")
    assert record.height() == 36
    assert view.maximumHeight() <= 100
    fields = [find_item(root, f"tableCell_0_{column}") for column in range(5)]
    assert all(field is not None and field.height() == 28 for field in fields)
    assert len({field.mapToScene(QPointF()).y() for field in fields}) == 1
    assert not view.qml_warnings


def test_gate_conditions_share_header_with_all_eight_fields_at_600px(table_factory):
    titles = ["Use", "#", "Input type", "Vtg", "Vbg", "Vbias", "Doping", "E-field"]
    source, view, root = table_factory(titles, 600)
    assert root.property("sharedHeaders")
    record = find_item(root, "tableRecord_0")
    assert record.height() == 36
    fields = [find_item(root, f"tableCell_0_{column}") for column in range(len(titles))]
    assert all(field is not None and field.height() == 28 for field in fields)
    assert len({field.mapToScene(QPointF()).y() for field in fields}) == 1
    for field in fields:
        left = field.mapToScene(QPointF()).x()
        assert 0 <= left and left + field.width() <= root.width()
    assert view.model.editCell(0, 3, "-1.25")
    assert source.item(0, 3).text() == "-1.25"
    assert view.model.editCell(0, 0, True)
    assert source.cellWidget(0, 0).isChecked()
    assert not view.qml_warnings


@pytest.mark.parametrize("width", [360, 570, 600, 620, 650, 700, 950, 1100, 1600])
@pytest.mark.parametrize("mode", ["light", "dark"])
def test_batch_keeps_every_field_on_one_record_line(table_factory, width, mode):
    source, view, root = table_factory(BATCH_SCHEMA, width, single_line=True)
    source.item(0, BATCH_SCHEMA.index("condition_label")).setText("TG+1BG=0_Rev")
    for title in BATCH_SCHEMA:
        if title.startswith(("Vbg_", "Vtg_", "Vbias_")):
            source.item(0, BATCH_SCHEMA.index(title)).setText("-12.0")
    view.model.refresh()
    root.setProperty("themeMode", mode)
    QTest.qWait(10)
    assert root.property("sharedHeaders")
    record = find_item(root, "tableRecord_0")
    assert record.height() == 36
    fields = []
    for column in range(len(BATCH_SCHEMA)):
        field = find_item(root, f"tableCell_0_{column}")
        fields.append(field)
        assert field is not None
        pos = field.mapToScene(QPointF())
        assert 0 <= pos.x() and pos.x() + field.width() <= root.width()
        assert 0 < field.width() and field.height() == 28
        assert field.mapToItem(record, QPointF()).y() + field.height() <= record.height()
    assert len({field.mapToScene(QPointF()).y() for field in fields}) == 1
    condition = fields[BATCH_SCHEMA.index("condition_label")]
    repeat = fields[BATCH_SCHEMA.index("repeat")]
    power = fields[BATCH_SCHEMA.index("MeasurePower")]
    assert condition.width() > repeat.width() * 3
    assert condition.width() > power.width() * 3
    assert condition.width() <= 320
    assert fields[BATCH_SCHEMA.index("When")].width() <= 240
    assert repeat.width() <= 46
    assert power.width() <= 54
    if width >= 570:
        assert condition.width() >= 120
        measured = QFontMetricsF(condition.property("font"))
        assert measured.horizontalAdvance(condition.property("text")) + condition.property("leftPadding") + condition.property("rightPadding") <= condition.width()
        for title in BATCH_SCHEMA:
            if title.startswith(("Vbg_", "Vtg_", "Vbias_")):
                field = fields[BATCH_SCHEMA.index(title)]
                measured = QFontMetricsF(field.property("font"))
                assert measured.horizontalAdvance(field.property("text")) + field.property("leftPadding") + field.property("rightPadding") <= field.width()
                assert field.property("font").pixelSize() == 12
    if width < 916:
        assert condition.property("leftPadding") == condition.property("rightPadding") == 4
        assert find_item(root, f"tableHeader_{BATCH_SCHEMA.index('MeasurePower')}").property("text") == "Pwr"
    if width >= 950:
        assert condition.width() >= 140
        assert fields[BATCH_SCHEMA.index("When")].width() >= 90
    if width >= 1600:
        assert all(fields[BATCH_SCHEMA.index(title)].width() > 72
                   for title in BATCH_SCHEMA if title.startswith(("Vbg_", "Vtg_", "Vbias_")))
    for title in BATCH_SCHEMA:
        if title.startswith(("Vbg_", "Vtg_", "Vbias_")):
            assert "(V)" in view.model.displayColumnTitles[BATCH_SCHEMA.index(title)]
    assert root.metaObject().indexOfProperty("columnPages") < 0
    grid = find_item(root, "QuickEditableTable")
    assert grid.property("contentWidth") <= grid.width()
    assert not view.qml_warnings


def test_batch_resize_preserves_expression_draft_selection_and_full_tooltips(table_factory):
    source, view, root = table_factory(BATCH_SCHEMA, 1600, single_line=True)
    model = view.model
    when = BATCH_SCHEMA.index("When")
    condition = BATCH_SCHEMA.index("condition_label")
    expression = "Center_Wavelength == 860 and Exposure_Time >= 200"
    label = "Condition with a descriptive name longer than the desktop field"
    token = model.cellAt(0, when)["token"]
    model.stageEdit(token, expression)
    model.selectRow(0, False)
    assert model.editCell(0, condition, label)
    generation = model._generation
    for width in (950, 700, 600, 570, 360, 1100, 1600):
        view.resize(width, view.height())
        QTest.qWait(30)
        assert model._generation == generation
        assert model.cellAt(0, when)["token"] == token
        assert model.cellAt(0, when)["value"] == expression
        assert model.cellAt(0, when)["tooltip"] == expression
        assert model.cellAt(0, condition)["tooltip"] == label
        assert model.cellAt(0, 0)["selected"]
        assert find_item(root, "tableRecord_0").height() == 36
    assert source.item(0, when).text() != expression
    assert model.commitCell(token)
    assert source.item(0, when).text() == expression
    assert model.columnTitles[condition] == "condition_label"
    assert model.displayColumnTitles[condition] == "Condition"
    assert model.displayColumnTitles[BATCH_SCHEMA.index("MeasurePower")] == "Power"
    assert not view.qml_warnings


def test_header_wrap_transition_keeps_draft_selection_and_cell_editing(table_factory):
    source, view, root = table_factory(["Enable", "Parameter", "Values", "This run", "Group"], 620)
    model = view.model
    token = model.cellAt(0, 2)["token"]
    model.stageEdit(token, "810, 850")
    model.selectRow(0, False)
    view.resize(320, view.height())
    QTest.qWait(40)
    assert not root.property("sharedHeaders")
    assert find_item(root, "tableCell_0_2").property("text") == "810, 850"
    assert model.cellAt(0, 0)["selected"]
    assert model.commitCell(token)
    assert source.item(0, 2).text() == "810, 850"
    assert model.editCell(0, 0, True)
    assert source.cellWidget(0, 0).isChecked()
    view.resize(620, view.height())
    QTest.qWait(40)
    assert root.property("sharedHeaders")
    assert find_item(root, "tableRecord_0").height() == 36
    assert not view.qml_warnings


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_flat_table_rows_preserve_selection_focus_error_and_disabled_states(table_factory, mode):
    source, view, root = table_factory(["Enable", "Parameter", "Values", "This run", "Group"], 620)
    root.setProperty("themeMode", mode)
    QTest.qWait(10)

    def value(item, expression):
        probe = QQmlExpression(QQmlEngine.contextForObject(item), item, expression)
        result, undefined = probe.evaluate()
        assert not undefined and not probe.hasError()
        return result

    first = find_item(root, "tableRecord_0")
    second = find_item(root, "tableRecord_1")
    editor = find_item(root, "tableCell_0_2")
    choice = find_item(root, "tableCell_0_3")
    assert value(first, "border.width") == 0
    assert value(second, "border.width") == 0
    assert first.property("color") != second.property("color")
    assert value(editor, "background.border.width") == 0
    assert value(choice, "background.border.width") == 0

    editor.forceActiveFocus(Qt.FocusReason.TabFocusReason)
    QTest.qWait(10)
    assert value(editor, "background.border.width") == 2
    assert find_item(root, "tableRowSelection_0").isVisible()
    assert not find_item(root, "tableRowSelection_1").isVisible()
    focused_border = value(editor, "background.border.color")
    editor.setProperty("invalid", True)
    assert value(editor, "background.border.width") == 2
    assert value(editor, "background.border.color") != focused_border
    editor.setProperty("invalid", False)
    find_item(root, "tableCell_1_2").forceActiveFocus(Qt.FocusReason.TabFocusReason)
    assert value(editor, "background.border.width") == 0

    choice.forceActiveFocus(Qt.FocusReason.TabFocusReason)
    assert value(choice, "background.border.width") == 2
    find_item(root, "tableCell_1_2").forceActiveFocus(Qt.FocusReason.TabFocusReason)
    source.item(0, 2).setText("")
    view.model.refresh()
    assert value(editor, "background.color").alpha() > 0
    assert value(editor, "background.border.width") == 0
    source.item(0, 2).setFlags(source.item(0, 2).flags() & ~Qt.ItemFlag.ItemIsEditable)
    view.model.refresh()
    assert editor.property("readOnly")
    assert value(editor, "background.border.width") == 0
    assert value(editor, "background.color").alpha() == 0
    source.item(0, 2).setFlags(source.item(0, 2).flags() | Qt.ItemFlag.ItemIsEditable)
    view.model.refresh()
    assert value(editor, "background.color").alpha() > 0
    source.setEnabled(False)
    view.model.refresh()
    assert not editor.property("enabled") and not choice.property("enabled")
    assert value(editor, "background.border.width") == 0
    assert value(editor, "background.color").alpha() == 0
    assert value(choice, "background.border.width") == 0
    assert not view.model.editCell(0, 2, "disabled edit")
    assert not view.qml_warnings
