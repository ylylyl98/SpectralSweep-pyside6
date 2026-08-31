# ui/settings_panel.py
# ──────────────────────────────────────────────────────────────────────────────
# LF6 settings + output path + filename defaults panel.
#
# Responsibilities:
#   - Exposure, centre wavelength, accumulations  →  lf6_ctrl.apply_settings()
#   - BASE_OUT folder picker                       →  cfg.filename.base_out
#   - Filename defaults                            →  cfg.filename.temperature /
#                                                    cfg.filename.measurement_mode /
#                                                    cfg.filename.power_coefficient
#   - "Save config" button                         →  cfg.save()
#
# Rules:
#   - No instrument state stored here.
#   - importlib.reload() safe: lf6_ctrl injected, cfg is a module singleton.
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QDoubleSpinBox, QSpinBox, QLineEdit, QFileDialog, QFormLayout, QComboBox,
    QSizePolicy, QCheckBox, QScrollArea,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from utils.config import cfg


class SettingsPanel(QWidget):
    """
    Injected dependency: lf6_ctrl (LF6Controller).
    Can be constructed with lf6_ctrl=None for layout preview.

    Usage:
        panel = SettingsPanel(lf6_ctrl=lf6)
    """

    def __init__(self, lf6_ctrl=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._ctrl = lf6_ctrl
        self._build()
        self._wire()
        self._load_from_cfg()

    # ── build ─────────────────────────────────────────────────────────────────

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")
        body = QWidget()
        root = QVBoxLayout(body)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        root.addWidget(self._build_lf6_group())
        root.addWidget(self._build_output_group())
        root.addWidget(self._build_filename_group())

        # Save config button
        self._save_btn = QPushButton("Save config to disk")
        self._save_btn.setToolTip("Persists all settings to config.json")
        self._save_btn.setMinimumHeight(28)
        self._save_btn.setMaximumWidth(200)
        self._save_btn.setStyleSheet(
            "QPushButton { font-weight: 600; border-color: #90a8c0; }"
            "QPushButton:hover { border-color: #5a82a8; }"
        )
        root.addWidget(self._save_btn)

        self._status_lbl = QLabel("")
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._status_lbl.setStyleSheet("font-size: 11px;")
        root.addWidget(self._status_lbl)

        root.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll)

    def _build_lf6_group(self) -> QGroupBox:
        grp = QGroupBox("Shared Spectrometer Settings")
        form = QFormLayout(grp)
        form.setContentsMargins(8, 10, 8, 8)
        form.setSpacing(6)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)

        self._exposure = QDoubleSpinBox()
        self._exposure.setRange(1.0, 600_000.0)
        self._exposure.setDecimals(1)
        self._exposure.setSingleStep(100.0)
        self._exposure.setSuffix(" ms")
        self._exposure.setFixedWidth(130)
        form.addRow("Exposure:", self._exposure)

        self._center = QDoubleSpinBox()
        self._center.setRange(200.0, 2000.0)
        self._center.setDecimals(1)
        self._center.setSingleStep(1.0)
        self._center.setSuffix(" nm")
        self._center.setFixedWidth(130)
        form.addRow("Centre λ:", self._center)

        self._accum = QSpinBox()
        self._accum.setRange(1, 1000)
        self._accum.setSuffix(" frame(s)")
        self._accum.setFixedWidth(130)
        form.addRow("Accumulations:", self._accum)

        self._apply_btn = QPushButton("Apply to spectrometer")
        self._apply_btn.setEnabled(False)
        self._apply_btn.setMaximumWidth(130)
        form.addRow("", self._apply_btn)

        self._lf6_status = QLabel("")
        self._lf6_status.setStyleSheet("font-size: 11px;")
        form.addRow("", self._lf6_status)

        self._andor_sdk_dir = QLineEdit()
        self._andor_sdk_dir.setMinimumWidth(320)
        form.addRow("Andor SDK2 DLL folder:", self._andor_sdk_dir)

        self._andor_shamrock_dir = QLineEdit()
        self._andor_shamrock_dir.setMinimumWidth(320)
        form.addRow("Shamrock DLL folder:", self._andor_shamrock_dir)

        self._andor_si_index = QSpinBox()
        self._andor_si_index.setRange(0, 31)
        self._andor_si_serial = QLineEdit()
        self._andor_si_serial.setPlaceholderText("optional serial override")
        si_row = QHBoxLayout()
        si_row.addWidget(QLabel("index"))
        si_row.addWidget(self._andor_si_index)
        si_row.addWidget(QLabel("serial"))
        si_row.addWidget(self._andor_si_serial)
        form.addRow("Andor Si camera:", si_row)

        self._andor_ingaas_index = QSpinBox()
        self._andor_ingaas_index.setRange(0, 31)
        self._andor_ingaas_serial = QLineEdit()
        self._andor_ingaas_serial.setPlaceholderText("optional serial override")
        ingaas_row = QHBoxLayout()
        ingaas_row.addWidget(QLabel("index"))
        ingaas_row.addWidget(self._andor_ingaas_index)
        ingaas_row.addWidget(QLabel("serial"))
        ingaas_row.addWidget(self._andor_ingaas_serial)
        form.addRow("Andor InGaAs camera:", ingaas_row)

        self._andor_spec_index = QSpinBox()
        self._andor_spec_index.setRange(0, 31)
        form.addRow("Shamrock index:", self._andor_spec_index)

        self._andor_si_temperature = QDoubleSpinBox()
        self._andor_si_temperature.setRange(-120.0, 30.0)
        self._andor_si_temperature.setDecimals(1)
        self._andor_si_temperature.setSuffix(" °C")
        self._andor_si_cooler = QCheckBox("cool on connect")
        self._andor_si_fan = QComboBox()
        self._andor_si_fan.addItems(["full", "low", "off"])
        self._andor_si_output_port = QComboBox()
        self._andor_si_output_port.addItems(["unchanged", "direct", "side"])
        si_operating = QHBoxLayout()
        si_operating.addWidget(self._andor_si_temperature)
        si_operating.addWidget(self._andor_si_cooler)
        si_operating.addWidget(QLabel("fan"))
        si_operating.addWidget(self._andor_si_fan)
        si_operating.addWidget(QLabel("output"))
        si_operating.addWidget(self._andor_si_output_port)
        form.addRow("Si operating profile:", si_operating)

        self._andor_ingaas_temperature = QDoubleSpinBox()
        self._andor_ingaas_temperature.setRange(-120.0, 30.0)
        self._andor_ingaas_temperature.setDecimals(1)
        self._andor_ingaas_temperature.setSuffix(" °C")
        self._andor_ingaas_cooler = QCheckBox("cool on connect")
        self._andor_ingaas_fan = QComboBox()
        self._andor_ingaas_fan.addItems(["full", "low", "off"])
        self._andor_ingaas_output_port = QComboBox()
        self._andor_ingaas_output_port.addItems(["unchanged", "direct", "side"])
        ingaas_operating = QHBoxLayout()
        ingaas_operating.addWidget(self._andor_ingaas_temperature)
        ingaas_operating.addWidget(self._andor_ingaas_cooler)
        ingaas_operating.addWidget(QLabel("fan"))
        ingaas_operating.addWidget(self._andor_ingaas_fan)
        ingaas_operating.addWidget(QLabel("output"))
        ingaas_operating.addWidget(self._andor_ingaas_output_port)
        form.addRow("InGaAs operating profile:", ingaas_operating)

        self._andor_safe_disconnect_temperature = QDoubleSpinBox()
        self._andor_safe_disconnect_temperature.setRange(-20.0, 30.0)
        self._andor_safe_disconnect_temperature.setDecimals(1)
        self._andor_safe_disconnect_temperature.setSuffix(" °C")
        self._andor_safe_disconnect_temperature.setToolTip(
            "Warm-up disconnect waits until the detector reaches at least this temperature."
        )
        form.addRow(
            "Safe disconnect temperature:",
            self._andor_safe_disconnect_temperature,
        )

        self._andor_shutter = QComboBox()
        self._andor_shutter.addItems(["auto", "open", "closed"])
        form.addRow("Camera shutter mode:", self._andor_shutter)

        self._andor_shamrock_shutter = QComboBox()
        self._andor_shamrock_shutter.addItems(
            ["unchanged", "closed", "opened", "bnc"]
        )
        form.addRow("Shamrock shutter default:", self._andor_shamrock_shutter)

        self._andor_grating = QSpinBox()
        self._andor_grating.setRange(1, 32)
        form.addRow("Shamrock grating:", self._andor_grating)

        self._andor_slit = QDoubleSpinBox()
        self._andor_slit.setRange(1.0, 5000.0)
        self._andor_slit.setDecimals(1)
        self._andor_slit.setSuffix(" µm")
        form.addRow("Input slit width:", self._andor_slit)

        self._andor_invert_wl = QCheckBox("Reverse calibration axis only")
        self._andor_invert_wl.setToolTip(
            "Matches the detector orientation used in the uploaded notebook. "
            "Verify with a known spectral line before production measurements."
        )
        form.addRow("Wavelength orientation:", self._andor_invert_wl)

        self._andor_discard_first = QCheckBox("Discard first frame")
        form.addRow("Acquisition:", self._andor_discard_first)

        return grp

    def _build_output_group(self) -> QGroupBox:
        grp = QGroupBox("Output Path")
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(8, 10, 8, 8)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._base_out_edit = QLineEdit()
        self._base_out_edit.setPlaceholderText("Base output folder…")
        self._browse_btn = QPushButton("Browse…")
        self._browse_btn.setFixedWidth(80)
        row.addWidget(self._base_out_edit)
        row.addWidget(self._browse_btn)
        lay.addLayout(row)

        return grp

    def _build_filename_group(self) -> QGroupBox:
        grp = QGroupBox("Filename Defaults")
        form = QFormLayout(grp)
        form.setContentsMargins(8, 10, 8, 8)
        form.setSpacing(6)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)

        self._temp_edit = QLineEdit()
        self._temp_edit.setPlaceholderText("e.g. 6 or 1.8")
        self._temp_edit.setFixedWidth(130)
        form.addRow("Temperature:", self._temp_edit)

        self._measurement_mode = QComboBox()
        self._measurement_mode.addItems(["PL", "Ref"])
        self._measurement_mode.setFixedWidth(130)
        form.addRow("Mode:", self._measurement_mode)

        self._power_coeff = QDoubleSpinBox()
        self._power_coeff.setRange(0.000001, 1_000_000.0)
        self._power_coeff.setDecimals(6)
        self._power_coeff.setSingleStep(0.1)
        self._power_coeff.setFixedWidth(130)
        self._power_coeff.setToolTip(
            "Multiplier applied to manual or PM100D power in generated filenames. "
            "Example: 1100 µW × 2 = 2200 µW."
        )
        form.addRow("Power coefficient:", self._power_coeff)

        hint = QLabel(
            "These values are the defaults used by the structured filename builder in the Dual Gate tab."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888888; font-size: 10px;")
        form.addRow("", hint)

        self._reset_filename_btn = QPushButton("Reset filename defaults")
        self._reset_filename_btn.setMaximumWidth(180)
        form.addRow("", self._reset_filename_btn)
        return grp

    # ── wire ──────────────────────────────────────────────────────────────────

    def _wire(self):
        self._apply_btn.clicked.connect(self._on_apply_lf6)
        self._browse_btn.clicked.connect(self._on_browse)
        self._save_btn.clicked.connect(self._on_save)
        self._reset_filename_btn.clicked.connect(self._on_reset_filename_defaults)

        # Enable Apply button only when LF6 connects
        if self._ctrl is not None:
            self._ctrl.connected.connect(
                lambda _: self._apply_btn.setEnabled(True)
            )
            self._ctrl.disconnected.connect(
                lambda: self._apply_btn.setEnabled(False)
            )
            self._ctrl.settings_applied.connect(
                lambda: self._lf6_status.setText("Applied ✓")
            )
            self._ctrl.error.connect(
                lambda msg: self._lf6_status.setText(f"Error: {msg[:50]}")
            )

        # Keep cfg in sync as user edits widgets (without saving)
        self._exposure.valueChanged.connect(
            lambda v: setattr(cfg.lf6, "exposure_ms", v)
        )
        self._center.valueChanged.connect(
            lambda v: setattr(cfg.lf6, "center_nm", v)
        )
        self._accum.valueChanged.connect(
            lambda v: setattr(cfg.lf6, "accumulations", v)
        )
        self._andor_sdk_dir.textChanged.connect(
            lambda value: setattr(cfg.lf6, "andor_sdk2_dll_dir", value.strip())
        )
        self._andor_shamrock_dir.textChanged.connect(
            lambda value: setattr(cfg.lf6, "andor_shamrock_dll_dir", value.strip())
        )
        self._andor_si_index.valueChanged.connect(
            lambda value: setattr(cfg.lf6, "andor_si_camera_index", int(value))
        )
        self._andor_ingaas_index.valueChanged.connect(
            lambda value: setattr(cfg.lf6, "andor_ingaas_camera_index", int(value))
        )
        self._andor_si_serial.textChanged.connect(
            lambda value: setattr(cfg.lf6, "andor_si_serial", value.strip())
        )
        self._andor_ingaas_serial.textChanged.connect(
            lambda value: setattr(cfg.lf6, "andor_ingaas_serial", value.strip())
        )
        self._andor_spec_index.valueChanged.connect(
            lambda value: setattr(cfg.lf6, "andor_spectrograph_index", int(value))
        )
        for role in ("si", "ingaas"):
            temperature = getattr(self, f"_andor_{role}_temperature")
            cooler = getattr(self, f"_andor_{role}_cooler")
            fan = getattr(self, f"_andor_{role}_fan")
            output_port = getattr(self, f"_andor_{role}_output_port")
            temperature.valueChanged.connect(
                lambda value, role=role: setattr(
                    cfg.lf6, f"andor_{role}_temperature_c", float(value)
                )
            )
            cooler.toggled.connect(
                lambda value, role=role: setattr(
                    cfg.lf6, f"andor_{role}_cooler_on_connect", bool(value)
                )
            )
            fan.currentTextChanged.connect(
                lambda value, role=role: setattr(
                    cfg.lf6, f"andor_{role}_fan_mode", value
                )
            )
            output_port.currentTextChanged.connect(
                lambda value, role=role: setattr(
                    cfg.lf6, f"andor_{role}_output_port", value
                )
            )
        self._andor_safe_disconnect_temperature.valueChanged.connect(
            lambda value: setattr(
                cfg.lf6, "andor_safe_disconnect_temperature_c", float(value)
            )
        )
        self._andor_shutter.currentTextChanged.connect(
            lambda value: setattr(cfg.lf6, "andor_shutter_mode", value)
        )
        self._andor_shamrock_shutter.currentTextChanged.connect(
            lambda value: setattr(cfg.lf6, "andor_shamrock_shutter_mode", value)
        )
        self._andor_grating.valueChanged.connect(
            lambda value: setattr(cfg.lf6, "andor_grating", int(value))
        )
        self._andor_slit.valueChanged.connect(
            lambda value: setattr(cfg.lf6, "andor_slit_width_um", float(value))
        )
        self._andor_invert_wl.toggled.connect(
            lambda value: setattr(cfg.lf6, "andor_invert_wavelength_axis", bool(value))
        )
        self._andor_discard_first.toggled.connect(
            lambda value: setattr(cfg.lf6, "andor_discard_first", bool(value))
        )
        self._base_out_edit.textChanged.connect(
            lambda t: setattr(cfg.filename, "base_out", t)
        )
        self._temp_edit.textChanged.connect(
            lambda t: setattr(cfg.filename, "temperature", t)
        )
        self._measurement_mode.currentTextChanged.connect(
            lambda t: setattr(cfg.filename, "measurement_mode", t)
        )
        self._power_coeff.valueChanged.connect(
            lambda v: setattr(cfg.filename, "power_coefficient", float(v))
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _load_from_cfg(self):
        """Populate widgets from the current cfg singleton."""
        self._exposure.setValue(cfg.lf6.exposure_ms)
        self._center.setValue(cfg.lf6.center_nm)
        self._accum.setValue(cfg.lf6.accumulations)
        self._andor_sdk_dir.setText(cfg.lf6.andor_sdk2_dll_dir)
        self._andor_shamrock_dir.setText(cfg.lf6.andor_shamrock_dll_dir)
        self._andor_si_index.setValue(int(cfg.lf6.andor_si_camera_index))
        self._andor_ingaas_index.setValue(int(cfg.lf6.andor_ingaas_camera_index))
        self._andor_si_serial.setText(cfg.lf6.andor_si_serial)
        self._andor_ingaas_serial.setText(cfg.lf6.andor_ingaas_serial)
        self._andor_spec_index.setValue(int(cfg.lf6.andor_spectrograph_index))
        for role in ("si", "ingaas"):
            getattr(self, f"_andor_{role}_temperature").setValue(
                float(getattr(cfg.lf6, f"andor_{role}_temperature_c"))
            )
            getattr(self, f"_andor_{role}_cooler").setChecked(
                bool(getattr(cfg.lf6, f"andor_{role}_cooler_on_connect"))
            )
            getattr(self, f"_andor_{role}_fan").setCurrentText(
                str(getattr(cfg.lf6, f"andor_{role}_fan_mode"))
            )
            getattr(self, f"_andor_{role}_output_port").setCurrentText(
                str(getattr(cfg.lf6, f"andor_{role}_output_port"))
            )
        self._andor_safe_disconnect_temperature.setValue(
            float(cfg.lf6.andor_safe_disconnect_temperature_c)
        )
        self._andor_shutter.setCurrentText(str(cfg.lf6.andor_shutter_mode))
        self._andor_shamrock_shutter.setCurrentText(
            str(cfg.lf6.andor_shamrock_shutter_mode)
        )
        self._andor_grating.setValue(int(cfg.lf6.andor_grating))
        self._andor_slit.setValue(float(cfg.lf6.andor_slit_width_um))
        self._andor_invert_wl.setChecked(bool(cfg.lf6.andor_invert_wavelength_axis))
        self._andor_discard_first.setChecked(bool(cfg.lf6.andor_discard_first))
        self._base_out_edit.setText(cfg.filename.base_out)
        self._temp_edit.setText(cfg.filename.temperature)
        self._measurement_mode.setCurrentText(cfg.filename.measurement_mode)
        self._power_coeff.setValue(float(cfg.filename.power_coefficient))

    # ── slots ─────────────────────────────────────────────────────────────────

    def capture_session_state(self) -> dict:
        """Capture global defaults so edits participate in automatic saving."""
        return {
            "lf6": {
                "exposure_ms": float(self._exposure.value()),
                "center_nm": float(self._center.value()),
                "accumulations": int(self._accum.value()),
            },
            "output": {
                "base_out": self._base_out_edit.text(),
                "temperature": self._temp_edit.text(),
                "measurement_mode": self._measurement_mode.currentText(),
                "power_coefficient": float(self._power_coeff.value()),
            },
        }

    def restore_session_state(self, state: dict) -> None:
        if not isinstance(state, dict):
            return
        lf6 = state.get("lf6")
        if isinstance(lf6, dict):
            for key, spin in (
                ("exposure_ms", self._exposure),
                ("center_nm", self._center),
            ):
                try:
                    spin.setValue(float(lf6[key]))
                except (KeyError, TypeError, ValueError):
                    pass
            try:
                self._accum.setValue(int(lf6["accumulations"]))
            except (KeyError, TypeError, ValueError):
                pass
        output = state.get("output")
        if isinstance(output, dict):
            for key, edit in (
                ("base_out", self._base_out_edit),
                ("temperature", self._temp_edit),
            ):
                value = output.get(key)
                if isinstance(value, str):
                    edit.setText(value)
            mode = output.get("measurement_mode")
            if isinstance(mode, str) and self._measurement_mode.findText(mode) >= 0:
                self._measurement_mode.setCurrentText(mode)
            try:
                self._power_coeff.setValue(float(output["power_coefficient"]))
            except (KeyError, TypeError, ValueError):
                pass

    @Slot()
    def _on_apply_lf6(self):
        if self._ctrl is None:
            return
        self._lf6_status.setText("Applying…")
        self._ctrl.apply_settings(
            exposure_ms=self._exposure.value(),
            center_nm=self._center.value(),
            accumulations=int(self._accum.value()),
        )

    @Slot()
    def _on_browse(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select output folder",
            self._base_out_edit.text() or str(cfg.base_out),
        )
        if folder:
            self._base_out_edit.setText(folder)

    @Slot()
    def _on_save(self):
        cfg.save()
        self._status_lbl.setText("Saved to config.json ✓")
        self._status_lbl.setStyleSheet("color: green;")

    @Slot()
    def _on_reset_filename_defaults(self):
        from utils.config import FilenameConfig
        default = FilenameConfig()
        self._temp_edit.setText(default.temperature)
        self._measurement_mode.setCurrentText(default.measurement_mode)
        self._power_coeff.setValue(float(default.power_coefficient))
