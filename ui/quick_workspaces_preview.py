"""Interactive full-workspace preview using real pages and no device runtime.

Call only after prepare_preview_environment has configured a disposable root.
The regular MainWindow is deliberately not imported: it owns real controllers.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QEvent, QObject, QSettings, Qt, Signal
from PySide6.QtWidgets import (QAbstractButton, QComboBox, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QPushButton, QSplitter, QStatusBar, QTabWidget,
                               QVBoxLayout, QWidget)


class DisconnectedPreviewMagnet(QObject):
    """Only the idle signal contract required to construct the real MCD page."""
    connected = Signal(object)
    disconnected = Signal()
    snapshot_updated = Signal(object)
    transition_progress = Signal(object)
    operation_finished = Signal(object)
    error = Signal(str)
    fault = Signal(str)
    is_connected = False
    exclusive_owner = None

    def unavailable(self, *args, **kwargs):
        raise RuntimeError("Devices are disconnected in the full-workspace preview")

    disconnect_instrument = take_remote = refresh_snapshot = refresh_rates = pause = unavailable


class WorkspacePreviewWindow(QMainWindow):
    def shutdown(self):
        if getattr(self, "_preview_closed", False):
            return True
        self.quick_workbench.shutdown()
        for _, _, page in self.workspace_pages:
            owner_shutdown = getattr(page, "shutdown", None)
            if callable(owner_shutdown) and owner_shutdown(timeout_ms=0) is False:
                return False
            dispose = getattr(page, "dispose_reloaded_page", None)
            if callable(dispose):
                dispose()
        self._lf6.abort_acquisition()
        self._preview_closed = True
        return True

    def closeEvent(self, event):
        if self.shutdown():
            event.accept()
        else:
            event.ignore()


class PreviewOperationGuard(QObject):
    """Keep disconnected preview actions disabled when page refreshes run."""
    def __init__(self, parent):
        super().__init__(parent)
        self.buttons = []

    def block(self, button):
        if button in self.buttons:
            return
        self.buttons.append(button)
        button.installEventFilter(self)
        button.setEnabled(False)
        button.setToolTip("Devices are disconnected in this preview")

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.EnabledChange and watched.isEnabled():
            watched.setEnabled(False)
        return False


def build_preview_window(root, app, *, hidden=False):
    """Return a real nine-page window with .quick_workbench and .shutdown().

    Configuration, history and QSettings must stay beneath ``root``. Page
    editing, tables, plotting navigation and themes remain interactive; all
    connection, measurement and magnet actions are disabled at their sources.
    Spectrum owns an in-memory PreviewSpectrumController, initially disconnected.
    """
    root = Path(root).resolve()
    for key in ("SPECTRALSWEEP_CONFIG_PATH", "SPECTRALSWEEP_HISTORY_PATH"):
        path = Path(os.environ.get(key, "")).resolve()
        if not path.is_relative_to(root):
            raise RuntimeError(f"Full-workspace preview requires isolated {key} beneath its root")
    from utils import config
    if not Path(config._CONFIG_FILE).resolve().is_relative_to(root):
        raise RuntimeError("Configuration was imported before preview isolation was configured")
    from app.experiment_metadata import ExperimentHistory
    from ui.bfp_panel_integrated import BFPPanel
    from ui.instrument_panel import InstrumentPanel
    from ui.mcd_panel import MCDPanel
    from ui.mcd2100_panel import MCD2100Panel
    from ui.megasweep_panel import MegaSweepPanel
    from ui.power_sweep_panel import PowerSweepPanel
    from ui.presets_panel import PresetsPanel
    from ui.quick_workbench import QuickWorkbench
    from ui.settings_panel import SettingsPanel
    from ui.spectrum_panel import SpectrumPanel
    from ui.spectrum_preview import PreviewSpectrumController

    app.setProperty("spectralQuick", True)
    from ui.quick_style import apply_quick_style
    apply_quick_style(app, app.property("quickThemeMode") or "system")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    for scope in (QSettings.Scope.UserScope, QSettings.Scope.SystemScope):
        QSettings.setPath(QSettings.Format.IniFormat, scope, str(root / "settings"))
    window = WorkspacePreviewWindow()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, hidden)
    window.setWindowTitle("SpectralSweep — full workspace PREVIEW · devices disconnected")
    window.preview_root = root
    window._tabs = QTabWidget(window)
    window._status = QStatusBar(window)
    window.setStatusBar(window._status)
    window._status.showMessage("Preview · devices disconnected · measurement actions disabled")
    window._lf6 = PreviewSpectrumController(window)
    window._power_sweep_running = False
    window._active_mcd_panel = None
    window._sample_id_binder = SimpleNamespace(value="PREVIEW")
    magnet = DisconnectedPreviewMagnet(window)
    atto = SimpleNamespace(state=SimpleNamespace(name="DISCONNECTED"), generation=0)
    window._spectrum = SpectrumPanel(window._lf6)
    window._presets = PresetsPanel()
    window._mega = MegaSweepPanel()
    window._power_sweep = PowerSweepPanel()
    window._mcd = MCDPanel(magnet, None, None, None)
    window._mcd2100 = MCD2100Panel(atto)
    window._bfp = BFPPanel()
    window._settings = SettingsPanel()
    window._calibration = window._spectrum.create_calibration_page()
    window._inst_panel = InstrumentPanel(lf6_ctrl=window._lf6)
    window.workspace_pages = [
        ("dual_gate", "Dual Gate", window._presets),
        ("mega_sweep", "2D Sweep", window._mega),
        ("power_sweep", "Motion Sweep", window._power_sweep),
        ("mcd", "MCD 1000", window._mcd),
        ("mcd2100", "MCD 2100", window._mcd2100),
        ("bfp", "BFP", window._bfp),
        ("spectrum", "Spectrum", window._spectrum),
        ("settings", "Settings", window._settings),
        ("calibration", "Calibration", window._calibration),
    ]
    window._tab_ids = {}
    for key, title, page in window.workspace_pages:
        page.setObjectName("PreviewPage_" + key)
        window._tabs.addTab(page, title)
        window._tab_ids[page] = key
    window._active_tab_id = lambda: window._tab_ids[window._tabs.currentWidget()]
    window._history_type = QComboBox(window)
    window._history_type.addItem("dual_gate_sweep")
    window._history = ExperimentHistory(root / "history.sqlite3")
    window._history_adapters = lambda: {}
    window._acquisition_active = lambda: False
    # Match the real shell's audited sidebar shape using only scratch-backed
    # callbacks. The production MainWindow and sample/device controllers remain
    # outside this isolated preview.
    sidebar = QWidget()
    sidebar.setObjectName("SharedInstrumentHistorySidebar")
    sidebar.setMinimumWidth(300)
    sidebar_layout = QVBoxLayout(sidebar)
    sidebar_layout.setContentsMargins(4, 4, 4, 4)
    sidebar_layout.setSpacing(4)
    header = QHBoxLayout()
    header.addWidget(QLabel("Workspace"))
    header.addStretch()
    window._sidebar_hide = QPushButton("Hide")
    header.addWidget(window._sidebar_hide)
    sidebar_layout.addLayout(header)
    window._sample_summary = QWidget()
    window._sample_summary.setObjectName("SidebarSampleSummary")
    summary = QVBoxLayout(window._sample_summary)
    summary.setContentsMargins(0, 0, 0, 4)
    selector_row = QHBoxLayout()
    selector_row.addWidget(QLabel("Sample ID"))
    window._sample_selector = QComboBox()
    window._sample_selector.setEditable(True)
    window._sample_selector.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    window._sample_selector.setAccessibleName("Current sample ID")
    sample_ids = ["PREVIEW", "PREVIEW_B"]
    window._sample_selector.addItems(sample_ids)
    selector_row.addWidget(window._sample_selector, 1)
    summary.addLayout(selector_row)
    window._sample_save_status = QLabel("Preview sample · scratch storage only")
    window._sample_save_status.setWordWrap(True)
    summary.addWidget(window._sample_save_status)
    sidebar_layout.addWidget(window._sample_summary)
    window._sample_settings_panel = QWidget()
    settings_layout = QVBoxLayout(window._sample_settings_panel)
    settings_layout.setContentsMargins(0, 0, 0, 0)
    window._sample_filter = QLineEdit()
    window._sample_filter.setPlaceholderText("Search saved samples")
    settings_layout.addWidget(window._sample_filter)
    buttons = QHBoxLayout()
    window._sample_new_btn = QPushButton("New sample")
    window._sample_duplicate_btn = QPushButton("Duplicate current")
    window._sample_save_btn = QPushButton("Save now")
    for button in (window._sample_new_btn, window._sample_duplicate_btn, window._sample_save_btn):
        buttons.addWidget(button)
    settings_layout.addLayout(buttons)
    window._run_history_btn = QPushButton("Open run history")
    settings_layout.addWidget(window._run_history_btn)
    window._history_container = QWidget()
    history_layout = QVBoxLayout(window._history_container)
    history_layout.setContentsMargins(0, 0, 0, 0)
    window._history_toggle = QPushButton("Show sample settings")
    history_layout.addWidget(window._history_toggle)
    history_layout.addWidget(window._sample_settings_panel)
    window._sample_settings_panel.hide()
    inner = QSplitter(Qt.Orientation.Vertical)
    window._sidebar_content_splitter = inner
    inner.addWidget(window._inst_panel)
    inner.addWidget(window._history_container)
    inner.setStretchFactor(0, 1)
    inner.setStretchFactor(1, 0)
    inner.setSizes([600, 40])
    sidebar_layout.addWidget(inner, 1)

    def commit_sample():
        value = window._sample_selector.currentText().strip()
        if value:
            window._sample_id_binder.value = value
            config.cfg.session.sample_id = value
            window._sample_save_status.setText("Sample " + value + " · not saved")
        return bool(value)

    def save_sample():
        if commit_sample():
            config.cfg.save()
            value = window._sample_id_binder.value
            if value not in sample_ids:
                sample_ids.append(value)
            window._sample_save_status.setText("Saved " + value + " to preview scratch")

    def filter_samples(text):
        selector = window._sample_selector
        previous = selector.blockSignals(True)
        selector.clear()
        selector.addItems([value for value in sample_ids if text.casefold() in value.casefold()])
        selector.setEditText(window._sample_id_binder.value)
        selector.blockSignals(previous)

    def new_sample(duplicate=False):
        value = window._sample_id_binder.value + "_copy" if duplicate else "PREVIEW_" + str(len(sample_ids) + 1)
        if value not in sample_ids:
            sample_ids.append(value)
        filter_samples(window._sample_filter.text())
        window._sample_selector.setEditText(value)
        commit_sample()

    def toggle_sample_settings():
        visible = window._sample_settings_panel.isHidden()
        window._sample_settings_panel.setVisible(visible)
        window._history_toggle.setText("Hide sample settings" if visible else "Show sample settings")

    window._sample_selector.lineEdit().editingFinished.connect(commit_sample)
    window._sample_selector.activated.connect(lambda *_: commit_sample())
    window._sample_filter.textChanged.connect(filter_samples)
    window._sample_new_btn.clicked.connect(lambda: new_sample(False))
    window._sample_duplicate_btn.clicked.connect(lambda: new_sample(True))
    window._sample_save_btn.clicked.connect(save_sample)
    window._history_toggle.clicked.connect(toggle_sample_settings)
    window._run_history_btn.clicked.connect(lambda: window.quick_workbench.showHistory())
    window._save_current_sample = save_sample
    splitter = QSplitter(Qt.Orientation.Horizontal)
    window._sidebar_splitter = splitter
    splitter.addWidget(sidebar)
    splitter.addWidget(window._tabs)
    splitter.setStretchFactor(1, 1)
    splitter.setSizes([260, 1140])
    window._toggle_sidebar = lambda: sidebar.setVisible(sidebar.isHidden())
    window._sidebar_hide.clicked.connect(window._toggle_sidebar)
    window.setCentralWidget(splitter)

    disabled = []
    guard = PreviewOperationGuard(window)
    window.preview_operation_guard = guard
    # Retain the real signal/adapter paths, with disconnected admission at the
    # source controls. Never replace a Run action with a simulated acquisition.
    operation_attributes = ("_run_btn", "_run_1d_btn", "_run_2d_btn", "start_btn",
        "_acquire_btn", "_acquire_2d_btn", "_connect_btn", "connect_btn",
        "_disconnect_btn", "disconnect_btn", "_remote_btn", "_refresh_btn",
        "refresh_btn", "_safe_move_btn", "_safe_zero_btn", "_driven_btn",
        "_persistent_btn", "_safe_rates_refresh_btn", "prepare_magnet_btn",
        "read_ramp_tables_btn", "_pause_btn")
    for _, _, page in window.workspace_pages:
        for name in operation_attributes:
            button = getattr(page, name, None)
            if isinstance(button, QAbstractButton):
                guard.block(button)
                disabled.append(page.objectName() + "." + name)
    for button in window.findChildren(QAbstractButton):
        text = button.text().replace("&", "").strip().lower()
        if (text in {"connect", "disconnect", "apply + verify", "refresh all",
                     "scan and save calibration", "measure reference"}
                or text.startswith(("acquire ", "collect "))):
            guard.block(button)
            disabled.append("preview." + button.text())
    window.preview_disabled_operations = disabled
    window.quick_workbench = QuickWorkbench(window)
    window.resize(1400, 900)
    return window
