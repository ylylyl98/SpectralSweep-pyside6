from types import MethodType
from unittest.mock import Mock

from PySide6.QtWidgets import QApplication, QLineEdit

from tests.test_dual_gate_page_reload import shell
from ui import page_reload
from ui.main_window import MainWindow, _SharedSampleIdBinder


def test_dual_gate_reload_rebinds_sample_id_without_committing_draft(shell):
    old = shell._presets
    other = QLineEdit(shell)
    shell._sample_id_binder = _SharedSampleIdBinder(
        [old._sample_edit, other], initial="saved sample", parent=shell, commit_on_edit=True)
    shell._commit_sample_id_edit = Mock()
    edit = old._sample_edit
    shell._sample_edit_connections = {
        edit: edit.editingFinished.connect(lambda: shell._commit_sample_id_edit(edit))}
    shell._rebind_reloaded_page = MethodType(MainWindow._rebind_reloaded_page, shell)
    edit.setText("unfinished sample draft")
    edit.setFocus()
    QApplication.instance().processEvents()
    shell._commit_sample_id_edit.reset_mock()

    new = page_reload.reload_dual_gate_page(shell)
    assert shell._sample_id_binder._edits == [new._sample_edit, other]
    assert shell._sample_id_binder.value == "saved sample"
    assert new._sample_edit.text() == "unfinished sample draft"
    shell._commit_sample_id_edit.assert_not_called()
    edit.editingFinished.emit()
    shell._commit_sample_id_edit.assert_not_called()
    new._sample_edit.editingFinished.emit()
    shell._commit_sample_id_edit.assert_called_once_with(new._sample_edit)
    shell._sample_id_binder.commit("next sample")
    assert other.text() == new._sample_edit.text() == "next sample"
    assert edit.text() == "unfinished sample draft"
