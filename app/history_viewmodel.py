"""Presentation state for history views; never imports or commands instruments."""
from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, Property, Qt, QTimer, Signal, Slot

from ui.history_loader import HistoryTaskRunner, load_preview, load_settings


class HistoryRows(QAbstractListModel):
    _names = ("runId", "startTime", "statusText", "workflow", "metadataPath")
    _keys = ("experiment_id", "started_utc", "status", "experiment_type", "metadata_path")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.records = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.records)

    def roleNames(self):
        return {Qt.ItemDataRole.UserRole + i + 1: name.encode() for i, name in enumerate(self._names)}

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.records):
            return None
        offset = role - Qt.ItemDataRole.UserRole - 1
        if 0 <= offset < len(self._keys):
            return str(self.records[index.row()].get(self._keys[offset]) or "")
        return None

    def replace(self, records):
        self.beginResetModel()
        self.records = [dict(row) for row in records]
        self.endResetModel()


class HistoryViewModel(QObject):
    changed = Signal()
    settingsLoaded = Signal(dict)
    settingsContextLoaded = Signal(str, dict)
    metadataOpenRequested = Signal(str)
    WORKFLOWS = ["dual_gate_sweep", "gate_map_2d", "motion_sweep", "mcd_aps100",
                 "mcd_attodry2100", "bfp_acquisition", "bfp_binned_rc", "bfp_full_sensor_rc",
                 "spectrum_preview"]
    WORKFLOW_LABELS = ["Dual Gate", "2D Sweep", "Motion Sweep", "MCD 1000", "MCD 2100",
                       "BFP", "Binned RC", "Full-sensor RC", "Spectrum"]

    def __init__(self, history, output_root, sample_id, supported_keys=None, parent=None, *, page_size=50):
        super().__init__(parent)
        self.history, self.output_root = history, Path(output_root)
        self.supported_keys = supported_keys
        self.rows_model = HistoryRows(self)
        self.runner = HistoryTaskRunner(self)
        self._sample_id, self._query, self._event_query = str(sample_id), "", ""
        self._experiment_type = self.WORKFLOWS[0]
        self._page, self._page_size, self._total = 0, max(1, int(page_size)), 0
        self._selected, self._event_page = -1, 0
        self._preview, self._metadata_path, self._error = "", "", ""
        self._loading = self._preview_loading = self._settings_loading = self._event_has_next = False
        self._closed = False
        self._query_timer = QTimer(self)
        self._query_timer.setSingleShot(True)
        self._query_timer.setInterval(180)
        self._query_timer.timeout.connect(self.refresh)
        self._event_timer = QTimer(self)
        self._event_timer.setSingleShot(True)
        self._event_timer.setInterval(180)
        self._event_timer.timeout.connect(self._load_preview)
        QTimer.singleShot(0, self.refresh)

    rows = Property(QObject, lambda self: self.rows_model, constant=True)
    workflows = Property('QStringList', lambda self: self.WORKFLOWS, constant=True)
    workflowOptions = Property('QVariantList', lambda self: [
        {"label": label, "key": key} for label, key in zip(self.WORKFLOW_LABELS, self.WORKFLOWS)], constant=True)
    sampleId = Property(str, lambda self: self._sample_id, notify=changed)
    query = Property(str, lambda self: self._query, notify=changed)
    eventQuery = Property(str, lambda self: self._event_query, notify=changed)
    experimentType = Property(str, lambda self: self._experiment_type, notify=changed)
    page = Property(int, lambda self: self._page, notify=changed)
    total = Property(int, lambda self: self._total, notify=changed)
    pageCount = Property(int, lambda self: max(1, (self._total + self._page_size - 1) // self._page_size), notify=changed)
    selectedIndex = Property(int, lambda self: self._selected, notify=changed)
    previewText = Property(str, lambda self: self._preview, notify=changed)
    errorText = Property(str, lambda self: self._error, notify=changed)
    loading = Property(bool, lambda self: self._loading, notify=changed)
    previewLoading = Property(bool, lambda self: self._preview_loading, notify=changed)
    settingsLoading = Property(bool, lambda self: self._settings_loading, notify=changed)
    canOpen = Property(bool, lambda self: bool(self._metadata_path) and not self._preview_loading, notify=changed)
    canLoad = Property(bool, lambda self: bool(self._metadata_path) and not self._preview_loading and not self._settings_loading, notify=changed)
    eventPage = Property(int, lambda self: self._event_page, notify=changed)
    eventHasNext = Property(bool, lambda self: self._event_has_next and not self._preview_loading, notify=changed)

    def _clear_selection(self):
        self.runner.cancel("preview")
        self.runner.cancel("settings")
        self._event_timer.stop()
        self._selected, self._event_page = -1, 0
        self._preview, self._metadata_path = "", ""
        self._preview_loading = self._settings_loading = self._event_has_next = False

    def _invalidate_list(self):
        self.runner.cancel_all()
        self._clear_selection()
        self.rows_model.replace([])
        self._page, self._total = 0, 0
        self._error = ""
        self._loading = True
        self.changed.emit()

    @Slot(str)
    def setSampleId(self, value):
        value = str(value).strip()
        if value != self._sample_id:
            self._sample_id = value
            self._invalidate_list()
            self.refresh()

    @Slot(str)
    def setExperimentType(self, value):
        if value in self.WORKFLOWS and value != self._experiment_type:
            self._experiment_type = value
            self._invalidate_list()
            self.refresh()

    @Slot(str)
    def setQuery(self, value):
        if value != self._query:
            self._query = str(value)
            self._invalidate_list()
            self._query_timer.start()

    @Slot()
    def refresh(self):
        if self._closed:
            return
        self._query_timer.stop()
        self.runner.cancel_all()
        self._clear_selection()
        self.rows_model.replace([])
        self._loading, self._error = True, ""
        sample, kind, page, query = self._sample_id, self._experiment_type, self._page, self._query
        self.changed.emit()
        if not sample:
            self._list_ready({"rows": [], "total": 0, "page": 0}, None)
            return
        self.runner.submit("list", lambda cancel: self.history.query_page(
            sample, kind, page=page, page_size=self._page_size, filter_text=query,
            cancel_event=cancel), self._list_ready)

    def _list_ready(self, result, error):
        self._loading = False
        if error or not isinstance(result, dict):
            self._error = f"History unavailable: {error or 'No result returned'}"
            self._total, self._page = 0, 0
        else:
            self.rows_model.replace(result["rows"])
            self._total, self._page = int(result["total"]), int(result["page"])
        self.changed.emit()

    @Slot(int)
    def changePage(self, delta):
        target = self._page + delta
        if not self._loading and 0 <= target < self.pageCount:
            self._page = target
            self.refresh()

    @Slot(int)
    def selectRow(self, index):
        self._clear_selection()
        self._error = ""
        if 0 <= index < len(self.rows_model.records):
            self._selected = index
            self._load_preview()
        else:
            self.changed.emit()

    @Slot(str)
    def setEventQuery(self, value):
        if value != self._event_query:
            self._event_query = str(value)
            self.runner.cancel("preview")
            self.runner.cancel("settings")
            self._event_page, self._metadata_path, self._preview = 0, "", ""
            self._event_has_next = self._settings_loading = False
            self._preview_loading = self._selected >= 0
            self.changed.emit()
            self._event_timer.start()

    @Slot(int)
    def changeEventPage(self, delta):
        if self._preview_loading or (delta > 0 and not self._event_has_next):
            return
        target = self._event_page + delta
        if target >= 0:
            self._event_page = target
            self._load_preview()

    @Slot()
    def _load_preview(self):
        if self._closed or self._selected < 0:
            return
        self._event_timer.stop()
        self.runner.cancel("settings")
        self._settings_loading = False
        self._metadata_path, self._preview, self._error = "", "", ""
        self._preview_loading = True
        path = self.rows_model.records[self._selected]["metadata_path"]
        query, offset = self._event_query.lower(), self._event_page * 20
        self.changed.emit()
        self.runner.submit("preview", lambda cancel: load_preview(
            path, self.output_root, query, offset, cancel), self._preview_ready)

    def _preview_ready(self, result, error):
        self._preview_loading = False
        if error or not isinstance(result, dict):
            self._error = f"Could not read metadata: {error or 'No result returned'}"
            self._event_has_next = False
        else:
            self._metadata_path, self._preview = result["path"], result["text"]
            self._event_has_next = bool(result["has_next"])
        self.changed.emit()

    @Slot()
    def requestSettings(self):
        if not self.canLoad:
            return
        self._settings_loading, self._error = True, ""
        path, kind = self._metadata_path, self._experiment_type
        try:
            keys = self.supported_keys(kind) if callable(self.supported_keys) else self.supported_keys
        except Exception as exc:
            self._settings_ready(kind, None, str(exc))
            return
        self.changed.emit()
        self.runner.submit("settings", lambda cancel: load_settings(
            path, self.output_root, cancel, supported_keys=keys),
            lambda result, error: self._settings_ready(kind, result, error))

    def _settings_ready(self, kind, result, error):
        self._settings_loading = False
        if error or not isinstance(result, dict):
            self._error = f"Could not load settings: {error or 'No result returned'}"
        else:
            self.settingsLoaded.emit(result)
            self.settingsContextLoaded.emit(kind, result)
        self.changed.emit()

    @Slot()
    def openMetadata(self):
        if self.canOpen:
            self.metadataOpenRequested.emit(self._metadata_path)

    @Slot()
    def shutdown(self):
        self._closed = True
        self._query_timer.stop()
        self._event_timer.stop()
        self.runner.cancel_all()
        self._loading = self._preview_loading = self._settings_loading = False
        self.changed.emit()
