"""Global sample controls keep one owner and preserve hidden recipe sources."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from types import SimpleNamespace
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QSplitter, QComboBox, QLineEdit, QPushButton
from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.quick_sidebar import install_sidebar_forms
from ui.quick_workspace_layouts import configure_workspace_layout
from ui.quick_parameter_forms import install_parameter_forms
from utils.config import cfg


def test_sample_sources_are_parked_and_sidebar_height_belongs_to_instruments():
    quick_application()
    window = QWidget()
    window.setProperty("quickGlobalSample", True)
    window._sidebar_splitter = QSplitter(Qt.Orientation.Horizontal, window)
    sidebar = QWidget()
    layout = QVBoxLayout(sidebar)
    window._sidebar_splitter.addWidget(sidebar)
    window._sample_summary = QWidget()
    summary = QVBoxLayout(window._sample_summary)
    window._sample_selector = QComboBox()
    window._sample_selector.setEditable(True)
    window._sample_selector.addItems(["A", "B"])
    window._sample_selector.setEditText("draft")
    summary.addWidget(window._sample_selector)
    layout.addWidget(window._sample_summary)
    window._sidebar_content_splitter = QSplitter(Qt.Orientation.Vertical)
    layout.addWidget(window._sidebar_content_splitter)
    window._inst_panel = QWidget()
    window._history_container = QWidget()
    history = QVBoxLayout(window._history_container)
    window._sample_settings_panel = QWidget()
    settings = QVBoxLayout(window._sample_settings_panel)
    window._sample_filter = QLineEdit("old filter")
    window._sample_filter.textChanged.connect(lambda _: window._sample_selector.setEditText("A"))
    settings.addWidget(window._sample_filter)
    window._sample_save_btn = QPushButton("Save")
    settings.addWidget(window._sample_save_btn)
    window._history_toggle = QPushButton("Sample settings")
    history.addWidget(window._history_toggle)
    history.addWidget(window._sample_settings_panel)
    window._sidebar_content_splitter.addWidget(window._inst_panel)
    window._sidebar_content_splitter.addWidget(window._history_container)
    calls = []
    window._sample_save_btn.clicked.connect(lambda: calls.append(window._sample_selector.currentText()))
    manager = install_sidebar_forms(window)
    try:
        assert window._sample_filter.text() == ""
        assert window._sample_selector.currentText() == "draft"
        assert window._sidebar_content_splitter.count() == 1
        assert window._sidebar_content_splitter.widget(0) is window._inst_panel
        assert window._quick_sample_state_host.isHidden()
        assert window._quick_sample_state_host.isAncestorOf(window._sample_selector)
        assert not any(window._sample_selector in view.adapter._fields.values() for view in manager.forms)
        window._sample_save_btn.click()
        assert calls == ["draft"]
    finally:
        manager.shutdown()
        window.deleteLater()
        drain_qt_deletions()


@pytest.mark.parametrize("page_id,attribute", [
    ("dual_gate", "_sample_edit"), ("mega_sweep", "_sample_edit"),
    ("power_sweep", "_devid_edit"), ("bfp", "_dev_edit"),
    ("mcd", "_sample_id"), ("mcd2100", "_sample_id")])
def test_workflow_sample_source_stays_bound_but_has_no_duplicate_editor(monkeypatch, page_id, attribute):
    app = quick_application()
    previous = app.property("spectralQuick")
    app.setProperty("spectralQuick", True)
    monkeypatch.setattr(cfg, "save", lambda: None)
    from ui.presets_panel import PresetsPanel
    from ui.megasweep_panel import MegaSweepPanel
    from ui.power_sweep_panel import PowerSweepPanel
    from ui.bfp_panel_integrated import BFPPanel
    from ui.mcd_panel import MCDPanel
    from ui.mcd2100_panel import MCD2100Panel
    from ui.quick_workspaces_preview import DisconnectedPreviewMagnet
    factories = {"dual_gate": PresetsPanel, "mega_sweep": MegaSweepPanel,
                 "power_sweep": PowerSweepPanel, "bfp": BFPPanel,
                 "mcd": lambda: MCDPanel(DisconnectedPreviewMagnet(), None, None, None),
                 "mcd2100": lambda: MCD2100Panel(SimpleNamespace(state=SimpleNamespace(name="DISCONNECTED"), generation=0))}
    panel = factories[page_id]()
    editor = getattr(panel, attribute)
    editor.setText("shared-sample")
    panel.setProperty("quickGlobalSample", True)
    forms = []
    try:
        configure_workspace_layout(panel, page_id)
        assert editor.isHidden()
        forms = install_parameter_forms(panel, page_id)["forms"]
        for view in forms:
            assert not view.adapter._visible(editor)
        editor.setText("next-sample")
        assert getattr(panel, attribute) is editor
        assert editor.text() == "next-sample"
        if page_id == "dual_gate":
            assert panel._quick_gate_sections["sample"].title() == "Location and temperature"
            assert not panel._point_edit.isHidden()
    finally:
        for view in forms:
            view.shutdown()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()
        app.setProperty("spectralQuick", previous)
