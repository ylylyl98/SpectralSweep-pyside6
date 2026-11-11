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
