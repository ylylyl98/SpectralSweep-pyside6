import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFileDialog, QLineEdit, QWidget

from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.quick_dialogs import install_dialog_migration
from ui.setup_sequence_editor import SetupSequenceDialog
from ui.loop_run_selection import LoopSelectionDialog
from ui.broad_calibration_dialog import BroadCalibrationDialog
from ui.motion_sequence_preview import MotionSequencePreviewDialog
from tests.test_quick_parameter_forms import find_item


@pytest.fixture
def owned_dialogs():
    quick_application()
    window = QWidget()
    manager = install_dialog_migration(window)
    yield window, manager
    manager.shutdown()
    window.close()
    window.deleteLater()
    drain_qt_deletions()


def source_field(entry, source):
    return next(field for view in entry["forms"]["forms"] for field, widget in view.adapter._fields.items() if widget is source)


def click_source_button(entry, source):
    view = next(view for view in entry["forms"]["forms"] if source in view.adapter._fields.values())
    field = source_field(entry, source)
    item = find_item(view.rootObject(), "parameter_" + field.fieldId)
    QTest.mouseClick(view, Qt.MouseButton.LeftButton,
                     pos=item.mapToScene(item.boundingRect().center()).toPoint())


def test_setup_actual_editor_accept_cancel_dynamic_rows_and_reopen(owned_dialogs):
    window, manager = owned_dialogs
    dialog = SetupSequenceDialog(["a"], {"a": "Setup A", "b": "Setup B"}, window)
    dialog.show()
    QTest.qWait(30)
    entry = manager._entries[id(dialog)]
    field = source_field(entry, dialog.rows[0].combo)
    assert field.select(1)
    assert dialog.selected_backends() == ["b"]
    count = len(entry["forms"]["forms"])
    dialog._add_row("a")
    QTest.qWait(30)
    entry = manager._entries[id(dialog)]
    assert source_field(entry, dialog.rows[1].combo)
    removed = dialog.rows[1]
    dialog._remove(removed)
    drain_qt_deletions()
    QTest.qWait(10)
    click_source_button(entry, dialog.buttons.button(QDialogButtonBox.StandardButton.Ok))
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert not dialog.isVisible()
    dialog.show()
    QTest.qWait(20)
    assert len(manager._entries[id(dialog)]["forms"]["forms"]) >= count
    click_source_button(entry, dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel))
    assert dialog.result() == QDialog.DialogCode.Rejected
    dialog.deleteLater()
    drain_qt_deletions()
    assert id(dialog) not in manager._entries


def test_unfinished_calibration_draft_cancel_never_writes_source_and_reopens(owned_dialogs):
    window, manager = owned_dialogs
    dialog = BroadCalibrationDialog(window)
    dialog.show()
    QTest.qWait(30)
    entry = manager._entries[id(dialog)]
    field = source_field(entry, dialog.tolerance)
    initial = dialog.tolerance.value()
    dialog.tolerance.setKeyboardTracking(False)
    view = next(view for view in entry["forms"]["forms"] if dialog.tolerance in view.adapter._fields.values())
    item = find_item(view.rootObject(), "parameter_" + field.fieldId)
    QTest.mouseClick(view, Qt.MouseButton.LeftButton,
                     pos=item.mapToScene(item.boundingRect().center()).toPoint())
    QTest.keyClick(view, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(view, "0.555")
    assert dialog.tolerance.value() == initial
    dialog.reject()
    QTest.qWait(10)
    assert dialog.tolerance.value() == initial
    assert not field.invalid
    assert field.text == dialog.tolerance.cleanText()
    forms = tuple(entry["forms"]["forms"])
    dialog.show()
    QTest.qWait(20)
    assert tuple(manager._entries[id(dialog)]["forms"]["forms"]) == forms
    assert field.commit("0.333")
    assert dialog.tolerance.value() == 0.333
    dialog.close()
    dialog.deleteLater()
    drain_qt_deletions()


def test_loop_value_picker_preserves_filter_model_check_validation(owned_dialogs):
    window, manager = owned_dialogs
    dialog = LoopSelectionDialog(["red", "blue", "green"], [0], window)
    dialog.show()
    QTest.qWait(20)
    entry = manager._entries[id(dialog)]
    model = entry["models"][0]
    assert model.rowCount() == 3
    assert model.toggle(1)
    assert dialog.positions() == [0, 1]
    search = dialog.findChild(QLineEdit)
    field = source_field(entry, search)
    assert field.edit("blue")
    assert model.rowCount() == 1
    assert model.data(model.index(0), model.TextRole) == "blue"
    assert model.toggle(0)
    assert dialog.positions() == [0]
    dialog.reject()
    dialog.deleteLater()
    drain_qt_deletions()


def test_motion_existing_dialog_keeps_model_result_and_accept_path(owned_dialogs):
    window, manager = owned_dialogs
    dialog = MotionSequencePreviewDialog([{"Vbg": 1., "Vtg": 2., "Vbias": 0.}], parent=window)
    original_model = dialog.sequence_model
    dialog.show()
    QTest.qWait(20)
    entry = manager._entries[id(dialog)]
    assert dialog.sequence_model is original_model
    assert entry["extras"][0].rootObject().property("sequenceModel") is original_model
    entry["models"][0].applySelection()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert len(dialog.result_rows()) == 1
    dialog.deleteLater()
    drain_qt_deletions()


def test_native_and_unowned_dialogs_are_outside_scope(owned_dialogs):
    window, manager = owned_dialogs
    native = QFileDialog(window)
    arbitrary = QDialog(window)
    unowned = SetupSequenceDialog(["a"], {"a": "A"})
    assert manager.migrate(native) is None
    assert manager.migrate(arbitrary) is None
    assert manager.migrate(unowned) is None
    native.deleteLater()
    arbitrary.deleteLater()
    unowned.deleteLater()
    drain_qt_deletions()
