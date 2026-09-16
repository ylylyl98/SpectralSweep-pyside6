"""Dedicated continuous attoDRY2100 MCD panel."""
from __future__ import annotations

import math
import threading
import time
import concurrent.futures
import inspect
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QDialog, QGroupBox, QHBoxLayout, QLabel, QHeaderView, QLineEdit, QPlainTextEdit,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QGridLayout, QSplitter, QWidget,
)

from app.engine.mcd2100_worker import MCD2100Worker
from app.engine.magnet_preparation import (
    MagnetPreparationWorker, format_preparation_progress, preparation_mode_observation,
)
from app.lightfield_metadata import bind_lightfield_metadata, set_lightfield_context
from app.experiment_metadata import ExperimentMetadataService, instrument_inventory
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
    parse_numeric_spec, build_mcd2100_filename,
)


class _LightFieldRotationService:
    _ACQUISITION_ABORT_TIMEOUT_S = 5.0
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
            spectrometer.calibration_wavelengths(force=False), dtype=float
        ).ravel().tolist()
        return self.wavelengths

    def ensure_ready(self):
        ensure = getattr(self._lf6, "ensure_ready", None)
        if callable(ensure):
            ensure(timeout_s=15.0, poll_interval_s=0.05)
        ready = getattr(self._lf6, "is_ready", None)
        if ready is not None and not bool(ready() if callable(ready) else ready):
            raise RuntimeError("LightField is not ready; shared controller did not publish READY")

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

    def acquire(self, angle, _label, stop_event, acquisition_id=None):
        if stop_event.is_set():
            raise RuntimeError("measurement cancelled")
        spectrometer = getattr(self._lf6, "adapter", None)
        if spectrometer is None:
            raise RuntimeError("LightField spectrometer is unavailable")
        result = {}
        completed = threading.Event()

        def capture() -> None:
            try:
                context = {"angle_deg": float(angle), "polarization_label": str(_label)}
                if acquisition_id:
                    context["acquisition_id"] = str(acquisition_id)
                set_lightfield_context(self._lf6, **context)
                result["value"] = spectrometer.acquire()
            except BaseException as exc:
                result["error"] = exc
            finally:
                completed.set()

        capture_thread = threading.Thread(
            target=capture, name="LightFieldCapture", daemon=True
        )
        capture_thread.start()
        while not completed.wait(0.05):
            if not stop_event.is_set():
                continue
            abort = getattr(spectrometer, "abort_acquisition", None)
            try:
                aborted = bool(abort()) if callable(abort) else False
            except Exception:
                aborted = False
            if not completed.wait(self._ACQUISITION_ABORT_TIMEOUT_S):
                detail = "LightField abort was not acknowledged" if aborted else (
                    "LightField exposes no acquisition-abort operation"
                )
                raise RuntimeError(
                    f"LightField acquisition did not stop after cancellation: {detail}"
                )
            raise RuntimeError("measurement cancelled during LightField acquisition")
        if "error" in result:
            raise result["error"]
        raw = result.get("value")
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


class _LazyOpticalService:
    """Delay injected optical construction until magnet preparation succeeds."""
    def __init__(self, factory):
        self._factory = factory
        self._instance = None

    def _get(self):
        if self._instance is None:
            self._instance = self._factory()
        return self._instance

    def __getattr__(self, name):
        return getattr(self._get(), name)


class _RampTablesDialog(QDialog):
    """Small nonmodal read-only view of a raw ramp-table report."""
    def __init__(self, report, parent=None, *, generation=None, read_at=None,
                 elapsed_s=None, stale=False, stale_reason=None, last_error=None):
        super().__init__(parent)
        self.setWindowTitle("attoDRY2100 ramp tables")
        self.resize(720, 420)
        layout = QVBoxLayout(self)
        caveat = QLabel(
            "Raw SDK values; units and index base are unverified. "
            "Requested indices: 0 through count − 1."
        )
        if read_at is not None:
            caveat.setText(
                caveat.text() + f" Read at {read_at.isoformat()}"
                + (f" · elapsed {elapsed_s:.3f}s" if elapsed_s is not None else "")
                + (f" · {stale_reason}" if stale_reason else
                   (" · previous connection / stale" if stale else ""))
                + (f" · latest read error: {last_error}" if last_error else "")
            )
        caveat.setWordWrap(True)
        layout.addWidget(caveat)
        tabs = QTabWidget()
        layout.addWidget(tabs)
        for table in (report.current, report.default):
            page = QWidget()
            page_layout = QVBoxLayout(page)
            row_errors = sum(1 for row in table.rows if row.error)
            table_errors = "; ".join(table.errors) if table.errors else "None"
            summary = QLabel(
                f"Reported count: {table.reported_count!r} · Table errors: {table_errors}"
                f" · Row errors: {row_errors}"
            )
            summary.setWordWrap(True)
            page_layout.addWidget(summary)
            grid = QTableWidget(len(table.rows), 5)
            grid.setHorizontalHeaderLabels(
                ["Channel", "Requested index", "Raw range", "Raw rate", "Error"]
            )
            grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            grid.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            grid.horizontalHeader().resizeSections(QHeaderView.ResizeMode.ResizeToContents)
            grid.horizontalHeader().setStretchLastSection(True)
            for row_index, row in enumerate(table.rows):
                values = (
                    str(report.channel), str(row.index),
                    "" if row.error else repr(row.raw_range),
                    "" if row.error else repr(row.raw_rate),
                    row.error or "",
                )
                for column, value in enumerate(values):
                    grid.setItem(row_index, column, QTableWidgetItem(value))
            page_layout.addWidget(grid)
            tabs.addTab(page, table.kind.title())


class _Runner(QObject):
    finished = Signal(object)
    progress = Signal(float, float, int, int)
    spectrum = Signal(object, object, str, float)
    spectrum_event = Signal(object)
    log = Signal(str)
    phase = Signal(str)
    preparation_progress = Signal(object)

    def __init__(self, worker):
        super().__init__()
        self.worker = worker
        setter = getattr(worker, "set_callbacks", None)
        if callable(setter):
            callbacks = dict(progress=self.progress.emit, spectrum=self.spectrum.emit,
                             spectrum_event=self.spectrum_event.emit, log=self.log.emit,
                             phase=self.phase.emit,
                             preparation_progress=self.preparation_progress.emit)
            parameters = inspect.signature(setter).parameters
            if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
                callbacks = {key: value for key, value in callbacks.items() if key in parameters}
            setter(**callbacks)

    @Slot()
    def run(self):
        try:
            result = self.worker.run()
        except BaseException as exc:
            result = {"status": "FAILED", "error": str(exc), "spectra_written": 0}
        self.finished.emit(result)


class _SmoothConditionTable(QTableWidget):
    """Condition table that yields wheel scrolling at its boundaries."""

    def wheelEvent(self, event) -> None:
        bar = self.verticalScrollBar()
        delta = event.angleDelta().y()
        at_boundary = (
            (delta > 0 and bar.value() <= bar.minimum())
            or (delta < 0 and bar.value() >= bar.maximum())
        )
        if at_boundary:
            event.ignore()
            return
        super().wheelEvent(event)


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
        self._last_magnet_success_at = None
        self._last_temperature_success_at = None
        self._telemetry_cycle = None
        self._telemetry_generation = None
        self._last_sample_temperature_k: Optional[float] = None
        self._last_sample_setpoint_k: Optional[float] = None
        self._applied_sample_target_k: Optional[float] = None
        self._last_spectrum_at: Optional[float] = None
        self._phase_started_at = time.monotonic()
        self._spectrum_count = 0
        self._active_phase = "Idle"
        self._preparation_progress = None
        self._preparation_closed = False
        self._mode_progress_logged_at = 0.
        self._settle_deadline: Optional[float] = None
        self._externally_busy = False
        self._interlock_held = False
        self._terminal_status = "Disconnected" if not self._connected else "Ready"
        self._connect_handle = None
        self._disconnect_handle = None
        self._temperature_apply_handle = None
        self._temperature_monitor_handle = None
        self._ramp_tables_handle = None
        self._ramp_tables_report = None
        self._ramp_tables_cache_generation = None
        self._ramp_tables_read_at = None
        self._ramp_tables_elapsed_s = None
        self._ramp_tables_started_at = None
        self._ramp_tables_cache_valid = False
        self._ramp_tables_auto_active = False
        self._ramp_tables_request_generation = None
        self._ramp_tables_request_token = 0
        self._ramp_tables_shutdown_token = 0
        self._ramp_tables_last_error = None
        # Once shutdown starts, queued connection/cycle callbacks must not
        # start another automatic diagnostic read.
        self._closing = False
        self._workflow_intent = None
        self._workflow_intent_token = 0
        self._workflow_waiting_drain = False
        self._workflow_restore_state = None
        self._ramp_auto_generation = None
        self._ramp_auto_pending = False
        self._ramp_auto_attempted = False
        self._ramp_auto_telemetry_ready = False
        self._ramp_tables_dialog = None
        self._ramp_tables_timed_out = False
        self._temperature_monitor_timer = QTimer(self)
        self._temperature_monitor_timer.setInterval(1000)
        self._temperature_monitor_timer.timeout.connect(self._monitor_applied_temperature)
        self._telemetry_age_timer = QTimer(self)
        self._telemetry_age_timer.setInterval(250)
        self._telemetry_age_timer.timeout.connect(self._refresh_telemetry_age)
        self._telemetry_age_timer.start()
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
        self.sample_temperature_setpoint_value = QLabel("N/A")
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
        telemetry.addWidget(QLabel("Magnet temp"), 1, 0)
        telemetry.addWidget(self.temperature_value, 1, 1)
        telemetry.addWidget(QLabel("Field control"), 1, 2)
        telemetry.addWidget(self.control_value, 1, 3)
        telemetry.addWidget(QLabel("Quench"), 2, 0)
        telemetry.addWidget(self.quench_value, 2, 1)
        telemetry.addWidget(QLabel("Target"), 2, 2)
        telemetry.addWidget(self.current_target, 2, 3)
        telemetry.addWidget(QLabel("Sample temp"), 3, 0)
        telemetry.addWidget(self.sample_temperature_value, 3, 1)
        telemetry.addWidget(QLabel("VTI temp"), 3, 2)
        telemetry.addWidget(self.vti_temperature_value, 3, 3)
        telemetry.addWidget(QLabel("Sample control"), 4, 0)
        telemetry.addWidget(self.sample_temperature_control_value, 4, 1)
        telemetry.addWidget(QLabel("Sample target"), 4, 2)
        telemetry.addWidget(self.sample_temperature_setpoint_value, 4, 3)
        telemetry.setColumnStretch(1, 1)
        telemetry.setColumnStretch(3, 1)
        connection_layout.addLayout(telemetry)
        self.telemetry_note.setWordWrap(True)
        connection_layout.addWidget(self.telemetry_note)
        buttons = QHBoxLayout()
        self.connect_btn = QPushButton("Connect")
        self.disconnect_btn = QPushButton("Disconnect")
        self.refresh_btn = QPushButton("Refresh telemetry")
        self.read_ramp_tables_btn = QPushButton("Refresh ramp tables")
        self.read_ramp_tables_btn.setToolTip(
            "Explicitly read raw current and factory-default ramp tables"
        )
        self.view_ramp_tables_btn = QPushButton("View ramp tables")
        self.view_ramp_tables_btn.setToolTip("View the last cached ramp-table report without communicating")
        buttons.addWidget(self.connect_btn)
        buttons.addWidget(self.disconnect_btn)
        buttons.addWidget(self.refresh_btn)
        buttons.addWidget(self.read_ramp_tables_btn)
        buttons.addWidget(self.view_ramp_tables_btn)
        connection_layout.addLayout(buttons)
        self.ramp_tables_status = QLabel("Ramp tables not read")
        self.ramp_tables_status.setWordWrap(True)
        connection_layout.addWidget(self.ramp_tables_status)
        content_layout.addWidget(connection)

        workflow = QGroupBox("Continuous attoDRY2100 MCD")
        workflow_layout = QVBoxLayout(workflow)
        self._workflow_layout = workflow_layout
        workflow_layout.setContentsMargins(8, 8, 8, 8)
        workflow_layout.setSpacing(6)

        sample_group = QGroupBox("Sample / Device")
        self._sample_group = sample_group
        sample_form = QHBoxLayout(sample_group)
        sample_form.setContentsMargins(8, 6, 8, 6)
        sample_form.setSpacing(6)
        self.start_field = QLineEdit()
        self.stop_field = QLineEdit()
        self._sample_id = QLineEdit()
        self._sample_id.setPlaceholderText("Sample ID")
        self._point = QLineEdit()
        self._point.setPlaceholderText("p5n2")
        self._point.setToolTip(
            "Optional measurement point or location token included in filenames. "
            "It does not change the Sample ID output folder."
        )
        self._compact(self.start_field, 100, 140)
        self._compact(self.stop_field, 100, 140)
        self._compact(self._sample_id, 150, 300)
        self._compact(self._point, 80, 160)
        sample_form.addWidget(QLabel("Sample ID"))
        sample_form.addWidget(self._sample_id, 1)
        sample_form.addWidget(QLabel("Point / Location"))
        sample_form.addWidget(self._point)
        workflow_layout.addWidget(sample_group)

        sweep_group = QGroupBox("Field Sweep")
        self._sweep_group = sweep_group
        sweep_form = QFormLayout(sweep_group)
        sweep_form.setContentsMargins(8, 6, 8, 6)
        sweep_form.setVerticalSpacing(4)
        self.bidirectional = QCheckBox("Round trip (always)")
        self.bidirectional.setChecked(True)
        self.bidirectional.setEnabled(False)
        self.bidirectional.setVisible(False)
        self.round_trip_notice = QLabel("Round trip: forward + backward for every enabled gate")
        self.round_trip_notice.setWordWrap(True)
        sweep_form.addRow("Start field (T)", self.start_field)
        sweep_form.addRow("Stop field (T)", self.stop_field)
        sweep_form.addRow("Sweep", self.round_trip_notice)

        rotation_group = QGroupBox("Rotation")
        self._rotation_group = rotation_group
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
        rotation_form.addRow("Angles", self.angles)
        rotation_form.addRow("Rotator", self.rotator)

        field_rotation_row = QHBoxLayout()
        field_rotation_row.setSpacing(6)
        field_rotation_row.addWidget(sweep_group, 1)
        field_rotation_row.addWidget(rotation_group, 1)
        workflow_layout.addLayout(field_rotation_row)

        temperature_group = QGroupBox("Temperature")
        self._temperature_group = temperature_group
        temperature_form = QFormLayout(temperature_group)
        temperature_form.setContentsMargins(8, 6, 8, 6)
        temperature_form.setVerticalSpacing(4)
        self.temperature_control_enabled = QCheckBox("Control temperature")
        self.sample_target = self._spin(1.67, 300.0, 3)
        self.sample_ramp_rate = self._spin(0.1, 100.0, 2)
        self.temperature_tolerance = self._spin(0.001, 20.0, 3)
        self.temperature_stable = self._spin(0.0, 3600.0, 1)
        self.temperature_timeout = self._spin(1.0, 86400.0, 0)
        self.sample_target.setSuffix(" K")
        self.sample_ramp_rate.setSuffix(" K/min")
        self.temperature_tolerance.setSuffix(" K")
        self.temperature_stable.setSuffix(" s")
        self.temperature_timeout.setSuffix(" s")
        self.apply_temperature_btn = QPushButton("Apply temperature")
        self.apply_temperature_btn.setToolTip(
            "Immediately send this sample target to the attoDRY2100 and start "
            "its automatic sample/VTI temperature coordination."
        )
        self.temperature_apply_status = QLabel("Not applied")
        self.temperature_apply_status.setWordWrap(True)
        for widget in (
            self.sample_target, self.sample_ramp_rate, self.temperature_tolerance,
            self.temperature_stable, self.temperature_timeout,
        ):
            self._compact(widget, 105, 150)
        temperature_form.addRow(self.temperature_control_enabled)
        temperature_form.addRow("Target", self.sample_target)
        temperature_form.addRow(self.apply_temperature_btn)
        temperature_form.addRow("Apply status", self.temperature_apply_status)
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
        self.temperature_control_enabled.toggled.connect(self._update_condition_preview)
        self.sample_target.valueChanged.connect(self._update_condition_preview)
        self.sample_target.valueChanged.connect(self._on_temperature_target_edited)
        self.apply_temperature_btn.clicked.connect(self.apply_temperature)

        lightfield_group = QGroupBox("LightField")
        self._lightfield_group = lightfield_group
        lightfield_form = QFormLayout(lightfield_group)
        lightfield_form.setContentsMargins(8, 6, 8, 6)
        lightfield_form.setVerticalSpacing(4)
        self.output = QLineEdit()
        self.output.setReadOnly(True)
        self.output.setToolTip("Derived from the shared Sample ID: base output / device / mcd")
        self.stem = QLineEdit("mcd2100_continuous")
        # These legacy widgets remain available internally for run setup and
        # compatibility, but the fixed output location is not exposed to users.
        self.stem.setReadOnly(True)
        self.output.setVisible(False)
        self.stem.setVisible(False)
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
        # This hidden compatibility control is not added to a layout, so give
        # it an explicit owner to keep destruction within the panel's lifetime.
        self.apply_voltages = QCheckBox("Apply gate voltages", gate_group)
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
        self.initial_voltage_settle = self._spin(0.0, 3600.0, 3)
        self.voltage_settle = self._spin(0.0, 3600.0, 3)
        self.initial_voltage_settle.setSuffix(" s")
        self.voltage_settle.setSuffix(" s")
        self.initial_voltage_settle.setSingleStep(0.5)
        self.voltage_settle.setSingleStep(0.1)
        self._compact(self.initial_voltage_settle, 88, 110)
        self._compact(self.voltage_settle, 88, 110)
        self.initial_voltage_settle.setToolTip(
            "Cancelable wait after ramping to the first enabled gate condition."
        )
        self.voltage_settle.setToolTip(
            "Cancelable wait after ramping to each later enabled gate condition."
        )
        for widget in (self.vtg, self.vbg, self.vbias):
            self._compact(widget, 100, 140)
            widget.setSuffix(" V")
        self._compact(self.gate_ratio, 100, 140)
        self._compact(self.gate_vtg_factor, 80, 110)
        self._compact(self.gate_vbg_factor, 80, 110)
        self.gate_vtg_factor.setFixedWidth(110)
        self.gate_vbg_factor.setFixedWidth(110)
        self.initial_voltage_settle.setFixedWidth(88)
        self.voltage_settle.setFixedWidth(88)
        # The table below is authoritative.  These scalar widgets remain as
        # hidden migration attributes for legacy config/session adapters.
        for legacy_widget in (self.vtg, self.vbg, self.vbias):
            legacy_widget.setVisible(False)
        ratio_row = QHBoxLayout()
        ratio_row.addWidget(QLabel("Weighting"))
        ratio_row.addWidget(QLabel("TG"))
        ratio_row.addWidget(self.gate_vtg_factor)
        ratio_row.addSpacing(6)
        ratio_row.addWidget(QLabel("BG"))
        ratio_row.addWidget(self.gate_vbg_factor)
        ratio_row.addWidget(self.gate_ratio_value)
        ratio_row.addStretch(1)
        gate_form.addRow(ratio_row)
        settle_row = QHBoxLayout()
        settle_row.addWidget(QLabel("Settle"))
        settle_row.addWidget(QLabel("First"))
        settle_row.addWidget(self.initial_voltage_settle)
        settle_row.addSpacing(8)
        settle_row.addWidget(QLabel("Later"))
        settle_row.addWidget(self.voltage_settle)
        settle_row.addStretch(1)
        gate_form.addRow(settle_row)

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
            editor.setToolTip("Enter a scalar, comma-separated values, legacy start:step:stop, or (start,stop,step); a short preview is shown below.")
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
        entry_layout.addWidget(self._gate_entry_a_label, 0, 1)
        entry_layout.addWidget(self._gate_entry_b_label, 0, 2)
        entry_layout.addWidget(self._gate_entry_mode, 1, 0)
        entry_layout.addWidget(self._gate_entry_a, 1, 1)
        entry_layout.addWidget(self._gate_entry_b, 1, 2)
        entry_layout.addWidget(QLabel("Vbias"), 2, 0)
        entry_layout.addWidget(self._gate_entry_expansion_label, 2, 1)
        entry_layout.addWidget(self._gate_entry_vbias, 3, 0)
        entry_layout.addWidget(self._gate_entry_expansion, 3, 1)
        entry_layout.addWidget(self._gate_entry_add, 3, 2)
        entry_layout.addWidget(self._gate_entry_status, 4, 0, 1, 3)
        entry_layout.setColumnStretch(0, 1)
        entry_layout.setColumnStretch(1, 1)
        entry_layout.setColumnStretch(2, 1)
        gate_form.addRow(entry_group)
        self._gate_mode = QComboBox()
        self._gate_mode.setVisible(False)
        self._gate_mode.addItem("Vtg / Vbg", "voltage")
        self._gate_mode.addItem("Doping / E-field", "coordinates")
        self._gate_mode.setToolTip("Choose which coordinate pair is editable in the gate table.")
        self._condition_table = _SmoothConditionTable(1, 10)
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
        self._condition_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._condition_table.verticalHeader().setDefaultSectionSize(26)
        self._condition_table.verticalScrollBar().setSingleStep(18)
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

        preview_group = QGroupBox("Gate run preview")
        preview_layout = QVBoxLayout(preview_group)
        preview_layout.setContentsMargins(8, 6, 8, 6)
        preview_layout.setSpacing(3)
        self._condition_summary = QLabel("1 total · 1 enabled")
        self._condition_summary.setStyleSheet("font-weight: 700;")
        self._condition_summary.setWordWrap(True)
        self._condition_summary.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self._selected_condition_summary = QLabel("Select a row to inspect it")
        self._selected_condition_summary.setWordWrap(True)
        self._selected_condition_summary.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self._condition_plan_preview = QPlainTextEdit()
        self._condition_plan_preview.setReadOnly(True)
        self._condition_plan_preview.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth
        )
        self._condition_plan_preview.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._condition_plan_preview.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self._condition_plan_preview.setMinimumHeight(68)
        self._condition_plan_preview.setMaximumHeight(112)
        self._condition_plan_preview.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        preview_layout.addWidget(self._condition_summary)
        preview_layout.addWidget(self._selected_condition_summary)
        preview_layout.addWidget(self._condition_plan_preview)
        gate_form.addRow(preview_group)

        temperature_lightfield_row = QHBoxLayout()
        temperature_lightfield_row.setSpacing(6)
        temperature_lightfield_row.addWidget(temperature_group, 1)
        temperature_lightfield_row.addWidget(lightfield_group, 1)
        workflow_layout.addLayout(temperature_lightfield_row)
        workflow_layout.addWidget(gate_group)

        output_group = QGroupBox("Filename")
        output_form = QFormLayout(output_group)
        output_form.setContentsMargins(8, 6, 8, 6)
        output_form.setVerticalSpacing(4)
        self.output_browse = QPushButton("Browse…")
        self.output_browse.setEnabled(False)
        self.output_browse.setVisible(False)
        self.output_browse.setMinimumWidth(82)
        self.output_browse.clicked.connect(self._browse_output)
        self.filename_preview = QLineEdit()
        self.filename_preview.setReadOnly(True)
        self.filename_preview.setToolTip(
            "Preview for the first enabled gate condition. The output folder is fixed automatically."
        )
        self.filename_preview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        output_form.addRow("Filename preview", self.filename_preview)
        workflow_layout.addWidget(output_group)
        content_layout.addWidget(workflow)

        run_group = QGroupBox("Run control")
        controls = QHBoxLayout(run_group)
        controls.setContentsMargins(8, 6, 8, 6)
        controls.setSpacing(8)
        self.start_btn = QPushButton("Start MCD 2100")
        self.start_btn.setToolTip(
            "Magnet safety and Driven readiness are checked automatically before MCD starts"
        )
        self.prepare_magnet_btn = QPushButton("Prepare magnet")
        self.prepare_magnet_btn.setToolTip("Prepare the magnet automatically before starting MCD")
        self.stop_btn = QPushButton("Stop / Cancel")
        for button in (self.start_btn, self.stop_btn):
            button.setMinimumHeight(36)
        self.start_btn.setStyleSheet("font-weight: 700; background: #e6f3e8;")
        self.stop_btn.setStyleSheet("font-weight: 700; background: #f9e5e5;")
        controls.addWidget(self.start_btn)
        controls.addWidget(self.prepare_magnet_btn)
        controls.addWidget(self.stop_btn)
        content_layout.addWidget(run_group)

        status_group = QGroupBox("Status / Progress / Log")
        status_layout = QVBoxLayout(status_group)
        status_layout.setContentsMargins(8, 6, 8, 8)
        status_layout.setSpacing(4)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.status = QLabel(self._terminal_status)
        self.status.setWordWrap(True)
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
        self.error_display.setMinimumHeight(0)
        self.error_display.setMaximumHeight(120)
        self.error_display.setVisible(False)
        status_layout.addWidget(self.error_display)
        content_layout.addWidget(status_group)
        content_layout.addStretch(1)

        display = QWidget()
        display_layout = QVBoxLayout(display)
        display_layout.setContentsMargins(8, 6, 8, 6)
        display_layout.setSpacing(6)
        display_header = QHBoxLayout()
        live_title = QLabel("Live spectrum")
        live_title.setStyleSheet("font-weight: 700;")
        self.run_activity = QLabel("● Idle")
        self.run_activity.setStyleSheet("color: #6b7280;")
        self.spectrum_activity = QLabel("Spectrum 0 · no spectrum yet")
        display_header.addWidget(live_title)
        display_header.addWidget(self.run_activity)
        display_header.addWidget(self.spectrum_activity)
        display_header.addStretch(1)
        self._clear_log_btn = QPushButton("Clear log")
        display_header.addWidget(self._clear_log_btn)
        display_layout.addLayout(display_header)
        self._plot = pg.PlotWidget()
        self._plot.setMinimumHeight(180)
        self._plot.setLabel("bottom", "Wavelength", units="nm")
        self._plot.setLabel("left", "Intensity", units="counts")
        self._plot_overlay = pg.TextItem("No spectrum yet", color="#4b5563", anchor=(0, 0))
        self._plot_overlay.setPos(0, 0)
        self._plot.addItem(self._plot_overlay)
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
        self._splitter.setSizes([680, 520])
        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(500)
        self._activity_timer.timeout.connect(self._refresh_activity)
        self._activity_timer.start()

        self.connect_btn.clicked.connect(self.connect_instrument)
        self.disconnect_btn.clicked.connect(self.disconnect_instrument)
        self.refresh_btn.clicked.connect(self.refresh_telemetry)
        self.read_ramp_tables_btn.clicked.connect(self.read_ramp_tables)
        self.view_ramp_tables_btn.clicked.connect(self.view_ramp_tables)
        self.start_btn.clicked.connect(self.start)
        self.prepare_magnet_btn.clicked.connect(self.prepare_magnet)
        self.stop_btn.clicked.connect(self.stop)
        self.terminal.connect(self._on_terminal)
        self._clear_log_btn.clicked.connect(self._log.clear)
        self._sample_id.textChanged.connect(self._on_filename_context_changed)
        self._point.textChanged.connect(self._update_filename_preview)
        self.start_field.textChanged.connect(self._update_filename_preview)
        self.stop_field.textChanged.connect(self._update_filename_preview)
        self.gate_ratio.valueChanged.connect(self._on_gate_ratio_changed)
        self.gate_vtg_factor.valueChanged.connect(self._on_gate_factors_changed)
        self.gate_vbg_factor.valueChanged.connect(self._on_gate_factors_changed)
        self._gate_mode.currentIndexChanged.connect(self._update_condition_editable)
        self._condition_table.itemChanged.connect(self._on_condition_item_changed)
        self._condition_table.cellClicked.connect(self._select_condition_row)
        self._condition_table.itemSelectionChanged.connect(self._update_condition_preview)
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
        recovery = bool(getattr(self.controller, "mode_recovery_required", False))
        pending_snapshot = self._pending_work_snapshot()
        pending = pending_snapshot.pending
        control_pending = pending_snapshot.control_pending
        reading_ramp = self._ramp_tables_handle is not None
        enabled = (
            self.temperature_control_enabled.isChecked()
            and self.worker is None
            and self._workflow_intent is None
            and not recovery
            and not control_pending
            and not pending
            and not reading_ramp
        )
        for widget in (
            self.sample_target, self.sample_ramp_rate, self.temperature_tolerance,
            self.temperature_stable, self.temperature_timeout,
        ):
            widget.setEnabled(enabled)
        applying = self._temperature_apply_handle is not None
        self.apply_temperature_btn.setEnabled(
            enabled and self._connected and not applying and not self._externally_busy
        )

    def _on_temperature_target_edited(self, *_args) -> None:
        if self._temperature_apply_handle is not None:
            return
        target = float(self.sample_target.value())
        if (
            self._applied_sample_target_k is None
            or abs(target - self._applied_sample_target_k) > 0.001
        ):
            self._temperature_monitor_timer.stop()
            self.temperature_apply_status.setText("Target edited — click Apply temperature")

    @Slot()
    def apply_temperature(self) -> None:
        if (self._temperature_apply_handle is not None
                or self._ramp_tables_handle is not None
                or self._workflow_intent is not None):
            return
        if not self._connected:
            self._show_error("Connect the attoDRY2100 before applying temperature")
            return
        if self.worker is not None or self._externally_busy:
            self._show_error("Temperature cannot be changed while an MCD workflow is active")
            return
        if bool(getattr(self.controller, "mode_recovery_required", False)):
            self._show_error("Magnet mode recovery is required; use Prepare magnet first")
            return
        if self._pending_work_snapshot().pending:
            self._show_error("attoDRY2100 owner work is still draining")
            return
        if not self.temperature_control_enabled.isChecked():
            self._show_error("Enable Control temperature before applying the target")
            return
        target = float(self.sample_target.value())
        ramp_rate = float(self.sample_ramp_rate.value())
        if self._last_sample_setpoint_k is not None and abs(target - self._last_sample_setpoint_k) >= 25.0:
            answer = QMessageBox.question(
                self,
                "Confirm large temperature change",
                f"Change the sample target from {self._last_sample_setpoint_k:g} K to {target:g} K?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        configure = getattr(self.controller, "configure_sample_temperature_async", None)
        if not callable(configure):
            self._show_error("The connected attoDRY2100 does not expose sample-temperature control")
            return
        self._temperature_monitor_timer.stop()
        self.temperature_apply_status.setText(f"Applying {target:g} K…")
        self.error_display.clear()
        self.error_display.setVisible(False)
        try:
            self._temperature_apply_handle = configure(target, ramp_rate)
        except Exception as exc:
            self._temperature_apply_handle = None
            self.temperature_apply_status.setText("Apply failed")
            self._show_error(f"Temperature apply failed: {exc}")
            self._refresh_controls()
            return
        self._refresh_controls()
        QTimer.singleShot(0, self._poll_apply_temperature)

    def _poll_apply_temperature(self) -> None:
        handle = self._temperature_apply_handle
        if handle is None:
            return
        state = getattr(getattr(handle, "state", None), "name", "")
        if state not in {"SUCCEEDED", "FAILED", "CANCELLED"} and not (
                state == "TIMED_OUT_DRAINING" and bool(getattr(handle, "drained_done", False))):
            QTimer.singleShot(20, self._poll_apply_temperature)
            return
        self._temperature_apply_handle = None
        try:
            snapshot = handle.result(timeout=0)
            handle.wait_drained(timeout=0)
        except Exception as exc:
            self.temperature_apply_status.setText("Apply failed")
            self._show_error(f"Temperature apply failed: {exc}")
        else:
            self._applied_sample_target_k = float(self.sample_target.value())
            invalidate = getattr(self.controller, "invalidate_display_cache", None)
            if callable(invalidate):
                invalidate("temperature")
            self._on_temperature_snapshot(snapshot)
            self._append_log(
                f"Sample temperature target applied immediately: "
                f"{self._applied_sample_target_k:g} K"
            )
            if not self._temperature_is_stable(snapshot):
                self._temperature_monitor_timer.start()
        self._refresh_controls()

    def _temperature_is_stable(self, snapshot) -> bool:
        sample = getattr(snapshot, "sample_temperature_k", None)
        setpoint = getattr(snapshot, "sample_setpoint_k", None)
        active = getattr(snapshot, "sample_control_active", None)
        if not all(
            isinstance(value, (int, float)) and math.isfinite(float(value))
            for value in (sample, setpoint)
        ) or active is not True:
            return False
        return abs(float(sample) - float(setpoint)) <= float(self.temperature_tolerance.value())

    def _monitor_applied_temperature(self) -> None:
        if (
            not self._connected or self.worker is not None
            or self._temperature_apply_handle is not None
            or self._temperature_monitor_handle is not None
            or self._ramp_tables_handle is not None
            or self._workflow_intent is not None
        ):
            return
        read = getattr(self.controller, "read_display_temperature_async", None)
        if not callable(read):
            self._temperature_monitor_timer.stop()
            return
        try:
            self._temperature_monitor_handle = read(source="monitor")
        except Exception as exc:
            self._temperature_monitor_timer.stop()
            self._show_error(f"Temperature telemetry failed: {exc}")
            return
        QTimer.singleShot(0, self._poll_applied_temperature)

    def _poll_applied_temperature(self) -> None:
        handle = self._temperature_monitor_handle
        if handle is None:
            return
        state = getattr(getattr(handle, "state", None), "name", "")
        if state not in {"SUCCEEDED", "FAILED", "CANCELLED"} and not (
                state == "TIMED_OUT_DRAINING" and bool(getattr(handle, "drained_done", False))):
            QTimer.singleShot(20, self._poll_applied_temperature)
            return
        self._temperature_monitor_handle = None
        try:
            snapshot = handle.result(timeout=0)
            handle.wait_drained(timeout=0)
        except Exception as exc:
            # A display subscriber can time out while its owner still drains.
            # Preserve the owner completion timestamp/generation when the
            # late display result is available, rather than making a cache hit
            # appear newly measured.
            try:
                snapshot = handle.wait_drained(timeout=0)
            except Exception:
                self._temperature_monitor_timer.stop()
                self._show_error(f"Temperature telemetry failed: {exc}")
                return
            self._on_temperature_snapshot(
                snapshot, completed_at=getattr(handle, "completed_at", None),
                generation=getattr(handle, "generation", None),
            )
        else:
            self._on_temperature_snapshot(
                snapshot, completed_at=getattr(handle, "completed_at", None),
                generation=getattr(handle, "generation", None),
            )
            if self._temperature_is_stable(snapshot):
                self._temperature_monitor_timer.stop()

    def _wire_controller(self):
        for name, slot in (
            ("connected", self._on_connected), ("disconnected", self._on_disconnected),
            ("state_changed", self._on_controller_state),
            ("work_status_changed", self._on_work_status_changed),
            ("snapshot_updated", self._on_snapshot), ("error", self._show_error),
            ("display_snapshot_updated", self._on_snapshot),
            ("display_temperature_updated", self._on_temperature_snapshot),
            ("display_cycle_finished", self._on_display_cycle_finished),
        ):
            signal = getattr(self.controller, name, None)
            if signal is not None and hasattr(signal, "connect"):
                signal.connect(slot)

    def _load_config(self):
        settings = cfg.mcd2100
        self._sample_id.setText(settings.sample_id)
        self._point.setText(settings.point)
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
        self.initial_voltage_settle.setValue(float(
            getattr(settings, "initial_voltage_settle_s", cfg.ramp.settle_s)
        ))
        self.voltage_settle.setValue(float(
            getattr(settings, "voltage_settle_s", cfg.ramp.settle_s)
        ))
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
        self._update_filename_preview()
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

    def _on_filename_context_changed(self, *_args) -> None:
        self._update_derived_output()
        self._update_filename_preview()

    def _filename_temperature(self, *, required: bool = True) -> tuple[float, str]:
        if self.temperature_control_enabled.isChecked():
            return float(self.sample_target.value()), "controlled_target"
        if self._last_sample_temperature_k is not None:
            return float(self._last_sample_temperature_k), "live_sample_readback"
        # Standalone panels are used by controller/UI tests and saved-setting
        # editors without a live temperature stream.  Keep those previews
        # deterministic while the real MainWindow workflow remains fail-closed.
        if self.parent() is None:
            text = str(cfg.filename.temperature).strip().upper().removesuffix("K")
            try:
                value = float(text.replace("P", "."))
                if math.isfinite(value) and value > 0:
                    return value, "configured_default"
            except (TypeError, ValueError):
                pass
        if required:
            raise ValueError(
                "Connect temperature telemetry or enable temperature control to create the filename"
            )
        return float("nan"), "unavailable"

    def _update_filename_preview(self, *_args) -> None:
        if not hasattr(self, "filename_preview"):
            return
        try:
            start_field = float(self.start_field.text().strip())
            stop_field = float(self.stop_field.text().strip())
            if not math.isfinite(start_field) or not math.isfinite(stop_field):
                raise ValueError
            enabled = [row for row in self._condition_rows() if row.get("enabled", True)]
            if not enabled:
                self.filename_preview.setText("Enable a gate condition to preview its filename")
                return
            condition = enabled[0]
            temperature_k, _source = self._filename_temperature()
            filename = build_mcd2100_filename(
                self._sample_id.text().strip() or "SampleID",
                1,
                start_field,
                stop_field,
                "roundtrip",
                doping_v=condition.get("doping_v"),
                efield_v=condition.get("efield_v"),
                vtg_v=condition.get("vtg_v"),
                vbg_v=condition.get("vbg_v"),
                vbias_v=condition.get("vbias_v"),
                ratio=self._gate_ratio(),
                point=self._point.text().strip(),
                temperature_k=temperature_k,
            )
            self.filename_preview.setText(filename)
        except (TypeError, ValueError) as exc:
            message = str(exc)
            self.filename_preview.setText(
                message if "temperature" in message.lower()
                else "Enter valid field and gate values to preview the filename"
            )

    @Slot(int, int)
    def _select_condition_row(self, row: int, _column: int) -> None:
        if row >= 0:
            self._condition_table.selectRow(row)

    def _update_condition_table_height(self) -> None:
        rows = max(1, self._condition_table.rowCount())
        visible_rows = min(8, max(4, rows))
        height = (
            self._condition_table.horizontalHeader().height()
            + visible_rows * self._condition_table.verticalHeader().defaultSectionSize()
            + 4
        )
        self._condition_table.setFixedHeight(height)

    @staticmethod
    def _condition_mode_label(mode: str) -> str:
        return {
            MODE_DIRECT: "Direct",
            MODE_DOPING_EFIELD: "Doping/E-field",
            MODE_VTG_FROM_VBG_RATIO: "Vtg from Vbg",
            MODE_VBG_FROM_VTG_RATIO: "Vbg from Vtg",
            MODE_FIXED_EFIELD: "Fixed E-field",
            MODE_FIXED_DOPING: "Fixed doping",
        }.get(str(mode), str(mode))

    def _update_condition_preview(self, *_args) -> None:
        if not hasattr(self, "_condition_summary"):
            return
        try:
            rows = self._condition_rows()
        except (TypeError, ValueError) as exc:
            self._condition_summary.setText(f"Condition preview unavailable: {exc}")
            return
        enabled = [(table_row, item) for table_row, item in enumerate(rows, start=1)
                   if item.get("enabled", True)]
        count = len(enabled)
        try:
            filename_temperature, _source = self._filename_temperature()
            temperature_summary = f" · filename {filename_temperature:g} K"
        except ValueError:
            temperature_summary = " · filename temperature unavailable"
        self._condition_summary.setText(
            f"{len(rows)} total · {count} enabled · {count} round trips · "
            f"{count * 2} field legs · {count} CSV files{temperature_summary}"
        )
        lines = []
        for gate_index, (table_row, item) in enumerate(enabled, start=1):
            lines.append(
                f"G{gate_index:02d} · Table row {table_row} · "
                f"{self._condition_mode_label(item.get('mode', MODE_DIRECT))} · "
                f"Vtg {float(item.get('vtg_v', 0.0)):+g} V · "
                f"Vbg {float(item.get('vbg_v', 0.0)):+g} V · "
                f"Vbias {float(item.get('vbias_v', 0.0)):+g} V · "
                f"D {float(item.get('doping_v', 0.0)):g} V · "
                f"F {float(item.get('efield_v', 0.0)):g} V"
            )
        self._condition_plan_preview.setPlainText(
            "\n".join(lines) if lines else "No enabled gate rows"
        )
        selected = self._condition_table.currentRow()
        if 0 <= selected < len(rows):
            item = rows[selected]
            execution = next(
                (index for index, (table_row, _item) in enumerate(enabled, start=1)
                 if table_row == selected + 1),
                None,
            )
            execution_text = f"G{execution:02d}" if execution is not None else "disabled"
            self._selected_condition_summary.setText(
                f"Selected table row {selected + 1} ({execution_text}) · "
                f"{self._condition_mode_label(item.get('mode', MODE_DIRECT))} · "
                f"inputs {float(item.get('input_a', 0.0)):g}, "
                f"{float(item.get('input_b', 0.0)):g} · "
                f"resolved Vtg {float(item.get('vtg_v', 0.0)):+g} V, "
                f"Vbg {float(item.get('vbg_v', 0.0)):+g} V"
            )
        else:
            self._selected_condition_summary.setText("Select a row to inspect it")
        self._update_move_buttons()
        self._update_filename_preview()

    def _update_move_buttons(self) -> None:
        if not hasattr(self, "_move_condition_up_btn"):
            return
        row = self._condition_table.currentRow()
        count = self._condition_table.rowCount()
        self._move_condition_up_btn.setEnabled(row > 0)
        self._move_condition_down_btn.setEnabled(0 <= row < count - 1)
        self._edit_condition_btn.setEnabled(row >= 0)
        self._remove_condition_btn.setEnabled(row >= 0 and count > 1)

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

    def _condition_rows_raw(self) -> list[dict[str, Any]]:
        """Capture editable condition cells verbatim, including invalid drafts."""
        rows = []
        for row in range(self._condition_table.rowCount()):
            check = self._condition_table.item(row, 0)
            mode_widget = self._condition_table.cellWidget(row, 2)
            rows.append({
                "enabled": bool(check and check.checkState() == Qt.CheckState.Checked),
                "mode": mode_widget.currentData() if mode_widget is not None else MODE_DIRECT,
                "input_a": self._condition_table.item(row, 8).text() if self._condition_table.item(row, 8) else "",
                "input_b": self._condition_table.item(row, 9).text() if self._condition_table.item(row, 9) else "",
                "vbias_v": self._condition_table.item(row, 5).text() if self._condition_table.item(row, 5) else "",
            })
        return rows

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

    def _set_row_text(self, row: int, column: int, value: object) -> None:
        item = self._condition_table.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            self._condition_table.setItem(row, column, item)
        item.setText(str(value))

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
                check.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
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
                for column, key, fallback in ((8, "input_a", condition.get("vtg_v", 0.0)),
                                               (9, "input_b", condition.get("vbg_v", 0.0)),
                                               (5, "vbias_v", 0.0)):
                    raw_value = condition.get(key, fallback)
                    try:
                        self._set_row_value(row, column, float(raw_value))
                    except (TypeError, ValueError):
                        self._set_row_text(row, column, raw_value)
                for column in (3, 4, 6, 7):
                    self._set_row_value(row, column, 0.0)
        finally:
            self._updating_table = False
        self._update_condition_table_height()
        self._update_condition_preview()

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
        self._update_condition_preview()

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
        self._update_condition_preview()

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
        def _preview(values: list[float]) -> str:
            shown = ", ".join(f"{value:.6g}" for value in values[:4])
            return f"[{shown}{', …' if len(values) > 4 else ''}]"
        self._gate_entry_status.setText(
            f"{count} row{'s' if count != 1 else ''} ready · "
            f"A={_preview(values_a)}, B={_preview(values_b)}"
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
        self.error_display.setVisible(False)
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
        self._condition_table.selectRow(min(row, self._condition_table.rowCount() - 1))
        self._gate_edit_row = None
        self._update_condition_editable()

    def _move_condition(self, delta: int) -> None:
        row = self._condition_table.currentRow()
        target = row + int(delta)
        if row < 0 or target < 0 or target >= self._condition_table.rowCount():
            return
        rows = self._condition_rows()
        rows[row], rows[target] = rows[target], rows[row]
        scrollbar = self._condition_table.verticalScrollBar()
        scroll_value = scrollbar.value()
        self._condition_table.setUpdatesEnabled(False)
        try:
            self._seed_condition_table(rows)
            self._update_condition_editable()
            self._condition_table.selectRow(target)
            scrollbar.setValue(scroll_value)
            current = self._condition_table.item(target, 1)
            if current is not None:
                self._condition_table.scrollToItem(
                    current, QAbstractItemView.ScrollHint.EnsureVisible
                )
        finally:
            self._condition_table.setUpdatesEnabled(True)
            self._condition_table.viewport().update()
        self._update_condition_preview()

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
        settings.point = self._point.text().strip()
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
        settings.initial_voltage_settle_s = self.initial_voltage_settle.value()
        settings.voltage_settle_s = self.voltage_settle.value()
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
            "point": lambda v: self._point.setText(str(v)),
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
            "initial_voltage_settle_s": lambda v: self.initial_voltage_settle.setValue(float(v)),
            "voltage_settle_s": lambda v: self.voltage_settle.setValue(float(v)),
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
        raw_conditions = self._condition_rows_raw()
        try:
            ratio = self._gate_ratio()
            conditions = self._condition_rows()
        except (TypeError, ValueError, KeyError):
            # A partially edited factor/table must remain restorable even when
            # it is not currently runnable.
            ratio = None
            conditions = []
        state = {
            "sample_id": self._sample_id.text(),
            "point": self._point.text(),
            "start_field_t": self.start_field.text(),
            "stop_field_t": self.stop_field.text(),
            "angles": self.angles.text(),
            "rotator": self.rotator.currentText(),
            "conditions": conditions,
            "gate_conditions": conditions,
            "condition_drafts": raw_conditions,
            "gate_batches": list(self._gate_batch_provenance),
            "mcd2100_settings_version": 4,
            "gate_mode": self._gate_mode.currentData(),
            "gate_ratio": ratio,
            "gate_vtg_factor": self.gate_vtg_factor.value(),
            "gate_vbg_factor": self.gate_vbg_factor.value(),
            "initial_voltage_settle_s": self.initial_voltage_settle.value(),
            "voltage_settle_s": self.voltage_settle.value(),
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
            "gate_entry_mode": self._gate_entry_mode.currentData(),
            "gate_entry_a": self._gate_entry_a.text(),
            "gate_entry_b": self._gate_entry_b.text(),
            "gate_entry_vbias": self._gate_entry_vbias.value(),
            "gate_entry_expansion": self._gate_entry_expansion.currentData(),
            "splitter_sizes": [int(value) for value in self._splitter.sizes()],
            "plot_log_sizes": [int(value) for value in self._plot_log_splitter.sizes()],
        }
        return state

    def restore_session_state(self, state: dict) -> None:
        if not isinstance(state, dict):
            return
        for key, widget in (("gate_entry_a", self._gate_entry_a), ("gate_entry_b", self._gate_entry_b)):
            value = state.get(key)
            if isinstance(value, str):
                widget.setText(value)
        mode = state.get("gate_entry_mode")
        if mode is not None:
            index = self._gate_entry_mode.findData(mode)
            if index >= 0:
                self._gate_entry_mode.setCurrentIndex(index)
        expansion = state.get("gate_entry_expansion")
        if expansion is not None:
            index = self._gate_entry_expansion.findData(expansion)
            if index >= 0:
                self._gate_entry_expansion.setCurrentIndex(index)
        try:
            if "gate_entry_vbias" in state:
                self._gate_entry_vbias.setValue(float(state["gate_entry_vbias"]))
        except (TypeError, ValueError):
            pass
        if "sample_id" in state:
            self._sample_id.setText(str(state["sample_id"]))
        if "point" in state:
            self._point.setText(str(state["point"]))
        for widget, key in ((self.start_field, "start_field_t"), (self.stop_field, "stop_field_t"),
                            (self.angles, "angles")):
            if key in state:
                widget.setText(str(state[key]))
        if "rotator" in state:
            self.rotator.setCurrentText(str(state["rotator"]))
        saved_rows = state.get(
            "condition_drafts",
            state.get("gate_conditions", state.get("conditions")),
        )
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
        legacy_settle = state.get("voltage_settle_s")
        if "initial_voltage_settle_s" in state:
            self.initial_voltage_settle.setValue(float(state["initial_voltage_settle_s"]))
        elif legacy_settle is not None:
            self.initial_voltage_settle.setValue(float(legacy_settle))
        if legacy_settle is not None:
            self.voltage_settle.setValue(float(legacy_settle))
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
        if self._workflow_intent is not None:
            self._advance_workflow_intent(self._workflow_intent.token)
            self._refresh_controls()
            return
        if not self._externally_busy:
            self._restore_workflow_display_state()
        if not self._externally_busy and self._ramp_auto_pending:
            # Let an in-flight controller display cycle publish its final
            # drain state before retrying automatic ramp admission.  The
            # queued callback is still local and performs no I/O by itself.
            if bool(getattr(self.controller, "_display_polling_enabled", False)):
                generation = self._ramp_auto_generation
                QTimer.singleShot(
                    0, lambda: (
                        self._start_ramp_tables_read(auto=True)
                        if generation == getattr(self.controller, "generation", generation)
                        else None
                    )
                )
            else:
                self._start_ramp_tables_read(auto=True)
        self._refresh_controls()

    def _pending_work_snapshot(self):
        snapshot = getattr(self.controller, "pending_work_snapshot", None)
        if callable(snapshot):
            return snapshot()
        pending = bool(getattr(self.controller, "has_pending_work", False))
        slots = getattr(self.controller, "_display_slots", {})
        display_pending = any(value is not None for value in slots.values())
        return SimpleNamespace(
            generation=getattr(self.controller, "generation", None),
            pending=pending, display_pending=display_pending,
            control_pending=pending and not display_pending,
            pending_request_ids=(), display_request_ids=(),
        )

    def _workflow_admission_error(self, operation, snapshot=None, *, intent=None):
        """Return a lifecycle conflict before a worker is launched."""
        snapshot = snapshot or self._pending_work_snapshot()
        if self._closing:
            return "The MCD panel is closing"
        if (intent is not None and intent.generation
                != getattr(self.controller, "generation", intent.generation)):
            return "The attoDRY2100 connection generation changed"
        if not self._connected or self._connect_handle is not None or self._disconnect_handle is not None:
            return "Connect the attoDRY2100 before starting"
        if self.worker is not None or (self.thread is not None and self.thread.isRunning()):
            return "Wait for the current operation to finish"
        if self._workflow_intent is not None and self._workflow_intent is not intent:
            return "Wait for the current operation to finish"
        if self._ramp_tables_handle is not None or self._temperature_apply_handle is not None:
            return "Wait for the current operation to finish"
        if self._externally_busy:
            return "Another MCD workflow is using the shared instruments"
        if snapshot.control_pending:
            return "attoDRY2100 owner work is still draining"
        if operation != "magnet_preparation" and bool(
                getattr(self.controller, "mode_recovery_required", False)):
            return "Magnet mode recovery is required; use Prepare magnet first"
        return None

    def _restore_workflow_display_state(self):
        saved = self._workflow_restore_state
        if saved is None:
            return
        saved_generation = getattr(saved, "generation", None)
        current_generation = getattr(self.controller, "generation", None)
        if (saved_generation is not None and current_generation is not None
                and saved_generation != current_generation):
            # Do not apply state captured from a previous connection.
            self._workflow_restore_state = None
            return
        if (self._closing or not self._connected or self._externally_busy
                or self.worker is not None
                or (self.thread is not None and self.thread.isRunning())
                or self._workflow_intent is not None
                or bool(getattr(self.controller, "mode_recovery_required", False))
                or self._pending_work_snapshot().control_pending
                or self._pending_work_snapshot().pending):
            return
        self._workflow_restore_state = None
        polling = getattr(self.controller, "set_polling_enabled", None)
        if callable(polling):
            polling(bool(saved.polling))
        if saved.monitor:
            self._temperature_monitor_timer.start()
        else:
            self._temperature_monitor_timer.stop()

    def _handoff_worker(self, worker, operation, metadata_run=None):
        """Launch now or defer behind already accepted display requests."""
        snapshot = self._pending_work_snapshot()
        admission_error = self._workflow_admission_error(operation, snapshot)
        if admission_error is not None:
            if metadata_run is not None:
                self._finalize_workflow_intent(
                    SimpleNamespace(metadata_run=metadata_run), error=admission_error
                )
            self._show_error(admission_error)
            return False
        if self._workflow_restore_state is None:
            self._workflow_restore_state = SimpleNamespace(
                polling=bool(getattr(self.controller, "_display_polling_enabled", False)),
                monitor=self._temperature_monitor_timer.isActive(),
                generation=getattr(self.controller, "generation", None),
            )
        if not snapshot.display_pending:
            try:
                self._launch_worker(worker, operation)
            except Exception as exc:
                if metadata_run is not None:
                    try:
                        metadata_run.fail(exc)
                    except Exception:
                        pass
                self._show_error(str(exc))
                self._restore_workflow_display_state()
                self._release_interlock_if_drained()
                return False
            return True
        self._workflow_intent_token += 1
        intent = SimpleNamespace(
            token=self._workflow_intent_token,
            generation=getattr(self.controller, "generation", None),
            worker=worker, operation=operation, metadata_run=metadata_run,
        )
        # Install before emitting the shared lock signal; MainWindow callbacks
        # must observe the intent rather than race a new automatic read.
        self._workflow_intent = intent
        self._workflow_waiting_drain = False
        self._interlock_held = True
        self.run_state_changed.emit(True)
        polling = getattr(self.controller, "set_polling_enabled", None)
        if callable(polling):
            polling(False)
        self._temperature_monitor_timer.stop()
        self._terminal_status = "Waiting for display telemetry to drain"
        self.status.setText(self._terminal_status)
        self.stop_btn.setText("Cancel waiting")
        self._advance_workflow_intent(intent.token)
        self._refresh_controls()
        return True

    def _finalize_workflow_intent(self, intent, *, error=None):
        run = getattr(intent, "metadata_run", None)
        if run is None:
            return
        try:
            if run.metadata.get("status") == "running":
                if error is None:
                    run.cancel("workflow handoff cancelled")
                else:
                    run.fail(error)
        except Exception:
            pass

    def _advance_workflow_intent(self, token):
        intent = self._workflow_intent
        if intent is None or intent.token != token:
            return
        snapshot = self._pending_work_snapshot()
        admission_error = self._workflow_admission_error(
            intent.operation, snapshot, intent=intent
        )
        if admission_error is not None:
            self._workflow_waiting_drain = snapshot.display_pending
            self._workflow_intent = None
            self._finalize_workflow_intent(intent, error=admission_error)
            self._terminal_status = "Workflow handoff cancelled"
            self.status.setText(self._terminal_status)
            self._refresh_controls()
            self._release_interlock_if_drained()
            self._restore_workflow_display_state()
            return
        if snapshot.display_pending:
            self._terminal_status = "Waiting for display telemetry to drain"
            self.status.setText(self._terminal_status)
            QTimer.singleShot(25, lambda: self._advance_workflow_intent(token))
            return
        self._workflow_intent = None
        self._workflow_waiting_drain = False
        try:
            self._launch_worker(intent.worker, intent.operation, from_intent=True)
        except Exception as exc:
            self._finalize_workflow_intent(intent, error=exc)
            self._show_error(str(exc))
            self._release_interlock_if_drained()
            self._refresh_controls()
            self._restore_workflow_display_state()

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
        state = getattr(getattr(handle, "state", None), "name", "")
        if state not in {"SUCCEEDED", "FAILED", "CANCELLED"} and not (
                state == "TIMED_OUT_DRAINING" and bool(getattr(handle, "drained_done", False))):
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
        if (
            not self._connected or self.worker is not None
            or self._ramp_tables_handle is not None
            or self._disconnect_handle is not None
            or self._temperature_apply_handle is not None
            or self._workflow_intent is not None
            or bool(getattr(self.controller, "mode_recovery_required", False))
            or self._pending_work_snapshot().control_pending
        ):
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
        state = getattr(getattr(handle, "state", None), "name", "")
        if state not in {"SUCCEEDED", "FAILED", "CANCELLED"} and not (
                state == "TIMED_OUT_DRAINING" and bool(getattr(handle, "drained_done", False))):
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
        if self._workflow_intent is not None:
            return
        control_pending = self._pending_work_snapshot().control_pending
        if (
            not self._connected or self._ramp_tables_handle is not None
            or self.worker is not None or self._temperature_apply_handle is not None
            or self._telemetry_cycle is not None
            or bool(getattr(self.controller, "mode_recovery_required", False))
            or control_pending
        ):
            return
        magnet_reader = getattr(self.controller, "read_display_snapshot_async", None)
        temperature_reader = getattr(self.controller, "read_display_temperature_async", None)
        if not callable(magnet_reader) or not callable(temperature_reader):
            self._show_error("Display telemetry broker is unavailable")
            return
        self._telemetry_generation = getattr(self.controller, "generation", None)
        cycle = {"magnet": None, "temperature": None, "done": set(),
                 "timed_out": set(), "errors": {}, "generation": self._telemetry_generation}
        cycle["started_at"] = time.monotonic()
        self._telemetry_cycle = cycle
        self.refresh_btn.setText("Refreshing…")
        try:
            cycle["magnet"] = magnet_reader(max_age_s=0.5, source="manual")
            cycle["temperature"] = temperature_reader(max_age_s=0.5, source="manual")
        except Exception as exc:
            self._telemetry_cycle = None
            self.refresh_btn.setText("Refresh telemetry")
            self._show_error(str(exc))
            self._refresh_controls()
            return
        self._refresh_controls()
        QTimer.singleShot(0, lambda: self._poll_telemetry(cycle))

    def _poll_telemetry(self, cycle=None):
        if cycle is None:
            cycle = self._telemetry_cycle
        if cycle is None or cycle is not self._telemetry_cycle:
            return
        current_generation = getattr(self.controller, "generation", cycle.get("generation"))
        if (cycle.get("generation") is not None and current_generation is not None
                and current_generation != cycle.get("generation")):
            self._telemetry_cycle = None
            self.refresh_btn.setText("Refresh telemetry")
            self.telemetry_note.setText("Telemetry refresh discarded after reconnect")
            self._refresh_controls()
            return
        for group, handle in (("magnet", cycle["magnet"]), ("temperature", cycle["temperature"])):
            if group in cycle["done"]:
                continue
            state = getattr(getattr(handle, "state", None), "name", "")
            drained_done = bool(getattr(handle, "drained_done", False))
            if state not in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT_DRAINING"}:
                continue
            if state == "TIMED_OUT_DRAINING" and not drained_done:
                cycle["timed_out"].add(group)
                self.telemetry_note.setText("Refresh timed out; waiting for device response")
                continue
            result = None
            try:
                result = handle.result(timeout=0)
            except Exception as exc:
                cycle["errors"][group] = exc
                if "timed out" in str(exc).lower() or state == "TIMED_OUT_DRAINING":
                    cycle["timed_out"].add(group)
            try:
                drained = handle.wait_drained(timeout=0)
            except concurrent.futures.TimeoutError:
                continue
            except Exception as exc:
                cycle["errors"].setdefault(group, exc)
                drained = None
            result = drained if drained is not None else result
            if result is not None:
                completed_at = getattr(handle, "completed_at", None)
                if group == "magnet":
                    self._on_snapshot(result, completed_at=completed_at)
                else:
                    self._on_temperature_snapshot(result, completed_at=completed_at)
            cycle["done"].add(group)
        if len(cycle["done"]) < 2:
            QTimer.singleShot(20, lambda: self._poll_telemetry(cycle))
            return
        if cycle.get("generation") == getattr(self.controller, "generation", cycle.get("generation")):
            self._ramp_auto_telemetry_ready = True
        self._telemetry_cycle = None
        self.refresh_btn.setText("Refresh telemetry")
        errors = [f"{group}: {error}" for group, error in cycle["errors"].items()]
        if errors:
            self._show_error("Telemetry failed: " + " | ".join(errors))
            self._append_log(
                f"Telemetry refresh partial/errors ({len(errors)} group(s)) after "
                f"{max(0.0, time.monotonic() - cycle.get('started_at', time.monotonic())):.3f}s"
            )
        else:
            self._append_log(
                f"Telemetry refresh completed in "
                f"{max(0.0, time.monotonic() - cycle.get('started_at', time.monotonic())):.3f}s"
            )
        if self._ramp_auto_pending and self._workflow_intent is None:
            self._start_ramp_tables_read(auto=True)
        if cycle["timed_out"] and not errors:
            self.telemetry_note.setText("Refresh timed out; waiting for device response")
        self._refresh_controls()

    def _poll_temperature_telemetry(self, handle):
        state = getattr(getattr(handle, "state", None), "name", "")
        if state not in {"SUCCEEDED", "FAILED", "CANCELLED"} and not (
                state == "TIMED_OUT_DRAINING" and bool(getattr(handle, "drained_done", False))):
            QTimer.singleShot(20, lambda: self._poll_temperature_telemetry(handle))
            return
        try:
            snapshot = handle.result(timeout=0)
            handle.wait_drained(timeout=0)
        except Exception as exc:
            self._show_error(f"Temperature telemetry failed: {exc}")
        else:
            self._on_temperature_snapshot(snapshot)

    @Slot()
    def read_ramp_tables(self):
        self._start_ramp_tables_read(auto=False)

    def view_ramp_tables(self):
        report = self._ramp_tables_report
        if report is None:
            self._show_error("No ramp-table report is cached yet")
            return
        self._show_ramp_tables_report(report)

    def _on_display_cycle_finished(self, generation):
        if self._closing or self._workflow_intent is not None:
            return
        if generation != getattr(self.controller, "generation", generation):
            return
        self._ramp_auto_telemetry_ready = True
        if self._ramp_auto_pending:
            self._start_ramp_tables_read(auto=True)

    def _start_ramp_tables_read(self, *, auto=False):
        if self._closing or self._workflow_intent is not None or self._workflow_waiting_drain:
            return
        if self._ramp_tables_handle is not None:
            return
        generation = getattr(self.controller, "generation", None)
        if auto and (not self._ramp_auto_pending or self._ramp_auto_attempted
                     or generation != self._ramp_auto_generation
                     or not self._ramp_auto_telemetry_ready):
            return
        if self.thread is not None and self.thread.isRunning():
            return
        if (getattr(self.controller, "_display_poll_cycle", None) is not None
                or self._telemetry_cycle is not None):
            return
        if (
            not self._connected
            or self.worker is not None
            or self._externally_busy
            or self._temperature_apply_handle is not None
            or bool(getattr(self.controller, "mode_recovery_required", False))
            or self._pending_work_snapshot().control_pending
        ):
            self._show_error("Ramp-table read is unavailable while the instrument is busy or disconnected")
            return
        pending_snapshot = self._pending_work_snapshot()
        pending = pending_snapshot.pending
        display_pending = pending_snapshot.display_pending
        owner_requests = getattr(self.controller, "_requests", {})
        non_display_pending = any(
            getattr(request, "command", None).name != "READ_RAMP_TABLES"
            and getattr(request, "source", "fresh") not in {"manual", "background", "monitor"}
            for request in owner_requests.values()
            if getattr(getattr(request, "state", None), "name", "")
            in {"QUEUED", "RUNNING", "TIMED_OUT_DRAINING"}
        )
        if pending_snapshot.control_pending or (pending and not display_pending and non_display_pending):
            return
        reader = getattr(self.controller, "read_ramp_tables_async", None)
        if not callable(reader):
            self._show_error("The connected attoDRY2100 does not expose ramp-table diagnostics")
            if auto:
                self._ramp_auto_attempted = True
                self._ramp_auto_pending = False
            return
        self._ramp_tables_auto_active = bool(auto)
        self._ramp_tables_request_generation = generation
        self._ramp_tables_request_token = self._ramp_tables_shutdown_token
        self._ramp_tables_started_at = time.monotonic()
        self._interlock_held = True
        self.run_state_changed.emit(True)
        self.ramp_tables_status.setText("Reading ramp tables…")
        self._ramp_tables_timed_out = False
        try:
            self._ramp_tables_handle = reader()
        except Exception as exc:
            self._ramp_tables_handle = None
            self.ramp_tables_status.setText("Ramp-table read failed")
            self._ramp_tables_cache_valid = False
            self._ramp_tables_last_error = str(exc)
            if auto:
                self._ramp_auto_attempted = True
                self._ramp_auto_pending = False
            self._show_error(str(exc))
            self._release_interlock_if_drained()
            self._refresh_controls()
            return
        accepted = getattr(self._ramp_tables_handle, "accepted", False) is True
        if accepted and self._ramp_auto_pending:
            self._ramp_auto_attempted = True
            self._ramp_auto_pending = False
        handle = self._ramp_tables_handle
        QTimer.singleShot(0, lambda: self._poll_ramp_tables(handle))
        self._refresh_controls()

    def _show_ramp_tables_report(self, report):
        self._ramp_tables_report = report
        if self._ramp_tables_dialog is not None:
            self._ramp_tables_dialog.close()
        generation = self._ramp_tables_cache_generation
        current_generation = getattr(self.controller, "generation", generation)
        generation_stale = (
            generation is not None and current_generation is not None
            and generation != current_generation
        )
        stale = not self._ramp_tables_cache_valid or generation_stale
        if generation_stale or not self._connected:
            stale_reason = "previous connection / stale"
        elif not self._ramp_tables_cache_valid and self._ramp_tables_last_error:
            stale_reason = "latest read failed; historical"
        elif stale:
            stale_reason = "historical / stale"
        else:
            stale_reason = None
        self._ramp_tables_dialog = _RampTablesDialog(
            report, self, generation=generation, read_at=self._ramp_tables_read_at,
            elapsed_s=self._ramp_tables_elapsed_s,
            stale=stale, stale_reason=stale_reason,
            last_error=self._ramp_tables_last_error,
        )
        self._ramp_tables_dialog.show()

    def _store_ramp_tables_report(self, report, *, elapsed_s=None, generation=None):
        self._ramp_tables_report = report
        self._ramp_tables_cache_generation = (
            generation if generation is not None
            else getattr(self.controller, "generation", None)
        )
        self._ramp_tables_read_at = datetime.now().astimezone()
        self._ramp_tables_elapsed_s = elapsed_s
        self._ramp_tables_cache_valid = True

    @staticmethod
    def _ramp_table_error_messages(report):
        """Return one ordered message per table error, without mutating report.

        The adapter preserves a row failure in both the table-level and row
        fields.  Deduplicate only within each table so the two SDK tables keep
        independent error evidence.
        """
        messages = []
        for table in (getattr(report, "current", None), getattr(report, "default", None)):
            if table is None:
                continue
            seen = set()
            table_errors = getattr(table, "errors", ()) or ()
            if isinstance(table_errors, (str, bytes)):
                table_errors = (table_errors,)
            candidates = [str(error) for error in table_errors if error is not None]
            candidates.extend(
                str(row.error)
                for row in (getattr(table, "rows", ()) or ())
                if getattr(row, "error", None) is not None
            )
            for message in candidates:
                if message not in seen:
                    seen.add(message)
                    messages.append(message)
        return messages

    def _poll_ramp_tables(self, handle=None):
        active = self._ramp_tables_handle
        if active is None or (handle is not None and handle is not active):
            return
        state = getattr(getattr(active, "state", None), "name", "")
        stale_request = (
            not self._connected
            or
            getattr(active, "generation", self._ramp_tables_request_generation)
            != getattr(self.controller, "generation", self._ramp_tables_request_generation)
            or self._ramp_tables_request_token != self._ramp_tables_shutdown_token
        )
        timed_out = self._ramp_tables_timed_out or state == "TIMED_OUT_DRAINING"
        if timed_out:
            self._ramp_tables_timed_out = True
        if state not in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT_DRAINING"}:
            QTimer.singleShot(25, lambda: self._poll_ramp_tables(active))
            return
        client_result = None
        client_error = None
        if state == "SUCCEEDED" and not timed_out:
            try:
                client_result = active.result(timeout=0)
            except Exception as exc:
                client_error = exc
                timed_out = "timed out" in str(exc).lower()
                self._ramp_tables_timed_out = timed_out
        elif state in {"FAILED", "CANCELLED"}:
            try:
                client_result = active.result(timeout=0)
            except Exception as exc:
                client_error = exc
        try:
            drained = active.wait_drained(timeout=0)
        except concurrent.futures.TimeoutError:
            if not stale_request:
                self.ramp_tables_status.setText(
                    "Read timed out; waiting for owner drain…"
                    if timed_out else "Reading ramp tables…"
                )
            QTimer.singleShot(25, lambda: self._poll_ramp_tables(active))
            return
        except Exception as exc:
            client_error = client_error or exc
            drained = None
        report = drained if drained is not None else client_result
        auto = self._ramp_tables_auto_active
        self._ramp_tables_auto_active = False
        self._ramp_tables_handle = None
        if stale_request:
            self._refresh_controls()
            self._release_interlock_if_drained()
            return
        stored_current = False
        ramp_errors = []
        if report is not None and hasattr(report, "current"):
            elapsed_s = (
                max(0.0, time.monotonic() - self._ramp_tables_started_at)
                if self._ramp_tables_started_at is not None else None
            )
            self._store_ramp_tables_report(
                report, elapsed_s=elapsed_s,
                generation=self._ramp_tables_request_generation,
            )
            stored_current = True
            ramp_errors = self._ramp_table_error_messages(report)
            if ramp_errors:
                self._ramp_tables_last_error = (
                    f"{len(ramp_errors)} ramp-table error(s): "
                    + " | ".join(ramp_errors)
                )
                self._append_log(
                    f"Ramp-table read completed with {len(ramp_errors)} error(s): "
                    + " | ".join(ramp_errors)
                )
        if client_error is not None:
            reason = str(client_error)
            self.ramp_tables_status.setText(
                "Read timed out; partial results retained; "
                f"error: {reason}" if timed_out else f"Read finished with errors: {reason}"
            )
            self._ramp_tables_cache_valid = False
            self._ramp_tables_last_error = reason
            self._show_error(reason)
        elif timed_out:
            suffix = f" ({len(ramp_errors)} error(s))" if ramp_errors else ""
            self.ramp_tables_status.setText(
                f"Read timed out; partial results retained{suffix}"
            )
        elif report is not None and (
            getattr(report, "interrupted", False)
            or any(
                getattr(table, "errors", ()) or ()
                for table in (getattr(report, "current", None), getattr(report, "default", None))
                if table is not None
            )
            or any(
                getattr(row, "error", None)
                for table in (getattr(report, "current", None), getattr(report, "default", None))
                if table is not None
                for row in (getattr(table, "rows", ()) or ())
            )
        ):
            self.ramp_tables_status.setText(
                f"Read finished with errors ({len(ramp_errors)} error(s))"
                if ramp_errors else "Read finished with errors"
            )
            self._ramp_tables_cache_valid = True
        else:
            self.ramp_tables_status.setText("Ramp tables read")
            self._ramp_tables_last_error = None
        if stored_current and self._ramp_tables_read_at is not None:
            elapsed = (
                f" · elapsed {self._ramp_tables_elapsed_s:.3f}s"
                if self._ramp_tables_elapsed_s is not None else ""
            )
            self.ramp_tables_status.setToolTip(
                f"Read at {self._ramp_tables_read_at.isoformat()}{elapsed}"
            )
            self._append_log(
                f"Ramp-table read {'automatic' if auto else 'manual'} completed; "
                f"read_at={self._ramp_tables_read_at.isoformat()}"
                + (f" elapsed={self._ramp_tables_elapsed_s:.3f}s"
                   if self._ramp_tables_elapsed_s is not None else "")
            )
        self._refresh_controls()
        self._release_interlock_if_drained()
        # The owner terminal signal may be queued behind this polling callback;
        # retry once after that bookkeeping opportunity without issuing any
        # device request.
        QTimer.singleShot(0, self._release_interlock_if_drained)

    @Slot(object)
    def _on_connected(self, identity=None):
        if self._closing:
            return
        if self._workflow_intent is not None:
            self._advance_workflow_intent(self._workflow_intent.token)
            return
        generation = getattr(self.controller, "generation", None)
        if self._connected and generation == self._ramp_auto_generation:
            self._refresh_controls()
            return
        self._connected = True
        if (self._ramp_tables_cache_generation is not None
                and generation != self._ramp_tables_cache_generation):
            self._ramp_tables_cache_valid = False
        self._ramp_auto_generation = generation
        self._ramp_auto_pending = True
        self._ramp_auto_attempted = False
        self._ramp_auto_telemetry_ready = False
        self._detached_after_completion = False
        self._telemetry_cycle = None
        self._last_magnet_success_at = None
        self._last_temperature_success_at = None
        host = getattr(identity, "host", "") if identity is not None else ""
        self.connection_status.setText(f"Connected{f' — {host}' if host else ''}")
        self.connect_btn.setText("Connect")
        self.telemetry_note.setText("Live telemetry — awaiting update")
        self._terminal_status = "Ready"
        self.status.setText("Ready")
        polling = getattr(self.controller, "set_polling_enabled", None)
        if callable(polling):
            polling(True)
        self._refresh_controls()

    @Slot()
    def _on_disconnected(self):
        if self._workflow_intent is not None:
            intent = self._workflow_intent
            self._workflow_intent = None
            self._workflow_intent_token += 1
            self._workflow_waiting_drain = self._pending_work_snapshot().pending
            self._finalize_workflow_intent(intent, error="workflow handoff cancelled after disconnect")
        # Disconnection invalidates saved state even when no waiting intent
        # was installed (for example, an idle worker handoff).
        self._workflow_restore_state = None
        self._connected = False
        self._ramp_auto_pending = False
        self._ramp_tables_cache_valid = False
        if self._ramp_tables_report is not None:
            self.ramp_tables_status.setText("Ramp tables are historical (previous connection)")
        elif self._ramp_tables_last_error:
            self.ramp_tables_status.setText(
                f"Ramp-table read failed; historical ({self._ramp_tables_last_error})"
            )
        else:
            self.ramp_tables_status.setText("Ramp tables unavailable (disconnected)")
        self._telemetry_cycle = None
        polling = getattr(self.controller, "set_polling_enabled", None)
        if callable(polling):
            polling(False)
        self._temperature_monitor_timer.stop()
        self._temperature_monitor_handle = None
        self.sample_temperature_setpoint_value.setText("N/A")
        self.temperature_apply_status.setText("Disconnected")
        if self._last_magnet_success_at is not None or self._last_temperature_success_at is not None:
            self.telemetry_note.setText("Telemetry values are last-known, not live")
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
        if name == "RECOVERY_REQUIRED":
            self._terminal_status = "Magnet mode recovery required"
            self.status.setText(self._terminal_status)
        if name == "DETACHED":
            self._detached_after_completion = True
        if name == "DISCONNECTED" and self._detached_after_completion:
            self._connected = False
            self._show_completed_detach()
            self._refresh_controls()
        else:
            self._refresh_controls()
        self._release_interlock_if_drained()
        self._restore_workflow_display_state()

    @Slot()
    def _on_work_status_changed(self):
        """Refresh pending-owner controls after terminal bookkeeping."""
        if self._workflow_intent is not None:
            self._advance_workflow_intent(self._workflow_intent.token)
            self._refresh_controls()
            return
        if self._ramp_auto_pending:
            self._start_ramp_tables_read(auto=True)
        self._refresh_controls()
        self._release_interlock_if_drained()
        self._restore_workflow_display_state()

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
    def _on_snapshot(self, snapshot, *, completed_at=None, generation=None):
        generation = generation if generation is not None else getattr(snapshot, "generation", None)
        if hasattr(snapshot, "value") and hasattr(snapshot, "completed_at"):
            completed_at = snapshot.completed_at
            snapshot = snapshot.value
        current_generation = getattr(self.controller, "generation", generation)
        if (generation is not None and current_generation is not None
                and generation != current_generation):
            return
        progress = self._preparation_progress
        sampled_at = getattr(snapshot, "monotonic_s", None)
        if (progress is not None and progress["stage"] == "mode readiness"
                and not self._preparation_closed and sampled_at is not None
                and sampled_at >= progress["started_at"]
                and (progress["sampled_at"] is None or sampled_at > progress["sampled_at"])):
            # Mode preparation already publishes owner snapshots. Reuse them.
            self._preparation_progress = dict(progress, sampled_at=sampled_at,
                field_t=getattr(snapshot, "field_t", None),
                mode_observation=preparation_mode_observation(snapshot))
            self._refresh_activity()
        self._last_telemetry_time = datetime.now().astimezone()
        self._last_magnet_success_at = completed_at if completed_at is not None else time.monotonic()
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
            self._refresh_telemetry_age()

    @Slot(object)
    def _on_temperature_snapshot(self, snapshot, *, completed_at=None, generation=None):
        generation = generation if generation is not None else getattr(snapshot, "generation", None)
        if hasattr(snapshot, "value") and hasattr(snapshot, "completed_at"):
            completed_at = snapshot.completed_at
            snapshot = snapshot.value
        current_generation = getattr(self.controller, "generation", generation)
        if (generation is not None and current_generation is not None
                and generation != current_generation):
            return
        self._last_temperature_success_at = completed_at if completed_at is not None else time.monotonic()
        def display(value):
            return (
                f"{float(value):.6g} K"
                if isinstance(value, (int, float)) and math.isfinite(float(value)) else "N/A"
            )
        sample_temperature = getattr(snapshot, "sample_temperature_k", None)
        self.sample_temperature_value.setText(display(sample_temperature))
        if isinstance(sample_temperature, (int, float)) and math.isfinite(float(sample_temperature)):
            self._last_sample_temperature_k = float(sample_temperature)
            self._update_condition_preview()
        self.vti_temperature_value.setText(
            display(getattr(snapshot, "vti_temperature_k", None))
        )
        sample_setpoint = getattr(snapshot, "sample_setpoint_k", None)
        self.sample_temperature_setpoint_value.setText(display(sample_setpoint))
        if isinstance(sample_setpoint, (int, float)) and math.isfinite(float(sample_setpoint)):
            self._last_sample_setpoint_k = float(sample_setpoint)
        active = getattr(snapshot, "sample_control_active", None)
        self.sample_temperature_control_value.setText(
            "Active" if active is True else "Inactive" if active is False else "N/A"
        )
        if active is not True:
            self.temperature_apply_status.setText("Control inactive")
        elif self._temperature_is_stable(snapshot):
            self.temperature_apply_status.setText(
                f"Stable at {float(sample_temperature):.6g} K"
            )
        elif isinstance(sample_setpoint, (int, float)) and math.isfinite(float(sample_setpoint)):
            self.temperature_apply_status.setText(f"Ramping to {float(sample_setpoint):.6g} K")
        else:
            self.temperature_apply_status.setText("Temperature control active")

    def _refresh_telemetry_age(self) -> None:
        if not self._connected:
            return
        now = time.monotonic()
        def age(value):
            return "never" if value is None else f"{max(0.0, now - value):.1f}s ago"
        self.telemetry_note.setText(
            f"Live telemetry · Magnet {age(self._last_magnet_success_at)} · "
            f"Temperature {age(self._last_temperature_success_at)}"
        )

    @Slot(str)
    def _show_error(self, message):
        self.error_display.setPlainText(str(message))
        self.error_display.setVisible(True)
        if hasattr(self, "_log"):
            self._append_log(f"ERROR: {message}")

    @Slot()
    def prepare_magnet(self):
        admission_error = self._workflow_admission_error("magnet_preparation")
        if admission_error is not None:
            self._show_error(admission_error)
            return
        if (self.worker is not None or self._temperature_apply_handle is not None
                or self._ramp_tables_handle is not None
                or self._workflow_intent is not None):
            self._show_error("Wait for the current operation to finish")
            return
        if self._pending_work_snapshot().control_pending:
            self._show_error("attoDRY2100 owner work is still draining")
            return
        if self._externally_busy:
            self._show_error("Another MCD workflow is using the shared instruments")
            return
        if not self._connected:
            self._show_error("Connect the attoDRY2100 before preparing the magnet")
            return
        try:
            start_field = float(self.start_field.text().strip())
            if not math.isfinite(start_field) or abs(start_field) > 6.0:
                raise ValueError("Start field must be finite and within ±6 T")
        except (TypeError, ValueError) as exc:
            self._show_error(str(exc))
            return
        try:
            worker = MagnetPreparationWorker(
                self.controller, start_field,
                targets_t=(start_field,),
                gate_t=float(cfg.mcd2100.gate_t or 0.001),
                poll_interval_s=cfg.attodry2100.poll_interval_s,
                timeout_s=cfg.attodry2100.mode_prepare_timeout_s,
                position_timeout_s=cfg.attodry2100.position_timeout_s,
                operation_timeout_s=cfg.mcd2100.operation_timeout_s,
                cleanup_timeout_s=cfg.mcd2100.operation_timeout_s,
            )
        except Exception as exc:
            self._show_error(f"Magnet preparation could not start: {exc}")
            return
        self.error_display.clear(); self.error_display.setVisible(False)
        self._handoff_worker(worker, "magnet_preparation")

    def _launch_worker(self, worker, operation="measurement", *, from_intent=False):
        polling = getattr(self.controller, "set_polling_enabled", None)
        if callable(polling):
            polling(False)
        runner = None
        thread = None
        try:
            runner = _Runner(worker)
            runner.progress.connect(self._on_progress)
            runner.spectrum_event.connect(self._on_spectrum_event)
            runner.log.connect(self._append_log)
            runner.phase.connect(self._on_phase)
            runner.preparation_progress.connect(self._on_preparation_progress)
            thread = QThread(self)
            runner.moveToThread(thread)
            thread.started.connect(runner.run)
            runner.finished.connect(self.terminal)
            runner.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
            thread.finished.connect(self._thread_finished)
            # Publish panel ownership only after all pre-start construction
            # and signal wiring has succeeded.
            self.worker = worker
            self.runner = runner
            self.thread = thread
        except Exception:
            self.worker = self.runner = self.thread = None
            if runner is not None:
                runner.deleteLater()
            if thread is not None:
                thread.deleteLater()
            raise
        self._operation = operation
        self._preparation_progress = None
        self._preparation_closed = False
        self._terminal_status = "Preparing magnet" if operation == "magnet_preparation" else "Running"
        self.status.setText(self._terminal_status)
        self._active_phase = self._terminal_status
        self._phase_started_at = time.monotonic()
        self._last_spectrum_at = None
        self._spectrum_count = 0
        self.progress_bar.setValue(0)
        if not self._interlock_held:
            self._interlock_held = True
            self.run_state_changed.emit(True)
        self._refresh_controls()
        try:
            self.thread.start()
        except Exception:
            self.worker = self.runner = self.thread = None
            runner.deleteLater()
            thread.deleteLater()
            raise

    @Slot()
    def start(self):
        admission_error = self._workflow_admission_error("measurement")
        if admission_error is not None:
            self._show_error(admission_error)
            return
        if (self.worker is not None or self._ramp_tables_handle is not None
                or self._workflow_intent is not None):
            return
        if self._pending_work_snapshot().control_pending:
            self._show_error("attoDRY2100 owner work is still draining")
            return
        if bool(getattr(self.controller, "mode_recovery_required", False)):
            self._show_error("Magnet mode recovery is required; use Prepare magnet first")
            return
        if self._temperature_apply_handle is not None:
            self._show_error("Wait for the temperature target to finish applying")
            return
        if self._externally_busy:
            self._show_error("Another MCD workflow is using the shared instruments")
            return
        if not self._connected:
            self._show_error("Connect the attoDRY2100 before starting")
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
            point = self._point.text().strip()
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
            filename_temperature_k, filename_temperature_source = self._filename_temperature()
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
                    "mcd2100_settings_version": 4,
                    "point": point,
                    "start_field_t": start_field, "stop_field_t": stop_field,
                    "angles_deg": angles, "rotator": rotator_name,
                    "lf_center_nm": self.lf_center.value(), "lf_exposure_ms": self.lf_exposure.value(),
                    "lf_frames": self.lf_frames.value(), "gate_ratio": self._gate_ratio(),
                    "gate_vtg_factor": self.gate_vtg_factor.value(),
                    "gate_vbg_factor": self.gate_vbg_factor.value(),
                    "initial_voltage_settle_s": self.initial_voltage_settle.value(),
                    "voltage_settle_s": self.voltage_settle.value(),
                    "gate_conditions": conditions,
                    "gate_batches": list(self._gate_batch_provenance),
                    "temperature_control_enabled": self.temperature_control_enabled.isChecked(),
                    "sample_target_k": self.sample_target.value(),
                    "sample_ramp_rate_k_per_min": self.sample_ramp_rate.value(),
                    "temperature_tolerance_k": self.temperature_tolerance.value(),
                    "temperature_stable_s": self.temperature_stable.value(),
                    "temperature_timeout_s": self.temperature_timeout.value(),
                    "filename_temperature_k": filename_temperature_k,
                    "filename_temperature_source": filename_temperature_source,
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
                    instruments=instrument_inventory(lightfield=self._lf6, magnet=self.controller,
                                                     rotation=self._rotation, smu=self._smu),
                )
                self._experiment_run.record_event(
                    "plan_requested", plan_id="mcd2100-plan-1",
                    plan_summary={"condition_count": len(enabled_conditions),
                                  "angle_count": len(angles),
                                  "start_T": start_field, "stop_T": stop_field},
                )
                for condition_index, condition in enumerate(enabled_conditions, 1):
                    self._experiment_run.register_condition(
                        condition, condition_id=f"condition-{condition_index}")
                bind_lightfield_metadata(self._lf6, self._experiment_run)
            optical = _LazyOpticalService(self._optical_factory) if self._optical_factory else _LightFieldRotationService(
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
                initial_voltage_settle_s=self.initial_voltage_settle.value(),
                voltage_settle_s=self.voltage_settle.value(),
                filename_temperature_k=filename_temperature_k,
                filename_temperature_source=filename_temperature_source,
                **self._continuous_settings(),
                metadata={
                    "device_id": self._sample_id.text().strip(),
                    "point": point,
                    "filename_temperature_k": filename_temperature_k,
                    "filename_temperature_source": filename_temperature_source,
                    "experiment_type": "mcd_attodry2100",
                },
                metadata_run=self._experiment_run,
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
        self.error_display.setVisible(False)
        self._plot_overlay.setText("Waiting for first spectrum")
        self.spectrum_activity.setText("Spectrum 0 · no spectrum yet")
        self._handoff_worker(worker, "measurement", self._experiment_run)

    @Slot()
    def stop(self):
        if self.worker is None:
            intent = self._workflow_intent
            if intent is not None:
                self._workflow_intent = None
                self._workflow_intent_token += 1
                self._finalize_workflow_intent(intent)
                self._workflow_waiting_drain = self._pending_work_snapshot().display_pending
                self._terminal_status = "Workflow handoff cancelled"
                self.status.setText(self._terminal_status)
                self.stop_btn.setText("Stop")
                self._refresh_controls()
                self._release_interlock_if_drained()
                self._restore_workflow_display_state()
            return
        self.worker.request_cancel()
        self._on_phase("Cancellation requested — waiting for safe cleanup")

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
        self.direction_value.setText(str(event.get("direction", "N/A")).title())
        self._last_spectrum_at = time.monotonic()
        self._spectrum_count = int(event.get("total_spectra", self._spectrum_count + 1))
        gate_index = int(event.get("gate_index", 1))
        gate_count = int(event.get("gate_count", 1))
        field_t = float(event.get("B1_T", 0.0))
        self._plot_overlay.setText(
            f"Spectrum {self._spectrum_count} · Gate {gate_index}/{gate_count}\n"
            f"{label} · {field_t:+.6g} T"
        )
        if wavelengths and counts:
            self._plot_overlay.setPos(float(min(wavelengths)), float(max(counts)))
        self._refresh_activity()

    @Slot(str)
    def _on_phase(self, message: str) -> None:
        sender = self.sender()
        if isinstance(sender, _Runner) and (sender is not self.runner or self._preparation_closed
                and self._terminal_status in {"FAILED", "CANCELLED", "COMPLETED", "Magnet ready at Start"}):
            return
        if str(message).startswith(("Cancellation", "Run failed", "Stopping magnet")):
            self._preparation_progress = None
            self._preparation_closed = True
        self._active_phase = str(message)
        self._phase_started_at = time.monotonic()
        self._settle_deadline = None
        if self._active_phase.startswith("Gate settling") and self._active_phase.endswith(" s"):
            try:
                settle_s = float(self._active_phase.rsplit(":", 1)[1][:-2].strip())
                self._settle_deadline = self._phase_started_at + max(0.0, settle_s)
            except (IndexError, ValueError):
                pass
        self.status.setText(self._active_phase)
        self._append_log(self._active_phase)
        self._refresh_activity()

    @Slot(object)
    def _on_preparation_progress(self, progress):
        if self.worker is None or self.sender() is not self.runner or self._preparation_closed:
            return
        if progress["stage"] in {"ready", "position timeout"}:
            self._preparation_progress = None
            self._preparation_closed = True
            return
        self._preparation_progress = dict(progress)
        if progress["stage"] == "mode readiness":
            self._mode_progress_logged_at = time.monotonic()
        self._refresh_activity()

    @Slot()
    def _refresh_activity(self) -> None:
        now = time.monotonic()
        running = self.worker is not None
        preparation = self._preparation_progress if running else None
        if preparation is not None:
            self.status.setText(format_preparation_progress(preparation, now))
            if preparation["stage"] == "mode readiness" and now - self._mode_progress_logged_at >= 15.:
                self._append_log(format_preparation_progress(preparation, now))
                self._mode_progress_logged_at = now
        since_spectrum = (
            None if self._last_spectrum_at is None
            else max(0.0, now - self._last_spectrum_at)
        )
        phase_lower = self._active_phase.lower()
        waiting = any(token in phase_lower for token in (
            "settling", "ramping gate", "positioning", "configuring", "starting"
        ))
        if preparation is not None:
            self.run_activity.setText("● Preparing magnet")
            self.run_activity.setStyleSheet("color: #b45309; font-weight: 700;")
        elif running and since_spectrum is not None and since_spectrum < 1.5:
            self.run_activity.setText("● New spectrum")
            self.run_activity.setStyleSheet("color: #15803d; font-weight: 700;")
        elif running and self._settle_deadline is not None:
            remaining = max(0, int(math.ceil(self._settle_deadline - now)))
            self.run_activity.setText(f"● Settling · {remaining} s remaining")
            self.run_activity.setStyleSheet("color: #b45309; font-weight: 700;")
        elif running and waiting:
            self.run_activity.setText("● Setup / waiting")
            self.run_activity.setStyleSheet("color: #b45309; font-weight: 700;")
        elif running:
            self.run_activity.setText("● Running")
            self.run_activity.setStyleSheet("color: #2563eb; font-weight: 700;")
        else:
            self.run_activity.setText(f"● {self._terminal_status}")
            self.run_activity.setStyleSheet("color: #6b7280;")

        if since_spectrum is None and not running:
            text = "Spectrum 0 · no spectrum yet"
        elif since_spectrum is None:
            elapsed = max(0.0, now - self._phase_started_at)
            text = f"Spectrum 0 · waiting {elapsed:.0f} s"
        else:
            text = f"Spectrum {self._spectrum_count} · updated {since_spectrum:.1f} s ago"
        expected_interval = max(
            15.0,
            3.0 * self.lf_exposure.value() * self.lf_frames.value() / 1000.0 + 5.0,
        )
        expecting = running and phase_lower.startswith("acquiring spectra")
        phase_age = max(0.0, now - self._phase_started_at)
        reference_age = phase_age if since_spectrum is None else min(since_spectrum, phase_age)
        if expecting and reference_age > expected_interval:
            text += " · no recent spectrum"
            self.spectrum_activity.setStyleSheet("color: #b91c1c; font-weight: 700;")
        else:
            self.spectrum_activity.setStyleSheet("color: #4b5563;")
        self.spectrum_activity.setText(text)

    @Slot(str)
    def _append_log(self, message: str) -> None:
        timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
        self._log.appendPlainText(f"[{timestamp}] {message}")

    @Slot(object)
    def _on_terminal(self, result):
        self._preparation_progress = None
        self._preparation_closed = True
        terminal = str(result.get("status", "FAILED")).upper()
        if terminal not in {"COMPLETED", "CANCELLED", "FAILED"}:
            terminal = "FAILED"
        standalone_prepare = result.get("operation") == "magnet_preparation"
        if standalone_prepare and terminal == "COMPLETED":
            self._detached_after_completion = False
            self._connected = True
            self._terminal_status = "Magnet ready at Start"
            self.status.setText(self._terminal_status)
        elif standalone_prepare:
            self._terminal_status = terminal
            self.status.setText(
                "Preparation cancelled; device mode transition may continue. Use Prepare magnet to recheck readiness."
                if terminal == "CANCELLED" or result.get("recovery_required")
                else terminal
            )
        elif terminal == "COMPLETED":
            self._detached_after_completion = True
            self._connected = False
            self._show_completed_detach()
        else:
            self._terminal_status = terminal
            self.status.setText(self._terminal_status)
        spectra = int(result.get("spectra_written", 0))
        self.progress.setText(f"{spectra} spectra")
        self.progress_bar.setValue(100 if terminal == "COMPLETED" else 0)
        self._active_phase = self._terminal_status
        self._phase_started_at = time.monotonic()
        self._append_log(f"Run finished: {terminal}; {spectra} spectra written")
        self._refresh_activity()
        primary_error = result.get("error")
        cleanup_error = result.get("cleanup_error")
        errors = [str(value) for value in (primary_error, cleanup_error) if value]
        if errors:
            self._show_error("\n".join(errors))
        run = getattr(self, "_experiment_run", None)
        if standalone_prepare:
            self._refresh_controls()
            return
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
                    run.register_file(
                        legacy, "intermediate",
                        details={"compatibility_projection": True,
                                 "derived_from_experiment_id": run.experiment_id},
                    )
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
        # Capture before releasing the shared interlock: its synchronous
        # callback may re-enter the panel and consume saved state.
        had_restore_state = self._workflow_restore_state is not None
        if self.runner is not None:
            self.runner.deleteLater()
        if self.thread is not None:
            self.thread.deleteLater()
        self.worker = self.runner = self.thread = None
        if self._ramp_auto_pending:
            self._start_ramp_tables_read(auto=True)
        self._release_interlock_if_drained()
        self._restore_workflow_display_state()
        if (not had_restore_state and self._workflow_restore_state is None
                and self._connected and not self._closing):
            polling = getattr(self.controller, "set_polling_enabled", None)
            if callable(polling):
                polling(True)
        self.status.setText(self._terminal_status)
        self._refresh_controls()

    def _refresh_controls(self):
        running = self.worker is not None
        applying_temperature = self._temperature_apply_handle is not None
        reading_ramp = self._ramp_tables_handle is not None
        recovery = bool(getattr(self.controller, "mode_recovery_required", False))
        pending_snapshot = self._pending_work_snapshot()
        pending = pending_snapshot.pending
        display_pending = pending_snapshot.display_pending
        control_pending = pending_snapshot.control_pending
        refreshing = self._telemetry_cycle is not None
        waiting_intent = self._workflow_intent is not None
        # An in-flight display cycle is admissible for a workflow handoff;
        # the intent pauses its continuations and waits for both owners.
        blocked = recovery or control_pending or reading_ramp or waiting_intent
        self.start_btn.setEnabled(
            self._connected and not running and not blocked and not self._externally_busy and not applying_temperature
        )
        self.prepare_magnet_btn.setEnabled(
            self._connected and not running and not control_pending and not reading_ramp
            and not waiting_intent and not self._externally_busy and not applying_temperature
        )
        self.stop_btn.setEnabled(running or waiting_intent)
        self.stop_btn.setText("Cancel waiting" if waiting_intent and not running else "Stop")
        self.connect_btn.setEnabled(not self._connected and not running and self._connect_handle is None)
        self.disconnect_btn.setEnabled(
            self._connected and not running and self._disconnect_handle is None
            and not applying_temperature and not blocked and not refreshing
        )
        self.read_ramp_tables_btn.setEnabled(
            self._connected and not running and not blocked and not refreshing
            and not self._externally_busy
        )
        self.view_ramp_tables_btn.setEnabled(self._ramp_tables_report is not None)
        self.refresh_btn.setEnabled(
            self._connected and not running and not blocked and not refreshing
            and not applying_temperature
        )
        self.temperature_control_enabled.setEnabled(not running and not blocked)
        self.initial_voltage_settle.setEnabled(not running)
        self.voltage_settle.setEnabled(not running)
        self._update_temperature_controls()

    def _release_interlock_if_drained(self) -> None:
        """Release the shared workflow lock only after owner work drains."""
        if not self._interlock_held:
            return
        # Runner completion is a separate lifecycle from owner request drain;
        # never unlock while this panel still owns a live worker/thread.
        if self.worker is not None:
            return
        if self.thread is not None and self.thread.isRunning():
            return
        if self._workflow_intent is not None:
            return
        if self._workflow_waiting_drain:
            if self._pending_work_snapshot().pending:
                return
            self._workflow_waiting_drain = False
        # A timed-out read can finish its client future before the SDK owner
        # has drained. Keep the shared workflow lock until that owner work is
        # fully terminal, even if a stale status signal arrives first.
        if self._ramp_tables_handle is not None:
            return
        if bool(getattr(self.controller, "mode_recovery_required", False)):
            return
        if self._pending_work_snapshot().control_pending:
            return
        self._interlock_held = False
        self.run_state_changed.emit(False)

    def shutdown(self, timeout_ms=30_000):
        restore_state = self._workflow_restore_state or SimpleNamespace(
            polling=bool(getattr(self.controller, "_display_polling_enabled", False)),
            monitor=self._temperature_monitor_timer.isActive(),
            generation=getattr(self.controller, "generation", None),
        )

        def refused():
            # MainWindow keeps the window open when shutdown is refused.
            # Recovery must remain available, with display work deferred until
            # the existing owner and any uncertain mode transition are safe.
            self._closing = False
            self._workflow_restore_state = restore_state
            self._telemetry_age_timer.start()
            self._restore_workflow_display_state()
            self._refresh_controls()
            return False

        self._closing = True
        if self._workflow_intent is not None:
            intent = self._workflow_intent
            self._workflow_intent = None
            self._workflow_intent_token += 1
            self._workflow_waiting_drain = self._pending_work_snapshot().pending
            self._finalize_workflow_intent(intent, error="workflow handoff cancelled during shutdown")
        # Prevent late terminal callbacks from restoring a prior connection.
        self._workflow_restore_state = None
        self._temperature_monitor_timer.stop()
        self._telemetry_age_timer.stop()
        self._telemetry_cycle = None
        self._ramp_auto_pending = False
        self._ramp_tables_shutdown_token += 1
        polling = getattr(self.controller, "set_polling_enabled", None)
        if callable(polling):
            polling(False)
        if self.worker is not None:
            self.worker.request_cancel()
        thread = self.thread
        if thread is not None and thread.isRunning():
            if not thread.wait(int(timeout_ms)):
                return refused()
        if self._ramp_tables_handle is not None:
            cancel = getattr(self.controller, "cancel_ramp_tables", None)
            if callable(cancel):
                cancel()
            self._show_error("Ramp-table read is still draining")
            return refused()
        if bool(getattr(self.controller, "mode_recovery_required", False)):
            self._show_error("Magnet mode recovery is required; use Prepare magnet before shutdown")
            return refused()
        if bool(getattr(self.controller, "has_pending_work", False)):
            self._show_error("attoDRY2100 owner work is still draining")
            return refused()
        return True
