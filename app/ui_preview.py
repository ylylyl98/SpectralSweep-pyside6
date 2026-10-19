"""Start/restart a disposable UI process without importing the hardware runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


RELOAD_EXIT_CODE = 75
PREVIEW_ROOT_ENV = "SPECTRALSWEEP_PREVIEW_ROOT"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def app_command(*args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, str(PROJECT_ROOT / "main.py"), *args]


def launch_preview() -> None:
    """Use Qt's detached launch so the preview can outlive the measurement UI."""
    from PySide6.QtCore import QProcess
    command = app_command("--preview-spectrum")
    if sys.platform == "win32" and not getattr(sys, "frozen", False):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if pythonw.is_file():
            command[0] = str(pythonw)
    started, _pid = QProcess.startDetached(command[0], command[1:], str(PROJECT_ROOT))
    if not started:
        raise OSError("The preview process could not be started")


def prepare_preview_environment(root: Path) -> dict[str, str]:
    root = root.resolve()
    (root / "data").mkdir(parents=True, exist_ok=True)
    config = root / "config.json"
    # Write before importing utils.config, otherwise it can fall back to the
    # repository's config.json and inherit the real output paths.
    config.write_text(json.dumps({
        "filename": {"base_out": str(root / "data")},
        "session": {"sample_id": "PREVIEW"},
        "lf6": {"center_nm": 800.0, "exposure_ms": 250.0, "accumulations": 1},
    }), encoding="utf-8")
    env = os.environ.copy()
    env[PREVIEW_ROOT_ENV] = str(root)
    env["SPECTRALSWEEP_CONFIG_PATH"] = str(config)
    env["SPECTRALSWEEP_HISTORY_PATH"] = str(root / "history.sqlite3")
    env["SPECTRAL_MOCK_LF6"] = "1"
    return env


def run_preview() -> int:
    # The supervisor owns this directory across F5 restarts. Ordinary close
    # removes the scratch config and synthetic outputs, never production data.
    with tempfile.TemporaryDirectory(prefix="spectralsweep-preview-") as folder:
        root = Path(folder)
        env = prepare_preview_environment(root)
        while True:
            result = subprocess.run(app_command("--preview-child"), env=env, cwd=root)
            if result.returncode != RELOAD_EXIT_CODE:
                return result.returncode


def run_preview_child() -> int:
    root_text = os.environ.get(PREVIEW_ROOT_ENV, "")
    if not root_text:
        raise RuntimeError("Start previews with --preview-spectrum")
    root = Path(root_text).resolve()
    config = root / "config.json"
    if not config.is_file():
        raise RuntimeError("The isolated preview configuration is missing")
    # Override even if the inherited environment points at production config.
    os.environ["SPECTRALSWEEP_CONFIG_PATH"] = str(config)
    os.environ["SPECTRALSWEEP_HISTORY_PATH"] = str(root / "history.sqlite3")
    os.chdir(root)

    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication, QMessageBox
    from ui.application_identity import (
        PREVIEW_APP_ID, configure_window_taskbar, load_app_icon, set_windows_app_user_model_id,
    )

    set_windows_app_user_model_id(PREVIEW_APP_ID)
    app = QApplication(sys.argv)
    app.setApplicationName("SpectralSweep Preview")
    app.setApplicationDisplayName("SpectralSweep Preview")
    app.setOrganizationName("SpectralSweepPreview")
    app.setWindowIcon(load_app_icon())
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    for scope in (QSettings.Scope.UserScope, QSettings.Scope.SystemScope):
        QSettings.setPath(QSettings.Format.IniFormat, scope, str(root / "settings"))
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 9))
    try:
        from ui.spectrum_preview import SpectrumPreviewWindow
        window = SpectrumPreviewWindow(root)
    except Exception as exc:
        # A syntax/import error while editing must not start a crash/retry loop.
        import traceback
        traceback.print_exc()
        choice = QMessageBox.critical(
            None, "Spectrum preview could not load",
            f"{type(exc).__name__}: {exc}\n\nFix the source, then choose Retry. The measurement app is unaffected.",
            QMessageBox.StandardButton.Retry | QMessageBox.StandardButton.Close,
        )
        return RELOAD_EXIT_CODE if choice == QMessageBox.StandardButton.Retry else 1
    window.setWindowIcon(app.windowIcon())
    configure_window_taskbar(window, preview=True)
    window.show()
    try:
        return app.exec()
    finally:
        window.controller.abort_acquisition()
