"""Actual Quick page transactions, with inert controllers and temporary source edits."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QObject, QItemSelectionModel, SIGNAL
from PySide6.QtTest import QTest

from tests.quick_helpers import quick_application, drain_qt_deletions
from tests.test_quick_workbench import app, workbench
from tests.test_dual_gate_page_reload import Controller
from ui import page_reload
from ui.presets_panel import PresetsPanel
from ui.refresh_manifest import QUICK_PAGE_MODULES
from ui.workflow_sections import DisclosureSection


@pytest.fixture(autouse=True)
def quick_mode():
    application = quick_application()
    previous = application.property("spectralQuick")
    application.setProperty("spectralQuick", True)
    yield
    application.setProperty("spectralQuick", previous)


@pytest.fixture
def refresh_shell(workbench):
    quick, window = workbench
    names = set(name for spec in page_reload.PAGE_RELOAD_REGISTRY.values() for name in spec.modules)
    names.update(name for modules in QUICK_PAGE_MODULES.values() for name in modules)
    original = {name: sys.modules.get(name) for name in names}
    old = window._presets
    controllers = [Controller() for _ in range(5)]
    panel = PresetsPanel(*controllers)
    index = window._tabs.indexOf(old)
    window._tabs.insertTab(index, panel, "Dual Gate")
    window._tabs.removeTab(window._tabs.indexOf(old))
    window._tab_ids.pop(old)
    window._tab_ids[panel] = "dual_gate"
    window._presets = panel
    window._session_panels = {key: value for value, key in window._tab_ids.items()}
    window._tabs.setCurrentWidget(panel)
    quick.rebind_page("dual_gate", old, panel)
    window._quick_workbench = quick
    window.controllers = controllers
    window.resize(1920, 1020)
    QTest.qWait(80)
    yield quick, window
    window._presets.dispose_reloaded_page()
    # Shut down fresh types before restoring the module identities used by
    # other tests. Shared QML components/engine are deliberately unchanged.
    quick.shutdown()
    drain_qt_deletions()
    for name, module in original.items():
        if module is None:
            sys.modules.pop(name, None)
            if hasattr(sys.modules["ui"], name.rsplit(".", 1)[1]):
                delattr(sys.modules["ui"], name.rsplit(".", 1)[1])
        else:
            sys.modules[name] = module
            setattr(sys.modules["ui"], name.rsplit(".", 1)[1], module)


def source_override(monkeypatch, tmp_path, name, transform):
    original = page_reload._source_path
    path = tmp_path / (name.rsplit(".", 1)[-1] + ".py")
    path.write_text(transform(original(name).read_text(encoding="utf-8")), encoding="utf-8")
    monkeypatch.setattr(page_reload, "_source_path", lambda module: path if module == name else original(module))


def test_calculator_layout_refresh_keeps_parameters_and_connected_owners(refresh_shell, monkeypatch, tmp_path):
    quick, window = refresh_shell
    old = window._presets
    old._sample_edit.setText("keep-sample")
    old._sweep_calc._frames_spin.setValue(77)
    old._sweep_calc._condition_edit.setText("keep-calculator")
    old._sweep_calc._toggle.setChecked(True)
    table = old._batch_table
    table.setCurrentCell(0, 3)
    table.selectionModel().select(table.model().index(1, 3), QItemSelectionModel.SelectionFlag.Select)
    section = old.findChildren(DisclosureSection)[0]
    section.set_expanded(True)
    section_name = section.objectName()
    selected = [(item.row(), item.column()) for item in table.selectedIndexes()]
    previous_engine = quick.navigation.engine()
    old_command = quick._commands["dual_gate"]
    source_override(monkeypatch, tmp_path, "ui.quick_sweep_calculator",
                    lambda text: text.replace('"Range limits"', '"Limits updated"'))

    new = page_reload.reload_page(window, "dual_gate")
    QTest.qWait(100)
    assert new._quick_calculator_layout.sections[1].title() == "Limits updated"
    assert new._sample_edit.text() == "keep-sample"
    assert new._sweep_calc._frames_spin.value() == 77
    assert new._sweep_calc._condition_edit.text() == "keep-calculator"
    assert new._sweep_calc._toggle.isChecked()
    assert [(item.row(), item.column()) for item in new._batch_table.selectedIndexes()] == selected
    assert new.findChild(DisclosureSection, section_name).toggle.isChecked()
    assert quick._page_editors["dual_gate"][0] is new
    assert old_command.adapter._closed
    assert new._lf6 is window.controllers[0] and new._pm is window.controllers[-1]
    assert quick.navigation.engine() is previous_engine
    for controller in window.controllers:
        controller.shutdown.assert_not_called()
        controller.connect_instrument.assert_not_called()
        controller.disconnect_instrument.assert_not_called()


@pytest.mark.parametrize("failure", ["syntax", "layout", "commit"])
def test_bad_layout_or_commit_retains_old_quick_page_and_module_generation(refresh_shell, monkeypatch, tmp_path, failure):
    quick, window = refresh_shell
    old = window._presets
    old._sample_edit.setText("still-editable")
    original_entry = quick._page_editors["dual_gate"]
    command = quick._commands["dual_gate"]
    modules = {name: sys.modules.get(name) for name in QUICK_PAGE_MODULES["dual_gate"]}
    if failure == "commit":
        commit = quick.commit_page_refresh
        def fail(*args):
            commit(*args)
            raise RuntimeError("broken commit")
        monkeypatch.setattr(quick, "commit_page_refresh", fail)
    else:
        source_override(monkeypatch, tmp_path, "ui.quick_workspace_layouts", lambda text:
            text + "\nbroken syntax !\n" if failure == "syntax" else text.replace(
                '    app = QApplication.instance()\n', '    raise RuntimeError("broken layout")\n    app = QApplication.instance()\n'))
    with pytest.raises((SyntaxError, RuntimeError), match="broken|invalid syntax"):
        page_reload.reload_page(window, "dual_gate")
    drain_qt_deletions()
    assert window._presets is old and window._tabs.currentWidget() is old
    assert old._sample_edit.text() == "still-editable"
    assert quick._page_editors["dual_gate"] is original_entry
    assert quick._commands["dual_gate"] is command and not command.adapter._closed
    assert all(not form.adapter._closed for form in original_entry[2]["forms"])
    assert all(sys.modules.get(name) is module for name, module in modules.items())
    window.controllers[-1].power_ready.emit(3e-6)
    assert old._last_power_uw == pytest.approx(3)


def test_repeated_refresh_keeps_one_controller_subscription_and_fresh_table_binding(refresh_shell):
    quick, window = refresh_shell
    pm = window.controllers[-1]
    expected = pm.receivers(SIGNAL("power_ready(double)"))
    for _ in range(3):
        new = page_reload.reload_page(window, "dual_gate")
        drain_qt_deletions()
        assert pm.receivers(SIGNAL("power_ready(double)")) == expected
        assert new._batch_table._quick_editor.model.table is new._batch_table
        assert not new._batch_table._quick_editor.model._closed
        assert quick._commands["dual_gate"].adapter.buttons["run"] is new._run_btn


def test_refresh_refuses_active_acquisition_before_loading_helpers(refresh_shell, monkeypatch):
    quick, window = refresh_shell
    window._acquisition_active = lambda: True
    loader = Mock(side_effect=AssertionError("must not load sources"))
    monkeypatch.setattr(page_reload, "_fresh_page_modules", loader)
    with pytest.raises(RuntimeError, match="acquisition"):
        page_reload.reload_page(window, "dual_gate")
    loader.assert_not_called()


def test_invalid_quick_draft_remains_editable_and_blocks_refresh(refresh_shell, monkeypatch):
    from ui.presets_panel import BATCH_SCHEMA
    quick, window = refresh_shell
    old = window._presets
    model = old._batch_table._quick_editor.model
    column = model._columns.index(BATCH_SCHEMA.index("repeat"))
    token = model.data(model.index(0, column), model.CellRole)["token"]
    model.stageEdit(token, "1.5")
    loader = Mock(side_effect=AssertionError("must not load sources"))
    monkeypatch.setattr(page_reload, "_fresh_page_modules", loader)
    with pytest.raises(RuntimeError, match="highlighted Quick editor"):
        page_reload.reload_page(window, "dual_gate")
    assert window._presets is old and not model._closed
    assert model.data(model.index(0, column), model.CellRole)["value"] == "1.5"
    loader.assert_not_called()


def test_cleanup_failure_reports_committed_page_without_false_rollback(refresh_shell, monkeypatch):
    quick, window = refresh_shell
    old = window._presets
    finish = quick.finish_page_refresh
    def fail(*args):
        finish(*args)
        raise RuntimeError("cleanup failed")
    monkeypatch.setattr(quick, "finish_page_refresh", fail)
    new = page_reload.reload_page(window, "dual_gate")
    assert window._presets is new and new is not old
    assert quick._page_editors["dual_gate"][0] is new
    assert "cleanup failed" in new._page_reload_ui_error
    assert not quick._commands["dual_gate"].adapter._closed
