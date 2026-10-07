"""Real mouse/keyboard editing, including Qt's transient Values editor."""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from ui.presets_panel import PresetsPanel


@pytest.fixture(scope='module', autouse=True)
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def widget(app):
    result = PresetsPanel()
    result._load_dual_setup_recipe()
    result.resize(1500, 1000)
    result.show()
    result.activateWindow()
    app.processEvents()
    yield result
    result.close()


def double_click_cell(table, row, column):
    item = table.item(row, column)
    table.scrollToItem(item)
    QApplication.processEvents()
    point = table.visualItemRect(item).center()
    QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QApplication.processEvents()
    editor = QApplication.focusWidget()
    assert isinstance(editor, QLineEdit), f'Cell ({row}, {column}) did not open for editing'
    return editor


@pytest.mark.parametrize('next_column', [2, 3])
@pytest.mark.parametrize('phase', ['initial', 'moved_and_applied', 'discarded'])
def test_committing_values_does_not_block_next_double_click(widget, next_column, phase):
    table = widget._loop_table
    center_row = 1
    if phase == 'moved_and_applied':
        table.selectRow(1)
        widget._loop_up_btn.click()
        widget._on_apply()
        center_row = 0
    elif phase == 'discarded':
        widget._on_apply()
        table.item(1, 2).setText('800, 1200')
        widget._on_discard()

    editor = double_click_cell(table, center_row, 2)
    QTest.keyClick(editor, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(editor, '735, 1110')
    QTest.keyClick(editor, Qt.Key.Key_Return)
    QApplication.processEvents()
    assert table.item(center_row, 2).text() == '735, 1110'

    editor = double_click_cell(table, center_row, next_column)
    QTest.keyClick(editor, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    replacement = '740, 1120' if next_column == 2 else '2'
    QTest.keyClicks(editor, replacement)
    QTest.keyClick(editor, Qt.Key.Key_Return)
    QApplication.processEvents()
    assert table.item(center_row, next_column).text() == replacement


def test_tab_after_values_commit_opens_group_editor(widget):
    table = widget._loop_table
    editor = double_click_cell(table, 1, 2)
    QTest.keyClick(editor, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(editor, '735, 1110')
    QTest.keyClick(editor, Qt.Key.Key_Tab)
    QApplication.processEvents()
    group_editor = QApplication.focusWidget()
    assert isinstance(group_editor, QLineEdit)
    assert table.currentColumn() == 3
    QTest.keyClick(group_editor, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(group_editor, '2')
    QTest.keyClick(group_editor, Qt.Key.Key_Return)
    QApplication.processEvents()
    assert table.item(1, 2).text() == '735, 1110'
    assert table.item(1, 3).text() == '2'
