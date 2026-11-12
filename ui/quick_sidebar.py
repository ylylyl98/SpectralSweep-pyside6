"""Scalar Quick presentation for the audited outer shared sidebar.

Instrument views retain their own existing Quick models. This manager handles
the sample summary/editor, sidebar header and history disclosure only, and does
not instantiate MainWindow, a controller, a sample store or a persistence path.
"""
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QSplitter, QWidget
from shiboken6 import isValid

from ui.quick_parameter_forms import install_parameter_forms


SIDEBAR_SOURCES = ("_sample_summary", "_sample_settings_panel", "_sample_selector", "_sample_filter",
                   "_sample_new_btn", "_sample_duplicate_btn", "_sample_save_btn", "_sample_save_status",
                   "_run_history_btn", "_sidebar_hide", "_history_toggle")


def _park_sample_controls(window, sidebar):
    """Retain transaction sources without a second sample-management surface."""
    if not window.property("quickGlobalSample"):
        return
    parking = QWidget(sidebar)
    parking.setObjectName("QuickGlobalSampleStateHost")
    parking.hide()
    selector = getattr(window, "_sample_selector", None)
    sample_filter = getattr(window, "_sample_filter", None)
    if selector is not None and sample_filter is not None and sample_filter.text():
        draft = selector.currentText()
        sample_filter.clear()
        selector.setEditText(draft)
    # Detaching a splitter child also releases its reserved height. Leave the
    # source objects, ownership, enabled states and callbacks intact.
    for name in ("_sample_summary", "_history_container", "_sample_settings_panel", "_history_toggle"):
        source = getattr(window, name, None)
        if isinstance(source, QWidget) and not parking.isAncestorOf(source):
            source.setParent(parking)
            source.hide()
    window._quick_sample_state_host = parking


class SidebarForms(QObject):
    def __init__(self, window):
        super().__init__(window)
        self._closed = False
        self.coverage = dict(forms=[], migrated_fields=0, boundaries=[])
        splitter = getattr(window, "_sidebar_splitter", None)
        if not isinstance(splitter, QSplitter) or not splitter.count():
            return
        sidebar = splitter.widget(0)
        if sidebar is getattr(window, "_inst_panel", None):
            return
        if not any(isinstance(getattr(window, name, None), QWidget) for name in SIDEBAR_SOURCES):
            return
        _park_sample_controls(window, sidebar)
        # Keep stable source/automation names without copying instrument fields.
        for name in SIDEBAR_SOURCES:
            source = getattr(window, name, None)
            if isinstance(source, QWidget):
                setattr(sidebar, name, source)
        self.sidebar = sidebar
        self.coverage = install_parameter_forms(sidebar, "instruments", refresh=True)
        window.destroyed.connect(self.shutdown)

    @property
    def forms(self):
        return self.coverage["forms"]

    def shutdown(self, *_):
        if self._closed:
            return
        self._closed = True
        for form in self.forms:
            if isValid(form):
                form.shutdown()


def install_sidebar_forms(window):
    manager = getattr(window, "_quick_sidebar_forms", None)
    if manager is None:
        manager = SidebarForms(window)
        window._quick_sidebar_forms = manager
    return manager
