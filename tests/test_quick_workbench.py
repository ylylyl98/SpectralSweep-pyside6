"""Real QML shell with one real Spectrum view and entirely simulated executors.

No MainWindow, production launcher, SDK or device controller is constructed.
History and output paths are temporary; any config save fails the test.
"""
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtCore import Qt
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QComboBox, QGroupBox, QLabel, QLineEdit, QMainWindow, QPushButton, QStatusBar, QTabWidget,
    QSplitter, QVBoxLayout, QWidget,
)

from app.experiment_metadata import ExperimentHistory
from ui.quick_workbench import QuickWorkbench
from ui.spectrum_panel import SpectrumPanel
from ui.spectrum_preview import PreviewSpectrumController
from utils.config import cfg
from tests.quick_helpers import quick_application, drain_qt_deletions


@pytest.fixture(scope="module")
def app():
    application = quick_application()
    font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
    if not QFontDatabase.families() and font.exists():
        QFontDatabase.addApplicationFont(str(font))
        application.setFont(QFont("Segoe UI", 9))
    return application


class FakeThread:
    def __init__(self, running=True):
        self.running = running

    def isRunning(self):
        return self.running


class ControlledPreview(PreviewSpectrumController):
    """Preview acquisition clock is manually advanced, including native drain."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings_pending = False
        self.acquisition_in_progress = False
        self.hold_cleanup = False
        self.shutdown_calls = 0

    def apply_settings(self, exposure_ms, center_nm, accumulations):
        self._settings_pending = True
        super().apply_settings(exposure_ms, center_nm, accumulations)
        self._timer.setInterval(60_000)

    def acquire_single(self):
        self.acquisition_in_progress = True
        super().acquire_single()
        self._timer.setInterval(60_000)

    def acquire_2d(self):
        self.acquisition_in_progress = True
        super().acquire_2d()
        self._timer.setInterval(60_000)

    def _complete(self):
        self._settings_pending = False
        if self._pending != "settings":
            self.acquisition_in_progress = False
        super()._complete()

    def abort_acquisition(self):
        if not self.hold_cleanup:
            self.acquisition_in_progress = False
        return super().abort_acquisition()

    def shutdown(self):
        self.shutdown_calls += 1


def workflow_panel(kind):
    panel = QWidget()
    layout = QVBoxLayout(panel)
    panel.starts = 0
    panel.stops = 0
    panel.loaded_settings = []
    panel.metadata_finalizing = False
    panel._interlock_held = False
    panel._thread = None
    panel.thread = None

    def button(text):
        control = QPushButton(text, panel)
        layout.addWidget(control)
        return control

    panel._run_btn = button("Run")
    panel._stop_btn = button("Stop")
    panel._apply_btn = button("Apply")
    panel._acquire_btn = button("Acquire")
    panel.start_btn, panel.stop_btn = panel._run_btn, panel._stop_btn
    panel._stop_btn.setEnabled(False)
    panel.pauses = panel.prepares = 0
    if kind in {"mcd", "mcd2100"}:
        panel._run_footer = QGroupBox("Run control", panel)
        footer = QVBoxLayout(panel._run_footer)
        layout.addWidget(panel._run_footer)
        for control in (panel._run_btn, panel._stop_btn):
            layout.removeWidget(control)
            footer.addWidget(control)
        if kind == "mcd":
            panel._mode_notice = QLabel("MCD ready — magnet in driven mode (heater ON).")
            footer.addWidget(panel._mode_notice)
            panel._pause_btn = QPushButton("PAUSE MAGNET")
            footer.addWidget(panel._pause_btn)
            panel._pause_btn.clicked.connect(lambda: setattr(panel, "pauses", panel.pauses + 1))
        else:
            panel.prepare_magnet_btn = QPushButton("Prepare magnet")
            footer.addWidget(panel.prepare_magnet_btn)
            panel.status = QLabel("Ready", panel)
            layout.addWidget(panel.status)
            def prepare():
                panel.prepares += 1
                panel.thread = FakeThread()
                panel._interlock_held = True
            panel.prepare_magnet_btn.clicked.connect(prepare)

    def start():
        panel.starts += 1
        if kind == "mcd2100":
            panel.thread = FakeThread()
            panel._interlock_held = True
        else:
            panel._thread = FakeThread()
        panel._run_btn.setEnabled(False)
        panel._acquire_btn.setEnabled(False)
        panel._stop_btn.setEnabled(True)

    def stop():
        panel.stops += 1
        panel._stop_btn.setEnabled(False)
        # Cancellation is deliberately NOT a drain acknowledgement.

    def load(settings, *, keys_only=False):
        if keys_only:
            return {"supported_keys": ["exposure_ms"]}
        panel.loaded_settings.append(dict(settings))
        return {"skipped": []}

    panel._run_btn.clicked.connect(start)
    panel._acquire_btn.clicked.connect(start)
    panel._stop_btn.clicked.connect(stop)
    panel.apply_saved_experiment_settings = load
    return panel


@pytest.fixture
def workbench(app, tmp_path, monkeypatch, request):
    def forbidden_save(*_args, **_kwargs):
        raise AssertionError("The Quick workbench test must not write production config")

    monkeypatch.setattr(cfg, "save", forbidden_save)
    monkeypatch.setattr(cfg.filename, "base_out", str(tmp_path / "output"))
    monkeypatch.setattr(cfg.session, "sample_id", "test-sample")
    monkeypatch.setattr("ui.detector_wavelength_advice.confirm_detector_wavelength", lambda *_a, **_k: True)
    window = QMainWindow()
    window._tabs = QTabWidget(window)
    window.setCentralWidget(window._tabs)
    window._status = QStatusBar(window)
    window.setStatusBar(window._status)
    window._status.showMessage("Ready")
    window._sample_id_binder = SimpleNamespace(value="test-sample")
    window._power_sweep_running = False
    window._active_mcd_panel = None
    window._lf6 = ControlledPreview(window)
    window._spectrum = SpectrumPanel(window._lf6)
    window._presets = workflow_panel("dual_gate")
    window._mega = workflow_panel("mega_sweep")
    window._power_sweep = workflow_panel("power_sweep")
    window._mcd = workflow_panel("mcd")
    window._mcd2100 = workflow_panel("mcd2100")
    window._bfp = workflow_panel("bfp")
    window._settings, window._calibration = QWidget(), QWidget()
    window._tab_ids = {}
    titles = dict(dual_gate="Dual Gate", mega_sweep="2D Sweep", power_sweep="Motion Sweep",
                  mcd="MCD 1000", mcd2100="MCD 2100", bfp="BFP", spectrum="Spectrum",
                  settings="Settings", calibration="Calibration")
    for panel, page_id in (
        (window._presets, "dual_gate"), (window._mega, "mega_sweep"),
        (window._power_sweep, "power_sweep"), (window._mcd, "mcd"),
        (window._mcd2100, "mcd2100"), (window._bfp, "bfp"),
        (window._spectrum, "spectrum"), (window._settings, "settings"),
        (window._calibration, "calibration"),
    ):
        window._tabs.addTab(panel, titles[page_id])
        window._tab_ids[panel] = page_id
    window._tabs.setTabVisible(window._tabs.indexOf(window._calibration), False)
    window._active_tab_id = lambda: window._tab_ids[window._tabs.currentWidget()]
    window._acquisition_active = lambda: False  # runtime must independently guard history
    window._history_adapters = lambda: {"dual_gate_sweep": "apply_saved_experiment_settings"}
    window._history_type = QComboBox(window)
    window._history_type.addItem("dual_gate_sweep")
    window._history = ExperimentHistory(tmp_path / "history.sqlite")
    window.sidebar_toggles, window.sample_saves = 0, 0

    def toggle():
        window.sidebar_toggles += 1

    def save_sample():
        window.sample_saves += 1

    window._toggle_sidebar, window._save_current_sample = toggle, save_sample
    if hasattr(request, "param"):
        window._sidebar_width = request.param
        tabs = window.takeCentralWidget()
        window._sidebar_splitter = QSplitter(Qt.Orientation.Horizontal)
        sidebar = QWidget()
        sidebar.setMinimumWidth(300)
        window._sidebar_splitter.addWidget(sidebar)
        window._sidebar_splitter.addWidget(tabs)
        window._sidebar_splitter.setCollapsible(1, False)
        window.setCentralWidget(window._sidebar_splitter)
    shell = QuickWorkbench(window)
    window.resize(1300, 850)
    window.show()
    window._lf6.set_connected(True)
    shell.refresh()
    app.processEvents()
    yield shell, window
    shell.shutdown()
    if shell._history_dialog is not None:
        shell._history_dialog.close()
    window._lf6.hold_cleanup = False
    window._lf6.abort_acquisition()
    app.processEvents()
    window.close()
    window.deleteLater()
    drain_qt_deletions()


@pytest.mark.parametrize("workbench", [300, 330], indirect=True)
def test_legacy_sidebar_default_grows_with_window_until_dragged(workbench):
    shell, window = workbench
    splitter = window._sidebar_splitter
    sidebar = splitter.widget(0)
    for width, expected in ((1400, 350), (1920, 460), (1400, 350)):
        window.resize(width, 850)
        QTest.qWait(30)
        shell._fit_workspace()
        assert splitter.sizes()[0] == expected
        assert window._sidebar_width == expected

    window.resize(1920, 850)
    QTest.qWait(30)
    shell._fit_workspace()
    # Startup/layout restoration can still replace the splitter sizes after
    # the first fit. Keep the automatic default until an actual handle drag.
    splitter.setSizes([300, splitter.width() - 300])
    shell.refresh()
    assert splitter.sizes()[0] == 460
    splitter.setSizes([0, splitter.width()])
    shell.refresh()
    assert splitter.sizes()[0] == 0
    splitter.setSizes([window._sidebar_width, splitter.width() - window._sidebar_width])
    shell.refresh()
    assert splitter.sizes()[0] == 460

    splitter.moveSplitter(386, 1)
    manual_width = splitter.sizes()[0]
    assert shell._auto_sidebar_width is False
    for width in (1400, 1920, 1000):
        window.resize(width, 850)
        QTest.qWait(30)
        shell._fit_workspace()
        if width >= 1200:
            assert splitter.sizes()[0] == manual_width
    assert sidebar.isHidden()
    assert window._tabs.isVisible()
    shell.toggleSidebar()
    assert sidebar.isVisible()
    assert window._tabs.isHidden()
    shell.toggleSidebar()
    assert sidebar.isHidden()
    assert window._tabs.isVisible()
    window.resize(1920, 850)
    QTest.qWait(30)
    shell._fit_workspace()
    assert splitter.sizes()[0] == manual_width


@pytest.mark.parametrize("workbench", [412], indirect=True)
def test_custom_saved_sidebar_width_is_preserved(workbench):
    shell, window = workbench
    for width in (1400, 1920):
        window.resize(width, 850)
        QTest.qWait(30)
        shell._fit_workspace()
        assert window._sidebar_splitter.sizes()[0] == 412


def test_header_commands_stay_right_aligned_above_scrolling_workspace(workbench):
    from PySide6.QtCore import QPoint
    shell, window = workbench
    tall_content = QWidget(window._presets)
    tall_content.setFixedHeight(1500)
    window._presets.layout().addWidget(tall_content)
    tall_content.show()
    window._presets.layout().activate()
    shell.refresh()
    QTest.qWait(40)
    footer = shell.command_stack
    top = footer.mapTo(window, QPoint()).y()
    assert top + footer.height() <= shell.body_scroll.mapTo(window, QPoint()).y()
    assert footer.mapTo(window, QPoint()).x() >= window.width() - footer.width() - 8
    assert footer.width() < window.width() / 2
    assert footer.isVisibleTo(window)
    assert top + footer.height() <= window.height()
    scroll = shell.body_scroll.verticalScrollBar()
    assert scroll.maximum() > 0
    scroll.setValue(scroll.maximum())
    QTest.qWait(20)
    assert footer.mapTo(window, QPoint()).y() == top
    assert footer.isVisibleTo(window)
    shell.selectPage("settings")
    QTest.qWait(20)
    assert not footer.isVisibleTo(window)


@pytest.mark.parametrize('page_id', ['mega_sweep', 'power_sweep', 'mcd', 'mcd2100', 'bfp'])
def test_commands_use_existing_monitoring_pane_without_footer_or_new_column(workbench, page_id):
    from PySide6.QtCore import QPoint, QPointF
    from tests.test_quick_parameter_forms import find_item

    shell, window = workbench
    panel = next(panel for panel, key in window._tab_ids.items() if key == page_id)
    splitter = QSplitter(Qt.Orientation.Horizontal, panel)
    settings = QWidget()
    settings.setMinimumWidth(300)
    splitter.addWidget(settings)
    if page_id == 'bfp':
        monitoring = QTabWidget()
        monitoring.addTab(QWidget(), 'Existing plot')
    else:
        monitoring = QWidget()
        QVBoxLayout(monitoring).addWidget(QLabel('Existing monitoring'))
    splitter.addWidget(monitoring)
    splitter.setSizes([400, 700])
    panel.layout().addWidget(splitter, 1)
    panel._splitter = splitter
    if page_id == 'mega_sweep':
        panel._right_panel = monitoring
    shell.selectPage(page_id)
    window.resize(1920, 1080)
    for _ in range(3):
        shell.refresh()
        QTest.qWait(30)
    command = shell._commands[page_id]
    pane = splitter.widget(1)
    assert splitter.count() == 2
    assert pane is monitoring or pane.isAncestorOf(monitoring)
    assert monitoring.isVisibleTo(window)
    assert command.parentWidget() is pane
    assert pane.layout().indexOf(command) == pane.layout().count() - 1
    assert command.rootObject().property('compact')
    assert command.rootObject().property('embedded')
    assert not shell.command_header.isVisibleTo(window)
    assert not shell.command_stack.isVisibleTo(window)
    assert command.isVisibleTo(window)
    assert command.width() <= pane.width()
    assert command.x() + command.width() >= pane.width() - 12
    assert command.y() + command.height() >= pane.height() - 12
    assert command.mapTo(window, QPoint()).x() > settings.mapTo(window, QPoint()).x() + settings.width()
    assert command.height() < 170
    assert shell.body_scroll.horizontalScrollBar().maximum() == 0
    for key in command.adapter.buttons:
        button = find_item(command.rootObject(), 'command_' + key)
        point = button.mapToScene(QPointF())
        assert 0 <= point.x() <= command.width() - button.width() + 1
        assert 0 <= point.y() <= command.height() - button.height() + 1
    action_y = [find_item(command.rootObject(), 'command_' + key).y()
                for key in command.adapter.buttons]
    assert max(action_y) - min(action_y) <= 1
    assert command.width() >= command.sizeHint().width() - 1
    assert command.height() <= 100
    # Late responsive content must appear before the fixed lower action area.
    late = QLabel('Optional monitoring detail', pane)
    pane.layout().addWidget(late)
    shell.refresh()
    assert pane.layout().indexOf(command) == pane.layout().count() - 1


def test_spectrum_actions_dock_below_right_tools_and_survive_reference_sidebar_hide(workbench, app, request):
    from tests.test_quick_parameter_forms import find_item
    shell, window = workbench
    previous = app.property('spectralQuick')
    app.setProperty('spectralQuick', True)
    request.addfinalizer(lambda: app.setProperty('spectralQuick', previous))
    shell.selectPage('spectrum')
    window.resize(1920, 1080)
    QTest.qWait(60)
    shell.refresh()
    command = shell._commands['spectrum']
    panel = window._spectrum
    pane = panel._content_splitter.widget(1)
    assert command.parentWidget() is pane
    assert pane.isAncestorOf(panel._side_panel)
    assert panel._side_panel.isVisibleTo(window)
    assert panel._content_splitter.count() == 2
    assert pane.layout().indexOf(command) == pane.layout().count() - 1
    assert not shell.command_header.isVisibleTo(window)
    assert command.rootObject().property('embedded')
    assert command.rootObject().property('compact')
    assert command.width() < window.width() * .75
    assert command.height() < 150
    assert command.y() >= panel._side_panel.y() + panel._side_panel.height()
    assert command.y() + command.height() >= pane.height() - 4
    panel._references_toggle.click()
    QTest.qWait(20)
    shell.refresh()
    assert command.isVisibleTo(window)
    assert command.y() + command.height() >= pane.height() - 4
    assert find_item(command.rootObject(), 'command_stop').isVisible()
    assert not shell.command_header.isVisibleTo(window)


def test_offscreen_lower_action_area_uses_header_only_for_active_stop(workbench):
    shell, window = workbench
    panel = window._mega
    pane = QWidget(panel)
    pane.setFixedHeight(1600)
    QVBoxLayout(pane).addWidget(QLabel('Tall monitoring'))
    panel.layout().addWidget(pane)
    panel._right_panel = pane
    shell.selectPage('mega_sweep')
    QTest.qWait(30)
    shell.refresh()
    command = shell._commands['mega_sweep']
    assert command.parentWidget() is pane
    assert not shell.command_header.isVisibleTo(window)
    panel._stop_btn.setEnabled(True)
    shell.refresh()
    assert shell.command_header.isVisibleTo(window)
    assert command.parentWidget() is shell.command_stack
    assert command.isVisibleTo(window)
    panel._stop_btn.setEnabled(False)
    shell.refresh()
    assert command.parentWidget() is pane
    assert not shell.command_header.isVisibleTo(window)


def test_running_stop_dock_is_stable_through_repeated_refresh_and_outer_scroll(workbench):
    from PySide6.QtCore import QPoint
    shell, window = workbench
    panel = window._mega
    pane = QWidget(panel)
    content = QWidget(pane)
    QVBoxLayout(pane).addWidget(content, 1)
    panel.layout().addWidget(pane, 1)
    panel._right_panel = pane
    panel._thread = FakeThread()
    panel._stop_btn.setEnabled(True)
    shell.selectPage('mega_sweep')
    window.resize(900, 700)
    command = shell._commands['mega_sweep']
    for minimum in (380, 480, 640):
        content.setMinimumHeight(minimum)
        QTest.qWait(30)
        for _ in range(3):
            shell.refresh()
            QTest.qWait(10)
        scroll = shell.body_scroll.verticalScrollBar()
        for position in (0, scroll.maximum()):
            scroll.setValue(position)
            QTest.qWait(20)
            parents = []
            for _ in range(10):
                shell.refresh()
                QTest.qWait(10)
                parents.append(command.parentWidget())
                assert command.isVisibleTo(window)
                if command.parentWidget() is pane:
                    top = command.mapTo(shell.body_scroll.viewport(), QPoint()).y()
                    assert top >= -4
                    assert top + command.height() <= shell.body_scroll.viewport().height() + 4
                else:
                    assert shell.command_header.isVisibleTo(window)
                    assert command.height() <= shell.command_header.height()
            assert all(parent is parents[0] for parent in parents)
    panel._thread = None
    panel._stop_btn.setEnabled(False)
    shell.refresh()
    assert command.parentWidget() is pane
    assert not shell.command_header.isVisibleTo(window)


def test_bfp_output_actions_move_with_original_widgets_and_quick_adapter(workbench, app, request):
    from PySide6.QtWidgets import QHBoxLayout

    shell, window = workbench
    panel = window._bfp
    previous = app.property('spectralQuick')
    app.setProperty('spectralQuick', True)
    request.addfinalizer(lambda: app.setProperty('spectralQuick', previous))
    panel._splitter = QSplitter(Qt.Orientation.Horizontal, panel)
    panel._splitter.addWidget(QWidget())
    tabs = QTabWidget()
    tabs.addTab(QWidget(), 'Original data')
    panel._splitter.addWidget(tabs)
    panel.layout().addWidget(panel._splitter, 1)
    footer = QHBoxLayout()
    panel.layout().removeWidget(panel._acquire_btn)
    footer.addWidget(panel._acquire_btn)
    panel.output_calls = []
    for name, text in (('_clear_btn', 'Clear'), ('_save_csv_btn', 'Save CSV'),
                       ('_save_png_btn', 'Save PNG')):
        source = QPushButton(text, panel)
        setattr(panel, name, source)
        source.clicked.connect(lambda checked=False, text=text: panel.output_calls.append(text))
        footer.addWidget(source)
    panel._status_lbl = QLabel('Original output status', panel)
    footer.addWidget(panel._status_lbl)
    panel.layout().addLayout(footer)
    sources = (panel._clear_btn, panel._save_csv_btn, panel._save_png_btn, panel._status_lbl)
    shell.selectPage('bfp')
    window.resize(1920, 1080)
    QTest.qWait(80)
    shell.refresh()
    pane = panel._splitter.widget(1)
    holder = panel._quick_output_actions
    command = shell._commands['bfp']
    assert holder.parentWidget() is pane
    assert panel._acquire_btn.isHidden()  # Its Quick command remains the sole Acquire action.
    assert panel.layout().indexOf(footer) == -1
    assert pane.isAncestorOf(tabs) and tabs.isVisibleTo(window)
    assert pane.layout().indexOf(holder) < pane.layout().indexOf(command)
    assert holder.y() + holder.height() <= command.y() + 2
    assert command.y() + command.height() >= pane.height() - 4
    assert not shell.command_header.isVisibleTo(window)
    forms = panel._quick_parameter_forms['forms']
    output_fields = [(form, field, source) for form in forms
                     for field, source in form.adapter._fields.items() if source in sources]
    assert {source for _, _, source in output_fields} == set(sources)
    assert all(holder.isAncestorOf(form) and form.isVisibleTo(window)
               for form, _, _ in output_fields)
    for form, field, source in output_fields:
        if isinstance(source, QPushButton):
            assert field.trigger()
    assert panel.output_calls == ['Clear', 'Save CSV', 'Save PNG']
    panel._save_csv_btn.setEnabled(False)
    form, field, _ = next(entry for entry in output_fields if entry[2] is panel._save_csv_btn)
    form.adapter.refresh()
    assert not field.trigger()


def test_navigation_resize_settles_the_complete_render_viewport(workbench):
    shell, window = workbench
    for width in (1920, 900, 1920):
        window.resize(width, 850)
        for _ in range(50):
            QTest.qWait(10)
            nav = shell.navigation
            size = nav.size()
            if (not shell._navigation_height_timer.isActive()
                    and nav.rootObject().height() == size.height()
                    and nav.quickWindow().size() == size
                    and nav.quickWindow().contentItem().height() == size.height()):
                break
        else:
            pytest.fail("Navigation widget and render viewport did not settle together")


def test_dual_gate_actions_dock_inside_right_pane_without_global_footer(workbench):
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtWidgets import QScrollArea
    from tests.test_quick_parameter_forms import find_item
    shell, window = workbench
    panel = window._presets
    pane = QWidget(panel)
    pane.setFixedWidth(380)
    pane_layout = QVBoxLayout(pane)
    pane_layout.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea(pane)
    scroll.setWidgetResizable(True)
    content = QWidget()
    content.setMinimumHeight(1600)
    scroll.setWidget(content)
    pane_layout.addWidget(scroll, 1)
    panel.layout().addWidget(pane, 1, Qt.AlignmentFlag.AlignRight)
    panel._results_pane = pane
    shell.refresh()
    QTest.qWait(60)
    command = shell._commands["dual_gate"]
    assert command.parentWidget() is pane
    assert command.rootObject().property("compact")
    assert command.height() <= 48
    assert not shell.command_stack.isVisibleTo(window)
    assert command.isVisibleTo(window)
    assert command.y() >= scroll.y() + scroll.height()
    assert command.y() + command.height() <= pane.height()
    before = command.mapTo(window, QPoint())
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
    QTest.qWait(10)
    assert command.mapTo(window, QPoint()) == before
    for name in ("apply", "run", "stop"):
        button = find_item(command.rootObject(), "command_" + name)
        position = button.mapToScene(QPointF())
        assert 0 <= position.x() <= command.width() - button.width()
    run = find_item(command.rootObject(), "command_run")
    QTest.mouseClick(command, Qt.MouseButton.LeftButton,
                     pos=run.mapToScene(run.boundingRect().center()).toPoint())
    assert panel.starts == 1
    command.adapter.refresh()
    stop = find_item(command.rootObject(), "command_stop")
    QTest.mouseClick(command, Qt.MouseButton.LeftButton,
                     pos=stop.mapToScene(stop.boundingRect().center()).toPoint())
    assert panel.stops == 1
    shell.selectPage("spectrum")
    QTest.qWait(20)
    assert not shell.command_stack.isVisibleTo(window)
    assert shell._commands['spectrum'].parentWidget() is window._spectrum._quick_command_pane
    assert not command.isVisibleTo(window)
    shell.selectPage("dual_gate")
    QTest.qWait(20)
    assert shell._commands["dual_gate"] is command
    assert command.isVisibleTo(window)
    assert not shell.command_stack.isVisibleTo(window)
    # Replacing a page must retire the old dock and mount its new presenter
    # in the new right pane, rather than reviving the global footer.
    replacement = workflow_panel("dual_gate")
    replacement._results_pane = QWidget(replacement)
    QVBoxLayout(replacement._results_pane).addWidget(QWidget())
    replacement.layout().addWidget(replacement._results_pane)
    index = window._tabs.indexOf(panel)
    window._tabs.blockSignals(True)
    window._tabs.removeTab(index)
    window._tabs.insertTab(index, replacement, "Dual Gate")
    window._tabs.setCurrentWidget(replacement)
    window._tabs.blockSignals(False)
    del window._tab_ids[panel]
    window._tab_ids[replacement] = "dual_gate"
    window._presets = replacement
    shell.rebind_page("dual_gate", panel, replacement)
    QTest.qWait(40)
    new_command = shell._commands["dual_gate"]
    assert command.adapter._closed
    assert new_command is not command
    assert new_command.parentWidget() is replacement._results_pane
    assert new_command.isVisibleTo(window)
    assert not shell.command_stack.isVisibleTo(window)
    panel.deleteLater()


def test_constructs_real_navigation_and_each_command_surface(workbench):
    shell, window = workbench
    assert shell.navigation.status() == QQuickWidget.Status.Ready
    assert shell.navigation.rootObject() is not None
    assert window._tabs.tabBar().isHidden()
    assert len(shell.pages) == 9
    assert shell.runtime.sampleId == "test-sample"
    assert shell.runtime.activePage == "dual_gate"
    assert set(shell._commands) == {"dual_gate", "mega_sweep", "power_sweep", "mcd", "mcd2100", "bfp", "spectrum"}
    assert all(widget.status() == QQuickWidget.Status.Ready for widget in shell._commands.values())
    shell.toggleSidebar()
    shell.saveSample()
    assert window.sidebar_toggles == window.sample_saves == 1
    screenshot = os.environ.get("SPECTRALSWEEP_QML_SCREENSHOT")
    if screenshot:
        shell.selectPage("spectrum")
        wavelength, counts = window._lf6.spectrum()
        window._spectrum._spec_plot.update_spectrum(wavelength, counts)
        QTest.qWait(100)
        destination = Path(screenshot)
        destination.parent.mkdir(parents=True, exist_ok=True)
        assert window.grab().save(str(destination))


def navigation_item(shell, predicate):
    def find(item):
        if predicate(item):
            return item
        for child in item.childItems():
            found = find(child)
            if found is not None:
                return found
    item = find(shell.navigation.rootObject())
    assert item is not None
    return item


@pytest.fixture
def sample_navigation(workbench, monkeypatch):
    """Exercise the real sample transaction with an in-memory profile store."""
    from app.sample_settings import SampleSettingsStore
    from ui.main_window import MainWindow, _SharedSampleIdBinder

    shell, window = workbench
    edits = [QLineEdit(window), QLineEdit(window)]
    for edit in edits:
        edit.hide()
    window._sample_id_binder = _SharedSampleIdBinder(
        edits, "test-sample", window, commit_on_edit=True)
    window._sample_store = SampleSettingsStore()
    window._sample_restore_in_progress = False
    window._sample_selector = QComboBox(window)
    window._sample_selector.setEditable(True)
    window._sample_selector.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    window._sample_selector.hide()
    window._sample_marker = "current draft"
    window._sample_store.save("saved-sample", {"panels": {"fake": "saved settings"}})
    calls, persisted = [], []

    def persist():
        sample = window._sample_id_binder.value
        persisted.append((sample, window._sample_marker))
        window._sample_store.save(sample, {"panels": {"fake": window._sample_marker}})

    def select(value):
        calls.append(("select", value))
        return MainWindow._commit_sample_selection(window, value)

    def save():
        calls.append(("save", window._sample_id_binder.value))
        MainWindow._save_current_sample(window)

    window._persist_session = persist
    window._commit_sample_selection = select
    window._sample_scoped_state = MainWindow._sample_scoped_state
    window._restore_state = lambda state: setattr(window, "_sample_marker", state["panels"]["fake"])
    window._refresh_sample_selector = lambda: MainWindow._refresh_sample_selector(window)
    window._refresh_history = lambda: None
    window._save_current_sample = save
    monkeypatch.setattr(window._spectrum, "restore_sample_references", lambda *_args: None)
    window._sample_selector.lineEdit().editingFinished.connect(
        lambda: MainWindow._commit_sample_id_edit(window, window._sample_selector.lineEdit()))
    window._sample_selector.activated.connect(
        lambda index: MainWindow._sample_selector_activated(window, index))
    window._refresh_sample_selector()
    shell.refresh()
    editor = navigation_item(shell, lambda item: item.objectName() == "WorkspaceSampleId")
    yield shell, window, editor, edits, calls, persisted


def type_sample(shell, editor, text):
    QTest.mouseClick(shell.navigation, Qt.MouseButton.LeftButton,
                     pos=editor.mapToScene(editor.boundingRect().center()).toPoint())
    QTest.keyClick(shell.navigation, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(shell.navigation, text)
    assert editor.property("text") == text


def test_sample_header_draft_survives_status_and_enter_commits_once(sample_navigation):
    shell, window, editor, edits, calls, persisted = sample_navigation
    type_sample(shell, editor, "  new sample  ")
    assert window._sample_selector.currentText() == "  new sample  "
    window._status.showMessage("Unrelated status update")
    shell.refresh()
    assert editor.property("text") == "  new sample  "
    assert window._sample_id_binder.value == "test-sample"
    assert [edit.text() for edit in edits] == ["test-sample"] * 2
    assert not calls and not persisted
    QTest.keyClick(shell.navigation, Qt.Key.Key_Return)
    assert calls == [("select", "  new sample  ")]
    assert persisted == [("test-sample", "current draft"), ("new sample", "current draft")]
    assert editor.property("text") == shell.sampleId == "new sample"
    assert window._sample_selector.currentText() == "new sample"
    assert [edit.text() for edit in edits] == ["new sample"] * 2
    QTest.keyClick(shell.navigation, Qt.Key.Key_Tab)
    assert len(calls) == 1


def test_sample_header_focus_loss_commits_before_save(sample_navigation):
    shell, window, editor, _, calls, _ = sample_navigation
    type_sample(shell, editor, "saved from header")
    save = navigation_item(shell, lambda item: item.property("text") == "Save sample")
    QTest.mouseClick(shell.navigation, Qt.MouseButton.LeftButton,
                     pos=save.mapToScene(save.boundingRect().center()).toPoint())
    assert calls == [("select", "saved from header"), ("save", "saved from header"),
                     ("select", "saved from header")]
    assert window._sample_store.load("saved from header")["panels"]["fake"] == "current draft"


def test_sample_header_escape_restores_without_committing(sample_navigation):
    shell, window, editor, _, calls, persisted = sample_navigation
    type_sample(shell, editor, "discard this draft")
    QTest.keyClick(shell.navigation, Qt.Key.Key_Escape)
    QTest.keyClick(shell.navigation, Qt.Key.Key_Tab)
    assert editor.property("text") == window._sample_id_binder.value == "test-sample"
    assert window._sample_selector.currentText() == "test-sample"
    assert not calls and not persisted


@pytest.mark.parametrize("rejection", ["empty", "acquisition"])
def test_sample_header_rejected_id_resets_to_committed_sample(sample_navigation, rejection):
    shell, window, editor, edits, calls, persisted = sample_navigation
    if rejection == "acquisition":
        window._acquisition_active = lambda: True
    type_sample(shell, editor, "   " if rejection == "empty" else "blocked sample")
    QTest.keyClick(shell.navigation, Qt.Key.Key_Return)
    assert len(calls) == 1
    assert not persisted
    assert editor.property("text") == shell.sampleId == window._sample_selector.currentText() == "test-sample"
    assert [edit.text() for edit in edits] == ["test-sample"] * 2
    assert ("cannot be empty" if rejection == "empty" else "during acquisition") in window._status.currentMessage()


def test_sample_header_tracks_existing_dropdown_and_source_updates(sample_navigation):
    shell, window, editor, edits, calls, persisted = sample_navigation
    selector = window._sample_selector
    index = selector.findText("saved-sample")
    assert index >= 0
    selector.setCurrentIndex(index)
    selector.activated.emit(index)
    shell.refresh()
    assert editor.property("text") == shell.sampleId == "saved-sample"
    assert window._sample_marker == "saved settings"
    assert [edit.text() for edit in edits] == ["saved-sample"] * 2
    assert persisted == [("test-sample", "current draft"), ("saved-sample", "saved settings")]
    window._sample_id_binder.commit("source update")
    shell.refresh()
    assert editor.property("text") == window._sample_selector.currentText() == "source update"
    assert calls == [("select", "saved-sample")]


@pytest.fixture
def shared_sample_editors(sample_navigation):
    """Render the sidebar's real combo adapter alongside the navigation input."""
    from ui.quick_parameter_forms import install_parameter_forms
    from tests.test_quick_parameter_forms import find_item

    shell, window, editor, edits, calls, persisted = sample_navigation
    sidebar = QWidget(window)
    sidebar._sample_selector = window._sample_selector
    layout = QVBoxLayout(sidebar)
    layout.addWidget(window._sample_selector)
    window._sample_selector.show()
    coverage = install_parameter_forms(sidebar, "instruments")
    view = next(form for form in coverage["forms"]
                if window._sample_selector in form.adapter._fields.values())
    sidebar.setGeometry(0, shell.navigation.height() + 10, 350, 100)
    sidebar.show()
    QTest.qWait(20)
    combo = find_item(view.rootObject(), "parameter_sample_selector")
    yield sample_navigation, view, combo
    for form in coverage["forms"]:
        form.shutdown()


def test_header_and_sidebar_render_the_same_uncommitted_draft(shared_sample_editors):
    (shell, window, editor, _, calls, persisted), view, combo = shared_sample_editors
    type_sample(shell, editor, "Header draft")
    QTest.qWait(10)
    assert combo.property("editText") == editor.property("text") == "Header draft"
    assert window._sample_id_binder.value == "test-sample"
    assert not calls and not persisted
    QTest.keyClick(shell.navigation, Qt.Key.Key_Escape)
    QTest.qWait(10)
    assert combo.property("editText") == editor.property("text") == "test-sample"
    assert not calls and not persisted


def test_sidebar_typing_updates_header_before_commit_and_can_continue_there(shared_sample_editors):
    (shell, window, editor, edits, calls, persisted), view, combo = shared_sample_editors
    input_item = combo.property("contentItem")
    QTest.mouseClick(view, Qt.MouseButton.LeftButton,
                     pos=input_item.mapToScene(input_item.boundingRect().center()).toPoint())
    QTest.keyClick(view, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(view, "Sidebar draft")
    assert editor.property("text") == combo.property("editText") == "Sidebar draft"
    assert window._sample_id_binder.value == "test-sample"
    assert not calls and not persisted
    window._status.showMessage("Unrelated status update")
    shell.refresh()
    assert editor.property("text") == combo.property("editText") == "Sidebar draft"
    # Moving to the other presentation commits once, then edits the same value.
    type_sample(shell, editor, "Continued in header")
    assert calls == [("select", "Sidebar draft")]
    shell.refresh()
    assert editor.property("text") == window._sample_selector.currentText() == "Continued in header"
    QTest.keyClick(shell.navigation, Qt.Key.Key_Return)
    QTest.keyClick(shell.navigation, Qt.Key.Key_Tab)
    QTest.qWait(10)
    assert calls == [("select", "Sidebar draft"), ("select", "Continued in header")]
    assert combo.property("editText") == editor.property("text") == "Continued in header"
    assert [edit.text() for edit in edits] == ["Continued in header"] * 2


def test_external_committed_sample_replaces_shared_draft(sample_navigation):
    shell, window, editor, edits, calls, persisted = sample_navigation
    type_sample(shell, editor, "Unfinished draft")
    window._sample_id_binder.commit("external sample")
    assert editor.property("text") == window._sample_selector.currentText() == "external sample"
    assert shell.sampleId == "external sample"
    shell.refresh()
    assert editor.property("text") == window._sample_selector.currentText() == "external sample"
    assert [edit.text() for edit in edits] == ["external sample"] * 2
    QTest.keyClick(shell.navigation, Qt.Key.Key_Tab)
    assert not calls and not persisted


def test_spectrum_apply_holds_resource_until_settings_acknowledgement(workbench):
    shell, window = workbench
    presenter = shell._commands["spectrum"].adapter.presenter
    shell._commands["spectrum"].adapter.refresh()
    assert presenter.invoke("apply")
    assert window._lf6._settings_pending
    assert window._spectrum._pending_acquisition is None
    assert shell.runtime.workflow("spectrum").pending
    assert not shell.runtime.workflow("dual_gate").canStart
    for _ in range(3):
        shell.refresh()
    assert shell.runtime.workflow("spectrum").pending
    window._lf6._timer.stop()
    window._lf6._complete()
    shell.refresh()
    assert not shell.runtime.workflow("spectrum").pending
    assert shell.runtime.workflow("dual_gate").canStart


def test_spectrum_stop_does_not_release_native_cleanup_ownership(workbench):
    shell, window = workbench
    widget = shell._commands["spectrum"]
    widget.adapter.refresh()
    assert widget.adapter.presenter.invoke("run")
    window._lf6._timer.stop()
    window._lf6._complete()  # settings acknowledgement begins acquisition
    assert window._lf6.acquisition_in_progress
    window._lf6.hold_cleanup = True
    widget.adapter.refresh()
    assert widget.adapter.presenter.invoke("stop")
    assert window._spectrum._continuous_mode is None
    assert window._lf6.acquisition_in_progress
    shell.refresh()
    state = shell.runtime.workflow("spectrum")
    assert state.pending and state.stopping and state.phase == "Stopping"
    assert not shell.runtime.workflow("power_sweep").canStart
    window._lf6.acquisition_in_progress = False
    shell.refresh()
    assert state.drained and not state.pending and not state.stopping


def test_mcd2100_true_thread_and_interlock_retain_ownership(workbench):
    shell, window = workbench
    panel = window._mcd2100
    panel.thread = FakeThread()
    shell.refresh()
    assert shell.runtime.workflow("mcd2100").busy
    assert not shell.runtime.workflow("spectrum").canStart
    panel.thread.running = False
    panel._interlock_held = True
    shell.refresh()
    assert shell.runtime.workflow("mcd2100").pending
    panel._interlock_held = False
    shell.refresh()
    assert not shell.runtime.workflow("mcd2100").pending


def test_mcd_unified_commands_keep_safety_available_and_native_footer_removed(workbench, monkeypatch):
    shell, window = workbench
    panel = window._mcd
    shell.selectPage("mcd")
    command = shell._commands["mcd"]
    assert panel._run_footer.isHidden()
    assert panel.layout().indexOf(panel._run_footer) == -1
    assert set(command.adapter.buttons) == {"run", "stop", "pause"}
    assert command.adapter.buttons["pause"] is panel._pause_btn
    assert command.adapter.status is panel._mode_notice
    command.adapter.refresh()
    assert command.adapter.presenter.invoke("run")
    assert panel.starts == 1
    # A Pause remains a native magnet action, independent of parameter edits
    # and the runtime's separate acquisition Stop state.
    monkeypatch.setattr(shell, "_parameters_valid", lambda *_a, **_k: False)
    command.adapter.refresh()
    assert command.adapter.presenter.invoke("pause")
    assert panel.pauses == 1 and panel.stops == 0
    assert not shell.runtime.workflow("mcd").stopping
    assert command.adapter.presenter.invoke("stop")
    assert panel.stops == 1 and shell.runtime.workflow("mcd").stopping
    panel._pause_btn.setEnabled(False)
    command.adapter.refresh()
    assert not command.adapter.presenter.invoke("pause")


def test_prepare_magnet_commits_parameters_and_reserves_shared_instruments(workbench, monkeypatch):
    shell, window = workbench
    panel = window._mcd2100
    adapter = shell._commands["mcd2100"].adapter
    assert panel._run_footer.isHidden()
    assert panel.layout().indexOf(panel._run_footer) == -1
    assert adapter.buttons["prepare"] is panel.prepare_magnet_btn
    assert adapter.status is panel.status
    shell.selectPage("mcd2100")
    monkeypatch.setattr(shell, "_parameters_valid", lambda *_a, **_k: False)
    adapter.refresh()
    assert not adapter.presenter.invoke("prepare")
    assert panel.prepares == 0
    # A stale enabled snapshot is also rejected by fresh dispatch validation.
    monkeypatch.setattr(shell, "_parameters_valid", lambda *_a, **_k: True)
    adapter.refresh()
    monkeypatch.setattr(shell, "_parameters_valid", lambda *_a, **_k: False)
    adapter.presenter.invoke("prepare")
    assert panel.prepares == 0
    monkeypatch.setattr(shell, "_parameters_valid", lambda *_a, **_k: True)
    committed = []
    monkeypatch.setattr("ui.quick_editing.commit_quick_edits", lambda source: committed.append(source) or True)
    adapter.refresh()
    assert adapter.presenter.invoke("prepare")
    assert committed == [panel] and panel.prepares == 1
    assert shell.runtime.workflow("mcd2100").pending
    assert not shell._command_available("mega_sweep", "run")
    adapter.refresh()
    assert not adapter.presenter.invoke("prepare")
    # Keep the real button's lock authoritative even before its snapshot refresh.
    panel.thread.running = False
    panel._interlock_held = False
    shell.refresh()
    adapter.refresh()
    panel.prepare_magnet_btn.setEnabled(False)
    adapter.presenter.invoke("prepare")
    assert panel.prepares == 1


@pytest.mark.parametrize("workbench", [300], indirect=True)
def test_mcd_safety_strip_remains_visible_during_narrow_instrument_focus(workbench):
    shell, window = workbench
    window.resize(900, 760)
    shell.selectPage("mcd")
    QTest.qWait(30)
    shell.toggleSidebar()
    shell.refresh()
    assert shell._instrument_focus
    assert shell.command_stack.isVisibleTo(window)
    assert shell._commands["mcd"].isVisibleTo(window)


def test_qml_command_guard_rechecks_button_and_excludes_concurrent_run(workbench):
    shell, window = workbench
    dual = shell._commands["dual_gate"].adapter
    mega = shell._commands["mega_sweep"].adapter
    dual.refresh()
    # Presenter still has enabled=True, but the actual widget has become locked.
    window._presets._run_btn.setEnabled(False)
    dual.presenter.invoke("run")
    assert window._presets.starts == 0
    window._presets._run_btn.setEnabled(True)
    dual.refresh()
    assert dual.presenter.invoke("run")
    assert window._presets.starts == 1
    mega.refresh()
    assert not mega.presenter.invoke("run")
    assert window._mega.starts == 0
    assert not shell._before_command("mega_sweep", "run")
    assert "Shared instruments" in window._status.currentMessage()


def test_disabled_and_hidden_navigation_cannot_be_bypassed(workbench):
    shell, window = workbench
    start = window._tabs.currentWidget()
    spectrum_index = window._tabs.indexOf(window._spectrum)
    window._tabs.setTabEnabled(spectrum_index, False)
    shell.refresh()
    shell.selectPage("spectrum")
    assert window._tabs.currentWidget() is start
    shell.selectPage("calibration")
    assert window._tabs.currentWidget() is start
    window._tabs.setTabEnabled(spectrum_index, True)
    shell.selectPage("spectrum")
    assert window._tabs.currentWidget() is window._spectrum
    assert shell.runtime.activePage == "spectrum"


def test_history_opens_real_qml_once_and_updates_sample(workbench):
    shell, window = workbench
    shell.showHistory()
    dialog, widget = shell._history_dialog, shell._history_widget
    assert widget.status() == QQuickWidget.Status.Ready
    assert shell._history_context == (window._presets, "dual_gate_sweep", "test-sample")
    shell.showHistory()
    assert shell._history_dialog is dialog
    window._sample_id_binder.value = "other-sample"
    shell.refresh()
    assert widget.view_model.sampleId == "other-sample"
    dialog.close()
    assert shell._history_dialog is None
    assert shell._history_widget is None
    assert shell._history_context is None


@pytest.mark.parametrize("change", ["busy", "sample", "page", "kind"])
def test_history_settings_rejected_when_bound_context_changes(workbench, change):
    shell, window = workbench
    shell._history_context = (window._presets, "dual_gate_sweep", "test-sample")
    kind = "dual_gate_sweep"
    if change == "busy":
        shell.runtime.try_start("spectrum")
    elif change == "sample":
        window._sample_id_binder.value = "other-sample"
    elif change == "page":
        window._tabs.setCurrentWidget(window._spectrum)
    else:
        kind = "motion_sweep"
    shell._load_history_settings(kind, {"exposure_ms": 25})
    assert not window._presets.loaded_settings
    assert shell._history_context is None or "not applied" in window._status.currentMessage()


def test_history_settings_use_same_page_adapter_when_idle(workbench):
    shell, window = workbench
    shell._history_context = (window._presets, "dual_gate_sweep", "test-sample")
    shell._load_history_settings("dual_gate_sweep", {"exposure_ms": 25})
    assert window._presets.loaded_settings == [{"exposure_ms": 25}]


@pytest.mark.parametrize("switch_back", [False, True])
def test_bfp_history_kind_change_invalidates_same_panel_adapter(workbench, switch_back):
    shell, window = workbench
    window._history_adapters = lambda: {
        "bfp_acquisition": "apply_saved_experiment_settings",
        "bfp_binned_rc": "apply_saved_experiment_settings",
    }
    window._history_type.addItems(["bfp_acquisition", "bfp_binned_rc"])
    window._tabs.setCurrentWidget(window._bfp)
    window._history_type.setCurrentText("bfp_acquisition")
    shell.refresh()
    shell._history_context = (window._bfp, "bfp_acquisition", "test-sample")
    # A real BFP internal tab switch updates MainWindow._history_type while
    # retaining the same BFPPanel, sample and top-level tab.
    window._history_type.setCurrentText("bfp_binned_rc")
    assert window._tabs.currentWidget() is window._bfp
    assert shell._history_context is None
    if switch_back:
        window._history_type.setCurrentText("bfp_acquisition")
    # No refresh/event-loop turn: even a fast return to the old kind must not
    # revive a settings result that belongs to the previously bound adapter.
    shell._load_history_settings("bfp_acquisition", {"exposure_ms": 25})
    assert not window._bfp.loaded_settings
    assert "not applied" in window._status.currentMessage()


def test_rebind_spectrum_preserves_runtime_and_retires_old_presenter(workbench, app):
    shell, window = workbench
    old_panel, old_widget = window._spectrum, shell._commands["spectrum"]
    state = shell.runtime.workflow("spectrum")
    owner = window._lf6
    new_panel = SpectrumPanel(owner)
    index = window._tabs.indexOf(old_panel)
    window._tabs.removeTab(index)
    window._tabs.insertTab(index, new_panel, "Spectrum")
    del window._tab_ids[old_panel]
    window._tab_ids[new_panel] = "spectrum"
    window._spectrum = new_panel
    shell.rebind_page("spectrum", old_panel, new_panel)
    assert old_widget.adapter._closed
    assert not old_widget.adapter.presenter.invoke("apply")
    assert shell._commands["spectrum"] is not old_widget
    assert shell.runtime.workflow("spectrum") is state
    assert new_panel._ctrl is owner
    assert owner.shutdown_calls == 0
    assert shell._commands["spectrum"].status() == QQuickWidget.Status.Ready
    old_panel.deleteLater()
    drain_qt_deletions()


def test_shutdown_closes_qml_commands_without_touching_hardware(workbench, app):
    shell, window = workbench
    shell.shutdown()
    assert not shell._timer.isActive()
    assert window._lf6.shutdown_calls == 0
    for widget in shell._commands.values():
        assert not widget.adapter.presenter.invoke("run")
        assert not widget.adapter.presenter.invoke("apply")
        assert not widget.adapter._timer.isActive()
    window._presets._run_btn.setEnabled(False)
    window._presets._run_btn.setEnabled(True)
    app.processEvents()
    assert not shell._commands["dual_gate"].adapter._timer.isActive()


@pytest.mark.parametrize("switch", ["page", "sample"])
def test_history_context_does_not_revive_after_switching_back(workbench, switch):
    shell, window = workbench
    shell._history_context = (window._presets, "dual_gate_sweep", "test-sample")
    if switch == "page":
        window._tabs.setCurrentWidget(window._spectrum)
        window._tabs.setCurrentWidget(window._presets)
    else:
        window._sample_id_binder.value = "temporary-sample"
        shell.refresh()
        window._sample_id_binder.value = "test-sample"
        shell.refresh()
    shell._load_history_settings("dual_gate_sweep", {"exposure_ms": 25})
    assert not window._presets.loaded_settings


def test_shutdown_rejects_all_navigation_and_history_entry_points(workbench):
    shell, window = workbench
    shell._history_context = (window._presets, "dual_gate_sweep", "test-sample")
    initial = window._tabs.currentWidget()
    shell.shutdown()
    shell._load_history_settings("dual_gate_sweep", {"exposure_ms": 25})
    shell.showHistory()
    shell.toggleSidebar()
    shell.saveSample()
    shell.selectPage("spectrum")
    assert not window._presets.loaded_settings
    assert shell._history_dialog is None
    assert window.sidebar_toggles == window.sample_saves == 0
    assert window._tabs.currentWidget() is initial


def test_programmatic_run_commits_the_focused_quick_table_value(workbench):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtWidgets import QTableWidget, QTableWidgetItem
    shell, window = workbench
    panel = window._mega
    table = QTableWidget(1, 1, panel)
    table.setItem(0, 0, QTableWidgetItem("1"))
    panel.layout().addWidget(table)
    captured = []
    panel._run_btn.clicked.connect(lambda: captured.append(table.item(0, 0).text()))
    shell.selectPage("mega_sweep")
    QTest.qWait(50)
    surface = table._quick_editor.quick_widget
    def find(item):
        if item.objectName() == "tableCell_0_0":
            return item
        for child in item.childItems():
            found = find(child)
            if found is not None:
                return found
    field = find(surface.rootObject())
    assert field is not None
    QTest.mouseClick(surface, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     field.mapToScene(QPointF(20, 15)).toPoint())
    QTest.keyClick(surface, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(surface, "12")
    assert table.item(0, 0).text() == "1"
    assert shell._commands["mega_sweep"].adapter.presenter.invoke("run")
    assert captured == ["12"]


def test_display_refresh_uses_published_validation_but_dispatch_reads_sources(workbench, monkeypatch):
    shell, window = workbench
    shell.selectPage("spectrum")
    command = shell._commands["spectrum"].adapter
    calls = []
    def valid(panel, *, refresh=True):
        calls.append(refresh)
        return not refresh
    monkeypatch.setattr("ui.quick_parameter_forms.parameters_valid", valid)
    command.refresh()
    assert calls and not any(calls)
    calls.clear()
    # A stale displayed enabled state cannot bypass fresh command validation.
    assert not shell._before_command("spectrum", "apply")
    assert True in calls
    assert window._lf6._pending is None
