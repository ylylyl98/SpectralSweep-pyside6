"""Semantic gate-workflow boundaries over the existing source controls.

Run before scalar migration. Native rows/columns retain their association while
their individual controls still use the ordinary Quick parameter adapters.
"""
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QApplication, QFormLayout, QGridLayout, QGroupBox, QLabel, QSizePolicy,
    QVBoxLayout, QWidget,
)


def _boundary(widget):
    widget.setProperty("quickSectionBoundary", True)
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)


def _cell(widget, parent):
    hidden = widget.isHidden()
    cell = QWidget(parent)
    _boundary(cell)
    layout = QVBoxLayout(cell)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    layout.addWidget(widget)
    widget.setVisible(not hidden)
    return cell


def _mega_sections(panel):
    safety = panel._safety_widget
    _boundary(safety)
    grid = safety.layout()
    for row in range(4):
        for column in range(3):
            item = grid.itemAtPosition(row, column)
            widget = item.widget() if item is not None else None
            if widget is None:
                continue
            if isinstance(widget, QLabel):
                # Keep the row captions and column headers in the source grid.
                # Migrating a header-only row to Flow loses column ownership.
                widget.setProperty("quickSectionBoundary", True)
                continue
            grid.removeWidget(widget)
            axis = ("Vtg", "Vbg", "Vbias")[row - 1]
            bound = "minimum" if column == 1 else "maximum"
            widget.setAccessibleName(f"{axis} {bound} voltage")
            widget.setProperty("quickPreferredWidth", 116)
            grid.addWidget(_cell(widget, safety), row, column)
    grid.setColumnStretch(0, 0)
    grid.setColumnStretch(1, 1)
    grid.setColumnStretch(2, 1)
    grid.setColumnStretch(3, 0)
    grid.setHorizontalSpacing(12)
    # Clear section ownership adds genuine heading boundaries. Reclaim the
    # old inter-section padding so the default desktop recipe still fits.
    panel._settings_grid.setVerticalSpacing(5)
    for index in range(panel._left_lay.count()):
        spacer = panel._left_lay.itemAt(index).spacerItem()
        if spacer is not None and spacer.sizeHint().height() == 12:
            spacer.changeSize(0, 4, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)

    # These are disclosure actions, not measurement enable flags. Keeping the
    # native header/body boundary stops a heading joining its first input row.
    for section in (panel._timing_section, panel._optical_group, panel._metadata_group):
        _boundary(section)
        section._header.setProperty("quickDisclosure", True)
        section._content.setProperty("quickSectionBoundary", True)
    return {"safety": safety, "metadata": panel._metadata_group,
            "timing": panel._timing_section, "optical": panel._optical_group}


class _MetadataColumns(QObject):
    def __init__(self, frame, groups):
        super().__init__(frame)
        self.frame = frame
        self.groups = groups
        self.columns = None
        frame.installEventFilter(self)
        self.fit()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.fit()
        return False

    def fit(self):
        columns = 3 if self.frame.width() >= 1200 else 2 if self.frame.width() >= 750 else 1
        if columns == self.columns:
            return
        self.columns = columns
        grid = self.frame.layout()
        for group in self.groups:
            grid.removeWidget(group)
        if columns == 3:
            for column, group in enumerate(self.groups):
                grid.addWidget(group, 0, column, Qt.AlignmentFlag.AlignTop)
        elif columns == 2:
            grid.addWidget(self.groups[0], 0, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(self.groups[1], 0, 1, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(self.groups[2], 1, 0, 1, 2, Qt.AlignmentFlag.AlignTop)
        else:
            for row, group in enumerate(self.groups):
                grid.addWidget(group, row, 0, Qt.AlignmentFlag.AlignTop)
        for column in range(3):
            grid.setColumnStretch(column, (2 if column < 2 else 1) if columns == 3 else int(column < columns))


def _dual_sections(panel):
    frame = panel._sample_edit.parentWidget()
    original = frame.layout()
    # Retain legacy labels/dividers and their layout without displaying unused
    # separators. The original fields and buddies are moved, never recreated.
    labels = {label.buddy(): label for label in frame.findChildren(QLabel) if label.buddy() is not None}
    parking = QWidget(frame)
    parking.setObjectName("QuickGateMetadataLegacyLayout")
    parking.setLayout(original)
    parking.hide()
    grid = QGridLayout(frame)
    grid.setContentsMargins(4, 0, 4, 0)
    grid.setHorizontalSpacing(20)
    grid.setVerticalSpacing(8)
    groups = []
    for title, object_name, fields in (
        ("Sample and location", "QuickGateSampleMetadata",
         ((panel._sample_edit, 180), (panel._point_edit, 140), (panel._temp_edit, 90))),
        ("Measurement context", "QuickGateMeasurementMetadata",
         ((panel._mode_combo_name, 100), (panel._laser_edit, 120), (panel._power_edit, 120))),
        ("Output", "QuickGateOutputMetadata", ((panel._subfolder_edit, 230),)),
    ):
        group = QGroupBox(title, frame)
        group.setObjectName(object_name)
        _boundary(group)
        form = QFormLayout(group)
        form.setContentsMargins(0, 4, 0, 0)
        form.setSpacing(6)
        for widget, width in fields:
            hidden = widget.isHidden()
            widget.setMinimumWidth(0)
            widget.setProperty("quickPreferredWidth", width)
            widget.setProperty("quickLabelAbove", True)
            label = labels[widget]
            form.addRow(label, widget)
            widget.setVisible(not hidden)
            label.show()
        groups.append(group)
    panel._quick_metadata_columns = _MetadataColumns(frame, groups)
    return {"sample": groups[0], "measurement": groups[1], "output": groups[2]}


def configure_gate_sections(panel, page_id):
    """Create readable gate sections in Quick, before scalar migration."""
    app = QApplication.instance()
    if app is None or not app.property("spectralQuick"):
        return {}
    existing = getattr(panel, "_quick_gate_sections", None)
    if existing is not None:
        return existing
    if page_id == "mega_sweep" and hasattr(panel, "_safety_widget"):
        result = _mega_sections(panel)
    elif page_id == "dual_gate" and hasattr(panel, "_sample_edit"):
        result = _dual_sections(panel)
    else:
        return {}
    panel._quick_gate_sections = result
    return result
