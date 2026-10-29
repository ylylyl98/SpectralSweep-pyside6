"""Audit the real instrument sidebar without importing hardware or scanning devices.

Native Windows/default RHI is the default. All controllers below are inert UI
fixtures; unexpected commands are recorded as failures. Configuration and Qt
settings live in a disposable directory. No test modules or MainWindow are used.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from tools.verify_quick_workspaces import HardwareImportGuard, configure_graphics_backend, graphics_api


def audit(output, *, native=True, backend="default", baseline=False, widths=(460, 350), themes=("light", "dark")):
    os.environ["QT_QPA_PLATFORM"] = "windows" if native and os.name == "nt" else "offscreen"
    graphics_environment = configure_graphics_backend(backend)
    from app.ui_preview import prepare_preview_environment

    with tempfile.TemporaryDirectory(prefix="spectralsweep-instrument-audit-") as folder:
        scratch = Path(folder)
        isolated = prepare_preview_environment(scratch)
        for key in ("SPECTRALSWEEP_PREVIEW_ROOT", "SPECTRALSWEEP_CONFIG_PATH", "SPECTRALSWEEP_HISTORY_PATH", "SPECTRAL_MOCK_LF6"):
            os.environ[key] = isolated[key]
        guard = HardwareImportGuard()
        sys.meta_path.insert(0, guard)

        from PySide6.QtCore import QObject, QPointF, QSettings, Qt, Signal, qInstallMessageHandler
        from PySide6.QtGui import QFont
        from PySide6.QtQuickWidgets import QQuickWidget
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit, QScrollArea
        from ui import instrument_panel as ip
        from ui.quick_style import apply_quick_style
        from ui.quick_parameter_forms import install_parameter_forms
        from ui.quick_table_editor import install_table_editors
        from utils.config import cfg

        commands = []

        class Inert(QObject):
            error = Signal(str)

            def __init__(self):
                super().__init__()
                self.is_connected = False
                self.is_busy = False

            def reject(self, *args, **kwargs):
                commands.append({"controller": type(self).__name__, "args": repr(args), "kwargs": repr(kwargs)})
                raise RuntimeError("Hardware command forbidden in instrument audit")

            connect_instrument = disconnect_instrument = scan_devices = reject

        class Detector(Inert):
            connected = Signal(list)
            disconnected = Signal()
            backend = "lightfield"
            identity = {}
            connected_backends = ()

        class SMU(Inert):
            connected = Signal(list)
            disconnected = Signal()
            readings_ready = Signal(object)
            manual_finished = Signal(str, str, float)
            manual_error = Signal(str)
            limits_result = Signal(str, str, object)
            limits_error = Signal(str, str, str)
            limits_state_changed = Signal()

            def role_is_available(self, role):
                return False

            manual_control = apply_smu_limits = read_smu_limits = Inert.reject

            def mark_smu_limits_dirty(self, address):
                # Local draft bookkeeping is not a device command.
                self.limits_state_changed.emit()

        class Rotation(Inert):
            connected = Signal(str, str)
            disconnected = Signal(str)
            axes_scanned = Signal(str, list)
            motion_profile_ready = Signal(str, object)
            settings_saved = Signal(str)

            def __init__(self):
                super().__init__()
                del self.is_connected

            def logical_slots(self):
                return ("rot1", "rot2")

            def is_connected(self, slot):
                return False

            scan_axes = inspect_motion_profile = apply_motion_profile = save_esp300_settings = Inert.reject

        class Stage(Inert):
            connected = Signal(str)
            disconnected = Signal()
            axes_scanned = Signal(list)
            scan_axes = Inert.reject

        class Imaging(Inert):
            connected = Signal(str)
            disconnected = Signal()
            state_changed = Signal(object)
            stop = save_current_as_maximum = set_frequency = set_maximum = set_direction = set_scale = send = goto = jog = Inert.reject

        class Meter(Inert):
            connected = Signal()
            disconnected = Signal()
            devices_scanned = Signal(list)

        class Flipper(Inert):
            state_changed = Signal(object)
            devices_scanned = Signal(list)
            read_position = move_to = Inert.reject
            state = {"connected": False, "busy": False, "position": None,
                     "serial_number": "", "operation": "", "error": ""}

        app = QApplication.instance() or QApplication([])
        app.setOrganizationName("SpectralSweepInstrumentAudit")
        app.setApplicationName("IsolatedInstrumentSidebar")
        app.setProperty("spectralQuick", True)
        app.setStyle("Fusion")
        app.setFont(QFont("Segoe UI", 9))
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        for scope in (QSettings.Scope.UserScope, QSettings.Scope.SystemScope):
            QSettings.setPath(QSettings.Format.IniFormat, scope, str(scratch / "settings"))
        config_saves = []
        cfg.save = lambda *args, **kwargs: config_saves.append("isolated save blocked")
        # Constructors may enumerate available addresses; replace the boundary
        # before constructing any section, never by importing serial/pyvisa.
        ip._list_com_ports = lambda: ["COM99 (audit fixture)"]
        ip._com_ports_as_asrl = lambda: ["ASRL99::INSTR"]
        ip._list_visa_resources = lambda *args, **kwargs: ["GPIB0::99::INSTR"]
        messages, callbacks, warnings = [], [], []
        previous_handler = qInstallMessageHandler(lambda kind, context, message: messages.append({"kind": kind.name, "message": message}))
        previous_hook = sys.excepthook
        sys.excepthook = lambda kind, value, tb: callbacks.append("".join(traceback.format_exception(kind, value, tb)))
        output = Path(output).resolve()
        output.mkdir(parents=True, exist_ok=True)
        records = []
        controllers = {"lf6_ctrl": Detector(), "smu_ctrl": SMU(), "rotation_ctrl": Rotation(),
                       "stage_ctrl": Stage(), "imaging_stage_ctrl": Imaging(), "pm_ctrl": Meter(), "mff101_ctrl": Flipper()}

        def settle():
            for _ in range(4):
                app.processEvents()
                QTest.qWait(30)

        def inspect(panel, section):
            bounds, surfaces, widget_bounds = [], [], []
            for quick in section.findChildren(QQuickWidget):
                if not quick.isVisibleTo(panel):
                    continue
                root = quick.rootObject()
                if root is None:
                    continue
                surfaces.append({"name": quick.objectName(), "source": Path(quick.source().toLocalFile()).name,
                                 "size": [quick.width(), quick.height()], "root": [root.width(), root.height()],
                                 "graphics_api": graphics_api(quick)})
                owner = quick.parentWidget()
                geom = quick.geometry()
                if geom.x() < -1 or geom.right() > owner.width() + 1 or geom.y() < -1 or geom.bottom() > owner.height() + 1:
                    bounds.append({"surface": quick.objectName(), "kind": "host_bounds", "rect": [geom.x(), geom.y(), geom.width(), geom.height()], "owner": [owner.width(), owner.height()]})

                def visit(item, clipped=False):
                    if not item.isVisible():
                        return
                    meta, interactive = item.metaObject(), False
                    while meta is not None:
                        if meta.className() in {"QQuickAbstractButton", "QQuickComboBox", "QQuickTextField", "QQuickSpinBox"}:
                            interactive = True
                            break
                        meta = meta.superClass()
                    if interactive and not clipped:
                        pt = item.mapToItem(root, QPointF(0, 0))
                        if pt.x() < -1 or pt.y() < -1 or pt.x() + item.width() > root.width() + 1 or pt.y() + item.height() > root.height() + 1:
                            bounds.append({"surface": quick.objectName(), "control": item.objectName() or item.metaObject().className(),
                                           "rect": [round(pt.x(), 1), round(pt.y(), 1), round(item.width(), 1), round(item.height(), 1)], "root": [root.width(), root.height()]})
                    for child in item.childItems():
                        visit(child, clipped or item.clip())
                visit(root)
            for widget in section.findChildren(QObject):
                if not isinstance(widget, (QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit)):
                    continue
                if not widget.isVisibleTo(panel):
                    continue
                pt = widget.mapTo(section, widget.rect().topLeft())
                if pt.x() < -1 or pt.x() + widget.width() > section.width() + 1:
                    widget_bounds.append({"class": type(widget).__name__, "name": widget.objectName(), "x": pt.x(), "width": widget.width(), "section_width": section.width()})
            return {"section_size": [section.width(), section.height()], "surfaces": surfaces,
                    "qml_bounds": bounds, "widget_horizontal_bounds": widget_bounds,
                    "horizontal_ranges": [{"class": type(scroll).__name__, "maximum": scroll.horizontalScrollBar().maximum()} for scroll in [panel, *panel.findChildren(QScrollArea)]]}

        try:
            for theme in themes:
                apply_quick_style(app, theme)
                panel = ip.InstrumentPanel(**controllers)
                panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
                # Keep the scrollbar gutter that is present in the real
                # multi-section sidebar, even in an isolated short section.
                panel._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
                if not baseline:
                    from ui.quick_instrument_layouts import configure_instrument_layout
                    configure_instrument_layout(panel)
                install_table_editors(panel)
                install_parameter_forms(panel, "instruments")
                observed = set()

                def observe():
                    for quick in panel.findChildren(QQuickWidget):
                        if id(quick) in observed:
                            continue
                        observed.add(id(quick))
                        quick.engine().warnings.connect(lambda errors: warnings.extend(error.toString() for error in errors))
                        if quick.status() == QQuickWidget.Status.Error:
                            warnings.extend(error.toString() for error in quick.errors())

                panel.resize(widths[0], 1000)
                panel.show()
                settle()
                observe()
                for width in widths:
                    panel.setFixedWidth(width)
                    # Overview preserves the real default expansion policy.
                    panel.resize(width, 1100)
                    settle()
                    image = f"{theme}_{width}_overview.png"
                    assert panel.grab().save(str(output / image))
                    records.append({"theme": theme, "requested_width": width, "case": "overview", "image": image,
                                    "panel_size": [panel.width(), panel.height()], "sections": list(panel._sections)})
                    cases = [(key, "default") for key in panel._sections]
                    cases += [("lf6", "advanced"), ("lf6", "andor_si"), ("lf6", "winspec_ingaas"),
                              ("rot1", "esp300"), ("rot2", "esp300"), ("stage", "esp300"), ("imaging_stage", "advanced")]
                    for key, variant in cases:
                        for candidate, expander in panel._expanders.items():
                            expander._btn.setChecked(candidate == key)
                            expander.setVisible(candidate == key)
                        section = panel._sections[key]
                        if key == "lf6":
                            section._advanced_expander._btn.setChecked(variant == "advanced")
                            index = section._backend.findData(variant if variant in {"andor_si", "winspec_ingaas"} else "lightfield")
                            section._backend.setCurrentIndex(index)
                        if key.startswith("rot"):
                            section._type_combo.setCurrentText("Newport ESP300 (shared)" if variant == "esp300" else "Thorlabs Elliptec")
                            if variant == "esp300":
                                controllers["rotation_ctrl"].axes_scanned.emit(key, [1, 2, 3])
                                controllers["rotation_ctrl"].motion_profile_ready.emit(key, SimpleNamespace(
                                    axis=1 if key == "rot1" else 2, stage_id="AUDIT ONLY", velocity=10.0,
                                    max_velocity=20.0, acceleration=15.0, deceleration=15.0, max_acceleration=30.0))
                        if key == "stage":
                            section._type_combo.setCurrentIndex(section._type_combo.findData("esp300" if variant == "esp300" else "elliptec"))
                            if variant == "esp300":
                                controllers["stage_ctrl"].axes_scanned.emit([1, 2, 3])
                        if key == "imaging_stage":
                            section._advanced_btn.setChecked(variant == "advanced")
                        panel.resize(width, 1100)
                        panel._scroll_area.verticalScrollBar().setValue(0)
                        settle()
                        # Render the complete section in a tall hidden native
                        # host so each audit image includes its final controls.
                        height = min(4400, max(250, section.sizeHint().height() + 80))
                        panel.resize(width, height)
                        settle()
                        observe()
                        image = f"{theme}_{width}_{key}_{variant}.png"
                        assert panel.grab().save(str(output / image))
                        records.append({"theme": theme, "requested_width": width, "case": key, "variant": variant,
                                        "image": image, "panel_size": [panel.width(), panel.height()], **inspect(panel, section)})
                    for key, expander in panel._expanders.items():
                        expander.show()
                        expander._btn.setChecked(key != "manual_smu")
                panel.close()
                panel.deleteLater()
                settle()
        finally:
            sys.excepthook = previous_hook
            qInstallMessageHandler(previous_handler)
            sys.meta_path.remove(guard)
        report = {"baseline": baseline, "native": native, "backend": backend, "backend_environment": graphics_environment,
                  "isolated_configuration": True, "hardware_import_attempts": guard.attempts,
                  "unexpected_commands": commands, "config_saves_blocked": len(config_saves),
                  "qml_warnings": sorted(set(warnings)), "callback_errors": callbacks, "qt_messages": messages, "captures": records}
        (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        summary = {"output": str(output), "captures": len(records), "hardware_import_attempts": len(guard.attempts),
                   "unexpected_commands": len(commands), "callback_errors": len(callbacks), "qml_warnings": len(set(warnings)),
                   "bounds_cases": sum(bool(record.get("qml_bounds") or record.get("widget_horizontal_bounds")) for record in records),
                   "horizontal_range_cases": sum(any(scroll["maximum"] > 0 for scroll in record.get("horizontal_ranges", [])) for record in records)}
        print(json.dumps(summary, ensure_ascii=False))
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "tmp" / "quick-instruments")
    parser.add_argument("--native", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--backend", choices=("default", "software", "d3d11", "opengl"), default="default")
    parser.add_argument("--baseline", action="store_true", help="Capture the existing sidebar before layout regrouping")
    parser.add_argument("--widths", type=int, nargs="+", default=[460, 350])
    parser.add_argument("--themes", choices=("light", "dark"), nargs="+", default=["light", "dark"])
    args = parser.parse_args()
    report = audit(args.output, native=args.native, backend=args.backend, baseline=args.baseline, widths=tuple(args.widths), themes=tuple(args.themes))
    bad_geometry = any(record.get("qml_bounds") or record.get("widget_horizontal_bounds")
                       or record["panel_size"][0] != record["requested_width"]
                       or any(scroll["maximum"] > 0 for scroll in record.get("horizontal_ranges", []))
                       for record in report["captures"])
    return int(bool(report["hardware_import_attempts"] or report["unexpected_commands"]
                    or report["callback_errors"] or report["qml_warnings"] or bad_geometry))


if __name__ == "__main__":
    raise SystemExit(main())
