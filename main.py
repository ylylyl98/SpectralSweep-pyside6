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
from PySide6.QtCore import QTimer

from ui.application_identity import (
    APP_NAME as _APP_NAME,
    configure_window_taskbar,
    load_app_icon as _load_app_icon,
    set_windows_app_user_model_id as _set_windows_app_user_model_id,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SpectralSweep PySide6")
    p.add_argument("--mock", action="store_true",
                   help="Use mock LF6 (no hardware required)")
    p.add_argument("--smoke-test", type=Path, metavar="OUTPUT",
                   help="Audit the packaged UI and worker without connecting instruments")
    p.add_argument("--watch-styles", action="store_true",
                   help="Reload application.qss automatically when edited")
    p.add_argument("--preview-spectrum", action="store_true",
                   help="Open an isolated Spectrum preview with synthetic data")
    p.add_argument("--preview-child", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--ui", choices=("widgets", "quick"), default="quick",
                   help="Qt Quick workspaces by default; use widgets for the compatibility frontend")
    p.add_argument("--preview-quick", action="store_true",
                   help="Open the isolated Qt Quick preview with synthetic data")
    p.add_argument("--preview-workspaces", action="store_true",
                   help="Open all Qt Quick workspaces with isolated settings and disconnected devices")
    p.add_argument("--preview-workspaces-child", action="store_true", help=argparse.SUPPRESS)
    return p.parse_args()


def main():
    args = _parse_args()
    if args.smoke_test is not None:
        from app.release_smoke import run
        sys.exit(run(args.smoke_test))
    if args.ui == "quick" or args.preview_quick or args.preview_workspaces or args.preview_workspaces_child:
        from app.quick_runtime import require_quick_runtime
        require_quick_runtime()

    # Branch before importing config, notifications, watchdogs or controllers.
    if args.preview_spectrum or args.preview_child or args.preview_quick or args.preview_workspaces or args.preview_workspaces_child:
        from app.ui_preview import run_preview, run_preview_child
        workspaces = args.preview_workspaces or args.preview_workspaces_child
        quick = workspaces or args.preview_quick or args.ui == "quick"
        if workspaces:
            sys.exit(run_preview_child(quick=True, workspaces=True) if args.preview_workspaces_child else run_preview(quick=True, workspaces=True))
        sys.exit(run_preview_child(quick=quick) if args.preview_child else run_preview(quick=quick))

    from app.ntfy_notifications import NtfyNotifier
    from app.runtime_exception_reporting import install as install_exception_reporting
    notifier = NtfyNotifier()
    uninstall_exception_reporting = install_exception_reporting(notifier)

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
    app.setProperty("spectralQuick", args.ui == "quick")
    app.setApplicationName(_APP_NAME)
    app.setApplicationDisplayName(_APP_NAME)
    app.setOrganizationName(_APP_NAME)
    icon = _load_app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)

    # Resolve startup dependencies before starting the background watchdog, so
    # an import failure cannot leave an orphan worker that blocks installation.
    from ui.main_window import MainWindow

    from app.ntfy_notifications import NTFY_URL
    from app.process_watchdog import WatchdogSession
    watchdog = WatchdogSession(NTFY_URL)
    watchdog.start()
    watchdog_timer = QTimer(app)
    watchdog_timer.setInterval(5000)
    watchdog_timer.timeout.connect(watchdog.beat)
    watchdog_timer.start()

    from ui.input_safety import install_input_wheel_guard
    install_input_wheel_guard(app)

    win = MainWindow(notifier=notifier, watch_styles=args.watch_styles, ui_mode=args.ui)
    win.notification_configured.connect(watchdog.enable)
    if not icon.isNull():
        win.setWindowIcon(icon)
    configure_window_taskbar(win)
    win.show()
    updates = getattr(win, '_software_updates', None)
    if updates is not None:
        updates.start_automatic()

    try:
        exit_code = app.exec()
        watchdog.close(normal_exit=True)
    finally:
        uninstall_exception_reporting()
        watchdog.close(normal_exit=False)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
