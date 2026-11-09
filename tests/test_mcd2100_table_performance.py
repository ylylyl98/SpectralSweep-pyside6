import os
from types import SimpleNamespace
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt
from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.mcd2100_panel import MCD2100Panel


def test_row_operations_do_not_rebuild_existing_rows(monkeypatch):
    quick_application()
    panel = MCD2100Panel(SimpleNamespace(state=SimpleNamespace(name='DISCONNECTED'), generation=0))
    rows = [{'mode': 'direct', 'input_a': i / 100, 'input_b': 0,
             'vbias_v': 0, 'enabled': True} for i in range(100)]
    panel._seed_condition_table(rows)
    panel._update_condition_editable()
    table = panel._condition_table
    untouched = table.item(80, 8)
    table.selectRow(20)
    monkeypatch.setattr(panel, '_seed_condition_table', lambda *_: (_ for _ in ()).throw(AssertionError('full rebuild')))
    try:
        panel._move_condition(1)
        assert table.item(80, 8) is untouched
        assert panel._condition_rows()[21]['input_a'] == .2
        assert table.currentRow() == 21
        panel._remove_condition_row()
        assert table.item(79, 8) is untouched
        panel._add_condition_row()
        assert table.item(79, 8) is untouched
        assert all(not table.item(i, 1).flags() & Qt.ItemFlag.ItemIsEditable for i in range(table.rowCount()))
        assert [table.item(i, 1).text() for i in range(table.rowCount())] == [str(i + 1) for i in range(table.rowCount())]
    finally:
        panel.shutdown(timeout_ms=0); panel.close(); panel.deleteLater(); drain_qt_deletions()


def test_disabled_only_row_remains_editable_without_reenabling():
    quick_application()
    panel = MCD2100Panel(SimpleNamespace(state=SimpleNamespace(name='DISCONNECTED'), generation=0))
    try:
        panel._condition_table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        panel._condition_table.selectRow(0)
        panel._edit_selected_condition()
        assert panel._gate_edit_row == 0
        panel._gate_entry_a.setText('0.25')
        panel._commit_gate_entry()
        assert panel._condition_table.item(0, 0).checkState() == Qt.CheckState.Unchecked
        assert float(panel._condition_table.item(0, 8).text()) == .25
        assert '0 enabled' in panel._condition_summary.text()
    finally:
        panel.shutdown(timeout_ms=0); panel.close(); panel.deleteLater(); drain_qt_deletions()


def test_quick_cached_modes_follow_row_move():
    from PySide6.QtTest import QTest
    from ui.quick_table_editor import QuickTableEditor
    quick_application()
    panel = MCD2100Panel(SimpleNamespace(state=SimpleNamespace(name='DISCONNECTED'), generation=0))
    panel._seed_condition_table([
        {'mode': 'direct', 'input_a': 1, 'input_b': 0, 'vbias_v': 0, 'enabled': True},
        {'mode': 'doping_efield', 'input_a': 2, 'input_b': 0, 'vbias_v': 0, 'enabled': True},
    ])
    view = QuickTableEditor(panel._condition_table)
    view.resize(600, 250); view.show(); QTest.qWait(40)
    model = view.model
    col = model._columns.index(2)
    model.watchRow(0); model.watchRow(1)
    first, second = model.cellAt(0, col), model.cellAt(1, col)
    try:
        assert first['value'] != second['value']
        panel._condition_table.selectRow(0)
        panel._move_condition(1)
        QTest.qWait(50)
        assert model.cellAt(0, col)['value'] == second['value']
        assert model.cellAt(1, col)['value'] == first['value']
        assert model.cellAt(0, col)['kind'] == model.cellAt(1, col)['kind'] == 'text'
        assert not view.qml_warnings
    finally:
        view.shutdown(); view.close(); view.deleteLater()
        panel.shutdown(timeout_ms=0); panel.close(); panel.deleteLater(); drain_qt_deletions()
