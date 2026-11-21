"""Paired manual Spectrum capture and its per-detector readouts."""
from datetime import datetime, timezone
from itertools import count
from threading import Event

import numpy as np
from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QLabel, QSizePolicy, QSplitter, QStackedWidget, QVBoxLayout, QWidget

from app.paired_spectrum_files import PairedSpectrumFiles, PairedSpectrumSaveError
from ui.plot_factory import create_spectrum_plot
from ui.spectrum_plot import ElidedSpectrumLabel, LIVE_SPECTRUM_COLOR
from ui.station_pair_controls import PAIR_LABELS

ANDOR_OVERLAY_COLOR = "#C45100"


class SpectrumPairCenterApply(QThread):
    def __init__(self, controller, backend, centers, parent):
        super().__init__(parent)
        self.controller = controller
        self.backend = backend
        self.centers = centers
        self.stop = Event()
        self.readbacks = {}
        self.error = ""

    def run(self):
        try:
            self.readbacks = self.controller.apply_station_pair_centers(self.backend, self.centers, self.stop)
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__


class SpectrumPairCapture(QThread):
    point_ready = Signal(object)

    def __init__(self, controller, backend, recipes, directory, parent, *,
                 stem="spectrum_pair", owner=None, repeats=1, continuous=False):
        super().__init__(parent)
        self._controller = controller
        self.backend = backend
        self._recipes = recipes
        self._directory = directory
        self._stem = stem
        self._owner = owner
        self._repeats = repeats
        self.continuous = continuous
        self.stop = Event()
        self.records = {}
        self.paths = {}
        self.point_id = None
        self.complete_points = 0
        self.manifest_path = None
        self.capture_error = ""
        self.save_error = ""
        self.repeat_error = ""

    def _capture(self):
        acquire = self._controller.acquire_station_pair
        if self._owner is None:
            return acquire(self.backend, self._recipes, self.stop)
        return acquire(self.backend, self._recipes, self.stop, owner=self._owner)

    def _average_repeats(self):
        results = {}
        counts_sum = {}
        repeats_done = dict.fromkeys(self._recipes, 0)
        problems = []
        for repeat in range(1, self._repeats + 1):
            if self.stop.is_set():
                problems.append("Stopped before all repeats completed")
                break
            try:
                capture = self._capture()
            except Exception as exc:
                problems.append(f"Repeat {repeat}: {exc}")
                break
            for backend in self._recipes:
                record = capture.get(backend, {"error": "No capture result received"})
                if record.get("error"):
                    problems.append(f"{PAIR_LABELS[backend]} repeat {repeat}: {record['error']}")
                    continue
                first = results.get(backend)
                wavelengths = np.asarray(record["wavelengths"], dtype=float)
                counts = np.asarray(record["counts"], dtype=float)
                if wavelengths.ndim != 1 or not wavelengths.size or counts.shape != wavelengths.shape:
                    problems.append(f"{PAIR_LABELS[backend]} repeat {repeat}: invalid 1D spectrum")
                    continue
                if first is None:
                    results[backend] = dict(record)
                    counts_sum[backend] = counts.copy()
                elif (first.get("axis_unit", "nm") != record.get("axis_unit", "nm")
                      or np.asarray(first["wavelengths"]).shape != wavelengths.shape
                      or not np.allclose(first["wavelengths"], wavelengths, atol=0.00005, rtol=0)):
                    problems.append(f"{PAIR_LABELS[backend]} repeat {repeat}: spectrum axis changed")
                    continue
                else:
                    counts_sum[backend] += counts
                    results[backend]["finished_at"] = record.get("finished_at")
                repeats_done[backend] += 1
            if problems:
                break
        self.repeat_error = "\n".join(problems)
        for backend, recipe in self._recipes.items():
            record = results.setdefault(backend, {"error": self.repeat_error or "No spectrum acquired."})
            count = repeats_done[backend]
            if count:
                record["counts"] = counts_sum[backend] / count
            record["settings"] = {
                **recipe, **(record.get("settings") or {}),
                "repeats_requested": self._repeats, "repeats_averaged": count,
                **({"repeat_issue": self.repeat_error} if self.repeat_error else {}),
            }
        return results

    def run(self):
        try:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            files = PairedSpectrumFiles(self._directory, f"{self._stem}_{timestamp}", self.backend)
            self.manifest_path = files.manifest_path
        except Exception as exc:
            self.save_error = str(exc)
            return
        for point in count(1) if self.continuous else (1,):
            if self.continuous and self.stop.is_set():
                break
            self._capture_point(files, point)
            if not self.continuous:
                break
            self.point_ready.emit((point, self.records, self.paths))
            if self.save_error or self.capture_error or len(self.paths) != 2:
                break

    def _capture_point(self, files, point):
        self.point_id = point
        self.paths = {}
        try:
            self.records = self._capture() if self._repeats == 1 else self._average_repeats()
        except Exception as exc:
            self.capture_error = str(exc)
            self.records = {key: {"error": self.capture_error, "settings": recipe}
                            for key, recipe in self._recipes.items()}
        try:
            self.paths = files.write_point(point, self.records, incomplete_reason=self.repeat_error)
            if len(self.paths) == 2 and not self.repeat_error:
                self.complete_points += 1
        except PairedSpectrumSaveError as exc:
            self.paths = exc.paths
            self.save_error = str(exc)
        except Exception as exc:
            self.save_error = str(exc)


class SpectrumPairDisplay(QWidget):
    layout_changed = Signal()

    def __init__(self, parent=None, *, orientation=Qt.Orientation.Vertical, selectable_layout=False):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._splitter = splitter = QSplitter(orientation)
        splitter.setChildrenCollapsible(False)
        self._entries = []
        self._side_by_side = orientation == Qt.Orientation.Horizontal
        for title in ("PIXIS", "Andor"):
            pane = QWidget() if self._side_by_side else QGroupBox(title)
            body = QVBoxLayout(pane)
            if self._side_by_side:
                body.setContentsMargins(0, 0, 0, 0)
                body.setSpacing(3)
                heading = QLabel(title)
                font = heading.font()
                font.setBold(True)
                heading.setFont(font)
                status = ElidedSpectrumLabel()
                header = QHBoxLayout()
                header.addWidget(heading)
                header.addWidget(status, 1)
                body.addLayout(header)
            else:
                heading = pane
                status = QLabel()
                status.setWordWrap(True)
                status.setTextFormat(Qt.TextFormat.PlainText)
                body.addWidget(status)
            plot = create_spectrum_plot(paired_pane=self._side_by_side)
            body.addWidget(plot, 1)
            splitter.addWidget(pane)
            self._entries.append((heading, status, plot))
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        self._views = QStackedWidget()
        self._views.addWidget(splitter)
        self.overlay_plot = None
        self._last_result = None
        self.layout_choice = None
        if selectable_layout:
            tools = QHBoxLayout()
            label = QLabel("Plot layout:")
            self.layout_choice = QComboBox()
            self.layout_choice.setObjectName("SpectrumPairPlotLayout")
            self.layout_choice.setAccessibleName("Plot layout")
            label.setBuddy(self.layout_choice)
            self.layout_choice.addItems(["Side by side", "Overlay"])
            tools.addWidget(label)
            tools.addWidget(self.layout_choice)
            self._layout_note = QLabel()
            self._layout_note.setWordWrap(True)
            tools.addWidget(self._layout_note, 1)
            self.fold_gaps = QCheckBox("Hide wavelength gaps")
            self.fold_gaps.setChecked(True)
            self.fold_gaps.setToolTip("Fold wide unsampled intervals and label their wavelength limits.")
            self.fold_gaps.hide()
            self.fold_gaps.toggled.connect(self._set_fold_gaps)
            tools.addWidget(self.fold_gaps)
            layout.addLayout(tools)
            self.layout_choice.currentIndexChanged.connect(self._update_layout)
        layout.addWidget(self._views, 1)
        self._manifest = QLabel()
        self._manifest.setWordWrap(True)
        self._manifest.setTextFormat(Qt.TextFormat.PlainText)
        self._manifest.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if self._side_by_side:
            self._manifest.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._manifest)

    def plots(self, andor_backend):
        return (("lightfield", self._entries[0][2]), (andor_backend, self._entries[1][2]))

    @property
    def showing_overlay(self):
        return self.overlay_plot is not None and self._views.currentWidget() is self.overlay_plot

    def _set_fold_gaps(self, enabled):
        if self.overlay_plot is not None:
            self.overlay_plot.set_fold_overlay_gaps(enabled)

    def _update_layout(self):
        if self.layout_choice is None:
            return
        backend, records = self._last_result or ("andor_ingaas", {})
        units = {r.get("axis_unit", "nm") for r in records.values() if r.get("wavelengths") is not None}
        overlay = self.layout_choice.currentIndex() == 1
        self.fold_gaps.setVisible(overlay and (not units or units == {"nm"}))
        changed = self.showing_overlay != (overlay and len(units) <= 1)
        if overlay and len(units) <= 1:
            if self.overlay_plot is None:
                self.overlay_plot = create_spectrum_plot(self)
                self._views.addWidget(self.overlay_plot)
                self.overlay_plot._peak_lbl.hide()
                self.overlay_plot._range_lbl.hide()
                self._set_fold_gaps(self.fold_gaps.isChecked())
            plot = self.overlay_plot
            self._views.setCurrentWidget(plot)
            plot.set_axis_unit(next(iter(units), plot.axis_unit))
            primary = records.get("lightfield", {})
            secondary = records.get(backend, {})
            plot.update_spectrum(primary.get("wavelengths", []), primary.get("counts", []))
            plot.update_secondary_spectrum(secondary.get("wavelengths"), secondary.get("counts"), ANDOR_OVERLAY_COLOR)
            missing = " (no spectrum)" if primary.get("error") else ""
            missing_andor = " (no spectrum)" if secondary.get("error") else ""
            self._layout_note.setText(
                f'<font color="{LIVE_SPECTRUM_COLOR}">━━</font> PIXIS{missing} &nbsp; '
                f'<font color="{ANDOR_OVERLAY_COLOR}">━━</font> {PAIR_LABELS[backend]}{missing_andor}')
            if not units:
                plot._empty_label.setText("No spectra received." if records else "Waiting for paired spectra.")
        else:
            self._views.setCurrentWidget(self._splitter)
            self._layout_note.setText("Showing separate plots because one axis is in pixels and the other in nm."
                                      if overlay else "")
        if changed:
            self.layout_changed.emit()

    def show_result(self, backend, records, paths, manifest_path, *, waiting=False):
        self._last_result = backend, records
        for key, (heading, status, plot) in zip(("lightfield", backend), self._entries):
            if self._side_by_side:
                heading.setText(PAIR_LABELS[key])
            else:
                heading.setTitle(PAIR_LABELS[key])
            record = records.get(key, {})
            wavelengths = record.get("wavelengths")
            counts = record.get("counts")
            plot.set_axis_unit(record.get("axis_unit", "nm"))
            if wavelengths is not None and counts is not None:
                plot.update_spectrum(wavelengths, counts)
                status.setText(f"Saved to {paths[key].name}" if key in paths else "Acquired. File not saved.")
                status.show()
            else:
                plot.update_spectrum(np.empty(0), np.empty(0))
                plot._empty_label.setText(
                    "Acquiring…" if waiting else str(record.get("error") or "No spectrum acquired."))
                if self._side_by_side:
                    status.setText("")
                else:
                    status.hide()
        self._manifest.setText(f"Pair manifest: {manifest_path}" if manifest_path else "")
        self._manifest.setVisible(bool(manifest_path))
        self._update_layout()
