import os
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QSplitter, QVBoxLayout, QWidget)

from tests.quick_helpers import quick_application, drain_qt_deletions
from tests.test_quick_parameter_forms import find_item
from ui.quick_parameter_forms import install_parameter_forms
from ui.quick_sidebar import install_sidebar_forms
from ui.quick_workbench import QuickWorkbench


def test_outer_sidebar_preserves_sample_callbacks_without_copying_instrument_fields():
    quick_application()
    window = QWidget()
    window._sidebar_splitter = QSplitter(Qt.Orientation.Horizontal)
    outer = QVBoxLayout(window)
    outer.addWidget(window._sidebar_splitter)
    sidebar = QWidget()
    layout = QVBoxLayout(sidebar)
    window._sidebar_hide = QPushButton("Hide")
    layout.addWidget(window._sidebar_hide)
    window._sample_summary = QWidget()
    summary = QHBoxLayout(window._sample_summary)
    summary.addWidget(QLabel("Sample ID"))
    window._sample_selector = QComboBox()
    window._sample_selector.setEditable(True)
    window._sample_selector.addItems(["First", "Second"])
    summary.addWidget(window._sample_selector)
    layout.addWidget(window._sample_summary)
    window._inst_panel = QWidget()
    instrument_layout = QVBoxLayout(window._inst_panel)
    instrument = QDoubleSpinBox()
    instrument_layout.addWidget(instrument)
    layout.addWidget(window._inst_panel)
    instrument_forms = install_parameter_forms(window._inst_panel, "instruments")
    window._history_toggle = QPushButton("Sample settings")
    layout.addWidget(window._history_toggle)
    window._sample_settings_panel = QWidget()
    settings = QVBoxLayout(window._sample_settings_panel)
    window._sample_filter = QLineEdit()
    window._sample_filter.setPlaceholderText("Search saved samples")
    settings.addWidget(window._sample_filter)
    window._sample_save_btn = QPushButton("Save now")
    settings.addWidget(window._sample_save_btn)
    layout.addWidget(window._sample_settings_panel)
    window._sidebar_splitter.addWidget(sidebar)
    window._sidebar_splitter.addWidget(QWidget())
    calls = dict(activated=[], finished=[], saved=[])
    window._sample_selector.activated.connect(lambda index: calls["activated"].append(index))
    window._sample_selector.lineEdit().editingFinished.connect(lambda: calls["finished"].append(window._sample_selector.currentText()))
    window._sample_save_btn.clicked.connect(lambda: calls["saved"].append(window._sample_selector.currentText()))
    window._history_toggle.clicked.connect(lambda: window._sample_settings_panel.setVisible(window._sample_settings_panel.isHidden()))
    manager = install_sidebar_forms(window)
    window.resize(900, 600)
    window.show()
    window._sidebar_splitter.setSizes([330, 570])
    QTest.qWait(30)
    try:
        assert not any(source is instrument for view in manager.forms for source in view.adapter._fields.values())
        assert sum(source is instrument for view in instrument_forms["forms"] for source in view.adapter._fields.values()) == 1
        combo_view = next(view for view in manager.forms if window._sample_selector in view.adapter._fields.values())
        combo_field = next(field for field, source in combo_view.adapter._fields.items() if source is window._sample_selector)
        assert combo_field.select(1)
        assert calls["activated"] == [1]
        combo = find_item(combo_view.rootObject(), "parameter_sample_selector")
        input_item = combo.property("contentItem")
        QTest.mouseClick(combo_view, Qt.MouseButton.LeftButton,
                         pos=input_item.mapToScene(input_item.boundingRect().center()).toPoint())
        QTest.keyClick(combo_view, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClicks(combo_view, "Typed sample")
        filter_view = next(view for view in manager.forms if window._sample_filter in view.adapter._fields.values())
        filter_item = find_item(filter_view.rootObject(), "parameter_sample_filter")
        QTest.mouseClick(filter_view, Qt.MouseButton.LeftButton,
                         pos=filter_item.mapToScene(filter_item.boundingRect().center()).toPoint())
        assert calls["finished"] == ["Typed sample"]
        save_field = next(field for view in manager.forms for field, source in view.adapter._fields.items() if source is window._sample_save_btn)
        assert save_field.trigger()
        assert calls["saved"] == ["Typed sample"]
        toggle = next(field for view in manager.forms for field, source in view.adapter._fields.items() if source is window._history_toggle)
        assert toggle.trigger()
        for view in manager.forms:
            view.adapter.refresh()
        assert window._sample_settings_panel.isHidden()
        assert install_sidebar_forms(window) is manager
    finally:
        manager.shutdown()
        for view in instrument_forms["forms"]:
            view.shutdown()
        window.close()
        window.deleteLater()
        drain_qt_deletions()


def test_workbench_sidebar_preserves_manual_width_across_narrow_and_wide_transitions(monkeypatch):
    quick_application()
    window = QWidget()
    splitter = QSplitter(Qt.Orientation.Horizontal, window)
    sidebar = QWidget()
    sidebar.setMinimumWidth(220)
    window._tabs = QWidget()
    splitter.addWidget(sidebar)
    splitter.addWidget(window._tabs)
    splitter.setStretchFactor(0, 0)
    splitter.setStretchFactor(1, 1)
    outer = QVBoxLayout(window)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.addWidget(splitter)
    shell = SimpleNamespace(window=window, _content_splitter=splitter,
                            _narrow=None, _wide_sidebar_width=300, _instrument_focus=False)
    splitter.splitterMoved.connect(
        lambda position, index: QuickWorkbench._remember_wide_sidebar_width(shell, position, index))
    window.resize(1600, 700)
    window.show()
    QTest.qWait(10)
    try:
        fit = lambda: QuickWorkbench._fit_workspace(shell)
        resize_sidebar = Mock(wraps=splitter.setSizes)
        monkeypatch.setattr(splitter, "setSizes", resize_sidebar)

        # The first wide pass must establish a usable sidebar even before any
        # transition; subsequent wide refreshes must leave a user's drag alone.
        fit()
        assert shell._narrow is False
        assert sidebar.isVisible()
        assert splitter.sizes()[0] == 300
        resize_sidebar.assert_called_once()
        splitter.moveSplitter(412, 1)
        manual_width = splitter.sizes()[0]
        assert shell._wide_sidebar_width == manual_width
        resize_sidebar.reset_mock()
        fit()
        fit()
        resize_sidebar.assert_not_called()
        assert splitter.sizes()[0] == manual_width
        for _ in range(2):
            window.resize(1000, 700)
            QTest.qWait(10)
            fit()
            window.resize(1600, 700)
            QTest.qWait(10)
            fit()
            assert splitter.sizes()[0] == manual_width

        window.resize(1000, 700)
        QTest.qWait(10)
        fit()
        assert shell._narrow is True
        assert sidebar.isHidden()
        assert window._tabs.isVisible()
        assert shell._wide_sidebar_width == manual_width
        QuickWorkbench._remember_wide_sidebar_width(shell, 80, 1)
        assert shell._wide_sidebar_width == manual_width
        fit()
        assert shell._wide_sidebar_width == manual_width

        window.resize(1600, 700)
        QTest.qWait(10)
        fit()
        assert sidebar.isVisible()
        assert splitter.sizes()[0] == manual_width
        resize_sidebar.reset_mock()
        fit()
        resize_sidebar.assert_not_called()
        assert splitter.sizes()[0] == manual_width
    finally:
        window.close()
        window.deleteLater()
        drain_qt_deletions()
