"""Threaded ND calibration and session reference measurement helpers."""
from __future__ import annotations

import ast
import math
import threading
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QWidget

from app.nd_calibration import save_calibration
from app.power_reading import power_correction_factor, power_reading_lock, read_power
from utils.config import cfg


class NDCalibrationWorker(QObject):
    """Move a stage and collect corrected PM readings without spectra."""
    progress = Signal(int, int)
    reading = Signal(float, float, float)
    finished = Signal(object)
    error = Signal(str)
    changed = Signal()
    busy_changed = Signal(bool)

    def __init__(self, stage, pm, positions, *, settle_s=0.3, samples=3,
                 reference_only=False, correction_factor=None, wavelength_nm=None):
        super().__init__()
        self._stage, self._pm = stage, pm
        self._positions = np.asarray(positions, dtype=float).ravel()
        self._settle_s = float(settle_s)
        if not math.isfinite(self._settle_s) or self._settle_s < 0:
            raise ValueError("Settle time must be finite and non-negative.")
        self._samples = int(samples)
        if self._samples < 1:
            raise ValueError("At least one power sample is required.")
        self._reference_only = bool(reference_only)
        self._factor = power_correction_factor(correction_factor)
        self._wavelength_nm = None if wavelength_nm is None else float(wavelength_nm)
        if self._wavelength_nm is not None and (not math.isfinite(self._wavelength_nm) or self._wavelength_nm <= 0):
            raise ValueError("Wavelength must be finite and positive.")
        self._stop = threading.Event()

    def request_stop(self):
        self._stop.set()

    def _validate_positions(self):
        if self._stage is None or self._pm is None:
            raise RuntimeError("Stage and connected power meter are required.")
        if self._positions.size == 0 or np.any(~np.isfinite(self._positions)):
            raise ValueError("Calibration positions must be finite and non-empty.")
        lo = getattr(self._stage, "minimum_position", None)
        hi = getattr(self._stage, "maximum_position", None)
        if lo is not None or hi is not None:
            if lo is None or hi is None:
                raise ValueError("Stage range is incomplete.")
            lo, hi = float(lo), float(hi)
            if not math.isfinite(lo) or not math.isfinite(hi) or lo > hi:
                raise ValueError("Stage range is invalid.")
            if np.any(self._positions < lo) or np.any(self._positions > hi):
                raise ValueError(f"Calibration position is outside stage range [{lo:g}, {hi:g}].")

    def _read_position(self, target):
        getter = getattr(self._stage, "get_position", None)
        if not callable(getter):
            raise RuntimeError("Stage position readback is required for calibration.")
        actual = float(getter())
        if not math.isfinite(actual):
            raise ValueError("Stage returned a non-finite position.")
        return actual

    def _read_sample(self):
        result = read_power(self._pm, factor=self._factor)
        raw_w, corrected_w = float(result.raw_w), float(result.corrected_w)
        if (not math.isfinite(raw_w) or raw_w <= 0 or not math.isfinite(corrected_w) or corrected_w <= 0):
            raise ValueError("Power meter returned a non-finite or non-positive reading.")
        return corrected_w * 1e6, raw_w * 1e6

    @Slot()
    def run(self):
        self.busy_changed.emit(True)
        try:
            self._validate_positions()
            if self._wavelength_nm is not None:
                configure = getattr(self._pm, "configure_wavelength", None)
                if not callable(configure):
                    raise RuntimeError("Power meter wavelength configuration is unavailable.")
                with power_reading_lock:
                    configure(self._wavelength_nm)
            points = []
            for index, target in enumerate(self._positions, 1):
                target = float(target)
                if self._stop.is_set(): break
                self._stage.move_to(target)
                if self._settle_s and self._stop.wait(self._settle_s): break
                if self._stop.is_set(): break
                actual = self._read_position(target)
                corrected, raw = [], []
                for _ in range(self._samples):
                    if self._stop.is_set(): break
                    c, r = self._read_sample()
                    corrected.append(c); raw.append(r)
                if self._stop.is_set() or not corrected: break
                c, r = float(np.mean(corrected)), float(np.mean(raw))
                if not (math.isfinite(c) and c > 0 and math.isfinite(r) and r > 0):
                    raise ValueError("Averaged power reading is invalid.")
                points.append((actual, c, r))
                self.reading.emit(actual, c, r)
                self.progress.emit(index, len(self._positions))
            if self._stop.is_set():
                self.finished.emit(None)
            elif self._reference_only:
                if not points: raise ValueError("Reference measurement produced no reading.")
                self.finished.emit((points[0][0], points[0][1]))
            else:
                self.finished.emit(points)
        except Exception as exc:
            self.error.emit(str(exc)); self.finished.emit(None)
        finally:
            self.busy_changed.emit(False)


def install_calibration(points, *, persist=True, profile_name=None, wavelength_nm=None, correction_factor=None):
    """Validate and install completed worker points, preserving raw readings."""
    if points is None or len(points) < 2:
        raise ValueError("Calibration requires at least two completed points.")
    try:
        positions = [float(p[0]) for p in points]
        corrected = [float(p[1]) for p in points]
        raw = [float(p[2]) for p in points]
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError("Calibration points must contain position, corrected, and raw power.") from exc
    if correction_factor is None:
        # The legacy panel invokes this helper directly after a worker which
        # froze the meter's current factor; retain that factor in the profile.
        correction_factor = power_correction_factor()
    return save_calibration(positions, corrected, raw_powers=raw, persist=persist,
                            profile_name=profile_name, wavelength_nm=wavelength_nm,
                            correction_factor=correction_factor)


class NDCalibrationWidget(QWidget):
    """Self-contained controls for the shared ND calibration workflow."""
    changed = Signal()
    busy_changed = Signal(bool)

    def __init__(self, stage_ctrl=None, pm_ctrl=None, parent=None):
        super().__init__(parent)
        self._stage_ctrl, self._pm_ctrl = stage_ctrl, pm_ctrl
        self._thread: Optional[QThread] = None; self._worker: Optional[NDCalibrationWorker] = None
        self._operation_busy = False; self._external_busy = False; self._last_reference = (None, None)
        self._last_actual_position = None
        self._reference_signature = self._context_signature()
        form = QFormLayout(self)
        self.positions_edit = QLineEdit("(0, 50, 30)"); self.profile_edit = QLineEdit(cfg.nd_calibration.profile_name or "default")
        self.settle_spin = QDoubleSpinBox(); self.settle_spin.setRange(0, 60); self.settle_spin.setValue(.3); self.settle_spin.setSuffix(" s")
        self.averages_spin = QSpinBox(); self.averages_spin.setRange(1, 100); self.averages_spin.setValue(3)
        self.wavelength_spin = QDoubleSpinBox(); self.wavelength_spin.setRange(200, 2000); self.wavelength_spin.setValue(float(cfg.nd_calibration.wavelength_nm or 730))
        self.reference_position_edit = QLineEdit(); self.reference_position_edit.setPlaceholderText("stage position")
        self.scan_button = QPushButton("Scan and save calibration"); self.stop_button = QPushButton("Stop"); self.stop_button.setEnabled(False); self.reference_button = QPushButton("Measure reference")
        self.status_label = QLabel(self._loaded_status())
        self.status_label.setWordWrap(True)
        self._inputs = [self.positions_edit, self.profile_edit, self.settle_spin, self.averages_spin, self.wavelength_spin, self.reference_position_edit, self.scan_button, self.reference_button]
        for label, widget in (("Positions", self.positions_edit), ("Profile", self.profile_edit), ("Settle", self.settle_spin), ("Averages", self.averages_spin), ("PM wavelength", self.wavelength_spin), ("Reference position", self.reference_position_edit)): form.addRow(label + ":", widget)
        row = QHBoxLayout(); row.addWidget(self.scan_button); row.addWidget(self.stop_button); form.addRow(row); form.addRow(self.reference_button); form.addRow("Status:", self.status_label)
        self.scan_button.clicked.connect(self.start_scan); self.reference_button.clicked.connect(self.measure_reference); self.stop_button.clicked.connect(self.stop); self.wavelength_spin.valueChanged.connect(self._on_wavelength_changed)
        for controller in (stage_ctrl, pm_ctrl):
            for name in ("connected", "disconnected", "connection_changed", "backend_changed", "settings_changed"):
                signal = getattr(controller, name, None)
                if signal is not None and hasattr(signal, "connect"):
                    try: signal.connect(self._on_device_changed)
                    except (TypeError, RuntimeError): pass
    def _loaded_status(self):
        cal = cfg.nd_calibration
        return f"Calibration loaded ({cal.profile_name})" if len(cal.positions) >= 2 else "No calibration loaded"

    def _connected(self, controller): return controller is not None and bool(getattr(controller, "is_connected", True))

    def _context_signature(self):
        try: factor = power_correction_factor()
        except Exception: factor = None
        wavelength = float(self.wavelength_spin.value()) if hasattr(self, "wavelength_spin") else None
        stage = getattr(self._stage_ctrl, "adapter", self._stage_ctrl); pm = getattr(self._pm_ctrl, "adapter", self._pm_ctrl)
        meter_wavelength = getattr(pm, "wavelength_nm", getattr(pm, "_wavelength_nm", None))
        try: meter_wavelength = None if meter_wavelength is None else float(meter_wavelength)
        except (TypeError, ValueError): meter_wavelength = None
        return (factor, wavelength, meter_wavelength, self._connected(self._stage_ctrl), self._connected(self._pm_ctrl), id(stage), id(pm))

    def _invalidate_reference(self):
        had_reference = self._last_reference != (None, None); self._last_reference = (None, None)
        if had_reference: self.changed.emit()

    def _invalidate_if_context_changed(self):
        signature = self._context_signature()
        if signature != self._reference_signature: self._invalidate_reference(); self._reference_signature = signature

    @Slot()
    def _on_device_changed(self): self._invalidate_if_context_changed()

    @Slot(float)
    def _on_wavelength_changed(self, _value): self._invalidate_if_context_changed()

    def _controller_adapters(self):
        if not self._connected(self._stage_ctrl) or not self._connected(self._pm_ctrl): raise RuntimeError("Connected stage and PM100D are required.")
        stage = getattr(self._stage_ctrl, "adapter", self._stage_ctrl); pm = getattr(self._pm_ctrl, "adapter", self._pm_ctrl)
        if stage is None or pm is None: raise RuntimeError("Connected stage and PM100D are required.")
        return stage, pm

    def _set_inputs_enabled(self, enabled):
        for widget in self._inputs: widget.setEnabled(bool(enabled))
        self.stop_button.setEnabled(bool(self._operation_busy))

    def _start(self, positions, reference_only=False):
        if self._operation_busy or self._thread is not None: raise RuntimeError("An ND calibration operation is already running.")
        if self._external_busy: raise RuntimeError("ND calibration is unavailable while the power sweep is running.")
        self._invalidate_if_context_changed(); stage, pm = self._controller_adapters(); factor = power_correction_factor()
        wavelength = float(self.wavelength_spin.value()); profile = self.profile_edit.text().strip() or "default"
        self._run_profile, self._run_wavelength, self._run_factor = profile, wavelength, factor
        self._run_cancelled = False
        self._thread = QThread(self); self._worker = NDCalibrationWorker(stage, pm, positions, settle_s=self.settle_spin.value(), samples=self.averages_spin.value(), reference_only=reference_only, correction_factor=factor, wavelength_nm=wavelength)
        worker, thread = self._worker, self._thread; worker.moveToThread(thread); thread.started.connect(worker.run); worker.progress.connect(self._on_progress); worker.error.connect(self._on_error)
        worker.reading.connect(self._on_reading)
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection); worker.finished.connect(self._handle_finished); thread.finished.connect(worker.deleteLater); thread.finished.connect(self._on_thread_finished)
        self._operation_busy = True; self._set_inputs_enabled(False); self.status_label.setText("Starting…"); self.busy_changed.emit(True); thread.start()

    @Slot()
    def start_scan(self):
        try: self._start(_parse_positions(self.positions_edit.text()))
        except Exception as exc: self._on_error(str(exc))

    @Slot()
    def measure_reference(self):
        try: self._start([float(self.reference_position_edit.text())], True)
        except Exception as exc: self._on_error(str(exc))

    @Slot()
    def stop(self):
        if self._worker is not None:
            self._run_cancelled = True
            self._worker.request_stop()
            self.status_label.setText("Stopping…")

    @Slot(int, int)
    def _on_progress(self, done, total):
        suffix = ""
        if self._last_actual_position is not None:
            suffix = f" (actual {self._last_actual_position:g})"
        self.status_label.setText(f"Scanning {done}/{total}…{suffix}")

    @Slot(float, float, float)
    def _on_reading(self, actual, _corrected, _raw):
        self._last_actual_position = float(actual)
        self.status_label.setText(f"Accepted actual position {actual:g}")

    @Slot(str)
    def _on_error(self, message): self.status_label.setText("⚠ " + str(message))

    @Slot(object)
    def _handle_finished(self, result):
        if isinstance(result, list):
            # A stop request can race with the worker's final check.  Never
            # install a curve from an operation the user cancelled.
            if getattr(self, "_run_cancelled", False) or (self._worker is not None and self._worker._stop.is_set()):
                self.status_label.setText("ND calibration stopped")
                return
            try:
                install_calibration(result, profile_name=self._run_profile, wavelength_nm=self._run_wavelength, correction_factor=self._run_factor)
                self._invalidate_reference(); self.status_label.setText(f"Saved calibration ({len(result)} points)"); self.changed.emit()
            except Exception as exc: self._on_error(str(exc))
        elif isinstance(result, tuple):
            # A stop request can race with the worker's completion signal;
            # never install a reference from an operation the user cancelled.
            if getattr(self, "_run_cancelled", False) or (self._worker is not None and self._worker._stop.is_set()):
                self.status_label.setText("ND calibration stopped")
                return
            position, power = result
            if not (math.isfinite(float(position)) and math.isfinite(float(power)) and float(power) > 0): self._on_error("Invalid reference reading")
            else:
                self._last_reference = (float(position), float(power)); self._reference_signature = self._context_signature(); self.status_label.setText(f"Reference: {power:.4g} µW at actual {position:g}"); self.changed.emit()
        elif getattr(self, "_run_cancelled", False) or (self._worker is not None and self._worker._stop.is_set()): self.status_label.setText("ND calibration stopped")

    @Slot()
    def _on_thread_finished(self):
        if self.sender() is not self._thread: return
        self._thread = None; self._worker = None; self._operation_busy = False; self._set_inputs_enabled(not self._external_busy); self.busy_changed.emit(False)

    def reference(self): self._invalidate_if_context_changed(); return self._last_reference

    @Slot(bool)
    def set_external_busy(self, busy):
        self._external_busy = bool(busy)
        if not self._operation_busy: self._set_inputs_enabled(not self._external_busy)

    def shutdown(self, timeout_ms=30000):
        self.stop(); thread = self._thread
        if thread is None: return True
        ok = thread.wait(int(timeout_ms))
        if ok and self._thread is thread:
            self._thread = None; self._worker = None; self._operation_busy = False; self._set_inputs_enabled(not self._external_busy); self.busy_changed.emit(False)
        return bool(ok)


def _parse_positions(text):
    value = ast.literal_eval(text.strip())
    if isinstance(value, tuple) and len(value) == 3:
        start, stop, count = float(value[0]), float(value[1]), int(value[2])
        if count < 1: raise ValueError("Position count must be positive.")
        return np.linspace(start, stop, count)
    return np.asarray(value if isinstance(value, (list, tuple)) else [value], dtype=float)
