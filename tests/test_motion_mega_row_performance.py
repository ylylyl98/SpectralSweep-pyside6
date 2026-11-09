import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
os.environ.setdefault('QT_QUICK_BACKEND','software')
from PySide6.QtTest import QTest, QSignalSpy
from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.motion_conditions_widget import MotionConditionsWidget
from ui.megasweep_panel import _OpticalSequenceWidget, OpticalCondition
from ui.quick_table_editor import QuickTableEditor


def test_motion_move_duplicate_preserve_other_items_and_undo(monkeypatch):
    quick_application()
    w = MotionConditionsWidget()
    rows = [{'values':['',str(i),'0','0','0','0','0'], 'checked':True,
             'bundle':{'provenance':{'row':i}}} for i in range(30)]
    w._restore(rows); untouched = w.table.item(20, 1)
    monkeypatch.setattr(w, '_restore', lambda *_: (_ for _ in ()).throw(AssertionError('full rebuild')))
    try:
        w.table.selectRow(4); original = w._snapshot(); bundle = w._row_bundle(4); w.reorder(1)
        assert w.table.item(20, 1) is untouched
        assert w.table.item(5, 1).text() == '4'
        assert w._row_bundle(5) == bundle
        w.duplicate()
        assert w.table.item(20, 1) is untouched
        assert w.table.item(30, 1).text() == '4'
        assert w._undo[-2] == original
        monkeypatch.undo()
        w.undo_add()
        assert w.table.rowCount() == 30 and w.table.item(5, 1).text() == '4'
        w.undo_add()
        assert w._snapshot() == original
    finally:
        w.close(); w.deleteLater(); drain_qt_deletions()


def test_optical_move_updates_quick_cached_cells_without_reset(monkeypatch):
    quick_application()
    w = _OpticalSequenceWidget()
    w.set_conditions([OpticalCondition(True,'A',700,30,1),
                      OpticalCondition(True,'B',800,40,2),
                      OpticalCondition(True,'C',900,50,3)])
    view = QuickTableEditor(w._table); view.resize(650, 240); view.show(); QTest.qWait(50)
    model = view.model
    model.watchRow(0); model.watchRow(1)
    name_col = model._columns.index(1); center_col = model._columns.index(2)
    for row in (0, 1):
        model.cellAt(row, name_col); model.cellAt(row, center_col)
    untouched = w._table.cellWidget(2, 2)
    resets = QSignalSpy(w._table.model().modelReset)
    monkeypatch.setattr(w, 'set_conditions', lambda *_: (_ for _ in ()).throw(AssertionError('full rebuild')))
    try:
        w._table.selectRow(0); w._move_selected(1); QTest.qWait(50)
        assert w._table.cellWidget(2, 2) is untouched
        assert model.cellAt(0, name_col)['value'] == 'B'
        assert model.cellAt(1, name_col)['value'] == 'A'
        assert float(model.cellAt(0, center_col)['value']) == 800
        assert float(model.cellAt(1, center_col)['value']) == 700
        assert resets.count() == 0 and not view.qml_warnings
        w._duplicate_selected(); w._remove_selected()
        assert w._table.cellWidget(2, 2) is untouched
    finally:
        view.shutdown(); view.close(); view.deleteLater(); w.close(); w.deleteLater(); drain_qt_deletions()


def test_motion_select_row_does_not_rewrite_other_rows_provenance():
    quick_application()
    w = MotionConditionsWidget()
    rows = [{'values':['',str(i),'0','0','0','0','0'], 'checked':True,
             'bundle':{'provenance':{'row':i}, 'edited':False}} for i in range(30)]
    w._restore(rows)
    before = w._snapshot()
    try:
        w.table.selectRow(4)
        assert w._snapshot() == before
    finally:
        w.close(); w.deleteLater(); drain_qt_deletions()
