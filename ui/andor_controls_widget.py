"""Live Andor camera and Shamrock controls embedded in the Spectrum tab."""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from utils.config import cfg


class AndorControlsWidget(QWidget):
    """Apply-and-readback UI for one connected Andor/Shamrock pair."""

    status_changed = Signal(str)

    def __init__(self, controller=None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._ctrl = controller
        self._identity: dict[str, Any] = {}
        self._snapshot: dict[str, Any] = {}
        self._locked = False
        self._available = False
        self._slit_present = False
        self._shutter_present = False
        self._output_flipper_present = False
        self._build()
        self._wire()
        self.set_backend_identity({})

    def _build(self) -> None:
        root = QGridLayout(self)
        root.setContentsMargins(0, 8, 0, 8)
        root.setHorizontalSpacing(10)
        root.setVerticalSpacing(8)
        root.addWidget(self._build_spectrograph_group(), 0, 0)
        root.addWidget(self._build_detector_group(), 0, 1)
        root.addWidget(self._build_status_group(), 0, 2)
        root.setColumnStretch(0, 1)
        root.setColumnStretch(1, 1)
        root.setColumnStretch(2, 1)

    def _build_spectrograph_group(self) -> QGroupBox:
        group = QGroupBox("Spectrograph")
        form = QFormLayout(group)

        self.center = QDoubleSpinBox()
        self.center.setRange(0.0, 5000.0)
        self.center.setDecimals(2)
        self.center.setSuffix(" nm")
        form.addRow("Center:", self.center)

        self.grating = QComboBox()
        self.grating.currentIndexChanged.connect(self._update_grating_detail)
        form.addRow("Grating:", self.grating)
        self.grating_info = QLabel("No grating information read")
        self.grating_info.setWordWrap(True)
        form.addRow("", self.grating_info)

        self.slit = QDoubleSpinBox()
        self.slit.setRange(1.0, 5000.0)
        self.slit.setDecimals(1)
        self.slit.setSuffix(" µm")
        form.addRow("Input slit:", self.slit)

        self.shutter = QComboBox()
        self.shutter.addItems(["unchanged", "closed", "opened", "bnc"])
        form.addRow("Shutter:", self.shutter)

        self.output_port = QComboBox()
        self.output_port.addItems(["unchanged", "direct", "side"])
        self.output_port.setToolTip(
            "Routes the Shamrock output flipper mirror. Direct and side are "
            "vendor port names; verify which physical detector is attached to each."
        )
        form.addRow("Output port:", self.output_port)
        return group

    def _build_detector_group(self) -> QGroupBox:
        group = QGroupBox("Detector readout")
        form = QFormLayout(group)

        self.temperature_target = QDoubleSpinBox()
        self.temperature_target.setRange(-120.0, 30.0)
        self.temperature_target.setDecimals(1)
        self.temperature_target.setSuffix(" °C")
        form.addRow("Temperature target:", self.temperature_target)

        self.cooler = QCheckBox("Cooler enabled")
        form.addRow("", self.cooler)

        self.fan = QComboBox()
        self.fan.addItems(["full", "low", "off"])
        form.addRow("Fan:", self.fan)

        self.read_mode = QComboBox()
        self.read_mode.addItem("1D spectrum (FVB)", "fvb")
        self.read_mode.addItem("2D image", "image")
        form.addRow("Sensor mode:", self.read_mode)

        roi_row = QHBoxLayout()
        self.roi_hstart = self._roi_spin()
        self.roi_hend = self._roi_spin()
        roi_row.addWidget(QLabel("H"))
        roi_row.addWidget(self.roi_hstart)
        roi_row.addWidget(QLabel("to"))
        roi_row.addWidget(self.roi_hend)
        form.addRow("Horizontal ROI:", roi_row)

        roi_v_row = QHBoxLayout()
        self.roi_vstart = self._roi_spin()
        self.roi_vend = self._roi_spin()
        roi_v_row.addWidget(QLabel("V"))
        roi_v_row.addWidget(self.roi_vstart)
        roi_v_row.addWidget(QLabel("to"))
        roi_v_row.addWidget(self.roi_vend)
        form.addRow("Vertical ROI:", roi_v_row)

        bin_row = QHBoxLayout()
        self.hbin = QSpinBox()
        self.hbin.setRange(1, 32)
        self.vbin = QSpinBox()
        self.vbin.setRange(1, 32)
        bin_row.addWidget(QLabel("H"))
        bin_row.addWidget(self.hbin)
        bin_row.addWidget(QLabel("V"))
        bin_row.addWidget(self.vbin)
        form.addRow("Binning:", bin_row)
        return group

    @staticmethod
    def _roi_spin() -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(0, 16384)
        return spin

    def _build_status_group(self) -> QGroupBox:
        group = QGroupBox("Status and stored calibration")
        layout = QVBoxLayout(group)
        self.identity = QLabel("No Andor hardware connected")
        self.identity.setWordWrap(True)
        self.cooling_status = QLabel("Cooling status: —")
        self.cooling_status.setWordWrap(True)
        self.calibration = QLabel("Stored calibration: not read")
        self.calibration.setWordWrap(True)
        self.calibration.setToolTip(
            "This reads calibration coefficients already stored in the Shamrock. "
            "The application does not fit or overwrite them."
        )
        layout.addWidget(self.identity)
        layout.addWidget(self.cooling_status)
        layout.addWidget(self.calibration)
        layout.addStretch()

        actions = QHBoxLayout()
        self.apply_button = QPushButton("Apply + verify")
        self.refresh_button = QPushButton("Refresh all")
        self.diagnostics_button = QPushButton("Diagnostics")
        actions.addWidget(self.apply_button)
        actions.addWidget(self.refresh_button)
        actions.addWidget(self.diagnostics_button)
        layout.addLayout(actions)
        return group

    def _wire(self) -> None:
        self.apply_button.clicked.connect(self.apply)
        self.refresh_button.clicked.connect(self.refresh)
        self.diagnostics_button.clicked.connect(self.show_diagnostics)
        if self._ctrl is None:
            return
        status_signal = getattr(self._ctrl, "andor_status_ready", None)
        if status_signal is not None:
            status_signal.connect(self._on_status)
        applied_signal = getattr(self._ctrl, "andor_controls_applied", None)
        if applied_signal is not None:
            applied_signal.connect(self._on_applied)
        self._ctrl.error.connect(self._on_error)

    def set_backend_identity(self, identity: dict[str, Any]) -> None:
        self._identity = dict(identity or {})
        self._available = self._identity.get("backend") == "andor_sdk2"
        self.setVisible(self._available)
        self._update_enabled()
        if not self._available:
            return
        role = str(self._identity.get("camera_role", "ingaas"))
        target = getattr(
            cfg.lf6,
            f"andor_{role}_temperature_c",
            cfg.lf6.andor_temperature_c,
        )
        fan = getattr(cfg.lf6, f"andor_{role}_fan_mode", cfg.lf6.andor_fan_mode)
        port = getattr(cfg.lf6, f"andor_{role}_output_port", "unchanged")
        self.temperature_target.setValue(float(target))
        self.fan.setCurrentText(str(fan))
        self.output_port.setCurrentText(str(port))
        self.center.setValue(float(cfg.lf6.center_nm))
        self.slit.setValue(float(cfg.lf6.andor_slit_width_um))
        self.refresh()

    def set_controls_locked(self, locked: bool) -> None:
        self._locked = bool(locked)
        self._update_enabled()

    def _update_enabled(self) -> None:
        enabled = self._available and not self._locked
        for widget in (
            self.center,
            self.grating,
            self.temperature_target,
            self.cooler,
            self.fan,
            self.read_mode,
            self.roi_hstart,
            self.roi_hend,
            self.roi_vstart,
            self.roi_vend,
            self.hbin,
            self.vbin,
            self.apply_button,
            self.refresh_button,
        ):
            widget.setEnabled(enabled)
        self.slit.setEnabled(enabled and self._slit_present)
        self.shutter.setEnabled(enabled and self._shutter_present)
        self.output_port.setEnabled(enabled and self._output_flipper_present)
        self.diagnostics_button.setEnabled(self._available)
        role = str(self._identity.get("camera_role", ""))
        si_controls = enabled and role == "si"
        for widget in (
            self.read_mode,
            self.roi_hstart,
            self.roi_hend,
            self.roi_vstart,
            self.roi_vend,
            self.hbin,
            self.vbin,
        ):
            widget.setEnabled(si_controls)

    @Slot()
    def refresh(self) -> None:
        method = getattr(self._ctrl, "refresh_andor_status", None)
        if self._available and callable(method):
            self.refresh_button.setEnabled(False)
            self.status_changed.emit("Reading Andor status…")
            method()

    @Slot()
    def apply(self) -> None:
        method = getattr(self._ctrl, "apply_andor_controls", None)
        if not self._available or self._locked or not callable(method):
            return
        role = str(self._identity.get("camera_role", "ingaas"))
        grating = self.grating.currentData()
        settings: dict[str, Any] = {
            "wavelength_nm": float(self.center.value()),
            "grating": int(grating if grating is not None else cfg.lf6.andor_grating),
            "temperature_setpoint_c": float(self.temperature_target.value()),
            "cooler_on": bool(self.cooler.isChecked()),
            "fan_mode": self.fan.currentText(),
        }
        if self._slit_present:
            settings["input_slit_width_um"] = float(self.slit.value())
        if self._shutter_present:
            settings["shutter_mode"] = self.shutter.currentText()
        if self._output_flipper_present:
            settings["output_port"] = self.output_port.currentText()
        if role == "si":
            settings.update(
                {
                    "read_mode": self.read_mode.currentData(),
                    "roi_hstart": int(self.roi_hstart.value()),
                    "roi_hend": int(self.roi_hend.value()),
                    "roi_vstart": int(self.roi_vstart.value()),
                    "roi_vend": int(self.roi_vend.value()),
                    "horizontal_binning": int(self.hbin.value()),
                    "vertical_binning": int(self.vbin.value()),
                }
            )
        self._persist_requested(role, settings)
        self.apply_button.setEnabled(False)
        self.status_changed.emit("Applying and verifying Andor controls…")
        method(settings)

    def _persist_requested(self, role: str, settings: dict[str, Any]) -> None:
        cfg.lf6.center_nm = float(settings["wavelength_nm"])
        cfg.lf6.andor_grating = int(settings["grating"])
        if "input_slit_width_um" in settings:
            cfg.lf6.andor_slit_width_um = float(settings["input_slit_width_um"])
        if "shutter_mode" in settings:
            cfg.lf6.andor_shamrock_shutter_mode = str(settings["shutter_mode"])
        setattr(cfg.lf6, f"andor_{role}_temperature_c", float(settings["temperature_setpoint_c"]))
        setattr(cfg.lf6, f"andor_{role}_cooler_on_connect", bool(settings["cooler_on"]))
        setattr(cfg.lf6, f"andor_{role}_fan_mode", str(settings["fan_mode"]))
        if "output_port" in settings:
            setattr(cfg.lf6, f"andor_{role}_output_port", str(settings["output_port"]))
        if role == "si":
            cfg.lf6.andor_si_horizontal_binning = int(settings["horizontal_binning"])
            cfg.lf6.andor_si_vertical_binning = int(settings["vertical_binning"])
            cfg.lf6.andor_si_roi_hstart = int(settings["roi_hstart"])
            cfg.lf6.andor_si_roi_hend = int(settings["roi_hend"])
            cfg.lf6.andor_si_roi_vstart = int(settings["roi_vstart"])
            cfg.lf6.andor_si_roi_vend = int(settings["roi_vend"])

    @Slot(object)
    def _on_status(self, snapshot: object) -> None:
        data = dict(snapshot or {})
        if data.get("backend") != "andor_sdk2":
            return
        self._snapshot = data
        self.refresh_button.setEnabled(not self._locked)
        self.apply_button.setEnabled(not self._locked)
        self._populate_spectrograph(data)
        self._populate_detector(data)
        self._populate_status(data)
        errors = data.get("readback_errors", {})
        self.status_changed.emit(
            "Andor status has readback warnings"
            if isinstance(errors, dict) and errors
            else "Andor controls verified"
        )

    def _populate_spectrograph(self, data: dict[str, Any]) -> None:
        limits = data.get("wavelength_limits_nm")
        if isinstance(limits, (list, tuple)) and len(limits) == 2:
            self.center.setRange(float(limits[0]), float(limits[1]))
        if data.get("wavelength_nm") is not None:
            self.center.setValue(float(data["wavelength_nm"]))
        self.grating.blockSignals(True)
        self.grating.clear()
        current = int(data.get("grating", 1))
        selected_index = -1
        for item in data.get("grating_infos", []):
            if not isinstance(item, dict):
                continue
            index = int(item.get("index", 0))
            info = item.get("info", {})
            if isinstance(info, dict):
                lines = info.get("lines", "?")
                blaze = info.get("blaze_wavelength", "?")
                label = f"{index} — {lines} lines/mm · {blaze} nm blaze"
                detail = (
                    f"Groove density: {lines} lines/mm\n"
                    f"Blaze: {blaze} nm\n"
                    f"Home: {info.get('home', '—')}\n"
                    f"Offset: {info.get('offset', '—')} steps"
                )
            else:
                label = f"Grating {index} — {info}"
                detail = str(info)
            self.grating.addItem(label, index)
            self.grating.setItemData(
                self.grating.count() - 1,
                detail,
                Qt.ItemDataRole.ToolTipRole,
            )
            if index == current:
                selected_index = self.grating.count() - 1
        if not self.grating.count():
            self.grating.addItem(f"Grating {current}", current)
            selected_index = 0
        self.grating.setCurrentIndex(max(0, selected_index))
        self.grating.blockSignals(False)
        self._update_grating_detail()
        if data.get("input_slit_width_um") is not None:
            self.slit.setValue(float(data["input_slit_width_um"]))
        self._slit_present = bool(data.get("input_slit_present", False))
        if data.get("shutter_mode") in {"closed", "opened", "bnc"}:
            self.shutter.setCurrentText(str(data["shutter_mode"]))
        self._shutter_present = bool(data.get("shutter_present", False))
        if data.get("output_port") in {"direct", "side"}:
            self.output_port.setCurrentText(str(data["output_port"]))
        self._output_flipper_present = bool(data.get("output_flipper_present", False))
        self._update_enabled()

    def _populate_detector(self, data: dict[str, Any]) -> None:
        temperature_range = data.get("temperature_range_c")
        if isinstance(temperature_range, (list, tuple)) and len(temperature_range) == 2:
            self.temperature_target.setRange(
                float(temperature_range[0]), float(temperature_range[1])
            )
        if data.get("temperature_setpoint_c") is not None:
            self.temperature_target.setValue(float(data["temperature_setpoint_c"]))
        if data.get("cooler_on") is not None:
            self.cooler.setChecked(bool(data["cooler_on"]))
        if data.get("fan_mode") in {"full", "low", "off"}:
            self.fan.setCurrentText(str(data["fan_mode"]))
        if data.get("read_mode") in {"fvb", "image"}:
            self.read_mode.setCurrentIndex(self.read_mode.findData(data["read_mode"]))
        detector = data.get("detector_size")
        if isinstance(detector, (list, tuple)) and len(detector) == 2:
            width, height = int(detector[0]), int(detector[1])
            self.roi_hstart.setMaximum(max(0, width - 1))
            self.roi_hend.setMaximum(width)
            self.roi_vstart.setMaximum(max(0, height - 1))
            self.roi_vend.setMaximum(height)
            if self.roi_hend.value() == 0:
                self.roi_hend.setValue(width)
            if self.roi_vend.value() == 0:
                self.roi_vend.setValue(height)
        roi = data.get("roi")
        if isinstance(roi, (list, tuple)) and len(roi) >= 6:
            for widget, value in zip(
                (self.roi_hstart, self.roi_hend, self.roi_vstart, self.roi_vend, self.hbin, self.vbin),
                roi[:6],
            ):
                widget.setValue(int(value))
        self._update_enabled()

    def _populate_status(self, data: dict[str, Any]) -> None:
        self.identity.setText(
            f"Camera {str(data.get('camera_role', '')).upper()} · "
            f"S/N {data.get('camera_serial', '—')} · detector {data.get('detector_size', '—')}\n"
            f"Shamrock S/N {data.get('spectrograph_serial', '—')}"
        )
        self.cooling_status.setText(
            f"Detector {data.get('temperature_c', '—')} °C · "
            f"{data.get('temperature_status', 'unknown')} · "
            f"cooler {'on' if data.get('cooler_on') else 'off'}"
        )
        cal_range = data.get("calibration_range_nm")
        if data.get("calibration_pixel_count") and cal_range:
            self.calibration.setText(
                f"Stored calibration: {data['calibration_pixel_count']} pixels · "
                f"{float(cal_range[0]):.2f} to {float(cal_range[1]):.2f} nm\n"
                f"Source: {data.get('calibration_source', 'Shamrock stored coefficients')}"
            )
        else:
            self.calibration.setText(
                "Stored calibration: unavailable (see diagnostics for readback details)"
            )

    @Slot()
    def _update_grating_detail(self) -> None:
        detail = self.grating.itemData(
            self.grating.currentIndex(), Qt.ItemDataRole.ToolTipRole
        )
        limits = self._snapshot.get("wavelength_limits_nm")
        range_text = ""
        if isinstance(limits, (list, tuple)) and len(limits) == 2:
            range_text = f"\nCurrent wavelength range: {float(limits[0]):.1f}–{float(limits[1]):.1f} nm"
        self.grating_info.setText(f"{detail or 'No grating details available'}{range_text}")

    @Slot(object)
    def _on_applied(self, snapshot: object) -> None:
        self._on_status(snapshot)
        self.status_changed.emit("Andor controls applied and verified")

    @Slot(str)
    def _on_error(self, _message: str) -> None:
        self.refresh_button.setEnabled(self._available and not self._locked)
        self.apply_button.setEnabled(self._available and not self._locked)

    @Slot()
    def show_diagnostics(self) -> None:
        if not self._snapshot:
            QMessageBox.information(self, "Andor diagnostics", "No status has been read yet.")
            return
        keys = (
            "camera_role", "camera_serial", "detector_size", "spectrograph_serial",
            "wavelength_nm", "grating", "input_slit_width_um", "shutter_mode",
            "output_port", "temperature_c", "temperature_status", "cooler_on",
            "fan_mode", "read_mode", "roi", "calibration_source",
            "calibration_pixel_count", "calibration_range_nm",
        )
        lines = [f"{key}: {self._snapshot.get(key, '—')}" for key in keys]
        errors = self._snapshot.get("readback_errors", {})
        if isinstance(errors, dict) and errors:
            lines.extend(["", "Readback errors:"])
            lines.extend(f"• {key}: {value}" for key, value in errors.items())
        QMessageBox.information(self, "Andor diagnostics", "\n".join(lines))
