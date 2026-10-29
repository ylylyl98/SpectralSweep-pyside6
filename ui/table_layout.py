"""Keep table columns inside their viewport, including after model changes."""
from PySide6.QtCore import QEvent, QObject, QTimer, Qt, Slot
from PySide6.QtWidgets import QHeaderView, QToolTip
from shiboken6 import isValid


class _FittedColumns(QObject):
    def __init__(self, table, widths):
        super().__init__(table)
        self.table = table
        self.viewport = table.viewport()
        self.widths = tuple(widths)
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
            weights = [self.widths[c] if c < len(self.widths) else 80 for c in columns]
            # Reserve Qt's minimum for each column before sharing remaining space.
            # A low-weight checkbox column must not overflow at narrow widths.
            weights = [max(1, weight - 16) for weight in weights]
            total = sum(weights)
            extra = available - len(columns) * 16
            # Allocate by cumulative boundaries to avoid rounding overflow.
            used = 0
            cumulative = 0
            for position, (column, weight) in enumerate(zip(columns, weights)):
                cumulative += weight
                edge = 16 * (position + 1) + round(extra * cumulative / total)
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
                header.resizeSection(column, edge - used)
                used = edge
            table.horizontalScrollBar().setValue(0)
            # Only measure visible rows; sequence previews may contain thousands.
            first = max(0, table.rowAt(0))
            last = table.rowAt(table.viewport().height() - 1)
            last = min(table.rowCount() - 1, first + 60) if last < 0 else last
            for row in range(first, min(last + 1, first + 61)):
                table.resizeRowToContents(row)
        finally:
            self._fitting = False


def fit_table_columns(table, widths):
    """Install once; callers may reconfigure widths after repopulating a table."""
    controller = getattr(table, "_fitted_columns", None)
    if controller is None:
        controller = _FittedColumns(table, widths)
        table._fitted_columns = controller
    else:
        controller.widths = tuple(widths)
        controller.schedule()
    return controller
