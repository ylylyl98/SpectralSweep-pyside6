"""Shared Quick controls keep standard keyboard editing and activation behavior."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import QObject, Property, Signal, Slot, Qt, QUrl
from PySide6.QtQml import QQmlComponent
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QTest

from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.quick_controls import QML_ROOT
from ui.quick_workbench import NavigationItem


@pytest.fixture
def probe():
    app = quick_application()
    widget = QQuickWidget()
    warnings = []
    widget.engine().warnings.connect(lambda messages: warnings.extend(m.toString() for m in messages))
    source = QUrl.fromLocalFile(str(QML_ROOT / "ControlProbe.qml"))
    component = QQmlComponent(widget.engine())
    component.setData(b'''
import QtQuick
import QtQuick.Layouts
AppPane {
    width: 460; height: 280
    property int activations: 0
    readonly property bool actionFocused: action.visualFocus
    readonly property color canvasColor: Theme.canvas
    readonly property color surfaceColor: Theme.surface
    readonly property color textColor: Theme.text
    readonly property color accentColor: Theme.accent
    readonly property color accentTextColor: Theme.accentForeground
    Column {
        spacing: 12
        AppButton { id: action; objectName: "action"; text: "Run"; onClicked: ++activations }
        AppComboBox { objectName: "editable"; editable: true; model: ["COM1", "COM2"] }
        AppSpinBox { objectName: "count"; from: 1; to: 9; value: 3 }
        AppCheckBox { objectName: "check"; text: "Enable" }
    }
}
''', source)
    root = component.create()
    assert root is not None, [error.toString() for error in component.errors()]
    widget.setContent(source, component, root)
    widget.resize(460, 280)
    widget.show()
    QTest.qWait(20)
    yield widget, root, warnings
    widget.close()
    widget.deleteLater()
    drain_qt_deletions()


def test_keyboard_focus_activation_disabled_and_check_state(probe):
    widget, root, warnings = probe
    button = root.findChild(QObject, "action")
    button.forceActiveFocus(Qt.FocusReason.TabFocusReason)
    assert root.property("actionFocused")
    QTest.keyClick(widget, Qt.Key.Key_Space)
    assert root.property("activations") == 1
    button.setProperty("enabled", False)
    QTest.keyClick(widget, Qt.Key.Key_Space)
    assert root.property("activations") == 1
    check = root.findChild(QObject, "check")
    assert check.property("contentItem").property("lineCount") == 1
    check.forceActiveFocus(Qt.FocusReason.TabFocusReason)
    QTest.keyClick(widget, Qt.Key.Key_Space)
    assert check.property("checked")
    assert not warnings


def test_editable_combo_spinbox_and_live_theme_switch(probe):
    widget, root, warnings = probe
    combo = root.findChild(QObject, "editable")
    combo.forceActiveFocus(Qt.FocusReason.TabFocusReason)
    QTest.keyClick(widget, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(widget, "COM7")
    assert combo.property("editText") == "COM7"
    combo.setProperty("editable", False)
    QTest.keyClick(widget, Qt.Key.Key_Space)
    QTest.qWait(10)
    QTest.keyClick(widget, Qt.Key.Key_Down)
    QTest.keyClick(widget, Qt.Key.Key_Return)
    assert combo.property("currentText") == "COM2"
    spin = root.findChild(QObject, "count")
    spin.forceActiveFocus(Qt.FocusReason.TabFocusReason)
    QTest.keyClick(widget, Qt.Key.Key_Up)
    assert spin.property("value") == 4
    QTest.keyClick(widget, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(widget, "7")
    QTest.keyClick(widget, Qt.Key.Key_Return)
    assert spin.property("value") == 7
    root.setProperty("themeMode", "light")
    light = root.property("canvasColor")
    root.setProperty("themeMode", "dark")
    assert root.property("canvasColor").lightness() < light.lightness()
    assert not warnings


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_native_palette_and_quick_tokens_agree_and_keep_readable_contrast(probe, mode):
    from PySide6.QtGui import QPalette
    from ui.application_style import application_style
    from ui.quick_style import apply_quick_style

    widget, root, warnings = probe
    app = quick_application()
    original_palette = app.palette()
    original_font = app.font()
    original_mode = app.property("quickThemeMode")
    manager = application_style(app)
    original_path = manager.path

    def luminance(color):
        channels = (color.redF(), color.greenF(), color.blueF())
        values = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                  for value in channels]
        return sum(value * weight for value, weight in zip(values, (0.2126, 0.7152, 0.0722)))

    def contrast(first, second):
        values = sorted((luminance(first), luminance(second)))
        return (values[1] + 0.05) / (values[0] + 0.05)

    try:
        apply_quick_style(app, mode)
        root.setProperty("themeMode", mode)
        for token, role in (("canvasColor", QPalette.ColorRole.Window),
                            ("surfaceColor", QPalette.ColorRole.Base),
                            ("textColor", QPalette.ColorRole.Text),
                            ("accentColor", QPalette.ColorRole.Highlight),
                            ("accentTextColor", QPalette.ColorRole.HighlightedText)):
            assert root.property(token) == app.palette().color(role)
        assert contrast(root.property("surfaceColor"), root.property("textColor")) >= 4.5
        assert contrast(root.property("accentColor"), root.property("accentTextColor")) >= 4.5
        assert not warnings
    finally:
        app.setPalette(original_palette)
        app.setFont(original_font)
        app.setProperty("quickThemeMode", original_mode)
        manager.path = original_path
        manager.reload()


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("appearance", ["primary", "accent", "danger"])
def test_action_emphasis_keeps_text_readable_and_disabled_actions_inert(probe, mode, appearance):
    widget, root, warnings = probe
    root.setProperty("themeMode", mode)
    button = root.findChild(QObject, "action")
    button.setProperty("appearance", appearance)

    def luminance(color):
        channels = (color.redF(), color.greenF(), color.blueF())
        return sum((value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4) * weight
                   for value, weight in zip(channels, (.2126, .7152, .0722)))

    def contrast():
        low, high = sorted(luminance(button.property(key)) for key in ("foregroundColor", "fillColor"))
        return (high + .05) / (low + .05)

    QTest.mouseMove(widget, widget.rect().bottomRight())
    assert contrast() >= 4.5
    center = button.mapToScene(button.boundingRect().center()).toPoint()
    QTest.mouseMove(widget, center)
    assert contrast() >= 4.5
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=center)
    assert contrast() >= 4.5
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=center)
    assert root.property("activations") == 1
    # A mouse-focused control needs an actual keyboard focus transition.
    QTest.keyClick(widget, Qt.Key.Key_Tab)
    QTest.keyClick(widget, Qt.Key.Key_Backtab)
    assert root.property("actionFocused")
    active_fill = button.property("fillColor")
    button.setProperty("enabled", False)
    assert button.property("fillColor") != active_fill
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=center)
    QTest.keyClick(widget, Qt.Key.Key_Space)
    assert root.property("activations") == 1
    assert not warnings


def test_navigation_collapses_to_filtered_workspace_selector():
    app = quick_application()

    class Bridge(QObject):
        changed = Signal()

        def __init__(self):
            super().__init__()
            self.items = [NavigationItem(key, title, self) for key, title in (
                ("spectrum", "Spectrum"), ("busy", "Busy workflow"),
                ("hidden", "Hidden workflow"), ("settings", "Settings"))]
            self.items[0].update(True, True, True)
            self.items[1].update(False, True, False)
            self.items[2].update(True, False, False)

        pages = Property("QVariantList", lambda self: self.items, constant=True)
        sampleId = Property(str, lambda self: "QA sample", constant=True)
        activityText = Property(str, lambda self: "Ready", constant=True)

        @Slot(str)
        def selectPage(self, page):
            pass

        @Slot()
        def toggleSidebar(self):
            pass

        @Slot()
        def showHistory(self):
            pass

        @Slot()
        def saveSample(self):
            pass

    bridge = Bridge()
    widget = QQuickWidget()
    widget.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
    warnings = []
    widget.engine().warnings.connect(lambda errors: warnings.extend(e.toString() for e in errors))
    widget.setInitialProperties({"bridge": bridge})
    widget.setSource(QUrl.fromLocalFile(str(QML_ROOT / "Navigation.qml")))
    try:
        assert widget.status() == QQuickWidget.Status.Ready
        widget.resize(640, 180)
        widget.show()
        QTest.qWait(20)
        root = widget.rootObject()
        def visual_item(item, name):
            if item.objectName() == name:
                return item
            for child in item.childItems():
                result = visual_item(child, name)
                if result is not None:
                    return result
            return None

        selector = root.findChild(QObject, "WorkspaceSelector")
        assert selector.isVisible()
        assert selector.property("count") == 3
        assert selector.property("currentValue") == "spectrum"
        assert not visual_item(root, "page_busy").property("enabled")
        assert root.implicitHeight() < 180
        widget.resize(1300, 180)
        QTest.qWait(20)
        assert not selector.isVisible()
        assert visual_item(root, "page_spectrum").isVisible()
        assert not warnings
    finally:
        widget.close()
        widget.deleteLater()
        drain_qt_deletions()
