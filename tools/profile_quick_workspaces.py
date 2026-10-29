"""Repeatable isolated nine-workspace performance probe.

Default runs fresh child processes. Timings use real routing, QML keyboard
events and synchronous framebuffer grabs, never a fixed settling delay.
No MainWindow, device runtime, notification service or production settings.
"""
from __future__ import annotations

import time
PROCESS_ENTER_NS = time.perf_counter_ns()

import argparse
from collections import defaultdict
import cProfile
import ctypes
from ctypes import wintypes
import importlib
import hashlib
import json
import os
from pathlib import Path
import pstats
import statistics
import subprocess
import sys
import tempfile
import traceback
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def distribution(values):
    """Linear interpolated percentiles; raw samples stay in the report."""
    values = sorted(values)
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    def percentile(p):
        pos = (len(values) - 1) * p
        low = int(pos)
        high = min(low + 1, len(values) - 1)
        return round(values[low] + (values[high] - values[low]) * (pos - low), 3)
    return {"count": len(values), "p50_ms": percentile(.5), "p95_ms": percentile(.95),
            "max_ms": round(values[-1], 3), "stalls_over_50ms": sum(v > 50 for v in values),
            "stalls_over_100ms": sum(v > 100 for v in values)}


def memory_bytes():
    if os.name != "nt":
        import resource
        return {"peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                "rss_bytes": None}
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
            "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
            "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    get_info = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if not get_info(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"rss_bytes": counters.WorkingSetSize, "peak_rss_bytes": counters.PeakWorkingSetSize}


def source_fingerprint():
    """Detect UI changes between fresh-process runs of one comparison."""
    digest = hashlib.sha256()
    paths = sorted(p for folder in (PROJECT_ROOT / "ui", PROJECT_ROOT / "app")
                   for p in folder.rglob("*") if p.suffix in {".py", ".qml"})
    for path in paths:
        digest.update(path.relative_to(PROJECT_ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return {"sha256": digest.hexdigest(), "files": len(paths)}


def run_worker(args):
    os.environ["QT_QPA_PLATFORM"] = "windows" if args.native and os.name == "nt" else "offscreen"
    if args.backend == "software":
        os.environ["QT_QUICK_BACKEND"] = "software"
    else:
        os.environ.pop("QT_QUICK_BACKEND", None)
    from tools.verify_quick_workspaces import HardwareImportGuard
    from app.ui_preview import prepare_preview_environment
    with tempfile.TemporaryDirectory(prefix="spectralsweep-perf-") as directory:
        scratch = Path(directory)
        env = prepare_preview_environment(scratch)
        for key in ("SPECTRALSWEEP_PREVIEW_ROOT", "SPECTRALSWEEP_CONFIG_PATH",
                    "SPECTRALSWEEP_HISTORY_PATH", "SPECTRAL_MOCK_LF6"):
            os.environ[key] = env[key]
        guard = HardwareImportGuard()
        sys.meta_path.insert(0, guard)
        import_started = time.perf_counter_ns()
        import PySide6
        from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QSettings, QTimer, Qt, qInstallMessageHandler
        from PySide6.QtGui import QFont
        from PySide6.QtQuickWidgets import QQuickWidget
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QLineEdit
        from shiboken6 import getCppPointer, isValid
        from ui.application_style import application_style
        from ui.quick_workspaces_preview import build_preview_window
        from utils.config import cfg
        import_ms = (time.perf_counter_ns() - import_started) / 1e6
        messages, errors, saves = [], [], []
        prior_message = qInstallMessageHandler(lambda kind, context, message: messages.append({"kind": kind.name, "message": message}))
        prior_hook = sys.excepthook
        sys.excepthook = lambda kind, value, tb: errors.append("".join(traceback.format_exception(kind, value, tb)))
        cfg.save = lambda *a, **kw: saves.append("blocked")
        app_started = time.perf_counter_ns()
        app = QApplication([])
        app.setOrganizationName("SpectralSweepPerformance")
        app.setApplicationName("IsolatedWorkspaces")
        app.setProperty("spectralQuick", True)
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        for scope in (QSettings.Scope.UserScope, QSettings.Scope.SystemScope):
            QSettings.setPath(QSettings.Format.IniFormat, scope, str(scratch / "settings"))
        app.setStyle("Fusion")
        app.setFont(QFont("Segoe UI", 9))
        application_style(app).reload()
        app_ms = (time.perf_counter_ns() - app_started) / 1e6

        calls = defaultdict(lambda: {"count": 0, "total_ms": 0., "max_ms": 0.})
        phase = ["build"]
        patches = []
        # Timed Python callback boundaries only: no per-call sys.setprofile
        # overhead during headline measurements. Nested totals overlap.
        targets = {
            "ui.quick_workbench": {"QuickWorkbench": ("refresh", "_ensure_page_editors", "_ensure_sidebar_editors", "_resize_commands", "_fit_workspace")},
            "ui.quick_parameter_forms": {"WidgetsParameterAdapter": ("refresh", "_source_changed"), "QuickParameterForm": ("_resize_height",)},
            "ui.quick_controls": {"PanelCommandAdapter": ("refresh",), "QuickCommandWidget": ("_resize_height",)},
            "ui.quick_table_editor": {"TableEditorModel": ("refresh",), "QuickTableEditor": ("_fit_height",)},
            "ui.quick_scientific_plot": {"ScientificPlotItem": ("paint",), "QuickCurve": ("setData",)},
            "ui.quick_dual_gate_sequence": {"QuickDualGateSequencePreview": ("update_plan",)},
        }
        for module_name, classes in targets.items():
            module = importlib.import_module(module_name)
            for cls_name, methods in classes.items():
                cls = getattr(module, cls_name)
                for name in methods:
                    original = getattr(cls, name, None)
                    if original is None:
                        continue
                    label = cls_name + "." + name
                    def wrapped(self, *a, _original=original, _label=label, **kw):
                        key = phase[0] + ":" + _label
                        start = time.perf_counter_ns()
                        try:
                            return _original(self, *a, **kw)
                        finally:
                            elapsed = (time.perf_counter_ns() - start) / 1e6
                            record = calls[key]
                            record["count"] += 1
                            record["total_ms"] += elapsed
                            record["max_ms"] = max(record["max_ms"], elapsed)
                    p = patch.object(cls, name, wrapped)
                    p.start()
                    patches.append(p)
        qml_loads, surfaces = [], []
        original_init, original_source = QQuickWidget.__init__, QQuickWidget.setSource
        def widget_init(widget, *a, **kw):
            original_init(widget, *a, **kw)
            surfaces.append(widget)
        def set_source(widget, url):
            start = time.perf_counter_ns()
            try:
                return original_source(widget, url)
            finally:
                qml_loads.append({"phase": phase[0], "source": "" if url.isEmpty() else url.fileName(),
                                  "ms": (time.perf_counter_ns() - start) / 1e6})
        for name, replacement in (("__init__", widget_init), ("setSource", set_source)):
            p = patch.object(QQuickWidget, name, replacement)
            p.start()
            patches.append(p)
        heartbeat = defaultdict(list)
        last_beat = [time.perf_counter_ns()]
        def beat():
            now = time.perf_counter_ns()
            heartbeat[phase[0]].append((now - last_beat[0]) / 1e6)
            last_beat[0] = now
        timer = QTimer()
        timer.setTimerType(Qt.TimerType.PreciseTimer)
        timer.setInterval(10)
        timer.timeout.connect(beat)
        timer.start()
        profiler = cProfile.Profile() if args.profile_worker else None
        if profiler:
            profiler.enable()
        fingerprint = source_fingerprint()
        construction_start = time.perf_counter_ns()
        window = build_preview_window(scratch, app, hidden=True)
        construction_ms = (time.perf_counter_ns() - construction_start) / 1e6
        shell = window.quick_workbench

        def drain():
            # Process posted resize/source notifications without an artificial
            # settling sleep. Framebuffer grabs provide the rendering boundary.
            app.processEvents()
            app.processEvents()

        def grab_visible():
            start = time.perf_counter_ns()
            visible = [w for w in surfaces if isValid(w) and w.isVisibleTo(window)]
            for widget in visible:
                if widget.status() != QQuickWidget.Status.Ready:
                    raise RuntimeError([error.toString() for error in widget.errors()])
                if widget.grabFramebuffer().isNull():
                    raise RuntimeError("Null QML framebuffer")
            if window.grab().isNull():
                raise RuntimeError("Null workspace framebuffer")
            return {"ms": (time.perf_counter_ns() - start) / 1e6, "visible_surfaces": len(visible)}

        window.show()
        drain()
        first_grab = grab_visible()
        cold_end = time.perf_counter_ns()
        startup = {"process_entry_to_first_grab_ms": (cold_end - PROCESS_ENTER_NS) / 1e6,
                   "launch_to_first_grab_ms": (cold_end - args.launch_ns) / 1e6 if args.launch_ns else None,
                   "imports_ms": import_ms, "application_ms": app_ms,
                   "construction_ms": construction_ms, "first_grab": first_grab,
                   "qml_synchronous_load_ms": sum(row["ms"] for row in qml_loads if row["source"]),
                   "surfaces": len([w for w in surfaces if isValid(w)]),
                   "engines": len({getCppPointer(w.engine())[0] for w in surfaces if isValid(w)})}
        first, warm = {}, defaultdict(list)

        def switch(page_id):
            start = time.perf_counter_ns()
            shell.selectPage(page_id)
            route_end = time.perf_counter_ns()
            drain()
            grab = grab_visible()
            assert window._active_tab_id() == page_id
            return {"routing_ms": (route_end - start) / 1e6,
                    "through_grab_ms": (time.perf_counter_ns() - start) / 1e6, "grab": grab}

        phase[0] = "first_switch"
        for page_id, title, page in window.workspace_pages:
            first[page_id] = switch(page_id)
        phase[0] = "warm_switch"
        for _ in range(args.warm_rounds):
            for page_id, title, page in window.workspace_pages:
                warm[page_id].append(switch(page_id))

        phase[0] = "input"
        shell.selectPage("dual_gate")
        drain()
        def find_item(item, name):
            if item.objectName() == name:
                return item
            for child in item.childItems():
                found = find_item(child, name)
                if found is not None:
                    return found
            return None
        candidate = None
        for form in window._presets._quick_parameter_forms["forms"]:
            if not form.isVisibleTo(window):
                continue
            for field, source in form.adapter._fields.items():
                if (field.fieldId != args.input_field or field.kind != "text"
                        or not isinstance(source, QLineEdit) or source.isReadOnly()):
                    continue
                item = find_item(form.rootObject(), "parameter_" + field.fieldId)
                if item is not None and item.isVisible() and item.isEnabled():
                    candidate = (form, field, source, item)
                    break
            if candidate:
                break
        if not candidate:
            raise RuntimeError("No visible real QML text editor for keyboard probe: " + args.input_field)
        form, field, source, item = candidate
        original_text = source.text()
        text = "perf-point-0123456789"
        source_seen = []
        source.textChanged.connect(lambda value: source_seen.append(time.perf_counter_ns()) if value == text else None)
        item.forceActiveFocus(Qt.FocusReason.OtherFocusReason)
        QTest.keyClick(form, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        input_start = time.perf_counter_ns()
        QTest.keyClicks(form, text, delay=0)
        dispatch_end = time.perf_counter_ns()
        deadline = dispatch_end + 2_000_000_000
        while source.text() != text and time.perf_counter_ns() < deadline:
            app.processEvents()
        if source.text() != text or not source_seen:
            raise RuntimeError("Full QML input did not reach retained source: " + repr(source.text()))
        input_report = {"field": field.fieldId, "characters": len(text),
                        "dispatch_ms": (dispatch_end - input_start) / 1e6,
                        "complete_string_to_source_ms": (source_seen[-1] - input_start) / 1e6,
                        "render_grab": grab_visible()}
        source.setText(original_text)
        drain()

        idle = []
        # Empty/disconnected Spectrum and denser form workspace, after all pages
        # have been visited: detects hidden-page timer/engine overhead.
        for page_id in ("spectrum", "mega_sweep"):
            phase[0] = "idle_" + page_id
            shell.selectPage(page_id)
            drain()
            grab_visible()
            last_beat[0] = time.perf_counter_ns()
            heartbeat[phase[0]].clear()
            cpu_start, wall_start = time.process_time(), time.perf_counter()
            loop = QEventLoop()
            QTimer.singleShot(round(args.idle_seconds * 1000), loop.quit)
            loop.exec()
            wall = time.perf_counter() - wall_start
            cpu = time.process_time() - cpu_start
            idle.append({"page": page_id, "wall_seconds": wall, "process_cpu_seconds": cpu,
                         "single_core_cpu_percent": 100 * cpu / wall, **memory_bytes()})
        stream_report = None
        if args.stream_test:
            import numpy as np
            phase[0] = "stream"
            shell.selectPage("spectrum")
            drain()
            plot = window._spectrum._spec_plot
            wavelength = np.linspace(450., 850., 512)
            frames = [100. + 500. * np.exp(-((wavelength - 620. - i * .2) / 24.) ** 2)
                      for i in range(60)]
            callback_times, arrival_times = [], []
            loop = QEventLoop()
            publisher = QTimer()
            publisher.setTimerType(Qt.TimerType.PreciseTimer)
            publisher.setInterval(33)
            def publish_frame():
                index = len(callback_times)
                start = time.perf_counter_ns()
                arrival_times.append(start)
                plot.update_spectrum(wavelength, frames[index])
                callback_times.append((time.perf_counter_ns() - start) / 1e6)
                if len(callback_times) == len(frames):
                    publisher.stop()
                    loop.quit()
            publisher.timeout.connect(publish_frame)
            timeout = QTimer()
            timeout.setSingleShot(True)
            timeout.timeout.connect(loop.quit)
            timeout.start(6000)
            last_beat[0] = time.perf_counter_ns()
            heartbeat[phase[0]].clear()
            stream_start = time.perf_counter_ns()
            publisher.start()
            loop.exec()
            publisher.stop()
            timeout.stop()
            stream_elapsed = (time.perf_counter_ns() - stream_start) / 1e9
            if len(callback_times) != len(frames):
                raise RuntimeError(f"Synthetic plot stream completed only {len(callback_times)}/60 frames")
            exact = (np.array_equal(plot._wl, wavelength) and np.array_equal(plot._cts, frames[-1])
                     and np.array_equal(plot._curve.yData, frames[-1]))
            if not exact:
                raise RuntimeError("Synthetic spectrum did not retain exact final arrays")
            stream_report = {"frames": len(callback_times), "points_per_frame": len(wavelength),
                             "target_interval_ms": 33, "wall_seconds": stream_elapsed,
                             "frames_per_second": len(callback_times) / stream_elapsed,
                             "callback": {**distribution(callback_times), "samples_ms": callback_times},
                             "arrival_intervals": distribution([(b - a) / 1e6 for a, b in zip(arrival_times, arrival_times[1:])]),
                             "exact_final_arrays": exact, "final_grab": grab_visible(),
                             "note": "Precomputed synthetic data calls real update_spectrum; no acquisition dispatch, hardware, driver or storage throughput is measured."}
        timer.stop()
        if profiler:
            profiler.disable()
            profile_path = Path(args.profile_worker)
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            profiler.dump_stats(str(profile_path.with_suffix(".pstats")))
            with profile_path.open("w", encoding="utf-8") as stream:
                pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(100)
                pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("tottime").print_stats(60)
                stream.write("\nTargeted callback/validation totals (nested cumulative times overlap):\n")
                for (filename, line, name), (primitive, total, self_time, cumulative, callers) in sorted(
                        pstats.Stats(profiler).stats.items(), key=lambda row: row[1][3], reverse=True):
                    if name == "parameters_valid" or (Path(filename).name.startswith("quick_") and name in {
                            "refresh", "_source_changed", "_resize_height", "_fit_height", "setSource"}):
                        stream.write(f"{total:8d} calls {self_time:9.4f}s self {cumulative:9.4f}s cumulative {Path(filename).name}:{line}({name})\n")
        alive = [w for w in surfaces if isValid(w)]
        engine_ids = {getCppPointer(w.engine())[0] for w in alive}
        result = {"schema": 1, "platform": app.platformName(), "pyside6": PySide6.__version__,
                  "quick_backend": os.environ.get("QT_QUICK_BACKEND"),
                  "requested_backend": args.backend,
                  "graphics_apis": sorted({w.quickWindow().rendererInterface().graphicsApi().name
                                           for w in alive if w.isVisibleTo(window)}),
                  "scale_factor": os.environ.get("QT_SCALE_FACTOR", "1"), "profiled": bool(profiler),
                  "source_fingerprint": fingerprint,
                  "startup": startup, "first_switch": first, "warm_switch": dict(warm),
                  "input": input_report, "idle": idle,
                  "stream": stream_report,
                  "heartbeat": {key: {**distribution(samples), "samples_ms": samples} for key, samples in heartbeat.items()},
                  "surfaces": {"constructed": len(surfaces), "alive": len(alive), "engines": len(engine_ids),
                               "visible": sum(w.isVisibleTo(window) for w in alive)},
                  "qml_loads": [dict(row) for row in qml_loads if row["source"]], "callbacks": dict(calls),
                  "warnings": messages, "callback_errors": errors, "hardware_import_attempts": guard.attempts,
                  "config_save_attempts_blocked": len(saves), "isolated_root": str(scratch),
                  "measurement_notes": [
                      "Routing ends at selectPage return; through_grab includes two event dispatch passes and synchronous grabs of every visible QML surface plus workspace.",
                      "Not a compositor presentation timestamp; hidden native software rendering matches the workspace screenshot audit.",
                      "First initial Dual Gate already exists at startup; first_switch reports first explicit navigation, other pages include lazy migration.",
                      "10ms precise heartbeat reports interarrival, including scheduler and UI work; idle phases reset first tick origin.",
                      "Single-core equivalent CPU=process CPU/wall*100; idle waits are sampling windows, never response timings.",
                      "Python method wrappers count only listed callback boundaries, nested times overlap. Optional cProfile uses a separate process excluded from medians.",
                      "Cold timing means a fresh Python process; OS filesystem/QML caches are not flushed."]}
        phase[0] = "shutdown"
        assert window.shutdown()
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        result["closed"] = not isValid(window)
        result["qml_source_clear_count"] = sum(not row["source"] for row in qml_loads)
        for p in reversed(patches):
            p.stop()
        qInstallMessageHandler(prior_message)
        sys.excepthook = prior_hook
        return result


def summarize(runs):
    med = lambda values: round(statistics.median(values), 3)
    return {"repeats": len(runs),
            "startup_ms": {key: med([r["startup"][key] for r in runs]) for key in (
                "process_entry_to_first_grab_ms", "construction_ms", "qml_synchronous_load_ms")},
            "warm_switch_ms": {page: med([sample["through_grab_ms"] for r in runs for sample in r["warm_switch"][page]]) for page in runs[0]["warm_switch"]},
            "first_switch_ms": {page: med([r["first_switch"][page]["through_grab_ms"] for r in runs]) for page in runs[0]["first_switch"]},
            "input_complete_string_to_source_ms": med([r["input"]["complete_string_to_source_ms"] for r in runs]),
            "idle": {page: {"single_core_cpu_percent": med([next(i for i in r["idle"] if i["page"] == page)["single_core_cpu_percent"] for r in runs]),
                            "rss_bytes": med([next(i for i in r["idle"] if i["page"] == page)["rss_bytes"] for r in runs if next(i for i in r["idle"] if i["page"] == page)["rss_bytes"] is not None]) if os.name == "nt" else None,
                            "heartbeat": distribution([s for r in runs for s in r["heartbeat"]["idle_" + page]["samples_ms"]])} for page in ("spectrum", "mega_sweep")},
            "source_consistent": len({r.get("source_fingerprint", {}).get("sha256") for r in runs}) == 1,
            "clean": all(not r["warnings"] and not r["callback_errors"] and not r["hardware_import_attempts"] and r["closed"] for r in runs)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--backend", choices=("software", "default"), default="software")
    parser.add_argument("--input-field", default="laser_edit", help="same source field used by the original baseline")
    parser.add_argument("--stream-test", action="store_true", help="additional 60-frame synthetic live-spectrum phase after comparable timings")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--warm-rounds", type=int, default=3)
    parser.add_argument("--idle-seconds", type=float, default=5.)
    parser.add_argument("--profile", action="store_true", help="additional fresh cProfile process; excluded from headline timings")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--launch-ns", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--profile-worker", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.repeat < 1 or args.warm_rounds < 1 or args.idle_seconds < .1:
        parser.error("repeat/warm-rounds must be positive and idle-seconds >= .1")
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.worker:
        report = run_worker(args)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 0 if report["closed"] and not report["warnings"] and not report["callback_errors"] and not report["hardware_import_attempts"] else 1
    runs, diagnostics = [], []
    for index in range(args.repeat + int(args.profile)):
        diagnostic = index == args.repeat
        target = args.output.with_name(args.output.stem + ("-profile.json" if diagnostic else f"-run{index + 1}.json"))
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--output", str(target),
                   "--warm-rounds", str(args.warm_rounds), "--idle-seconds", str(args.idle_seconds),
                   "--backend", args.backend, "--input-field", args.input_field]
        if args.native:
            command.append("--native")
        if args.stream_test:
            command.append("--stream-test")
        if diagnostic:
            command += ["--profile-worker", str(args.output.with_suffix(".profile.txt"))]
        command += ["--launch-ns", str(time.perf_counter_ns())]
        completed = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=300)
        log_path = target.with_suffix(".log")
        log_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
        if completed.returncode or not target.exists():
            raise RuntimeError(f"Performance worker failed ({completed.returncode}), see {log_path}")
        report = json.loads(target.read_text(encoding="utf-8"))
        (diagnostics if diagnostic else runs).append(report)
    result = {"summary": summarize(runs), "runs": runs, "profile_runs": diagnostics}
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], indent=2))
    return 0 if result["summary"]["clean"] and result["summary"]["source_consistent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
