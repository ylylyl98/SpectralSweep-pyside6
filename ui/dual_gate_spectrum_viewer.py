from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

class DualGateSpectrumViewer(QWidget):
    visibility_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Last LightField Acquisition")
        self.resize(900, 650)
        self.setMinimumSize(600, 420)

        root = QVBoxLayout(self)
        header = QHBoxLayout()
        self._info = QLabel("No completed acquisition yet.")
        self._info.setWordWrap(True)
        self._log = QCheckBox("Log intensity")
        self._fit = QPushButton("Fit View")
        self._hide = QPushButton("Hide")
        header.addWidget(self._info, stretch=1)
        header.addWidget(self._log)
        header.addWidget(self._fit)
        header.addWidget(self._hide)
        root.addLayout(header)

        self._stack = QStackedWidget()
        self._plot = pg.PlotWidget()
        self._plot.setLabel("bottom", "Wavelength", units="nm")
        self._plot.setLabel("left", "Intensity")
        self._plot.showGrid(x=True, y=True, alpha=0.25)
        self._curve = self._plot.plot(pen=pg.mkPen("#1769aa", width=1.4))
        self._image = pg.ImageView(view=pg.PlotItem())
        self._image.ui.roiBtn.hide()
        self._image.ui.menuBtn.hide()
        self._image.getView().setAspectLocked(False)
        self._image.view.setLabel("bottom", "Wavelength", units="nm")
        self._image.view.setLabel("left", "Y Pixel")
        self._stack.addWidget(self._plot)
        self._stack.addWidget(self._image)
        root.addWidget(self._stack, stretch=1)

        self._last_payload: Optional[dict[str, Any]] = None
        self._hide.clicked.connect(self.hide)
        self._fit.clicked.connect(self.fit_view)
        self._log.toggled.connect(self._redraw)

    def showEvent(self, event):
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event):
        self.clear_data()
        self.visibility_changed.emit(False)
        super().hideEvent(event)

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def clear_data(self) -> None:
        self._last_payload = None
        self._curve.setData([], [])
        self._image.clear()

    def show_message(self, message: str) -> None:
        self._info.setText(str(message))

    def set_acquisition(self, payload: dict[str, Any]) -> None:
        self._last_payload = payload
        self._redraw()

    def _redraw(self) -> None:
        payload = self._last_payload
        if not payload:
            return
        wl = np.asarray(payload["wavelengths"], dtype=float)
        data = np.asarray(payload["data"], dtype=float)
        shown = np.log1p(np.clip(data, 0, None)) if self._log.isChecked() else data
        point = payload.get("point_number")
        total = payload.get("point_total")
        point_text = f"Point {point}/{total} | " if point is not None and total is not None else ""
        voltage_bits = []
        for label, key in (("Vbg", "Vbg_set"), ("Vtg", "Vtg_set"), ("Vbias", "Vbias_set")):
            value = payload.get(key)
            if value is not None:
                voltage_bits.append(f"{label}={float(value):g} V")
        if data.ndim == 1:
            self._stack.setCurrentWidget(self._plot)
            self._curve.setData(wl, shown)
            self._info.setText(point_text + " | ".join(voltage_bits + [f"1D spectrum: {data.size} points"]))
        else:
            self._stack.setCurrentWidget(self._image)
            self._image.setImage(shown.T, autoLevels=True, autoRange=True)
            y = np.asarray(payload.get("y_pixels", np.arange(data.shape[0])), dtype=float)
            if wl.size and y.size:
                self._image.getImageItem().setRect(
                    float(wl[0]), float(y[0]),
                    float(wl[-1] - wl[0]) if wl.size > 1 else 1.0,
                    float(y[-1] - y[0]) if y.size > 1 else 1.0,
                )
            self._info.setText(point_text + " | ".join(voltage_bits + [f"Full sensor: {data.shape[0]} x {data.shape[1]}"]))
        self.fit_view()

    def fit_view(self) -> None:
        if self._stack.currentWidget() is self._plot:
            self._plot.enableAutoRange()
        else:
            self._image.getView().autoRange()
