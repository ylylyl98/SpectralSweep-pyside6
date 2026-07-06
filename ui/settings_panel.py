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
    QSizePolicy,
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
        root = QVBoxLayout(self)
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

    def _build_lf6_group(self) -> QGroupBox:
        grp = QGroupBox("LF6 Spectrometer Settings")
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

        self._apply_btn = QPushButton("Apply to LF6")
        self._apply_btn.setEnabled(False)
        self._apply_btn.setMaximumWidth(130)
        form.addRow("", self._apply_btn)

        self._lf6_status = QLabel("")
        self._lf6_status.setStyleSheet("font-size: 11px;")
        form.addRow("", self._lf6_status)

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
