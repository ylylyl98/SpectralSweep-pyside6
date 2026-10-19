"""Transactional Dual Gate reloads use only in-memory controllers and data."""
from copy import deepcopy
import sys
from unittest.mock import Mock

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
from PySide6.QtWidgets import QApplication, QLineEdit, QMainWindow, QTabWidget, QWidget
from shiboken6 import isValid

from ui import page_reload
from ui.presets_panel import PresetsPanel
from utils.config import cfg


class Controller(QObject):
    connected = Signal(list)
    disconnected = Signal()
    settings_applied = Signal()
    power_ready = Signal(float)
    limits_result = Signal(object)
    limits_error = Signal(str)
    limits_state_changed = Signal()

    def __init__(self):
        super().__init__()
        self.is_connected = False
        self.has_vbias = False
        self.is_busy = False
        self.switching_locked = False
        self.spectrum_actions_blocked = False
        self.shutdown = Mock(side_effect=AssertionError("hardware must not be called"))
        self.connect_instrument = Mock(side_effect=AssertionError("hardware must not be called"))
        self.disconnect_instrument = Mock(side_effect=AssertionError("hardware must not be called"))


@pytest.fixture
def shell(monkeypatch):
    app = QApplication.instance() or QApplication([])
    # Restore the source module identities so other tests can use their imports.
    names = page_reload._DUAL_GATE_MODULES
    modules = {name: sys.modules.get(name) for name in names}
    previous = deepcopy((cfg.filename, cfg.ramp))
    monkeypatch.setattr(cfg, "save", Mock())
    window = QMainWindow()
    window._tabs = QTabWidget(window)
    window.setCentralWidget(window._tabs)
    window.controllers = [Controller() for _ in range(5)]
    window._presets = PresetsPanel(*window.controllers)
    window._tabs.addTab(QWidget(), "Other")
    window._tabs.addTab(window._presets, "Dual Gate")
    window._tabs.setCurrentWidget(window._presets)
    window._session_panels = {"dual_gate": window._presets}
    window._tab_ids = {window._presets: "dual_gate"}
    window.resize(1400, 900)
    window.show()
    app.processEvents()
    yield window
    window._presets.dispose_reloaded_page()
    window.hide()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    for name, module in modules.items():
        if module is None:
            sys.modules.pop(name, None)
            if hasattr(sys.modules["ui"], name.rsplit(".", 1)[1]):
                delattr(sys.modules["ui"], name.rsplit(".", 1)[1])
        else:
            sys.modules[name] = module
            setattr(sys.modules["ui"], name.rsplit(".", 1)[1], module)
    cfg.filename, cfg.ramp = previous


def test_preserves_applied_plan_invalid_draft_selections_history_and_logs(shell, monkeypatch, tmp_path):
    old = shell._presets
    old._loop_table.item(0, 2).setText("861,862")
    old._set_loop_run_selection(0, [1])
    old._on_apply()
    applied = deepcopy(old._final_seq)
    old._loop_table.item(0, 2).setText("899, incomplete")
    old._batch_table.item(0, 4).setText("unfinished")
    old._sample_edit.setText("draft sample")
    old._record_batch_history()
    old._batch_row_clipboard = [{"condition_label": "clipboard"}]
    old._sweep_calc._frames_spin.setValue(77)
    old._sweep_calc._condition_edit.setText("calculator draft")
    old._sweep_calc._label_auto = False
    old._run_outcome = "stopped"
    old._done_frames = 1
    old._log("unsaved operator log")
    old._log_toggle.setChecked(True)
    old._status_lbl.setText("Last run stopped")
    old._status_lbl.setProperty("spectralTone", "warning")
    cfg.ramp.safe_jump_V = 987.0  # A different page changed the shared default.
    old._batch_table.selectRow(0)
    old._loop_table.selectRow(0)
    old_session = old.capture_session_state()
    original_path = page_reload._source_path
    source = original_path("ui.presets_panel").read_bytes()
    path = tmp_path / "presets_panel.py"
    path.write_bytes(source.replace(b'"Run note:"', b'"Run note updated:"'))
    monkeypatch.setattr(page_reload, "_source_path", lambda name: path if name == "ui.presets_panel" else original_path(name))
    hook_calls = []
    shell._rebind_reloaded_page = lambda key, before, after: hook_calls.append((key, before, after))

    new = page_reload.reload_dual_gate_page(shell)
    assert new is not old and new._tag_label.text() == "Run note updated:"
    assert (new._lf6, new._smu, new._rot, new._stage, new._pm) == tuple(shell.controllers)
    assert new.capture_session_state()["draft_loop"] == old_session["draft_loop"]
    assert new.capture_session_state()["draft_batch"] == old_session["draft_batch"]
    assert new._final_seq == applied
    assert new._loop_run_selections.export() == old._loop_run_selections.export()
    assert new._applied_loop_run_selections == old._applied_loop_run_selections
    assert new._execution_order == old._execution_order
    assert new._applied_execution_order == old._applied_execution_order
    assert new._sample_edit.text() == "draft sample"
    assert new._sweep_calc._frames_spin.value() == 77
    assert new._sweep_calc._condition_edit.text() == "calculator draft"
    assert not new._sweep_calc._label_auto
    assert new._batch_history_index == old._batch_history_index
    assert new._batch_row_clipboard == old._batch_row_clipboard
    assert "unsaved operator log" in new._log_text.toPlainText()
    assert new._status_lbl.text() == "Last run stopped"
    assert new._status_lbl.property("spectralTone") == "warning"
    assert cfg.ramp.safe_jump_V == 987.0
    assert new._run_outcome == "stopped" and new._done_frames == 1
    assert new._batch_table.currentRow() == 0
    assert shell._tabs.currentWidget() is new
    assert shell._session_panels["dual_gate"] is new
    assert hook_calls == [("dual_gate", old, new)]
    cfg.save.assert_not_called()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(old)


def test_retains_open_spectrum_viewer_and_rebinds_it(shell):
    old = shell._presets
    old._toggle_spectrum_viewer(True)
    viewer = old._spectrum_viewer
    payload = {"wavelengths": np.arange(3.) + 600, "data": np.arange(3.), "metadata": {}}
    viewer.set_acquisition(payload)
    viewer._log.setChecked(True)
    new = page_reload.reload_dual_gate_page(shell)
    assert new._spectrum_viewer is viewer and viewer.parentWidget() is new
    assert viewer.isVisible() and viewer._log.isChecked()
    assert viewer._last_payload is payload
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    viewer.hide()
    assert not new._spectrum_btn.isChecked()
    assert not new._preview_event.is_set()


def test_captures_table_editor_before_shortcut_commits_it(shell):
    old = shell._presets
    table = old._loop_table
    item = table.item(0, 2)
    table.setCurrentItem(item)
    table.editItem(item)
    QApplication.processEvents()
    editor = QApplication.focusWidget()
    assert isinstance(editor, QLineEdit)
    editor.setText("812, unfinished")
    assert item.text() != editor.text()
    new = page_reload.reload_dual_gate_page(shell)
    assert new._loop_table.item(0, 2).text() == "812, unfinished"


def test_reload_keeps_inactive_tab_and_tab_properties(shell):
    shell._tabs.setCurrentIndex(0)
    active = shell._tabs.currentWidget()
    shell._tabs.setTabToolTip(1, "Dual Gate details")
    shell._tabs.setTabVisible(1, False)
    new = page_reload.reload_dual_gate_page(shell)
    assert shell._tabs.currentWidget() is active
    index = shell._tabs.indexOf(new)
    assert not shell._tabs.isTabVisible(index)
    assert shell._tabs.tabToolTip(index) == "Dual Gate details"


def test_repeat_reload_has_one_subscription_and_keeps_other_listeners(shell):
    pm = shell.controllers[-1]
    others = []
    pm.power_ready.connect(others.append)
    for count in range(3):
        previous = shell._presets
        new = page_reload.reload_dual_gate_page(shell)
        previous_power = previous._last_power_uw
        new._update_filename_preview = Mock()
        pm.power_ready.emit((count + 1) * 1e-6)
        assert new._last_power_uw == pytest.approx(count + 1)
        assert previous._last_power_uw == previous_power
        new._update_filename_preview.assert_called_once()
    assert len(others) == 3


@pytest.mark.parametrize("failure", ["syntax", "constructor", "restore", "bind", "shell", "plan"])
def test_failed_reload_retains_original_modules_page_and_subscriptions(shell, monkeypatch, tmp_path, failure):
    old = shell._presets
    old._sample_edit.setText("keep me")
    original_modules = {name: sys.modules[name] for name in page_reload._DUAL_GATE_MODULES}
    original_path = page_reload._source_path
    if failure == "restore":
        from dataclasses import replace
        spec = page_reload.PAGE_RELOAD_REGISTRY["dual_gate"]
        monkeypatch.setitem(page_reload.PAGE_RELOAD_REGISTRY, "dual_gate", replace(spec, restore=Mock(side_effect=ValueError("restore failed"))))
    elif failure == "shell":
        def rebind(key, before, after):
            if before is old:
                raise ValueError("shell bind failed")
        shell._rebind_reloaded_page = rebind
    else:
        source = original_path("ui.presets_panel").read_text(encoding="utf-8")
        if failure == "syntax":
            source += "\ninvalid syntax !"
        elif failure == "constructor":
            source = source.replace("        self._build()\n        self._refresh_tables()", "        raise RuntimeError('constructor failed')\n        self._refresh_tables()")
        elif failure == "plan":
            source = source.replace("self._final_seq    = seq", "self._final_seq    = [dict(row, reload_changed=True) for row in seq]")
        else:
            source = source.replace("    def bind_controller_signals(self) -> None:\n", "    def bind_controller_signals(self) -> None:\n        raise RuntimeError('bind failed')\n")
        path = tmp_path / "presets_panel.py"
        path.write_text(source, encoding="utf-8")
        monkeypatch.setattr(page_reload, "_source_path", lambda name: path if name == "ui.presets_panel" else original_path(name))
    with pytest.raises((ValueError, RuntimeError, SyntaxError)):
        page_reload.reload_dual_gate_page(shell)
    assert shell._presets is old and old._sample_edit.text() == "keep me"
    assert shell._tabs.currentWidget() is old
    assert all(sys.modules[name] is module for name, module in original_modules.items())
    shell.controllers[-1].power_ready.emit(2e-6)
    assert old._last_power_uw == pytest.approx(2)


@pytest.mark.parametrize("busy", ["worker", "saving", "controller", "dialog"])
def test_busy_blocks_before_loading_source(shell, monkeypatch, busy):
    from types import SimpleNamespace
    old = shell._presets
    if busy == "worker":
        old._run_thread = SimpleNamespace(isRunning=lambda: True)
    elif busy == "saving":
        old._run_worker = SimpleNamespace(metadata_finalizing=True)
    elif busy == "controller":
        shell.controllers[0].switching_locked = True
    else:
        old._tree.show_full_sequence()
    loader = Mock(side_effect=AssertionError("source must not load while busy"))
    monkeypatch.setattr(page_reload, "_fresh_page_modules", loader)
    try:
        with pytest.raises(RuntimeError):
            page_reload.reload_dual_gate_page(shell)
        loader.assert_not_called()
        assert shell._presets is old
    finally:
        old._run_thread = old._run_worker = None
