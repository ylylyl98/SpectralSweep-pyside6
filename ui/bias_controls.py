"""Compact optional second-bias recipe controls shared by experiment pages."""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QLabel, QVBoxLayout, QWidget


class _BiasVoltageSpin(QDoubleSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class Bias2Control(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SecondBiasSettings")
        self.setProperty("quickSectionBoundary", True)
        self._configured = False
        self._voltage_visible = True
        self._updating = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.enable = QCheckBox("Use second bias")
        self.enable.setToolTip("Unchecked leaves the second source unchanged; it does not switch its output off.")
        layout.addWidget(self.enable)
        self.body = QWidget(self)
        self.body.setProperty("quickSectionBoundary", True)
        self.form = QFormLayout(self.body)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.mode = QComboBox()
        self.mode.addItem("Independent", "independent")
        self.mode.addItem("Follow Vbias + offset", "follow")
        self.form.addRow("Vbias2 mode:", self.mode)
        self.voltage = _BiasVoltageSpin()
        self.offset = _BiasVoltageSpin()
        for spin, name in ((self.voltage, "Vbias2 voltage"), (self.offset, "Vbias2 offset from Vbias")):
            spin.setRange(-200.0, 200.0)
            spin.setDecimals(6)
            spin.setSuffix(" V")
            spin.setKeyboardTracking(False)
            spin.setAccessibleName(name)
            spin.valueChanged.connect(self._changed)
        self.form.addRow("Vbias2:", self.voltage)
        self.form.addRow("Offset:", self.offset)
        self.summary = QLabel("Vbias2 = Vbias + offset")
        self.summary.setWordWrap(True)
        self.form.addRow(self.summary)
        layout.addWidget(self.body)
        self.enable.toggled.connect(self._changed)
        self.mode.currentIndexChanged.connect(self._changed)
        self._sync()

    def settings(self):
        return {"bias2_mode": self.mode.currentData() if self.enable.isChecked() else "off",
                "vbias2_v": self.voltage.value(), "bias2_offset_v": self.offset.value()}

    def set_settings(self, settings):
        settings = dict(settings or {})
        mode = settings.get("bias2_mode", "off")
        if mode not in ("off", "independent", "follow"):
            raise ValueError(f"Unknown second bias mode: {mode}")
        self._updating = True
        try:
            self.mode.setCurrentIndex(max(0, self.mode.findData(mode)))
            self.enable.setChecked(mode != "off")
            self.voltage.setValue(float(settings.get("vbias2_v", 0.0)))
            self.offset.setValue(float(settings.get("bias2_offset_v", 0.0)))
        finally:
            self._updating = False
        self._changed()

    def refresh_visibility(self, configured):
        self._configured = bool(configured)
        self._sync()

    def set_voltage_visible(self, visible):
        self._voltage_visible = bool(visible)
        self._sync()

    def _sync(self):
        active = self.enable.isChecked()
        follow = self.mode.currentData() == "follow"
        self.setVisible(self._configured or active)
        self.body.setVisible(active)
        self.form.setRowVisible(self.voltage, not follow and self._voltage_visible)
        self.form.setRowVisible(self.offset, follow)
        self.summary.setVisible(follow)

    def _changed(self, *_):
        if not self._updating:
            self._sync()
            self.changed.emit()
