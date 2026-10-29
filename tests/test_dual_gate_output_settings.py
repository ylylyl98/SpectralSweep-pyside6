"""The infrequently changed output subfolder lives in Settings, not metadata."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMainWindow

from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.main_window import MainWindow
from ui.presets_panel import PresetsPanel
from ui.settings_panel import SettingsPanel
from ui.quick_parameter_forms import install_parameter_forms
from ui.quick_settings_layout import configure_quick_settings
from utils.config import AppConfig, cfg


@pytest.mark.parametrize("quick", [False, True])
def test_settings_subfolder_drives_output_and_survives_reload_and_disk(quick, monkeypatch, tmp_path):
    app = quick_application()
    previous = app.property("spectralQuick")
    app.setProperty("spectralQuick", quick)
    monkeypatch.setattr(cfg.filename, "dual_gate_subfolder", "Initial Data")
    monkeypatch.setattr(cfg, "save", lambda: None)
    shell = QMainWindow()
    presets = PresetsPanel()
    settings = SettingsPanel()
    replacements = []
    try:
        MainWindow._bind_dual_gate_subfolder(shell, presets, settings)
        if quick:
            configure_quick_settings(settings)
            coverage = install_parameter_forms(settings, "settings")
            settings._categories.setCurrentIndex(1)
            settings.resize(1000, 700)
            settings.show()
            QTest.qWait(100)
            fields = [field for form in coverage["forms"]
                      for field, source in form.adapter._fields.items()
                      if source is settings._dual_gate_subfolder]
            assert len(fields) == 1
            assert fields[0].edit("Measurements")
        else:
            settings._dual_gate_subfolder.setText("Measurements")
        assert cfg.filename.dual_gate_subfolder == "Measurements"
        meta = presets._current_run_meta(include_executed_plan=False)
        assert meta["subfolder"] == "Measurements"
        assert presets._current_output_dir(meta).name == "Measurements"
        assert presets._subfolder_edit.isHidden()

        # Loading older experiments still restores their original output path.
        presets.restore_session_state({"metadata": {"subfolder": "Legacy data"}})
        assert settings._dual_gate_subfolder.text() == "Legacy data"
        assert cfg.filename.dual_gate_subfolder == "Legacy data"

        new_settings = SettingsPanel()
        replacements.append(new_settings)
        MainWindow._bind_dual_gate_subfolder(shell, presets, new_settings)
        settings._dual_gate_subfolder.setText("Detached old page")
        assert presets._subfolder_edit.text() == "Legacy data"
        new_presets = PresetsPanel()
        replacements.append(new_presets)
        new_presets.restore_session_state(presets.capture_session_state())
        MainWindow._bind_dual_gate_subfolder(shell, new_presets, new_settings)
        new_settings._dual_gate_subfolder.setText("After reload")
        assert new_presets._subfolder_edit.text() == "After reload"
        assert presets._subfolder_edit.text() == "Legacy data"
        # The legacy metadata key is the sole session copy; Settings reload
        # separately captures all its source controls without replaying config.
        assert new_presets.capture_session_state()["metadata"]["subfolder"] == "After reload"
        from ui.settings_reload_state import capture_settings_state, restore_settings_state
        state = capture_settings_state(new_settings)
        restored_settings = SettingsPanel()
        replacements.append(restored_settings)
        restore_settings_state(restored_settings, state)
        assert restored_settings._dual_gate_subfolder.text() == "After reload"

        path = tmp_path / "config.json"
        AppConfig.save(cfg, path)
        loaded = AppConfig()
        loaded.load(path)
        assert loaded.filename.dual_gate_subfolder == "After reload"
    finally:
        shell._subfolder_connections.clear()
        for panel in (presets, settings, *replacements):
            for form in getattr(panel, "_quick_parameter_forms", {}).get("forms", []):
                form.shutdown()
            if hasattr(panel, "dispose_reloaded_page"):
                panel.dispose_reloaded_page()
            panel.close()
            panel.deleteLater()
        shell.deleteLater()
        QTest.qWait(1)
        drain_qt_deletions()
        app.setProperty("spectralQuick", previous)
