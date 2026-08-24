"""Dedicated continuous attoDRY2100 MCD panel."""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QHeaderView, QLineEdit, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QGridLayout, QSplitter, QWidget,
)

from app.engine.mcd2100_worker import MCD2100Worker
from app.experiment_metadata import ExperimentMetadataService
from controllers.rotation_controller import RotationController
from utils.config import cfg
from utils.filename_builder import sanitize_token, make_unique_stem
from ui.mcd_panel import (
    _mcd_coordinates,
    _vtg_vbg_from_doping_efield,
    build_mcd_filename_base,
)
from utils.mcd_common import (
    GATE_MODES, MODE_DIRECT, MODE_VTG_FROM_VBG_RATIO, MODE_VBG_FROM_VTG_RATIO,
    MODE_FIXED_EFIELD, MODE_FIXED_DOPING, resolve_condition_line,
    resolve_gate_conditions, validate_gate_conditions, smu_readiness_issues,
    MODE_DOPING_EFIELD, gate_ratio_from_factors, build_condition_batch,
    parse_numeric_spec,
)


class _LightFieldRotationService:
    """Narrow optical/gate contract used by the continuous worker."""

    def __init__(self, lf6_controller, rotation_controller, rotator: str = "rot1", smu_controller=None):
        self._lf6 = lf6_controller
        self._rotation = rotation_controller
        self._rotator_name = rotator
        self._smu = smu_controller
        self._last_position = None
        self.wavelengths = None

    def prepare(self, _stop_event):
        if self._lf6 is None or not getattr(self._lf6, "is_connected", False):
            raise RuntimeError("LightField is not connected")
        ready = getattr(self._lf6, "is_ready", None)
        if ready is not None and not bool(ready() if callable(ready) else ready):
            raise RuntimeError("LightField is not ready for configuration")
        spectrometer = getattr(self._lf6, "adapter", None)
        if spectrometer is None:
            raise RuntimeError("LightField spectrometer is unavailable")
        self.wavelengths = np.asarray(
            spectrometer.calibration_wavelengths(force=True), dtype=float
        ).ravel().tolist()
        return self.wavelengths

    def configure(self, *, center_nm=None, exposure_ms=None, frames=None):
        """Use only the established LightField controller surface."""
        spectrometer = getattr(self._lf6, "adapter", None)
        if spectrometer is None:
            raise RuntimeError("LightField spectrometer is unavailable")
        if center_nm is not None:
            # MCD2100 is deliberately bound to the application's shared LF6
            # controller.  Center wavelength is frozen while LightField starts,
            # loads an experiment, or acquires; let that controller wait for its
            # evidence-backed readiness/writeability surface before writing.
            prepare = getattr(self._lf6, "configure_for_acquisition", None)
            if callable(prepare) and exposure_ms is not None and frames is not None:
                return prepare(center_nm=float(center_nm), exposure_ms=float(exposure_ms), frames=int(frames))
            prepare = getattr(spectrometer, "configure_for_acquisition", None)
            if not callable(prepare):
                raise RuntimeError("LightField acquisition preparation surface is unavailable")
            return prepare(center_nm=float(center_nm), exposure_ms=float(exposure_ms), frames=int(frames))
        if exposure_ms is not None:
            raise RuntimeError("LightField acquisition preparation requires all settings")
        if frames is not None:
            raise RuntimeError("LightField acquisition preparation requires all settings")

    def apply_gates(self, *, vtg_v, vbg_v, vbias_v, ratio, stop_cb=None):
        """Delegate gate setup when the existing SMU service exposes it."""
        service = getattr(self, "_smu", None)
        if service is None or not bool(getattr(service, "is_connected", False)):
            raise RuntimeError("SMU is not connected")
        device = getattr(service, "device", None)
        if device is None or not callable(getattr(device, "set_gates", None)):
            raise RuntimeError("SMU gate device is unavailable")
        device.set_gates(Vbg=float(vbg_v), Vtg=float(vtg_v),
                         ramp_step=cfg.ramp.step_V, delay_s=cfg.ramp.delay_s,
                         stop_cb=stop_cb)
        if float(vbias_v) != 0.0:
            set_bias = getattr(device, "set_bias", None)
            if not callable(set_bias):
                raise RuntimeError("SMU bias control is unavailable")
            set_bias(Vbias=float(vbias_v), ramp_step=cfg.ramp.vbias_step_V,
                     delay_s=cfg.ramp.delay_s, stop_cb=stop_cb)
        readback = getattr(device, "read_current_gates", None)
        return {"Vbg_V": readback()[0], "Vtg_V": readback()[1]} if callable(readback) else {}

    def move_to(self, angle):
        rotator = self._rotation.adapter(self._rotator_name) if self._rotation else None
        if rotator is None:
            raise RuntimeError(f"{self._rotator_name.upper()} is not connected")
        rotator.move_to(float(angle))

    def get_position(self):
        rotator = self._rotation.adapter(self._rotator_name) if self._rotation else None
        if rotator is None:
            raise RuntimeError(f"{self._rotator_name.upper()} is not connected")
        self._last_position = float(rotator.get_position())
        return self._last_position

    def acquire(self, angle, _label, stop_event):
        if stop_event.is_set():
            raise RuntimeError("measurement cancelled")
        spectrometer = getattr(self._lf6, "adapter", None)
        if spectrometer is None:
            raise RuntimeError("LightField spectrometer is unavailable")
        raw = spectrometer.acquire()
        if not isinstance(raw, tuple) or len(raw) < 2:
            raise RuntimeError("LightField returned an invalid spectrum")
        wavelengths = np.asarray(raw[0], dtype=float).ravel()
        counts = np.asarray(raw[1], dtype=float)
        while counts.ndim > 1:
            counts = counts.mean(axis=0)
        counts = counts.ravel()
        return wavelengths.tolist(), counts.tolist(), self._last_position

    def cleanup(self):
        return None


class _Runner(QObject):
    finished = Signal(object)
    progress = Signal(float, float, int, int)
    spectrum = Signal(object, object, str, float)
    spectrum_event = Signal(object)
    log = Signal(str)

    def __init__(self, worker):
        super().__init__()
        self.worker = worker
        setter = getattr(worker, "set_callbacks", None)
        if callable(setter):
            try:
                setter(progress=self.progress.emit, spectrum=self.spectrum.emit,
                       spectrum_event=self.spectrum_event.emit, log=self.log.emit)
            except TypeError:
                setter(progress=self.progress.emit, spectrum=self.spectrum.emit,
                       log=self.log.emit)

    @Slot()
    def run(self):
        try:
            result = self.worker.run()
        except BaseException as exc:
            result = {"status": "FAILED", "error": str(exc), "spectra_written": 0}
        self.finished.emit(result)


class MCD2100Panel(QWidget):
    terminal = Signal(object)
    run_state_changed = Signal(bool)

    def __init__(
        self,
        controller,
        lf6_ctrl=None,
        rotation_ctrl=None,
        smu_ctrl=None,
        *,
        worker_factory: Callable[..., Any] = MCD2100Worker,
        optical_factory: Optional[Callable[[], Any]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.controller = controller
        self._lf6 = lf6_ctrl
        self._rotation = rotation_ctrl
        self._smu = smu_ctrl
        self._worker_factory = worker_factory
        self._optical_factory = optical_factory
        self.thread = None
        self.worker = None
        self.runner = None
        self._connected = getattr(getattr(controller, "state", None), "name", "") in {"IDLE", "ARMED", "ACTIVE"}
        self._detached_after_completion = getattr(getattr(controller, "state", None), "name", "") == "DETACHED"
        self._last_telemetry_time = None
        self._externally_busy = False
        self._terminal_status = "Disconnected" if not self._connected else "Ready"
        self._connect_handle = None
        self._disconnect_handle = None
        self._build_ui()
        self._legacy_table_api = False
        self._wire_controller()
        self._load_config()
        if self._detached_after_completion:
            self._show_completed_detach()
        self._refresh_controls()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(self._splitter, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(560)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(4, 4, 4, 4)
        content_layout.setSpacing(6)
        scroll.setWidget(content)
        self._splitter.addWidget(scroll)

        connection = QGroupBox("attoDRY2100 connection and telemetry")
        connection_layout = QVBoxLayout(connection)
        connection_layout.setContentsMargins(8, 6, 8, 6)
        connection_layout.setSpacing(4)
        self.connection_status = QLabel("Disconnected")
        self.field_value = QLabel("N/A")
        self.temperature_value = QLabel("N/A")
        self.sample_temperature_value = QLabel("N/A")
        self.vti_temperature_value = QLabel("N/A")
        self.sample_temperature_control_value = QLabel("N/A")
        self.control_value = QLabel("N/A")
        self.quench_value = QLabel("N/A")
        self.current_target = QLabel("N/A")
        self.telemetry_note = QLabel("")
        telemetry = QGridLayout()
        telemetry.setHorizontalSpacing(10)
        telemetry.setVerticalSpacing(3)
        telemetry.addWidget(QLabel("Connection"), 0, 0)
        telemetry.addWidget(self.connection_status, 0, 1)
        telemetry.addWidget(QLabel("Field"), 0, 2)
        telemetry.addWidget(self.field_value, 0, 3)
        telemetry.addWidget(QLabel("Magnet temp"), 0, 4)
        telemetry.addWidget(self.temperature_value, 0, 5)
        telemetry.addWidget(QLabel("Field control"), 1, 0)
        telemetry.addWidget(self.control_value, 1, 1)
        telemetry.addWidget(QLabel("Quench"), 1, 2)
        telemetry.addWidget(self.quench_value, 1, 3)
        telemetry.addWidget(QLabel("Current target"), 1, 4)
        telemetry.addWidget(self.current_target, 1, 5)
        telemetry.addWidget(QLabel("Sample temp"), 2, 0)
        telemetry.addWidget(self.sample_temperature_value, 2, 1)
        telemetry.addWidget(QLabel("VTI temp"), 2, 2)
        telemetry.addWidget(self.vti_temperature_value, 2, 3)
        telemetry.addWidget(QLabel("Sample control"), 2, 4)
        telemetry.addWidget(self.sample_temperature_control_value, 2, 5)
        telemetry.setColumnStretch(1, 1)
        telemetry.setColumnStretch(3, 1)
        telemetry.setColumnStretch(5, 1)
        connection_layout.addLayout(telemetry)
        self.telemetry_note.setWordWrap(True)
        connection_layout.addWidget(self.telemetry_note)
        buttons = QHBoxLayout()
        self.connect_btn = QPushButton("Connect")
        self.disconnect_btn = QPushButton("Disconnect")
        self.refresh_btn = QPushButton("Refresh telemetry")
        buttons.addWidget(self.connect_btn)
        buttons.addWidget(self.disconnect_btn)
        buttons.addWidget(self.refresh_btn)
        connection_layout.addLayout(buttons)
        content_layout.addWidget(connection)

        workflow = QGroupBox("Continuous attoDRY2100 MCD")
        workflow_layout = QVBoxLayout(workflow)
        self._workflow_layout = workflow_layout
        workflow_layout.setContentsMargins(8, 8, 8, 8)
        workflow_layout.setSpacing(6)

        sample_group = QGroupBox("Sample / Device")
        sample_form = QFormLayout(sample_group)
        sample_form.setContentsMargins(8, 6, 8, 6)
        sample_form.setVerticalSpacing(4)
        self.start_field = QLineEdit()
        self.stop_field = QLineEdit()
        self._sample_id = QLineEdit()
        self._sample_id.setPlaceholderText("Sample ID")
        self._compact(self.start_field, 100, 140)
        self._compact(self.stop_field, 100, 140)
        self._compact(self._sample_id, 180, 300)
        sample_form.addRow("Sample ID", self._sample_id)
        workflow_layout.addWidget(sample_group)

        sweep_group = QGroupBox("Field Sweep")
        sweep_form = QFormLayout(sweep_group)
        sweep_form.setContentsMargins(8, 6, 8, 6)
        sweep_form.setVerticalSpacing(4)
        self.bidirectional = QCheckBox("Round trip (always)")
        self.bidirectional.setChecked(True)
        self.bidirectional.setEnabled(False)
        self.bidirectional.setVisible(False)
        self.round_trip_notice = QLabel("Round trip: forward + backward for every enabled gate")
        sweep_form.addRow("Start field (T)", self.start_field)
        sweep_form.addRow("Stop field (T)", self.stop_field)
        sweep_form.addRow("Sweep", self.round_trip_notice)

        rotation_group = QGroupBox("Polarization / Rotation")
        rotation_form = QFormLayout(rotation_group)
        rotation_form.setContentsMargins(8, 6, 8, 6)
        rotation_form.setVerticalSpacing(4)
        self.angles = QLineEdit("45, 135")
        self.rotator = QComboBox()
        slots = getattr(self._rotation, "logical_slots", None)
        if callable(slots):
            slots = slots()
        slots = tuple(slots or RotationController.ROTATION_SLOTS)
        self.rotator.addItems([str(slot) for slot in slots])
        self.rotator.setEditable(False)
        self._compact(self.angles, 160, 240)
        self.rotator.setMinimumWidth(120)
        self.rotator.setMaximumWidth(180)
        self.rotator.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        rotation_form.addRow("Angles (deg)", self.angles)
        rotation_form.addRow("Rotator", self.rotator)

        field_rotation_row = QHBoxLayout()
        field_rotation_row.setSpacing(6)
        field_rotation_row.addWidget(sweep_group, 1)
        field_rotation_row.addWidget(rotation_group, 1)
        workflow_layout.addLayout(field_rotation_row)

        temperature_group = QGroupBox("Sample Temperature (optional)")
        self._temperature_group = temperature_group
        temperature_form = QFormLayout(temperature_group)
        temperature_form.setContentsMargins(8, 6, 8, 6)
        temperature_form.setVerticalSpacing(4)
        self.temperature_control_enabled = QCheckBox("Control sample temperature")
        self.sample_target = self._spin(1.8, 300.0, 3)
        self.sample_ramp_rate = self._spin(0.1, 100.0, 2)
        self.temperature_tolerance = self._spin(0.001, 20.0, 3)
        self.temperature_stable = self._spin(0.0, 3600.0, 1)
        self.temperature_timeout = self._spin(1.0, 86400.0, 0)
        self.sample_target.setSuffix(" K")
        self.sample_ramp_rate.setSuffix(" K/min")
        self.temperature_tolerance.setSuffix(" K")
        self.temperature_stable.setSuffix(" s")
        self.temperature_timeout.setSuffix(" s")
        for widget in (
            self.sample_target, self.sample_ramp_rate, self.temperature_tolerance,
            self.temperature_stable, self.temperature_timeout,
        ):
            self._compact(widget, 105, 150)
        temperature_form.addRow(self.temperature_control_enabled)
        temperature_form.addRow("Target temperature", self.sample_target)
        # These remain persisted compatibility/settings attributes and retain
        # the existing stabilization semantics, but are intentionally not
        # exposed in the routine fixed-temperature workflow.
        for advanced in (
            self.sample_ramp_rate, self.temperature_tolerance,
            self.temperature_stable, self.temperature_timeout,
        ):
            advanced.setParent(temperature_group)
            advanced.setVisible(False)
        temperature_note = QLabel(
            "Uses the cryostat's automatic sample/VTI coordination; direct VTI control is not used."
        )
        temperature_note.setWordWrap(True)
        temperature_form.addRow(temperature_note)
        self.temperature_control_enabled.toggled.connect(self._update_temperature_controls)

        lightfield_group = QGroupBox("LightField")
        self._lightfield_group = lightfield_group
        lightfield_form = QFormLayout(lightfield_group)
        lightfield_form.setContentsMargins(8, 6, 8, 6)
        lightfield_form.setVerticalSpacing(4)
        self.output = QLineEdit()
        self.output.setReadOnly(True)
        self.output.setToolTip("Derived from the shared Sample ID: base output / device / mcd")
        self.stem = QLineEdit("mcd2100_continuous")
        # Output identity is derived from the shared device and gate context;
        # legacy stem remains visible for compatibility but is not editable.
        self.stem.setReadOnly(True)
        self.output.setMinimumWidth(240)
        self.output.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._compact(self.stem, 250, 450)
        self.lf_center = self._spin(200, 1200, 1)
        self.lf_exposure = self._spin(0.1, 60000, 1)
        self.lf_frames = QSpinBox(); self.lf_frames.setRange(1, 10000)
        self._compact(self.lf_center, 100, 140)
        self._compact(self.lf_exposure, 100, 140)
        self._compact(self.lf_frames, 80, 100)
        self.lf_center.setSuffix(" nm")
        self.lf_exposure.setSuffix(" ms")
        lightfield_form.addRow("Center", self.lf_center)
        lightfield_form.addRow("Exposure", self.lf_exposure)
        lightfield_form.addRow("Frames / accums", self.lf_frames)

        gate_group = QGroupBox("Gate / SMU")
        self._gate_group = gate_group
        gate_form = QFormLayout(gate_group)
        gate_form.setContentsMargins(8, 6, 8, 6)
        gate_form.setVerticalSpacing(4)
        self.apply_voltages = QCheckBox("Apply gate voltages")
        self._apply_requested = False
        self._syncing_gate_ratio = False
        self._gate_batch_provenance: list[dict[str, Any]] = []
        self.apply_voltages.setChecked(False)
        self.apply_voltages.stateChanged.connect(lambda _state: setattr(self, "_apply_requested", True))
        self.apply_voltages.setEnabled(False)
        self.apply_voltages.setVisible(False)
        self.vtg = self._spin(-1000, 1000, 4)
        self.vbg = self._spin(-1000, 1000, 4)
        self.vbias = self._spin(-1000, 1000, 4)
        self.gate_ratio = self._spin(-100, 100, 6)
        for legacy_gate_widget in (self.vtg, self.vbg, self.vbias, self.gate_ratio):
            legacy_gate_widget.setParent(gate_group)
        self.gate_ratio.setVisible(False)
        self.gate_vtg_factor = self._spin(-100, 100, 6)
        self.gate_vbg_factor = self._spin(-100, 100, 6)
        self.gate_vtg_factor.setValue(1.0)
        self.gate_vbg_factor.setValue(1.0)
        self.gate_ratio_value = QLabel("r = 1")
        for widget in (self.vtg, self.vbg, self.vbias):
            self._compact(widget, 100, 140)
            widget.setSuffix(" V")
        self._compact(self.gate_ratio, 100, 140)
        self._compact(self.gate_vtg_factor, 80, 110)
        self._compact(self.gate_vbg_factor, 80, 110)
        # The table below is authoritative.  These scalar widgets remain as
        # hidden migration attributes for legacy config/session adapters.
        for legacy_widget in (self.vtg, self.vbg, self.vbias):
            legacy_widget.setVisible(False)
        ratio_row = QHBoxLayout()
        ratio_row.addWidget(self.gate_vtg_factor)
        ratio_row.addWidget(QLabel("× Vtg  ="))
        ratio_row.addWidget(self.gate_vbg_factor)
        ratio_row.addWidget(QLabel("× Vbg"))
        ratio_row.addWidget(self.gate_ratio_value)
        ratio_row.addStretch(1)
        gate_form.addRow("Gate weighting", ratio_row)

        entry_group = QGroupBox("New gate rows")
        entry_layout = QGridLayout(entry_group)
        entry_layout.setContentsMargins(8, 6, 8, 6)
        entry_layout.setHorizontalSpacing(6)
        entry_layout.setVerticalSpacing(4)
        self._gate_entry_mode = QComboBox()
        self._gate_entry_mode.addItem("Direct Vtg / Vbg", MODE_DIRECT)
        self._gate_entry_mode.addItem("Doping / E-field", MODE_DOPING_EFIELD)
        self._gate_entry_a_label = QLabel()
        self._gate_entry_b_label = QLabel()
        self._gate_entry_a = QLineEdit("0")
        self._gate_entry_b = QLineEdit("0")
        for editor in (self._gate_entry_a, self._gate_entry_b):
            editor.setToolTip("Enter one value, comma-separated values, or start:step:stop")
        self._gate_entry_vbias = self._spin(-1000, 1000, 4)
        self._gate_entry_vbias.setSuffix(" V")
        self._gate_entry_expansion_label = QLabel("Combine")
        self._gate_entry_expansion = QComboBox()
        self._gate_entry_expansion.addItem("Match by position", "paired")
        self._gate_entry_expansion.addItem("Every combination", "grid")
        self._gate_entry_add = QPushButton("Add 1 row")
        self._gate_entry_add.setStyleSheet("font-weight: 700;")
        self._gate_entry_status = QLabel()
        self._gate_entry_status.setWordWrap(True)
        self._gate_edit_row: Optional[int] = None
        entry_layout.addWidget(QLabel("Input type"), 0, 0)
        entry_layout.addWidget(self._gate_entry_mode, 1, 0)
        entry_layout.addWidget(self._gate_entry_a_label, 0, 1)
        entry_layout.addWidget(self._gate_entry_a, 1, 1)
        entry_layout.addWidget(self._gate_entry_b_label, 0, 2)
        entry_layout.addWidget(self._gate_entry_b, 1, 2)
        entry_layout.addWidget(QLabel("Vbias"), 0, 3)
        entry_layout.addWidget(self._gate_entry_vbias, 1, 3)
        entry_layout.addWidget(self._gate_entry_expansion_label, 0, 4)
        entry_layout.addWidget(self._gate_entry_expansion, 1, 4)
        entry_layout.addWidget(self._gate_entry_add, 1, 5)
        entry_layout.addWidget(self._gate_entry_status, 2, 0, 1, 6)
        entry_layout.setColumnStretch(1, 1)
        entry_layout.setColumnStretch(2, 1)
        gate_form.addRow(entry_group)
        self._gate_mode = QComboBox()
        self._gate_mode.setVisible(False)
        self._gate_mode.addItem("Vtg / Vbg", "voltage")
        self._gate_mode.addItem("Doping / E-field", "coordinates")
        self._gate_mode.setToolTip("Choose which coordinate pair is editable in the gate table.")
        self._condition_table = QTableWidget(1, 10)
        self._condition_table.setHorizontalHeaderLabels(
            ["Use", "#", "Input type", "Vtg", "Vbg", "Vbias", "Doping",
             "E-field", "Input A", "Input B"]
        )
        self._condition_table.verticalHeader().setVisible(False)
        header = self._condition_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((38, 28, 120, 82, 82, 82, 82, 82)):
            self._condition_table.setColumnWidth(column, width)
        # Give spare width to the descriptive mode column, never to the final
        # numeric E-field column.  This keeps full labels readable without
        # recreating the oversized last-column layout.
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._condition_table.setColumnHidden(8, True)
        self._condition_table.setColumnHidden(9, True)
        self._condition_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._condition_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._condition_table.setFixedHeight(150)
        self._condition_table.setToolTip("One row per gate condition. Mode selects the canonical input line; calculated voltage and D/F columns are read-only.")
        gate_form.addRow(self._condition_table)
        condition_buttons = QHBoxLayout()
        self._add_condition_btn = self._gate_entry_add
        self._edit_condition_btn = QPushButton("Edit selected")
        self._remove_condition_btn = QPushButton("Remove")
        self._move_condition_up_btn = QPushButton("Up")
        self._move_condition_down_btn = QPushButton("Down")
        for button in (self._edit_condition_btn, self._remove_condition_btn,
                       self._move_condition_up_btn, self._move_condition_down_btn):
            condition_buttons.addWidget(button)
        gate_form.addRow(condition_buttons)

        temperature_lightfield_row = QHBoxLayout()
        temperature_lightfield_row.setSpacing(6)
        temperature_lightfield_row.addWidget(temperature_group, 1)
        temperature_lightfield_row.addWidget(lightfield_group, 1)
        workflow_layout.addLayout(temperature_lightfield_row)
        workflow_layout.addWidget(gate_group)

        output_group = QGroupBox("Output")
        output_form = QFormLayout(output_group)
        output_form.setContentsMargins(8, 6, 8, 6)
        output_form.setVerticalSpacing(4)
        self.output_browse = QPushButton("Browse…")
        self.output_browse.setEnabled(False)
        self.output_browse.setMinimumWidth(82)
        self.output_browse.clicked.connect(self._browse_output)
        output_row = QHBoxLayout()
        output_row.setContentsMargins(0, 0, 0, 0)
        output_row.setSpacing(6)
        output_row.addWidget(self.output, 1)
        output_row.addWidget(self.output_browse)
        output_form.addRow("Output directory", output_row)
        output_form.addRow("Filename stem", self.stem)
        workflow_layout.addWidget(output_group)
        content_layout.addWidget(workflow)

        run_group = QGroupBox("Run control")
        controls = QHBoxLayout(run_group)
        controls.setContentsMargins(8, 6, 8, 6)
        controls.setSpacing(8)
        self.start_btn = QPushButton("Start MCD 2100")
        self.stop_btn = QPushButton("Stop / Cancel")
        for button in (self.start_btn, self.stop_btn):
            button.setMinimumHeight(36)
        self.start_btn.setStyleSheet("font-weight: 700; background: #e6f3e8;")
        self.stop_btn.setStyleSheet("font-weight: 700; background: #f9e5e5;")
        controls.addWidget(self.start_btn)
        controls.addWidget(self.stop_btn)
        content_layout.addWidget(run_group)

        status_group = QGroupBox("Status / Progress / Log")
        status_layout = QVBoxLayout(status_group)
        status_layout.setContentsMargins(8, 6, 8, 8)
        status_layout.setSpacing(4)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.status = QLabel(self._terminal_status)
        self.progress = QLabel("0 spectra")
        self.current_field = QLabel("N/A")
        self.direction_value = QLabel("N/A")
        self.polarization_value = QLabel("N/A")
        telemetry = QGridLayout()
        telemetry.setHorizontalSpacing(12)
        telemetry.setVerticalSpacing(3)
        telemetry.addWidget(QLabel("Current field"), 0, 0)
        telemetry.addWidget(self.current_field, 0, 1)
        telemetry.addWidget(QLabel("Direction"), 0, 2)
        telemetry.addWidget(self.direction_value, 0, 3)
        telemetry.addWidget(QLabel("Polarization"), 1, 0)
        telemetry.addWidget(self.polarization_value, 1, 1)
        telemetry.setColumnStretch(1, 1)
        telemetry.setColumnStretch(3, 1)
        status_layout.addWidget(self.progress_bar)
        status_layout.addWidget(self.status)
        status_layout.addWidget(self.progress)
        status_layout.addLayout(telemetry)
        self.error_display = QPlainTextEdit()
        self.error_display.setReadOnly(True)
        self.error_display.setMinimumHeight(60)
        self.error_display.setMaximumHeight(120)
        status_layout.addWidget(self.error_display)
        content_layout.addWidget(status_group)
        content_layout.addStretch(1)

        display = QWidget()
        display_layout = QVBoxLayout(display)
        display_layout.setContentsMargins(8, 6, 8, 6)
        display_layout.setSpacing(6)
        display_header = QHBoxLayout()
        display_header.addWidget(QLabel("Live spectrum"))
        display_header.addStretch(1)
        self._clear_log_btn = QPushButton("Clear log")
        display_header.addWidget(self._clear_log_btn)
        display_layout.addLayout(display_header)
        self._plot = pg.PlotWidget()
        self._plot.setMinimumHeight(180)
        self._plot.setLabel("bottom", "Wavelength", units="nm")
        self._plot.setLabel("left", "Intensity", units="counts")
        self._curve_a = self._plot.plot(pen=pg.mkPen("#2374c6", width=1.5), name="A")
        self._curve_b = self._plot.plot(pen=pg.mkPen("#c06020", width=1.5), name="B")
        self._plot.addLegend()
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(2000)
        self._plot_log_splitter = QSplitter(Qt.Orientation.Vertical)
        self._plot_log_splitter.addWidget(self._plot)
        self._plot_log_splitter.addWidget(self._log)
        self._plot_log_splitter.setStretchFactor(0, 1)
        self._plot_log_splitter.setStretchFactor(1, 1)
        display_layout.addWidget(self._plot_log_splitter, 1)
        self._splitter.addWidget(display)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([760, 440])

        self.connect_btn.clicked.connect(self.connect_instrument)
        self.disconnect_btn.clicked.connect(self.disconnect_instrument)
        self.refresh_btn.clicked.connect(self.refresh_telemetry)
        self.start_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.stop)
        self.terminal.connect(self._on_terminal)
        self._clear_log_btn.clicked.connect(self._log.clear)
        self._sample_id.textChanged.connect(self._update_derived_output)
        self.gate_ratio.valueChanged.connect(self._on_gate_ratio_changed)
        self.gate_vtg_factor.valueChanged.connect(self._on_gate_factors_changed)
        self.gate_vbg_factor.valueChanged.connect(self._on_gate_factors_changed)
        self._gate_mode.currentIndexChanged.connect(self._update_condition_editable)
        self._condition_table.itemChanged.connect(self._on_condition_item_changed)
        for signal in (
            self._gate_entry_mode.currentIndexChanged,
            self._gate_entry_a.textChanged, self._gate_entry_b.textChanged,
            self._gate_entry_vbias.valueChanged,
            self._gate_entry_expansion.currentIndexChanged,
        ):
            signal.connect(self._update_gate_entry)
        self._gate_entry_add.clicked.connect(self._commit_gate_entry)
        self._edit_condition_btn.clicked.connect(self._edit_selected_condition)
        self._remove_condition_btn.clicked.connect(self._remove_condition_row)
        self._move_condition_up_btn.clicked.connect(lambda: self._move_condition(-1))
        self._move_condition_down_btn.clicked.connect(lambda: self._move_condition(1))
        self._update_gate_entry()

    @staticmethod
    def _compact(widget, minimum: int, maximum: int):
        """Keep short controls readable without letting them consume the pane."""
        widget.setMinimumWidth(int(minimum))
        widget.setMaximumWidth(int(maximum))
        widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        return widget

    @Slot()
    def _browse_output(self):
        current = self.output.text().strip() or str(cfg.base_out)
        selected = QFileDialog.getExistingDirectory(self, "Select output directory", current)
        if selected:
            self.output.setText(selected)

    @staticmethod
    def _spin(minimum, maximum, decimals):
        widget = QDoubleSpinBox()
        widget.setRange(minimum, maximum)
        widget.setDecimals(decimals)
        return widget

    def _update_temperature_controls(self, *_args) -> None:
        enabled = self.temperature_control_enabled.isChecked() and self.worker is None
        for widget in (
            self.sample_target, self.sample_ramp_rate, self.temperature_tolerance,
            self.temperature_stable, self.temperature_timeout,
        ):
            widget.setEnabled(enabled)

    def _wire_controller(self):
        for name, slot in (
            ("connected", self._on_connected), ("disconnected", self._on_disconnected),
            ("state_changed", self._on_controller_state),
            ("snapshot_updated", self._on_snapshot), ("error", self._show_error),
        ):
            signal = getattr(self.controller, name, None)
            if signal is not None and hasattr(signal, "connect"):
                signal.connect(slot)

    def _load_config(self):
        settings = cfg.mcd2100
        self._sample_id.setText(settings.sample_id)
        self.start_field.setText(f"{settings.start_field_t:g}")
        self.stop_field.setText(f"{settings.stop_field_t:g}")
        self.angles.setText(", ".join(f"{value:g}" for value in settings.angles_deg))
        self.rotator.setCurrentText(settings.rotator)
        self.lf_center.setValue(settings.lf_center_nm)
        self.lf_exposure.setValue(settings.lf_exposure_ms)
        self.lf_frames.setValue(settings.lf_frames)
        self.vtg.setValue(settings.vtg_v); self.vbg.setValue(settings.vbg_v)
        self.vbias.setValue(settings.vbias_v)
        tg_factor = float(getattr(settings, "gate_vtg_factor", 1.0))
        bg_factor = float(getattr(settings, "gate_vbg_factor", settings.gate_ratio))
        # Legacy configurations only had canonical r; prefer it when the new
        # factor fields are still at their untouched defaults.
        if tg_factor == 1.0 and bg_factor == 1.0 and float(settings.gate_ratio) != 1.0:
            bg_factor = float(settings.gate_ratio)
        self.gate_vtg_factor.setValue(tg_factor)
        self.gate_vbg_factor.setValue(bg_factor)
        self._on_gate_factors_changed()
        self._seed_condition_table(settings.conditions or [{
            "enabled": True, "vtg_v": settings.vtg_v,
            "vbg_v": settings.vbg_v, "vbias_v": settings.vbias_v,
        }])
        self._gate_batch_provenance = list(getattr(settings, "gate_batches", []) or [])
        self._update_condition_editable()
        if hasattr(self, "_gate_entry_mode"):
            self._update_gate_entry()
        self._update_derived_output()
        self.stem.setText("mcd2100_continuous")
        self.temperature_control_enabled.setChecked(settings.temperature_control_enabled)
        self.sample_target.setValue(settings.sample_target_k)
        self.sample_ramp_rate.setValue(settings.sample_ramp_rate_k_per_min)
        self.temperature_tolerance.setValue(settings.temperature_tolerance_k)
        self.temperature_stable.setValue(settings.temperature_stable_s)
        self.temperature_timeout.setValue(settings.temperature_timeout_s)
        self._update_temperature_controls()

    def _update_derived_output(self, *_args) -> None:
        device = sanitize_token(self._sample_id.text())
        if device:
            self.output.setText(str(Path(cfg.filename.base_out) / device / "mcd"))

    def _condition_rows(self) -> list[dict[str, Any]]:
        rows = []
        for row in range(self._condition_table.rowCount()):
            check = self._condition_table.item(row, 0)
            mode_widget = self._condition_table.cellWidget(row, 2)
            rows.append({
                "enabled": bool(check and check.checkState() == Qt.CheckState.Checked),
                "row_number": row + 1,
                "mode": mode_widget.currentData() if mode_widget is not None else MODE_DIRECT,
                "input_a": self._cell_value(row, 8), "input_b": self._cell_value(row, 9),
                "vbias_v": self._cell_value(row, 5),
            })
        return resolve_gate_conditions(rows, self._gate_ratio())

    def _gate_ratio(self) -> float:
        return gate_ratio_from_factors(
            self.gate_vtg_factor.value(), self.gate_vbg_factor.value()
        )

    def _row_value(self, row: int, column: int) -> float:
        if getattr(self, "_legacy_table_api", False) and column in (4, 5):
            column = 6 if column == 4 else 7
        item = self._condition_table.item(row, column)
        try:
            return float(item.text()) if item is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _cell_value(self, row: int, column: int) -> float:
        item = self._condition_table.item(row, column)
        try:
            return float(item.text()) if item is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _set_row_value(self, row: int, column: int, value: float) -> None:
        if column in (1, 2):
            self._legacy_table_api = True
            column = 8 if column == 1 else 9
        item = self._condition_table.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            self._condition_table.setItem(row, column, item)
        item.setText(f"{float(value):.6g}")

    def _set_row_error(self, row: int, message: str = "") -> None:
        for column in range(2, 10):
            item = self._condition_table.item(row, column)
            if item is not None:
                item.setBackground(QColor("#ffc7ce") if message else QColor(Qt.GlobalColor.transparent))
                if message or column not in (3, 4):
                    item.setToolTip(message)

    def _mode_combo(self, row: int, mode: str) -> QComboBox:
        combo = QComboBox()
        labels = {
            MODE_DIRECT: ("Direct Vtg / Vbg", "Enter Vtg and Vbg directly."),
            MODE_DOPING_EFIELD: (
                "Doping / E-field",
                "Enter Doping and E-field; Vtg and Vbg are calculated from the gate weighting.",
            ),
            MODE_VTG_FROM_VBG_RATIO: ("Vtg from Vbg × q", "Input A=Vbg; Input B=q; Vtg=q×Vbg."),
            MODE_VBG_FROM_VTG_RATIO: ("Vbg from Vtg × q", "Legacy: Input A=Vtg; Input B=q; Vbg=q×Vtg."),
            MODE_FIXED_EFIELD: ("Fixed E-field", "Input A=F; Input B=Vbg anchor; Vtg=F+r×Vbg."),
            MODE_FIXED_DOPING: ("Fixed Doping", "Input A=D; Input B=Vbg anchor; Vtg=D-r×Vbg."),
        }
        choices = [MODE_DIRECT, MODE_DOPING_EFIELD]
        if mode not in choices and mode in labels:
            choices.append(mode)
        for key in choices:
            combo.addItem(labels[key][0], key)
            combo.setItemData(combo.count() - 1, labels[key][1], Qt.ItemDataRole.ToolTipRole)
        combo.setCurrentIndex(max(0, combo.findData(mode)))
        combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        combo.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        return combo

    def _seed_condition_table(self, conditions: list[dict]) -> None:
        self._updating_table = True
        try:
            self._condition_table.setRowCount(max(1, len(conditions)))
            for row, condition in enumerate(conditions):
                check = QTableWidgetItem()
                check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                check.setCheckState(Qt.CheckState.Checked if condition.get("enabled", True) else Qt.CheckState.Unchecked)
                self._condition_table.setItem(row, 0, check)
                mode = str(condition.get("mode", MODE_DIRECT))
                if mode == "coordinates":
                    mode = MODE_FIXED_DOPING
                if "input_a" not in condition:
                    mode = MODE_DIRECT
                    condition = {**condition, "input_a": condition.get("vtg_v", 0.0),
                                 "input_b": condition.get("vbg_v", 0.0)}
                self._condition_table.setItem(row, 1, QTableWidgetItem(str(row + 1)))
                self._condition_table.setCellWidget(row, 2, self._mode_combo(row, mode))
                self._set_row_value(row, 8, float(condition.get("input_a", condition.get("vtg_v", 0.0))))
                self._set_row_value(row, 9, float(condition.get("input_b", condition.get("vbg_v", 0.0))))
                self._set_row_value(row, 5, float(condition.get("vbias_v", 0.0)))
                for column in (3, 4, 6, 7):
                    self._set_row_value(row, column, 0.0)
        finally:
            self._updating_table = False

    def _refresh_condition_row(self, row: int) -> None:
        mode_widget = self._condition_table.cellWidget(row, 2)
        mode = mode_widget.currentData() if mode_widget is not None else MODE_DIRECT
        try:
            resolved = resolve_condition_line({"mode": mode, "input_a": self._cell_value(row, 8),
                                               "input_b": self._cell_value(row, 9),
                                               "vbias_v": self._cell_value(row, 5)}, self._gate_ratio())
            for column, value in ((3, resolved["vtg_v"]), (4, resolved["vbg_v"]),
                                  (6, resolved["doping_v"]), (7, resolved["efield_v"])):
                self._set_row_value(row, column, value)
            self._set_row_error(row)
        except ValueError as exc:
            self._set_row_error(row, str(exc))

    def _update_condition_editable(self, *_args) -> None:
        self._updating_table = True
        try:
            for row in range(self._condition_table.rowCount()):
                self._refresh_condition_row(row)
                for column in (3, 4, 5, 6, 7, 8, 9):
                    item = self._condition_table.item(row, column)
                    if item is not None:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        finally:
            self._updating_table = False

    def _on_gate_ratio_changed(self, _value: float) -> None:
        if not self._syncing_gate_ratio:
            self._syncing_gate_ratio = True
            try:
                self.gate_vtg_factor.setValue(1.0)
                self.gate_vbg_factor.setValue(float(_value))
            finally:
                self._syncing_gate_ratio = False
        try:
            ratio = self._gate_ratio()
            self.gate_ratio_value.setText(f"r = {ratio:.6g}")
            self.gate_ratio_value.setStyleSheet("")
        except ValueError as exc:
            self.gate_ratio_value.setText(str(exc))
            self.gate_ratio_value.setStyleSheet("color: #a40000;")
        self._update_condition_editable()
        if hasattr(self, "_gate_entry_mode"):
            self._update_gate_entry()

    def _on_gate_factors_changed(self, *_args) -> None:
        if self._syncing_gate_ratio:
            return
        try:
            ratio = self._gate_ratio()
        except ValueError as exc:
            self.gate_ratio_value.setText(str(exc))
            self.gate_ratio_value.setStyleSheet("color: #a40000;")
            self._update_condition_editable()
            return
        self._syncing_gate_ratio = True
        try:
            self.gate_ratio.setValue(ratio)
        finally:
            self._syncing_gate_ratio = False
        self.gate_ratio_value.setText(f"r = {ratio:.6g}")
        self.gate_ratio_value.setStyleSheet("")
        self._update_condition_editable()
        if hasattr(self, "_gate_entry_mode"):
            self._update_gate_entry()

    def _on_condition_item_changed(self, item) -> None:
        if self._updating_table or item is None:
            return
        row, column = item.row(), item.column()
        self._updating_table = True
        try:
            if column in (5, 8, 9):
                self._refresh_condition_row(row)
        except ValueError as exc:
            self._set_row_error(row, str(exc))
        finally:
            self._updating_table = False

    def _update_gate_entry(self, *_args) -> None:
        direct = self._gate_entry_mode.currentData() == MODE_DIRECT
        self._gate_entry_a_label.setText("Vtg values" if direct else "Doping values")
        self._gate_entry_b_label.setText("Vbg values" if direct else "E-field values")
        try:
            label_a = "Vtg" if direct else "Doping"
            label_b = "Vbg" if direct else "E-field"
            values_a = parse_numeric_spec(self._gate_entry_a.text(), label_a)
            values_b = parse_numeric_spec(self._gate_entry_b.text(), label_b)
            multiple_both = len(values_a) > 1 and len(values_b) > 1
            self._gate_entry_expansion_label.setVisible(multiple_both)
            self._gate_entry_expansion.setVisible(multiple_both)
            expansion = self._gate_entry_expansion.currentData() if multiple_both else "paired"
            rows = build_condition_batch(
                self._gate_entry_mode.currentData(), self._gate_entry_a.text(),
                self._gate_entry_b.text(), expansion, self._gate_ratio(),
                vbias_v=self._gate_entry_vbias.value(),
                voltage_limit=cfg.smu.volt_compliance_V,
            )
        except ValueError as exc:
            self._gate_entry_rows = []
            self._gate_entry_status.setText(str(exc))
            self._gate_entry_status.setStyleSheet("color: #a40000;")
            self._gate_entry_add.setEnabled(False)
            self._gate_entry_add.setText("Add rows")
            return
        self._gate_entry_rows = rows
        count = len(rows)
        action = "Replace selected with" if self._gate_edit_row is not None else "Add"
        self._gate_entry_add.setText(f"{action} {count} row{'s' if count != 1 else ''}")
        self._gate_entry_add.setEnabled(True)
        self._gate_entry_status.setStyleSheet("")
        self._gate_entry_status.setText(
            f"{count} row{'s' if count != 1 else ''} ready. "
            "Use commas or start:step:stop for multiple values."
        )

    def _gate_entry_provenance(self) -> dict[str, Any]:
        return {
            "mode": self._gate_entry_mode.currentData(),
            "input_a_spec": self._gate_entry_a.text().strip(),
            "input_b_spec": self._gate_entry_b.text().strip(),
            "expansion": (
                self._gate_entry_expansion.currentData()
                if not self._gate_entry_expansion.isHidden() else "paired"
            ),
            "gate_ratio": self._gate_ratio(),
            "vbias_v": self._gate_entry_vbias.value(),
            "row_count": len(getattr(self, "_gate_entry_rows", [])),
        }

    def _commit_gate_entry(self) -> None:
        self._update_gate_entry()
        rows = list(getattr(self, "_gate_entry_rows", []))
        if not rows:
            return
        provenance = self._gate_entry_provenance()
        try:
            if self._gate_edit_row is None:
                self._append_condition_rows(rows, provenance)
            else:
                existing = self._condition_rows()
                edit_row = self._gate_edit_row
                if edit_row < 0 or edit_row >= len(existing):
                    raise ValueError("The selected gate row no longer exists")
                combined = existing[:edit_row] + rows + existing[edit_row + 1:]
                combined = resolve_gate_conditions(combined, self._gate_ratio())
                validate_gate_conditions(combined, cfg.smu.volt_compliance_V)
                self._seed_condition_table(combined)
                self._condition_table.selectRow(edit_row + len(rows) - 1)
                provenance["replaced_row"] = edit_row + 1
                self._gate_batch_provenance.append(provenance)
                self._update_condition_editable()
        except ValueError as exc:
            self._show_error(str(exc))
            return
        self._gate_edit_row = None
        self.error_display.clear()
        self._update_gate_entry()

    def _edit_selected_condition(self) -> None:
        row = self._condition_table.currentRow()
        if row < 0:
            self._show_error("Select a gate row to edit")
            return
        conditions = self._condition_rows()
        condition = conditions[row]
        mode = condition.get("mode", MODE_DIRECT)
        if mode not in (MODE_DIRECT, MODE_DOPING_EFIELD):
            # Legacy definitions are converted to their resolved voltages so
            # future edits use the unambiguous normal entry path.
            mode = MODE_DIRECT
            input_a, input_b = condition["vtg_v"], condition["vbg_v"]
        else:
            input_a, input_b = condition["input_a"], condition["input_b"]
        self._gate_entry_mode.setCurrentIndex(self._gate_entry_mode.findData(mode))
        self._gate_entry_a.setText(f"{float(input_a):.12g}")
        self._gate_entry_b.setText(f"{float(input_b):.12g}")
        self._gate_entry_vbias.setValue(float(condition.get("vbias_v", 0.0)))
        self._gate_edit_row = row
        self._update_gate_entry()

    def _add_condition_row(self) -> None:
        rows = self._condition_rows()
        source = dict(rows[-1]) if rows else {}
        rows.append({"enabled": True, **source})
        row = len(rows) - 1
        self._seed_condition_table(rows)
        self._condition_table.selectRow(row)
        self._update_condition_editable()

    def _append_condition_rows(self, rows: list[dict[str, Any]],
                               provenance: Optional[dict[str, Any]] = None) -> None:
        if not rows:
            raise ValueError("No gate rows were provided")
        # Resolve and validate the complete result before changing the table.
        existing = self._condition_rows()
        if len(existing) == 1:
            only = existing[0]
            if (
                only.get("mode") == MODE_DIRECT
                and abs(float(only.get("input_a", 0.0))) <= 1e-12
                and abs(float(only.get("input_b", 0.0))) <= 1e-12
                and abs(float(only.get("vbias_v", 0.0))) <= 1e-12
            ):
                existing = []
        combined = resolve_gate_conditions([*existing, *rows], self._gate_ratio())
        validate_gate_conditions(combined, cfg.smu.volt_compliance_V)
        self._seed_condition_table(combined)
        if provenance:
            self._gate_batch_provenance.append(dict(provenance))
        self._condition_table.selectRow(len(combined) - 1)
        self._update_condition_editable()

    def _remove_condition_row(self) -> None:
        if self._condition_table.rowCount() <= 1:
            return
        row = self._condition_table.currentRow()
        row = row if row >= 0 else self._condition_table.rowCount() - 1
        rows = self._condition_rows()
        rows.pop(row)
        self._seed_condition_table(rows)
        self._gate_edit_row = None
        self._update_condition_editable()

    def _move_condition(self, delta: int) -> None:
        row = self._condition_table.currentRow()
        target = row + int(delta)
        if row < 0 or target < 0 or target >= self._condition_table.rowCount():
            return
        rows = self._condition_rows(); rows[row], rows[target] = rows[target], rows[row]
        self._seed_condition_table(rows); self._condition_table.selectRow(target)

    @staticmethod
    def _finite_list(text: str, label: str) -> list[float]:
        try:
            values = [float(item.strip()) for item in text.split(",") if item.strip()]
        except ValueError as exc:
            raise ValueError(f"{label} must be comma-separated numbers") from exc
        if not values or any(not math.isfinite(value) for value in values):
            raise ValueError(f"{label} must be a non-empty finite list")
        return values

    def _continuous_settings(self):
        return {
            "poll_interval_s": cfg.mcd2100.polling_interval_s,
            "gate_timeout_s": cfg.mcd2100.settle_timeout_s,
            "operation_timeout_s": cfg.mcd2100.operation_timeout_s,
        }

    def _save_config_from_ui(self) -> None:
        settings = cfg.mcd2100
        try:
            settings.start_field_t = float(self.start_field.text())
            settings.stop_field_t = float(self.stop_field.text())
        except (TypeError, ValueError):
            pass
        settings.sample_id = self._sample_id.text().strip()
        settings.bidirectional = True
        settings.angles_deg = self._finite_list(self.angles.text(), "Angles")
        settings.rotator = self.rotator.currentText()
        settings.lf_center_nm = self.lf_center.value()
        settings.lf_exposure_ms = self.lf_exposure.value()
        settings.lf_frames = self.lf_frames.value()
        settings.vtg_v = self.vtg.value(); settings.vbg_v = self.vbg.value(); settings.vbias_v = self.vbias.value()
        settings.gate_ratio = self._gate_ratio()
        settings.gate_vtg_factor = self.gate_vtg_factor.value()
        settings.gate_vbg_factor = self.gate_vbg_factor.value()
        settings.conditions = self._condition_rows()
        settings.gate_batches = list(self._gate_batch_provenance)
        settings.filename_stem = self.stem.text().strip()
        settings.temperature_control_enabled = self.temperature_control_enabled.isChecked()
        settings.sample_target_k = self.sample_target.value()
        settings.sample_ramp_rate_k_per_min = self.sample_ramp_rate.value()
        settings.temperature_tolerance_k = self.temperature_tolerance.value()
        settings.temperature_stable_s = self.temperature_stable.value()
        settings.temperature_timeout_s = self.temperature_timeout.value()

    def apply_saved_experiment_settings(self, settings: dict) -> dict:
        allowed = {
            "start_field_t": lambda v: self.start_field.setText(str(v)),
            "stop_field_t": lambda v: self.stop_field.setText(str(v)),
            "lf_center_nm": lambda v: self.lf_center.setValue(float(v)),
            "lf_exposure_ms": lambda v: self.lf_exposure.setValue(float(v)),
            "lf_frames": lambda v: self.lf_frames.setValue(int(v)),
            "vtg_v": lambda v: self.vtg.setValue(float(v)),
            "vbg_v": lambda v: self.vbg.setValue(float(v)),
            "vbias_v": lambda v: self.vbias.setValue(float(v)),
            "gate_ratio": lambda v: self.gate_ratio.setValue(float(v)),
            "gate_vtg_factor": lambda v: self.gate_vtg_factor.setValue(float(v)),
            "gate_vbg_factor": lambda v: self.gate_vbg_factor.setValue(float(v)),
            "gate_conditions": lambda v: self._seed_condition_table(list(v)),
            "conditions": lambda v: self._seed_condition_table(list(v)),
            "gate_batches": lambda v: setattr(self, "_gate_batch_provenance", list(v)),
            "angles_deg": lambda v: self.angles.setText(", ".join(str(x) for x in v)),
            "rotator": lambda v: self.rotator.setCurrentText(str(v)),
            "mcd2100_settings_version": lambda _v: None,
            "temperature_control_enabled": lambda v: self.temperature_control_enabled.setChecked(bool(v)),
            "sample_target_k": lambda v: self.sample_target.setValue(float(v)),
            "sample_ramp_rate_k_per_min": lambda v: self.sample_ramp_rate.setValue(float(v)),
            "temperature_tolerance_k": lambda v: self.temperature_tolerance.setValue(float(v)),
            "temperature_stable_s": lambda v: self.temperature_stable.setValue(float(v)),
            "temperature_timeout_s": lambda v: self.temperature_timeout.setValue(float(v)),
        }
        skipped = []
        for key, value in dict(settings or {}).items():
            setter = allowed.get(key)
            if setter is None:
                skipped.append(key)
                continue
            try:
                setter(value)
            except Exception:
                skipped.append(key)
        self._update_condition_editable()
        return {"applied": [k for k in settings if k not in skipped], "skipped": skipped}

    def capture_session_state(self) -> dict:
        return {
            "sample_id": self._sample_id.text(),
            "start_field_t": self.start_field.text(),
            "stop_field_t": self.stop_field.text(),
            "angles": self.angles.text(),
            "rotator": self.rotator.currentText(),
            "conditions": self._condition_rows(),
            "gate_conditions": self._condition_rows(),
            "gate_batches": list(self._gate_batch_provenance),
            "mcd2100_settings_version": 3,
            "gate_mode": self._gate_mode.currentData(),
            "gate_ratio": self._gate_ratio(),
            "gate_vtg_factor": self.gate_vtg_factor.value(),
            "gate_vbg_factor": self.gate_vbg_factor.value(),
            "vtg_v": self.vtg.value(), "vbg_v": self.vbg.value(), "vbias_v": self.vbias.value(),
            "lf_center_nm": self.lf_center.value(),
            "lf_exposure_ms": self.lf_exposure.value(),
            "lf_frames": self.lf_frames.value(),
            "temperature_control_enabled": self.temperature_control_enabled.isChecked(),
            "sample_target_k": self.sample_target.value(),
            "sample_ramp_rate_k_per_min": self.sample_ramp_rate.value(),
            "temperature_tolerance_k": self.temperature_tolerance.value(),
            "temperature_stable_s": self.temperature_stable.value(),
            "temperature_timeout_s": self.temperature_timeout.value(),
            "splitter_sizes": [int(value) for value in self._splitter.sizes()],
            "plot_log_sizes": [int(value) for value in self._plot_log_splitter.sizes()],
        }

    def restore_session_state(self, state: dict) -> None:
        if not isinstance(state, dict):
            return
        if "sample_id" in state:
            self._sample_id.setText(str(state["sample_id"]))
        for widget, key in ((self.start_field, "start_field_t"), (self.stop_field, "stop_field_t"),
                            (self.angles, "angles")):
            if key in state:
                widget.setText(str(state[key]))
        if "rotator" in state:
            self.rotator.setCurrentText(str(state["rotator"]))
        saved_rows = state.get("gate_conditions", state.get("conditions"))
        if isinstance(saved_rows, list):
            self._seed_condition_table(saved_rows)
        elif any(key in state for key in ("vtg_v", "vbg_v", "vbias_v")):
            # Migrate the pre-table flat MCD2100 session representation.
            self._seed_condition_table([{"enabled": True, "mode": MODE_DIRECT,
                                         "input_a": state.get("vtg_v", 0.0),
                                         "input_b": state.get("vbg_v", 0.0),
                                         "vbias_v": state.get("vbias_v", 0.0)}])
        if isinstance(state.get("gate_batches"), list):
            self._gate_batch_provenance = list(state["gate_batches"])
        for widget, key in ((self.gate_ratio, "gate_ratio"), (self.vtg, "vtg_v"),
                            (self.vbg, "vbg_v"), (self.vbias, "vbias_v")):
            try:
                if key in state:
                    widget.setValue(float(state[key]))
            except (TypeError, ValueError):
                pass
        if "gate_vtg_factor" in state:
            self.gate_vtg_factor.setValue(float(state["gate_vtg_factor"]))
        if "gate_vbg_factor" in state:
            self.gate_vbg_factor.setValue(float(state["gate_vbg_factor"]))
        self._on_gate_factors_changed()
        if state.get("gate_mode") in {"voltage", "coordinates"}:
            self._gate_mode.setCurrentIndex(self._gate_mode.findData(state["gate_mode"]))
        for widget, key in ((self.lf_center, "lf_center_nm"), (self.lf_exposure, "lf_exposure_ms"),
                            (self.lf_frames, "lf_frames")):
            try:
                if key in state:
                    widget.setValue(float(state[key]))
            except (TypeError, ValueError):
                pass
        if "temperature_control_enabled" in state:
            self.temperature_control_enabled.setChecked(bool(state["temperature_control_enabled"]))
        for widget, key in (
            (self.sample_target, "sample_target_k"),
            (self.sample_ramp_rate, "sample_ramp_rate_k_per_min"),
            (self.temperature_tolerance, "temperature_tolerance_k"),
            (self.temperature_stable, "temperature_stable_s"),
            (self.temperature_timeout, "temperature_timeout_s"),
        ):
            try:
                if key in state:
                    widget.setValue(float(state[key]))
            except (TypeError, ValueError):
                pass
        self._update_temperature_controls()
        self._update_condition_editable()
        for splitter, key in ((self._splitter, "splitter_sizes"), (self._plot_log_splitter, "plot_log_sizes")):
            sizes = state.get(key)
            if isinstance(sizes, (list, tuple)) and len(sizes) == 2:
                try:
                    splitter.setSizes([int(sizes[0]), int(sizes[1])])
                except (TypeError, ValueError):
                    pass

    def set_externally_busy(self, busy: bool):
        self._externally_busy = bool(busy)
        self._refresh_controls()

    @Slot()
    def connect_instrument(self):
        if self._connect_handle is not None or self._connected:
            return
        self.connection_status.setText(
            "Reconnecting telemetry…" if self._detached_after_completion else "Connecting…"
        )
        self.connect_btn.setEnabled(False)
        try:
            self._connect_handle = self.controller.connect_async()
        except Exception as exc:
            self._connect_handle = None
            self._show_error(f"Connection failed: {exc}")
            self._refresh_controls()
            return
        QTimer.singleShot(0, self._poll_connect)

    def _poll_connect(self):
        handle = self._connect_handle
        if handle is None:
            return
        if getattr(getattr(handle, "state", None), "name", "") not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            QTimer.singleShot(20, self._poll_connect)
            return
        self._connect_handle = None
        try:
            identity = handle.result(timeout=0)
            handle.wait_drained(timeout=0)
        except Exception as exc:
            self._show_error(f"Connection failed: {exc}")
            self.connection_status.setText("Disconnected")
        else:
            self._on_connected(identity)
            self.refresh_telemetry()
        self._refresh_controls()

    @Slot()
    def disconnect_instrument(self):
        if not self._connected or self.worker is not None or self._disconnect_handle is not None:
            return
        self.connection_status.setText("Disconnecting…")
        try:
            self._disconnect_handle = self.controller.disconnect_async()
        except Exception as exc:
            self._show_error(f"Disconnect failed: {exc}")
            return
        QTimer.singleShot(0, self._poll_disconnect)

    def _poll_disconnect(self):
        handle = self._disconnect_handle
        if handle is None:
            return
        if getattr(getattr(handle, "state", None), "name", "") not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            QTimer.singleShot(20, self._poll_disconnect)
            return
        self._disconnect_handle = None
        try:
            handle.result(timeout=0)
            handle.wait_drained(timeout=0)
        except Exception as exc:
            self._show_error(f"Disconnect failed: {exc}")
        else:
            self._on_disconnected()
        self._refresh_controls()

    @Slot()
    def refresh_telemetry(self):
        if not self._connected:
            return
        try:
            handle = self.controller.read_snapshot_async()
        except Exception as exc:
            self._show_error(str(exc))
            return
        self._telemetry_handle = handle
        QTimer.singleShot(0, lambda: self._poll_telemetry(handle))

    def _poll_telemetry(self, handle):
        if getattr(getattr(handle, "state", None), "name", "") not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            QTimer.singleShot(20, lambda: self._poll_telemetry(handle))
            return
        try:
            snap = handle.result(timeout=0)
            handle.wait_drained(timeout=0)
        except Exception as exc:
            self._show_error(f"Telemetry failed: {exc}")
        else:
            self._on_snapshot(snap)
            read_temperature = getattr(self.controller, "read_temperature_snapshot_async", None)
            if callable(read_temperature):
                try:
                    temperature_handle = read_temperature()
                except Exception as exc:
                    self._show_error(f"Temperature telemetry failed: {exc}")
                else:
                    QTimer.singleShot(
                        0, lambda: self._poll_temperature_telemetry(temperature_handle)
                    )

    def _poll_temperature_telemetry(self, handle):
        if getattr(getattr(handle, "state", None), "name", "") not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            QTimer.singleShot(20, lambda: self._poll_temperature_telemetry(handle))
            return
        try:
            snapshot = handle.result(timeout=0)
            handle.wait_drained(timeout=0)
        except Exception as exc:
            self._show_error(f"Temperature telemetry failed: {exc}")
        else:
            self._on_temperature_snapshot(snapshot)

    @Slot(object)
    def _on_connected(self, identity=None):
        self._connected = True
        self._detached_after_completion = False
        host = getattr(identity, "host", "") if identity is not None else ""
        self.connection_status.setText(f"Connected{f' — {host}' if host else ''}")
        self.connect_btn.setText("Connect")
        self.telemetry_note.setText("Live telemetry — awaiting update")
        self._terminal_status = "Ready"
        self.status.setText("Ready")
        self._refresh_controls()

    @Slot()
    def _on_disconnected(self):
        self._connected = False
        if self._detached_after_completion:
            self._show_completed_detach()
        else:
            self.connection_status.setText("Disconnected")
            self.connect_btn.setText("Connect")
            self.telemetry_note.setText("Telemetry unavailable")
            self._terminal_status = "Disconnected"
            self.status.setText("Disconnected")
        self._refresh_controls()

    @Slot(object)
    def _on_controller_state(self, state):
        """Preserve the reason when completed-run detach settles to DISCONNECTED."""
        name = getattr(state, "name", str(state)).upper()
        if name == "DETACHED":
            self._detached_after_completion = True
            self._connected = False
            self._show_completed_detach()
            self._refresh_controls()

    def _show_completed_detach(self):
        self.connection_status.setText("Detached — magnet left at final field")
        self.connect_btn.setText("Reconnect telemetry")
        observed = (
            self._last_telemetry_time.strftime("%H:%M:%S")
            if self._last_telemetry_time is not None else "before detachment"
        )
        self.telemetry_note.setText(
            f"Field and temperature are last-known values ({observed}), not live telemetry."
        )
        self._terminal_status = "Completed — telemetry detached; magnet left at final field"
        self.status.setText(self._terminal_status)

    @Slot(object)
    def _on_snapshot(self, snapshot):
        self._last_telemetry_time = datetime.now().astimezone()
        def display(value, suffix=""):
            return f"{float(value):.6g}{suffix}" if isinstance(value, (int, float)) and math.isfinite(float(value)) else "N/A"
        self.field_value.setText(display(getattr(snapshot, "field_t", None), " T"))
        self.current_field.setText(display(getattr(snapshot, "field_t", None), " T"))
        self.temperature_value.setText(display(getattr(snapshot, "temperature_k", None), " K"))
        setpoint = getattr(snapshot, "setpoint_t", None)
        self.current_target.setText(display(setpoint, " T"))
        field = getattr(snapshot, "field_t", None)
        if isinstance(field, (int, float)) and isinstance(setpoint, (int, float)):
            self.direction_value.setText("Increasing" if field < setpoint else "Decreasing" if field > setpoint else "At target")
        status = getattr(snapshot, "status", None)
        details = getattr(status, "backend_details", {}) if status is not None else {}
        active = details.get("field_control") if isinstance(details, dict) else None
        self.control_value.setText("Active" if active is True else "Inactive" if active is False else "N/A")
        quench = getattr(status, "quench", None) if status is not None else None
        self.quench_value.setText("YES" if quench is True else "No" if quench is False else "N/A")
        if self._connected:
            self.telemetry_note.setText(
                f"Live telemetry updated {self._last_telemetry_time.strftime('%H:%M:%S')}"
            )

    @Slot(object)
    def _on_temperature_snapshot(self, snapshot):
        def display(value):
            return (
                f"{float(value):.6g} K"
                if isinstance(value, (int, float)) and math.isfinite(float(value)) else "N/A"
            )
        self.sample_temperature_value.setText(
            display(getattr(snapshot, "sample_temperature_k", None))
        )
        self.vti_temperature_value.setText(
            display(getattr(snapshot, "vti_temperature_k", None))
        )
        active = getattr(snapshot, "sample_control_active", None)
        self.sample_temperature_control_value.setText(
            "Active" if active is True else "Inactive" if active is False else "N/A"
        )

    @Slot(str)
    def _show_error(self, message):
        self.error_display.setPlainText(str(message))

    @Slot()
    def start(self):
        if self.worker is not None:
            return
        if self._externally_busy:
            self._show_error("Another MCD workflow is using the shared instruments")
            return
        if not self._connected:
            self._show_error("Connect the attoDRY2100 before starting")
            return
        if self._lf6 is not None:
            try:
                ensure_ready = getattr(self._lf6, "ensure_ready", None)
                if callable(ensure_ready):
                    self.status.setText("LightField: Initializing...")
                    ensure_ready(timeout_s=15.0, poll_interval_s=0.05)
                lf_ready = getattr(self._lf6, "is_ready", None)
                if lf_ready is not None and not bool(lf_ready() if callable(lf_ready) else lf_ready):
                    raise RuntimeError("LightField is not ready; shared controller did not publish READY")
            except Exception as exc:
                self._show_error(f"LightField readiness failed: {exc}")
                return
        if self.apply_voltages.isChecked():
            if self._smu is None or not bool(getattr(self._smu, "is_connected", False)) or getattr(self._smu, "device", None) is None:
                self._show_error("SMU is not connected")
                return
        try:
            start_field = float(self.start_field.text().strip())
            stop_field = float(self.stop_field.text().strip())
            if not math.isfinite(start_field) or not math.isfinite(stop_field):
                raise ValueError("Start and stop fields must be finite")
            angles = self._finite_list(self.angles.text(), "Angles")
            conditions = self._condition_rows()
            enabled_conditions = [item for item in conditions if item.get("enabled", True)]
            if not enabled_conditions:
                raise ValueError("At least one enabled gate condition is required")
            validate_gate_conditions(enabled_conditions, cfg.smu.volt_compliance_V)
            if self._smu is None and self.parent() is None and self._apply_requested:
                raise ValueError("SMU is not connected")
            if self._smu is not None or self.parent() is not None:
                required_roles = ("Vbg", "Vtg")
                if any(abs(float(item.get("vbias_v", 0.0))) > 1e-12 for item in enabled_conditions):
                    required_roles += ("Vbias",)
                readiness = smu_readiness_issues(self._smu, required_roles)
                if readiness:
                    raise ValueError(readiness[0])
            device_id = self._sample_id.text().strip()
            if device_id:
                self._update_derived_output()
            output_text = self.output.text().strip()
            if not output_text and not device_id:
                raise ValueError("Output directory is required")
            output = Path(output_text)
            stem = "mcd2100_continuous"
            # MainWindow owns the shared Sample ID binder.  A panel created
            # standalone for controller/UI tests has no provider or parent;
            # it may exercise worker wiring without creating persisted data.
            standalone_injected = self.parent() is None
            if not device_id and not standalone_injected:
                raise ValueError("Sample ID is required")
            if device_id:
                cfg.mcd2100.sample_id = device_id
            stem = sanitize_token(stem)
            stem = make_unique_stem(output, stem)
            suffix = 1
            while (output / f"{stem}.meta.json").exists():
                stem = f"{sanitize_token(self.stem.text().strip())}_{suffix:03d}"
                suffix += 1
            rotator_name = self.rotator.currentText().strip() or "rot1"
            if self._optical_factory is None:
                if self._lf6 is None or not bool(getattr(self._lf6, "is_connected", False)):
                    raise ValueError("LightField is not connected")
                connected = getattr(self._rotation, "is_connected", None)
                if not callable(connected) or not connected(rotator_name):
                    raise ValueError(f"{rotator_name.upper()} is not connected")
            self._save_config_from_ui()
            self._experiment_run = None
            if device_id:
                base_root = Path(cfg.filename.base_out)
                settings_snapshot = {
                    "mcd2100_settings_version": 3,
                    "start_field_t": start_field, "stop_field_t": stop_field,
                    "angles_deg": angles, "rotator": rotator_name,
                    "lf_center_nm": self.lf_center.value(), "lf_exposure_ms": self.lf_exposure.value(),
                    "lf_frames": self.lf_frames.value(), "gate_ratio": self._gate_ratio(),
                    "gate_vtg_factor": self.gate_vtg_factor.value(),
                    "gate_vbg_factor": self.gate_vbg_factor.value(),
                    "gate_conditions": conditions,
                    "gate_batches": list(self._gate_batch_provenance),
                    "temperature_control_enabled": self.temperature_control_enabled.isChecked(),
                    "sample_target_k": self.sample_target.value(),
                    "sample_ramp_rate_k_per_min": self.sample_ramp_rate.value(),
                    "temperature_tolerance_k": self.temperature_tolerance.value(),
                    "temperature_stable_s": self.temperature_stable.value(),
                    "temperature_timeout_s": self.temperature_timeout.value(),
                }
                safety_policy = {
                    "attodry2100": vars(cfg.attodry2100),
                    "smu": {"volt_compliance_V": cfg.smu.volt_compliance_V},
                    "ramp": vars(cfg.ramp),
                }
                self._experiment_run = ExperimentMetadataService(base_root).begin(
                    "mcd_attodry2100", device_id, output_dir=output,
                    sample_id=device_id, settings=settings_snapshot,
                    safety_policy=safety_policy,
                )
            optical = self._optical_factory() if self._optical_factory else _LightFieldRotationService(
                self._lf6, self._rotation, rotator_name, self._smu
            )
            worker = self._worker_factory(
                self.controller, optical, start_field, stop_field, angles, output,
                stem=stem, bidirectional=True,
                rotator=rotator_name,
                lf_center_nm=self.lf_center.value(),
                lf_exposure_ms=self.lf_exposure.value(),
                lf_frames=self.lf_frames.value(),
                vtg_v=self.vtg.value(), vbg_v=self.vbg.value(), vbias_v=self.vbias.value(),
                gate_ratio=self._gate_ratio(), apply_voltages=True,
                conditions=enabled_conditions,
                temperature_control_enabled=self.temperature_control_enabled.isChecked(),
                sample_target_k=self.sample_target.value(),
                sample_ramp_rate_k_per_min=self.sample_ramp_rate.value(),
                temperature_tolerance_k=self.temperature_tolerance.value(),
                temperature_stable_s=self.temperature_stable.value(),
                temperature_timeout_s=self.temperature_timeout.value(),
                **self._continuous_settings(),
                metadata={"device_id": self._sample_id.text().strip(), "experiment_type": "mcd_attodry2100"},
            )
        except Exception as exc:
            run = getattr(self, "_experiment_run", None)
            if run is not None and run.metadata.get("status") == "running":
                try:
                    run.fail(exc)
                except Exception:
                    pass
            self._show_error(str(exc))
            return
        self.error_display.clear()
        self.worker = worker
        self.runner = _Runner(worker)
        self.runner.progress.connect(self._on_progress)
        self.runner.spectrum_event.connect(self._on_spectrum_event)
        self.runner.log.connect(self._append_log)
        self.thread = QThread(self)
        self.runner.moveToThread(self.thread)
        self.thread.started.connect(self.runner.run)
        self.runner.finished.connect(self.terminal)
        # QThread lives in the GUI thread; invoke its thread-safe quit directly
        # so owner-loop termination never depends on another queued GUI event.
        self.runner.finished.connect(
            self.thread.quit, Qt.ConnectionType.DirectConnection
        )
        self.thread.finished.connect(self._thread_finished)
        self._terminal_status = "Running"
        self.status.setText("Running")
        self.progress_bar.setValue(0)
        self.run_state_changed.emit(True)
        self._refresh_controls()
        self.thread.start()

    @Slot()
    def stop(self):
        if self.worker is None:
            return
        self.worker.request_cancel()
        self.status.setText("Cancellation requested — waiting for safe cleanup")

    @Slot(float, float, int, int)
    def _on_progress(self, field_t: float, percent: float, condition_index: int, condition_count: int) -> None:
        self.progress_bar.setValue(max(0, min(100, int(round(percent)))))
        self.current_field.setText(f"{field_t:+.6g} T")
        self.progress.setText(
            f"Gate {condition_index}/{condition_count} · {percent:.1f}%"
            if condition_count > 1 else f"{percent:.1f}%"
        )

    @Slot(object, object, str, float)
    def _on_spectrum(self, wavelengths, counts, label: str, field_t: float) -> None:
        curve = self._curve_a if label == "A" else self._curve_b
        curve.setData(np.asarray(wavelengths), np.asarray(counts))
        self.polarization_value.setText(f"{label} at {field_t:+.6g} T")

    @Slot(object)
    def _on_spectrum_event(self, event: dict[str, Any]) -> None:
        """Consume only the structured, post-durable observer event."""
        wavelengths = event.get("wavelengths", [])
        counts = event.get("counts", [])
        label = str(event.get("label", "A"))
        curve = self._curve_a if label == "A" else self._curve_b
        curve.setData(np.asarray(wavelengths), np.asarray(counts))
        self.polarization_value.setText(
            f"{label} at {float(event.get('B1_T', 0.0)):+.6g} T"
        )

    @Slot(str)
    def _append_log(self, message: str) -> None:
        self._log.appendPlainText(str(message))

    @Slot(object)
    def _on_terminal(self, result):
        terminal = str(result.get("status", "FAILED")).upper()
        if terminal not in {"COMPLETED", "CANCELLED", "FAILED"}:
            terminal = "FAILED"
        if terminal == "COMPLETED":
            self._detached_after_completion = True
            self._connected = False
            self._show_completed_detach()
        else:
            self._terminal_status = terminal
            self.status.setText(self._terminal_status)
        spectra = int(result.get("spectra_written", 0))
        self.progress.setText(f"{spectra} spectra")
        self.progress_bar.setValue(100 if terminal == "COMPLETED" else 0)
        error = result.get("error") or result.get("cleanup_error")
        if error:
            self._show_error(str(error))
        run = getattr(self, "_experiment_run", None)
        if run is not None:
            try:
                csv_paths = result.get("csv_paths") or ([result.get("csv_path")] if result.get("csv_path") else [])
                details = {str(item.get("path")): item for item in (result.get("file_details") or []) if isinstance(item, dict)}
                for csv_name in csv_paths:
                    csv_path = Path(csv_name)
                    if csv_path.exists():
                        detail = details.get(str(csv_path), {})
                        role = detail.get("role", "raw")
                        run.register_file(csv_path, role=role,
                                         kind=detail.get("kind", "continuous_mcd_spectrum"),
                                         details=detail)
                legacy = Path(result.get("metadata_path", ""))
                if legacy.exists():
                    run.register_file(legacy, "intermediate")
                if terminal == "COMPLETED":
                    run.complete({"spectra_written": spectra})
                elif terminal == "CANCELLED":
                    run.cancel(error or "user cancellation")
                else:
                    run.fail(error or "MCD 2100 failed")
            except Exception as exc:
                self._show_error(f"Metadata finalization failed: {exc}")

    @Slot()
    def _thread_finished(self):
        if self.runner is not None:
            self.runner.deleteLater()
        if self.thread is not None:
            self.thread.deleteLater()
        self.worker = self.runner = self.thread = None
        self.run_state_changed.emit(False)
        self.status.setText(self._terminal_status)
        self._refresh_controls()

    def _refresh_controls(self):
        running = self.worker is not None
        self.start_btn.setEnabled(self._connected and not running and not self._externally_busy)
        self.stop_btn.setEnabled(running)
        self.connect_btn.setEnabled(not self._connected and not running and self._connect_handle is None)
        self.disconnect_btn.setEnabled(self._connected and not running and self._disconnect_handle is None)
        self.refresh_btn.setEnabled(self._connected and not running)
        self.temperature_control_enabled.setEnabled(not running)
        self._update_temperature_controls()

    def shutdown(self, timeout_ms=30_000):
        if self.worker is not None:
            self.worker.request_cancel()
        thread = self.thread
        if thread is not None and thread.isRunning():
            if not thread.wait(int(timeout_ms)):
                return False
        return True
