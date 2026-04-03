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
from PySide6.QtCore    import Qt


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SpectralSweep PySide6")
    p.add_argument("--mock", action="store_true",
                   help="Use mock LF6 (no hardware required)")
    return p.parse_args()


def main():
    args = _parse_args()

    # Propagate --mock flag via environment so LF6Controller picks it up
    if args.mock:
        import os
        os.environ["SPECTRAL_MOCK_LF6"] = "1"

    # High-DPI scaling (Qt 6 enables it by default; this is explicit)
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("SpectralSweep")
    app.setOrganizationName("SpectralSweep")

    from ui.main_window import MainWindow
    win = MainWindow()
    win.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
