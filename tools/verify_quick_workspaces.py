"""Render every real workspace with isolated configuration and no hardware.

This audit builds the page classes, not MainWindow (which owns real devices).
No acquisition, connection, calibration, or settings action is dispatched.
Use --native --backend default for Windows' normal graphics backend. Omitting
--backend retains the legacy inherited-or-software selection. Use QT_SCALE_FACTOR to audit
100/150/200 percent in separate processes. Reports retain layout findings rather
than treating an oversized minimum width as proof of a successful layout.
"""
from __future__ import annotations

import argparse
import importlib.abc
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


class HardwareImportGuard(importlib.abc.MetaPathFinder):
    denied = ("controllers", "adapters", "drivers", "CRYO2100", "clr", "pythonnet",
              "pyvisa", "serial", "nidaqmx", "pylablib", "app.ntfy_notifications",
              "app.process_watchdog")

    def __init__(self):
        self.attempts = []

    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in self.denied):
            self.attempts.append(fullname)
            raise RuntimeError("Hardware/service import forbidden in workspace audit: " + fullname)


def _rect(rect):
    return [rect.x(), rect.y(), rect.width(), rect.height()]


GRAPHICS_BACKENDS = ("default", "software", "d3d11", "opengl")


def configure_graphics_backend(backend=None, environment=None):
    """Select before importing Qt; never replace an unsuccessful GPU request.

    None preserves the existing audit behavior, including an inherited backend.
    Explicit default removes the two selectors so a test runner's software
    environment cannot accidentally turn a native RHI audit into a raster run.
    """
    if backend is not None and backend not in GRAPHICS_BACKENDS:
        raise ValueError(f"Unsupported graphics backend: {backend}")
    environment = os.environ if environment is None else environment
    if backend is None:
        environment.setdefault("QT_QUICK_BACKEND", "software")
    else:
        environment.pop("QT_QUICK_BACKEND", None)
        environment.pop("QSG_RHI_BACKEND", None)
        if backend == "software":
            environment["QT_QUICK_BACKEND"] = "software"
        elif backend != "default":
            environment["QSG_RHI_BACKEND"] = backend
    return {key: environment.get(key) for key in (
        "QT_QUICK_BACKEND", "QSG_RHI_BACKEND", "QSG_RHI_PREFER_SOFTWARE_RENDERER", "QT_OPENGL")}


def graphics_api(widget):
    """Read the renderer that the mounted view actually initialized."""
    return widget.quickWindow().rendererInterface().graphicsApi().name


def audit(output, *, native=False, sizes=((1400, 900), (1024, 768)), backend=None):
    os.environ["QT_QPA_PLATFORM"] = "windows" if native and os.name == "nt" else "offscreen"
    backend_environment = configure_graphics_backend(backend)
    from app.ui_preview import prepare_preview_environment

    with tempfile.TemporaryDirectory(prefix="spectralsweep-workspaces-") as directory:
        scratch = Path(directory)
        environment = prepare_preview_environment(scratch)
        for key in ("SPECTRALSWEEP_PREVIEW_ROOT", "SPECTRALSWEEP_CONFIG_PATH",
                    "SPECTRALSWEEP_HISTORY_PATH", "SPECTRAL_MOCK_LF6"):
            os.environ[key] = environment[key]
        guard = HardwareImportGuard()
        sys.meta_path.insert(0, guard)

        import PySide6
        from PySide6.QtCore import (QCoreApplication, QEvent, QPoint, QPointF, QRect,
                                    QSettings, Qt, qInstallMessageHandler)
        from PySide6.QtGui import QFont, QFontDatabase
        from PySide6.QtQuickWidgets import QQuickWidget
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QScrollArea, QTabBar, QToolButton
        from ui.application_style import application_style
        from ui.quick_workspaces_preview import build_preview_window
        from utils.config import cfg

        app = QApplication([])
        app.setOrganizationName("SpectralSweepWorkspaceAudit")
        app.setApplicationName("IsolatedQuickWorkspaces")
        app.setProperty("spectralQuick", True)
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        for scope in (QSettings.Scope.UserScope, QSettings.Scope.SystemScope):
            QSettings.setPath(QSettings.Format.IniFormat, scope, str(scratch / "settings"))
        if not QFontDatabase.families():
            font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
            if font_path.is_file():
                QFontDatabase.addApplicationFont(str(font_path))
        app.setFont(QFont("Segoe UI", 9))
        app.setStyle("Fusion")
        application_style(app).reload()
        config_saves = []
        cfg.save = lambda *args, **kwargs: config_saves.append("blocked in isolated audit")
        messages = []
        previous_handler = qInstallMessageHandler(
            lambda kind, context, message: messages.append({"kind": kind.name, "message": message}))
        callback_errors = []
        previous_hook = sys.excepthook

        def record_callback_error(kind, value, tb):
            callback_errors.append("".join(traceback.format_exception(kind, value, tb)))
            previous_hook(kind, value, tb)

        sys.excepthook = record_callback_error

        window = build_preview_window(scratch, app, hidden=True)
        shell = window.quick_workbench
        pages = window.workspace_pages
        splitter = window._sidebar_splitter
        warnings = []
        observed = set()

        def observe_quick():
            for widget in window.findChildren(QQuickWidget):
                if id(widget) in observed:
                    continue
                observed.add(id(widget))
                widget.engine().warnings.connect(lambda errors: warnings.extend(e.toString() for e in errors))
                if widget.status() == QQuickWidget.Status.Error:
                    warnings.extend(e.toString() for e in widget.errors())

        def metrics(page):
            coverage = getattr(page, "_quick_parameter_forms", {})
            if not isinstance(coverage, dict):
                coverage = {}
            quick = ([page] if isinstance(page, QQuickWidget) else []) + page.findChildren(QQuickWidget)
            if not coverage:
                scalar_forms = [surface for surface in quick if type(surface).__name__ == "QuickParameterForm"]
                coverage = {"forms": scalar_forms, "migrated_fields": sum(
                    field.kind not in {"label", "heading", "divider"}
                    for form in scalar_forms for field in form.adapter._fields)}
            outside = []
            control_outside = []
            surfaces = []
            paged_tabs = []
            for bar in page.findChildren(QTabBar):
                if not bar.isVisibleTo(window):
                    continue
                arrows = [button.objectName() for button in bar.findChildren(QToolButton)
                          if button.isVisibleTo(bar) and button.objectName() in
                          {"ScrollLeftButton", "ScrollRightButton"}]
                outside_tabs = [bar.tabText(index) for index in range(bar.count())
                                if bar.isTabVisible(index) and
                                (bar.tabRect(index).left() < -1 or
                                 bar.tabRect(index).right() > bar.width())]
                if arrows or outside_tabs:
                    paged_tabs.append({"width": bar.width(), "paging_buttons": arrows,
                                       "outside_tabs": outside_tabs})
            # Scroll contents can legitimately extend beyond a viewport; inspect
            # each mounted Quick surface against its immediate layout owner.
            for widget in quick:
                if not widget.isVisibleTo(window) or widget.width() <= 0 or widget.height() <= 0:
                    continue
                parent = widget.parentWidget()
                rect = widget.geometry()
                if parent and (rect.left() < -1 or rect.top() < -1
                        or rect.right() > parent.width() + 1 or rect.bottom() > parent.height() + 1):
                    outside.append({"name": widget.objectName(), "rect": _rect(rect),
                                    "owner_size": [parent.width(), parent.height()],
                                    "owner_name": parent.objectName(),
                                    "owner_class": type(parent).__name__,
                                    "owner_minimum_height": parent.minimumHeight(),
                                    "owner_maximum_height": parent.maximumHeight()})
                root = widget.rootObject()
                if root is None:
                    continue
                render_window = widget.quickWindow()
                render_size = ([render_window.width(), render_window.height()]
                               if render_window is not None else None)
                render_content = render_window.contentItem() if render_window is not None else None
                render_content_size = ([render_content.width(), render_content.height()]
                                       if render_content is not None else None)
                if render_size is not None and render_size != [widget.width(), widget.height()]:
                    outside.append({"name": widget.objectName(), "source": Path(widget.source().toLocalFile()).name,
                                    "kind": "render_window_size_mismatch",
                                    "widget_size": [widget.width(), widget.height()],
                                    "render_window_size": render_size})
                if render_content_size is not None and render_content_size != [widget.width(), widget.height()]:
                    outside.append({"name": widget.objectName(), "source": Path(widget.source().toLocalFile()).name,
                                    "kind": "render_content_size_mismatch",
                                    "widget_size": [widget.width(), widget.height()],
                                    "render_content_size": render_content_size})
                surfaces.append({"name": widget.objectName(),
                    "source": Path(widget.source().toLocalFile()).name,
                    "rect": _rect(rect), "root_size": [root.width(), root.height()],
                    "render_window_size": render_size,
                    "render_content_size": render_content_size,
                    "graphics_api": graphics_api(widget),
                    "root_implicit_height": round(root.implicitHeight(), 1),
                    "maximum_height": widget.maximumHeight()})

                def inspect_item(item, clipped=False):
                    if not item.isVisible() or len(control_outside) >= 200:
                        return
                    name = item.objectName()
                    meta = item.metaObject()
                    interactive = False
                    while meta is not None:
                        if meta.className() in {"QQuickAbstractButton", "QQuickComboBox", "QQuickTextField", "QQuickSpinBox"}:
                            interactive = True
                            break
                        meta = meta.superClass()
                    if (interactive or name.startswith(("parameter_", "command_", "field_"))) and not clipped:
                        origin = item.mapToItem(root, QPointF(0, 0))
                        if (origin.x() < -1 or origin.y() < -1
                                or origin.x() + item.width() > root.width() + 1
                                or origin.y() + item.height() > root.height() + 1):
                            control_outside.append({"surface": widget.objectName(), "control": name or item.metaObject().className(),
                                "rect": [round(origin.x(), 1), round(origin.y(), 1),
                                         round(item.width(), 1), round(item.height(), 1)],
                                "surface_size": [root.width(), root.height()]})
                    for child in item.childItems():
                        inspect_item(child, clipped or item.clip())

                inspect_item(root)
            return {"page_size": [page.width(), page.height()],
                    "minimum_size_hint": [page.minimumSizeHint().width(), page.minimumSizeHint().height()],
                    "minimum_size": [page.minimumWidth(), page.minimumHeight()],
                    "quick_widgets": len(quick),
                    "quick_visible": sum(widget.isVisibleTo(page) for widget in quick),
                    "graphics_apis": sorted({surface["graphics_api"] for surface in surfaces}),
                    "parameter_forms": len(coverage.get("forms", [])),
                    "parameter_fields": coverage.get("migrated_fields", 0),
                    "table_editors": len(getattr(page, "_quick_table_editors", [])),
                    "widget_boundaries": coverage.get("boundaries", []),
                    "surface_bounds_findings": outside,
                    "qml_control_bounds_findings": control_outside,
                    "tab_paging_findings": paged_tabs,
                    "mounted_surfaces": surfaces}

        def scroll_metrics(page):
            entries = []
            for scroll in [shell.body_scroll] + page.findChildren(QScrollArea):
                if not scroll.isVisibleTo(window):
                    continue
                viewport = scroll.viewport()
                content = scroll.widget()
                hint = content.sizeHint() if content else None
                entries.append({"name": scroll.objectName() or type(scroll).__name__,
                    "viewport_size": [viewport.width(), viewport.height()],
                    "horizontal_maximum": scroll.horizontalScrollBar().maximum(),
                    "vertical_maximum": scroll.verticalScrollBar().maximum(),
                    "horizontal_bar_visible": scroll.horizontalScrollBar().isVisibleTo(window),
                    "vertical_bar_visible": scroll.verticalScrollBar().isVisibleTo(window),
                    "content_size": [content.width(), content.height()] if content else None,
                    "content_size_hint": [hint.width(), hint.height()] if hint else None,
                    "potential_vertical_slack": max(0, viewport.height() - hint.height())
                        if hint and hint.height() >= 0 else None})
            return entries

        def chrome_metrics():
            viewport = shell.body_scroll.viewport()
            nav_origin = shell.navigation.mapTo(window, shell.navigation.rect().topLeft())
            command_origin = shell.command_stack.mapTo(window, shell.command_stack.rect().topLeft())
            return {"navigation_rect": [nav_origin.x(), nav_origin.y(), shell.navigation.width(), shell.navigation.height()],
                    "command_stack_rect": [command_origin.x(), command_origin.y(), shell.command_stack.width(), shell.command_stack.height()],
                    "navigation": metrics(shell.navigation),
                    "command_stack": metrics(shell.command_stack),
                    "body_viewport_size": [viewport.width(), viewport.height()],
                    "body_scroll_range": [shell.body_scroll.horizontalScrollBar().maximum(),
                                           shell.body_scroll.verticalScrollBar().maximum()],
                    "sidebar_visible": window._inst_panel.isVisibleTo(window)}

        def sequence_metrics(page):
            preview = getattr(page, "_tree", None)
            if preview is None:
                return None
            origin = preview.mapTo(window, QPoint(0, 0))
            visible = QRect(origin, preview.size())
            ancestor = preview.parentWidget()
            while ancestor is not None:
                corner = ancestor.mapTo(window, QPoint(0, 0))
                visible = visible.intersected(QRect(corner, ancestor.size()))
                if ancestor is window:
                    break
                ancestor = ancestor.parentWidget()
            return {"allocated_size": [preview.width(), preview.height()],
                    "visible_rect_in_window": _rect(visible),
                    "visible_height": visible.height() if preview.isVisibleTo(window) else 0,
                    "safety_expanded": page._quick_safety_disclosure.toggle.isChecked()
                        if hasattr(page, "_quick_safety_disclosure") else None,
                    "execution_expanded": page._quick_execution_disclosure.toggle.isChecked()
                        if hasattr(page, "_quick_execution_disclosure") else None}

        def settle_layout():
            """Wait for deferred QML/Widgets height negotiation, not one frame."""
            started = time.monotonic()
            last = None
            unchanged = 0
            while (time.monotonic() - started) < 2.5:
                QTest.qWait(40)
                snapshot = []
                for surface in window.findChildren(QQuickWidget):
                    if not surface.isVisibleTo(window):
                        continue
                    owner = surface.parentWidget()
                    root = surface.rootObject()
                    snapshot.append((id(surface), surface.width(), surface.height(),
                        round(root.implicitHeight(), 2) if root else None,
                        owner.width() if owner else 0, owner.height() if owner else 0))
                current = tuple(snapshot)
                unchanged = unchanged + 1 if current == last else 0
                last = current
                if unchanged >= 4 and time.monotonic() - started >= .24:
                    return {"stable": True, "wait_ms": round((time.monotonic() - started) * 1000)}
            return {"stable": False, "wait_ms": round((time.monotonic() - started) * 1000)}

        captures = []
        try:
            window.resize(*sizes[0])
            window.show()
            QTest.qWait(100)
            for width, height in sizes:
                window.resize(width, height)
                # Exercise the application's responsive sidebar policy. A
                # hard-coded 250px override hid its real desktop proportions.
                for mode in ("light", "dark"):
                    shell.setTheme(mode)
                    for key, title, page in pages:
                        shell.selectPage(key)
                        assert window._tabs.currentWidget() is page, key
                        observe_quick()
                        settling = settle_layout()
                        observe_quick()
                        name = f"{key}-{width}x{height}-{mode}.png"
                        assert window.grab().save(str(output / name)), name
                        entry = {"page": key, "title": title, "theme": mode,
                                 "requested_window_size": [width, height],
                                 "actual_window_size": [window.width(), window.height()],
                                 "screenshot": name, "settling": settling,
                                 "sequence_preview": sequence_metrics(page),
                                 "workspace_scrolling": scroll_metrics(page),
                                 "chrome": chrome_metrics(), **metrics(page)}
                        captures.append(entry)
                    # Exercise the real Instruments navigation path as well.
                    shell.toggleSidebar()
                    if width >= 1200 and not window._inst_panel.isVisibleTo(window):
                        shell.toggleSidebar()
                    settling = settle_layout()
                    observe_quick()
                    name = f"instruments-{width}x{height}-{mode}.png"
                    assert window.grab().save(str(output / name)), name
                    sidebar_widget = splitter.widget(0)
                    captures.append({"page": "instruments", "title": "Instruments", "theme": mode,
                        "requested_window_size": [width, height],
                        "actual_window_size": [window.width(), window.height()],
                        "screenshot": name, "settling": settling,
                        "workspace_scrolling": scroll_metrics(sidebar_widget),
                        "chrome": chrome_metrics(), **metrics(sidebar_widget)})
                    shell.selectPage("dual_gate")
            loaded_hardware = [name for name in sys.modules
                               if any(name == prefix or name.startswith(prefix + ".")
                                      for prefix in guard.denied)]
            assert not loaded_hardware, loaded_hardware
            assert window._lf6._frame == 0 and window._lf6._pending is None
            instrument_metrics = metrics(window._inst_panel)
        finally:
            assert window.shutdown()
            for _, _, page in pages:
                assert page.close(), page.objectName()
            assert window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            qInstallMessageHandler(previous_handler)
            sys.excepthook = previous_hook
        report = {"pyside6": PySide6.__version__, "platform": app.platformName(),
                  "requested_backend": backend if backend is not None else "legacy",
                  "backend_environment": backend_environment,
                  "graphics_apis": sorted({api for entry in captures
                      for block in (entry, entry["chrome"]["navigation"], entry["chrome"]["command_stack"])
                      for api in block["graphics_apis"]}),
                  "scale": os.environ.get("QT_SCALE_FACTOR", "system"),
                  "screen": {"name": app.primaryScreen().name(),
                             "geometry": _rect(app.primaryScreen().geometry()),
                             "available_geometry": _rect(app.primaryScreen().availableGeometry()),
                             "device_pixel_ratio": app.primaryScreen().devicePixelRatio()},
                  "pages": len(pages), "captures": captures,
                  "qml_warnings": list(dict.fromkeys(warnings + [entry["message"] for entry in messages
                      if "ui/qml/" in entry["message"] or "qml:" in entry["message"]])),
                  "qt_messages": messages,
                  "python_callback_errors": callback_errors,
                  "hardware_import_attempts": guard.attempts,
                  "hardware_controllers_loaded": loaded_hardware,
                  "acquisition_dispatched": False, "all_pages_switched_and_closed": True,
                  "configuration_saves_blocked": len(config_saves),
                  "isolated_config": str(scratch / "config.json"),
                  "isolated_history": str(scratch / "history.sqlite3"),
                  "isolated_qsettings": str(scratch / "settings"),
                  "instrument_sidebar": instrument_metrics,
                  "requested_sizes_honored": all(entry["requested_window_size"] == entry["actual_window_size"] for entry in captures),
                  "all_layouts_stable": all(entry["settling"]["stable"] for entry in captures)}
        report["all_visible_bounds_clear"] = all(
            not block["surface_bounds_findings"] and not block["qml_control_bounds_findings"]
            for entry in captures for block in (entry, entry["chrome"]["navigation"],
                                                entry["chrome"]["command_stack"]))
        report["all_horizontal_ranges_zero"] = all(
            scroll["horizontal_maximum"] == 0
            for entry in captures for scroll in entry["workspace_scrolling"])
        report["all_tab_headers_unpaged"] = all(not entry["tab_paging_findings"] for entry in captures)
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "tmp" / "quick-workspaces")
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--backend", choices=GRAPHICS_BACKENDS,
                        help="Graphics selection: default clears backend overrides; omit to retain legacy inherited-or-software behavior")
    parser.add_argument("--size", action="append", metavar="WIDTHxHEIGHT",
                        help="Audit this logical client size; repeat for multiple sizes")
    args = parser.parse_args()
    sizes = []
    for raw in args.size or ["1400x900", "1024x768"]:
        try:
            width, height = map(int, raw.lower().split("x"))
            if min(width, height) <= 0:
                raise ValueError
        except ValueError:
            parser.error("--size must be positive WIDTHxHEIGHT")
        sizes.append((width, height))
    args.output.mkdir(parents=True, exist_ok=True)
    report = audit(args.output.resolve(), native=args.native, sizes=sizes, backend=args.backend)
    print(json.dumps({"report": str(args.output.resolve() / "report.json"),
                      "platform": report["platform"], "scale": report["scale"],
                      "requested_backend": report["requested_backend"],
                      "backend_environment": report["backend_environment"],
                      "graphics_apis": report["graphics_apis"],
                      "pages": report["pages"], "screenshots": len(report["captures"]),
                      "qml_warnings": report["qml_warnings"],
                      "qt_message_count": len(report["qt_messages"]),
                      "python_callback_errors": report["python_callback_errors"],
                      "hardware_import_attempts": report["hardware_import_attempts"],
                      "requested_sizes_honored": report["requested_sizes_honored"],
                      "all_layouts_stable": report["all_layouts_stable"],
                      "all_visible_bounds_clear": report["all_visible_bounds_clear"],
                      "all_horizontal_ranges_zero": report["all_horizontal_ranges_zero"],
                      "all_tab_headers_unpaged": report["all_tab_headers_unpaged"],
                      "all_pages_switched_and_closed": report["all_pages_switched_and_closed"]}, indent=2))
    return 1 if (report["qml_warnings"] or report["hardware_import_attempts"]
                 or report["python_callback_errors"]
                 or not report["requested_sizes_honored"] or not report["all_layouts_stable"]
                 or not report["all_visible_bounds_clear"] or not report["all_horizontal_ranges_zero"]
                 or not report["all_tab_headers_unpaged"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
