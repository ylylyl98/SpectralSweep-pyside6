"""Stage timings for native-hidden, simulated Quick startup (no device runtime)."""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def profile_startup(mode="preview", *, native=False):
    started = time.perf_counter()
    if not native:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    else:
        os.environ.pop("QT_QPA_PLATFORM", None)
    records = []

    @contextmanager
    def span(name):
        begin = time.perf_counter()
        try:
            yield
        finally:
            records.append({"stage": name, "ms": round((time.perf_counter() - begin) * 1000, 3)})

    from app.ui_preview import prepare_preview_environment
    with tempfile.TemporaryDirectory(prefix="spectralsweep-quick-profile-") as folder:
        scratch = Path(folder)
        env = prepare_preview_environment(scratch)
        for key in ("SPECTRALSWEEP_PREVIEW_ROOT", "SPECTRALSWEEP_CONFIG_PATH",
                    "SPECTRALSWEEP_HISTORY_PATH", "SPECTRAL_MOCK_LF6"):
            os.environ[key] = env[key]
        with span("imports"):
            import PySide6
            from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
            from PySide6.QtGui import QFont, QFontDatabase
            from PySide6.QtQuickWidgets import QQuickWidget
            from PySide6.QtTest import QTest
            from PySide6.QtWidgets import QApplication, QComboBox, QMainWindow, QPushButton, QStatusBar, QTabWidget, QVBoxLayout, QWidget
            from app.experiment_metadata import ExperimentHistory
            from ui.spectrum_preview import SpectrumPreviewWindow, PreviewSpectrumController
            from ui.quick_preview import enhance_preview
            from ui.quick_workbench import QuickWorkbench
            from ui.spectrum_panel import SpectrumPanel
        with span("application"):
            app = QApplication([])
            app.setStyle("Fusion")
            app.setProperty("spectralQuick", True)
            app.setOrganizationName("SpectralSweepProfile")
            app.setApplicationName("QuickStartupProfile")
            QSettings.setDefaultFormat(QSettings.Format.IniFormat)
            for scope in (QSettings.Scope.UserScope, QSettings.Scope.SystemScope):
                QSettings.setPath(QSettings.Format.IniFormat, scope, str(scratch / "settings"))
            if not QFontDatabase.families():
                font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
                if font.exists():
                    QFontDatabase.addApplicationFont(str(font))
            app.setFont(QFont("Segoe UI", 9))
        original_init, original_source = QQuickWidget.__init__, QQuickWidget.setSource
        original_upsert, original_connect = ExperimentHistory.upsert, ExperimentHistory._connect
        original_write = Path.write_text
        widgets = []

        def widget_init(widget, *args, **kwargs):
            with span("qml.widget_constructor"):
                original_init(widget, *args, **kwargs)
            widgets.append(widget)

        def source(widget, url):
            with span("qml.load." + Path(url.toLocalFile()).name):
                return original_source(widget, url)

        def upsert(history, *args, **kwargs):
            with span("history.upsert"):
                return original_upsert(history, *args, **kwargs)

        def connect(history):
            with span("history.connect"):
                return original_connect(history)

        def write(path, *args, **kwargs):
            if path.name.startswith("preview-run-"):
                with span("history.fixture_write"):
                    return original_write(path, *args, **kwargs)
            return original_write(path, *args, **kwargs)

        with patch.object(QQuickWidget, "__init__", widget_init), patch.object(QQuickWidget, "setSource", source), \
                patch.object(ExperimentHistory, "upsert", upsert), patch.object(ExperimentHistory, "_connect", connect), \
                patch.object(Path, "write_text", write):
            construction_started = time.perf_counter()
            if mode == "preview":
                with span("spectrum.widgets"):
                    window = SpectrumPreviewWindow(scratch)
                with span("quick.enhance_preview"):
                    enhance_preview(window, scratch)
            else:
                with span("workbench.simulated_widgets"):
                    window = QMainWindow()
                    tabs = window._tabs = QTabWidget()
                    window.setCentralWidget(tabs)
                    window._lf6 = PreviewSpectrumController(window)
                    window._spectrum = SpectrumPanel(window._lf6)
                    window._tab_ids = {window._spectrum: "spectrum"}
                    tabs.addTab(window._spectrum, "Spectrum")
                    for attr, key, title in (("_presets", "dual_gate", "Dual Gate"), ("_mega", "mega_sweep", "2D Sweep"),
                            ("_power_sweep", "power_sweep", "Motion Sweep"), ("_mcd", "mcd", "MCD 1000"),
                            ("_mcd2100", "mcd2100", "MCD 2100"), ("_bfp", "bfp", "BFP")):
                        panel = QWidget()
                        panel.metadata_finalizing, panel._interlock_held = False, False
                        panel._thread = panel.thread = None
                        layout = QVBoxLayout(panel)
                        for name in ("_run_btn", "_stop_btn", "_apply_btn", "_acquire_btn"):
                            button = QPushButton(name, panel)
                            layout.addWidget(button)
                            setattr(panel, name, button)
                        panel.start_btn, panel.stop_btn = panel._run_btn, panel._stop_btn
                        tabs.addTab(panel, title)
                        window._tab_ids[panel] = key
                        setattr(window, attr, panel)
                    window._sample_id_binder = SimpleNamespace(value="PREVIEW")
                    window._history_type = QComboBox()
                    window._history_type.addItem("dual_gate_sweep")
                    window._history = ExperimentHistory(scratch / "workbench.sqlite")
                    window._status = QStatusBar(window)
                    window.setStatusBar(window._status)
                    window._active_mcd_panel, window._power_sweep_running = None, False
                    window._active_tab_id = lambda: window._tab_ids[tabs.currentWidget()]
                with span("quick.workbench"):
                    window._quick_workbench = QuickWorkbench(window)
            with span("window.first_grab"):
                window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
                window.resize(1400, 900)
                window.show()
                app.processEvents()
                image = window.grab()
                assert not image.isNull()
            first_window_ms = (time.perf_counter() - construction_started) * 1000
            if mode == "preview":
                with span("history.first_visible_grab"):
                    window.preview_tabs.setCurrentIndex(1)
                    deadline = time.perf_counter() + 5
                    while window.quick_history.view_model.loading and time.perf_counter() < deadline:
                        QTest.qWait(5)
                    app.processEvents()
                    assert window.quick_history.view_model.total == 65
                    assert not window.quick_history.grabFramebuffer().isNull()
            for widget in widgets:
                assert widget.status() == QQuickWidget.Status.Ready, [error.toString() for error in widget.errors()]
            if hasattr(window, "_quick_workbench"):
                window._quick_workbench.shutdown()
            else:
                window.quick_commands.shutdown()
                window.quick_history.shutdown()
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
        assert not any(name == "controllers" or name.startswith("controllers.") for name in sys.modules)
        summaries = defaultdict(lambda: {"count": 0, "total_ms": 0.0})
        for record in records:
            summary = summaries[record["stage"]]
            summary["count"] += 1
            summary["total_ms"] = round(summary["total_ms"] + record["ms"], 3)
        return {"mode": mode, "platform": app.platformName(), "pyside6": PySide6.__version__,
                "quick_backend": os.environ.get("QT_QUICK_BACKEND", "default"),
                "construction_to_first_grab_ms": round(first_window_ms, 3),
                "process_measurement_ms": round((time.perf_counter() - started) * 1000, 3),
                "stages": dict(summaries), "records": records,
                "note": "Nested stages overlap; first grab is synchronous and not a render-thread presentation timestamp."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preview", "workbench"), default="preview")
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = profile_startup(args.mode, native=args.native)
    encoded = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "records"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
