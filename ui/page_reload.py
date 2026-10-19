"""Transactional, idle-only Spectrum UI reload; controllers are never reloaded."""
from contextlib import contextmanager
from pathlib import Path
import sys
from types import ModuleType

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QApplication, QWidget

from ui.spectrum_reload_state import (
    capture_spectrum_state, restore_spectrum_state, spectrum_reload_block_reason,
)


_MODULES = ("ui.andor_controls_widget", "ui.spectrum_plot", "ui.spectrum_panel")


def _source_path(name):
    return Path(__file__).parent / (name.rsplit(".", 1)[1] + ".py")


@contextmanager
def _fresh_spectrum_modules():
    """Fresh globals leave old instances usable if new code fails to load."""
    package = sys.modules["ui"]
    missing = object()
    modules = {name: sys.modules.get(name, missing) for name in _MODULES}
    attributes = {name: getattr(package, name.rsplit(".", 1)[1], missing) for name in _MODULES}
    try:
        for name in _MODULES:
            path = _source_path(name)
            # Read source directly: rapid same-size edits must not hit stale pyc.
            code = compile(path.read_bytes(), str(path), "exec")
            module = ModuleType(name)
            module.__file__ = str(path)
            module.__package__ = "ui"
            sys.modules[name] = module
            setattr(package, name.rsplit(".", 1)[1], module)
            exec(code, module.__dict__)
        yield sys.modules["ui.spectrum_panel"].SpectrumPanel
    except BaseException:
        for name in reversed(_MODULES):
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


def reload_spectrum_page(window):
    """Replace just Spectrum in the existing shell; preserve the Calibration editor."""
    old = window._spectrum
    reason = spectrum_reload_block_reason(old)
    if reason:
        raise RuntimeError(reason)
    if QApplication.activeModalWidget() is not None:
        raise RuntimeError("Close the open dialog before reloading Spectrum.")
    state = capture_spectrum_state(old)
    focused = QApplication.focusWidget()
    focus_attr = next((name for name, value in vars(old).items() if value is focused), None)
    focus_ref = next((r["id"] for r in old._references if r.get("_row_name") is focused), None)
    selection = (focused.selectionStart(), len(focused.selectedText())) if hasattr(focused, "selectedText") else None
    tabs = window._tabs
    index = tabs.indexOf(old)
    if index < 0:
        raise RuntimeError("Spectrum page is no longer attached to the window.")
    active = tabs.currentWidget()
    enabled, visible = tabs.isTabEnabled(index), tabs.isTabVisible(index)
    title, tooltip = tabs.tabText(index), tabs.tabToolTip(index)
    staging = QWidget()
    new = None
    calibration = old._wavelength_dialog
    detached = False
    try:
        with _fresh_spectrum_modules() as panel_class:
            new = panel_class(lf6_ctrl=old._ctrl, parent=staging, bind_controller=False)
            restore_spectrum_state(new, state)
            new.set_sidebar_snapshot_provider(old._sidebar_snapshot_provider)
            # A controller worker can change state while widgets are constructed.
            reason = spectrum_reload_block_reason(old)
            if reason:
                raise RuntimeError(reason)
            with QSignalBlocker(tabs):
                try:
                    detached = True
                    old.unbind_controller_signals()
                    old.detach_calibration_page()
                    if calibration is not None:
                        new.attach_calibration_page(calibration)
                    new.bind_controller_signals()
                    tabs.insertTab(index, new, title)
                    tabs.setTabToolTip(index, tooltip)
                    tabs.setTabEnabled(index, enabled)
                    tabs.setTabVisible(index, visible)
                    tabs.removeTab(tabs.indexOf(old))
                    window._spectrum = new
                    window._session_panels["spectrum"] = new
                    window._tab_ids.pop(old)
                    window._tab_ids[new] = "spectrum"
                    tabs.setCurrentWidget(new if active is old else active)
                except Exception:
                    new.unbind_controller_signals()
                    new.detach_calibration_page()
                    if calibration is not None:
                        old.attach_calibration_page(calibration)
                    old.bind_controller_signals()
                    if tabs.indexOf(new) >= 0:
                        tabs.removeTab(tabs.indexOf(new))
                    new.setParent(staging)
                    if tabs.indexOf(old) < 0:
                        tabs.insertTab(index, old, title)
                        tabs.setTabToolTip(index, tooltip)
                        tabs.setTabEnabled(index, enabled)
                        tabs.setTabVisible(index, visible)
                    window._spectrum = old
                    window._session_panels["spectrum"] = old
                    window._tab_ids.pop(new, None)
                    window._tab_ids[old] = "spectrum"
                    tabs.setCurrentWidget(active)
                    raise
        # Commit complete. No event-loop pumping occurs during the transaction.
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
        if new is not None and window._spectrum is not new and not detached:
            new.unbind_controller_signals()
        staging.deleteLater()
