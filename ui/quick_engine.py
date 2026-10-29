"""Application-owned QML engine shared by the embedded Quick surfaces.

Each view receives its own models through initial properties, never through
engine-global context properties. Closing a view releases its scene, while
compiled components and imports remain reusable by the other workspaces.
"""
from PySide6.QtCore import QEvent, QObject, QTimer, Qt
from PySide6.QtGui import QPalette
from PySide6.QtQml import QQmlEngine
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid


def quick_engine():
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("Create QApplication before a Quick surface")
    engine = getattr(app, "_spectral_quick_engine", None)
    if engine is None or not isValid(engine):
        engine = QQmlEngine(app)
        app._spectral_quick_engine = engine
    return engine


class _HostBackground(QObject):
    """Keep embedded editors opaque while matching their native work surface.

    Transparent QQuickWidget gaps can composite as black in the Windows RHI
    backing store even when grabFramebuffer()/QWidget.grab() look correct.
    These editors need a matching surface, not see-through widget stacking.
    """
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self._ancestors = ()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.sync)
        view.installEventFilter(self)
        self.sync()

    def sync(self):
        ancestors = []
        parent = self.view.parentWidget()
        while parent is not None:
            ancestors.append(parent)
            parent = parent.parentWidget()
        ancestors = tuple(ancestors)
        if ancestors != self._ancestors:
            for ancestor in self._ancestors:
                if isValid(ancestor):
                    ancestor.removeEventFilter(self)
            for ancestor in ancestors:
                ancestor.installEventFilter(self)
            self._ancestors = ancestors

        color = QApplication.instance().palette().color(QPalette.ColorRole.Window)
        # Explicit surface ownership wins over palette values left on
        # transparent descendants while a stylesheet is being repolished.
        for ancestor in ancestors:
            palette = ancestor.palette()
            if ancestor.property("quickWorkspaceSurface"):
                color = palette.color(QPalette.ColorRole.Base)
                break
            if ancestor.property("quickSidebar"):
                color = palette.color(QPalette.ColorRole.AlternateBase)
                break
        else:
            for ancestor in ancestors:
                if (ancestor.autoFillBackground()
                        or ancestor.testAttribute(Qt.WidgetAttribute.WA_StyledBackground)):
                    candidate = ancestor.palette().color(ancestor.backgroundRole())
                    if candidate.alpha() == 255:
                        color = candidate
                        break
        color.setAlpha(255)
        if self.view.quickWindow().color() != color:
            self.view.setClearColor(color)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.ParentChange, QEvent.Type.PaletteChange,
                            QEvent.Type.ApplicationPaletteChange, QEvent.Type.StyleChange,
                            QEvent.Type.DynamicPropertyChange, QEvent.Type.Show):
            # Palette/style repolishing and reparenting finish before sampling
            # the host. Coalesce theme changes instead of polling each view.
            self._timer.start(0)
        return False


class QuickSurface(QQuickWidget):
    def __init__(self, parent=None, *, match_host_background=False):
        super().__init__(quick_engine(), parent)
        self._host_background = _HostBackground(self) if match_host_background else None

    def setInitialProperties(self, properties):
        # Theme is application-wide. A newly opened dialog must not reset the
        # shared singleton to System while the existing window uses Dark.
        defaults = {"themeMode": QApplication.instance().property("quickThemeMode") or "system"}
        defaults.update(properties)
        super().setInitialProperties(defaults)
