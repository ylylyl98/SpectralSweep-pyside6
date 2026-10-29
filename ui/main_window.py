# ui/main_window.py
# ──────────────────────────────────────────────────────────────────────────────
# Top-level application window.
#
# Layout:
#   Left splitter pane : shared InstrumentPanel + per-sample settings
#   Main tabs         : Dual Gate | 2D Sweep | Motion Sweep | MCD | BFP | ...
#
# Controllers are created once here and injected into all panels.
# Reload of UI modules is possible without dropping controller connections.
#
# Rules:
#   - All instrument state lives in controllers only.
#   - Only registered pages are replaced transactionally while idle.
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import sys
import json
from copy import deepcopy
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QRect, QSettings, QTimer, QObject, QEvent, Slot, Signal, QUrl
from PySide6.QtGui  import QCloseEvent, QFont, QAction, QDesktopServices
from PySide6.QtWidgets import (
    QMainWindow, QTabWidget, QWidget, QStatusBar, QApplication,
    QMessageBox, QComboBox, QListWidget, QPushButton, QPlainTextEdit, QLabel,
    QLineEdit, QToolButton, QDialogButtonBox,
    QVBoxLayout, QHBoxLayout, QListWidgetItem, QSplitter, QDialog,
    QInputDialog,
)
from app.experiment_metadata import ExperimentHistory, ExperimentMetadataService
from app.ntfy_notifications import NtfyNotifier
from app.sample_settings import SampleSettingsStore
from ui.history_loader import HistoryTaskRunner, load_preview, load_settings

from ui.application_style import application_style


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from controllers.lf6_controller      import LF6Controller
from controllers.smu_controller      import SMUController
from controllers.rotation_controller import RotationController
from controllers.stage_controller    import StageController
from controllers.esp32_stage_controller import ESP32StageController
from controllers.pm100d_controller   import PM100DController
from controllers.mff101_controller import MFF101Controller
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

    valueChanged = Signal(str)

    def __init__(self, edits, initial: str = "", parent=None, *, commit_on_edit: bool = False):
        super().__init__(parent)
        self._edits = list(edits)
        self._value = ""
        self._updating = False
        self._commit_on_edit = bool(commit_on_edit)
        for edit in self._edits:
            if self._commit_on_edit:
                edit.textChanged.connect(self._on_deferred_text_changed)
            else:
                edit.textChanged.connect(self._on_text_changed)
        self.set_value(initial)

    @property
    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        value = str(value)
        changed = value != self._value
        self._value = value
        self._updating = True
        try:
            for edit in self._edits:
                if edit.text() != value:
                    edit.setText(value)
        finally:
            self._updating = False
        if changed:
            self.valueChanged.emit(value)

    def _on_text_changed(self, value: str) -> None:
        if self._updating:
            return
        self.set_value(value)

    def _on_deferred_text_changed(self, _value: str) -> None:
        # Keep the in-progress edit local until the owner explicitly commits
        # the Sample ID (Enter or a selector action).
        return

    def commit(self, value: Optional[str] = None) -> str:
        if value is None:
            value = next((edit.text() for edit in self._edits if edit.hasFocus()), self._value)
        value = str(value).strip()
        self.set_value(value)
        return value

    def replace_edit(self, old, new) -> None:
        """Replace a view without committing its unfinished Sample ID text."""
        index = self._edits.index(old)
        callback = self._on_deferred_text_changed if self._commit_on_edit else self._on_text_changed
        old.textChanged.disconnect(callback)
        self._edits[index] = new
        new.textChanged.connect(callback)


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

    notification_configured = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None, notifier=None, watch_styles=False, *, ui_mode="widgets"):
        super().__init__(parent)
        self._ui_mode = ui_mode
        self._ntfy_notifier = notifier if notifier is not None else NtfyNotifier()
        self.setWindowTitle("SpectralSweep — PySide6")

        # Apply global stylesheet once (on the QApplication so all windows share it)
        app = QApplication.instance()
        if app:
            app.setStyle("Fusion")
            if ui_mode == "quick":
                app.setProperty("spectralQuick", True)
                from ui.quick_style import apply_quick_style
                apply_quick_style(app, app.property("quickThemeMode") or "system")
            self._application_style = application_style(app)
            if not app.styleSheet():
                self._application_style.reload()
            self._application_style.set_watching(watch_styles)
            app_font = QFont("Segoe UI", 9 if ui_mode == "quick" else _valid_font_size(cfg.font_size_pt))
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
        self._imaging_stg = ESP32StageController(parent=self)
        self._pm   = PM100DController(parent=self)
        self._mff101 = MFF101Controller(parent=self)
        self._lf6.set_flipper_controller(self._mff101)
        self._magnet = MagnetController(parent=self)

        # ── status bar ────────────────────────────────────────────────────────
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Ready")
        self._initialize_style_actions()

        self._lf6.connected.connect(
            lambda _: self._status.showMessage("Spectrometer connected")
        )
        self._lf6.disconnected.connect(
            lambda: self._status.showMessage("Spectrometer disconnected")
        )
        self._lf6.error.connect(
            lambda m: self._status.showMessage(f"Spectrometer error: {m[:80]}")
        )
        self._lf6.capture_stalled.connect(self._on_capture_stalled)
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

        # ── shared instrument panel (left sidebar, assembled with sample settings) ──
        self._inst_panel = InstrumentPanel(
            lf6_ctrl=self._lf6,
            smu_ctrl=self._smu,
            rotation_ctrl=self._rot,
            stage_ctrl=self._stg,
            imaging_stage_ctrl=self._imaging_stg,
            pm_ctrl=self._pm,
            mff101_ctrl=self._mff101,
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
        self._power_sweep.busy_changed.connect(self._on_power_sweep_state_changed)
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
        self._power_sweep_running = False

        # BFP
        self._bfp = BFPPanel(lf6_ctrl=self._lf6)
        self._tabs.addTab(self._bfp, "BFP")

        # Spectrum
        self._spectrum = SpectrumPanel(lf6_ctrl=self._lf6)
        self._spectrum.set_sidebar_snapshot_provider(
            self._inst_panel.capture_spectrum_readbacks
        )
        self._tabs.addTab(self._spectrum, "Spectrum")
        self._calibration = self._spectrum.create_calibration_page()
        self._tabs.addTab(self._calibration, "Calibration")
        # The editor remains available for development, outside normal navigation.
        self._tabs.setTabVisible(self._tabs.indexOf(self._calibration), False)

        # Settings
        self._settings = SettingsPanel(lf6_ctrl=self._lf6)
        self._bind_settings_signals(self._settings)
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
            self._calibration: "calibration",
            self._settings: "settings",
        }

        # ── restore geometry ──────────────────────────────────────────────────
        self._initialize_shared_sample_id()
        self._initialize_history_ui()
        self._build_sidebar_splitter()
        legacy_state = {
            "schema_version": cfg.session.schema_version,
            "active_tab": cfg.session.active_tab,
            "sample_id": cfg.session.sample_id,
            "panels": deepcopy(cfg.session.panels),
        }
        current_id = self._sample_id_binder.value.strip()
        if current_id:
            self._sample_store.migrate_legacy(current_id, legacy_state)
        self._restore_session()
        # A saved active Calibration tab must not reveal an unfinished page.
        if self._tabs.currentWidget() is self._calibration:
            self._tabs.setCurrentWidget(self._spectrum)

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
        self._initialize_source_tracking()
        self._quick_workbench = None
        if ui_mode == "quick":
            from ui.quick_workbench import QuickWorkbench
            self._quick_workbench = QuickWorkbench(self)
        # Restore QMainWindow layout/state only after the Quick workbench has
        # installed its final central widget. Restoring/maximizing the old
        # central layout before reparenting it can crash Qt when first shown.
        self._restore_geometry()

    def _initialize_style_actions(self) -> None:
        # Keep the Python wrappers alive too (PySide6 menu-bar ownership).
        self._view_menu_bar = self.menuBar()
        self._view_menu = self._view_menu_bar.addMenu("&View")
        self._view_menu_action = self._view_menu.menuAction()
        menu = self._view_menu
        reload_action = menu.addAction("Reload styles")
        reload_action.setObjectName("ReloadApplicationStyles")
        reload_action.setShortcut("Ctrl+Alt+R")
        reload_action.setStatusTip("Apply the current application.qss without restarting")
        reload_action.triggered.connect(self._application_style.reload)
        self._application_style.reloaded.connect(self._styles_reloaded)
        self._application_style.failed.connect(self._styles_failed)
        if self._application_style.last_error:
            self._styles_failed(self._application_style.last_error)
        page_reload = menu.addAction("Reload Spectrum page")
        page_reload.setObjectName("ReloadSpectrumPage")
        page_reload.setStatusTip("Load Spectrum UI changes while idle; keep instrument connections and spectra")
        page_reload.triggered.connect(self._reload_spectrum_page)
        current_reload = menu.addAction("Refresh current interface")
        current_reload.setObjectName("ReloadCurrentPage")
        current_reload.setShortcut("Ctrl+Alt+U")
        current_reload.setStatusTip(
            "Refresh Dual Gate, Spectrum or Settings Python layouts, tables and forms while idle; "
            "preserve state and instrument connections. QML, global shell, controllers and drivers require restart.")
        current_reload.triggered.connect(self._reload_current_page)
        dual_reload = menu.addAction("Reload Dual Gate page")
        dual_reload.setObjectName("ReloadDualGatePage")
        dual_reload.triggered.connect(lambda: self._reload_registered_page("dual_gate"))
        settings_reload = menu.addAction("Reload Settings page")
        settings_reload.setObjectName("ReloadSettingsPage")
        settings_reload.triggered.connect(lambda: self._reload_registered_page("settings"))
        source_status = menu.addAction("Code update status…")
        source_status.setObjectName("SourceChangeStatus")
        source_status.triggered.connect(self._show_source_changes)
        self._show_calibration_action = menu.addAction("Show experimental Calibration page")
        self._show_calibration_action.setObjectName("ShowExperimentalCalibration")
        self._show_calibration_action.setCheckable(True)
        self._show_calibration_action.toggled.connect(self._set_calibration_visible)
        preview_action = menu.addAction("Open Spectrum preview…")
        preview_action.setObjectName("OpenSpectrumPreview")
        preview_action.setStatusTip("Open a separate window with simulated data and isolated settings")
        preview_action.triggered.connect(self._open_spectrum_preview)

    @Slot()
    def _reload_spectrum_page(self) -> None:
        self._reload_registered_page("spectrum")

    @Slot()
    def _reload_current_page(self) -> None:
        self._reload_registered_page(self._active_tab_id())

    def _reload_registered_page(self, page_id: str) -> None:
        from ui.page_reload import PAGE_RELOAD_REGISTRY, reload_page
        spec = PAGE_RELOAD_REGISTRY.get(page_id)
        if spec is None:
            self._status.showMessage("This page needs an application restart. See View > Code update status.", 10000)
            return
        title = spec.title
        if getattr(self, "_page_reload_in_progress", False):
            return
        if self._acquisition_active() or any(
            bool(getattr(panel, "_metadata_finalizing", False))
            for panel in self._session_panels.values()
        ):
            self._status.showMessage(f"Wait for acquisition and saving to finish before reloading {title}", 10000)
            return
        tracker = getattr(self, "_source_changes", None)
        snapshot = tracker.snapshot(tracker.groups.get(page_id, ())) if tracker else {}
        self._page_reload_in_progress = True
        try:
            new = reload_page(self, page_id)
        except Exception as exc:
            self._status.showMessage(f"{title} unchanged — reload failed: {type(exc).__name__}: {exc}", 15000)
        else:
            ui_error = getattr(new, "_page_reload_ui_error", None)
            if ui_error:
                self._status.showMessage(
                    f"{title} page rebuilt, but Quick UI refresh failed: {ui_error}. "
                    "Layout changes remain unconfirmed.", 15000)
                return
            if tracker:
                tracker.mark_page_applied(page_id, snapshot)
            if tracker and any(item["restart"] for item in tracker.pending()):
                self._status.showMessage(
                    f"{title} page code reloaded — shared UI/layout or device changes still require restart. "
                    "See Code update status.", 12000)
            else:
                self._status.showMessage(f"{title} page reloaded — connections and data preserved", 8000)
        finally:
            self._page_reload_in_progress = False

    def _rebind_reloaded_page(self, page_id, old, new) -> None:
        if page_id == "dual_gate" and hasattr(self, "_sample_id_binder"):
            self._sample_id_binder.replace_edit(old._sample_edit, new._sample_edit)
            connection = self._sample_edit_connections.pop(old._sample_edit, None)
            if connection is not None:
                QObject.disconnect(connection)
            edit = new._sample_edit
            self._sample_edit_connections[edit] = edit.editingFinished.connect(
                lambda edit=edit: self._commit_sample_id_edit(edit))
        elif page_id == "settings":
            connections = getattr(self, "_settings_connections", None)
            if connections is not None:
                connections.clear()
            self._bind_settings_signals(new)

    def _bind_settings_signals(self, panel) -> None:
        from ui.signal_connections import SignalConnections
        if not hasattr(self, "_settings_connections"):
            self._settings_connections = SignalConnections(self)
        connections = self._settings_connections
        connections.connect(panel.mff101_paths_changed, self._inst_panel.refresh_mff101_paths)
        connections.connect(panel.notification_configured, self._ntfy_notifier.set_url)
        connections.connect(panel.notification_configured, self.notification_configured.emit)

    def _initialize_source_tracking(self) -> None:
        if getattr(sys, "frozen", False):
            return
        from ui.page_reload import PAGE_RELOAD_REGISTRY
        from ui.refresh_manifest import QUICK_PAGE_MODULES, QUICK_SHARED_RESTART_SOURCES
        from ui.source_changes import SourceChangeTracker
        quick = getattr(self, "_ui_mode", "widgets") == "quick"
        groups = {}
        for key, spec in PAGE_RELOAD_REGISTRY.items():
            helpers = QUICK_PAGE_MODULES.get(key, ()) if quick else ()
            groups[key] = tuple(name.replace(".", "/") + ".py" for name in (*helpers, *spec.modules))
        shared = {"ui/spectrum_plot.py", "ui/andor_controls_widget.py"}
        if quick:
            shared.update(QUICK_SHARED_RESTART_SOURCES)
        self._source_changes = SourceChangeTracker(
            _PROJECT_ROOT, groups,
            shared=shared,
            style_path=self._application_style.path, parent=self)
        if self._application_style._last_source is not None:
            self._source_changes.mark_styles_applied(self._application_style._last_source)
        self._source_status = QToolButton(self)
        self._source_status.setObjectName("SourceChangeStatusButton")
        self._source_status.setAccessibleName("Code update status")
        self._source_status.clicked.connect(self._show_source_changes)
        self._status.addPermanentWidget(self._source_status)
        self._source_changes.changed.connect(self._update_source_status)
        self._update_source_status()

    def _update_source_status(self) -> None:
        pending = self._source_changes.pending()
        if any(item["restart"] for item in pending):
            can_refresh = any(item["pages"] or item["styles"] for item in pending)
            text = "Code: refresh available; restart pending" if can_refresh else "Code: restart required"
        elif pending:
            pages = any(item["pages"] for item in pending)
            styles = any(item["styles"] for item in pending)
            text = ("Code: page/style reload available" if pages and styles else
                    "Code: page reload available" if pages else "Code: style reload available")
        else:
            text = "Code: up to date"
        self._source_status.setText(text)
        self._source_status.setToolTip(self._source_changes_text())
        details = getattr(self, "_source_details", None)
        if details is not None:
            details.setPlainText(self._source_changes_text())

    def _source_changes_text(self) -> str:
        from ui.page_reload import PAGE_RELOAD_REGISTRY
        tracker = getattr(self, "_source_changes", None)
        if tracker is None:
            return "Source tracking is available when running from a source checkout."
        pending = tracker.pending()
        if not pending:
            return "No source changes pending since startup or the last successful page/style reload."
        lines = []
        for item in pending:
            if item["styles"]:
                action = "Reload styles (Ctrl+Alt+R)"
            else:
                pages = ", ".join(PAGE_RELOAD_REGISTRY[key].title for key in item["pages"])
                action = f"Refresh {pages}" if pages else "Restart application when idle"
                if item["restart"] and pages:
                    action += "; other users of this shared module still require restart"
                if item["path"] in {"launch.bat", "tools/launch_app.py"}:
                    action = "Applies on the next launch.bat start"
                elif item["path"] == "requirements.txt":
                    action = "Restart through launch.bat to check updated dependencies"
                elif item["restart"] and not pages and item["path"].startswith(("ui/qml/", "ui/quick_")):
                    action = "Restart application when idle — Qt Quick components or other page instances still use earlier code"
            lines.append(f"{item['path']}\n  {action}")
        return "\n\n".join(lines)

    @Slot()
    def _show_source_changes(self) -> None:
        tracker = getattr(self, "_source_changes", None)
        if tracker is not None:
            tracker.refresh()
        dialog = QDialog(self)
        dialog.setWindowTitle("Code update status")
        dialog.resize(720, 460)
        layout = QVBoxLayout(dialog)
        explanation = QLabel(
            "Refresh current interface (Ctrl+Alt+U) refreshes registered Dual Gate, Spectrum and Settings "
            "Python layouts, tables and forms while idle and keeps instrument connections open. "
            "Other pages using shared helpers still require restart. QML components, the global shell, "
            "controllers and drivers require an application restart.")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        details = QPlainTextEdit(self._source_changes_text())
        details.setReadOnly(True)
        layout.addWidget(details, 1)
        self._source_details = details
        refresh = QPushButton("Check again")
        refresh.clicked.connect(lambda: (tracker.refresh() if tracker else None,
                                         details.setPlainText(self._source_changes_text())))
        layout.addWidget(refresh)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        try:
            dialog.exec()
        finally:
            self._source_details = None
            dialog.deleteLater()

    @Slot(bool)
    def _set_calibration_visible(self, visible: bool) -> None:
        if not visible:
            from ui.spectrum_reload_state import spectrum_reload_block_reason
            if spectrum_reload_block_reason(self._spectrum):
                self._show_calibration_action.blockSignals(True)
                self._show_calibration_action.setChecked(True)
                self._show_calibration_action.blockSignals(False)
                self._status.showMessage("Wait for acquisition/calibration to finish before hiding Calibration", 8000)
                return
            if self._tabs.currentWidget() is self._calibration:
                self._tabs.setCurrentWidget(self._spectrum)
        self._tabs.setTabVisible(self._tabs.indexOf(self._calibration), visible)
        if visible:
            self._tabs.setCurrentWidget(self._calibration)
            self._status.showMessage("Experimental Calibration page — under development", 8000)

    @Slot(str)
    def _styles_reloaded(self, _path: str) -> None:
        tracker = getattr(self, "_source_changes", None)
        if tracker is not None and self._application_style._last_source is not None:
            tracker.mark_styles_applied(self._application_style._last_source, path=_path)
        self._status.showMessage("Styles reloaded", 4000)

    @Slot(str)
    def _styles_failed(self, reason: str) -> None:
        self._status.showMessage(f"Styles unchanged: {reason}", 10000)

    @Slot()
    def _open_spectrum_preview(self) -> None:
        from app.ui_preview import launch_preview
        try:
            launch_preview()
        except OSError as exc:
            self._status.showMessage(f"Could not open Spectrum preview: {exc}", 10000)

    # ── geometry persistence ──────────────────────────────────────────────────

    def _active_tab_id(self) -> str:
        widget = self._tabs.currentWidget()
        return self._tab_ids.get(widget, "dual_gate")

    @staticmethod
    def _saved_sample_id(session) -> str:
        """Prefer the last committed sample; legacy widget defaults are a last resort."""
        current = str(getattr(session, 'sample_id', '') or '').strip()
        if current:
            return current
        store = SampleSettingsStore({'sample_profiles': getattr(session, 'sample_profiles', {})})
        return next((key for key in store.ids() if store.load(key) is not None), '')

    def _initialize_shared_sample_id(self) -> None:
        edits_by_panel = {
            self._presets: self._presets._sample_edit,
            self._mega: self._mega._sample_edit,
            self._power_sweep: self._power_sweep._devid_edit,
            self._bfp: self._bfp._dev_edit,
            self._mcd: self._mcd._sample_id,
            self._mcd2100: self._mcd2100._sample_id,
        }
        initial = self._saved_sample_id(cfg.session)
        if not initial and cfg.session.schema_version < 2:
            # Legacy sessions kept Sample ID inside panel metadata rather than
            # on SessionConfig.  Read those harmless fields before widgets are
            # restored so migration selects the right profile.
            initial = ""
            for panel_state in cfg.session.panels.values():
                if not isinstance(panel_state, dict):
                    continue
                for candidate in (
                    panel_state.get("sample_id"),
                    (panel_state.get("metadata") or {}).get("sample_id")
                    if isinstance(panel_state.get("metadata"), dict) else None,
                    (panel_state.get("naming") or {}).get("device")
                    if isinstance(panel_state.get("naming"), dict) else None,
                ):
                    if isinstance(candidate, str) and candidate.strip():
                        initial = candidate
                        break
                if initial:
                    break
            active_edit = edits_by_panel.get(self._tabs.currentWidget())
            initial = initial or (active_edit.text() if active_edit is not None else "")
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
            commit_on_edit=True,
        )
        self._sample_edit_connections = {}
        for edit in edits_by_panel.values():
            self._sample_edit_connections[edit] = edit.editingFinished.connect(
                lambda edit=edit: self._commit_sample_id_edit(edit))

    def _commit_sample_id_edit(self, edit) -> None:
        """Commit on Enter or focus loss, never while restoring controls."""
        if not getattr(self, '_sample_restore_in_progress', False):
            self._commit_sample_selection(edit.text())

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
        self._history_filter = QLineEdit()
        self._history_filter.setPlaceholderText("Filter history")
        self._history_filter.setClearButtonEnabled(True)
        top.addWidget(self._history_filter)
        self._event_filter = QLineEdit()
        self._event_filter.setPlaceholderText("Filter events")
        self._event_filter.setClearButtonEnabled(True)
        top.addWidget(self._event_filter)
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
        self._history_open = QPushButton("Open metadata")
        self._history_open.setEnabled(False)
        self._history_open.clicked.connect(self._open_history_metadata)
        self._history_prev = QPushButton("‹")
        self._history_next = QPushButton("›")
        self._history_page_label = QLabel("Page 1")
        self._event_prev = QPushButton("Earlier events")
        self._event_next = QPushButton("Later events")
        buttons.addWidget(self._history_refresh)
        buttons.addWidget(self._history_prev)
        buttons.addWidget(self._history_next)
        buttons.addWidget(self._history_page_label)
        buttons.addWidget(self._history_preview_btn)
        buttons.addWidget(self._history_load)
        buttons.addWidget(self._history_open)
        buttons.addWidget(self._event_prev)
        buttons.addWidget(self._event_next)
        layout.addLayout(buttons)
        panel.setMinimumWidth(0)
        self._history_panel = panel
        self._history_refresh.clicked.connect(self._refresh_history)
        self._history_type.currentTextChanged.connect(self._history_type_changed)
        self._tabs.currentChanged.connect(self._history_type_for_active_tab)
        self._history_list.currentRowChanged.connect(self._history_selection_changed)
        self._history_preview_btn.clicked.connect(self._preview_selected_history)
        self._history_load.clicked.connect(self._load_history_settings)
        self._history_filter.textChanged.connect(self._history_query_changed)
        self._event_filter.textChanged.connect(self._history_event_query_changed)
        self._history_page = 0
        self._event_offset = 0
        self._history_prev.clicked.connect(lambda: self._change_history_page(-1))
        self._history_next.clicked.connect(lambda: self._change_history_page(1))
        self._event_prev.clicked.connect(lambda: self._change_event_page(-20))
        self._event_next.clicked.connect(lambda: self._change_event_page(20))
        self._refresh_history()
        self._bfp._tabs.currentChanged.connect(lambda _i: self._history_type_for_active_tab(self._tabs.currentIndex()))

        # Sample settings are the primary sidebar workflow.  Run history is
        # still available from a separate window via the button below.
        self._sample_store = SampleSettingsStore({"sample_profiles": cfg.session.sample_profiles})
        sample_panel = QWidget()
        sample_layout = QVBoxLayout(sample_panel)
        sample_layout.setContentsMargins(0, 0, 0, 0)
        sample_layout.setSpacing(4)
        self._sample_summary = QWidget()
        self._sample_summary.setObjectName("SidebarSampleSummary")
        summary_layout = QVBoxLayout(self._sample_summary)
        summary_layout.setContentsMargins(0, 0, 0, 4)
        summary_layout.setSpacing(3)
        row = QHBoxLayout()
        row.addWidget(QLabel("Sample ID"))
        self._sample_selector = QComboBox()
        self._sample_selector.setEditable(True)
        self._sample_selector.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._sample_selector.setPlaceholderText("Select or enter Sample ID")
        self._sample_selector.setAccessibleName("Current sample ID")
        self._sample_selector.setMinimumContentsLength(10)
        self._sample_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        row.addWidget(self._sample_selector, 1)
        summary_layout.addLayout(row)
        self._sample_filter = QLineEdit()
        self._sample_filter.setPlaceholderText("Search saved samples")
        self._sample_filter.setClearButtonEnabled(True)
        sample_layout.addWidget(self._sample_filter)
        actions = QHBoxLayout()
        self._sample_new_btn = QPushButton("New sample")
        self._sample_duplicate_btn = QPushButton("Duplicate current")
        self._sample_save_btn = QPushButton("Save now")
        actions.addWidget(self._sample_new_btn)
        actions.addWidget(self._sample_duplicate_btn)
        actions.addWidget(self._sample_save_btn)
        sample_layout.addLayout(actions)
        self._sample_save_status = QLabel("Not saved")
        self._sample_save_status.setWordWrap(True)
        summary_layout.addWidget(self._sample_save_status)
        self._run_history_btn = QPushButton("Open run history")
        self._run_history_btn.setToolTip("Open completed and failed acquisition records.")
        sample_layout.addWidget(self._run_history_btn)
        self._sample_settings_panel = sample_panel
        self._refresh_sample_selector()
        self._sample_selector.lineEdit().editingFinished.connect(
            lambda: self._commit_sample_id_edit(self._sample_selector.lineEdit())
        )
        self._sample_selector.activated.connect(self._sample_selector_activated)
        self._sample_filter.textChanged.connect(lambda _text: self._refresh_sample_selector())
        self._sample_new_btn.clicked.connect(lambda _checked=False: self._new_sample())
        self._sample_duplicate_btn.clicked.connect(
            lambda _checked=False: self._duplicate_current_sample()
        )
        self._sample_save_btn.clicked.connect(lambda _checked=False: self._save_current_sample())
        self._run_history_btn.clicked.connect(lambda _checked=False: self._show_run_history())

    def _save_current_sample(self) -> None:
        if self._commit_sample_selection(self._sample_selector.currentText()):
            self._persist_session()

    def _refresh_sample_selector(self) -> None:
        if not hasattr(self, "_sample_selector"):
            return
        current = self._sample_id_binder.value if hasattr(self, "_sample_id_binder") else ""
        query = self._sample_filter.text() if hasattr(self, "_sample_filter") else ""
        combo = self._sample_selector
        combo.blockSignals(True)
        try:
            combo.clear()
            ids = self._sample_store.ids(query)
            history = getattr(self, "_history", None)
            if history is not None:
                ids = list(dict.fromkeys(ids + history.sample_ids(query)))
            combo.addItems(ids)
            combo.setEditText(current)
        finally:
            combo.blockSignals(False)

    def _sample_selector_activated(self, index: int) -> None:
        combo = self._sample_selector
        value = combo.itemText(index) if isinstance(index, int) else str(index)
        self._commit_sample_selection(value)

    def _new_sample(self) -> None:
        value, accepted = QInputDialog.getText(self, "New sample", "Sample ID:")
        if accepted:
            target = self._sample_store.normalize_id(value)
            if target in self._sample_store.ids():
                self._status.showMessage("That Sample ID already exists; use Duplicate current", 5000)
                return
            self._commit_sample_selection(target)

    def _duplicate_current_sample(self, target_id: Optional[str] = None) -> None:
        source = self._sample_id_binder.value.strip()
        if target_id is None:
            value, accepted = QInputDialog.getText(
                self, "Duplicate current sample", "New Sample ID:", text=source
            )
            if not accepted:
                return
            target = str(value).strip()
        else:
            target = str(target_id).strip()
        if not target or not source:
            self._status.showMessage("Both current and new Sample IDs are required", 5000)
            return
        self._persist_session()
        if not self._sample_store.duplicate(source, target):
            self._status.showMessage("Could not duplicate: target Sample ID already exists", 5000)
            return
        cfg.session.sample_profiles = self._sample_store._profiles
        try:
            cfg.save()
        except Exception as exc:
            self._status.showMessage(f"Could not save sample settings: {exc}", 8000)
            return
        self._refresh_sample_selector()
        self._commit_sample_selection(target)

    def _show_run_history(self) -> None:
        quick = getattr(self, "_quick_workbench", None)
        if quick is not None:
            quick.showHistory()
            return
        dialog = getattr(self, "_run_history_dialog", None)
        if dialog is not None and not dialog.isHidden():
            dialog.raise_()
            dialog.activateWindow()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Experiment run history")
        dialog.resize(760, 620)
        layout = QVBoxLayout(dialog)
        layout.addWidget(self._history_panel)
        self._run_history_dialog = dialog
        dialog.finished.connect(lambda _code: setattr(self, "_run_history_dialog", None))
        dialog.show()

    def _build_sidebar_splitter(self) -> None:
        """Host instruments and sample settings in one responsive sidebar."""
        sidebar = QWidget()
        sidebar.setObjectName("SharedInstrumentHistorySidebar")
        # The splitter can still collapse this pane to zero. While expanded,
        # keep the manual actions inside the viewport instead of clipping them.
        sidebar.setMinimumWidth(300)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(4, 4, 4, 4)
        sidebar_layout.setSpacing(4)

        header = QHBoxLayout()
        title = QLabel("Instruments")
        title.setProperty("fluentRole", "sectionTitle")
        self._sidebar_hide = QPushButton("Hide")
        self._sidebar_hide.setAccessibleName("Hide instrument sidebar")
        self._sidebar_hide.setToolTip("Hide sidebar (Ctrl+Alt+B). Restore it from View or the same shortcut.")
        self._sidebar_hide.clicked.connect(self._toggle_sidebar)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self._sidebar_hide)
        sidebar_layout.addLayout(header)
        sidebar_layout.addWidget(self._sample_summary)

        inner = QSplitter(Qt.Orientation.Vertical)
        inner.setObjectName("SidebarContentSplitter")
        inner.setChildrenCollapsible(True)
        self._sidebar_content_splitter = inner
        self._sample_settings_panel.setMinimumHeight(0)

        history_container = QWidget()
        history_container.setObjectName("ExperimentHistoryContainer")
        history_layout = QVBoxLayout(history_container)
        history_layout.setContentsMargins(0, 0, 0, 0)
        history_layout.setSpacing(2)
        self._history_toggle = QPushButton()
        self._history_toggle.setToolTip("Collapse or expand saved sample settings.")
        self._history_toggle.clicked.connect(self._toggle_history)
        history_layout.addWidget(self._history_toggle)
        history_layout.addWidget(self._sample_settings_panel, 1)
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

        self._sidebar_toggle_action = QAction("Hide sidebar", self)
        self._sidebar_toggle_action.setObjectName("ToggleInstrumentSidebar")
        self._sidebar_toggle_action.setShortcut("Ctrl+Alt+B")
        self._sidebar_toggle_action.setToolTip("Show or hide instruments and sample settings.")
        self._sidebar_toggle_action.triggered.connect(self._toggle_sidebar)
        self._view_menu.addSeparator()
        self._view_menu.addAction(self._sidebar_toggle_action)

    def _toggle_history(self) -> None:
        self._set_history_collapsed(not getattr(self, "_history_collapsed", False))

    def _set_history_collapsed(self, collapsed: bool, *, persist: bool = True) -> None:
        collapsed = bool(collapsed)
        content_panel = getattr(self, "_sample_settings_panel", self._history_panel)
        if collapsed:
            sizes = self._sidebar_content_splitter.sizes()
            if len(sizes) > 1 and sizes[1] > 40:
                self._history_height = int(sizes[1])
            content_panel.hide()
            compact_height = self._history_toggle.sizeHint().height() + 4
            self._history_container.setMaximumHeight(compact_height)
            self._history_toggle.setText("▶ Sample settings")
        else:
            self._history_container.setMaximumHeight(16777215)
            content_panel.show()
            total = max(sum(self._sidebar_content_splitter.sizes()), 520)
            history_height = min(max(int(getattr(self, "_history_height", 260)), 140), total // 2)
            self._sidebar_content_splitter.setSizes([total - history_height, history_height])
            self._history_toggle.setText("▼ Sample settings")
        self._history_collapsed = collapsed
        if persist:
            QSettings(_ORG, _APP).setValue("historyCollapsed", collapsed)

    def _remember_sidebar_width(self, position: int, _index: int) -> None:
        if position > 16:
            self._sidebar_width = max(240, int(position))

    def _toggle_sidebar(self) -> None:
        if not hasattr(self, "_sidebar_splitter"):
            return
        quick = getattr(self, "_quick_workbench", None)
        if quick is not None and self.width() < 1200:
            quick.toggleSidebar()
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
        self._history_page = 0
        self._event_offset = 0
        if value.startswith("bfp_"):
            index = {"bfp_acquisition": 0, "bfp_binned_rc": 1, "bfp_full_sensor_rc": 2}.get(value)
            if index is not None:
                self._bfp._tabs.setCurrentIndex(index)
        self._refresh_history()

    def _history_tasks(self):
        if not hasattr(self, "_history_task_runner"):
            self._history_task_runner = HistoryTaskRunner(self)
        return self._history_task_runner

    def _refresh_history(self) -> None:
        if not hasattr(self, "_history_device"):
            return
        device = self._sample_id_binder.value.strip()
        if device != getattr(self, "_history_sample", None):
            self._history_sample = device
            self._history_page = 0
            self._event_offset = 0
        self._history_device.setText(f"Device: {device}")
        self._history_list.clear()
        self._history_preview.clear()
        self._history_load.setEnabled(False)
        self._history_preview_btn.setEnabled(False)
        self._history_tasks().cancel_all()
        self._history_metadata_path = None
        if hasattr(self, "_history_open"):
            self._history_open.setEnabled(False)
        for name in ("_event_prev", "_event_next", "_history_prev", "_history_next"):
            if hasattr(self, name):
                getattr(self, name).setEnabled(False)
        if not device:
            self._history_page_label.setText("Page 1")
            self._history_prev.setEnabled(False)
            self._history_next.setEnabled(False)
            return
        history = self._history
        kind, page = self._history_type.currentText(), self._history_page
        query = self._history_filter.text()
        self._history_page_label.setText("Loading…")
        self._history_tasks().submit("list", lambda cancel: history.query_page(
            device, kind, page=page, filter_text=query, cancel_event=cancel,
        ), self._history_page_ready)

    def _history_page_ready(self, result, error):
        if error:
            self._history_page_label.setText("History unavailable")
            self._history_preview.setPlainText(str(error))
            return
        self._history_page = result["page"]
        for row in result["rows"]:
            item = QListWidgetItem(
                f"{row['started_at']} · {row['status']} · {row['experiment_id'][:12]}"
            )
            item.setData(Qt.ItemDataRole.UserRole, row)
            self._history_list.addItem(item)
        self._history_preview_btn.setEnabled(self._history_list.count() > 0)
        self._history_page_label.setText(f"Page {self._history_page + 1}")
        self._history_prev.setEnabled(self._history_page > 0)
        self._history_next.setEnabled((self._history_page + 1) * result["page_size"] < result["total"])

    def _change_history_page(self, delta: int) -> None:
        self._history_page = max(0, self._history_page + int(delta))
        self._refresh_history()

    def _history_query_changed(self, _value: str = "") -> None:
        self._history_page = 0
        self._history_tasks().cancel_all()
        if not hasattr(self, "_history_query_timer"):
            self._history_query_timer = QTimer(self)
            self._history_query_timer.setSingleShot(True)
            self._history_query_timer.setInterval(150)
            self._history_query_timer.timeout.connect(self._refresh_history)
        self._history_query_timer.start()

    def _history_event_query_changed(self, _value: str = "") -> None:
        self._event_offset = 0
        self._history_tasks().cancel("preview")
        if not hasattr(self, "_history_event_timer"):
            self._history_event_timer = QTimer(self)
            self._history_event_timer.setSingleShot(True)
            self._history_event_timer.setInterval(150)
            self._history_event_timer.timeout.connect(self._preview_selected_history)
        self._history_event_timer.start()

    def _change_event_page(self, delta: int) -> None:
        self._event_offset = max(0, self._event_offset + int(delta))
        self._preview_history(self._history_list.currentRow())

    def _history_selection_changed(self, _row: int) -> None:
        self._history_tasks().cancel("preview")
        self._history_tasks().cancel("load")
        self._event_offset = 0
        self._history_preview.clear()
        self._history_load.setEnabled(False)
        self._history_preview_btn.setEnabled(self._history_list.currentItem() is not None)
        self._history_metadata_path = None
        if hasattr(self, "_history_open"):
            self._history_open.setEnabled(False)
        for name in ("_event_prev", "_event_next"):
            if hasattr(self, name):
                getattr(self, name).setEnabled(False)

    def _preview_history(self, _row: int) -> None:
        item = self._history_list.currentItem()
        if item is None:
            return
        row = item.data(Qt.ItemDataRole.UserRole) or {}
        raw_path, root = row.get("metadata_path", ""), cfg.base_out
        event_filter, offset = self._event_filter.text().strip().lower(), self._event_offset
        self._history_preview.setPlainText("Loading metadata…")
        self._history_load.setEnabled(False)
        self._event_prev.setEnabled(False)
        self._event_next.setEnabled(False)
        self._history_tasks().submit("preview", lambda cancel: load_preview(
            raw_path, root, event_filter, offset, cancel,
        ), self._history_preview_ready)

    def _history_preview_ready(self, result, error):
        self._history_preview.setPlainText(str(error) if error else result["text"])
        self._history_load.setEnabled(not bool(error))
        self._event_prev.setEnabled(not bool(error) and self._event_offset >= 20)
        self._event_next.setEnabled(not bool(error) and result["has_next"])
        self._history_metadata_path = None if error else result["path"]
        self._history_open.setEnabled(not bool(error))

    def _open_history_metadata(self):
        path = getattr(self, "_history_metadata_path", None)
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _preview_selected_history(self) -> None:
        self._preview_history(self._history_list.currentRow())

    def _load_history_settings(self) -> None:
        item = self._history_list.currentItem()
        if item is None:
            return
        row = item.data(Qt.ItemDataRole.UserRole) or {}
        raw_path, root = row.get("metadata_path", ""), cfg.base_out
        current, kind = self._tabs.currentWidget(), self._history_type.currentText()
        adapter = self._history_adapters().get(kind)
        method = getattr(current, adapter, None) if adapter else None
        if not callable(method):
            self._status.showMessage("No compatible settings adapter for this tab", 6000)
            return
        supported_keys = method({}, keys_only=True)["supported_keys"]
        self._history_load.setEnabled(False)
        def ready(loadable, error):
            if self._tabs.currentWidget() is not current or self._history_type.currentText() != kind:
                return
            self._history_load.setEnabled(True)
            if error:
                self._status.showMessage(f"History settings could not be loaded: {error}", 8000)
                return
            adapter = self._history_adapters().get(kind)
            if adapter is None or not callable(getattr(current, adapter, None)):
                self._status.showMessage("No compatible settings adapter for this tab", 6000)
                return
            try:
                report = getattr(current, adapter)(loadable)
                self._status.showMessage(f"Loaded compatible settings; skipped: {report.get('skipped', [])}", 8000)
            except Exception as exc:
                self._status.showMessage(f"History settings could not be loaded: {exc}", 8000)
        self._history_tasks().submit("load", lambda cancel: load_settings(
            raw_path, root, cancel, supported_keys=supported_keys,
        ), ready)

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
        capture_errors: list[str] = []
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
            except Exception as exc:
                capture_errors.append(f"{key}: {exc}")
                continue
            if isinstance(value, dict):
                panels[key] = value
        if capture_errors:
            self._session_capture_errors = capture_errors
            self._status.showMessage(
                "Some settings could not be captured: " + "; ".join(capture_errors),
                8000,
            )
        else:
            self._session_capture_errors = []
        return {
            "schema_version": 3,
            "active_tab": self._active_tab_id(),
            "sample_id": (
                self._sample_id_binder.value
                if hasattr(self, "_sample_id_binder")
                else cfg.session.sample_id
            ),
            "panels": panels,
            "capture_errors": capture_errors,
        }

    def _restore_panels(self, panels: object) -> None:
        if isinstance(panels, dict):
            for key, panel in self._session_panels.items():
                restore = getattr(panel, "restore_session_state", None)
                state = panels.get(key)
                if restore is None or not isinstance(state, dict):
                    continue
                try:
                    restore(state)
                    if getattr(self, "_ui_mode", "widgets") == "quick":
                        from ui.quick_workspace_layouts import remember_workspace_layout
                        remember_workspace_layout(panel, state)
                except Exception as exc:
                    self._status.showMessage(
                        f"Some saved {key.replace('_', ' ')} settings were skipped: {exc}",
                        8000,
                    )
    def _restore_state(self, state: object) -> None:
        if not isinstance(state, dict):
            return
        self._restore_panels(state.get("panels"))
        wanted = state.get("active_tab", "dual_gate")
        for widget, tab_id in self._tab_ids.items():
            if tab_id == wanted:
                if self._tabs.isTabVisible(self._tabs.indexOf(widget)):
                    self._tabs.setCurrentWidget(widget)
                elif tab_id == "calibration":
                    self._tabs.setCurrentWidget(self._spectrum)
                break

    @staticmethod
    def _sample_scoped_state(state: object) -> dict:
        """Remove global connection/output preferences from a sample profile."""
        if not isinstance(state, dict):
            return {}
        scoped = deepcopy(state)
        panels = scoped.get("panels")
        if not isinstance(panels, dict):
            return scoped
        # Settings owns global detector defaults and output paths.  Instrument
        # setup keeps useful per-sample UI presets (jog, expansion, wavelength)
        # while connection identities remain global.
        settings = panels.get("settings")
        if isinstance(settings, dict):
            settings.pop("mff101", None)
            output = settings.get("output")
            if isinstance(output, dict):
                # Output root is global; temperature/mode/coefficient remain
                # useful sample metadata and therefore stay profile-scoped.
                output.pop("base_out", None)
            andor = settings.get("andor")
            if isinstance(andor, dict):
                for key in ("sdk_dir", "shamrock_dir", "si_serial", "ingaas_serial", "spectrograph_index"):
                    andor.pop(key, None)
        instruments = panels.get("instruments")
        if isinstance(instruments, dict):
            lf6 = instruments.get("lf6")
            if isinstance(lf6, dict):
                lf6.pop("backend", None)
                lf6.pop("use_mock", None)
            smu = instruments.get("smu")
            if isinstance(smu, dict):
                for key in (
                    "vbg_resource", "vtg_resource", "vbias_resource",
                    "termination", "compliance_by_addr",
                ):
                    smu.pop(key, None)
            for key in ("rot1", "rot2", "stage"):
                section = instruments.get(key)
                if isinstance(section, dict):
                    section.pop("backend", None)
                    section.pop("address", None)
                    section.pop("axis", None)
                    section.pop("include_asrl", None)
            imaging = instruments.get("imaging_stage")
            if isinstance(imaging, dict):
                imaging.pop("port", None)
            flipper = instruments.get("mff101")
            if isinstance(flipper, dict):
                flipper.pop("serial_number", None)
            pm = instruments.get("pm100d")
            if isinstance(pm, dict):
                pm.pop("device", None)
                pm.pop("correction_factor", None)
        return scoped

    def _restore_session(self) -> None:
        sample_id = self._sample_id_binder.value.strip()
        # Restore global connection/UI controls once at startup.  Sample
        # switching later uses the scoped profile and deliberately leaves
        # these identities untouched.
        self._restore_panels(cfg.session.panels)
        profile = self._sample_store.load(sample_id) if sample_id else None
        if profile is not None:
            self._restore_state(self._sample_scoped_state(profile))
        else:
            # A sample without its own profile inherits the last session.
            self._restore_state({
                "active_tab": cfg.session.active_tab,
                "panels": cfg.session.panels,
            })
        if sample_id:
            self._sample_id_binder.commit(sample_id)
        spectrum = getattr(self, "_spectrum", None)
        if spectrum is not None:
            panels = profile.get("panels", {}) if profile is not None else cfg.session.panels
            spectrum.restore_sample_references(sample_id, panels.get("spectrum", {}))
        self._refresh_sample_selector()

    def _acquisition_active(self) -> bool:
        if getattr(self, "_power_sweep_running", False) or getattr(self, "_active_mcd_panel", None) is not None:
            return True
        for panel, thread_attrs in (
            (getattr(self, "_presets", None), ("_run_thread",)),
            (getattr(self, "_mega", None), ("_thread",)),
            (getattr(self, "_bfp", None), ("_thread",)),
        ):
            for attr in thread_attrs:
                thread = getattr(panel, attr, None)
                if thread is not None and callable(getattr(thread, "isRunning", None)) and thread.isRunning():
                    return True
        spectrum = getattr(self, "_spectrum", None)
        return bool(
            getattr(spectrum, "_continuous_mode", None) is not None
            or getattr(spectrum, "_pending_acquisition", None) is not None
            or bool(getattr(getattr(spectrum, "_abort_btn", None), "isEnabled", lambda: False)())
        )

    def _commit_sample_selection(self, sample_id: object) -> bool:
        target = self._sample_store.normalize_id(sample_id)
        current = self._sample_id_binder.value.strip()
        if not target:
            self._status.showMessage("Sample ID cannot be empty", 5000)
            self._refresh_sample_selector()
            return False
        if self._acquisition_active():
            self._status.showMessage("Sample settings cannot change during acquisition", 5000)
            self._refresh_sample_selector()
            return False
        if target == current:
            self._sample_id_binder.commit(target)
            self._refresh_sample_selector()
            self._refresh_history()
            return True
        # Persist the outgoing sample synchronously before any controls are
        # restored, preventing a debounce timer from crossing profile keys.
        self._persist_session()
        self._sample_restore_in_progress = True
        try:
            profile = self._sample_store.load(target)
            if profile is not None:
                self._restore_state(self._sample_scoped_state(profile))
            # Without a saved profile, leave the current controls and drafts
            # intact; persisting below copies them under the new Sample ID.
            self._sample_id_binder.commit(target)
            spectrum = getattr(self, "_spectrum", None)
            if spectrum is not None:
                panels = profile.get("panels", {}) if profile is not None else {}
                spectrum.restore_sample_references(target, panels.get("spectrum", {}))
            self._refresh_sample_selector()
            self._refresh_history()
        finally:
            self._sample_restore_in_progress = False
        self._persist_session()
        message = (f"Loaded sample settings: {target}" if profile is not None else
                   f"No saved settings for {target}; kept current settings")
        self._status.showMessage(message, 4000)
        return True

    def _poll_session_changes(self) -> None:
        if getattr(self, "_sample_restore_in_progress", False):
            return
        current = self._capture_session()
        if current != self._last_observed_session:
            self._last_observed_session = current
            if hasattr(self, "_sample_save_status"):
                self._sample_save_status.setText("Unsaved changes")
            self._session_save_timer.start()

    def _schedule_session_observation(self, *_args) -> None:
        self._session_observe_timer.start()

    def _persist_session(self) -> None:
        if getattr(self, "_sample_restore_in_progress", False):
            return
        session = self._capture_session()
        cfg.session.schema_version = int(session["schema_version"])
        cfg.session.active_tab = str(session["active_tab"])
        cfg.session.sample_id = str(session["sample_id"])
        cfg.session.panels = session["panels"]
        sample_id = str(session["sample_id"]).strip()
        if sample_id and hasattr(self, "_sample_store"):
            self._sample_store.save(sample_id, self._sample_scoped_state(session))
            cfg.session.sample_profiles = self._sample_store._profiles
        self._last_observed_session = session
        try:
            cfg.save()
            if hasattr(self, "_sample_save_status"):
                self._sample_save_status.setText(
                    "Saved with warnings" if session.get("capture_errors") else "Saved just now"
                )
        except Exception as exc:
            self._status.showMessage(f"Could not save settings: {exc}", 8000)
            if hasattr(self, "_sample_save_status"):
                self._sample_save_status.setText("Save failed")

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
        restored = bool(geom is not None and self.restoreGeometry(geom))
        if not restored and getattr(self, "_ui_mode", "widgets") == "quick":
            # A new desktop workspace uses the actual monitor's available area;
            # valid saved geometry still wins on subsequent launches.
            self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)
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
            if getattr(self, "_power_sweep_running", False):
                return
            if self._active_mcd_panel not in (None, source):
                return
            self._active_mcd_panel = source
        elif self._active_mcd_panel is source:
            self._active_mcd_panel = None
        elif self._active_mcd_panel is not None:
            return

        active = self._active_mcd_panel
        pause = getattr(getattr(self, "_lf6", None), "set_temperature_monitor_paused", None)
        if callable(pause):
            pause("mcd", active is not None)
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

    def _on_power_sweep_state_changed(self, running: bool) -> None:
        """Reserve shared instruments while the motion sweep owns hardware."""
        self._power_sweep_running = bool(running)
        pause = getattr(getattr(self, "_lf6", None), "set_temperature_monitor_paused", None)
        if callable(pause):
            pause("power_sweep", bool(running))
        if running and self._active_mcd_panel is not None:
            return
        self._inst_panel.setEnabled(not running)
        # Newer instrument panels may suspend their PM polling worker here;
        # keep this optional for older panels and lightweight test doubles.
        pm_busy_setter = getattr(self._inst_panel, "set_external_busy", None)
        if callable(pm_busy_setter):
            pm_busy_setter(bool(running))
        for index in range(self._tabs.count()):
            self._tabs.setTabEnabled(index, (not running) or self._tabs.widget(index) is self._power_sweep)
        for panel in (self._mcd, self._mcd2100):
            setter = getattr(panel, "set_externally_busy", None)
            if callable(setter):
                setter(bool(running))
        if running:
            self._tabs.setCurrentWidget(self._power_sweep)
            self._status.showMessage("Motion sweep running — instrument controls locked")
        else:
            if self._active_mcd_panel is not None:
                self._on_mcd_workflow_state_changed(self._active_mcd_panel, True)
            else:
                self._status.showMessage("Motion sweep finished — instrument controls unlocked")

    def _on_capture_stalled(self, status):
        phase = status.get("stalled_phase") or status.get("phase", "acquisition")
        metadata_stalled = status.get("timeout_domain") == "metadata"
        title = "LightField metadata persistence stalled" if metadata_stalled else "LightField acquisition stalled"
        label = "Metadata persistence stalled" if metadata_stalled else "LightField stalled"
        elapsed = status.get("wait_elapsed_s", status.get("elapsed_s", 0))
        message = (f"{label} in {phase} after {elapsed:.0f} s. "
                   "Device ownership retained; no automatic retry. "
                   "Wait for the call to return before disconnecting and reconnecting.")
        diagnostic = status.get("diagnostic_path")
        if diagnostic:
            message += f" Diagnostics: {diagnostic}"
        self._status.showMessage(message)
        self._ntfy_notifier.notify_warning(
            title, message,
            key=("lightfield-capture-stalled", status.get("incident_id")),
        )

    def _resume_close_after_plan_build(self):
        self._plan_close_retry_pending = False
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        status = getattr(getattr(self, "_lf6", None), "capture_status", {})
        if ((isinstance(status, dict) and status.get("active")) or
                getattr(getattr(self, "_lf6", None), "acquisition_in_progress", False) is True or
                getattr(getattr(self, "_lf6", None), "sensor_preflight_pending", False) is True):
            self._status.showMessage(
                "LightField still owns the device; wait for the hardware call before closing.", 5000)
            event.ignore()
            return
        presets = getattr(self, "_presets", None)
        if getattr(presets, "plan_build_running", lambda: False)():
            presets.cancel_plan_builds()
            if not getattr(self, "_plan_close_retry_pending", False):
                self._plan_close_retry_pending = True
                presets.plan_build_idle.connect(
                    self._resume_close_after_plan_build, Qt.ConnectionType.SingleShotConnection)
            self._status.showMessage("Cancelling the Dual Gate sequence build before closing…", 5000)
            event.ignore()
            return
        thread = getattr(presets, "_run_thread", None)
        finalizing = getattr(presets, "metadata_finalizing", False)
        if finalizing or (thread is not None and thread.isRunning()):
            message = ("Saving experiment metadata; wait before closing." if finalizing else
                       "Dual Gate is still running; stop the measurement and wait before closing.")
            self._status.showMessage(message, 5000)
            event.ignore()
            return
        try:
            if not self._power_sweep.shutdown():
                QMessageBox.critical(self, "Motion sweep is still stopping", "The motion sweep worker did not stop within 30 seconds.")
                event.ignore()
                return
        except Exception as exc:
            QMessageBox.critical(self, "Unable to stop motion sweep safely", str(exc))
            event.ignore()
            return
        try:
            if not self._mcd2100.shutdown():
                self._ntfy_notifier.notify_warning(
                    None,
                    "MCD 2100 shutdown could not be confirmed; application remains open.",
                    key="mcd2100-shutdown-unconfirmed",
                )
                QMessageBox.critical(
                    self,
                    "MCD 2100 acquisition is still stopping",
                    "The application will remain open until controller and acquisition cleanup finish.",
                )
                event.ignore()
                return
        except Exception as exc:
            try:
                self._ntfy_notifier.notify_warning(
                    None,
                    "MCD 2100 shutdown failed; application remains open.",
                    key="mcd2100-shutdown-unconfirmed",
                )
            except Exception:
                pass
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
        try:
            self._ntfy_notifier.shutdown()
        except Exception:
            pass
        try:
            if not self._imaging_stg.shutdown():
                QMessageBox.critical(
                    self,
                    "Unable to close imaging stage",
                    "The ESP32 imaging-stage worker did not confirm serial shutdown; the application remains open.",
                )
                event.ignore()
                return
        except Exception as exc:
            QMessageBox.critical(self, "Unable to close imaging stage", str(exc))
            event.ignore()
            return
        if not self._mff101.shutdown():
            detail = self._mff101.state.get("error") or "Waiting for the flipper operation to finish."
            QMessageBox.warning(self, "MFF101 could not close", f"{detail}\nPlease close again to retry.")
            event.ignore()
            return
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
        if hasattr(self, "_history_task_runner"):
            self._history_task_runner.cancel_all()
        for name in ("_history_query_timer", "_history_event_timer"):
            if hasattr(self, name):
                getattr(self, name).stop()
        quick = getattr(self, "_quick_workbench", None)
        if quick is not None:
            quick.shutdown()
        super().closeEvent(event)
