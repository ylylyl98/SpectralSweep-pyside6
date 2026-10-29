"""Opaque embedded editor gaps follow their native host without polling."""
import json
import os
from pathlib import Path
import subprocess
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QPalette
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget
from shiboken6 import isValid

from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.application_style import application_style
from ui.quick_parameter_forms import install_parameter_forms
from ui.quick_style import apply_quick_style
from ui.quick_table_editor import install_table_editors


def _build_editors(parent):
    panel = QWidget(parent)
    layout = QVBoxLayout(panel)
    layout.addWidget(QSpinBox(panel))
    table = QTableWidget(1, 2, panel)
    table.setHorizontalHeaderLabels(["Target", "Value"])
    table.setItem(0, 0, QTableWidgetItem("Sample"))
    table.setItem(0, 1, QTableWidgetItem("1"))
    layout.addWidget(table)
    editor = install_table_editors(panel)[0]
    form = install_parameter_forms(panel, "settings")["forms"][0]
    return panel, form, editor


def _settle(*views):
    for _ in range(50):
        QTest.qWait(10)
        if all(not view._host_background._timer.isActive() for view in views):
            return
    pytest.fail("Host background did not settle")


def _assert_color(views, expected):
    _settle(*views)
    for view in views:
        assert view.quickWindow().color().alpha() == 255
        assert view.quickWindow().color() == expected


@pytest.fixture
def editors():
    app = quick_application()
    previous = app.palette(), app.font(), app.property("quickThemeMode"), app.styleSheet()
    manager = application_style(app)
    previous_path = manager.path
    root = QWidget()
    root_layout = QVBoxLayout(root)
    middle = QWidget(root)
    root_layout.addWidget(middle)
    middle_layout = QVBoxLayout(middle)
    panel, form, table = _build_editors(middle)
    middle_layout.addWidget(panel)
    views = form, table.quick_widget
    try:
        yield app, root, middle, panel, form, table, views
    finally:
        if isValid(form):
            form.shutdown()
        if isValid(table):
            table.shutdown()
        root.deleteLater()
        drain_qt_deletions()
        app.setPalette(previous[0])
        app.setFont(previous[1])
        app.setProperty("quickThemeMode", previous[2])
        manager.path = previous_path
        app.setStyleSheet(previous[3])


@pytest.mark.parametrize("property_name,role", [
    (None, QPalette.ColorRole.Window),
    ("quickWorkspaceSurface", QPalette.ColorRole.Base),
    ("quickSidebar", QPalette.ColorRole.AlternateBase),
])
def test_form_and_table_clear_color_matches_host_role(editors, property_name, role):
    app, root, _, _, _, _, views = editors
    if property_name:
        root.setProperty(property_name, True)
    _assert_color(views, root.palette().color(role) if property_name else app.palette().color(role))


def test_nearest_surface_and_theme_changes_update_both_editors(editors):
    app, root, middle, _, _, _, views = editors
    root.setProperty("quickSidebar", True)
    middle.setProperty("quickWorkspaceSurface", True)
    for mode in ("light", "dark", "system"):
        apply_quick_style(app, mode)
        _assert_color(views, middle.palette().color(QPalette.ColorRole.Base))
        middle.setProperty("quickWorkspaceSurface", False)
        _assert_color(views, root.palette().color(QPalette.ColorRole.AlternateBase))
        middle.setProperty("quickWorkspaceSurface", True)
        _assert_color(views, middle.palette().color(QPalette.ColorRole.Base))


def test_reparenting_complete_intermediate_container_rebinds_ancestors(editors):
    _, root, middle, _, _, _, views = editors
    sidebar = QWidget(root)
    sidebar.setProperty("quickSidebar", True)
    surface = QWidget(root)
    surface.setProperty("quickWorkspaceSurface", True)
    for parent, role in ((sidebar, QPalette.ColorRole.AlternateBase),
                         (surface, QPalette.ColorRole.Base),
                         (sidebar, QPalette.ColorRole.AlternateBase)):
        middle.setParent(parent)
        _assert_color(views, parent.palette().color(role))
    # A former ancestor must no longer influence the moved editor package.
    surface.setProperty("quickWorkspaceSurface", False)
    _assert_color(views, sidebar.palette().color(QPalette.ColorRole.AlternateBase))


def test_background_stays_idle_and_pending_callbacks_are_safe_during_deletion(editors, monkeypatch):
    app, root, _, panel, form, table, views = editors
    _settle(*views)
    timers = [view._host_background.findChildren(QTimer) for view in views]
    assert all(len(found) == 1 and found[0].isSingleShot() for found in timers)
    spies = [QSignalSpy(found[0].timeout) for found in timers]
    QTest.qWait(80)
    assert all(spy.count() == 0 for spy in spies)
    assert all(not found[0].isActive() for found in timers)
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    root.setProperty("quickWorkspaceSurface", True)
    assert any(found[0].isActive() for found in timers)
    form.shutdown()
    table.shutdown()
    panel.deleteLater()
    drain_qt_deletions()
    root.setProperty("quickWorkspaceSurface", False)
    app.setPalette(app.palette())
    QTest.qWait(30)
    assert all(not isValid(view) for view in views)
    assert not errors


@pytest.mark.skipif(sys.platform != "win32", reason="Windows default RHI regression")
def test_native_default_rhi_editor_framebuffers_are_opaque():
    project = Path(__file__).resolve().parents[1]
    script = r'''
import json, os
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtQuick import QSGRendererInterface
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QVBoxLayout, QWidget
from tests.quick_helpers import quick_application, drain_qt_deletions
from tests.test_quick_host_background import _build_editors
from ui.quick_style import apply_quick_style
# Importing the reusable test builder sets batch defaults; clear them before
# QApplication / the first Quick window chooses its rendering backend.
os.environ.pop('QT_QUICK_BACKEND', None)
os.environ.pop('QSG_RHI_BACKEND', None)
app = quick_application()
apply_quick_style(app, 'light')
root = QWidget()
root.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
root.setProperty('quickWorkspaceSurface', True)
layout = QVBoxLayout(root)
panel, form, table = _build_editors(root)
layout.addWidget(panel)
root.resize(680, 260)
root.show()
QTest.qWait(160)
apis = []
try:
    for view in (form, table.quick_widget):
        api = view.quickWindow().rendererInterface().graphicsApi()
        assert api not in (QSGRendererInterface.GraphicsApi.Software,
                           QSGRendererInterface.GraphicsApi.Unknown), api
        apis.append(api.name)
        image = view.grabFramebuffer().convertToFormat(QImage.Format.Format_RGBA8888)
        assert not image.isNull()
        assert set(bytes(image.constBits())[3::4]) == {255}
    print(json.dumps({'graphics_apis': apis, 'framebuffer_alpha': 255}))
finally:
    form.shutdown()
    table.shutdown()
    root.deleteLater()
    drain_qt_deletions()
'''
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "windows"
    environment.pop("QT_QUICK_BACKEND", None)
    environment.pop("QSG_RHI_BACKEND", None)
    result = subprocess.run([sys.executable, "-c", script], cwd=project, env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["framebuffer_alpha"] == 255
    assert len(report["graphics_apis"]) == 2
