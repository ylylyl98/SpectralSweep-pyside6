"""Quick reload keeps view choices separately from source values and edits."""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import QObject, QItemSelectionModel, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import (
    QLineEdit, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from tests.quick_helpers import drain_qt_deletions, quick_application
from ui.quick_reload_state import capture_quick_view_state, restore_quick_view_state
from ui.quick_table_editor import install_table_editors
from ui.workflow_sections import DisclosureSection


@pytest.fixture
def widgets():
    app = quick_application()
    owned = []

    def make(rows=40, quick=False):
        panel = QWidget()
        owned.append(panel)
        layout = QVBoxLayout(panel)
        panel._source_edit = QLineEdit("unchanged source", panel)
        layout.addWidget(panel._source_edit)
        section = DisclosureSection("Optional", QWidget(), expanded=False)
        section.setObjectName("QuickOptionalDisclosure")
        layout.addWidget(section)
        panel._workflow_scroll = QScrollArea(panel)
        panel._workflow_scroll.setWidgetResizable(True)
        content = QWidget()
        content.setMinimumHeight(1500)
        panel._workflow_scroll.setWidget(content)
        layout.addWidget(panel._workflow_scroll)
        panel._splitter = QSplitter(Qt.Orientation.Horizontal, panel)
        panel._splitter.addWidget(QWidget())
        panel._splitter.addWidget(QWidget())
        layout.addWidget(panel._splitter)
        panel._sweep_calc = QWidget(panel)
        calc_layout = QVBoxLayout(panel._sweep_calc)
        table = panel._sweep_calc._multi_preview = QTableWidget(rows, 2)
        for row in range(rows):
            for column in range(2):
                table.setItem(row, column, QTableWidgetItem(f"{row}:{column}"))
        table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        table.setMaximumHeight(180)
        calc_layout.addWidget(table)
        layout.addWidget(panel._sweep_calc)
        if quick:
            install_table_editors(panel)
        panel.resize(760, 780)
        panel.show()
        QTest.qWait(50)
        return panel, section

    yield make
    for panel in owned:
        for editor in getattr(panel, "_quick_table_editors", []):
            editor.shutdown()
        panel.close()
        panel.deleteLater()
    drain_qt_deletions()


def test_restores_named_disclosure_nested_table_and_scroll_without_source_signals(widgets):
    old, section = widgets()
    section.set_expanded(True)
    table = old._sweep_calc._multi_preview
    table.setCurrentCell(8, 1)
    table.selectionModel().select(table.model().index(2, 0), QItemSelectionModel.SelectionFlag.Select)
    old._workflow_scroll.verticalScrollBar().setValue(340)
    old._splitter.setSizes([510, 210])
    state = capture_quick_view_state(old)

    new, new_section = widgets()
    target = new._sweep_calc._multi_preview
    text_changed = QSignalSpy(new._source_edit.textChanged)
    selected = QSignalSpy(target.selectionModel().selectionChanged)
    current = QSignalSpy(target.currentCellChanged)
    new._quick_pane_width = SimpleNamespace(
        splitter=new._splitter, index=1, initialized=False, responsive=True,
        restored_sizes=None, restored_orientation=None, last_width=None,
    )
    restore_quick_view_state(new, state)
    # Showing the staged page can change ranges; repeated restoration is safe.
    restore_quick_view_state(new, state)
    assert new_section.toggle.isChecked() and not new_section.content.isHidden()
    assert (target.currentRow(), target.currentColumn()) == (8, 1)
    assert {(index.row(), index.column()) for index in target.selectedIndexes()} == {(8, 1), (2, 0)}
    assert new._workflow_scroll.verticalScrollBar().value() == 340
    assert new._splitter.sizes() == old._splitter.sizes()
    assert new._quick_pane_width.initialized and not new._quick_pane_width.responsive
    assert new._quick_pane_width.last_width == old._splitter.sizes()[1]
    assert new._source_edit.text() == "unchanged source"
    assert text_changed.count() == selected.count() == current.count() == 0


def test_missing_and_shorter_views_clamp_positions_and_drop_out_of_range_selection(widgets):
    old, section = widgets()
    table = old._sweep_calc._multi_preview
    table.setCurrentCell(35, 1)
    old._workflow_scroll.verticalScrollBar().setValue(1000)
    state = capture_quick_view_state(old)
    new, new_section = widgets(rows=2)
    new_section.setObjectName("RenamedDisclosure")
    new._workflow_scroll.widget().setMinimumHeight(0)
    new._workflow_scroll.widget().setFixedHeight(60)
    QTest.qWait(20)
    restore_quick_view_state(new, state)
    target = new._sweep_calc._multi_preview
    assert target.currentRow() == -1 and not target.selectedIndexes()
    assert new._workflow_scroll.verticalScrollBar().value() == new._workflow_scroll.verticalScrollBar().maximum()
    assert not new_section.toggle.isChecked()


def test_settings_category_scroll_uses_tab_owner_for_unnamed_pages(widgets):
    old, _ = widgets()
    new, _ = widgets()
    for panel in (old, new):
        panel._categories = QTabWidget(panel)
        panel.layout().addWidget(panel._categories)
        for index in range(2):
            scroll = QScrollArea()
            content = QWidget()
            content.setMinimumHeight(1700)
            scroll.setWidget(content)
            panel._categories.addTab(scroll, f"Category {index}")
        panel._categories.setCurrentIndex(1)
    QTest.qWait(30)
    old._categories.widget(1).verticalScrollBar().setValue(460)
    state = capture_quick_view_state(old)
    assert state["scrolls"]["attr:_categories.pages[1]"][1] == 460
    restore_quick_view_state(new, state)
    assert new._categories.widget(1).verticalScrollBar().value() == 460


def test_preserves_actual_quick_list_scroll_and_selection_anchor(widgets):
    old, _ = widgets(rows=60, quick=True)
    table = old._sweep_calc._multi_preview
    editor = table._quick_editor
    rows = editor.quick_widget.rootObject().findChild(QObject, "QuickEditableTable")
    assert rows is not None and rows.property("contentHeight") > rows.property("height")
    rows.setProperty("contentY", 430.0)
    editor.model.selectRow(8, False)
    editor.model._selection_anchor = 5
    state = capture_quick_view_state(old)
    assert state["quick_lists"]["attr:_sweep_calc._multi_preview"] > 400

    new, _ = widgets(rows=60, quick=True)
    target = new._sweep_calc._multi_preview._quick_editor
    restore_quick_view_state(new, state)
    restored = target.quick_widget.rootObject().findChild(QObject, "QuickEditableTable")
    assert restored.property("contentY") - restored.property("originY") == pytest.approx(430)
    assert target.model.rowSelected(8)
    assert target.model._selection_anchor == 5
    # A changed layout may be much shorter than the saved list viewport.
    shorter, _ = widgets(rows=3, quick=True)
    restore_quick_view_state(shorter, state)
    shorter_rows = shorter._sweep_calc._multi_preview._quick_editor.quick_widget.rootObject().findChild(QObject, "QuickEditableTable")
    extent = max(0, shorter_rows.property("contentHeight") - shorter_rows.property("height"))
    assert shorter_rows.property("contentY") - shorter_rows.property("originY") == pytest.approx(min(430, extent))
