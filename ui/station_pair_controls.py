"""Shared detector selection and per-device recipes for Station 2100."""
from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QBoxLayout, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from utils.config import cfg
from ui.signal_connections import SignalConnections


PAIR_LABELS = {
    "lightfield": "PIXIS",
    "andor_si": "Andor Silicon",
    "andor_ingaas": "Andor InGaAs",
}


class StationPairControls(QWidget):
    backend_changed = Signal(str)

    def __init__(self, parent=None, *, side_by_side=True, compact=False, responsive_recipes=False):
        super().__init__(parent)
        self._station = "default"
        self._controller = None
        self._connections = SignalConnections(self)
        self.setObjectName("StationPairControls")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._compact = compact
        self._responsive_recipe_row = None
        selection = QFormLayout()
        self.mode = QComboBox()
        self.mode.setObjectName("StationPairMode")
        self.mode.addItem("Single detector", False)
        self.mode.addItem("Dual detectors", True)
        self.mode.setAccessibleName("Detection mode")
        selection.addRow("Mode" if compact else "Detection mode:", self.mode)
        self._active_detector = QLabel()
        self.set_active_backend("")

        self._details = QWidget()
        self._details.setProperty("quickSectionBoundary", True)
        details = QVBoxLayout(self._details)
        details.setContentsMargins(0, 0, 0, 0)
        detector_row = QFormLayout()
        self.andor_detector = QComboBox()
        self.andor_detector.setObjectName("StationPairAndorDetector")
        for backend in ("andor_ingaas", "andor_si"):
            self.andor_detector.addItem(PAIR_LABELS[backend], backend)
        self.andor_detector.setAccessibleName("Andor detector")
        if compact:
            detector_row.addRow(self.andor_detector)
        else:
            detector_row.addRow("Andor detector:", self.andor_detector)
        self._detector_row = detector_row
        if compact:
            self.mode.setProperty("quickPreferredWidth", 140)
            self.andor_detector.setProperty("quickPreferredWidth", 144)
            self._selectors = selectors = QHBoxLayout()
            selectors.addLayout(selection)
            selectors.addLayout(detector_row)
            self._active_detector.hide()
            self._active_detector.setParent(self)
            selectors.addStretch(1)
            layout.addLayout(selectors)
        elif responsive_recipes:
            self._selectors = QHBoxLayout()
            self._selectors.setSpacing(6)
            for form in (selection, detector_row):
                column = QWidget()
                column.setProperty("quickSectionBoundary", True)
                column.setLayout(form)
                form.setContentsMargins(0, 0, 0, 0)
                self._selectors.addWidget(column, 1)
            selection.addRow(self._active_detector)
            self._detector_selector = detector_row.parentWidget()
            self.mode.setProperty("quickPreferredWidth", 146)
            self.andor_detector.setProperty("quickPreferredWidth", 146)
            layout.setSpacing(4)
            layout.addLayout(self._selectors)
        else:
            selection.addRow(self._active_detector)
            layout.addLayout(selection)
            details.addLayout(detector_row)
        self._pixis_editors = self._recipe_editors("PIXIS", "Pixis")
        self._andor_editors = self._recipe_editors("Andor", "Andor")
        if not compact:
            recipe_row = QHBoxLayout() if side_by_side else QVBoxLayout()
            recipe_row.setProperty("quickSectionColumns", True)
            self._pixis = self._recipe("PIXIS", "Pixis", self._pixis_editors)
            self._andor = self._recipe("Andor", "Andor", self._andor_editors)
            recipe_row.addWidget(self._pixis, 1)
            recipe_row.addWidget(self._andor, 1)
            details.addLayout(recipe_row)
            if responsive_recipes:
                self._responsive_recipe_row = recipe_row
                recipe_row.setSpacing(self._selectors.spacing())
                labels = [selection.labelForField(self.mode), detector_row.labelForField(self.andor_detector)]
                recipe_editor_width = 122
                for group, editors in ((self._pixis, self._pixis_editors), (self._andor, self._andor_editors)):
                    labels.extend(group.layout().labelForField(editor) for editor in editors)
                    for editor in editors:
                        editor.setProperty("quickPreferredWidth", recipe_editor_width)
                    group.setProperty("detectorRecipeCard", True)
                    group.layout().setProperty("quickRowBoundary", True)
                    group.layout().setContentsMargins(5, 2, 5, 2)
                    group.layout().setVerticalSpacing(3)
                label_width = max(label.sizeHint().width() for label in labels) + 4
                for label in labels:
                    label.setMinimumWidth(label_width)
                    label.setProperty("quickPreferredWidth", label_width)
                # Reserve the label/editor gap and both padded card edges.
                self._recipe_column_width = label_width + recipe_editor_width + 26
        self._status = QLabel()
        if responsive_recipes:
            self._status.hide()
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        details.addWidget(self._status)
        layout.addWidget(self._details)
        if compact:
            detector_row.setRowVisible(self.andor_detector, False)
        self._details.hide()
        self.hide()
        self.mode.currentIndexChanged.connect(self._selection_changed)
        self.andor_detector.currentIndexChanged.connect(self._selection_changed)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        row = self._responsive_recipe_row
        if row is not None:
            fits = self.contentsRect().width() >= 2 * self._recipe_column_width + row.spacing()
            direction = QBoxLayout.Direction.LeftToRight if fits else QBoxLayout.Direction.TopToBottom
            row.setDirection(direction)
            self._selectors.setDirection(direction)

    def add_selector_action(self, widget):
        self._selectors.insertWidget(self._selectors.count() - 1, widget)

    @staticmethod
    def _recipe_editors(title, name):
        center = QDoubleSpinBox()
        center.setObjectName(f"StationPair{name}Center")
        center.setDecimals(1)
        center.setRange(0, 2000)
        center.setSuffix(" nm")
        center.setValue(cfg.lf6.center_nm)
        exposure = QDoubleSpinBox()
        exposure.setObjectName(f"StationPair{name}Exposure")
        exposure.setDecimals(1)
        exposure.setRange(1, 600_000)
        exposure.setSuffix(" ms")
        exposure.setValue(cfg.lf6.exposure_ms)
        frames = QSpinBox()
        frames.setObjectName(f"StationPair{name}Frames")
        frames.setRange(1, 1000)
        frames.setValue(cfg.lf6.accumulations)
        center.setAccessibleName(f"{title} spectrograph center wavelength")
        exposure.setAccessibleName(f"{title} exposure per frame")
        frames.setAccessibleName(f"{title} frames to average" if name == "Andor" else "PIXIS frames to combine")
        return center, exposure, frames

    @staticmethod
    def _recipe(title, name, editors):
        group = QGroupBox(title)
        group.setObjectName(f"StationPair{name}Recipe")
        form = QFormLayout(group)
        for label, editor in zip(("Center:", "Exposure / frame:",
                                  "Frames:" if name == "Pixis" else "Frames to average:"), editors):
            form.addRow(label, editor)
        return group

    @property
    def andor_backend(self):
        return (str(self.andor_detector.currentData())
                if self._station == "attodry2100" and self.mode.currentData() else "")

    def set_station(self, station_id):
        if station_id == self._station:
            return
        self._station = station_id
        if station_id != "attodry2100":
            with QSignalBlocker(self.mode), QSignalBlocker(self.andor_detector):
                self.mode.setCurrentIndex(0)
                self.andor_detector.setCurrentIndex(0)
        self.setVisible(station_id == "attodry2100")
        self._selection_changed()

    def set_editing_enabled(self, enabled):
        for widget in (self.mode, self.andor_detector, *self._pixis_editors, *self._andor_editors):
            widget.setEnabled(enabled)

    def bind_controller(self, controller):
        self.unbind_controller()
        self._controller = controller
        self.set_station(getattr(cfg.lf6, "station_id", "default"))
        connected = bool(getattr(controller, "is_connected", False))
        self.set_active_backend(getattr(controller, "backend", "") if connected else "")
        for name, slot in (("station_changed", self.set_station),
                           ("connected", self._on_controller_connected),
                           ("disconnected", self._on_controller_disconnected)):
            signal = getattr(controller, name, None)
            if signal is not None:
                self._connections.connect(signal, slot)

    def unbind_controller(self):
        self._connections.clear()
        self._controller = None

    def _on_controller_connected(self, _experiments):
        self.set_active_backend(getattr(self._controller, "backend", ""))

    def _on_controller_disconnected(self):
        self.set_active_backend("")

    def set_active_backend(self, backend):
        label = PAIR_LABELS.get(backend, "WinSpec InGaAs" if backend == "winspec_ingaas" else "Disconnected")
        self._active_detector.setText(f"Current detector in Instruments: {label}")

    def set_pair_status(self, text):
        self._status.setText(text)
        self._status.setVisible(bool(text))
        if self._compact:
            self._details.setVisible(bool(self.andor_backend and text))

    def _selection_changed(self):
        backend = self.andor_backend
        if not self._compact:
            self._andor.setTitle(PAIR_LABELS.get(backend, "Andor"))
        self._details.setVisible(bool(backend and (not self._compact or self._status.text())))
        self._active_detector.setVisible(not backend and not self._compact)
        if self._compact:
            self._detector_row.setRowVisible(self.andor_detector, bool(backend))
        elif self._responsive_recipe_row is not None:
            self._detector_selector.setVisible(bool(backend))
        self.backend_changed.emit(backend)

    @staticmethod
    def _values(editors):
        return dict(zip(("center_nm", "exposure_ms", "frames"), (editor.value() for editor in editors)))

    def recipes(self):
        return {
            "lightfield": self._values(self._pixis_editors),
            self.andor_backend: self._values(self._andor_editors),
        } if self.andor_backend else {}

    def request_state(self):
        return {"backend": self.andor_backend, "andor_detector": self.andor_detector.currentData(),
                "pixis": self._values(self._pixis_editors),
                "andor": self._values(self._andor_editors)}

    def restore_measurement_mode(self, recipes):
        """Apply an explicitly loaded measurement's detector choices to this station."""
        if not recipes:
            self.mode.setCurrentIndex(0)
            return
        from app.paired_spectra import ANDOR_BACKENDS, PAIRED_STATION_ID, validate_pair_recipes
        if self._station != PAIRED_STATION_ID:
            raise ValueError("Dual-detector recipes require the attoDRY2100 split optical path.")
        if not isinstance(recipes, dict):
            raise ValueError("Invalid dual-detector recipe.")
        detectors = set(recipes).intersection(ANDOR_BACKENDS)
        if len(detectors) != 1 or set(recipes) != {"lightfield", *detectors}:
            raise ValueError("A dual-detector recipe requires PIXIS and one Andor detector.")
        backend = detectors.pop()
        values = validate_pair_recipes(backend, recipes)
        for editors, settings in ((self._pixis_editors, values["lightfield"]), (self._andor_editors, values[backend])):
            for key, editor in zip(("center_nm", "exposure_ms", "frames"), editors):
                if not editor.minimum() <= settings[key] <= editor.maximum():
                    raise ValueError(f"The saved {key} is outside the supported range.")
        self.restore_state({"backend": backend, "andor_detector": backend,
                            "pixis": values["lightfield"], "andor": values[backend]})

    def restore_state(self, state):
        for key, editors in (("pixis", self._pixis_editors), ("andor", self._andor_editors)):
            values = state.get(key, {})
            for field, editor in zip(("center_nm", "exposure_ms", "frames"), editors):
                if field in values:
                    editor.setValue(values[field])
        if self._station == "attodry2100":
            backend = state.get("backend", "")
            index = self.andor_detector.findData(state.get("andor_detector") or backend)
            with QSignalBlocker(self.mode), QSignalBlocker(self.andor_detector):
                self.andor_detector.setCurrentIndex(max(0, index))
                self.mode.setCurrentIndex(int(backend in ("andor_si", "andor_ingaas")))
            self._selection_changed()
