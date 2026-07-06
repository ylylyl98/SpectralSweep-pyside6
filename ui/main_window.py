# ui/main_window.py
# ──────────────────────────────────────────────────────────────────────────────
# Top-level application window.
#
# Layout:
#   Left dock  : InstrumentPanel (connection controls, always visible)
#   Right tabs : Dual Gate | 2D Sweep | BFP | Spectrum | Settings
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

from PySide6.QtCore import Qt, QSettings, QTimer
from PySide6.QtGui  import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QMainWindow, QDockWidget, QTabWidget, QWidget, QStatusBar, QApplication,
)

# ── Global stylesheet ──────────────────────────────────────────────────────────
_STYLESHEET = """
/* Buttons */
QPushButton {
    padding: 4px 12px;
    min-height: 22px;
    border: 1px solid #b8b8b8;
    border-radius: 4px;
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 #f9f9f9, stop:1 #e9e9e9);
}
QPushButton:hover {
    border-color: #9090a8;
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 #ffffff, stop:1 #f0f0f0);
}
QPushButton:pressed {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 #dcdcdc, stop:1 #e8e8e8);
}
QPushButton:disabled {
    color: #aaaaaa;
    border-color: #d4d4d4;
    background: #f2f2f2;
}

/* GroupBox */
QGroupBox {
    font-weight: 600;
    border: 1px solid #cccccc;
    border-radius: 5px;
    margin-top: 10px;
    padding-top: 2px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 8px;
    top: -1px;
    padding: 0 4px;
    color: #3a3a3a;
    background: transparent;
}

/* Tab bar */
QTabBar::tab {
    padding: 5px 16px;
    border: 1px solid #c0c0c0;
    border-bottom: none;
    border-radius: 4px 4px 0 0;
    background: #e8e8e8;
    color: #555555;
    min-width: 72px;
}
QTabBar::tab:selected {
    background: palette(window);
    color: #111111;
    font-weight: 600;
    border-bottom: 1px solid palette(window);
}
QTabBar::tab:hover:!selected {
    background: #f0f0f0;
}

/* Table header */
QHeaderView::section {
    background: #f0f0f0;
    border: none;
    border-right: 1px solid #d4d4d4;
    border-bottom: 1px solid #d4d4d4;
    padding: 3px 6px;
    font-weight: 600;
    color: #333333;
}
QHeaderView::section:first {
    border-left: none;
}

/* Progress bar */
QProgressBar {
    border: 1px solid #bbbbbb;
    border-radius: 4px;
    text-align: center;
    background: #eeeeee;
    min-height: 16px;
    font-size: 11px;
}
QProgressBar::chunk {
    background: #5a8fc4;
    border-radius: 3px;
}

/* Status bar */
QStatusBar {
    border-top: 1px solid #cccccc;
    font-size: 11px;
    color: #505050;
}

/* Dock widget title */
QDockWidget::title {
    font-weight: 600;
    padding: 4px 6px;
    background: #ececec;
    border-bottom: 1px solid #c8c8c8;
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


class MainWindow(QMainWindow):
    """
    Application shell.

    Creates controllers, builds panels, wires signals.
    Geometry / dock state persisted via QSettings.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("SpectralSweep — PySide6")
        self.resize(1400, 900)

        # Apply global stylesheet once (on the QApplication so all windows share it)
        app = QApplication.instance()
        if app:
            if not app.styleSheet():
                app.setStyleSheet(_STYLESHEET)
            app_font = QFont("Segoe UI", _valid_font_size(cfg.font_size_pt))
            app.setFont(app_font)

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

        # Power Sweep
        self._power_sweep = PowerSweepPanel(
            lf6_ctrl=self._lf6,
            stage_ctrl=self._stg,
            pm_ctrl=self._pm,
            smu_ctrl=self._smu,
        )
        self._tabs.addTab(self._power_sweep, "Power Sweep")

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
            "schema_version": 1,
            "active_tab": self._active_tab_id(),
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
        cfg.session.panels = session["panels"]
        self._last_observed_session = session
        try:
            cfg.save()
        except Exception as exc:
            self._status.showMessage(f"Could not save settings: {exc}", 8000)

    def _restore_geometry(self):
        s = QSettings(_ORG, _APP)
        geom = s.value("geometry")
        state = s.value("windowState")
        if geom is not None:
            self.restoreGeometry(geom)
        if state is not None:
            self.restoreState(state)

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
