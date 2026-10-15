"""Native workflow rows, dividers and optional detail disclosures."""
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QBoxLayout, QFrame, QHBoxLayout, QLabel, QSizePolicy,
    QToolButton, QVBoxLayout, QWidget,
)


def inline_divider():
    """A non-interactive boundary between unrelated controls on one row."""
    divider = QFrame()
    divider.setFrameShape(QFrame.Shape.VLine)
    divider.setProperty("fluentRole", "inlineDivider")
    divider.setFixedWidth(1)
    divider.setFixedHeight(20)
    divider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    return divider


def divide_grid_fields(grid, column, rows):
    """Insert a separator column, preserving spanning fields and alignment."""
    entries = []
    while grid.count():
        position = grid.getItemPosition(0)
        item = grid.takeAt(0)
        entries.append((item, position))
    for old_column in range(grid.columnCount() - 1, column - 1, -1):
        grid.setColumnStretch(old_column + 1, grid.columnStretch(old_column))
        grid.setColumnMinimumWidth(old_column + 1, grid.columnMinimumWidth(old_column))
    grid.setColumnStretch(column, 0)
    grid.setColumnMinimumWidth(column, 1)
    for item, (row, col, row_span, col_span) in entries:
        if col >= column:
            col += 1
        elif col + col_span > column:
            col_span += 1
        grid.addItem(item, row, col, row_span, col_span, item.alignment())
    for row in rows:
        grid.addWidget(inline_divider(), row, column, Qt.AlignmentFlag.AlignCenter)


class CompactRow(QWidget):
    """Keep labelled fields together; stack them when a row cannot fit."""

    def __init__(self, fields, *, labels_above=False, parent=None):
        super().__init__(parent)
        self._segments = []
        self._fields = []
        self._labels_above = labels_above
        self._dividers = []
        self._stacked = False
        self._flow = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._flow.setContentsMargins(0, 0, 0, 0)
        self._flow.setSpacing(6)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setSizePolicy(policy)
        for label, editor in fields:
            segment = QWidget(self)
            layout = QVBoxLayout(segment) if labels_above else QHBoxLayout(segment)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(4)
            label = QLabel(label) if isinstance(label, str) else label
            if label is not None and label.text():
                label.setBuddy(editor)
                if not labels_above:
                    label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
                layout.addWidget(label)
            elif label is not None:
                label.hide()
            layout.addWidget(editor, 0 if labels_above else 1)
            if not labels_above and editor.maximumWidth() < 16777215:
                layout.addStretch(1)
            if self._segments:
                divider = inline_divider()
                self._dividers.append(divider)
                self._flow.addWidget(divider, 0, Qt.AlignmentFlag.AlignCenter)
            self._segments.append(segment)
            self._fields.append((label, editor))
            self._flow.addWidget(segment, 1)

    def _row_width(self):
        # Styles can be applied after construction (or reloaded at runtime).
        # Measure current native hints, including spin-button and text padding.
        for segment, (label, editor) in zip(self._segments, self._fields):
            editor_width = min(editor.maximumWidth(), max(
                editor.minimumWidth(), 76, editor.minimumSizeHint().width(),
            ))
            label_width = label.sizeHint().width() if label is not None and label.text() else 0
            segment.setMinimumWidth(max(label_width, editor_width) if self._labels_above
                                    else label_width + (4 if label_width else 0) + editor_width)
        return sum(segment.minimumWidth() for segment in self._segments) + 13 * len(self._dividers)

    def minimumSizeHint(self):
        self._row_width()
        return QSize(max((segment.minimumWidth() for segment in self._segments), default=0),
                     self._height_for_width(self.width()))

    def sizeHint(self):
        return QSize(self._row_width(), self._height_for_width(self.width()))

    def _height_for_width(self, width):
        heights = [segment.sizeHint().height() for segment in self._segments]
        if width < self._row_width():
            return sum(heights) + 6 * max(0, len(heights) - 1)
        return max(heights, default=0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        stacked = self.width() < self._row_width()
        if stacked != self._stacked:
            self._stacked = stacked
            self._flow.setDirection(QBoxLayout.Direction.TopToBottom if stacked
                                    else QBoxLayout.Direction.LeftToRight)
            for divider in self._dividers:
                divider.setVisible(not stacked)
            self.updateGeometry()


def compact_form_rows(form, editors, *, labels_above=False):
    """Move existing form fields intact, retaining their labels and bindings."""
    rows = [form.getWidgetPosition(editor)[0] for editor in editors]
    if any(row < 0 for row in rows):
        raise ValueError("Compact fields must belong to the form")
    fields = [(form.labelForField(editor), editor) for editor in editors]
    for row in sorted(rows, reverse=True):
        form.takeRow(row)
    row = CompactRow(fields, labels_above=labels_above)
    form.insertRow(min(rows), row)
    return row


def bind_log_toggle(toggle, splitter, log):
    """Keep a splitter log optional, including sessions with a zero log size."""
    def update(visible):
        log.setVisible(visible)
        if visible and splitter.sizes()[1] < 80:
            splitter.setSizes([max(180, splitter.height() - 140), 140])

    toggle.toggled.connect(update)
    update(toggle.isChecked())


class DisclosureSection(QWidget):
    def __init__(self, title: str, content: QWidget, *, expanded=False, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.toggle = QToolButton(self)
        self.toggle.setText(title)
        self.toggle.setAccessibleName(title)
        self.toggle.setProperty("fluentRole", "sectionHeader")
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.toggle.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.toggle.setCheckable(True)
        self.content = content
        layout.addWidget(self.toggle)
        layout.addWidget(content)
        self.toggle.toggled.connect(self._update_expanded)
        self.toggle.setChecked(expanded)
        self._update_expanded(expanded)

    def _update_expanded(self, expanded):
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.content.setVisible(expanded)

    def set_expanded(self, expanded: bool):
        self.toggle.setChecked(expanded)
