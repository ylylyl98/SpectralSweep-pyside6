"""Shared native splitter sizing and session preferences for workflow pages."""
import math
from types import MethodType

from PySide6.QtCore import QEvent, QObject, QTimer, Qt


def valid_splitter_sizes(value):
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(size, (int, float)) and not isinstance(size, bool)
                    and math.isfinite(size) and size > 0 for size in value)):
        return tuple(value)
    return None


class WorkspacePaneWidth(QObject):
    """Choose a readable initial pane width once the real viewport exists.

    QSplitter's construction-time sizes are weights, so applying [600, 340]
    before a desktop window is laid out can produce a 1000px settings pane.
    Normally seed only the initial geometry. A responsive default can follow
    the viewport until dragging or explicit session restoration takes over.
    """

    def __init__(self, panel, splitter, preferred, *, index=0, state_key="splitter_sizes", responsive=False):
        super().__init__(splitter)
        self.splitter = splitter
        self.preferred = preferred
        self.index = index
        self.responsive = responsive
        self.initialized = False
        self.last_width = None
        self.on_width_applied = None
        restored = getattr(panel, "_quick_restored_workspace_layout", {})
        self.restored_sizes = restored.get("sizes") if state_key else None
        self.restored_orientation = restored.get("orientation") if state_key else None
        splitter.setStretchFactor(index, 0)
        splitter.setStretchFactor(1 - index, 1)
        splitter.installEventFilter(self)
        splitter.splitterMoved.connect(self._moved)
        restore = getattr(panel, "restore_session_state", None)
        if callable(restore) and state_key:
            def restore_state(_panel, state):
                sizes = valid_splitter_sizes(state.get(state_key)) if isinstance(state, dict) else None
                if sizes is not None:
                    # A caller restoring its own layout takes precedence over
                    # the presentation default, even before the first show.
                    self.initialized = splitter.isVisible()
                    self.responsive = False
                    self.restored_sizes = sizes
                    self.restored_orientation = state.get("splitter_orientation")
                result = restore(state)
                if self.initialized and splitter.orientation() == Qt.Orientation.Horizontal:
                    self.last_width = splitter.sizes()[index]
                return result
            panel.restore_session_state = MethodType(restore_state, panel)
        QTimer.singleShot(0, self, self.initialize)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.Resize):
            callback = self.recover_width if self.initialized else self.initialize
            QTimer.singleShot(0, self, callback)
        return False

    def initialize(self):
        splitter = self.splitter
        if self.initialized or not splitter.isVisible() or splitter.orientation() != Qt.Orientation.Horizontal:
            return
        available = splitter.width() - splitter.handleWidth()
        if available <= 0:
            return
        preferred = self.preferred(available) if callable(self.preferred) else self.preferred
        if self.restored_sizes and self.restored_orientation in (None, "Horizontal"):
            preferred = available * self.restored_sizes[self.index] / sum(self.restored_sizes)
        other = splitter.widget(1 - self.index)
        width = min(int(preferred), max(0, available - other.minimumWidth()))
        sizes = [available - width, available - width]
        sizes[self.index] = width
        if splitter.sizes() != sizes:
            splitter.setSizes(sizes)
        self.initialized = True
        self.last_width = splitter.sizes()[self.index]
        if self.on_width_applied is not None:
            self.on_width_applied()

    def recover_width(self):
        """Recover a drag preference after a temporary minimum-width clamp."""
        splitter = self.splitter
        if self.last_width is None or splitter.orientation() != Qt.Orientation.Horizontal:
            return
        if self.responsive and not self.restored_sizes:
            self.initialized = False
            self.initialize()
            return
        available = splitter.width() - splitter.handleWidth()
        other = splitter.widget(1 - self.index)
        other_minimum = max(other.minimumWidth(), other.minimumSizeHint().width())
        # Qt handles constrained windows. Only restore once there is room for
        # the original width again; ordinary resize events keep native sizing.
        if available >= self.last_width + other_minimum and splitter.sizes()[self.index] != self.last_width:
            sizes = [available - self.last_width, available - self.last_width]
            sizes[self.index] = self.last_width
            splitter.setSizes(sizes)
            if self.on_width_applied is not None:
                self.on_width_applied()

    def reset(self):
        """Forget the saved/dragged preference and seed the default again."""
        self.restored_sizes = None
        self.restored_orientation = None
        self.last_width = None
        self.initialized = False
        self.initialize()

    def _moved(self, *_args):
        if self.splitter.orientation() == Qt.Orientation.Horizontal:
            self.responsive = False
            self.initialized = True
            self.last_width = self.splitter.sizes()[self.index]
