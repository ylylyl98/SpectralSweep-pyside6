"""Real QML model/view selection without controllers or device access."""
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import Q_ARG, QMetaObject, QObject, QPoint, QPointF, Qt
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from ui.motion_sequence_preview import MotionSequencePreviewDialog, create_motion_sequence_preview
from ui.quick_motion_sequence import QuickMotionSequencePreviewDialog


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def rows():
    return [{"sequence": 10 + i, "source_index": 70 + i, "repeat": 2 + i,
             "label": f"Condition {i}", "context": {"values": [i]}} for i in range(3)]


def invoke(root, method, *args):
    assert QMetaObject.invokeMethod(root, method, Qt.ConnectionType.DirectConnection,
                                    *[Q_ARG(int, arg) for arg in args])


def show(dialog):
    dialog.show()
    QTest.qWait(30)
    assert dialog.quick_widget.status() == QQuickWidget.Status.Ready, dialog.qml_warnings
    return dialog.quick_widget.rootObject()


def dispose(dialog):
    dialog.close()
    dialog.deleteLater()
    QTest.qWait(10)


def visible_item(root, name):
    # ListView owns delegates through its reuse pool; follow visual parenting.
    if root.objectName() == name and root.isVisible():
        return root
    for child in root.childItems():
        found = visible_item(child, name)
        if found is not None:
            return found
    return None


def click_item(dialog, item):
    point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
    assert 0 <= point.x() < dialog.quick_widget.width() and 0 <= point.y() < dialog.quick_widget.height()
    QTest.mouseClick(dialog.quick_widget, Qt.MouseButton.LeftButton, pos=point)


def test_all_thirteen_fields_wrap_in_one_narrow_view_without_field_switching(app):
    sample = {"sequence": 10, "condition_index": 33, "label": "532 nm optical condition with calibrated gate and motion targets",
              "D": 1.2345678, "F": -2.5, "Vtg": 3.14, "Vbg": -4.2, "Vbias": .01,
              "rot1": 22.5, "rot2": -10.75, "repeat": 9, "point_count": 12345}
    dialog = QuickMotionSequencePreviewDialog([sample, {**sample, "label": "Second", "repeat": 2}])
    dialog.resize(520, 540)
    try:
        root = show(dialog)
        view = root.findChild(QObject, "MotionSequenceTable")
        assert root.findChild(QObject, "MotionFieldView") is None
        assert root.metaObject().indexOfProperty("fieldView") < 0
        assert view.property("contentWidth") <= view.width() + 1
        row = visible_item(root, "MotionSequenceRow_0")
        assert row is not None
        for column in range(13):
            field = visible_item(root, f"MotionField_0_{column}")
            assert field is not None
            left = field.mapToItem(row, QPointF(0, 0)).x()
            assert left >= 0 and left + field.width() <= row.width() + 1
            assert field.width() >= 90
            if column:
                label = visible_item(root, f"MotionFieldLabel_0_{column}")
                value = visible_item(root, f"MotionFieldValue_0_{column}")
                assert label.property("text") == dialog.sequence_model.displayFields(0)[column]["label"]
                assert value.property("text") == dialog.sequence_model.data(dialog.sequence_model.index(0, column))
                assert value.height() >= value.implicitHeight() - 1
        assert visible_item(root, "MotionFieldValue_0_3").height() > 20
        assert not dialog.qml_warnings
    finally:
        dispose(dialog)


def test_mouse_move_buttons_keep_complete_record_values_and_execution_order(app):
    dialog = QuickMotionSequencePreviewDialog(rows())
    dialog.resize(520, 540)
    try:
        root = show(dialog)
        invoke(root, "selectRow", 2)
        QTest.qWait(20)
        click_item(dialog, root.findChild(QObject, "MoveMotionUp"))
        assert dialog.currentRow == 1
        assert dialog.selected_indices() == [70, 72, 71]
        QTest.qWait(20)
        assert visible_item(root, "MotionFieldValue_1_3").property("text") == "Condition 2"
        assert visible_item(root, "MotionFieldValue_1_11").property("text") == "4"
        click_item(dialog, root.findChild(QObject, "ApplyMotionSelection"))
        assert dialog.result_indices() == [70, 72, 71]
        assert [row["repeat"] for row in dialog.result_rows()] == [2, 4, 3]
    finally:
        dispose(dialog)


def test_real_qml_selection_order_repeat_and_apply_result(app):
    source = rows()
    dialog = QuickMotionSequencePreviewDialog(source, point_count=8)
    try:
        root = show(dialog)
        assert root.objectName() == "MotionSequence"
        assert root.property("liveCellCount") > 0
        applied = QSignalSpy(dialog.selectionApplied)
        invoke(root, "toggleRow", 1)
        assert dialog.selected_indices() == [70, 72]
        invoke(root, "selectRow", 2)
        invoke(root, "moveSelection", -1)
        assert dialog.currentRow == 1
        assert dialog.selected_indices() == [70, 72]
        invoke(root, "moveSelection", -1)
        assert dialog.selected_indices() == [72, 70]
        invoke(root, "applySelection")
        assert dialog.result() == 1 and applied.count() == 1
        assert dialog.result_indices() == [72, 70]
        assert [row["repeat"] for row in dialog.result_rows()] == [4, 2]
        assert dialog.result_rows()[0]["point_count"] == 8
        exported = dialog.result_rows()
        exported[0]["context"]["values"].append(99)
        dialog.sequence_model.set_checked(2, True)
        assert dialog.result_rows()[0]["context"]["values"] == [2]
        assert dialog.result_indices() == [72, 70]
        assert "enabled" not in source[0]
        assert not dialog.qml_warnings
    finally:
        dispose(dialog)


def test_clear_empty_apply_select_all_and_cancel_are_qml_actions(app):
    dialog = QuickMotionSequencePreviewDialog(rows())
    try:
        root = show(dialog)
        applied = QSignalSpy(dialog.selectionApplied)
        invoke(root, "clearSelection")
        invoke(root, "applySelection")
        assert "at least one" in dialog.statusText
        assert dialog.result() == 0 and applied.count() == 0
        invoke(root, "selectAll")
        assert dialog.sequence_model.selectedCount == 3 and dialog.statusText == ""
        invoke(root, "cancelSelection")
        assert dialog.result() == 0 and applied.count() == 0
        assert not dialog.qml_warnings
    finally:
        dispose(dialog)


def test_keyboard_and_themes_at_narrow_size(app):
    dialog = QuickMotionSequencePreviewDialog(rows())
    dialog.resize(520, 500)
    try:
        root = show(dialog)
        table = root.findChild(QObject, "MotionSequenceTable")
        table.forceActiveFocus()
        QTest.keyClick(dialog.quick_widget, Qt.Key.Key_Down)
        assert dialog.currentRow == 1
        QTest.keyClick(dialog.quick_widget, Qt.Key.Key_Space)
        assert dialog.selected_indices() == [70, 72]
        dialog.set_theme("light")
        QTest.qWait(20)
        light = dialog.quick_widget.grabFramebuffer()
        dialog.set_theme("dark")
        QTest.qWait(20)
        dark = dialog.quick_widget.grabFramebuffer()
        assert not light.isNull() and not dark.isNull()
        assert dark.pixelColor(1, 1).lightness() < light.pixelColor(1, 1).lightness()
        assert not dialog.qml_warnings
    finally:
        dispose(dialog)


def test_visible_checkbox_mouse_click_updates_model_and_rebound_state(app):
    dialog = QuickMotionSequencePreviewDialog(rows())
    try:
        root = show(dialog)
        checkbox = visible_item(root, "MotionUse_0")
        assert checkbox is not None
        click_item(dialog, checkbox)
        assert not dialog.sequence_model.is_checked(0)
        invoke(root, "selectAll")
        QTest.qWait(10)
        assert checkbox.property("checked")
        assert not dialog.qml_warnings
    finally:
        dispose(dialog)


def test_100000_rows_virtualize_and_interactions_never_copy_payloads(app):
    class Payload:
        copies = 0

        def __deepcopy__(self, memo):
            type(self).copies += 1
            return type(self)()

    source = [{"payload": Payload(), "repeat": 2}] * 100000
    dialog = QuickMotionSequencePreviewDialog(source)
    try:
        assert Payload.copies == 100000
        with patch("app.motion_sequence_model.copy.deepcopy", side_effect=AssertionError("unexpected QML payload copy")):
            root = show(dialog)
            assert 0 < root.property("liveCellCount") < 1000
            invoke(root, "selectRow", 99999)
            QTest.qWait(20)
            invoke(root, "toggleRow", 99999)
            invoke(root, "moveSelection", -1)
            assert dialog.currentRow == 99998
            assert dialog.sequence_model.selectedCount == 99999
            assert not dialog.sequence_model.is_checked(99998)
            invoke(root, "clearSelection")
            invoke(root, "selectAll")
            assert dialog.sequence_model.selectedCount == 100000
            assert 0 < root.property("liveCellCount") < 1000
            view = root.findChild(QObject, "MotionSequenceTable")
            assert view.property("contentHeight") > view.height()
            assert view.property("contentWidth") <= view.width() + 1
            assert visible_item(root, "MotionField_99998_12") is not None
        assert Payload.copies == 100000
        assert not dialog.qml_warnings
    finally:
        dispose(dialog)


def test_factory_preserves_widgets_default_and_selects_quick_only_when_enabled(app):
    previous = app.property("spectralQuick")
    legacy = quick = None
    try:
        app.setProperty("spectralQuick", False)
        legacy = create_motion_sequence_preview(rows())
        assert isinstance(legacy, MotionSequencePreviewDialog)
        app.setProperty("spectralQuick", True)
        quick = create_motion_sequence_preview(rows())
        assert isinstance(quick, QuickMotionSequencePreviewDialog)
        assert quick.quick_widget.status() == QQuickWidget.Status.Ready
    finally:
        app.setProperty("spectralQuick", previous)
        if legacy is not None:
            legacy.close()
        if quick is not None:
            dispose(quick)


@pytest.mark.skipif(os.name != "nt", reason="Windows native scaling verification")
@pytest.mark.parametrize("scale", ["1", "1.5", "2"])
def test_native_hidden_dialog_layout_and_render_at_multiple_scales(tmp_path, scale):
    script = r'''
import sys
from pathlib import Path
from PySide6.QtCore import QObject, QPointF, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from ui.quick_motion_sequence import QuickMotionSequencePreviewDialog
app=QApplication([])
assert app.platformName() == 'windows'
assert 'Segoe UI' in QFontDatabase.families()
dialog=QuickMotionSequencePreviewDialog([{'label': 'Optical condition '+str(i)+' with complete gate and motion targets', 'repeat': 2, 'D': 1.25, 'F': -2.5, 'Vtg': 3.14, 'Vbg': -4.2, 'Vbias': .01, 'rot1': 22.5, 'rot2': -10.75, 'point_count': 12345} for i in range(30)])
dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen,True)
dialog.resize(520,540)
dialog.show()
root=dialog.quick_widget.rootObject()
def find(item,name):
 if item.objectName()==name and item.isVisible(): return item
 for child in item.childItems():
  result=find(child,name)
  if result is not None:return result
for theme in ('light','dark'):
 dialog.set_theme(theme);QTest.qWait(80)
 table=root.findChild(QObject,'MotionSequenceTable')
 assert root.findChild(QObject,'MotionFieldView') is None
 assert root.metaObject().indexOfProperty('fieldView')<0
 assert table.property('contentWidth')<=table.property('width')+1
 assert table.property('contentHeight')>table.height()
 for column in range(13):
  item=find(root,'MotionField_0_'+str(column))
  assert item is not None,column
  left=item.mapToScene(QPointF(0,0)).x()
  assert left>=0 and left+item.width()<=root.width()+1,(column,left,item.width())
  if column:
   value=find(root,'MotionFieldValue_0_'+str(column))
   assert value.property('text')==dialog.sequence_model.data(dialog.sequence_model.index(0,column))
   assert value.height()>=value.implicitHeight()-1
 image=dialog.quick_widget.grabFramebuffer()
 assert not image.isNull()
 assert image.save(str(Path(sys.argv[1])/('motion-'+theme+'.png')))
 for name in ('ApplyMotionSelection','CancelMotionSelection','MoveMotionUp','MoveMotionDown','SelectAllMotion','ClearMotionSelection'):
  item=root.findChild(QObject,name)
  top=item.mapToScene(QPointF(0,0))
  bottom=item.mapToScene(QPointF(item.width(),item.height()))
  assert top.x()>=0 and top.y()>=0,(name,top)
  assert bottom.x()<=root.width() and bottom.y()<=root.height(),(name,bottom,root.size())
assert 0<root.property('liveCellCount')<1000
assert not dialog.qml_warnings,dialog.qml_warnings
dialog.close();dialog.deleteLater();QTest.qWait(10)
'''
    environment = dict(os.environ, QT_QPA_PLATFORM="windows", QT_QUICK_BACKEND="software",
                       QT_SCALE_FACTOR=scale, SPECTRALSWEEP_HISTORY_PATH=str(tmp_path / "history.sqlite"))
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                            cwd=Path(__file__).resolve().parents[1], env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "motion-light.png").is_file()
    assert (tmp_path / "motion-dark.png").is_file()
