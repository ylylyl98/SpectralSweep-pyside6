"""Render the real Dual Gate editor with Quick review and no device controllers."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "tmp" / "dual-gate-editor-quick")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ["QT_QPA_PLATFORM"] = "windows" if args.native else "offscreen"
    from app.ui_preview import prepare_preview_environment
    with tempfile.TemporaryDirectory(prefix="dual-gate-review-") as folder:
        root = Path(folder)
        environment = prepare_preview_environment(root)
        for name in ("SPECTRALSWEEP_PREVIEW_ROOT", "SPECTRALSWEEP_CONFIG_PATH", "SPECTRALSWEEP_HISTORY_PATH", "SPECTRAL_MOCK_LF6"):
            os.environ[name] = environment[name]
        from PySide6.QtCore import QCoreApplication, QEvent, QSettings, QPoint, Qt
        from PySide6.QtGui import QFont
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication
        from ui.presets_panel import PresetsPanel
        from ui.application_style import application_style
        app = QApplication([])
        app.setStyle("Fusion")
        app.setFont(QFont("Segoe UI", 9))
        application_style(app).reload()
        app.setProperty("spectralQuick", True)
        app.setOrganizationName("SpectralSweepVerification")
        app.setApplicationName("DualGateReview")
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        for scope in (QSettings.Scope.UserScope, QSettings.Scope.SystemScope):
            QSettings.setPath(QSettings.Format.IniFormat, scope, str(root / "settings"))
        panel = PresetsPanel()
        panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        state = panel.capture_session_state()
        loops = [dict(Enable=True, Parameter=name, Values=values, Group=i + 1)
                 for i, (name, values) in enumerate((("Center Wavelength (nm)", "750,810"),
                                                     ("Rotation1 Angle (deg)", "0,45,90")))]
        rows = [dict(Run=True, When=when, MeasurePower=False, condition_label=label,
                     repeat=2, frames=5, Vtg_start=start, Vtg_stop=stop, Vbg_start=-1,
                     Vbg_stop=1, Vbias_start=0, Vbias_stop=0)
                for label, start, stop, when in (("Forward", -2, 2, ""), ("Return", 2, -2, "Center_Wavelength > 800"))]
        order = ["conditions", "points", "group:1", "group:2"]
        state.update(draft_loop=loops, applied_loop=loops, draft_batch=rows, applied_batch=rows,
                     loop_mode="Synchronize", applied_loop_mode="Synchronize",
                     execution_order=order, applied_execution_order=order, nested_schedule_enabled=True)
        panel.restore_session_state(state)
        sizes = []
        for width, height in ((1400, 1000), (1000, 800)):
            panel.resize(width, height)
            panel.show()
            QTest.qWait(80)
            assert panel._tree.sequence_model.totalSteps == 90
            panel._tree.sequence_model.goToStep("61")
            QTest.qWait(30)
            assert panel.grab().save(str(args.output / f"editor-{width}x{height}.png"))
            # The existing parameter pane scrolls at short window heights.
            # Verify the complete new preview can be reached independently
            # while Apply/Run/Stop remain outside that scroll area.
            panel._results_scroll.ensureWidgetVisible(panel._tree, 0, 0)
            QTest.qWait(30)
            assert panel.grab().save(str(args.output / f"editor-{width}x{height}-review.png"))
            footer = {}
            for name in ("_run_btn", "_stop_btn", "_apply_btn"):
                button = getattr(panel, name)
                point = button.mapTo(panel, QPoint(0, 0))
                footer[name] = point.y() >= 0 and point.y() + button.height() <= panel.height()
            assert all(footer.values()), footer
            sizes.append(dict(requested=[width, height], actual=[panel.width(), panel.height()], footer_in_bounds=footer))
        assert panel._run_thread is None
        assert all(getattr(panel, name) is None for name in ("_lf6", "_smu", "_rot", "_stage", "_pm"))
        assert not panel._tree.qml_warnings, panel._tree.qml_warnings
        assert not any(name == "controllers" or name.startswith("controllers.") for name in sys.modules)
        report = dict(platform=app.platformName(), sizes=sizes, qml_warnings=panel._tree.qml_warnings,
                      compiled_steps=90, device_controllers_created=False)
        panel.dispose_reloaded_page()
        panel.close()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
