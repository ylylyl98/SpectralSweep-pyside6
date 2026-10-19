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
