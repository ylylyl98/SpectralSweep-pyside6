"""The real Dual Gate editor feeds Quick review, without starting any device."""
import os
from copy import deepcopy

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtTest import QTest

from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.presets_panel import PresetsPanel
from ui.quick_dual_gate_sequence import QuickDualGateSequencePreview
from ui.dual_gate_reload_state import capture_dual_gate_state, restore_dual_gate_state, dual_gate_reload_block_reason
from utils.config import cfg


@pytest.fixture
def panel(monkeypatch):
    app = quick_application()
    old = app.property("spectralQuick")
    app.setProperty("spectralQuick", True)
    monkeypatch.setattr(cfg, "save", lambda: None)
    widget = PresetsPanel()
    widget.resize(1400, 1000)
    widget.show()
    app.processEvents()
    yield widget
    widget.dispose_reloaded_page()
    widget.close()
    widget.deleteLater()
    drain_qt_deletions()
    app.setProperty("spectralQuick", old)


def test_real_editor_feeds_draft_applied_and_invalid_states_without_starting(panel):
    assert isinstance(panel._tree, QuickDualGateSequencePreview)
    assert panel._tree.sequence_model.previewState == "Applied sequence"
    original = panel._acquisition_schedule
    panel._loop_table.item(0, 2).setText("750,810")
    panel._refresh_draft_state()
    assert panel._tree.sequence_model.previewState == "Draft sequence"
    assert panel._acquisition_schedule is original
    panel._on_apply()
    assert panel._tree.sequence_model.previewState == "Applied sequence"
    applied = panel._acquisition_schedule
    assert applied is not original
    panel._loop_table.item(0, 2).setText("invalid(")
    panel._refresh_draft_state()
    assert "draft unavailable" in panel._tree.sequence_model.previewState
    assert panel._acquisition_schedule is applied
    assert not panel._apply_btn.isEnabled()
    assert panel._run_thread is None
    assert panel._tree.qml_warnings == []


def test_sequence_collapses_and_restores_preview(panel):
    original = panel._acquisition_schedule
    panel._sequence_toggle.setChecked(False)
    quick_application().processEvents()
    assert panel._tree.isHidden()
    collapsed_height = panel._preview_splitter.widget(1).height()
    panel._sequence_toggle.setChecked(True)
    quick_application().processEvents()
    assert not panel._tree.isHidden()
    assert panel._preview_splitter.widget(1).height() > collapsed_height
    assert panel._acquisition_schedule is original


def test_invalid_order_explains_reset_and_clears_after_reset(panel):
    panel._nested_schedule_enabled = True
    panel._execution_order = [{"kind": "group", "id": "group:missing", "parameters": ["missing"]}]
    panel._on_draft_edited()
    assert not panel._apply_btn.isEnabled()
    assert not panel._order_reset_notice.isHidden()
    assert 'Reset order' in panel._order_reset_notice.text()
    assert 'Apply plan' in panel._order_reset_notice.text()
    panel._reset_execution_order()
    assert panel._order_reset_notice.isHidden()
    assert panel._apply_btn.isEnabled()


def test_quick_view_state_survives_idle_page_transfer(panel):
    panel._loop_table.item(0, 2).setText("linspace(700,800,30)")
    panel._on_apply()
    panel._tree.sequence_model.goToStep("18")
    saved_view = panel._tree.capture_view_state()
    state = capture_dual_gate_state(panel)
    restored = PresetsPanel(initialize_plan=False)
    try:
        restored.resize(1400, 1000)
        restored.show()
        restore_dual_gate_state(restored, state)
        quick_application().processEvents()
        assert restored._tree.capture_view_state() == saved_view
        assert restored._acquisition_schedule == panel._acquisition_schedule
        assert restored._tree.sequence_model.selectedDetail["title"] == "Step 18"
        assert not restored._tree.sequence_model.followCurrent
    finally:
        restored.dispose_reloaded_page()
        restored.close()
        restored.deleteLater()
        drain_qt_deletions()


def test_full_quick_review_blocks_reload_until_closed(panel):
    panel._tree.show_full_sequence()
    QTest.qWait(20)
    assert "Close the full" in dual_gate_reload_block_reason(panel)
    panel._tree._full_sequence_dialog.close()
    drain_qt_deletions()
    assert dual_gate_reload_block_reason(panel) == ""
