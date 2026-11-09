"""MCD gate tables retain readable labels and editable source state at narrow widths."""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

from tests.quick_helpers import quick_application, drain_qt_deletions
from tests.test_quick_parameter_forms import find_item
from ui.quick_table_editor import QuickTableEditor
from utils.config import cfg


@pytest.fixture(params=["mcd", "mcd2100"])
def gate_table(request, monkeypatch):
    quick_application()
    monkeypatch.setattr(cfg, "save", lambda *args: None)
    if request.param == "mcd":
        from ui.mcd_panel import MCDPanel
        from ui.quick_workspaces_preview import DisconnectedPreviewMagnet
        panel = MCDPanel(DisconnectedPreviewMagnet(), None, None, None)
    else:
        from ui.mcd2100_panel import MCD2100Panel
        panel = MCD2100Panel(SimpleNamespace(state=SimpleNamespace(name="DISCONNECTED"), generation=0))
    source = panel._condition_table
    # The panel explicitly identifies this surface before Quick is installed.
    assert source.property("quickMcdGateTable")
    view = QuickTableEditor(source)
    view.resize(380, 260)
    view.show()
    QTest.qWait(100)
    yield request.param, panel, source, view
    view.shutdown()
    view.close()
    view.deleteLater()
    panel.shutdown(timeout_ms=0)
    panel.close()
    panel.deleteLater()
    drain_qt_deletions()


def click(view, item):
    position = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
    QTest.mouseClick(view.quick_widget, Qt.MouseButton.LeftButton, pos=position)
    QTest.qWait(20)


def test_real_gate_table_responsive_labels_and_check(gate_table):
    kind, panel, source, view = gate_table
    model = view.model
    assert not model.singleLineRows
    assert model.columnCount() == (6 if kind == "mcd" else 8)
    assert model.displayColumnTitles[0] == "Enabled"
    for title in ("Vtg (V)", "Vbg (V)", "Vbias (V)", "Doping (V)", "E-field (V)"):
        assert title in model.displayColumnTitles
    root = view.quick_widget.rootObject()
    assert not root.property("sharedHeaders")
    for i in range(model.columnCount()):
        cell = find_item(root, f"tableCell_0_{i}")
        assert cell and cell.width() >= 30
    check = find_item(root, "tableCell_0_0")
    click(view, check)
    assert source.item(0, 0).checkState() == Qt.CheckState.Unchecked
    view.resize(1000, 260)
    QTest.qWait(100)
    assert root.property("sharedHeaders")
    assert not view.qml_warnings


def test_1000_narrow_edit_survives_resize_and_enter(gate_table):
    kind, panel, source, view = gate_table
    if kind != "mcd":
        pytest.skip("2100 gate cells are intentionally read-only; use its row editor")
    root = view.quick_widget.rootObject()
    editor = find_item(root, "tableCell_0_1")
    click(view, editor)
    QTest.keyClick(view.quick_widget, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    for char in "1.234":
        QTest.keyClicks(view.quick_widget, char)
        QTest.qWait(15)
    assert editor.property("text") == "1.234"
    view.resize(700, 260)
    QTest.qWait(80)
    assert editor.property("text") == "1.234"
    QTest.keyClick(view.quick_widget, Qt.Key.Key_Return)
    QTest.qWait(50)
    assert float(source.item(0, 1).text()) == pytest.approx(1.234)
    assert not view.qml_warnings


def test_1000_tab_commits_and_selection_is_visible(gate_table):
    kind, panel, source, view = gate_table
    if kind != "mcd":
        pytest.skip("2100 voltage columns are intentionally read-only")
    root = view.quick_widget.rootObject()
    editor = find_item(root, "tableCell_0_1")
    click(view, editor)
    marker = find_item(root, "tableRowSelection_0")
    assert editor.hasActiveFocus()
    assert marker.property("visible")
    QTest.keyClick(view.quick_widget, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(view.quick_widget, "2.75")
    QTest.keyClick(view.quick_widget, Qt.Key.Key_Tab)
    QTest.qWait(50)
    assert float(source.item(0, 1).text()) == pytest.approx(2.75)
    assert not view.qml_warnings


def test_2100_select_row_reaches_existing_row_editor(gate_table):
    kind, panel, source, view = gate_table
    if kind != "mcd2100":
        pytest.skip("Only 2100 uses the separate row editor")
    root = view.quick_widget.rootObject()
    click(view, find_item(root, "tableCell_0_1"))
    assert source.currentRow() == 0
    assert panel._edit_condition_btn.isEnabled()
    panel._edit_condition_btn.click()
    assert panel._gate_edit_row == 0
    assert panel._gate_entry_a.text() and panel._gate_entry_b.text()
    assert not view.qml_warnings


def test_2100_display_only_combo_cannot_be_changed_in_quick(gate_table):
    kind, panel, source, view = gate_table
    if kind != "mcd2100":
        pytest.skip("Only 2100 has a display-only mode combo")
    column = view.model._columns.index(2)
    combo = source.cellWidget(0, 2)
    original = combo.currentIndex()
    cell = view.model.cellAt(0, column)
    assert cell["kind"] == "text" and not cell["editable"]
    assert cell["value"] == combo.currentText()
    assert not view.model.editCell(0, column, 1)
    assert combo.currentIndex() == original
    editor = find_item(view.quick_widget.rootObject(), f"tableCell_0_{column}")
    assert editor.property("readOnly")
    assert not view.qml_warnings
