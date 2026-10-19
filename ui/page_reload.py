"""Registered, transactional idle-only page reloads; controllers stay alive."""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sys
from types import ModuleType

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QApplication, QWidget

from ui.spectrum_reload_state import (
    capture_spectrum_state, restore_spectrum_state, spectrum_reload_block_reason,
)
from ui.dual_gate_reload_state import (
    capture_dual_gate_state, restore_dual_gate_state, dual_gate_reload_block_reason,
    detach_dual_gate_windows, attach_dual_gate_windows,
)
from ui.settings_reload_state import (
    capture_settings_state, restore_settings_state, settings_reload_block_reason,
)


_MODULES = ("ui.andor_controls_widget", "ui.spectrum_plot", "ui.spectrum_panel")
_DUAL_GATE_MODULES = (
    "ui.preview_widget", "ui.loop_run_selection", "ui.setup_sequence_editor", "ui.presets_panel",
)


@dataclass(frozen=True)
class PageReloadSpec:
    page_id: str
    title: str
    window_attr: str
    modules: tuple[str, ...]
    class_name: str
    construct: object
    capture: object
    restore: object
    block_reason: object
    detach: object = None
    attach: object = None


def _new_spectrum(cls, old, parent):
    new = cls(lf6_ctrl=old._ctrl, parent=parent, bind_controller=False)
    new.set_sidebar_snapshot_provider(old._sidebar_snapshot_provider)
    return new


def _new_dual_gate(cls, old, parent):
    return cls(lf6_ctrl=old._lf6, smu_ctrl=old._smu, rotation_ctrl=old._rot,
               stage_ctrl=old._stage, pm_ctrl=old._pm, parent=parent,
               bind_controller=False, initialize_plan=False)


PAGE_RELOAD_REGISTRY = {
    "spectrum": PageReloadSpec(
        "spectrum", "Spectrum", "_spectrum", _MODULES, "SpectrumPanel",
        _new_spectrum, capture_spectrum_state, restore_spectrum_state, spectrum_reload_block_reason,
        lambda page: page.detach_calibration_page(),
        lambda page, editor: page.attach_calibration_page(editor) if editor is not None else None,
    ),
    "dual_gate": PageReloadSpec(
        "dual_gate", "Dual Gate", "_presets", _DUAL_GATE_MODULES, "PresetsPanel",
        _new_dual_gate, capture_dual_gate_state, restore_dual_gate_state, dual_gate_reload_block_reason,
        detach_dual_gate_windows, attach_dual_gate_windows,
    ),
    "settings": PageReloadSpec(
        "settings", "Settings", "_settings", ("ui.settings_panel",), "SettingsPanel",
        lambda cls, old, parent: cls(lf6_ctrl=old._ctrl, parent=parent, bind_controller=False),
        capture_settings_state, restore_settings_state, settings_reload_block_reason,
    ),
}


def _source_path(name):
    return Path(__file__).parent / (name.rsplit(".", 1)[1] + ".py")


@contextmanager
def _fresh_page_modules(names, class_name):
    """Fresh globals leave old instances usable if new code fails to load."""
    package = sys.modules["ui"]
    missing = object()
    modules = {name: sys.modules.get(name, missing) for name in names}
    attributes = {name: getattr(package, name.rsplit(".", 1)[1], missing) for name in names}
    try:
        for name in names:
            path = _source_path(name)
            # Read source directly: rapid same-size edits must not hit stale pyc.
            code = compile(path.read_bytes(), str(path), "exec")
            module = ModuleType(name)
            module.__file__ = str(path)
            module.__package__ = "ui"
            sys.modules[name] = module
            setattr(package, name.rsplit(".", 1)[1], module)
            exec(code, module.__dict__)
        yield getattr(sys.modules[names[-1]], class_name)
    except BaseException:
        for name in reversed(names):
            if modules[name] is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = modules[name]
            attr = name.rsplit(".", 1)[1]
            if attributes[name] is missing:
                if hasattr(package, attr):
                    delattr(package, attr)
            else:
                setattr(package, attr, attributes[name])
        raise


def _fresh_spectrum_modules():
    """Compatibility hook used by Spectrum rollback tests and diagnostics."""
    return _fresh_page_modules(_MODULES, "SpectrumPanel")


def reload_page(window, page_id):
    """Replace one registered page, atomically transferring its shell bindings."""
    try:
        spec = PAGE_RELOAD_REGISTRY[page_id]
    except KeyError:
        raise ValueError(f"Page {page_id!r} does not support live reload") from None
    old = getattr(window, spec.window_attr)
    reason = spec.block_reason(old)
    if reason:
        raise RuntimeError(reason)
    if QApplication.activeModalWidget() is not None:
        raise RuntimeError(f"Close the open dialog before reloading {spec.title}.")
    state = spec.capture(old)
    focused = QApplication.focusWidget()
    focus_attr = next((name for name, value in vars(old).items() if value is focused), None)
    focus_ref = next((r["id"] for r in getattr(old, "_references", []) if r.get("_row_name") is focused), None)
    selection = (focused.selectionStart(), len(focused.selectedText())) if hasattr(focused, "selectedText") else None
    tabs = window._tabs
    index = tabs.indexOf(old)
    if index < 0:
        raise RuntimeError(f"{spec.title} page is no longer attached to the window.")
    active = tabs.currentWidget()
    enabled, visible = tabs.isTabEnabled(index), tabs.isTabVisible(index)
    title, tooltip = tabs.tabText(index), tabs.tabToolTip(index)
    staging = QWidget()
    new = None
    resource = None
    shell_rebound = False
    rebind = getattr(window, "_rebind_reloaded_page", None)
    loader = _fresh_spectrum_modules() if page_id == "spectrum" else _fresh_page_modules(spec.modules, spec.class_name)
    try:
        with loader as panel_class:
            new = spec.construct(panel_class, old, staging)
            new.resize(old.size())
            # Keep the existing public test hook for restoration failures.
            restore = restore_spectrum_state if page_id == "spectrum" else spec.restore
            restore(new, state)
            # A controller worker can change state while widgets are constructed.
            reason = spec.block_reason(old)
            if reason:
                raise RuntimeError(reason)
            with QSignalBlocker(tabs):
                try:
                    old.unbind_controller_signals()
                    if spec.detach is not None:
                        resource = spec.detach(old)
                        spec.attach(new, resource)
                    new.bind_controller_signals()
                    if rebind is not None:
                        shell_rebound = True
                        rebind(page_id, old, new)
                    tabs.insertTab(index, new, title)
                    tabs.setTabToolTip(index, tooltip)
                    tabs.setTabEnabled(index, enabled)
                    tabs.setTabVisible(index, visible)
                    tabs.removeTab(tabs.indexOf(old))
                    setattr(window, spec.window_attr, new)
                    window._session_panels[page_id] = new
                    window._tab_ids.pop(old)
                    window._tab_ids[new] = page_id
                    tabs.setCurrentWidget(new if active is old else active)
                except Exception:
                    new.unbind_controller_signals()
                    if spec.detach is not None:
                        spec.detach(new)
                        spec.attach(old, resource)
                    if shell_rebound:
                        rebind(page_id, new, old)
                    old.bind_controller_signals()
                    if tabs.indexOf(new) >= 0:
                        tabs.removeTab(tabs.indexOf(new))
                    new.setParent(staging)
                    if tabs.indexOf(old) < 0:
                        tabs.insertTab(index, old, title)
                        tabs.setTabToolTip(index, tooltip)
                        tabs.setTabEnabled(index, enabled)
                        tabs.setTabVisible(index, visible)
                    setattr(window, spec.window_attr, old)
                    window._session_panels[page_id] = old
                    window._tab_ids.pop(new, None)
                    window._tab_ids[old] = page_id
                    tabs.setCurrentWidget(active)
                    raise
        # Commit complete. No event-loop pumping occurs during the transaction.
        dispose = getattr(old, "dispose_reloaded_page", None)
        if dispose is not None:
            dispose()
        old.hide()
        old.deleteLater()
        if active is old:
            target = getattr(new, focus_attr, None) if focus_attr else None
            if focus_ref:
                target = next((r["_row_name"] for r in new._references if r["id"] == focus_ref), target)
            if target is not None and target.isEnabled():
                target.setFocus()
                if selection and selection[0] >= 0 and hasattr(target, "setSelection"):
                    target.setSelection(*selection)
        return new
    finally:
        # Failed construction remains parented here and has no subscriptions.
        if new is not None and getattr(window, spec.window_attr) is not new:
            new.unbind_controller_signals()
            dispose = getattr(new, "dispose_reloaded_page", None)
            if dispose is not None:
                dispose()
        staging.deleteLater()


def reload_spectrum_page(window):
    return reload_page(window, "spectrum")


def reload_dual_gate_page(window):
    return reload_page(window, "dual_gate")
