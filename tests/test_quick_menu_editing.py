import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMenu, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

from tests.quick_helpers import quick_application, drain_qt_deletions
from tests.test_quick_parameter_forms import find_item
from ui.quick_parameter_forms import install_parameter_forms
from ui.quick_table_editor import install_table_editors


def menu_table_panel(*, number=False):
    quick_application()
    panel = QWidget()
    layout = QVBoxLayout(panel)
    table = QTableWidget(1, 1)
    if number:
        value = QSpinBox()
        value.setRange(1, 100)
        value.setValue(2)
        table.setCellWidget(0, 0, value)
    else:
        table.setItem(0, 0, QTableWidgetItem("old"))
        value = None
    layout.addWidget(table)
    panel.menu_button = QPushButton("Actions")
    menu = QMenu(panel.menu_button)
    panel._batch_copy_action = menu.addAction("Copy Selected Rows")
    panel._batch_undo_action = menu.addAction("Undo Batch Edit")
    panel.menu_button.setMenu(menu)
    layout.addWidget(panel.menu_button)
    tables = install_table_editors(panel)
    coverage = install_parameter_forms(panel, "dual_gate")
    field = next(field for view in coverage["forms"] for field in view.adapter._fields if field.kind == "menu")
    assert field.openMenu()
    panel.resize(800, 450)
    panel.show()
    QTest.qWait(30)
    return panel, table, value, tables[0], field


def close_panel(panel, view):
    for form in panel._quick_parameter_forms["forms"]:
        form.shutdown()
    view.shutdown()
    panel.close()
    panel.deleteLater()
    drain_qt_deletions()


def test_menu_copy_commits_the_focused_qml_draft_before_original_action():
    panel, source, _, view, menu = menu_table_panel()
    copied = []
    panel._batch_copy_action.triggered.connect(lambda: copied.append(source.item(0, 0).text()))
    try:
        editor = find_item(view.quick_widget.rootObject(), "tableCell_0_0")
        QTest.mouseClick(view.quick_widget, Qt.MouseButton.LeftButton,
                         pos=editor.mapToScene(editor.boundingRect().center()).toPoint())
        QTest.keyClick(view.quick_widget, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClicks(view.quick_widget, "pending current value")
        assert editor.property("activeFocus")
        assert source.item(0, 0).text() == "old"
        assert menu.actions[0].trigger()
        assert copied == ["pending current value"]
        assert source.item(0, 0).text() == "pending current value"
    finally:
        close_panel(panel, view)


def test_menu_undo_discards_invalid_draft_before_original_undo():
    panel, source, value, view, menu = menu_table_panel(number=True)
    panel._batch_undo_action.triggered.connect(lambda: value.setValue(1))
    try:
        token = view.model.data(view.model.index(0, 0), view.model.CellRole)["token"]
        view.model.stageEdit(token, "1.5")
        assert not view.model.commitEdits()
        assert view.model.error
        assert value.value() == 2
        assert menu.actions[1].trigger()
        assert value.value() == 1
        assert not view.model.error
        assert not view.model._drafts
    finally:
        close_panel(panel, view)
