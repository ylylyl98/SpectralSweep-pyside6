from copy import deepcopy
from dataclasses import asdict, replace
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
from PySide6.QtWidgets import QApplication, QMainWindow, QStatusBar, QTabWidget

from ui import page_reload
from ui.main_window import MainWindow
from ui.settings_panel import SettingsPanel
from utils.config import cfg


class Controller(QObject):
    connected = Signal(object)
    disconnected = Signal()
    settings_applied = Signal()
    error = Signal(str)
    is_connected = True
    is_busy = False
    switching_locked = False
    spectrum_actions_blocked = False

    def __init__(self):
        super().__init__()
        self.apply_settings = Mock()
        self.disconnect_instrument = Mock()
        self.shutdown = Mock()


@pytest.fixture
def shell(monkeypatch):
    app = QApplication.instance() or QApplication([])
    saved_cfg = deepcopy(cfg.__dict__)
    monkeypatch.setattr("app.notification_config.read_config", lambda: None)
    module = sys.modules["ui.settings_panel"]
    window = MainWindow.__new__(MainWindow)
    QMainWindow.__init__(window)
    window._status = QStatusBar(window)
    window.setStatusBar(window._status)
    window._lf6 = Controller()
    window._inst_panel = SimpleNamespace(refresh_mff101_paths=Mock())
    window._ntfy_notifier = SimpleNamespace(set_url=Mock())
    window._settings = SettingsPanel(window._lf6)
    window._bind_settings_signals(window._settings)
    window._tabs = QTabWidget(window)
    window.setCentralWidget(window._tabs)
    window._tabs.addTab(window._settings, "Settings")
    window._session_panels = {"settings": window._settings}
    window._tab_ids = {window._settings: "settings"}
    yield window
    window._settings.unbind_controller_signals()
    window._settings_connections.clear()
    window.hide()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    sys.modules["ui.settings_panel"] = module
    sys.modules["ui"].settings_panel = module
    cfg.__dict__.clear()
    cfg.__dict__.update(saved_cfg)


def test_reload_keeps_drafts_config_and_owned_connections(shell):
    old = shell._settings
    old._exposure.setValue(321)
    old._base_out_edit.setText("D:/draft-output")
    old._notifications.name_edit.setText("unfinished PC name")
    old._categories.setCurrentIndex(2)
    before = asdict(cfg)
    new = page_reload.reload_page(shell, "settings")
    assert new is not old
    assert new._ctrl is old._ctrl is shell._lf6
    assert new._exposure.value() == 321
    assert new._base_out_edit.text() == "D:/draft-output"
    assert new._notifications.name_edit.text() == "unfinished PC name"
    assert new._categories.currentIndex() == 2
    assert asdict(cfg) == before
    shell._lf6.settings_applied.emit()
    assert new._lf6_status.text() == "Applied ✓"
    assert old._lf6_status.text() != "Applied ✓"
    new.mff101_paths_changed.emit()
    shell._inst_panel.refresh_mff101_paths.assert_called_once()
    old.mff101_paths_changed.emit()
    shell._inst_panel.refresh_mff101_paths.assert_called_once()
    shell._lf6.apply_settings.assert_not_called()
    shell._lf6.disconnect_instrument.assert_not_called()
    shell._lf6.shutdown.assert_not_called()


def test_repeated_reload_emits_once_and_keeps_error_subscription(shell):
    for _ in range(3):
        page_reload.reload_page(shell, "settings")
    shell._settings.notification_configured.emit("test subscription")
    shell._ntfy_notifier.set_url.assert_called_once_with("test subscription")
    shell._lf6.error.emit("example")
    assert shell._settings._lf6_status.text() == "Error: example"
    shell._lf6.disconnected.emit()
    assert not shell._settings._apply_btn.isEnabled()
    shell._lf6.connected.emit({})
    assert shell._settings._apply_btn.isEnabled()


def test_failed_restore_retains_old_page_module_config_and_connections(shell, monkeypatch):
    old = shell._settings
    module = sys.modules["ui.settings_panel"]
    before = asdict(cfg)
    def fail(*_args):
        raise ValueError("bad edited state transfer")
    spec = page_reload.PAGE_RELOAD_REGISTRY["settings"]
    monkeypatch.setitem(page_reload.PAGE_RELOAD_REGISTRY, "settings", replace(spec, restore=fail))
    with pytest.raises(ValueError, match="bad edited"):
        page_reload.reload_page(shell, "settings")
    assert shell._settings is old
    assert shell._tabs.currentWidget() is old
    assert sys.modules["ui.settings_panel"] is module
    assert asdict(cfg) == before
    shell._lf6.error.emit("still alive")
    assert old._lf6_status.text() == "Error: still alive"


def test_shell_binding_failure_rolls_back_external_connections(shell, monkeypatch):
    old = shell._settings
    original = shell._rebind_reloaded_page
    def fail_after_binding(page_id, previous, replacement):
        original(page_id, previous, replacement)
        if previous is old:
            raise RuntimeError("binding failure")
    monkeypatch.setattr(shell, "_rebind_reloaded_page", fail_after_binding)
    with pytest.raises(RuntimeError, match="binding failure"):
        page_reload.reload_page(shell, "settings")
    assert shell._settings is old
    old.notification_configured.emit("still connected")
    shell._ntfy_notifier.set_url.assert_called_once_with("still connected")


def test_busy_settings_reject_before_source_loading(shell, monkeypatch):
    shell._lf6.is_busy = True
    monkeypatch.setattr(page_reload, "_source_path", Mock(side_effect=AssertionError("must not read code")))
    with pytest.raises(RuntimeError, match="Wait for instrument"):
        page_reload.reload_page(shell, "settings")


def test_current_page_action_routes_to_settings_without_restarting_controllers(shell):
    from ui.application_style import application_style
    shell._application_style = application_style(QApplication.instance())
    shell._initialize_style_actions()
    shell._acquisition_active = lambda: False
    old = shell._settings
    old._temp_edit.setText("draft temperature")
    action = next(action for action in shell._view_menu.actions()
                  if action.objectName() == "ReloadCurrentPage")
    assert action.shortcut().toString() == "Ctrl+Alt+U"
    action.trigger()
    assert shell._settings is not old
    assert shell._settings._temp_edit.text() == "draft temperature"
    assert shell._settings._ctrl is shell._lf6
    assert shell._status.currentMessage() == "Settings page reloaded — connections and data preserved"
