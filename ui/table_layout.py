"""Keep table columns inside their viewport, including after model changes."""
from contextlib import contextmanager, nullcontext

from PySide6.QtCore import QEvent, QObject, QTimer, Qt, Slot
from PySide6.QtWidgets import QHeaderView, QToolTip
from shiboken6 import isValid


class _FittedColumns(QObject):
    def __init__(self, table, widths, minimum_widths):
        super().__init__(table)
        self.table = table
        self.viewport = table.viewport()
        self.widths = tuple(widths)
        self.minimum_widths = minimum_widths
        self._fitting = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.fit)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setWordWrap(True)
        table.installEventFilter(self)
        self.viewport.installEventFilter(self)
        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(16)
        header.geometriesChanged.connect(self.schedule)
        table.verticalScrollBar().valueChanged.connect(self.schedule)
        model = table.model()
        for signal in (model.modelReset, model.columnsInserted, model.columnsRemoved,
                       model.rowsInserted, model.dataChanged, model.headerDataChanged):
            signal.connect(self.schedule)
        self.schedule()

    @Slot()
    def schedule(self, *_args):
        # Header/model notifications can arrive while PySide tears down the
        # parent table and clears this wrapper's Python attributes.
        # One queued fit reads the final model and geometry. Restarting the
        # timer for every cell in a bulk refill adds thousands of Qt calls.
        if (not getattr(self, "_fitting", True)
                and isValid(self.table) and isValid(self._timer)
                and not self._timer.isActive()):
            self._timer.start(0)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show,
                            QEvent.Type.StyleChange, QEvent.Type.FontChange):
            self.schedule()
        if event.type() == QEvent.Type.ToolTip and watched is self.viewport:
            index = self.table.indexAt(event.pos())
            if index.isValid():
                text = index.data(Qt.ItemDataRole.ToolTipRole) or index.data(Qt.ItemDataRole.DisplayRole)
                if text:
                    QToolTip.showText(event.globalPos(), str(text), self.table)
                    return True
        return super().eventFilter(watched, event)

    @Slot()
    def fit(self):
        table = self.table
        if not isValid(table):
            return
        columns = [c for c in range(table.columnCount()) if not table.isColumnHidden(c)]
        available = table.viewport().width()
        if not columns or available < len(columns) * 16:
            return
        self._fitting = True
        try:
            header = table.horizontalHeader()
            requested = self.minimum_widths() if callable(self.minimum_widths) else self.minimum_widths
            minimums = [max(16, requested[c]) if c < len(requested) else 16 for c in columns]
            if sum(minimums) > available:
                minimums = [16] * len(columns)
            # Reserve the fitted minima before sharing remaining space. A
            # low-weight control column can keep room for its actual widgets.
            weights = [max(1, (self.widths[c] if c < len(self.widths) else 80) - minimum)
                       for c, minimum in zip(columns, minimums)]
            total = sum(weights)
            extra = available - sum(minimums)
            # Allocate by cumulative boundaries to avoid rounding overflow.
            used = 0
            cumulative = 0
            reserved = 0
            for column, minimum, weight in zip(columns, minimums, weights):
                reserved += minimum
                cumulative += weight
                edge = reserved + round(extra * cumulative / total)
                if header.sectionResizeMode(column) != QHeaderView.ResizeMode.Fixed:
                    header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
                if header.sectionSize(column) != edge - used:
                    header.resizeSection(column, edge - used)
                used = edge
            table.horizontalScrollBar().setValue(0)
            # The Quick editor lays out its own delegates. Its parked source
            # still supplies column widths but has no rows to paint.
            if getattr(table, "_quick_editor", None) is not None and not table.isVisible():
                return
            # Only measure visible rows; sequence previews may contain thousands.
            first = max(0, table.rowAt(0))
            last = table.rowAt(table.viewport().height() - 1)
            last = min(table.rowCount() - 1, first + 60) if last < 0 else last
            for row in range(first, min(last + 1, first + 61)):
                table.resizeRowToContents(row)
        finally:
            self._fitting = False


def fit_table_columns(table, widths, *, minimum_widths=()):
    """Fit weighted columns, reserving optional width floors when they fit."""
    controller = getattr(table, "_fitted_columns", None)
    if controller is None:
        controller = _FittedColumns(table, widths, minimum_widths)
        table._fitted_columns = controller
    else:
        controller.widths = tuple(widths)
        controller.minimum_widths = minimum_widths
        controller.schedule()
    return controller


@contextmanager
def batch_table_updates(table):
    """Defer painting and Quick structural resets until the table is filled."""
    editor = getattr(table, "_quick_editor", None)
    batch_updates = getattr(getattr(editor, "model", None), "batch_updates", nullcontext)
    updates_enabled = table.updatesEnabled()
    table.setUpdatesEnabled(False)
    try:
        with batch_updates():
            yield
    finally:
        table.setUpdatesEnabled(updates_enabled)
