# ui/main_window.py
# ──────────────────────────────────────────────────────────────────────────────
# Top-level application window.
#
# Layout:
#   Left dock  : InstrumentPanel (connection controls, always visible)
#   Right tabs : Dual Gate | 2D Sweep | Motion Sweep | BFP | Spectrum | Settings
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
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QRect, QSettings, QTimer, QObject
from PySide6.QtGui  import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QMainWindow, QDockWidget, QTabWidget, QWidget, QStatusBar, QApplication,
)

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

from ui.instrument_panel import InstrumentPanel
from ui.settings_panel   import SettingsPanel
from ui.spectrum_panel   import SpectrumPanel
from ui.presets_panel    import PresetsPanel
from ui.megasweep_panel  import MegaSweepPanel
from ui.power_sweep_panel import PowerSweepPanel
from ui.bfp_panel_integrated import BFPPanel
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


class MainWindow(QMainWindow):
    """
    Application shell.

    Creates controllers, builds panels, wires signals.
    Geometry / dock state persisted via QSettings.
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

        # ── status bar ────────────────────────────────────────────────────────
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Ready")

        self._lf6.connected.connect(lambda _: self._status.showMessage("LF6 connected"))
        self._lf6.disconnected.connect(lambda: self._status.showMessage("LF6 disconnected"))
        self._lf6.error.connect(lambda m: self._status.showMessage(f"LF6 error: {m[:80]}"))
        self._smu.connected.connect(lambda *_: self._status.showMessage("SMU connected"))
        self._smu.disconnected.connect(lambda: self._status.showMessage("SMU disconnected"))

        # ── instrument panel dock (left) ──────────────────────────────────────
        self._inst_panel = InstrumentPanel(
            lf6_ctrl=self._lf6,
            smu_ctrl=self._smu,
            rotation_ctrl=self._rot,
            stage_ctrl=self._stg,
            pm_ctrl=self._pm,
        )

        inst_dock = QDockWidget("Instruments", self)
        inst_dock.setObjectName("InstrumentDock")
        inst_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        inst_dock.setWidget(self._inst_panel)
        inst_dock.setFeatures(
            QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable
        )
        self.addDockWidget(Qt.LeftDockWidgetArea, inst_dock)

        # ── main tabs ─────────────────────────────────────────────────────────
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self.setCentralWidget(self._tabs)

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
            "bfp": self._bfp,
            "spectrum": self._spectrum,
            "settings": self._settings,
        }
        self._tab_ids = {
            self._presets: "dual_gate",
            self._mega: "mega_sweep",
            self._power_sweep: "power_sweep",
            self._bfp: "bfp",
            self._spectrum: "spectrum",
            self._settings: "settings",
        }

        # ── restore geometry ──────────────────────────────────────────────────
        self._restore_geometry()
        self._restore_session()
        self._initialize_shared_sample_id()

        # A short polling debounce also catches dynamic table-cell widgets.
        self._last_observed_session = self._capture_session()
        self._session_poll_timer = QTimer(self)
        self._session_poll_timer.setInterval(250)
        self._session_poll_timer.timeout.connect(self._poll_session_changes)
        self._session_save_timer = QTimer(self)
        self._session_save_timer.setSingleShot(True)
        self._session_save_timer.setInterval(500)
        self._session_save_timer.timeout.connect(self._persist_session)
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
        s.setValue("geometry",    self.saveGeometry())
        s.setValue("windowState", self.saveState())

    # ── close ─────────────────────────────────────────────────────────────────

    def closeEvent(self, event: QCloseEvent) -> None:
        self._session_poll_timer.stop()
        self._session_save_timer.stop()
        self._persist_session()
        self._save_geometry()
        # Shut controllers down gracefully
        for ctrl in (self._lf6, self._smu, self._rot, self._stg, self._pm):
            try:
                ctrl.shutdown()
            except Exception:
                pass
        super().closeEvent(event)
