# ui/settings_panel.py
# ──────────────────────────────────────────────────────────────────────────────
# LF6 settings + output path + filename pattern panel.
#
# Responsibilities:
#   - Exposure, centre wavelength, accumulations  →  lf6_ctrl.apply_settings()
#   - BASE_OUT folder picker                       →  cfg.filename.base_out
#   - Filename pattern editor                      →  cfg.filename.pattern
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
    QDoubleSpinBox, QSpinBox, QLineEdit, QFileDialog, QFormLayout,
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
        root.setSpacing(8)

        root.addWidget(self._build_lf6_group())
        root.addWidget(self._build_output_group())
        root.addWidget(self._build_filename_group())

        # Save config button
        self._save_btn = QPushButton("Save config to disk")
        self._save_btn.setToolTip("Persists all settings to config.json")
        root.addWidget(self._save_btn)

        self._status_lbl = QLabel("")
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._status_lbl)

        root.addStretch()

    def _build_lf6_group(self) -> QGroupBox:
        grp = QGroupBox("LF6 Spectrometer Settings")
        form = QFormLayout(grp)

        self._exposure = QDoubleSpinBox()
        self._exposure.setRange(1.0, 600_000.0)
        self._exposure.setDecimals(1)
        self._exposure.setSingleStep(100.0)
        self._exposure.setSuffix(" ms")
        form.addRow("Exposure:", self._exposure)

        self._center = QDoubleSpinBox()
        self._center.setRange(200.0, 2000.0)
        self._center.setDecimals(1)
        self._center.setSingleStep(1.0)
        self._center.setSuffix(" nm")
        form.addRow("Centre λ:", self._center)

        self._accum = QSpinBox()
        self._accum.setRange(1, 1000)
        self._accum.setSuffix(" frame(s)")
        form.addRow("Accumulations:", self._accum)

        self._apply_btn = QPushButton("Apply to LF6")
        self._apply_btn.setEnabled(False)
        form.addRow("", self._apply_btn)

        self._lf6_status = QLabel("")
        form.addRow("", self._lf6_status)

        return grp

    def _build_output_group(self) -> QGroupBox:
        grp = QGroupBox("Output Path")
        lay = QVBoxLayout(grp)

        row = QHBoxLayout()
        self._base_out_edit = QLineEdit()
        self._base_out_edit.setPlaceholderText("Base output folder…")
        self._browse_btn = QPushButton("Browse…")
        self._browse_btn.setFixedWidth(72)
        row.addWidget(self._base_out_edit)
        row.addWidget(self._browse_btn)
        lay.addLayout(row)

        return grp

    def _build_filename_group(self) -> QGroupBox:
        grp = QGroupBox("Filename Pattern")
        lay = QVBoxLayout(grp)

        self._pattern_edit = QLineEdit()
        self._pattern_edit.setPlaceholderText("${sample}$~${tag}$~…")
        lay.addWidget(self._pattern_edit)

        hint = QLabel(
            "Vars: {sample} {tag} {laser_nm} {power_uw} {exp_s} "
            "{epf} {center_nm} {rot_block} {cond_block}"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray; font-size: 10px;")
        lay.addWidget(hint)

        self._reset_pattern_btn = QPushButton("Reset to default")
        lay.addWidget(self._reset_pattern_btn)

        return grp

    # ── wire ──────────────────────────────────────────────────────────────────

    def _wire(self):
        self._apply_btn.clicked.connect(self._on_apply_lf6)
        self._browse_btn.clicked.connect(self._on_browse)
        self._save_btn.clicked.connect(self._on_save)
        self._reset_pattern_btn.clicked.connect(self._on_reset_pattern)

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
        self._pattern_edit.textChanged.connect(
            lambda t: setattr(cfg.filename, "pattern", t)
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _load_from_cfg(self):
        """Populate widgets from the current cfg singleton."""
        self._exposure.setValue(cfg.lf6.exposure_ms)
        self._center.setValue(cfg.lf6.center_nm)
        self._accum.setValue(cfg.lf6.accumulations)
        self._base_out_edit.setText(cfg.filename.base_out)
        self._pattern_edit.setText(cfg.filename.pattern)

    # ── slots ─────────────────────────────────────────────────────────────────

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
    def _on_reset_pattern(self):
        from dataclasses import fields
        from utils.config import FilenameConfig
        default = FilenameConfig().pattern
        self._pattern_edit.setText(default)
