"""Exercise Spectrum actions and reusable QML command fields without hardware."""
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import QObject, QPoint, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QDoubleSpinBox, QGroupBox, QLabel, QPushButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget

from ui.quick_controls import PanelCommandAdapter, QuickCommandWidget, install_spectrum_controls, install_workflow_controls
from ui.spectrum_panel import SpectrumPanel
from ui.spectrum_preview import PreviewSpectrumController
from tests.quick_helpers import quick_application, drain_qt_deletions


@pytest.fixture(scope="module")
def app():
    app = quick_application()
    font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
    if not QFontDatabase.families() and font.exists():
        QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Segoe UI", 9))
    return app


def find_item(item, name):
    if item.objectName() == name:
        return item
    for child in item.childItems():
        match = find_item(child, name)
        if match is not None:
            return match
    return None


@pytest.fixture
def spectrum(app, monkeypatch):
    monkeypatch.setattr("ui.detector_wavelength_advice.confirm_detector_wavelength", lambda *a, **k: True)
    controller = PreviewSpectrumController()
    panel = SpectrumPanel(controller)
    widget = install_spectrum_controls(panel)
    warnings = []
    widget.engine().warnings.connect(lambda messages: warnings.extend(e.toString() for e in messages))
    panel.resize(1150, 780)
    panel.show()
    controller.set_connected(True)
    QTest.qWait(30)
    widget.adapter.refresh()
    yield panel, widget, controller, warnings
    controller.abort_acquisition()
    widget.shutdown()
    panel.close()
    panel.deleteLater()
    controller.deleteLater()
    drain_qt_deletions()


@pytest.fixture
def command_fields(app):
    """Exercise reusable command fields independently of Spectrum's layout."""
    host = QWidget()
    layout = QVBoxLayout(host)
    exposure, frames = QDoubleSpinBox(host), QSpinBox(host)
    exposure.setRange(1, 600000)
    exposure.setDecimals(2)
    exposure.setValue(250)
    frames.setRange(1, 1000)
    status = QLabel("Ready", host)
    buttons = {key: QPushButton(text, host) for key, text in
               (("run", "Run"), ("stop", "Stop"))}
    for source in (exposure, frames, status, *buttons.values()):
        source.hide()
    adapter = PanelCommandAdapter("Generic command fields", buttons,
        fields={"exposure": (exposure, "Exposure (ms)"), "frames": (frames, "Frames")},
        status=status)
    widget = QuickCommandWidget(adapter, host)
    layout.addWidget(widget)
    warnings = []
    widget.engine().warnings.connect(lambda messages: warnings.extend(e.toString() for e in messages))
    host.resize(1150, 180)
    host.show()
    QTest.qWait(30)
    adapter.refresh()
    yield SimpleNamespace(exposure=exposure, frames=frames, status=status, buttons=buttons), widget, warnings
    widget.shutdown()
    host.close()
    host.deleteLater()
    drain_qt_deletions()


def test_qml_spectrum_uses_existing_apply_acquire_and_cancel(spectrum):
    panel, widget, controller, warnings = spectrum
    assert widget.status() == QQuickWidget.Status.Ready
    assert panel._acquisition_group.isHidden()
    bridge = widget.adapter.presenter
    assert panel._spectrograph_group.isAncestorOf(panel._exposure)
    assert panel._spectrograph_group.isAncestorOf(panel._accumulations)
    assert bridge.fields == []
    assert find_item(widget.rootObject(), "field_exposure") is None
    panel._exposure.setValue(15.5)
    assert panel._exposure.value() == 15.5
    panel._accumulations.setValue(3)
    assert panel._accumulations.value() == 3
    assert bridge.invoke("acquire")
    assert not bridge.invoke("acquire")
    assert controller.exposure_ms == 15.5
    # Rendering other embedded surfaces can delay the preview timer; wait for
    # the actual settings→acquisition transition instead of assuming 110 ms.
    for _ in range(50):
        if controller._pending == "1d":
            break
        QTest.qWait(20)
    widget.adapter.refresh()
    assert controller._pending == "1d"
    assert bridge.invoke("abort")
    assert controller._pending is None
    QTest.qWait(240)
    assert panel._last_data is None
    assert warnings == []


def test_invalid_draft_blocks_start_but_keeps_stop_available(command_fields):
    sources, widget, _ = command_fields
    bridge = widget.adapter.presenter
    started, stopped = QSignalSpy(sources.buttons["run"].clicked), QSignalSpy(sources.buttons["stop"].clicked)
    assert not bridge.editField("frames", "1.5")
    assert not bridge.invoke("run")
    assert started.count() == 0
    assert bridge.invoke("stop")
    assert stopped.count() == 1
    assert bridge.editField("frames", "2")
    assert bridge.invoke("run")
    assert started.count() == 1 and sources.frames.value() == 2
    widget.adapter.refresh()
    assert bridge.invoke("stop")
    assert stopped.count() == 2


def test_refresh_keeps_qml_field_identity_and_keyboard_focus(command_fields):
    sources, widget, warnings = command_fields
    sources.exposure.setDecimals(2)
    sources.exposure.setValue(42.5)
    widget.adapter.refresh()
    field = find_item(widget.rootObject(), "field_exposure")
    assert field is not None
    assert field.property("text") == "42.50"
    changed = QSignalSpy(sources.exposure.valueChanged)
    position = field.mapToScene(field.boundingRect().center()).toPoint()
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=position)
    QTest.qWait(1)
    assert field.property("activeFocus")
    assert field.property("text") == "42.50"
    assert sources.exposure.value() == 42.5 and changed.count() == 0

    # Preserve the existing value and cursor when editing inside the number,
    # including adapter publications between actual key events.
    field.setProperty("cursorPosition", 1)
    QTest.keyClicks(widget, "1")
    QTest.qWait(1)
    widget.adapter.refresh()
    assert field.property("text") == "412.50"
    assert field.property("cursorPosition") == 2
    assert sources.exposure.value() == 412.5
    QTest.keyClick(widget, Qt.Key.Key_Backspace)
    QTest.qWait(1)
    assert field.property("text") == "42.50"
    assert field.property("cursorPosition") == 1
    QTest.keyClick(widget, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(widget, "123.4")
    for index in range(5):
        sources.status.setText(f"Status {index}")
        widget.adapter.refresh()
    assert find_item(widget.rootObject(), "field_exposure") is field
    assert field.property("activeFocus")
    assert field.property("text") == "123.4"
    assert sources.exposure.value() == 123.4
    assert warnings == []


def test_shutdown_does_not_restart_adapters_on_widget_changes(app):
    button = QPushButton("Start")
    adapter = PanelCommandAdapter("Example", {"run": button})
    widget = QuickCommandWidget(adapter)
    widget.show()
    widget.shutdown()
    button.setEnabled(False)
    button.setEnabled(True)
    widget.hide()
    widget.show()
    QTest.qWait(20)
    assert not adapter._timer.isActive()
    assert not widget._refresh_timer.isActive()
    assert not adapter.presenter.invoke("run")
    widget.close()
    widget.deleteLater()
    button.deleteLater()


def test_wrapped_stop_renders_full_framebuffer_and_accepts_mouse_after_stack_resize(app):
    host = QWidget()
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    stack = QStackedWidget()
    layout.addWidget(stack)
    exposure, frames = QDoubleSpinBox(), QSpinBox()
    exposure.setRange(1, 600000)
    exposure.setValue(250)
    frames.setRange(1, 1000)
    buttons = {key: QPushButton(text) for key, text in (
        ("apply", "Apply"), ("acquire", "Acquire 1D"), ("acquire2d", "Acquire 2D"),
        ("abort", "Abort"), ("run", "Run 1D"), ("run2d", "Run 2D"), ("stop", "Stop"))}
    status = QLabel("Disconnected")
    long = QuickCommandWidget(PanelCommandAdapter("Acquisition", buttons,
        fields={"exposure": (exposure, "Exposure (ms)"), "frames": (frames, "Frames")}, status=status))
    short_button = QPushButton("Run")
    short = QuickCommandWidget(PanelCommandAdapter("Dual Gate", {"run": short_button}))
    stack.addWidget(long)
    stack.addWidget(short)
    def fit():
        stack.setFixedHeight(stack.currentWidget().height())
    stack.currentChanged.connect(fit)
    long.heightHintChanged.connect(fit)
    short.heightHintChanged.connect(fit)
    host.resize(1400, 180)
    host.show()
    QTest.qWait(30)
    stack.setCurrentWidget(short)
    # Leave a small font-metric margin so every supported Qt platform wraps.
    host.resize(960, 180)
    QTest.qWait(10)
    stack.setCurrentWidget(long)
    QTest.qWait(40)
    try:
        stop = find_item(long.rootObject(), "command_stop")
        rect = stop.mapRectToScene(stop.boundingRect())
        assert rect.y() >= 30  # Stop really wrapped onto its second row.
        assert long.quickWindow().size() == long.size()
        image = long.grabFramebuffer()
        ratio = image.width() / long.width()
        # Qt rounds a half physical pixel upward; Python round uses ties to
        # even (e.g. 82 logical px at 125% becomes 103 physical px).
        assert image.height() == int(long.height() * image.devicePixelRatio() + .5)
        assert image.height() >= round(rect.bottom() * ratio)
        colors = {image.pixelColor(x, y).rgba()
                  for x in range(round(rect.left() * ratio), round(rect.right() * ratio))
                  for y in range(round(rect.top() * ratio), round(rect.bottom() * ratio))}
        assert len(colors) > 5  # Visible border and text, not a blank lower strip.
        assert not long.adapter.presenter.editField("frames", "invalid")
        QTest.qWait(20)
        clicked = QSignalSpy(buttons["stop"].clicked)
        QTest.mouseClick(long, Qt.MouseButton.LeftButton,
                         pos=stop.mapToScene(stop.boundingRect().center()).toPoint())
        assert clicked.count() == 1
    finally:
        long.shutdown()
        short.shutdown()
        host.close()
        host.deleteLater()
        for source in [exposure, frames, status, short_button, *buttons.values()]:
            source.deleteLater()
        drain_qt_deletions()


def test_footer_actions_align_right_wrap_in_order_and_keep_keyboard_focus(app):
    buttons = {key: QPushButton(text) for key, text in
               (("apply", "Apply plan"), ("run", "Run"), ("stop", "Stop"))}
    widget = QuickCommandWidget(PanelCommandAdapter("Dual Gate", buttons))
    widget.resize(1900, 48)
    widget.show()
    QTest.qWait(30)
    root = widget.rootObject()
    items = [find_item(root, "command_" + key) for key in buttons]
    try:
        rects = [item.mapRectToScene(item.boundingRect()) for item in items]
        assert len({rect.top() for rect in rects}) == 1
        assert rects[0].left() < rects[1].left() < rects[2].left()
        assert abs(rects[-1].right() - (root.width() - root.property("rightPadding"))) < 1

        items[0].forceActiveFocus(Qt.FocusReason.TabFocusReason)
        QTest.keyClick(widget, Qt.Key.Key_Tab)
        assert items[1].property("activeFocus")
        widget.resize(220, widget.height())
        QTest.qWait(40)
        assert items[1].property("activeFocus")
        assert [find_item(root, "command_" + key) for key in buttons] == items
        rects = [item.mapRectToScene(item.boundingRect()) for item in items]
        assert rects[-1].top() > rects[0].top()
        assert sorted(rects, key=lambda rect: (rect.top(), rect.left())) == rects
        for rect in rects:
            assert rect.left() >= root.property("leftPadding")
            assert rect.right() <= root.width() - root.property("rightPadding") + 1
            assert rect.bottom() <= root.height()
        for row_y in {rect.top() for rect in rects}:
            right = max(rect.right() for rect in rects if rect.top() == row_y)
            assert abs(right - (root.width() - root.property("rightPadding"))) < 1

        QTest.keyClick(widget, Qt.Key.Key_Tab)
        assert items[2].property("activeFocus")
        clicked = QSignalSpy(buttons["stop"].clicked)
        QTest.keyClick(widget, Qt.Key.Key_Space)
        assert clicked.count() == 1
        buttons["run"].setEnabled(False)
        widget.adapter.refresh()
        assert not items[1].property("enabled")
        assert not widget.adapter.presenter.invoke("run")
        assert items[2].property("enabled")
    finally:
        widget.shutdown()
        widget.close()
        widget.deleteLater()
        for button in buttons.values():
            button.deleteLater()
        drain_qt_deletions()


@pytest.mark.parametrize("widths", [(100.125, 75.125), (100.375, 75.5)])
def test_fractional_action_widths_fit_one_row_until_viewport_requires_wrapping(app, widths):
    buttons = {key: QPushButton(text) for key, text in (("run", "Run Sweep"), ("stop", "Stop"))}
    widget = QuickCommandWidget(PanelCommandAdapter("2D Sweep", buttons))
    widget.resize(1400, widget.height())
    widget.show()
    QTest.qWait(30)
    root = widget.rootObject()
    items = [find_item(root, "command_" + key) for key in buttons]
    try:
        # Fractional font metrics occur at 125% scale. Exercise those widths
        # directly so this regression remains deterministic on every screen.
        for item, width in zip(items, widths):
            item.setImplicitWidth(width)
        QTest.qWait(40)
        area = find_item(root, "CommandActionArea")
        assert not root.property("fullStatus") and not root.property("stacked")
        assert area.width() >= sum(widths) + 8
        assert items[0].y() == items[1].y()
        assert items[0].x() + items[0].width() + 8 <= items[1].x()
        assert abs(items[1].x() + items[1].width() - area.width()) < .01

        items[1].forceActiveFocus(Qt.FocusReason.TabFocusReason)
        widget.resize(160, widget.height())
        # QML wraps through callLater, then the QWidget applies its new height
        # through a zero-delay timer. Wait for both stages instead of reading
        # a transient clipped geometry after a fixed number of milliseconds.
        for _ in range(50):
            if (items[1].y() > items[0].y()
                    and all(item.mapRectToScene(item.boundingRect()).bottom() <= widget.height()
                            for item in items)):
                break
            QTest.qWait(10)
        assert items[1].y() > items[0].y()
        assert items[1].property("activeFocus")
        for item in items:
            rect = item.mapRectToScene(item.boundingRect())
            assert 0 <= rect.left() < rect.right() <= root.width()
            assert rect.bottom() <= widget.height()
        clicked = QSignalSpy(buttons["stop"].clicked)
        QTest.keyClick(widget, Qt.Key.Key_Space)
        assert clicked.count() == 1
    finally:
        widget.shutdown()
        widget.close()
        widget.deleteLater()
        for button in buttons.values():
            button.deleteLater()
        drain_qt_deletions()


@pytest.mark.parametrize("width,compact", [(480, False), (350, True)])
def test_run_hover_hint_does_not_cover_or_consume_first_click(app, width, compact):
    button = QPushButton("Run")
    button.setToolTip("Start the full validated scan using the current settings. "
                      "This long help text must remain readable outside the short command bar.")
    widget = QuickCommandWidget(PanelCommandAdapter("Dual Gate", {"run": button}))
    root = widget.rootObject()
    root.setProperty("compact", compact)
    widget.resize(width, widget.height())
    widget.show()
    QTest.qWait(40)
    run = find_item(root, "command_run")
    center = run.mapToScene(run.boundingRect().center()).toPoint()
    clicked = QSignalSpy(button.clicked)
    warnings = []
    def capture_warnings(messages):
        warnings.extend(message.toString() for message in messages)
    widget.engine().warnings.connect(capture_warnings)
    hints = lambda: [window for window in app.topLevelWindows()
                     if window.objectName() == "AppToolTipWindow"]
    try:
        QTest.mouseMove(widget, QPoint(0, 0))
        run.forceActiveFocus(Qt.FocusReason.TabFocusReason)
        assert hints() == []  # Idle buttons allocate no native tooltip window.
        QTest.mouseMove(widget, center)
        QTest.qWait(100)
        assert hints() == []  # Hover hints have a deliberate delay.
        QTest.qWait(560)
        assert len(hints()) == 1
        hint = hints()[0]
        assert hint.isVisible()
        assert hint.flags() & Qt.WindowType.WindowTransparentForInput
        assert hint.flags() & Qt.WindowType.WindowDoesNotAcceptFocus
        assert not hint.isActive()
        assert run.property("activeFocus")
        button_rect = run.mapRectToScene(run.boundingRect()).toRect()
        button_rect.moveTopLeft(widget.mapToGlobal(button_rect.topLeft()))
        assert not hint.geometry().intersects(button_rect)

        # Before the fix, the clamped Popup.Item covered Run and this first
        # click was consumed by the tooltip instead of dispatching the command.
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=center)
        assert clicked.count() == 1
        QTest.qWait(10)
        assert hints() == []

        button.setEnabled(False)
        widget.adapter.refresh()
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=center)
        assert clicked.count() == 1
        assert not widget.adapter.presenter.invoke("run")
        assert warnings == []
    finally:
        widget.engine().warnings.disconnect(capture_warnings)
        widget.shutdown()
        widget.close()
        widget.deleteLater()
        button.deleteLater()
        drain_qt_deletions()
    assert hints() == []


def test_no_op_publish_preserves_items_and_emits_no_model_reset(command_fields):
    _sources, widget, _warnings = command_fields
    bridge = widget.adapter.presenter
    fields, actions = list(bridge.fields), list(bridge.actions)
    changed = []
    bridge.fieldsChanged.connect(lambda: changed.append("fields"))
    bridge.actionsChanged.connect(lambda: changed.append("actions"))
    for _ in range(10):
        widget.adapter.refresh()
    assert fields == bridge.fields and actions == bridge.actions
    assert not changed


def test_destroyed_spectrum_page_cancels_queued_optics_refresh(app):
    """A replacement page must not leave a callback into its old controller."""
    from shiboken6 import delete

    controller = PreviewSpectrumController()
    calls = []
    controller.lightfield_optics = lambda: calls.append("refresh")
    panel = SpectrumPanel(controller)
    controller.set_connected(True)
    # Delete before processing the zero-delay connection readback. Keeping the
    # controller alive lets the assertion detect a stale call without crashing.
    delete(panel)
    app.processEvents()
    assert calls == []
    controller.deleteLater()
    drain_qt_deletions()


def test_invalid_draft_stays_visible_after_focus_moves_until_escape(command_fields):
    sources, widget, warnings = command_fields
    exposure = find_item(widget.rootObject(), "field_exposure")
    frames = find_item(widget.rootObject(), "field_frames")
    original = sources.exposure.value()
    click = lambda field: QTest.mouseClick(
        widget, Qt.MouseButton.LeftButton,
        pos=field.mapToScene(field.boundingRect().center()).toPoint())
    click(exposure)
    QTest.keyClick(widget, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(widget, "-5")
    click(frames)
    widget.adapter.refresh()
    assert exposure.property("text") == "-5"
    assert sources.exposure.value() == original
    assert widget.adapter.presenter.invalidFields == ["exposure"]
    assert not widget.adapter.presenter.invoke("run")
    click(exposure)
    assert exposure.property("text") == "-5"
    assert sources.exposure.value() == original
    QTest.keyClick(widget, Qt.Key.Key_Escape)
    assert not widget.adapter.presenter.validationError
    assert float(exposure.property("text")) == original
    assert sources.exposure.value() == original
    assert warnings == []


def test_pause_keeps_native_check_state_and_keyboard_path_despite_invalid_draft(app):
    pause, run = QPushButton("PAUSE MAGNET"), QPushButton("Start MCD")
    pause.setCheckable(True)
    frames = QSpinBox()
    frames.setRange(1, 10)
    widget = QuickCommandWidget(PanelCommandAdapter("MCD 1000", {"run": run, "pause": pause},
                                                  fields={"frames": (frames, "Frames")}))
    widget.resize(1000, widget.height())
    widget.show()
    QTest.qWait(40)
    clicked = QSignalSpy(pause.clicked)
    try:
        assert not widget.adapter.presenter.editField("frames", "invalid")
        assert not widget.adapter.presenter.invoke("run")
        target = find_item(widget.rootObject(), "command_pause")
        assert target.property("enabled")
        target.forceActiveFocus(Qt.FocusReason.TabFocusReason)
        QTest.keyClick(widget, Qt.Key.Key_Space)
        assert clicked.count() == 1 and pause.isChecked()
        # Recheck the real source at dispatch even before the snapshot updates.
        pause.setEnabled(False)
        widget.adapter.presenter.invoke("pause")
        assert clicked.count() == 1 and pause.isChecked()
        widget.adapter.refresh()
        assert not widget.adapter.presenter.invoke("pause")
    finally:
        widget.shutdown()
        widget.close()
        widget.deleteLater()
        for source in (pause, run, frames):
            source.deleteLater()
        drain_qt_deletions()


def test_workflow_footer_is_replaced_and_hidden_status_stays_readable(app):
    panel = QWidget()
    layout = QVBoxLayout(panel)
    footer = QGroupBox("Run control", panel)
    source_layout = QVBoxLayout(footer)
    status = QLabel("MCD ready — magnet in driven mode (heater ON).")
    buttons = {key: QPushButton(text) for key, text in
               (("run", "Start MCD 1000"), ("stop", "Stop / Cancel"), ("pause", "PAUSE MAGNET"))}
    source_layout.addWidget(status)
    for button in buttons.values():
        source_layout.addWidget(button)
    layout.addWidget(footer)
    widget = install_workflow_controls(panel, "MCD 1000", buttons, status=status, legacy_footer=footer)
    warnings = []
    widget.engine().warnings.connect(lambda messages: warnings.extend(message.toString() for message in messages))
    panel.show()
    try:
        assert footer.isHidden() and layout.indexOf(footer) == -1
        assert widget.adapter.status is status
        assert all(widget.adapter.buttons[key] is source for key, source in buttons.items())
        assert install_workflow_controls(panel, "MCD 1000", buttons) is widget
        status.setText("MCD sweep requires driven mode — persistent heater is OFF. Use Enter Driven Mode first.")
        for width in (1300, 1024, 420):
            panel.resize(width, 250)
            QTest.qWait(250)  # Display-only poll also reads a hidden QLabel.
            label = find_item(widget.rootObject(), "CommandStatus")
            assert label.property("text") == status.text()
            assert label.property("truncated") is False
            rect = label.mapRectToScene(label.boundingRect())
            assert rect.bottom() <= widget.height()
            assert rect.right() <= widget.width()
            pause = find_item(widget.rootObject(), "command_pause")
            assert pause.mapRectToScene(pause.boundingRect()).bottom() <= widget.height()
            assert widget.height() < 180
        assert warnings == []
    finally:
        widget.shutdown()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()
