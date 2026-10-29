"""Presentation-only state for rebuilding the registered Quick workspaces.

Source values and edit validation belong to the page reload adapters. These
helpers run after that commit boundary and may be called again after showing
the rebuilt page, once its final scroll ranges are available.
"""
from collections import Counter, deque
from math import isfinite

from PySide6.QtCore import QObject, QItemSelectionModel, QSignalBlocker
from PySide6.QtWidgets import QScrollArea, QSplitter, QStackedWidget, QTableWidget, QTabWidget, QWidget
from shiboken6 import isValid


def _identified_widgets(panel):
    """Prefer source attribute paths; use unique names for layout-only owners."""
    identified, seen = {}, {id(panel)}
    pending = deque([(panel, "")])
    while pending:
        owner, prefix = pending.popleft()
        children = list(vars(owner).items())
        if isinstance(owner, (QTabWidget, QStackedWidget)):
            children.extend((f"pages[{index}]", owner.widget(index)) for index in range(owner.count()))
        for name, widget in children:
            if (not isinstance(widget, QWidget) or not isValid(widget)
                    or id(widget) in seen or not panel.isAncestorOf(widget)):
                continue
            seen.add(id(widget))
            path = prefix + name
            identified["attr:" + path] = widget
            pending.append((widget, path + "."))
    children = panel.findChildren(QWidget)
    names = Counter(widget.objectName() for widget in children if widget.objectName())
    for widget in children:
        name = widget.objectName()
        if id(widget) not in seen and name and names[name] == 1:
            identified["name:" + name] = widget
    return identified


def _list_view(surface, name):
    if surface is None or not isValid(surface):
        return None
    root = surface.rootObject()
    return root.findChild(QObject, name) if root is not None else None


def _quick_lists(panel, widgets):
    result = {}
    for key, table in widgets.items():
        if not isinstance(table, QTableWidget):
            continue
        editor = getattr(table, "_quick_editor", None)
        view = _list_view(getattr(editor, "quick_widget", None), "QuickEditableTable")
        if view is not None:
            result[key] = view
    reference = _list_view(getattr(panel, "_quick_reference_list", None), "SpectrumReferenceRows")
    if reference is not None:
        result["references"] = reference
    return result


def _number(value, default=0.0):
    return float(value) if isinstance(value, (int, float)) and isfinite(value) else default


def capture_quick_view_state(panel):
    """Read view positions and disclosure choices without committing any edit."""
    widgets = _identified_widgets(panel)
    state = {"disclosures": {}, "scrolls": {}, "splitters": {}, "tables": {}, "quick_lists": {}}
    for key, widget in widgets.items():
        # Fresh loading may replace the DisclosureSection class object.
        if (type(widget).__module__ == "ui.workflow_sections"
                and type(widget).__name__ == "DisclosureSection"):
            state["disclosures"][key] = widget.toggle.isChecked()
        elif isinstance(widget, QScrollArea):
            state["scrolls"][key] = (widget.horizontalScrollBar().value(), widget.verticalScrollBar().value())
        elif isinstance(widget, QSplitter):
            state["splitters"][key] = (widget.orientation().name, widget.sizes())
        elif isinstance(widget, QTableWidget):
            editor = getattr(widget, "_quick_editor", None)
            model = getattr(editor, "model", None)
            state["tables"][key] = {
                "selected": [(index.row(), index.column()) for index in widget.selectedIndexes()],
                "current": (widget.currentRow(), widget.currentColumn()),
                "scroll": (widget.horizontalScrollBar().value(), widget.verticalScrollBar().value()),
                "anchor": getattr(model, "_selection_anchor", widget.currentRow()),
            }
    for key, view in _quick_lists(panel, widgets).items():
        state["quick_lists"][key] = _number(view.property("contentY")) - _number(view.property("originY"))
    return state


def _restore_scroll(widget, values):
    for bar, value in zip((widget.horizontalScrollBar(), widget.verticalScrollBar()), values):
        with QSignalBlocker(bar):
            bar.setValue(max(bar.minimum(), min(bar.maximum(), int(value))))


def restore_quick_view_state(panel, state):
    """Restore supported view state immediately, bounded by the new geometry."""
    widgets = _identified_widgets(panel)
    for key, expanded in state.get("disclosures", {}).items():
        section = widgets.get(key)
        if section is not None and callable(getattr(section, "set_expanded", None)):
            section.set_expanded(expanded)
    for key, saved in state.get("tables", {}).items():
        table = widgets.get(key)
        if not isinstance(table, QTableWidget):
            continue
        selection = table.selectionModel()
        with QSignalBlocker(table), QSignalBlocker(selection):
            selection.clearSelection()
            row, column = saved["current"]
            selection.setCurrentIndex(table.model().index(row, column), QItemSelectionModel.SelectionFlag.NoUpdate)
            for row, column in saved["selected"]:
                index = table.model().index(row, column)
                if index.isValid():
                    selection.select(index, QItemSelectionModel.SelectionFlag.Select)
        _restore_scroll(table, saved["scroll"])
        editor = getattr(table, "_quick_editor", None)
        model = getattr(editor, "model", None)
        if model is not None and not model._closed:
            anchor = saved.get("anchor", -1)
            model._selection_anchor = anchor if 0 <= anchor < table.rowCount() else table.currentRow()
            model.refresh()
    for key, (orientation, sizes) in state.get("splitters", {}).items():
        splitter = widgets.get(key)
        if (isinstance(splitter, QSplitter) and splitter.orientation().name == orientation
                and splitter.count() == len(sizes)):
            splitter.setSizes(sizes)
            # Keep the first-show Quick width policy from replacing the saved
            # choice with its presentation default after the transaction.
            policy = getattr(panel, "_quick_pane_width", None)
            if policy is not None and getattr(policy, "splitter", None) is splitter:
                policy.initialized = True
                policy.responsive = False
                policy.restored_sizes = tuple(sizes)
                policy.restored_orientation = orientation
                policy.last_width = sizes[policy.index]
    for key, values in state.get("scrolls", {}).items():
        widget = widgets.get(key)
        if isinstance(widget, QScrollArea):
            _restore_scroll(widget, values)
    for key, view in _quick_lists(panel, widgets).items():
        if key in state.get("quick_lists", {}):
            origin = _number(view.property("originY"))
            extent = max(0.0, _number(view.property("contentHeight")) - _number(view.property("height")))
            view.setProperty("contentY", origin + max(0.0, min(extent, state["quick_lists"][key])))
