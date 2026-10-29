"""Keep scientific preview destinations readable without horizontal paging."""
from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PySide6.QtWidgets import QStyle, QStyleOptionTab


class _AdaptivePreviewTabs(QObject):
    def __init__(self, tabs):
        super().__init__(tabs)
        self.tabs = tabs
        self.bar = tabs.tabBar()
        self._signature = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.fit)
        self.bar.setElideMode(Qt.TextElideMode.ElideNone)
        tabs.installEventFilter(self)
        self.bar.installEventFilter(self)
        self.fit()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Paint:
            # Coordinate updates can setTabText without a LayoutRequest. Fit
            # before native painting; the signature makes ordinary paints free.
            self.fit()
        elif event.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.LayoutRequest,
                            QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._signature = None
            self.timer.start(0)
        return False

    def _width(self, index, text):
        option = QStyleOptionTab()
        self.bar.initStyleOption(option, index)
        option.text = text
        metrics = self.bar.fontMetrics()
        lines = text.split("\n")
        content = QSize(max(metrics.horizontalAdvance(line) for line in lines),
                        metrics.height() * len(lines))
        return self.bar.style().sizeFromContents(QStyle.ContentsType.CT_TabBarTab,
                                                option, content, self.bar).width()

    def fit(self):
        bar = self.bar
        if not bar.count():
            return
        # Axis/coordinate updates still own captions. Normalize our line break
        # before measuring so a larger pane restores the original single line.
        labels = [" ".join(bar.tabText(index).split()) for index in range(bar.count())]
        available = self.tabs.contentsRect().width()
        signature = (tuple(labels), available, bar.font().toString())
        if signature == self._signature:
            return
        self._signature = signature
        wrap = sum(self._width(index, text) for index, text in enumerate(labels)) > available
        if bar.property("quickMultilineTabs") != wrap:
            bar.setProperty("quickMultilineTabs", wrap)
            bar.style().unpolish(bar)
            bar.style().polish(bar)
            bar.updateGeometry()
        for index, text in enumerate(labels):
            label = text.replace(" (", "\n(", 1) if wrap else text
            if bar.tabToolTip(index) != text:
                bar.setTabToolTip(index, text)
            if bar.tabText(index) != label:
                bar.setTabText(index, label)


def install_adaptive_preview_tabs(tabs):
    """Wrap coordinate qualifications only when the native tab strip needs it."""
    existing = getattr(tabs, "_quick_adaptive_preview_tabs", None)
    if existing is None:
        existing = _AdaptivePreviewTabs(tabs)
        tabs._quick_adaptive_preview_tabs = existing
    return existing
