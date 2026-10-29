from types import MethodType
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication, QLineEdit, QMainWindow

from tests.test_dual_gate_page_reload import shell
from ui import page_reload
from ui.main_window import MainWindow, _SharedSampleIdBinder


@pytest.fixture
def tracked_window(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ui import main_window
    from ui.refresh_manifest import QUICK_PAGE_MODULES
    app = QApplication.instance() or QApplication([])
    sources = {
        "ui/styles/application.qss": "QWidget { color: black; }",
        "ui/styles/quick_application.qss": "QWidget { color: blue; }",
        "ui/qml/ParameterForm.qml": "import QtQuick\nItem { width: 80 }",
        "ui/quick_sweep_calculator.py": "value = 1\n",
        "ui/quick_workspace_layouts.py": "value = 1\n",
        "controllers/device.py": "value = 1\n",
    }
    for key, spec in page_reload.PAGE_RELOAD_REGISTRY.items():
        for name in (*QUICK_PAGE_MODULES[key], *spec.modules):
            sources.setdefault(name.replace(".", "/") + ".py", "value = 1\n")
    for name, source in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(main_window, "_PROJECT_ROOT", tmp_path)
    window = QMainWindow()
    window._ui_mode = "quick"
    window._status = window.statusBar()
    style = tmp_path / "ui/styles/quick_application.qss"
    window._application_style = SimpleNamespace(path=style, _last_source=style.read_text())
    window._show_source_changes = Mock()
    window._update_source_status = MethodType(MainWindow._update_source_status, window)
    window._source_changes_text = MethodType(MainWindow._source_changes_text, window)
    MainWindow._initialize_source_tracking(window)
    yield window
    window.deleteLater()
    from tests.quick_helpers import drain_qt_deletions
    drain_qt_deletions()


def test_quick_startup_does_not_report_legacy_style_as_a_pending_reload(tracked_window):
    assert tracked_window._source_changes.pending() == []
    assert tracked_window._source_status.text() == "Code: up to date"


def test_refresh_current_interface_keeps_the_existing_action_and_shortcut(tracked_window):
    from types import SimpleNamespace
    tracked_window._application_style = SimpleNamespace(
        reload=Mock(), reloaded=Mock(), failed=Mock(), last_error=None)
    for name in ("_styles_reloaded", "_styles_failed", "_reload_spectrum_page", "_reload_current_page",
                 "_reload_registered_page", "_set_calibration_visible", "_open_spectrum_preview"):
        setattr(tracked_window, name, Mock())
    MainWindow._initialize_style_actions(tracked_window)
    action = next(action for action in tracked_window._view_menu.actions()
                  if action.objectName() == "ReloadCurrentPage")
    assert action.text() == "Refresh current interface"
    assert action.shortcut().toString() == "Ctrl+Alt+U"
    assert "Dual Gate, Spectrum or Settings Python layouts, tables and forms while idle" in action.statusTip()
    assert "QML, global shell, controllers and drivers require restart" in action.statusTip()
    action.trigger()
    tracked_window._reload_current_page.assert_called_once()


def test_quick_tracking_groups_include_only_the_supported_page_helpers(tracked_window):
    from ui.refresh_manifest import QUICK_PAGE_MODULES, QUICK_SHARED_RESTART_SOURCES
    tracker = tracked_window._source_changes
    expected = {
        "dual_gate": (
            "ui.quick_sweep_calculator", "ui.quick_gate_sections", "ui.quick_workspace_layouts",
            "ui.quick_table_editor", "ui.quick_parameter_forms"),
        "spectrum": (
            "ui.quick_acquisition_sections", "ui.quick_workspace_layouts", "ui.quick_table_editor",
            "ui.quick_parameter_forms", "ui.quick_reference_list"),
        "settings": (
            "ui.quick_settings_layout", "ui.quick_workspace_layouts", "ui.quick_table_editor",
            "ui.quick_parameter_forms"),
    }
    assert QUICK_PAGE_MODULES == expected
    for key, helpers in expected.items():
        paths = tuple(name.replace(".", "/") + ".py" for name in helpers)
        assert tracker.groups[key][:len(paths)] == paths
    assert tracker.shared == QUICK_SHARED_RESTART_SOURCES | {
        "ui/spectrum_plot.py", "ui/andor_controls_widget.py"}
    assert "ui/quick_sweep_calculator.py" not in tracker.groups["spectrum"]


def test_widgets_tracking_excludes_quick_workspace_helpers(tracked_window):
    tracked_window._ui_mode = "widgets"
    MainWindow._initialize_source_tracking(tracked_window)
    tracker = tracked_window._source_changes
    assert "ui/quick_sweep_calculator.py" not in tracker.groups["dual_gate"]
    assert tracker.shared == {"ui/spectrum_plot.py", "ui/andor_controls_widget.py"}


def test_calculator_change_stays_pending_until_the_loaded_source_is_applied(tracked_window):
    tracker = tracked_window._source_changes
    original = tracker.snapshot(tracker.groups["dual_gate"])
    path = tracker.root / "ui/quick_sweep_calculator.py"
    path.write_text("value = 2\n", encoding="utf-8")
    tracker.refresh()
    expected = [{"path": "ui/quick_sweep_calculator.py", "pages": ["dual_gate"],
                 "styles": False, "restart": False}]
    assert tracker.pending() == expected
    assert "Refresh Dual Gate" in tracked_window._source_changes_text()
    tracker.mark_page_applied("dual_gate", original)
    assert tracker.pending() == expected
    tracker.mark_page_applied("dual_gate", tracker.snapshot(tracker.groups["dual_gate"]))
    assert tracker.pending() == []


def test_shared_layout_refresh_keeps_other_pages_and_restart_pending(tracked_window):
    tracker = tracked_window._source_changes
    path = tracker.root / "ui/quick_workspace_layouts.py"
    path.write_text("value = 2\n", encoding="utf-8")
    tracker.refresh()
    item, = tracker.pending()
    assert set(item["pages"]) == {"dual_gate", "spectrum", "settings"}
    assert item["restart"]
    assert tracked_window._source_status.text() == "Code: refresh available; restart pending"
    text = tracked_window._source_changes_text()
    assert "Refresh Spectrum, Dual Gate, Settings" in text
    assert "other users of this shared module still require restart" in text
    tracker.mark_page_applied("dual_gate", tracker.snapshot(tracker.groups["dual_gate"]))
    item, = tracker.pending()
    assert set(item["pages"]) == {"spectrum", "settings"}
    assert item["restart"]
    for page in ("spectrum", "settings"):
        tracker.mark_page_applied(page, tracker.snapshot(tracker.groups[page]))
    item, = tracker.pending()
    assert item["pages"] == []
    assert item["restart"]


def test_qml_and_controller_changes_still_require_restart_after_page_refresh(tracked_window):
    tracker = tracked_window._source_changes
    for name in ("ui/qml/ParameterForm.qml", "controllers/device.py"):
        path = tracker.root / name
        path.write_text(path.read_text() + "\n", encoding="utf-8")
    tracker.refresh()
    for page in tracker.groups:
        tracker.mark_page_applied(page, tracker.snapshot(tracker.groups[page]))
    pending = tracker.pending()
    assert {item["path"] for item in pending} == {
        "ui/qml/ParameterForm.qml", "controllers/device.py"}
    assert all(item["restart"] and not item["pages"] for item in pending)
    assert tracked_window._source_status.text() == "Code: restart required"


def test_quick_style_reload_leaves_changed_qml_pending(tracked_window):
    tracker = tracked_window._source_changes
    style = tracked_window._application_style
    style._last_source = "QWidget { color: green; }"
    style.path.write_text(style._last_source, encoding="utf-8")
    qml = tracker.root / "ui/qml/ParameterForm.qml"
    qml.write_text("import QtQuick\nItem { width: 160 }", encoding="utf-8")
    tracker.refresh()
    MainWindow._styles_reloaded(tracked_window, str(style.path))
    assert [item["path"] for item in tracker.pending()] == ["ui/qml/ParameterForm.qml"]
    assert tracked_window._source_status.text() == "Code: restart required"
    assert "Qt Quick" in tracked_window._source_changes_text()


def test_quick_rebuild_failure_remains_visible_and_is_not_acknowledged(shell):
    from types import SimpleNamespace
    shell._status = shell.statusBar()
    shell._acquisition_active = lambda: False
    shell._source_changes = Mock(groups={"dual_gate": ("ui/presets_panel.py",)})
    shell._quick_workbench = SimpleNamespace(rebind_page=Mock(side_effect=RuntimeError("broken Quick layout")))
    old = shell._presets
    MainWindow._reload_registered_page(shell, "dual_gate")
    assert shell._presets is not old  # The backend transfer was already committed.
    assert "broken Quick layout" in shell._status.currentMessage()
    assert "Quick" in shell._status.currentMessage()
    assert "connections and data preserved" not in shell._status.currentMessage()
    shell._source_changes.mark_page_applied.assert_not_called()


def test_page_reload_reports_pending_shared_layout_changes(shell):
    shell._status = shell.statusBar()
    shell._acquisition_active = lambda: False
    shell._source_changes = Mock(groups={"dual_gate": ("ui/presets_panel.py",)})
    shell._source_changes.pending.return_value = [dict(path="ui/quick_workspace_layouts.py", restart=True)]
    MainWindow._reload_registered_page(shell, "dual_gate")
    assert "still require restart" in shell._status.currentMessage()
    shell._source_changes.mark_page_applied.assert_called_once()


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
