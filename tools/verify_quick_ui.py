"""Render and exercise the isolated Quick preview; never import device controllers.

Run with the project interpreter. --native uses Windows' real Qt platform with
WA_DontShowOnScreen, so it does not open an interactive window on the desktop.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "tmp" / "quick-ui-verification")
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--frontend", choices=("widgets", "quick"), default="quick")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.native:
        os.environ.pop("QT_QPA_PLATFORM", None)
    else:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from app.ui_preview import prepare_preview_environment
    with tempfile.TemporaryDirectory(prefix="spectralsweep-quick-check-") as folder:
        scratch = Path(folder)
        env = prepare_preview_environment(scratch)
        for key in ("SPECTRALSWEEP_PREVIEW_ROOT", "SPECTRALSWEEP_CONFIG_PATH",
                    "SPECTRALSWEEP_HISTORY_PATH", "SPECTRAL_MOCK_LF6"):
            os.environ[key] = env[key]
        import PySide6
        from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt, QTimer
        from PySide6.QtGui import QFont, QFontDatabase
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication
        from ui.spectrum_preview import SpectrumPreviewWindow
        from ui.quick_preview import enhance_preview
        app = QApplication([])
        app.setOrganizationName("SpectralSweepVerification")
        app.setApplicationName("QuickVerification")
        app.setProperty("spectralQuick", args.frontend == "quick")
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        for scope in (QSettings.Scope.UserScope, QSettings.Scope.SystemScope):
            QSettings.setPath(QSettings.Format.IniFormat, scope, str(scratch / "settings"))
        if not QFontDatabase.families():
            font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
            if font.exists():
                QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Segoe UI", 9))
        started = time.perf_counter()
        window = SpectrumPreviewWindow(scratch)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        if args.frontend == "quick":
            enhance_preview(window, scratch)
        warnings = []
        quick_widgets = (window.quick_commands, window.quick_history) if args.frontend == "quick" else ()
        for widget in quick_widgets:
            widget.engine().warnings.connect(lambda errors: warnings.extend(e.toString() for e in errors))
        window.resize(1400, 900)
        window.show()
        QTest.qWait(150)
        window_ready_ms = (time.perf_counter() - started) * 1000
        window.grab().save(str(args.output / "spectrum.png"))
        bridge = window.quick_commands.adapter.presenter if args.frontend == "quick" else None
        # Fake controller only: no real wavelength confirmation or SDK.
        from unittest.mock import patch
        intervals = []
        last = [time.perf_counter()]
        timer = QTimer()
        timer.setInterval(10)
        def tick():
            now = time.perf_counter()
            intervals.append((now - last[0]) * 1000)
            last[0] = now
        timer.timeout.connect(tick)
        timer.start()
        with patch("ui.detector_wavelength_advice.confirm_detector_wavelength", return_value=True):
            if bridge is not None:
                assert bridge.invoke("run"), "Run command was rejected"
            else:
                window.panel._run_1d_btn.click()
            QTest.qWait(1200)
            if bridge is not None:
                window.quick_commands.adapter.refresh()
                assert bridge.invoke("stop"), "Stop command was rejected"
            else:
                window.panel._stop_btn.click()
        stopped_frame = window.controller._frame
        QTest.qWait(300)
        assert window.controller._frame == stopped_frame, "A cancelled preview produced another frame"
        timer.stop()
        if args.frontend == "quick":
            window.preview_tabs.setCurrentIndex(1)
            QTest.qWait(250)
            for mode in ("light", "dark"):
                window.quick_history.set_theme(mode)
                QTest.qWait(80)
                window.quick_history.grabFramebuffer().save(str(args.output / f"history-{mode}.png"))
            window.quick_history.set_theme("light")
            assert window.quick_sequence_demo is None, "Sequence example loaded before its tab was selected"
            window.preview_tabs.setCurrentIndex(2)
            QTest.qWait(80)
            demo = window.quick_sequence_demo
            preview = demo.preview
            assert preview.sequence_model.totalSteps == 90
            preview.sequence_model.goToStep("61")
            preview.show_full_sequence()
            full = preview._full_sequence_preview
            dialog = preview._full_sequence_dialog
            dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            QTest.qWait(50)
            assert full.sequence_model.selectedDetail["title"] == "Step 61"
            full.quick_widget.grabFramebuffer().save(str(args.output / "dual-gate-sequence.png"))
            assert not full.qml_warnings, full.qml_warnings
            dialog.close()
            demo.mode.setCurrentIndex(1)
            preview.sequence_model.setShowSkipped(True)
            assert preview.sequence_model.supportsSkipped
            assert preview.sequence_model.rowCount() == 8
            assert not preview.qml_warnings, preview.qml_warnings
            preview.quick_widget.grabFramebuffer().save(str(args.output / "dual-gate-when.png"))
        window.resize(800, 650)
        QTest.qWait(80)
        window.grab().save(str(args.output / "narrow.png"))
        loaded_controllers = [name for name in sys.modules if name.startswith("controllers.")]
        assert not loaded_controllers, loaded_controllers
        assert not warnings, warnings
        report = {
            "python": sys.version.split()[0], "pyside6": PySide6.__version__,
            "platform": app.platformName(), "scale": os.environ.get("QT_SCALE_FACTOR", "system"),
            "frontend": args.frontend,
            "module_root": str(Path(__file__).resolve().parents[1]),
            # Includes construction and a fixed settling wait; it is not a
            # presentation-time measurement from the render thread.
            "window_ready_ms": round(window_ready_ms, 2), "settle_wait_ms": 150,
            "quick_backend": os.environ.get("QT_QUICK_BACKEND", "default"),
            "simulated_frames": stopped_frame, "event_target_interval_ms": 10,
            "event_interval_median_ms": round(statistics.median(intervals), 2),
            "event_interval_p95_ms": round(sorted(intervals)[int((len(intervals) - 1) * .95)], 2),
            "event_interval_max_ms": round(max(intervals), 2),
            "stop_no_late_frame": True, "qml_warnings": warnings,
            "hardware_controllers_loaded": loaded_controllers,
        }
        for widget in quick_widgets:
            widget.shutdown()
        if args.frontend == "quick" and window.quick_sequence_demo is not None:
            window.quick_sequence_demo.shutdown()
        window.controller.abort_acquisition()
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
