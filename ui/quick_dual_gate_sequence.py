"""Bounded Dual Gate sequence preview; owns presentation only, no devices."""
from __future__ import annotations

import copy
import math
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Signal, Slot, Qt
from PySide6.QtQuickWidgets import QQuickWidget
from ui.quick_engine import QuickSurface
from PySide6.QtWidgets import QDialog, QVBoxLayout, QWidget

from app.dual_gate_sequence_model import DualGateSequenceModel


class QuickDualGateSequencePreview(QWidget):
    """RunPlanTree-compatible host with separate compact/full view state.

    Hidden previews retain only the latest plan reference. Opening the full
    view creates a second bounded model; closing it releases the entire view.
    """

    preferredHeightChanged = Signal(int)

    def __init__(self, parent=None, *, page_size=8, compact=True):
        super().__init__(parent)
        self.setObjectName("QuickDualGateSequencePreview")
        self._last_plan = None
        self._closed = False
        self._pending_view_state = None
        self._full_sequence_dialog = None
        self._full_sequence_preview = None
        self._compact = compact
        self._page_size = page_size
        self._theme_mode = self._application_theme("system")
        self.qml_warnings = []
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(50)
        self._update_timer.timeout.connect(self._apply_latest_plan)
        self.quick_widget = QuickSurface(self)
        self.quick_widget.setObjectName("QuickDualGateSequenceSurface")
        self.quick_widget.setAccessibleName("Dual Gate measurement sequence")
        self.quick_widget.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self.sequence_model = DualGateSequenceModel(self.quick_widget, page_size=page_size)
        self.quick_widget.engine().warnings.connect(self._warnings)
        self.quick_widget.setInitialProperties({"sequenceModel": self.sequence_model,
                                                 "bridge": self, "compact": compact})
        path = Path(__file__).resolve().parent / "qml" / "DualGateSequence.qml"
        self.quick_widget.setSource(QUrl.fromLocalFile(str(path)))
        if self.quick_widget.status() == QQuickWidget.Status.Error:
            raise RuntimeError("\n".join(error.toString() for error in self.quick_widget.errors()))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.quick_widget)
        self.setMinimumSize(280, 0 if compact else 300)
        self._height_timer = QTimer(self)
        self._height_timer.setSingleShot(True)
        self._height_timer.timeout.connect(self._sync_content_height)
        if compact:
            self.quick_widget.rootObject().preferredContentHeightChanged.connect(self._schedule_content_height)
            self._schedule_content_height()

    def _schedule_content_height(self):
        if not self._closed:
            self._height_timer.start(0)

    def _sync_content_height(self):
        root = self.quick_widget.rootObject()
        if self._closed or not self._compact or root is None:
            return
        height = max(1, math.ceil(root.property("preferredContentHeight")))
        if self.minimumHeight() != height or self.maximumHeight() != height:
            self.setFixedHeight(height)
            self.updateGeometry()
            self.preferredHeightChanged.emit(height)

    def _warnings(self, errors):
        self.qml_warnings.extend(error.toString() for error in errors)

    @property
    def _full_sequence_tree(self):
        """Compatibility for applied/draft preview integrations during migration."""
        return self._full_sequence_preview

    @staticmethod
    def _application_theme(fallback):
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        return (app.property("quickThemeMode") if app is not None else None) or fallback

    def update_plan(self, final_sequence, df_batch, **kwargs):
        if self._closed:
            return
        previous = self._last_plan
        self._last_plan = dict(final_sequence=final_sequence, df_batch=df_batch, **kwargs)
        if self.isVisible():
            same_running_plan = (previous is not None
                                 and previous["final_sequence"] is final_sequence
                                 and previous["df_batch"] is df_batch
                                 and previous.get("acquisition_schedule") is kwargs.get("acquisition_schedule")
                                 and previous.get("run_outcome") == kwargs.get("run_outcome") == "running")
            if same_running_plan:
                # Many frame/file notifications can describe the same instant.
                # Publish their latest position at most 20 Hz, without copying
                # the schedule or restarting a timer until updates go quiet.
                if not self._update_timer.isActive():
                    self._update_timer.start()
            else:
                self._apply_latest_plan()
        full = self._full_sequence_preview
        if full is not None:
            full.update_plan(**self._last_plan)

    def clear_plan(self):
        self._update_timer.stop()
        self._last_plan = None
        self._pending_view_state = None
        if not self._closed and self.isVisible():
            self._apply_latest_plan()
        if self._full_sequence_preview is not None:
            self._full_sequence_preview.clear_plan()

    def _apply_latest_plan(self):
        self._update_timer.stop()
        if self._closed or not self.isVisible():
            return
        self.sequence_model.update_plan(**(self._last_plan or {
            "final_sequence": [], "df_batch": [], "acquisition_schedule": [],
        }))
        if self._pending_view_state is not None:
            state, self._pending_view_state = self._pending_view_state, None
            self._restore_now(state)

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_latest_plan()

    def hideEvent(self, event):
        self._update_timer.stop()
        super().hideEvent(event)

    @Slot()
    def showFullSequence(self):
        self.show_full_sequence()

    def show_full_sequence(self):
        if self._closed or not self._compact:
            return
        dialog = self._full_sequence_dialog
        created = dialog is None
        if dialog is None:
            dialog = QDialog(self)
            if self.window().testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen):
                dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            dialog.setWindowTitle("Full Dual Gate measurement sequence")
            dialog.resize(1080, 760)
            dialog.setMinimumSize(480, 440)
            preview = QuickDualGateSequencePreview(dialog, page_size=200, compact=False)
            preview.set_theme(self._application_theme(self._theme_mode))
            layout = QVBoxLayout(dialog)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(preview)
            self._full_sequence_dialog, self._full_sequence_preview = dialog, preview

            def release():
                preview.shutdown()
                if self._full_sequence_dialog is dialog:
                    self._full_sequence_dialog = self._full_sequence_preview = None
                dialog.deleteLater()

            dialog.finished.connect(release)
        preview = self._full_sequence_preview
        if self._last_plan is not None:
            preview.update_plan(**self._last_plan)
        else:
            preview.clear_plan()
        dialog.show()
        if created:
            # Start the expanded view at the item the operator inspected,
            # including a skipped candidate that has no execution step id.
            row = int(self.sequence_model.selectedDetail.get("pageRow", 0))
            position = (self.sequence_model.page - 1) * self._page_size + max(0, row)
            preview.sequence_model.setShowSkipped(self.sequence_model.showSkipped)
            preview.sequence_model.goToPage(position // 200 + 1)
            preview.sequence_model.selectRow(position % 200)
            preview.sequence_model.setFollowCurrent(self.sequence_model.followCurrent)
        dialog.raise_()

    def capture_view_state(self):
        if self._pending_view_state is not None:
            return copy.deepcopy(self._pending_view_state)
        model = self.sequence_model
        root = self.quick_widget.rootObject()
        detail = model.selectedDetail
        state = {
            "page": model.page, "page_row": detail.get("pageRow", -1),
            "follow_current": model.followCurrent, "show_skipped": model.showSkipped,
            "theme": self._theme_mode,
            "details_expanded": bool(root.property("detailsExpanded")) if root else True,
            "filters_expanded": bool(root.property("filtersExpanded")) if root else False,
            "order_expanded": bool(root.property("orderExpanded")) if root else False,
        }
        if self._full_sequence_dialog is not None and self._full_sequence_dialog.isVisible():
            state["full_open"] = True
            state["full_state"] = self._full_sequence_preview.capture_view_state()
        return state

    def restore_view_state(self, state):
        if not isinstance(state, dict) or self._closed:
            return
        self._pending_view_state = copy.deepcopy(state)
        if self.isVisible():
            state, self._pending_view_state = self._pending_view_state, None
            self._restore_now(state)

    def _restore_now(self, state):
        model = self.sequence_model
        model.setShowSkipped(bool(state.get("show_skipped", False)))
        model.goToPage(int(state.get("page", 1)))
        row = int(state.get("page_row", -1))
        if 0 <= row < model.rowCount():
            model.selectRow(row)
        model.setFollowCurrent(bool(state.get("follow_current", True)))
        # Session state owns navigation, not the current application palette.
        self.set_theme(self._application_theme(state.get("theme", self._theme_mode)))
        root = self.quick_widget.rootObject()
        root.setProperty("detailsExpanded", bool(state.get("details_expanded", True)))
        root.setProperty("filtersExpanded", bool(state.get("filters_expanded", False)))
        root.setProperty("orderExpanded", bool(state.get("order_expanded", False)))
        if state.get("full_open") and self._compact:
            self.show_full_sequence()
            self._full_sequence_preview.restore_view_state(state.get("full_state", {}))

    def set_theme(self, mode):
        if mode not in {"system", "light", "dark"}:
            raise ValueError("Theme must be system, light or dark")
        self._theme_mode = mode
        root = self.quick_widget.rootObject()
        if root is not None:
            root.setProperty("themeMode", mode)
        if self._full_sequence_preview is not None:
            self._full_sequence_preview.set_theme(mode)

    @Slot()
    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._update_timer.stop()
        self._height_timer.stop()
        if self._full_sequence_dialog is not None:
            self._full_sequence_dialog.close()
        self._pending_view_state = self._last_plan = None
        self.quick_widget.setSource(QUrl())
        self.sequence_model.update_plan([], [], acquisition_schedule=[])

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
