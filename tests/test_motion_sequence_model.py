"""Device-free ownership, Qt model and bounded-work contracts."""
import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QModelIndex, QPersistentModelIndex, Qt, QUrl
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtTest import QAbstractItemModelTester, QSignalSpy
from PySide6.QtWidgets import QApplication

from app.motion_sequence_model import MAX_SEQUENCE_ROWS, MotionSequenceModel


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def rows():
    return [{"source_index": 40 + i, "repeat": i + 1, "rotation": 10.0 + i,
             "context": {"values": [i]}} for i in range(5)]


def test_owned_input_and_each_export_are_independent(app):
    original = rows()
    model = MotionSequenceModel(original, point_count=8)
    original[0]["context"]["values"].append(99)
    exported = model.selected_rows()
    assert exported[0]["context"]["values"] == [0]
    assert exported[0]["point_count"] == 8
    assert exported[0]["sequence"] == 1
    exported[0]["context"]["values"].append(98)
    one = model.row_data(0)
    assert one["context"]["values"] == [0]
    one["context"]["values"].append(97)
    assert model.row_data(0)["context"]["values"] == [0]
    assert model.selected_indices() == [40, 41, 42, 43, 44]
    assert model.row_data(-1) == model.row_data(5) == {}


def test_roles_headers_checkbox_and_noop_notifications(app):
    model = MotionSequenceModel(rows())
    assert model.roleNames()[model.CheckedRole] == b"checked"
    assert model.roleNames()[model.SourceIndexRole] == b"sourceIndex"
    assert model.data(model.index(0, 9)) == "10"
    assert model.data(model.index(0, 0), Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    assert model.headerData(2, Qt.Orientation.Horizontal) == "Cond.\n#"
    assert model.rowCount(model.index(0, 0)) == 0
    assert model.columnCount(model.index(0, 0)) == 0
    changed, selected = QSignalSpy(model.dataChanged), QSignalSpy(model.selectionChanged)
    assert not model.setData(model.index(0, 1), Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
    assert not model.setData(model.index(0, 0), Qt.CheckState.PartiallyChecked, Qt.ItemDataRole.CheckStateRole)
    assert model.setData(model.index(0, 0), 0, Qt.ItemDataRole.CheckStateRole)
    assert model.selectedCount == 4
    assert not model.data(model.index(0, 3), model.CheckedRole)
    assert model.selected_indices() == [41, 42, 43, 44]
    assert model.set_checked(0, False)
    assert changed.count() == selected.count() == 1
    model.set_all_checked(False)
    model.set_all_checked(False)
    assert model.selectedCount == 0
    assert changed.count() == selected.count() == 2
    model.set_all_checked(True)
    assert model.selectedCount == 5
    assert changed.count() == selected.count() == 3


def test_cell_roles_never_expose_mutable_owned_fields(app):
    model = MotionSequenceModel([{"label": ["unusual", "label"]}])
    value = model.data(model.index(0, 3), model.ValueRole)
    assert isinstance(value, str)
    assert model.row_data(0)["label"] == ["unusual", "label"]


def test_visible_record_fields_are_complete_scalar_reads_without_payload_copy(app):
    model = MotionSequenceModel([{"label": "Long condition", "doping_v": 1.25,
                                  "rotation": -12.5, "repeat": 7, "context": {"opaque": [1, 2]}}])
    with patch("app.motion_sequence_model.copy.deepcopy", side_effect=AssertionError("No delegate payload export")):
        fields = model.displayFields(0)
    assert len(fields) == 13
    assert [field["column"] for field in fields] == list(range(13))
    assert [field["label"] for field in fields] == ["Use", "Sequence", "Condition", "Label", "D", "F", "Vtg", "Vbg", "Vbias", "Rot1", "Rot2", "Repeat", "Points"]
    assert fields[3]["text"] == "Long condition"
    assert fields[4]["text"] == "1.25" and fields[9]["text"] == "-12.5"
    assert fields[11]["text"] == "7"
    fields[3]["text"] = "changed returned presentation"
    assert model.data(model.index(0, 3)) == "Long condition"
    assert model.displayFields(-1) == model.displayFields(1) == []


def test_move_rows_matches_qt_insertion_coordinates_and_persistent_indexes(app):
    model = MotionSequenceModel(rows())
    tester = QAbstractItemModelTester(model, QAbstractItemModelTester.FailureReportingMode.Fatal)
    first = QPersistentModelIndex(model.index(0, 1))
    last = QPersistentModelIndex(model.index(4, 1))
    moved, resets = QSignalSpy(model.rowsMoved), QSignalSpy(model.modelReset)
    assert model.move_entry(0, 4)
    assert model.selected_indices() == [41, 42, 43, 44, 40]
    assert first.row() == 4 and last.row() == 3
    assert model.move_entry(4, 0)
    assert first.row() == 0 and last.row() == 4
    assert model.moveRows(QModelIndex(), 0, 2, QModelIndex(), 5)
    assert model.selected_indices() == [42, 43, 44, 40, 41]
    assert model.moveRows(QModelIndex(), 3, 2, QModelIndex(), 0)
    assert model.selected_indices() == [40, 41, 42, 43, 44]
    assert moved.count() == 4 and resets.count() == 0
    assert not model.move_entry(2, 2)
    assert not model.move_entry(-1, 0)
    assert not model.move_entry(0, 5)
    assert not model.moveRows(QModelIndex(), 1, 2, QModelIndex(), 2)
    assert not model.moveRows(model.index(0, 0), 0, 1, QModelIndex(), 4)
    assert tester.model() is model


def test_100000_rows_copy_once_then_checkbox_counts_and_moves_copy_no_payloads(app):
    class Payload:
        copies = 0

        def __deepcopy__(self, memo):
            type(self).copies += 1
            return type(self)()

    # One row beyond the cap must not even be normalized/copied.
    source = [{"payload": Payload(), "repeat": 2}] * (MAX_SEQUENCE_ROWS + 1)
    model = MotionSequenceModel(source)
    assert model.count == model.selectedCount == MAX_SEQUENCE_ROWS
    assert model.truncated
    assert Payload.copies == MAX_SEQUENCE_ROWS
    moved, resets = QSignalSpy(model.rowsMoved), QSignalSpy(model.modelReset)
    with patch("app.motion_sequence_model.copy.deepcopy", side_effect=AssertionError("unexpected row copy")):
        assert model.set_checked(50000, False)
        assert model.selectedCount == MAX_SEQUENCE_ROWS - 1
        assert model.move_entry(MAX_SEQUENCE_ROWS - 1, 0)
        assert model.data(model.index(0, 1), model.SourceIndexRole) == MAX_SEQUENCE_ROWS - 1
        assert not model.is_checked(50001)
        assert len(model.selected_indices()) == MAX_SEQUENCE_ROWS - 1
        model.set_all_checked(False)
        assert model.selectedCount == 0
        model.set_all_checked(True)
        assert model.selectedCount == MAX_SEQUENCE_ROWS
    assert Payload.copies == MAX_SEQUENCE_ROWS
    assert moved.count() == 1 and resets.count() == 0


def test_qml_invokes_public_properties_and_slots(app):
    model = MotionSequenceModel(rows())
    engine = QQmlEngine()
    engine.rootContext().setContextProperty("sequenceModel", model)
    component = QQmlComponent(engine)
    component.setData(b'''import QtQml
QtObject {
    property int chosen: sequenceModel.selectedCount
    property int total: sequenceModel.count
    property bool capped: sequenceModel.truncated
    property bool success: false
    Component.onCompleted: {
        sequenceModel.setChecked(0, false)
        success = !sequenceModel.isChecked(0) && sequenceModel.moveEntry(4, 0)
        var result = sequenceModel.rowData(0)
        success = success && result._source_index === 44 && result.repeat === 5
        success = success && sequenceModel.selectedIndices()[0] === 44
        success = success && sequenceModel.selectedRows().length === 4
        sequenceModel.setAllChecked(true)
    }
}''', QUrl())
    obj = component.create()
    assert obj is not None, [error.toString() for error in component.errors()]
    assert obj.property("success")
    assert obj.property("total") == obj.property("chosen") == 5
    assert not obj.property("capped")
    obj.deleteLater()


def test_empty_model_is_valid(app):
    model = MotionSequenceModel()
    assert model.count == model.selectedCount == 0
    assert not model.truncated
    assert not model.set_checked(0, True)
    assert not model.move_entry(0, 0)
    model.set_all_checked(True)
    assert model.selected_rows() == model.selected_indices() == []
