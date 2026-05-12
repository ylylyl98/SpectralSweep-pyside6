# main.py
# ──────────────────────────────────────────────────────────────────────────────
# Entry point for SpectralSweep PySide6 application.
#
# Usage:
#   python main.py
#   python main.py --mock        # force mock LF6 (no hardware needed)
#
# The app/ directory is never imported here; all instrument access goes through
# controllers/.
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import sys
import argparse
from pathlib import Path

# ── project root on sys.path ──────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon
from PySide6.QtCore    import Qt

_APP_NAME = "SpectralSweep"
_APP_ID = "SpectralSweep.SpectralSweep.Desktop"


def _resource_path(*parts: str) -> Path:
    """Return a source-tree or PyInstaller runtime resource path."""
    base = Path(getattr(sys, "_MEIPASS", _ROOT))
    return base.joinpath(*parts)


def _set_windows_app_user_model_id() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_APP_ID)
    except Exception:
        # Older Windows shells or restricted runtimes can ignore this safely.
        pass


def _load_app_icon() -> QIcon:
    icon_path = _resource_path("assets", "icons", "spectralsweep.ico")
    return QIcon(str(icon_path)) if icon_path.exists() else QIcon()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SpectralSweep PySide6")
    p.add_argument("--mock", action="store_true",
                   help="Use mock LF6 (no hardware required)")
    return p.parse_args()


def main():
    args = _parse_args()

    _set_windows_app_user_model_id()

    # Propagate --mock flag via environment so LF6Controller picks it up
    if args.mock:
        import os
        os.environ["SPECTRAL_MOCK_LF6"] = "1"

    # High-DPI scaling (Qt 6 enables it by default; this is explicit)
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName(_APP_NAME)
    app.setApplicationDisplayName(_APP_NAME)
    app.setOrganizationName(_APP_NAME)
    icon = _load_app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)

    from ui.main_window import MainWindow
    win = MainWindow()
    if not icon.isNull():
        win.setWindowIcon(icon)
    win.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
