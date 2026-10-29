"""Shared compilation must not share view models or view lifetimes."""
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QPushButton, QWidget
from shiboken6 import isValid

from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.quick_controls import PanelCommandAdapter, QuickCommandWidget
from ui.quick_engine import quick_engine


def test_shared_engine_keeps_command_models_and_view_lifetimes_independent():
    app = quick_application()
    hosts, views, clicks = [], [], []
    for index in range(2):
        host = QWidget()
        button = QPushButton(f"Action {index}", host)
        button.clicked.connect(lambda checked=False, i=index: clicks.append(i))
        view = QuickCommandWidget(PanelCommandAdapter(str(index), {"run": button}), host)
        hosts.append(host)
        views.append(view)
    engine = quick_engine()
    assert all(view.engine() is engine for view in views)
    assert engine.parent() is app
    assert views[0].rootObject().property("bridge") is views[0].adapter.presenter
    assert views[1].rootObject().property("bridge") is views[1].adapter.presenter
    assert views[0].adapter.presenter.invoke("run")
    views[0].shutdown()
    hosts[0].deleteLater()
    drain_qt_deletions()
    assert isValid(engine)
    assert views[1].status() == QQuickWidget.Status.Ready
    assert views[1].adapter.presenter.invoke("run")
    assert clicks == [0, 1]
    views[1].shutdown()
    hosts[1].deleteLater()
    drain_qt_deletions()
    engine.collectGarbage()


def test_new_surface_inherits_application_theme():
    app = quick_application()
    previous = app.property("quickThemeMode")
    app.setProperty("quickThemeMode", "dark")
    host = QWidget()
    view = QuickCommandWidget(PanelCommandAdapter("Test", {"run": QPushButton("Run", host)}), host)
    try:
        assert view.rootObject().property("themeMode") == "dark"
        assert view.rootObject().property("darkMode")
    finally:
        view.shutdown()
        host.deleteLater()
        drain_qt_deletions()
        app.setProperty("quickThemeMode", previous)
