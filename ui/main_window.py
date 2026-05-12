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

from PySide6.QtCore import Qt, QSettings
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


_ORG  = "SpectralSweep"
_APP  = "SpectralSweep"


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
        if app and not app.styleSheet():
            app.setStyleSheet(_STYLESHEET)
            base_font = QFont("Segoe UI", 9)
            app.setFont(base_font)

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

        # ── restore geometry ──────────────────────────────────────────────────
        self._restore_geometry()

    # ── geometry persistence ──────────────────────────────────────────────────

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
        self._save_geometry()
        # Shut controllers down gracefully
        for ctrl in (self._lf6, self._smu, self._rot, self._stg, self._pm):
            try:
                ctrl.shutdown()
            except Exception:
                pass
        super().closeEvent(event)
