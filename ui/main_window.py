# ui/main_window.py
# ──────────────────────────────────────────────────────────────────────────────
# Top-level application window.
#
# Layout:
#   Left splitter pane : shared InstrumentPanel + Experiment History
#   Main tabs         : Dual Gate | 2D Sweep | Motion Sweep | MCD | BFP | ...
#
# Controllers are created once here and injected into all panels.
# Reload of UI modules is possible without dropping controller connections.
#
# Rules:
#   - All instrument state lives in controllers only.
#   - importlib.reload() safe for ui/ modules.
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import sys
import json
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QRect, QSettings, QTimer, QObject, QEvent, Slot
from PySide6.QtGui  import QCloseEvent, QFont, QAction
from PySide6.QtWidgets import (
    QMainWindow, QTabWidget, QWidget, QStatusBar, QApplication,
    QMessageBox, QComboBox, QListWidget, QPushButton, QPlainTextEdit, QLabel,
    QVBoxLayout, QHBoxLayout, QListWidgetItem, QSplitter, QToolBar,
)
from app.experiment_metadata import ExperimentHistory

# ── Global stylesheet ──────────────────────────────────────────────────────────
_STYLESHEET = """
/* Application surfaces */
QWidget {
    color: #202936;
    selection-background-color: #2a75c7;
    selection-color: #ffffff;
}
QMainWindow, QDialog {
    background: #f3f6fa;
}
QToolTip {
    color: #202936;
    background: #ffffff;
    border: 1px solid #cfd7e3;
    border-radius: 5px;
    padding: 5px 7px;
}

/* Cards and sections */
QGroupBox {
    background: #ffffff;
    font-weight: 600;
    border: 1px solid #d9e0e9;
    border-radius: 8px;
    margin-top: 14px;
    padding: 9px 8px 8px 8px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    top: 1px;
    padding: 0 5px;
    color: #29384c;
    background: #ffffff;
}
QFrame[frameShape="4"], QFrame[frameShape="5"] {
    color: #dce2ea;
}

/* Inputs */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox,
QDateEdit, QDateTimeEdit, QTimeEdit {
    min-height: 24px;
    color: #202936;
    background: #ffffff;
    border: 1px solid #cbd4e0;
    border-radius: 5px;
    padding: 2px 7px;
}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover,
QDateEdit:hover, QDateTimeEdit:hover, QTimeEdit:hover {
    border-color: #9caabd;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QDateEdit:focus, QDateTimeEdit:focus, QTimeEdit:focus {
    border: 1px solid #2574c7;
}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QComboBox:disabled, QDateEdit:disabled, QDateTimeEdit:disabled,
QTimeEdit:disabled {
    color: #8b96a5;
    background: #eef1f5;
    border-color: #dde2e9;
}
QComboBox::drop-down {
    width: 24px;
    border: none;
    border-left: 1px solid #e1e6ed;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    border: 1px solid #cbd4e0;
    border-radius: 5px;
    padding: 3px;
    outline: none;
}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {
    width: 18px;
    background: #f7f9fc;
    border: none;
    border-left: 1px solid #e2e7ee;
}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {
    background: #eaf2fb;
}
QPlainTextEdit, QTextEdit {
    color: #202936;
    background: #ffffff;
    border: 1px solid #d3dbe6;
    border-radius: 6px;
    padding: 4px;
}

/* Buttons */
QPushButton {
    min-height: 25px;
    color: #243246;
    background: #ffffff;
    border: 1px solid #c8d1dd;
    border-radius: 6px;
    padding: 4px 13px;
}
QPushButton:hover {
    color: #145da5;
    background: #edf5fd;
    border-color: #79a9d8;
}
QPushButton:pressed {
    background: #dcecfb;
    border-color: #4f8bc6;
}
QPushButton:focus, QPushButton:default {
    border: 1px solid #2877c9;
}
QPushButton:disabled {
    color: #96a0ae;
    background: #edf0f4;
    border-color: #dce1e8;
}
QToolButton {
    min-width: 22px;
    min-height: 22px;
    border: 1px solid transparent;
    border-radius: 5px;
    background: transparent;
}
QToolButton:hover {
    background: #e8f1fb;
    border-color: #c5d9ed;
}
QToolButton:pressed, QToolButton:checked {
    background: #d9eafa;
    border-color: #9fc1e2;
}

/* Navigation */
QTabWidget::pane {
    background: #ffffff;
    border: 1px solid #d7dee8;
    border-radius: 0 8px 8px 8px;
    top: -1px;
}
QTabBar::tab {
    min-width: 76px;
    color: #667386;
    background: transparent;
    border: 1px solid transparent;
    border-bottom: 2px solid transparent;
    padding: 8px 17px;
    margin-right: 2px;
}
QTabBar::tab:hover:!selected {
    color: #245f9b;
    background: #edf4fb;
    border-radius: 6px 6px 0 0;
}
QTabBar::tab:selected {
    color: #174f87;
    background: #ffffff;
    border: 1px solid #d7dee8;
    border-bottom: 2px solid #2374c6;
    border-radius: 6px 6px 0 0;
    font-weight: 600;
}
QDockWidget {
    color: #202936;
}
QDockWidget::title {
    color: #2b3b4f;
    background: #eaf0f7;
    border-bottom: 1px solid #d2dbe6;
    padding: 7px 9px;
    font-weight: 600;
}

/* Data views */
QTableView, QTableWidget, QTreeView, QListView {
    alternate-background-color: #f7f9fc;
    background: #ffffff;
    border: 1px solid #d5dde7;
    border-radius: 6px;
    gridline-color: #e6eaf0;
    outline: none;
}
QTableView::item, QTableWidget::item, QTreeView::item, QListView::item {
    padding: 3px 5px;
}
QTableView::item:hover, QTableWidget::item:hover,
QTreeView::item:hover, QListView::item:hover {
    background: #edf5fd;
}
QTableView::item:selected, QTableWidget::item:selected,
QTreeView::item:selected, QListView::item:selected {
    color: #173b61;
    background: #d9eafb;
}
QHeaderView::section {
    color: #344358;
    background: #eef2f7;
    border: none;
    border-right: 1px solid #dde3eb;
    border-bottom: 1px solid #d5dde7;
    padding: 5px 7px;
    font-weight: 600;
}
QTableCornerButton::section {
    background: #eef2f7;
    border: none;
    border-right: 1px solid #dde3eb;
    border-bottom: 1px solid #d5dde7;
}

/* Feedback and structure */
QProgressBar {
    min-height: 17px;
    color: #344358;
    background: #e8edf3;
    border: 1px solid #d2dae5;
    border-radius: 6px;
    text-align: center;
    font-size: 11px;
}
QProgressBar::chunk {
    background: #2c7bc9;
    border-radius: 5px;
}
QStatusBar {
    color: #536174;
    background: #edf1f6;
    border-top: 1px solid #d5dce6;
    font-size: 11px;
}
QSplitter::handle {
    background: #e1e6ed;
}
QSplitter::handle:hover {
    background: #b7cbe0;
}

/* Scroll bars */
QScrollBar:vertical {
    width: 11px;
    margin: 2px;
    background: transparent;
}
QScrollBar::handle:vertical {
    min-height: 28px;
    background: #c4ceda;
    border-radius: 5px;
}
QScrollBar::handle:vertical:hover { background: #9eacbd; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    height: 0px;
    background: transparent;
}
QScrollBar:horizontal {
    height: 11px;
    margin: 2px;
    background: transparent;
}
QScrollBar::handle:horizontal {
    min-width: 28px;
    background: #c4ceda;
    border-radius: 5px;
}
QScrollBar::handle:horizontal:hover { background: #9eacbd; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    width: 0px;
    background: transparent;
}
"""

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from controllers.lf6_controller      import LF6Controller
from controllers.smu_controller      import SMUController
from controllers.rotation_controller import RotationController
from controllers.stage_controller    import StageController
from controllers.pm100d_controller   import PM100DController
from controllers.magnet_controller   import MagnetController

from ui.instrument_panel import InstrumentPanel
from ui.settings_panel   import SettingsPanel
from ui.spectrum_panel   import SpectrumPanel
from ui.presets_panel    import PresetsPanel
from ui.megasweep_panel  import MegaSweepPanel
from ui.power_sweep_panel import PowerSweepPanel
from ui.bfp_panel_integrated import BFPPanel
from ui.mcd_panel import MCDPanel
from ui.mcd2100_panel import MCD2100Panel
from controllers.attodry2100_controller import AttoDRY2100Controller
from utils.config import cfg


_ORG  = "SpectralSweep"
_APP  = "SpectralSweep"


def _valid_font_size(value: object, default: int = 9) -> int:
    try:
        pt = int(value)
    except (TypeError, ValueError):
        pt = default
    if pt <= 0:
        pt = default
    return min(max(pt, 7), 18)


def _clamp_window_rect(rect: QRect, available: QRect, margin: int = 8) -> QRect:
    """Return a normal-window rectangle fully inside the available screen."""
    safe = available.adjusted(margin, margin, -margin, -margin)
    if safe.width() <= 0 or safe.height() <= 0:
        safe = QRect(available)
    width = min(max(1, rect.width()), safe.width())
    height = min(max(1, rect.height()), safe.height())
    max_x = safe.right() - width + 1
    max_y = safe.bottom() - height + 1
    x = min(max(rect.x(), safe.left()), max_x)
    y = min(max(rect.y(), safe.top()), max_y)
    return QRect(x, y, width, height)


class _SharedSampleIdBinder(QObject):
    """Keep workflow Sample ID edits synchronized without recursive updates."""

    def __init__(self, edits, initial: str = "", parent=None):
        super().__init__(parent)
        self._edits = list(edits)
        self._value = ""
        self._updating = False
        for edit in self._edits:
            edit.textChanged.connect(self._on_text_changed)
        self.set_value(initial)

    @property
    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        value = str(value)
        self._value = value
        self._updating = True
        try:
            for edit in self._edits:
                if edit.text() != value:
                    edit.setText(value)
        finally:
            self._updating = False

    def _on_text_changed(self, value: str) -> None:
        if self._updating:
            return
        self.set_value(value)


class _SessionChangeWatcher(QObject):
    """Debounce user input into a single session-state observation."""

    _CHANGE_EVENTS = {
        QEvent.Type.KeyRelease,
        QEvent.Type.MouseButtonRelease,
        QEvent.Type.Wheel,
        QEvent.Type.Drop,
        QEvent.Type.InputMethod,
        QEvent.Type.FocusOut,
    }

    def __init__(self, owner: QWidget, changed, parent=None):
        super().__init__(parent)
        self._owner = owner
        self._changed = changed

    def eventFilter(self, watched, event) -> bool:
        if event.type() in self._CHANGE_EVENTS and (
            watched is self._owner
            or (isinstance(watched, QWidget) and self._owner.isAncestorOf(watched))
        ):
            self._changed()
        return False


class MainWindow(QMainWindow):
    """
    Application shell.

    Creates controllers, builds panels, wires signals.
    Geometry and sidebar width persisted via QSettings.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("SpectralSweep — PySide6")

        # Apply global stylesheet once (on the QApplication so all windows share it)
        app = QApplication.instance()
        if app:
            app.setStyle("Fusion")
            if not app.styleSheet():
                app.setStyleSheet(_STYLESHEET)
            app_font = QFont("Segoe UI", _valid_font_size(cfg.font_size_pt))
            app.setFont(app_font)
            screen = app.primaryScreen()
            if screen is not None:
                available = screen.availableGeometry()
                self.resize(
                    min(1400, max(1, available.width() - 64)),
                    min(900, max(1, available.height() - 64)),
                )
            else:
                self.resize(1400, 900)
        else:
            self.resize(1400, 900)

        # ── create controllers (one per instrument family) ────────────────────
        self._lf6  = LF6Controller(parent=self)
        self._smu  = SMUController(parent=self)
        self._rot  = RotationController(parent=self)
        self._stg  = StageController(parent=self)
        self._pm   = PM100DController(parent=self)
        self._magnet = MagnetController(parent=self)

        # ── status bar ────────────────────────────────────────────────────────
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Ready")

        self._lf6.connected.connect(
            lambda _: self._status.showMessage("Spectrometer connected")
        )
        self._lf6.disconnected.connect(
            lambda: self._status.showMessage("Spectrometer disconnected")
        )
        self._lf6.error.connect(
            lambda m: self._status.showMessage(f"Spectrometer error: {m[:80]}")
        )
        self._lf6.state_changed.connect(
            lambda state: self._status.showMessage(
                f"Spectrometer {getattr(state, 'value', state).lower()}"
            )
        )
        self._smu.connected.connect(lambda *_: self._status.showMessage("SMU connected"))
        self._smu.disconnected.connect(lambda: self._status.showMessage("SMU disconnected"))
        self._magnet.connected.connect(
            lambda identity: self._status.showMessage(
                f"APS100 connected: {identity.serial}"
            )
        )
        self._magnet.disconnected.connect(
            lambda: self._status.showMessage("APS100 disconnected")
        )
        self._magnet.error.connect(
            lambda message: self._status.showMessage(f"APS100 error: {message[:80]}")
        )

        # ── shared instrument panel (left sidebar, assembled after history) ──
        self._inst_panel = InstrumentPanel(
            lf6_ctrl=self._lf6,
            smu_ctrl=self._smu,
            rotation_ctrl=self._rot,
            stage_ctrl=self._stg,
            pm_ctrl=self._pm,
        )

        # ── main tabs ─────────────────────────────────────────────────────────
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)

        # Dual Gate
        self._presets = PresetsPanel(
            lf6_ctrl=self._lf6,
            smu_ctrl=self._smu,
            rotation_ctrl=self._rot,
            stage_ctrl=self._stg,
            pm_ctrl=self._pm,
        )
        self._tabs.addTab(self._presets, "Dual Gate")

        # 2D Sweep
        self._mega = MegaSweepPanel(
            smu_ctrl=self._smu,
            lf6_ctrl=self._lf6,
        )
        self._tabs.addTab(self._mega, "2D Sweep")

        # Motion Sweep
        self._power_sweep = PowerSweepPanel(
            lf6_ctrl=self._lf6,
            stage_ctrl=self._stg,
            rotation_ctrl=self._rot,
            pm_ctrl=self._pm,
            smu_ctrl=self._smu,
        )
        self._tabs.addTab(self._power_sweep, "Motion Sweep")

        # Continuous magnetic circular dichroism
        self._mcd = MCDPanel(
            magnet_ctrl=self._magnet,
            lf6_ctrl=self._lf6,
            rotation_ctrl=self._rot,
            smu_ctrl=self._smu,
        )
        self._mcd.run_state_changed.connect(
            lambda running: self._on_mcd_workflow_state_changed(self._mcd, running)
        )
        self._tabs.addTab(self._mcd, "MCD 1000")
        self._magnet2100 = AttoDRY2100Controller(config=cfg.attodry2100, parent=self)
        self._mcd2100 = MCD2100Panel(
            self._magnet2100,
            lf6_ctrl=self._lf6,
            rotation_ctrl=self._rot,
            smu_ctrl=self._smu,
            parent=self,
        )
        self._mcd2100.run_state_changed.connect(
            lambda running: self._on_mcd_workflow_state_changed(self._mcd2100, running)
        )
        self._tabs.addTab(self._mcd2100, "MCD 2100")
        self._active_mcd_panel = None

        # BFP
        self._bfp = BFPPanel(lf6_ctrl=self._lf6)
        self._tabs.addTab(self._bfp, "BFP")

        # Spectrum
        self._spectrum = SpectrumPanel(lf6_ctrl=self._lf6)
        self._tabs.addTab(self._spectrum, "Spectrum")

        # Settings
        self._settings = SettingsPanel(lf6_ctrl=self._lf6)
        self._tabs.addTab(self._settings, "Settings")

        self._session_panels = {
            "instruments": self._inst_panel,
            "dual_gate": self._presets,
            "mega_sweep": self._mega,
            "power_sweep": self._power_sweep,
            "mcd": self._mcd,
            "mcd2100": self._mcd2100,
            "bfp": self._bfp,
            "spectrum": self._spectrum,
            "settings": self._settings,
        }
        self._tab_ids = {
            self._presets: "dual_gate",
            self._mega: "mega_sweep",
            self._power_sweep: "power_sweep",
            self._mcd: "mcd",
            self._mcd2100: "mcd2100",
            self._bfp: "bfp",
            self._spectrum: "spectrum",
            self._settings: "settings",
        }

        # ── restore geometry ──────────────────────────────────────────────────
        self._initialize_shared_sample_id()
        self._initialize_history_ui()
        self._build_sidebar_splitter()
        self._restore_geometry()
        self._restore_session()

        # User input schedules a debounced observation.  A slow fallback poll
        # catches programmatic/dynamic-widget changes without walking every
        # panel four times per second while the application is idle.
        self._last_observed_session = self._capture_session()
        self._session_observe_timer = QTimer(self)
        self._session_observe_timer.setSingleShot(True)
        self._session_observe_timer.setInterval(150)
        self._session_observe_timer.timeout.connect(self._poll_session_changes)
        self._session_poll_timer = QTimer(self)
        self._session_poll_timer.setInterval(5000)
        self._session_poll_timer.timeout.connect(self._poll_session_changes)
        self._session_save_timer = QTimer(self)
        self._session_save_timer.setSingleShot(True)
        self._session_save_timer.setInterval(500)
        self._session_save_timer.timeout.connect(self._persist_session)
        self._session_change_watcher = _SessionChangeWatcher(
            self, self._schedule_session_observation, self
        )
        if app is not None:
            app.installEventFilter(self._session_change_watcher)
        self._tabs.currentChanged.connect(self._schedule_session_observation)
        self._session_poll_timer.start()

    # ── geometry persistence ──────────────────────────────────────────────────

    def _active_tab_id(self) -> str:
        widget = self._tabs.currentWidget()
        return self._tab_ids.get(widget, "dual_gate")

    def _initialize_shared_sample_id(self) -> None:
        edits_by_panel = {
            self._presets: self._presets._sample_edit,
            self._mega: self._mega._sample_edit,
            self._power_sweep: self._power_sweep._devid_edit,
            self._bfp: self._bfp._dev_edit,
            self._mcd: self._mcd._sample_id,
            self._mcd2100: self._mcd2100._sample_id,
        }
        if cfg.session.schema_version >= 2:
            initial = cfg.session.sample_id
        else:
            active_edit = edits_by_panel.get(self._tabs.currentWidget())
            initial = active_edit.text() if active_edit is not None else ""
            if not initial:
                initial = next(
                    (
                        edit.text()
                        for edit in edits_by_panel.values()
                        if edit.text()
                    ),
                    "",
                )
        self._sample_id_binder = _SharedSampleIdBinder(
            edits_by_panel.values(),
            initial=initial,
            parent=self,
        )

    def _initialize_history_ui(self) -> None:
        """Compact, explicit history browser shared by all experiment tabs."""
        self._history = ExperimentHistory()
        panel = QWidget()
        layout = QVBoxLayout(panel)
        top = QHBoxLayout()
        self._history_device = QLabel(f"Device: {self._sample_id_binder.value}")
        self._history_type = QComboBox()
        self._history_type.addItems([
            "dual_gate_sweep", "gate_map_2d", "motion_sweep", "mcd_aps100",
            "mcd_attodry2100", "bfp_acquisition", "bfp_binned_rc", "bfp_full_sensor_rc",
        ])
        top.addWidget(self._history_device)
        top.addWidget(self._history_type)
        layout.addLayout(top)
        self._history_list = QListWidget()
        layout.addWidget(self._history_list)
        self._history_preview = QPlainTextEdit()
        self._history_preview.setReadOnly(True)
        self._history_preview.setMaximumHeight(150)
        layout.addWidget(self._history_preview)
        buttons = QHBoxLayout()
        self._history_refresh = QPushButton("Refresh")
        self._history_preview_btn = QPushButton("Preview settings")
        self._history_load = QPushButton("Load compatible settings")
        self._history_load.setEnabled(False)
        buttons.addWidget(self._history_refresh)
        buttons.addWidget(self._history_preview_btn)
        buttons.addWidget(self._history_load)
        layout.addLayout(buttons)
        panel.setMinimumWidth(0)
        self._history_panel = panel
        self._history_refresh.clicked.connect(self._refresh_history)
        self._history_type.currentTextChanged.connect(self._history_type_changed)
        self._tabs.currentChanged.connect(self._history_type_for_active_tab)
        self._history_list.currentRowChanged.connect(self._history_selection_changed)
        self._history_preview_btn.clicked.connect(self._preview_selected_history)
        self._history_load.clicked.connect(self._load_history_settings)
        self._sample_id_binder._edits[0].textChanged.connect(self._refresh_history)
        self._refresh_history()
        self._bfp._tabs.currentChanged.connect(lambda _i: self._history_type_for_active_tab(self._tabs.currentIndex()))

    def _build_sidebar_splitter(self) -> None:
        """Host instruments and history in one responsive, collapsible sidebar."""
        sidebar = QWidget()
        sidebar.setObjectName("SharedInstrumentHistorySidebar")
        sidebar.setMinimumWidth(0)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(4, 4, 4, 4)
        sidebar_layout.setSpacing(4)

        header = QHBoxLayout()
        title = QLabel("Instruments & history")
        title.setStyleSheet("font-weight: 700; color: #29384c;")
        self._sidebar_hide = QPushButton("Hide sidebar")
        self._sidebar_hide.setToolTip("Collapse the instrument and experiment-history sidebar.")
        self._sidebar_hide.clicked.connect(self._toggle_sidebar)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self._sidebar_hide)
        sidebar_layout.addLayout(header)

        inner = QSplitter(Qt.Orientation.Vertical)
        inner.setObjectName("SidebarContentSplitter")
        inner.setChildrenCollapsible(True)
        self._sidebar_content_splitter = inner
        self._history_panel.setMinimumHeight(0)

        history_container = QWidget()
        history_container.setObjectName("ExperimentHistoryContainer")
        history_layout = QVBoxLayout(history_container)
        history_layout.setContentsMargins(0, 0, 0, 0)
        history_layout.setSpacing(2)
        self._history_toggle = QPushButton()
        self._history_toggle.setToolTip("Collapse or expand saved experiment settings.")
        self._history_toggle.clicked.connect(self._toggle_history)
        history_layout.addWidget(self._history_toggle)
        history_layout.addWidget(self._history_panel, 1)
        self._history_container = history_container
        self._history_height = 260
        inner.addWidget(self._inst_panel)
        inner.addWidget(history_container)
        inner.setStretchFactor(0, 3)
        inner.setStretchFactor(1, 2)
        inner.setSizes([520, 260])
        sidebar_layout.addWidget(inner, 1)
        history_collapsed = QSettings(_ORG, _APP).value(
            "historyCollapsed", False, type=bool
        )
        self._set_history_collapsed(history_collapsed, persist=False)

        self._sidebar_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._sidebar_splitter.setObjectName("MainSidebarSplitter")
        self._sidebar_splitter.setChildrenCollapsible(True)
        self._sidebar_splitter.addWidget(sidebar)
        self._sidebar_splitter.addWidget(self._tabs)
        self._sidebar_splitter.setCollapsible(0, True)
        self._sidebar_splitter.setCollapsible(1, False)
        self._sidebar_splitter.setStretchFactor(0, 0)
        self._sidebar_splitter.setStretchFactor(1, 1)
        saved_width = QSettings(_ORG, _APP).value("sidebarWidth", 330, type=int)
        saved_width = min(max(int(saved_width), 240), 460)
        self._sidebar_width = saved_width
        self._sidebar_splitter.setSizes([saved_width, max(500, self.width() - saved_width)])
        self._sidebar_splitter.splitterMoved.connect(self._remember_sidebar_width)
        self.setCentralWidget(self._sidebar_splitter)

        toolbar = QToolBar("Layout", self)
        toolbar.setObjectName("LayoutToolbar")
        toolbar.setMovable(False)
        self._sidebar_toggle_action = QAction("Hide sidebar", self)
        self._sidebar_toggle_action.setToolTip("Show or hide the shared instruments and history sidebar.")
        self._sidebar_toggle_action.triggered.connect(self._toggle_sidebar)
        toolbar.addAction(self._sidebar_toggle_action)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

    def _toggle_history(self) -> None:
        self._set_history_collapsed(not getattr(self, "_history_collapsed", False))

    def _set_history_collapsed(self, collapsed: bool, *, persist: bool = True) -> None:
        collapsed = bool(collapsed)
        if collapsed:
            sizes = self._sidebar_content_splitter.sizes()
            if len(sizes) > 1 and sizes[1] > 40:
                self._history_height = int(sizes[1])
            self._history_panel.hide()
            compact_height = self._history_toggle.sizeHint().height() + 4
            self._history_container.setMaximumHeight(compact_height)
            self._history_toggle.setText("▶ Experiment history")
        else:
            self._history_container.setMaximumHeight(16777215)
            self._history_panel.show()
            total = max(sum(self._sidebar_content_splitter.sizes()), 520)
            history_height = min(max(int(getattr(self, "_history_height", 260)), 140), total // 2)
            self._sidebar_content_splitter.setSizes([total - history_height, history_height])
            self._history_toggle.setText("▼ Experiment history")
        self._history_collapsed = collapsed
        if persist:
            QSettings(_ORG, _APP).setValue("historyCollapsed", collapsed)

    def _remember_sidebar_width(self, position: int, _index: int) -> None:
        if position > 16:
            self._sidebar_width = max(240, int(position))

    def _toggle_sidebar(self) -> None:
        if not hasattr(self, "_sidebar_splitter"):
            return
        sizes = self._sidebar_splitter.sizes()
        collapsed = not sizes or sizes[0] <= 16
        if collapsed:
            width = min(max(int(getattr(self, "_sidebar_width", 330)), 240), 460)
            self._sidebar_splitter.setSizes([width, max(500, self.width() - width)])
            self._sidebar_hide.setVisible(True)
            self._sidebar_toggle_action.setText("Hide sidebar")
        else:
            self._sidebar_width = max(240, int(sizes[0]))
            self._sidebar_splitter.setSizes([0, max(500, sum(sizes))])
            self._sidebar_toggle_action.setText("Show sidebar")

    def _save_sidebar_width(self) -> None:
        if hasattr(self, "_sidebar_splitter"):
            sizes = self._sidebar_splitter.sizes()
            if sizes and sizes[0] > 16:
                self._sidebar_width = max(240, int(sizes[0]))
        QSettings(_ORG, _APP).setValue("sidebarWidth", int(getattr(self, "_sidebar_width", 330)))

    def _history_type_for_active_tab(self, _index: int) -> None:
        active = self._active_tab_id()
        mapping = {
            "dual_gate": "dual_gate_sweep", "mega_sweep": "gate_map_2d",
            "power_sweep": "motion_sweep", "mcd": "mcd_aps100",
            "mcd2100": "mcd_attodry2100", "bfp": self._bfp_history_type(),
        }
        value = mapping.get(active)
        if value:
            self._history_type.setCurrentText(value)

    def _bfp_history_type(self) -> str:
        try:
            return {0: "bfp_acquisition", 1: "bfp_binned_rc", 2: "bfp_full_sensor_rc"}.get(self._bfp._tabs.currentIndex(), "bfp_acquisition")
        except Exception:
            return "bfp_acquisition"

    def _history_type_changed(self, value: str) -> None:
        if value.startswith("bfp_"):
            index = {"bfp_acquisition": 0, "bfp_binned_rc": 1, "bfp_full_sensor_rc": 2}.get(value)
            if index is not None:
                self._bfp._tabs.setCurrentIndex(index)
        self._refresh_history()

    def _refresh_history(self) -> None:
        device = self._sample_id_binder.value.strip()
        self._history_device.setText(f"Device: {device}")
        self._history_list.clear()
        self._history_preview.clear()
        self._history_load.setEnabled(False)
        self._history_preview_btn.setEnabled(False)
        if not device:
            return
        rows = self._history.query(device, self._history_type.currentText())
        for row in rows:
            item = QListWidgetItem(
                f"{row['started_at']} · {row['status']} · {row['experiment_id'][:12]}"
            )
            item.setData(Qt.ItemDataRole.UserRole, row)
            self._history_list.addItem(item)
        self._history_preview_btn.setEnabled(self._history_list.count() > 0)

    def _history_selection_changed(self, _row: int) -> None:
        self._history_preview.clear()
        self._history_load.setEnabled(False)
        self._history_preview_btn.setEnabled(self._history_list.currentItem() is not None)

    def _preview_history(self, _row: int) -> None:
        item = self._history_list.currentItem()
        if item is None:
            return
        row = item.data(Qt.ItemDataRole.UserRole) or {}
        path = Path(row.get("metadata_path", ""))
        if not path.exists():
            matches = list(cfg.base_out.rglob(path.name)) if path.name else []
            path = matches[0] if matches else path
        # History stores a portable relative path; resolve it only for local UI preview.
        metadata = {}
        if path.exists():
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        self._history_preview.setPlainText(json.dumps(metadata.get("settings", {}), indent=2, sort_keys=True))
        self._history_load.setEnabled(bool(metadata))

    def _preview_selected_history(self) -> None:
        self._preview_history(self._history_list.currentRow())

    def _load_history_settings(self) -> None:
        item = self._history_list.currentItem()
        if item is None:
            return
        row = item.data(Qt.ItemDataRole.UserRole) or {}
        path = Path(row.get("metadata_path", ""))
        if not path.exists():
            matches = list(cfg.base_out.rglob(path.name)) if path.name else []
            path = matches[0] if matches else path
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
            settings = metadata.get("settings", {})
            loadable = settings.get("loadable", {}) if isinstance(settings, dict) else {}
            current = self._tabs.currentWidget()
            adapter = self._history_adapters().get(self._history_type.currentText())
            if adapter is None or not callable(getattr(current, adapter, None)):
                self._status.showMessage("No compatible settings adapter for this tab", 6000)
                return
            report = getattr(current, adapter)(loadable)
            self._status.showMessage(f"Loaded compatible settings; skipped: {report.get('skipped', [])}", 8000)
        except Exception as exc:
            self._status.showMessage(f"History settings could not be loaded: {exc}", 8000)

    def _history_adapters(self) -> dict[str, str]:
        return {
            "dual_gate_sweep": "apply_saved_experiment_settings",
            "gate_map_2d": "apply_saved_experiment_settings",
            "motion_sweep": "apply_saved_experiment_settings",
            "mcd_aps100": "apply_saved_experiment_settings",
            "mcd_attodry2100": "apply_saved_experiment_settings",
            "bfp_acquisition": "apply_saved_experiment_settings",
            "bfp_binned_rc": "apply_saved_experiment_settings",
            "bfp_full_sensor_rc": "apply_saved_experiment_settings",
        }

    def _capture_session(self) -> dict:
        panels = {
            str(key): value
            for key, value in cfg.session.panels.items()
            if isinstance(value, dict)
        }
        for key, panel in self._session_panels.items():
            capture = getattr(panel, "capture_session_state", None)
            if capture is None:
                continue
            try:
                value = capture()
            except Exception:
                continue
            if isinstance(value, dict):
                panels[key] = value
        return {
            "schema_version": 2,
            "active_tab": self._active_tab_id(),
            "sample_id": (
                self._sample_id_binder.value
                if hasattr(self, "_sample_id_binder")
                else cfg.session.sample_id
            ),
            "panels": panels,
        }

    def _restore_session(self) -> None:
        panels = cfg.session.panels
        if isinstance(panels, dict):
            for key, panel in self._session_panels.items():
                restore = getattr(panel, "restore_session_state", None)
                state = panels.get(key)
                if restore is None or not isinstance(state, dict):
                    continue
                try:
                    restore(state)
                except Exception as exc:
                    self._status.showMessage(
                        f"Some saved {key.replace('_', ' ')} settings were skipped: {exc}",
                        8000,
                    )
        wanted = cfg.session.active_tab
        for widget, tab_id in self._tab_ids.items():
            if tab_id == wanted:
                self._tabs.setCurrentWidget(widget)
                break

    def _poll_session_changes(self) -> None:
        current = self._capture_session()
        if current != self._last_observed_session:
            self._last_observed_session = current
            self._session_save_timer.start()

    def _schedule_session_observation(self, *_args) -> None:
        self._session_observe_timer.start()

    def _persist_session(self) -> None:
        session = self._capture_session()
        cfg.session.schema_version = int(session["schema_version"])
        cfg.session.active_tab = str(session["active_tab"])
        cfg.session.sample_id = str(session["sample_id"])
        cfg.session.panels = session["panels"]
        self._last_observed_session = session
        try:
            cfg.save()
        except Exception as exc:
            self._status.showMessage(f"Could not save settings: {exc}", 8000)

    def _available_geometry_for_window(self) -> Optional[QRect]:
        app = QApplication.instance()
        if app is None:
            return None
        screen = QApplication.screenAt(self.frameGeometry().center())
        if screen is None:
            screen = self.screen() or app.primaryScreen()
        return QRect(screen.availableGeometry()) if screen is not None else None

    def _clamp_to_available_screen(self) -> None:
        if self.isMaximized() or self.isFullScreen():
            return
        available = self._available_geometry_for_window()
        if available is None:
            return
        bounded = _clamp_window_rect(self.geometry(), available)
        if bounded != self.geometry():
            self.setGeometry(bounded)

    def _restore_geometry(self):
        s = QSettings(_ORG, _APP)
        geom = s.value("geometry")
        state = s.value("windowState")
        if geom is not None:
            self.restoreGeometry(geom)
        if state is not None:
            self.restoreState(state)
        self._clamp_to_available_screen()
        QTimer.singleShot(0, self._clamp_to_available_screen)

    def _save_geometry(self):
        s = QSettings(_ORG, _APP)
        self._save_sidebar_width()
        s.setValue("geometry",    self.saveGeometry())
        s.setValue("windowState", self.saveState())

    # ── close ─────────────────────────────────────────────────────────────────

    @Slot(bool)
    def _on_mcd_run_state_changed(self, running: bool) -> None:
        """Compatibility entry point for the existing APS100 MCD panel."""
        self._on_mcd_workflow_state_changed(self._mcd, running)

    def _on_mcd_workflow_state_changed(self, source, running: bool) -> None:
        """Reserve shared instruments for exactly one MCD workflow."""
        if running:
            if self._active_mcd_panel not in (None, source):
                return
            self._active_mcd_panel = source
        elif self._active_mcd_panel is source:
            self._active_mcd_panel = None
        elif self._active_mcd_panel is not None:
            return

        active = self._active_mcd_panel
        self._inst_panel.setEnabled(not running)
        for index in range(self._tabs.count()):
            widget = self._tabs.widget(index)
            self._tabs.setTabEnabled(index, active is None or widget is active)
        for panel in (self._mcd, self._mcd2100):
            setter = getattr(panel, "set_externally_busy", None)
            if callable(setter):
                setter(active is not None and panel is not active)
        if active is not None:
            self._tabs.setCurrentWidget(active)
            label = "MCD 2100" if active is self._mcd2100 else "MCD 1000"
            self._status.showMessage(f"{label} running — other instrument controls locked")
        else:
            self._status.showMessage("MCD finished — instrument controls unlocked")

    def closeEvent(self, event: QCloseEvent) -> None:
        try:
            if not self._mcd2100.shutdown():
                QMessageBox.critical(
                    self,
                    "MCD 2100 acquisition is still stopping",
                    "The application will remain open until controller and acquisition cleanup finish.",
                )
                event.ignore()
                return
        except Exception as exc:
            QMessageBox.critical(self, "Unable to stop MCD 2100 safely", str(exc))
            event.ignore()
            return
        try:
            if not self._mcd.shutdown():
                QMessageBox.critical(
                    self,
                    "MCD acquisition is still stopping",
                    "The acquisition worker did not stop within 30 seconds. "
                    "The application will remain open so the APS100 connection is not abandoned.",
                )
                event.ignore()
                return
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Unable to stop MCD safely",
                f"The application will remain open.\n\n{exc}",
            )
            event.ignore()
            return
        try:
            cooling = self._lf6.andor_disconnect_safety_snapshot()
        except Exception as exc:
            answer = QMessageBox.question(
                self,
                "Andor detector temperature is unknown",
                "The Andor detector temperature could not be verified before exit:\n\n"
                f"{exc}\n\nExit and close the camera connection anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        else:
            if cooling:
                temperature = float(cooling.get("temperature_c", -273.15))
                threshold = float(cfg.lf6.andor_safe_disconnect_temperature_c)
                cooler_on = bool(cooling.get("cooler_on", False))
                if cooler_on or temperature < threshold:
                    answer = QMessageBox.question(
                        self,
                        "Andor detector is cold",
                        f"The detector is {temperature:.1f} °C and the safe disconnect "
                        f"temperature is {threshold:.1f} °C.\n\n"
                        "Cancel exit and use 'Warm up + disconnect' in the "
                        "Instruments tab. Exit anyway only for an emergency.",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if answer != QMessageBox.StandardButton.Yes:
                        event.ignore()
                        return
        app = QApplication.instance()
        if app is not None and hasattr(self, "_session_change_watcher"):
            app.removeEventFilter(self._session_change_watcher)
        self._session_observe_timer.stop()
        self._session_poll_timer.stop()
        self._session_save_timer.stop()
        self._persist_session()
        self._save_geometry()
        if not self._magnet2100.shutdown():
            QMessageBox.critical(
                self,
                "Unable to close attoDRY2100 safely",
                "The owner thread or socket is still active; the application will remain open.",
            )
            event.ignore()
            return
        # Shut controllers down gracefully
        for ctrl in (
            self._magnet,
            self._lf6,
            self._smu,
            self._rot,
            self._stg,
            self._pm,
        ):
            try:
                ctrl.shutdown()
            except Exception:
                pass
        super().closeEvent(event)
