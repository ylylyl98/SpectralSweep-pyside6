import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import Qt, QTimer
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
    label = find_item(view.rootObject(), "referenceLabel_" + ref["id"])
    point = label.mapToScene(label.boundingRect().center()).toPoint()
    assert not editor.isVisible()
    QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=point)
    assert not editor.isVisible()
    assert ref["visible"]
    QTest.mouseDClick(view, Qt.MouseButton.LeftButton, pos=point)
    assert editor.isVisible()
    QTest.keyClick(view, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(view, "Renamed")
    QTest.keyClick(view, Qt.Key.Key_Return)
    assert ref["name"] == "Renamed"
    assert panel._reference_list.currentItem().data(Qt.ItemDataRole.UserRole) == ref["id"]
    checkbox = find_item(view.rootObject(), "referenceVisibility_" + ref["id"])
    QTest.mouseClick(view, Qt.MouseButton.LeftButton,
                     pos=checkbox.mapToScene(checkbox.boundingRect().center()).toPoint())
    assert not ref["visible"]
    assert not row.checked
    swatch = find_item(view.rootObject(), "referenceColor_" + ref["id"])
    with patch.object(panel, "_choose_reference_color") as choose:
        QTest.mouseClick(view, Qt.MouseButton.LeftButton,
                         pos=swatch.mapToScene(swatch.boundingRect().center()).toPoint())
    choose.assert_called_once_with(ref["id"])


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
    label = find_item(view.rootObject(), "referenceLabel_" + ref["id"])
    QTest.mouseDClick(view, Qt.MouseButton.LeftButton,
                     pos=label.mapToScene(label.boundingRect().center()).toPoint())
    QTest.keyClick(view, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(view, "Focus loss name")
    panel._center.setFocus()
    QTest.qWait(10)
    assert ref["name"] == "Focus loss name"


def test_f2_escape_and_keyboard_navigation_keep_selection_separate_from_visibility(references):
    panel, view, ref = references
    second = panel.add_reference(np.array([702., 703.]), np.array([2., 3.]),
                                 name="Second", auto_save=False)
    view.model.refresh()
    QTest.qWait(10)
    label = find_item(view.rootObject(), "referenceLabel_" + ref["id"])
    QTest.mouseClick(view, Qt.MouseButton.LeftButton,
                     pos=label.mapToScene(label.boundingRect().center()).toPoint())
    QTest.keyClick(view, Qt.Key.Key_Down)
    QTest.qWait(10)
    assert panel._selected_reference() is second
    assert second["visible"] and ref["visible"]
    QTest.keyClick(view, Qt.Key.Key_F2)
    editor = find_item(view.rootObject(), "referenceName_" + second["id"])
    assert editor.isVisible() and editor.hasActiveFocus()
    QTest.keyClicks(view, "Discard this")
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert second["name"] == "Second"
    assert not editor.isVisible()
    assert not view.model._drafts
    assert panel.commit_reference_edits()
    QTest.keyClick(view, Qt.Key.Key_Space)
    assert not second["visible"] and ref["visible"]


def test_keyboard_navigation_scrolls_to_uninstantiated_rows_before_f2(references):
    panel, view, ref = references
    for number in range(24):
        panel.add_reference(np.array([702., 703.]), np.array([2., 3.]),
                            name=f"Spectrum {number}", auto_save=False)
    panel._side_panel.setSizes([160, 600])
    view.model.refresh()
    QTest.qWait(20)
    label = find_item(view.rootObject(), "referenceLabel_" + ref["id"])
    QTest.mouseClick(view, Qt.MouseButton.LeftButton,
                     pos=label.mapToScene(label.boundingRect().center()).toPoint())
    for _ in range(24):
        QTest.keyClick(view, Qt.Key.Key_Down)
        QTest.qWait(5)
    target = panel.references[-1]
    assert panel._selected_reference() is target
    QTest.keyClick(view, Qt.Key.Key_F2)
    editor = find_item(view.rootObject(), "referenceName_" + target["id"])
    assert editor is not None and editor.isVisible() and editor.hasActiveFocus()
    point = editor.mapToScene(editor.boundingRect().center()).toPoint()
    assert 0 <= point.y() < view.height()
    QTest.keyClicks(view, "Last spectrum")
    QTest.keyClick(view, Qt.Key.Key_Return)
    assert target["name"] == "Last spectrum"


def test_right_click_selects_target_and_reuses_header_menu_commands(references):
    panel, view, ref = references
    second = panel.add_reference(np.array([702., 703.]), np.array([2., 3.]),
                                 name="Second", auto_save=False)
    view.model.refresh()
    QTest.qWait(10)
    view.model.rows[0].select()
    label = find_item(view.rootObject(), "referenceLabel_" + second["id"])
    QTest.mouseClick(view, Qt.MouseButton.RightButton,
                     pos=label.mapToScene(label.boundingRect().center()).toPoint())
    assert panel._selected_reference() is second
    assert panel._reference_menu.isVisible()
    assert panel._details_reference_btn.isHidden()
    rename = next(action for action, button in panel._reference_menu_actions
                  if button is panel._rename_reference_btn)
    panel._reference_menu.close()
    rename.trigger()
    QTest.qWait(10)
    editor = find_item(view.rootObject(), "referenceName_" + second["id"])
    assert editor.isVisible()
    QTest.keyClicks(view, "Menu name")
    QTest.keyClick(view, Qt.Key.Key_Return)
    assert second["name"] == "Menu name"
    assert ref["name"] == "Original"
    QTest.keyClick(view, Qt.Key.Key_F10, Qt.KeyboardModifier.ShiftModifier)
    assert panel._reference_menu.isVisible()
    assert panel._selected_reference() is second
    panel._reference_menu.close()


def test_header_menu_can_rename_selected_row_after_scrolling_it_out_of_view(references):
    panel, view, ref = references
    for number in range(24):
        panel.add_reference(np.array([702., 703.]), np.array([2., 3.]),
                            name=f"Spectrum {number}", auto_save=False)
    panel._side_panel.setSizes([160, 600])
    view.model.refresh()
    view.model.rows[0].select()
    QTest.qWait(20)
    listing = find_item(view.rootObject(), "SpectrumReferenceRows")
    listing.setProperty("contentY", listing.property("contentHeight") - listing.height())
    QTest.qWait(20)
    assert listing.property("contentY") > 0
    assert panel._selected_reference() is ref
    rename = next(action for action, button in panel._reference_menu_actions
                  if button is panel._rename_reference_btn)
    # QToolButton opens a native menu with a nested event loop on Windows.
    QTimer.singleShot(20, lambda: QTest.mouseClick(
        panel._reference_menu, Qt.MouseButton.LeftButton,
        pos=panel._reference_menu.actionGeometry(rename).center()))
    QTest.mouseClick(panel._reference_more, Qt.MouseButton.LeftButton)
    QTest.qWait(20)
    editor = find_item(view.rootObject(), "referenceName_" + ref["id"])
    assert editor is not None and editor.isVisible() and editor.hasActiveFocus()
    point = editor.mapToScene(editor.boundingRect().center()).toPoint()
    assert 0 <= point.y() < view.height()
    QTest.keyClicks(view, "Back at top")
    QTest.keyClick(view, Qt.Key.Key_Return)
    assert ref["name"] == "Back at top"


def test_saved_rows_hide_redundant_status_but_keep_metadata_and_save_errors(references):
    panel, view, ref = references
    status = find_item(view.rootObject(), "referenceStatus_" + ref["id"])
    ref.update(saved=True, path="C:/spectra/original.csv")
    panel._refresh_reference_item(ref)
    view.model.refresh()
    assert not status.isVisible()
    row = view.model.rows[0]
    assert "Saved" in row.tooltip and ref["path"] in row.tooltip
    ref.update(saved=False, save_error="Disk full")
    panel._refresh_reference_item(ref)
    view.model.refresh()
    assert status.isVisible() and status.property("text") == "Save failed"
    assert "Disk full" in row.tooltip
    retry = next(action for action, button in panel._reference_menu_actions
                 if button is panel._retry_reference_btn)
    assert retry.isVisible()
    assert panel._retry_reference_btn.isHidden()


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


def test_idle_and_selection_do_not_rescan_reference_library(references, monkeypatch):
    from ui.quick_reference_list import ReferenceRow
    panel, view, ref = references
    panel.add_reference(np.array([702., 703.]), np.array([2., 3.]),
                        name="Second", auto_save=False)
    view.model.refresh()
    QTest.qWait(30)
    assert view.model.rows[0].select()
    touched = []
    original = ReferenceRow.refresh
    def refresh(row, resolved=None):
        touched.append(row.referenceId)
        return original(row, resolved)
    monkeypatch.setattr(ReferenceRow, "refresh", refresh)
    with patch.object(view.model, "refresh", wraps=view.model.refresh) as full:
        QTest.qWait(350)
        assert not touched
        assert view.model.rows[1].select()
        QTest.qWait(10)
        assert full.call_count == 0
        assert len(touched) == 2  # Old and new selection only.


def test_source_presentation_changes_arrive_without_polling(references):
    panel, view, ref = references
    row = view.model.rows[0]
    with patch.object(view.model, "refresh", wraps=view.model.refresh) as full:
        panel.set_reference_color(ref["id"], "#123456")
        ref.update(saved=False, save_error="Disk full")
        panel._refresh_reference_item(ref)
        QTest.qWait(10)
        assert row.color == "#123456" and row.saveFailed
        assert "Disk full" in row.tooltip
        assert full.call_count == 0
    panel._reference_list.setEnabled(False)
    QTest.qWait(10)
    assert not row.enabled
    panel._reference_list.setEnabled(True)
    ref["setup_key"] = "other"
    panel._active_setup_key = "current"
    panel._refresh_comparison()
    QTest.qWait(10)
    assert row.enabled and not row.visible
    panel._comparison_mode.setCurrentText("Compare")
    QTest.qWait(10)
    assert row.visible


def test_remove_selected_reference_before_queued_refresh_is_safe(references):
    panel, view, ref = references
    second = panel.add_reference(np.array([702., 703.]), np.array([2., 3.]),
                                 name="Second", auto_save=False)
    view.model.refresh()
    view.model.rows[0].select()
    panel.remove_reference(ref["id"])
    panel._reference_list.setCurrentRow(0)
    QTest.qWait(10)
    assert len(view.model.rows) == 1
    assert view.model.rows[0].referenceId == second["id"]
    assert view.model.rows[0].selected
