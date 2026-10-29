"""Exercise live application styling without rebuilding controls or hardware."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from ui.application_style import ApplicationStyle, STYLE_PATH, read_style
from ui.style_roles import set_style_role


@pytest.fixture
def app():
    app = QApplication.instance() or QApplication([])
    original = app.styleSheet()
    app.setStyleSheet(read_style(STYLE_PATH))
    yield app
    app.setStyleSheet(original)


def test_status_changes_repolish_only_the_existing_label(app):
    label = QLabel("Acquisition failed")
    label.setObjectName("ExistingStatusId")
    set_style_role(label, tone="danger", size="status", strong=True)
    label.ensurePolished()
    assert label.palette().color(QPalette.ColorRole.WindowText).name() == "#b42318"
    set_style_role(label, tone="success")
    assert label.palette().color(QPalette.ColorRole.WindowText).name() == "#23642c"
    assert label.property("spectralTextSize") == ""
    assert label.property("spectralStrong") is False
    assert label.objectName() == "ExistingStatusId"
    assert label.text() == "Acquisition failed"
    assert label.styleSheet() == ""
    set_style_role(label, "")
    assert label.palette().color(QPalette.ColorRole.WindowText).name() == "#202936"


def test_application_style_reload_updates_semantic_control_without_rewiring(app, tmp_path):
    path = tmp_path / "application.qss"
    source = read_style(STYLE_PATH)
    path.write_text(source, encoding="utf-8")
    manager = ApplicationStyle(app, path)
    button = QPushButton("Run")
    set_style_role(button, "runAction")
    clicks = []
    button.clicked.connect(lambda: clicks.append("run"))
    assert manager.reload()
    button.ensurePolished()
    assert button.palette().color(QPalette.ColorRole.ButtonText).name() == "#1a4020"
    path.write_text(source.replace("color: #1a4020", "color: #164016"), encoding="utf-8")
    assert manager.reload()
    assert button.palette().color(QPalette.ColorRole.ButtonText).name() == "#164016"
    button.click()
    assert clicks == ["run"]
    assert button.styleSheet() == ""
    button.setEnabled(False)
    assert button.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText).name() == "#aaaaaa"
    manager.deleteLater()


def test_unchanged_stylesheet_reload_does_not_repolish_all_widgets(app, monkeypatch):
    manager = ApplicationStyle(app, STYLE_PATH)
    calls, reloaded = [], []
    monkeypatch.setattr(app, "setStyleSheet", lambda source: calls.append(source))
    manager.reloaded.connect(reloaded.append)
    assert manager.reload()
    assert manager.reload()
    assert not calls
    assert len(reloaded) == 2
    manager.deleteLater()


def test_palette_change_repolishes_palette_based_stylesheet(app):
    from PySide6.QtWidgets import QMainWindow
    from ui.quick_style import apply_quick_style
    from ui.application_style import application_style
    original_palette = app.palette()
    original_mode = app.property("quickThemeMode")
    manager = application_style(app)
    original_path = manager.path
    window = QMainWindow()
    try:
        for mode, expected in (("light", "#eef2f6"), ("dark", "#151c26"), ("light", "#eef2f6")):
            apply_quick_style(app, mode)
            window.ensurePolished()
            assert window.palette().color(QPalette.ColorRole.Window).name() == expected
    finally:
        window.deleteLater()
        app.setPalette(original_palette)
        app.setProperty("quickThemeMode", original_mode)
        manager.path = original_path
        manager.reload()


@pytest.mark.parametrize("canvas,surface", [("#eef2f6", "#ffffff"), ("#151c26", "#202a37")])
def test_empty_stacks_and_scrolled_tools_share_the_owning_workspace_surface(app, canvas, surface):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QHBoxLayout, QScrollArea, QStackedWidget, QVBoxLayout, QWidget
    from ui.application_style import QUICK_STYLE_PATH

    original_palette = app.palette()
    palette = QPalette(original_palette)
    palette.setColor(QPalette.ColorRole.Window, QColor(canvas))
    palette.setColor(QPalette.ColorRole.Base, QColor(surface))
    app.setPalette(palette)
    app.setStyleSheet(read_style(QUICK_STYLE_PATH))
    panel = QWidget()
    panel.setProperty("quickWorkspaceSurface", True)
    panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    layout = QHBoxLayout(panel)
    layout.setContentsMargins(12, 12, 12, 12)
    stack = QStackedWidget()
    stack.addWidget(QLabel("No spectrum yet"))
    layout.addWidget(stack)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    content = QWidget()
    content_layout = QVBoxLayout(content)
    content_layout.addWidget(QLabel("Current acquisition"))
    scroll.setWidget(content)
    layout.addWidget(scroll)
    panel.resize(400, 200)
    panel.show()
    app.processEvents()
    try:
        image = panel.grab().toImage()
        # Sample clear background below each label, avoiding the pane border.
        for widget in (stack, content):
            point = widget.mapTo(panel, QPoint(8, widget.height() - 8))
            assert image.pixelColor(point).name() == surface
    finally:
        panel.close()
        panel.deleteLater()
        app.setPalette(original_palette)
