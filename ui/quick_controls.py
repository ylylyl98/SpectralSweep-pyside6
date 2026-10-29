"""Qt Quick command surfaces that share the existing validated command paths.

This adapter is the explicit temporary Widgets boundary. The QML presentation
only sees CommandPresenter; it cannot access a device or call arbitrary methods.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Signal, QTimer, QUrl, QSize
from PySide6.QtQuickWidgets import QQuickWidget
from ui.quick_engine import QuickSurface
from PySide6.QtWidgets import QGridLayout, QLabel, QSizePolicy, QWidget

from app.command_presenter import CommandPresenter


QML_ROOT = Path(__file__).resolve().parent / "qml"


class PanelCommandAdapter(QObject):
    def __init__(self, title, buttons, *, fields=None, status=None, parent=None):
        super().__init__(parent)
        self.title, self.buttons = title, dict(buttons)
        self.fields, self.status = dict(fields or {}), status
        self._closed = False
        self.before_dispatch = None
        self.after_dispatch = None
        self.availability = None
        self.presenter = CommandPresenter(self)
        self.presenter.commandRequested.connect(self._dispatch)
        self.presenter.fieldEdited.connect(self._edit)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.refresh)
        for button in self.buttons.values():
            button.installEventFilter(self)
            button.clicked.connect(self.schedule)
        for field, _label in self.fields.values():
            field.installEventFilter(self)
            field.valueChanged.connect(self.schedule)
        if status is not None:
            status.installEventFilter(self)
        self.refresh()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.EnabledChange, QEvent.Type.LayoutRequest,
                            QEvent.Type.Show, QEvent.Type.Hide):
            self.schedule()
        return False

    def schedule(self, *_args):
        if not self._closed:
            self._timer.start(0)

    def refresh(self):
        if self._closed:
            return
        actions = [dict(id=key, text=button.text().strip("▶■ "),
                        enabled=button.isEnabled() and (self.availability is None or self.availability(key)),
                        cancel=key in {"stop", "abort", "pause"},
                        tooltip=button.toolTip()) for key, button in self.buttons.items()]
        fields = [dict(id=key, label=label, value=field.value(),
                       minimum=field.minimum(), maximum=field.maximum(),
                       integer=not hasattr(field, "decimals"), enabled=field.isEnabled(),
                       decimals=field.decimals() if hasattr(field, "decimals") else 0)
                  for key, (field, label) in self.fields.items()]
        self.presenter.publish(title=self.title,
                               status=self.status.text() if self.status is not None else "",
                               actions=actions, fields=fields)

    def _dispatch(self, key):
        if self._closed:
            return
        button = self.buttons.get(key)
        # Recheck at dispatch: an earlier queued device result may have locked it.
        if button is not None and button.isEnabled():
            if self.before_dispatch is not None and not self.before_dispatch(key):
                return
            try:
                button.click()
            finally:
                if self.after_dispatch is not None:
                    self.after_dispatch(key)
        self.refresh()

    def _edit(self, key, value):
        if self._closed:
            return
        field = self.fields.get(key)
        if field and field[0].isEnabled():
            widget = field[0]
            widget.setValue(value if hasattr(widget, "decimals") else int(value))
        self.refresh()

    def shutdown(self):
        self._closed = True
        self._timer.stop()
        self.presenter.close()


class QuickCommandWidget(QuickSurface):
    heightHintChanged = Signal()
    def __init__(self, adapter, parent=None):
        super().__init__(parent, match_host_background=True)
        self.adapter = adapter
        adapter.setParent(self)
        self.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setInitialProperties({"bridge": adapter.presenter})
        self.setSource(QUrl.fromLocalFile(str(QML_ROOT / "CommandBar.qml")))
        if self.status() == QQuickWidget.Status.Error:
            raise RuntimeError("\n".join(error.toString() for error in self.errors()))
        self._height_timer = QTimer(self)
        self._height_timer.setSingleShot(True)
        self._height_timer.timeout.connect(self._resize_height)
        self.rootObject().implicitHeightChanged.connect(self._schedule_height)
        self.rootObject().widthChanged.connect(self._schedule_height)
        self.rootObject().dockPreferredWidthChanged.connect(self._schedule_height)
        self._resize_height()
        # Bounded display-only refresh covers legacy labels without a change
        # signal. It never walks a plan, reads a device, or captures a session.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(200)
        self._refresh_timer.timeout.connect(adapter.refresh)

    def _resize_height(self):
        root = self.rootObject()
        if root is not None:
            from math import ceil
            previous_width_limit = self.maximumWidth()
            if root.property("embedded"):
                self.setMaximumWidth(max(1, ceil(root.property("dockPreferredWidth"))))
            previous = self.height()
            self.setFixedHeight(max(36 if root.property("compact") else 48,
                                    ceil(root.implicitHeight()) + 2))
            # Qt may update QWidget geometry while a stacked page is hidden
            # without resizing its render-control window. Updating just the
            # QML root masks that mismatch and clips the framebuffer at 48px.
            render_window = self.quickWindow()
            if render_window is not None and render_window.size() != self.size():
                render_window.resize(self.size())
                render_window.update()
                self.update()
            root.setHeight(self.height())
            self.updateGeometry()
            if previous != self.height() or previous_width_limit != self.maximumWidth():
                self.heightHintChanged.emit()

    def sizeHint(self):
        root = self.rootObject()
        if root is not None and root.property("embedded"):
            from math import ceil
            return QSize(max(1, ceil(root.property("dockPreferredWidth"))), self.height())
        return super().sizeHint()

    def minimumSizeHint(self):
        # The preferred command width is allowed to shrink and wrap locally.
        # A size hint from the initial QML viewport must not pin it to 90 px.
        root = self.rootObject()
        if root is not None and root.property("embedded"):
            return QSize(0, self.height())
        return super().minimumSizeHint()

    def set_docked(self, *, compact=True):
        """Fit the existing command group into a pane without a footer band."""
        root = self.rootObject()
        root.setProperty("embedded", True)
        root.setProperty("compact", compact)
        self._schedule_height()

    def _schedule_height(self):
        if not self.adapter._closed:
            self._height_timer.start(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if getattr(self, "_height_timer", None) is not None:
            self._schedule_height()

    def showEvent(self, event):
        super().showEvent(event)
        if not self.adapter._closed:
            self._schedule_height()
            self.adapter.refresh()
            self._refresh_timer.start()

    def hideEvent(self, event):
        self._refresh_timer.stop()
        super().hideEvent(event)

    def shutdown(self):
        self._height_timer.stop()
        self._refresh_timer.stop()
        self.adapter.shutdown()
        self.setSource(QUrl())


def install_spectrum_controls(panel):
    """Keep measurement parameters together and replace only run actions."""
    if getattr(panel, "_quick_acquisition", None) is not None:
        return panel._quick_acquisition
    # Source controls keep their controller/configuration bindings. Reparent
    # them before hiding the old action group so parameter-form migration can
    # discover them beside wavelength, independently of the command dock.
    timing = QWidget(panel._spectrograph_group)
    timing.setObjectName("QuickSpectrumAcquisitionTiming")
    timing.setProperty("quickSectionBoundary", True)
    timing.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    timing_layout = QGridLayout(timing)
    timing_layout.setContentsMargins(0, 0, 0, 0)
    timing_layout.setHorizontalSpacing(8)
    timing_layout.addWidget(QLabel("Exposure / frame:", timing), 0, 0)
    timing_layout.addWidget(panel._exposure, 0, 1)
    timing_layout.addWidget(panel._frames_label, 0, 2)
    timing_layout.addWidget(panel._accumulations, 0, 3)
    timing_layout.setColumnStretch(4, 1)
    panel._spectrograph_group.setTitle("Measurement settings")
    panel._spectrograph_group.layout().insertWidget(1, timing)
    for source in (panel._exposure, panel._frames_label, panel._accumulations):
        source.show()
    panel._quick_acquisition_timing = timing
    adapter = PanelCommandAdapter("Acquisition", {
        "apply": panel._apply_btn, "acquire": panel._acquire_btn,
        "acquire2d": panel._acquire_2d_btn, "abort": panel._abort_btn,
        "run": panel._run_1d_btn, "run2d": panel._run_2d_btn, "stop": panel._stop_btn,
    }, status=panel._status_lbl)
    widget = QuickCommandWidget(adapter, panel)
    replaced = panel.layout().replaceWidget(panel._acquisition_group, widget)
    if replaced is None:
        widget.deleteLater()
        raise RuntimeError("Acquisition layout could not be replaced")
    panel._acquisition_group.hide()
    panel._quick_acquisition = widget
    return widget


def install_workflow_controls(panel, title, buttons, *, status=None, legacy_footer=None):
    existing = getattr(panel, "_quick_workflow_commands", None)
    if existing is not None:
        return existing
    adapter = PanelCommandAdapter(title, buttons, status=status)
    widget = QuickCommandWidget(adapter, panel)
    widget.rootObject().setProperty("fullStatus", status is not None)
    panel.layout().insertWidget(0, widget)
    for button in buttons.values():
        button.hide()
    if legacy_footer is not None:
        # Retain the source buttons/labels and their controller connections,
        # but release the old fixed band as soon as every command is forwarded.
        panel.layout().removeWidget(legacy_footer)
        legacy_footer.hide()
    panel._quick_workflow_commands = widget
    return widget
