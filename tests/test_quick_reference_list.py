import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from unittest.mock import patch

from tests.quick_helpers import quick_application, drain_qt_deletions
from tests.test_quick_parameter_forms import find_item
from ui.quick_reference_list import install_reference_list
from ui.spectrum_panel import SpectrumPanel
from utils.config import cfg


@pytest.fixture
def references(monkeypatch):
    quick_application()
    monkeypatch.setattr(cfg, "save", lambda *a, **k: pytest.fail("No production config writes"))
    panel = SpectrumPanel(bind_controller=False)
    ref = panel.add_reference(np.array([700., 701.]), np.array([1., 2.]),
                              name="Original", auto_save=False)
    view = install_reference_list(panel)
    panel._references_toggle.setChecked(True)
    panel.resize(1100, 850)
    panel.show()
    QTest.qWait(30)
    view.model.refresh()
    yield panel, view, ref
    view.shutdown()
    panel.close()
    panel.deleteLater()
    drain_qt_deletions()


def test_reference_qml_name_edit_visibility_and_source_selection(references):
    panel, view, ref = references
    row = view.model.rows[0]
    editor = find_item(view.rootObject(), "referenceName_" + ref["id"])
    assert editor is not None
    QTest.mouseClick(view, Qt.MouseButton.LeftButton,
                     pos=editor.mapToScene(editor.boundingRect().center()).toPoint())
    QTest.keyClick(view, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(view, "Renamed")
    QTest.keyClick(view, Qt.Key.Key_Return)
    assert ref["name"] == "Renamed"
    assert panel._reference_list.currentItem().data(Qt.ItemDataRole.UserRole) == ref["id"]
    assert row.toggle()
    assert not ref["visible"]
    assert not row.checked


def test_dynamic_rows_setup_status_and_source_stack_routing(references):
    panel, view, ref = references
    second = panel.add_reference(np.array([702., 703.]), np.array([2., 3.]),
                                 name="Second", auto_save=False)
    view.model.refresh()
    assert len(view.model.rows) == 2
    panel._reference_stack.setCurrentWidget(panel._reference_list)
    assert panel._reference_stack.currentWidget() is view
    row = view.model.rows[1]
    assert row.select()
    assert panel._reference_list.currentItem().data(Qt.ItemDataRole.UserRole) == second["id"]
    second["_row_source"].setText("Andor Si")
    second["_row_status"].setText("Saved")
    view.model.refresh()
    assert row.setup == "Andor Si"
    assert row.status == "Saved"


def test_removed_reference_and_shutdown_reject_stale_row_commands(references):
    panel, view, ref = references
    row = view.model.rows[0]
    panel.remove_reference(ref["id"])
    view.model.refresh()
    drain_qt_deletions()
    assert view.model.rows == []
    assert not row.rename("Stale")
    assert not row.toggle()
    assert not row.chooseColor()
    view.shutdown()
    assert not row.select()


def test_name_edit_is_committed_on_focus_loss_without_enter(references):
    panel, view, ref = references
    editor = find_item(view.rootObject(), "referenceName_" + ref["id"])
    QTest.mouseClick(view, Qt.MouseButton.LeftButton,
                    pos=editor.mapToScene(editor.boundingRect().center()).toPoint())
    QTest.keyClick(view, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(view, "Focus loss name")
    panel._center.setFocus()
    QTest.qWait(10)
    assert ref["name"] == "Focus loss name"


def test_draft_is_committed_at_explicit_boundary_before_session_save(references):
    panel, view, ref = references
    row = view.model.rows[0]
    row.stageName("Saved on close")
    assert panel.capture_session_state()["reference_library"]["entries"][0]["name"] == "Original"
    assert panel.commit_reference_edits()
    assert ref["name"] == "Saved on close"
    assert panel.capture_session_state()["reference_library"]["entries"][0]["name"] == "Saved on close"
    assert not view.model._drafts


def test_rename_failure_is_returned_and_draft_reverted(references):
    panel, view, ref = references
    row = view.model.rows[0]
    row.stageName("Cannot save")
    with patch.object(panel, "rename_reference", return_value=False):
        assert not row.rename("Cannot save")
    assert row.name == "Original"
    assert not view.model._drafts


def test_empty_name_cancels_edit_without_blocking_session_commit(references):
    panel, view, ref = references
    row = view.model.rows[0]
    row.stageName("   ")
    assert panel.commit_reference_edits()
    assert row.name == "Original"
    assert ref["name"] == "Original"
    assert not view.model._drafts
