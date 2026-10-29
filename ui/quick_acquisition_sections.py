"""Semantic acquisition rows retained through Quick scalar migration.

These are layout boundaries around existing controls and layouts. Device,
session, enabled-state and signal ownership remain with the source pages.
"""
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QApplication, QBoxLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QSizePolicy, QVBoxLayout, QWidget,
)


def _boundary(widget):
    widget.setProperty("quickSectionBoundary", True)
    widget.setSizePolicy(widget.sizePolicy().horizontalPolicy(), QSizePolicy.Policy.Maximum)
    return widget


def _row(name, parent=None):
    widget = _boundary(QWidget(parent))
    widget.setObjectName(name)
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    return widget, layout


def _section(title, parent=None):
    widget = _boundary(QGroupBox(title, parent))
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(6, 6, 6, 6)
    layout.setSpacing(6)
    return widget, layout


def _slot(layout, target):
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item.widget() is target or item.layout() is target:
            return layout, index
        if item.layout() is not None:
            found = _slot(item.layout(), target)
            if found:
                return found
    return None


def _take_layout(layout):
    parent = layout.parent()
    if isinstance(parent, QBoxLayout):
        index = next(index for index in range(parent.count()) if parent.itemAt(index).layout() is layout)
        parent.takeAt(index)
    elif isinstance(parent, QGridLayout):
        index = next(index for index in range(parent.count()) if parent.itemAt(index).layout() is layout)
        parent.takeAt(index)
    layout.setParent(None)
    return layout


def _split_grid_rows(grid, name):
    """Keep existing labelled pairs on their original, separate source rows."""
    column_count = grid.columnCount()
    stretches = [grid.columnStretch(column) for column in range(column_count)]
    entries = []
    while grid.count():
        position = grid.getItemPosition(0)
        entries.append((grid.takeAt(0), position))
    rows = {}
    for item, (row, column, row_span, column_span) in entries:
        if row not in rows:
            owner, holder = _row(f"{name}_{row}")
            inner = QGridLayout()
            inner.setContentsMargins(0, 0, 0, 0)
            inner.setHorizontalSpacing(grid.horizontalSpacing())
            for source_column, stretch in enumerate(stretches):
                inner.setColumnStretch(source_column, stretch)
            holder.addLayout(inner)
            rows[row] = owner, inner
        owner, inner = rows[row]
        inner.addItem(item, 0, column, row_span, column_span)
    for row, (owner, _) in rows.items():
        grid.addWidget(owner, row, 0, 1, max(1, column_count))
    return tuple(owner for owner, _ in rows.values())


class _SpectrumSectionsFit(QObject):
    def __init__(self, panel, row):
        super().__init__(panel)
        self.panel, self.row = panel, row
        panel.installEventFilter(self)
        self.fit()

    def fit(self):
        horizontal = self.panel.width() >= 1120
        self.row.setDirection(QBoxLayout.Direction.LeftToRight if horizontal
                              else QBoxLayout.Direction.TopToBottom)
        for index in range(self.row.count()):
            self.row.setStretch(index, 1 if horizontal else 0)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.fit()
        return False


class _McdSectionsFit(QObject):
    def __init__(self, panel, display, row, status_body, progress, telemetry):
        super().__init__(panel)
        self.display, self.row = display, row
        self.status_body = status_body
        self.progress, self.telemetry = progress, telemetry
        display.installEventFilter(self)
        telemetry.installEventFilter(self)
        self.fit()

    def fit(self):
        horizontal = self.display.width() >= 750
        self.row.setDirection(QBoxLayout.Direction.LeftToRight if horizontal
                              else QBoxLayout.Direction.TopToBottom)
        for index in range(self.row.count()):
            self.row.setStretch(index, 1 if horizontal else 0)
        # The narrow pane already stacks the two sections. Keep its progress
        # beside the readbacks when they fit, rather than paying for a third
        # full-width header row above the plot. Below that natural minimum,
        # restore the vertical arrangement instead of clipping telemetry.
        margins = (self.display.layout().contentsMargins(),
                   self.telemetry.parentWidget().contentsMargins(),
                   self.status_body.parent().contentsMargins())
        available = self.display.width() - sum(m.left() + m.right() for m in margins)
        inline = not horizontal and available >= (
            self.progress.minimumWidth() + self.telemetry.minimumSizeHint().width()
            + self.status_body.spacing())
        self.status_body.setDirection(QBoxLayout.Direction.LeftToRight if inline
                                      else QBoxLayout.Direction.TopToBottom)
        self.status_body.setStretch(0, 1 if inline else 0)
        self.status_body.setStretch(1, 4 if inline else 0)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.LayoutRequest):
            self.fit()
        return False


def _spectrum(panel):
    # Keep identity, frame rate and state together instead of stretching their
    # native labels to opposite edges of the desktop header.
    panel._device_status_row.setSpacing(12)
    panel._device_status_row.setStretch(0, 0)
    panel._device_status.setMinimumWidth(max(panel._device_status.minimumWidth(), 260))
    panel._device_status_row.addStretch(1)
    optics = _boundary(panel._spectrograph_group)
    optics_layout = optics.layout()
    optics_rows = _split_grid_rows(optics_layout.itemAt(0).layout(), "QuickSpectrumOptics")
    # A readback is its own native row, so it cannot drift beside routing edits.
    optics_layout.removeWidget(panel._optics_readback)
    readback, readback_layout = _row("QuickSpectrumOpticsReadback", optics)
    readback_layout.addWidget(panel._optics_readback)
    optics_layout.addWidget(readback)

    root = panel.layout()
    view_layout, _ = _slot(root, panel._comparison_mode)
    root_index = next(index for index in range(root.count()) if root.itemAt(index).layout() is view_layout)
    root.takeAt(root_index)
    view_layout.setParent(None)
    view_layout.removeWidget(panel._comparison_hint)
    view, view_group_layout = _section("Spectrum display", panel)
    options, options_layout = _row("QuickSpectrumDisplayOptions", view)
    options_layout.addLayout(view_layout)
    view_group_layout.addWidget(options)
    view_group_layout.addWidget(panel._comparison_hint)
    # Retain the existing acquisition command location if this helper is used
    # before the workbench has moved that command widget into the shell.
    controls_row, _ = _slot(root, optics)
    controls_row.setProperty("quickSectionColumns", True)
    controls_row.addWidget(view, 1, Qt.AlignmentFlag.AlignTop)
    controls_row.setAlignment(optics, Qt.AlignmentFlag.AlignTop)
    fit = _SpectrumSectionsFit(panel, controls_row)
    return dict(optics=optics, optics_rows=optics_rows, readback=readback,
                display=view, display_options=options, fit=fit)


def _motion(panel):
    groups = {group.title(): group for group in panel.findChildren(QGroupBox)}
    optical = _boundary(groups["Optical Settings"])
    optical_rows = _split_grid_rows(optical.layout(), "QuickMotionOptical")
    metadata = _boundary(groups["File / Metadata"])
    gates = _boundary(panel._gate_grp)
    owner = gates.parentWidget()
    parent_layout, index = _slot(owner.layout(), gates)
    options_layout, _ = _slot(owner.layout(), panel._apply_gates_chk)
    options = _take_layout(options_layout)
    wrapper, wrapper_layout = _row("QuickMotionSingleCondition", owner)
    parent_layout.takeAt(index)
    parent_layout.insertWidget(index, wrapper)
    wrapper_layout.addWidget(gates)
    options_row, options_holder = _row("QuickMotionGateOptions", wrapper)
    options_holder.addLayout(options)
    wrapper_layout.addWidget(options_row)
    return dict(optics=optical, optical_rows=optical_rows, gates=gates,
                gate_options=options_row, metadata=metadata)


def _mcd(panel, page_id):
    display = panel._splitter.widget(1)
    layout = display.layout()
    if page_id == "mcd":
        # The existing outer work area retains plot/splitter ownership.
        display.setTitle("Live acquisition")
        progress, field_row, status_row, title, header = [layout.takeAt(0) for _ in range(5)]
        status, status_layout = _section("Run status", display)
        progress_row, progress_layout = _row("QuickMcdProgress", status)
        progress_layout.addWidget(progress.widget())
        metrics, metrics_layout = _row("QuickMcdTelemetry", status)
        for item in (field_row, status_row):
            item.layout().setParent(None)
            metrics_layout.addLayout(item.layout())
        old_title = title.widget()
        old_title.hide()
        old_title.setParent(panel)
        header_layout = header.layout()
        header_layout.setParent(None)
        actions, action_layout = _section("Spectrum and log", display)
        action_layout.addLayout(header_layout)
        layout.insertWidget(0, status)
        layout.insertWidget(1, actions)
    else:
        status = _boundary(layout.itemAt(0).widget())
        status.setTitle("Run status")
        status_layout = status.layout()
        progress, state, count, telemetry = [status_layout.takeAt(0) for _ in range(4)]
        progress_row, progress_layout = _row("QuickMcdProgress", status)
        progress_layout.addWidget(progress.widget())
        metrics, metrics_layout = _row("QuickMcdTelemetry", status)
        state_row = QHBoxLayout()
        state_row.setSpacing(12)
        state_row.addWidget(state.widget())
        state_row.addWidget(count.widget())
        state_row.addStretch(1)
        metrics_layout.addLayout(state_row)
        telemetry.layout().setParent(None)
        metrics_layout.addLayout(telemetry.layout())
        header = layout.takeAt(1).layout()
        header.setParent(None)
        old_title = header.takeAt(0).widget()
        old_title.hide()
        old_title.setParent(panel)
        layout.removeWidget(panel.run_activity)
        layout.removeWidget(panel.spectrum_activity)
        actions, action_layout = _section("Live spectrum and log", display)
        action_layout.addLayout(header)
        activity, activity_layout = _row("QuickMcdSpectrumActivity", actions)
        activity_line = QHBoxLayout()
        activity_line.setSpacing(12)
        activity_line.addWidget(panel.run_activity)
        activity_line.addWidget(panel.spectrum_activity, 1)
        activity_layout.addLayout(activity_line)
        action_layout.addWidget(activity)
        layout.insertWidget(1, actions)
    progress_row.setMinimumWidth(64)
    status_body = QBoxLayout(QBoxLayout.Direction.TopToBottom)
    status_body.setContentsMargins(0, 0, 0, 0)
    status_body.setSpacing(6)
    status_body.addWidget(progress_row, 0, Qt.AlignmentFlag.AlignTop)
    status_body.addWidget(metrics, 0, Qt.AlignmentFlag.AlignTop)
    status_layout.insertLayout(0, status_body)
    layout.removeWidget(status)
    layout.removeWidget(actions)
    header_row = QHBoxLayout()
    header_row.setContentsMargins(0, 0, 0, 0)
    header_row.setSpacing(12)
    header_row.setProperty("quickSectionColumns", True)
    header_row.addWidget(status, 1, Qt.AlignmentFlag.AlignTop)
    header_row.addWidget(actions, 1, Qt.AlignmentFlag.AlignTop)
    layout.insertLayout(0, header_row)
    fit = _McdSectionsFit(panel, display, header_row, status_body, progress_row, metrics)
    return dict(status=status, progress=progress_row, telemetry=metrics,
                display_options=actions, header_row=header_row, status_body=status_body, fit=fit)


def configure_acquisition_sections(panel, page_id):
    """Call after workspace assembly and before installing parameter forms."""
    app = QApplication.instance()
    if app is None or not app.property("spectralQuick"):
        return {}
    existing = getattr(panel, "_quick_acquisition_sections", None)
    if existing is not None:
        return existing
    if page_id == "spectrum" and hasattr(panel, "_spectrograph_group"):
        result = _spectrum(panel)
    elif page_id == "power_sweep" and hasattr(panel, "_motion_grp") and hasattr(panel, "_gate_grp"):
        result = _motion(panel)
    elif page_id in {"mcd", "mcd2100"} and hasattr(panel, "_splitter") and hasattr(panel, "_condition_table"):
        result = _mcd(panel, page_id)
    else:
        result = {}
    panel._quick_acquisition_sections = result
    return result
