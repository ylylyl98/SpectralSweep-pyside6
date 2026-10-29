"""Qt Quick history embedded in the existing Widgets shell."""
from pathlib import Path

from PySide6.QtCore import QUrl, Signal, Slot
from PySide6.QtQuickWidgets import QQuickWidget
from ui.quick_engine import QuickSurface
from app.history_viewmodel import HistoryViewModel


class QuickHistoryWidget(QuickSurface):
    settingsLoaded = Signal(dict)
    settingsContextLoaded = Signal(str, dict)
    metadataOpenRequested = Signal(str)
    qmlWarning = Signal(str)

    def __init__(self, history, output_root, sample_id, supported_keys=None, parent=None, *, page_size=50):
        super().__init__(parent)
        self.setObjectName("QuickHistoryWidget")
        self.setAccessibleName("Experiment run history")
        self.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self.view_model = HistoryViewModel(history, output_root, sample_id, supported_keys,
                                           self, page_size=page_size)
        self.view_model.settingsLoaded.connect(self.settingsLoaded)
        self.view_model.settingsContextLoaded.connect(self.settingsContextLoaded)
        self.view_model.metadataOpenRequested.connect(self.metadataOpenRequested)
        self.qml_warnings = []
        self.engine().warnings.connect(self._warnings)
        self.setInitialProperties({"historyModel": self.view_model})
        self.setSource(QUrl.fromLocalFile(str(Path(__file__).resolve().parent / "qml" / "HistoryPage.qml")))

    def _warnings(self, errors):
        for error in errors:
            message = error.toString()
            self.qml_warnings.append(message)
            self.qmlWarning.emit(message)

    @Slot(str)
    def setSampleId(self, value):
        self.view_model.setSampleId(value)

    @Slot(str)
    def setExperimentType(self, value):
        self.view_model.setExperimentType(value)

    @Slot()
    def refresh(self):
        self.view_model.refresh()

    def set_theme(self, mode):
        """Allow the shared shell/preview to choose system, light or dark."""
        if mode not in {"system", "light", "dark"}:
            raise ValueError("Theme must be system, light or dark")
        root = self.rootObject()
        if root is not None:
            root.setProperty("themeMode", mode)

    @Slot()
    def shutdown(self):
        self.view_model.shutdown()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
