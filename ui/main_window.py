# ui/main_window.py
# ──────────────────────────────────────────────────────────────────────────────
# Top-level application window.
#
# Layout:
#   Left dock  : InstrumentPanel (connection controls, always visible)
#   Right tabs : Presets | MegaSweep | BFP | Spectrum | Settings
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
from PySide6.QtGui  import QCloseEvent
from PySide6.QtWidgets import (
    QMainWindow, QDockWidget, QTabWidget, QWidget, QStatusBar,
)

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
from ui.bfp_panel        import BFPPanel


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
        self._smu.connected.connect(lambda: self._status.showMessage("SMU connected"))
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

        # Presets
        self._presets = PresetsPanel(
            lf6_ctrl=self._lf6,
            smu_ctrl=self._smu,
            rotation_ctrl=self._rot,
            stage_ctrl=self._stg,
        )
        self._tabs.addTab(self._presets, "Presets")

        # MegaSweep
        self._mega = MegaSweepPanel(
            smu_ctrl=self._smu,
            lf6_ctrl=self._lf6,
        )
        self._tabs.addTab(self._mega, "MegaSweep")

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
