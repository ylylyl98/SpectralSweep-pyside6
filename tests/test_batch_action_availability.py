import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from ui.presets_panel import PresetsPanel


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("initialize_plan", [False, True])
def test_set_all_available_after_initial_population(app, initialize_plan):
    panel = PresetsPanel(bind_controller=False, initialize_plan=initialize_plan)
    try:
        assert panel._batch_table.rowCount() > 0
        assert panel._batch_repeat_all_btn.isEnabled()
        panel._batch_repeat_spin.setValue(7)
        panel._batch_repeat_all_btn.click()
        assert all(panel._batch_row_values(row)["repeat"] == "7"
                   for row in range(panel._batch_table.rowCount()))
    finally:
        panel.close()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_restoring_empty_and_populated_session_updates_set_all(app):
    panel = PresetsPanel(bind_controller=False, initialize_plan=False)
    try:
        state = panel.capture_session_state()
        empty = dict(state, draft_batch=[])
        panel.restore_session_state(empty)
        assert panel._batch_table.rowCount() == 0
        assert not panel._batch_repeat_all_btn.isEnabled()
        panel.restore_session_state(state)
        assert panel._batch_table.rowCount() > 0
        assert panel._batch_repeat_all_btn.isEnabled()
    finally:
        panel.close()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_quick_repeat_menu_applies_selected_all_and_cancel(app, monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    from ui.quick_parameter_forms import ParameterMenuAction, WidgetsParameterAdapter
    import ui.quick_editing as editing

    panel = PresetsPanel(bind_controller=False, initialize_plan=False)
    adapter = WidgetsParameterAdapter([], visibility_root=panel)
    try:
        panel._insert_batch_rows(1, [panel._batch_row_values(0)])
        panel._batch_table.selectRow(0)
        panel._update_batch_row_buttons()
        assert panel._batch_repeat_selected_action.isEnabled()
        assert panel._batch_repeat_all_action.isEnabled()
        assert panel._batch_repeat_spin.isHidden()
        calls = []
        monkeypatch.setattr(editing, "commit_quick_edits", lambda _panel: calls.append("commit") or True)
        answers = iter([(4, True), (8, True), (11, False)])
        monkeypatch.setattr(QInputDialog, "getInt", lambda *args: next(answers))
        selected = ParameterMenuAction(panel._batch_repeat_selected_action, adapter)
        all_rows = ParameterMenuAction(panel._batch_repeat_all_action, adapter)
        assert selected.trigger()
        assert [panel._batch_row_values(i)["repeat"] for i in range(2)] == ["4", "1"]
        assert all_rows.trigger()
        assert [panel._batch_row_values(i)["repeat"] for i in range(2)] == ["8", "8"]
        assert all_rows.trigger()
        assert [panel._batch_row_values(i)["repeat"] for i in range(2)] == ["8", "8"]
        assert calls == ["commit", "commit", "commit"]
    finally:
        adapter.shutdown()
        panel.dispose_reloaded_page()
        panel.close()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
