"""Configurable physical light-path labels must remain passive and global."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from tests.test_mff101_control import Device, connect
from utils.config import AppConfig, MFF101Config, cfg


@pytest.fixture
def app(monkeypatch):
    application = QApplication.instance() or QApplication([])
    monkeypatch.setattr(cfg, "mff101", MFF101Config())
    return application


def test_old_config_gets_lab_mapping_and_edits_round_trip(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"mff101": {"serial_number": "37862922"}}', encoding="utf-8")
    config = AppConfig()
    config.load(path)
    assert config.mff101.position_1_path == "Andor"
    assert config.mff101.position_2_path == "LightField"
    config.mff101.position_1_path = "LightField"
    config.mff101.position_2_path = "New spectrometer"
    config.save(path)
    restored = AppConfig()
    restored.load(path)
    assert restored.mff101.serial_number == "37862922"
    assert restored.mff101.position_1_path == "LightField"
    assert restored.mff101.position_2_path == "New spectrometer"


def test_edit_updates_sidebar_without_motion_or_new_readback_timestamp(app):
    from controllers.mff101_controller import MFF101Controller
    from ui.instrument_panel import InstrumentPanel
    from ui.settings_panel import SettingsPanel

    device = Device()
    ctrl = MFF101Controller(adapter_factory=device.open)
    sidebar = InstrumentPanel(mff101_ctrl=ctrl)
    settings = SettingsPanel()
    try:
        settings.mff101_paths_changed.connect(sidebar.refresh_mff101_paths)
        assert settings._mff101_position1_path.text() == "Andor"
        assert settings._mff101_position2_path.text() == "LightField"
        connect(app, ctrl)
        section = sidebar._sections["mff101"]
        assert "Andor" in section._position.text()
        observed = section._spectrum_readbacks["position"]["timestamp_utc"]
        calls = list(device.calls)
        settings._mff101_position1_path.setText("LightField")
        settings._mff101_position2_path.setText("Andor")
        assert "LightField" in section._position.text()
        assert "LightField" in section._position1.toolTip()
        assert "Andor" in section._position2.toolTip()
        assert section._spectrum_readbacks["position"]["timestamp_utc"] == observed
        assert section._spectrum_readbacks["position"]["configured_path"] == "LightField"
        assert device.calls == calls
        assert settings.capture_session_state()["mff101"] == {
            "position_1_path": "LightField", "position_2_path": "Andor",
            "auto_route": False, "andor_position": 1, "lightfield_position": 2,
        }
    finally:
        sidebar.close()
        settings.close()
        assert ctrl.shutdown()


@pytest.mark.parametrize('auto_route', [False, True])
def test_settings_save_and_reopen_keeps_custom_names_and_routing(app, tmp_path, monkeypatch, auto_route):
    import utils.config as config_module
    from ui.settings_panel import SettingsPanel

    path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "_CONFIG_FILE", path)
    settings = SettingsPanel()
    reopened = None
    try:
        settings._mff101_position1_path.setText("Camera A")
        settings._mff101_position2_path.setText("Camera B")
        assert not settings._mff101_auto_route.isChecked()
        settings._mff101_auto_route.setChecked(auto_route)
        settings._save_btn.click()
        loaded = AppConfig()
        loaded.load(path)
        monkeypatch.setattr(cfg, "mff101", loaded.mff101)
        reopened = SettingsPanel()
        assert reopened._mff101_position1_path.text() == "Camera A"
        assert reopened._mff101_position2_path.text() == "Camera B"
        assert reopened._mff101_auto_route.isChecked() is auto_route
        assert cfg.mff101.auto_route is auto_route
    finally:
        settings.close()
        if reopened is not None:
            reopened.close()


def test_sample_and_history_restore_do_not_replace_physical_mapping(app):
    from ui.main_window import MainWindow
    from ui.settings_panel import SettingsPanel

    settings = SettingsPanel()
    try:
        settings._mff101_position1_path.setText("Camera A")
        settings.restore_session_state({"mff101": {
            "position_1_path": "Old path", "position_2_path": "Old camera",
            "auto_route": True,
        }})
        assert not cfg.mff101.auto_route
        assert cfg.mff101.position_1_path == "Camera A"
        assert settings._mff101_position1_path.text() == "Camera A"
        profile = MainWindow._sample_scoped_state({"panels": {"settings": settings.capture_session_state()}})
        assert "mff101" not in profile["panels"]["settings"]
    finally:
        settings.close()
