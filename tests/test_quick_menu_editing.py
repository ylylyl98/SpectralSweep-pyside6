import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtQml import QQmlExpression, qmlContext
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLineEdit, QMenu, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

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
    panel.menu_button = QPushButton("Batch Actions")
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


def open_rendered_menu(panel, field):
    surface = next(form for form in panel._quick_parameter_forms["forms"]
                   if field in form.adapter._fields)
    button = find_item(surface.rootObject(), "parameter_" + field.fieldId)
    assert not button.property("contentItem").property("truncated")
    QTest.mouseClick(surface, Qt.MouseButton.LeftButton,
                     pos=button.mapToScene(button.boundingRect().center()).toPoint())
    QTest.qWait(30)
    expression = QQmlExpression(qmlContext(button), button, "popup")
    popup, _ = expression.evaluate()
    assert not expression.hasError()
    assert popup.property("opened")
    window = next(window for window in quick_application().topLevelWindows()
                  if window.metaObject().className() == "QQuickPopupWindow" and window.isVisible())
    assert window.isVisible()
    # Checking opened alone misses the original bug: an Item popup could be
    # entirely outside the one-row QQuickWidget's render target.
    assert window is not surface.quickWindow()
    assert window.height() > surface.height()
    return surface, button, popup, window


def click_menu_item(window, index):
    item = find_item(window.contentItem(), "parameterMenuItem_" + str(index))
    assert item is not None and item.isVisible()
    QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                     pos=item.mapToScene(item.boundingRect().center()).toPoint())
    QTest.qWait(30)


@pytest.mark.parametrize("theme,width", [("light", 360), ("dark", 800)])
def test_menu_renders_outside_short_form_and_routes_submenu_mouse_click(theme, width):
    panel, source, _, view, field = menu_table_panel()
    called = []
    disabled = panel.menu_button.menu().addAction("Unavailable")
    disabled.setToolTip("Select one or more rows first.")
    disabled.setEnabled(False)
    submenu = panel.menu_button.menu().addMenu("Rows to Run")
    submenu.addAction("Enable Only Selected Rows").triggered.connect(lambda: called.append("enabled"))
    for form in panel._quick_parameter_forms["forms"]:
        form.rootObject().setProperty("themeMode", theme)
    panel.resize(width, 450)
    # Place the trigger at the screen bottom; a native popup must remain usable.
    available = quick_application().primaryScreen().availableGeometry()
    panel.move(available.left(), available.bottom() - panel.height())
    QTest.qWait(30)
    try:
        _, _, popup, window = open_rendered_menu(panel, field)
        assert available.contains(window.geometry())
        hint = find_item(window.contentItem(), "parameterMenuHint_2")
        assert hint.isVisible() and "Select" in hint.property("text")
        click_menu_item(window, 3)
        assert len(popup.property("path").toVariant()) == 2
        click_menu_item(window, 0)
        assert called == ["enabled"]
        assert not popup.property("visible")
    finally:
        close_panel(panel, view)


def test_menu_keyboard_submenu_back_escape_and_focus_return():
    panel, _, _, view, field = menu_table_panel()
    submenu = panel.menu_button.menu().addMenu("Tools")
    submenu.addAction("Validate")
    try:
        _, button, popup, window = open_rendered_menu(panel, field)
        QTest.keyClick(window, Qt.Key.Key_End)
        QTest.keyClick(window, Qt.Key.Key_Right)
        QTest.qWait(30)
        assert len(popup.property("path").toVariant()) == 2
        QTest.keyClick(window, Qt.Key.Key_Left)
        QTest.qWait(30)
        assert len(popup.property("path").toVariant()) == 1
        QTest.keyClick(window, Qt.Key.Key_Escape)
        QTest.qWait(30)
        assert not popup.property("visible")
        assert button.hasActiveFocus()

        # Keyboard activation reaches the original QAction once, too.
        calls = []
        panel._batch_undo_action.triggered.connect(lambda: calls.append("undo"))
        _, _, popup, window = open_rendered_menu(panel, field)
        QTest.keyClick(window, Qt.Key.Key_Down)
        QTest.keyClick(window, Qt.Key.Key_Return)
        QTest.qWait(30)
        assert calls == ["undo"]
        assert not popup.property("visible")

        # Clicking outside dismisses the independent window.
        outside = QLineEdit(panel)
        panel.layout().insertWidget(0, outside)
        surface, _, popup, window = open_rendered_menu(panel, field)
        outside.setFocus()
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=QPoint(-10, -10))
        QTest.qWait(30)
        assert not popup.property("visible")
        assert panel.focusWidget() is outside
    finally:
        close_panel(panel, view)


def test_failed_menu_commit_keeps_popup_open_and_explains_error():
    panel, _, value, view, field = menu_table_panel(number=True)
    copied = []
    panel._batch_copy_action.triggered.connect(lambda: copied.append(value.value()))
    panel._batch_undo_action.triggered.connect(lambda: value.setValue(1))
    token = view.model.data(view.model.index(0, 0), view.model.CellRole)["token"]
    view.model.stageEdit(token, "1.5")
    try:
        _, _, popup, window = open_rendered_menu(panel, field)
        click_menu_item(window, 0)
        assert popup.property("visible")
        error = find_item(window.contentItem(), "parameterMenuError")
        assert error.isVisible() and error.property("text") == view.model.error
        assert not copied and value.value() == 2
        # Undo remains available to discard an invalid draft from the same menu.
        click_menu_item(window, 1)
        assert value.value() == 1
        assert not popup.property("visible")
    finally:
        close_panel(panel, view)


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
