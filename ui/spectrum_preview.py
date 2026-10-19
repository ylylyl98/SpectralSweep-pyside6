"""The real SpectrumPanel driven exclusively by an in-memory simulator."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QToolBar, QVBoxLayout, QWidget

from app.ui_preview import RELOAD_EXIT_CODE
from ui.application_style import application_style
from ui.spectrum_panel import SpectrumPanel
from utils.config import cfg


class PreviewSpectrumController(QObject):
    connected = Signal(list)
    disconnected = Signal()
    spectrum_ready = Signal(object, object)
    frame_ready = Signal(object)
    settings_applied = Signal()
    error = Signal(str)
    spectrograph_status_ready = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.backend = "lightfield"
        self.identity = {"backend": "lightfield", "model": "Synthetic preview", "simulated": True}
        self.center_nm = float(cfg.lf6.center_nm)
        self.exposure_ms = float(cfg.lf6.exposure_ms)
        self.accumulations = int(cfg.lf6.accumulations)
        self.center_wavelength_write_stats = {}
        self.is_connected = False
        self._frame = 0
        self._pending = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._complete)

    def set_connected(self, connected: bool) -> None:
        self.abort_acquisition()
        self.is_connected = connected
        if connected:
            self.connected.emit([])
        else:
            self.disconnected.emit()

    def apply_settings(self, exposure_ms, center_nm, accumulations) -> None:
        self.center_nm = float(center_nm)
        self.exposure_ms = float(exposure_ms)
        self.accumulations = int(accumulations)
        self.center_wavelength_write_stats = {"result": "succeeded", "readback": self.center_nm}
        self._schedule("settings", 80)

    def lightfield_optics(self, requested=None) -> None:
        if requested and "wavelength_nm" in requested:
            self.center_nm = float(requested["wavelength_nm"])
        self.spectrograph_status_ready.emit({
            "wavelength_nm": self.center_nm, "grating": "Preview grating",
        })

    def set_temperature_monitor_paused(self, _source, _paused) -> None:
        pass  # No temperature monitor or SDK exists in this process.

    def _schedule(self, kind: str, delay: int = 200) -> None:
        if self.is_connected:
            self._pending = kind
            self._timer.start(delay)

    def acquire_single(self) -> None:
        self._schedule("1d")

    def acquire_2d(self) -> None:
        self._schedule("2d")

    def abort_acquisition(self) -> bool:
        self._timer.stop()
        self._pending = None
        return True

    def spectrum(self, phase=0.0):
        x = np.linspace(self.center_nm - 90, self.center_nm + 90, 1024)
        shift = 4 * math.sin(phase)
        y = (80 + 650 * np.exp(-((x - self.center_nm + 28 - shift) / 12) ** 2)
             + 430 * np.exp(-((x - self.center_nm - 32) / 19) ** 2)
             + 8 * np.sin(x * 0.7 + phase))
        return x, y * max(self.exposure_ms, 1) / 250

    def _complete(self) -> None:
        kind, self._pending = self._pending, None
        if not self.is_connected:
            return
        if kind == "settings":
            self.settings_applied.emit()
        elif kind in {"1d", "2d"}:
            self._frame += 1
            x, y = self.spectrum(self._frame * 0.2)
            if kind == "1d":
                self.spectrum_ready.emit(x, y)
            else:
                profile = np.exp(-(np.linspace(-2, 2, 128) ** 2))
                self.frame_ready.emit(profile[:, None] * y[None, :])


class SpectrumPreviewWindow(QMainWindow):
    def __init__(self, scratch_root: Path):
        super().__init__()
        self.setWindowTitle("SpectralSweep — Spectrum PREVIEW (simulated)")
        self.resize(1400, 900)
        self.controller = PreviewSpectrumController(self)
        self.panel = SpectrumPanel(self.controller)
        self._style = application_style(QApplication.instance())
        self._style.reloaded.connect(self._styles_reloaded)
        self._style.failed.connect(self._styles_failed)
        self._style.reload()
        self._style.set_watching(True)

        container = QWidget()
        layout = QVBoxLayout(container)
        note = QLabel("PREVIEW — Synthetic data only. F5 reloads Python/layout changes and resets this scene. "
                      "Styles refresh automatically. Scratch files are removed when the preview closes.")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(self.panel, 1)
        self.setCentralWidget(container)

        toolbar = QToolBar("Preview controls", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        reload_action = QAction("Reload preview (F5)", self)
        reload_action.setShortcut("F5")
        reload_action.triggered.connect(self.reload_preview)
        toolbar.addAction(reload_action)
        styles = QAction("Reload styles", self)
        styles.setShortcut("Ctrl+Alt+R")
        styles.triggered.connect(self._style.reload)
        toolbar.addAction(styles)
        self.connection_action = QAction("Simulated connection", self)
        self.connection_action.setCheckable(True)
        self.connection_action.setChecked(True)
        self.connection_action.toggled.connect(self.controller.set_connected)
        toolbar.addAction(self.connection_action)
        error_action = toolbar.addAction("Show sample error")
        error_action.triggered.connect(self.show_sample_error)
        self.statusBar().showMessage(f"Preview scratch folder: {scratch_root}")
        self.controller.set_connected(True)
        x, y = self.controller.spectrum()
        self.controller.spectrum_ready.emit(x, y)
        for index, factor in enumerate((0.72, 1.15), start=1):
            self.panel.add_reference(x, y * factor, name=f"Simulated reference {index}", auto_save=False)
        screen = self.screen()
        if screen:
            available = screen.availableGeometry()
            self.resize(min(1400, available.width() - 60), min(900, available.height() - 60))

    def _styles_reloaded(self, _path):
        self.statusBar().showMessage("Styles reloaded — preview scene preserved", 4000)

    def _styles_failed(self, reason):
        self.statusBar().showMessage(f"Styles unchanged: {reason}", 10000)

    def show_sample_error(self) -> None:
        self.controller.abort_acquisition()
        self.controller.error.emit("Simulated acquisition failure. No instrument was contacted.")

    def reload_preview(self) -> None:
        self.controller.abort_acquisition()
        QApplication.instance().exit(RELOAD_EXIT_CODE)

    def closeEvent(self, event) -> None:
        self.controller.abort_acquisition()
        self._style.set_watching(False)
        super().closeEvent(event)
