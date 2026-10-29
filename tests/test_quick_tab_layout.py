"""Coordinate preview tabs remain fully readable without horizontal paging."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QTabWidget, QToolButton, QWidget

from tests.quick_helpers import drain_qt_deletions, quick_application
from ui.application_style import QUICK_STYLE_PATH, read_style
from ui.quick_tab_layout import install_adaptive_preview_tabs


def test_preview_tabs_fit_minimum_width_restore_wide_labels_and_follow_axis_updates():
    app = quick_application()
    original_style = app.styleSheet()
    app.setStyleSheet(read_style(QUICK_STYLE_PATH))
    tabs = QTabWidget()
    full_labels = ["Raw (Vtg/Vbg)", "Derived (Doping / E-field)"]
    for label in full_labels:
        tabs.addTab(QWidget(), label)
    tabs.resize(420, 260)
    helper = install_adaptive_preview_tabs(tabs)
    assert install_adaptive_preview_tabs(tabs) is helper
    tabs.show()
    QTest.qWait(20)
    bar = tabs.tabBar()

    def assert_all_labels_visible(labels):
        assert bar.elideMode() == Qt.TextElideMode.ElideNone
        assert not any(button.isVisibleTo(tabs) for button in bar.findChildren(QToolButton))
        for index, full_label in enumerate(labels):
            assert " ".join(bar.tabText(index).split()) == full_label
            assert bar.tabToolTip(index) == full_label
            rect = bar.tabRect(index)
            assert rect.left() >= 0 and rect.right() < bar.width()
            assert rect.height() >= bar.fontMetrics().height() * len(bar.tabText(index).split("\n"))

    try:
        assert_all_labels_visible(full_labels)
        tabs.resize(900, 260)
        QTest.qWait(20)
        assert_all_labels_visible(full_labels)
        assert [bar.tabText(index) for index in range(2)] == full_labels

        # The preview owns these titles and changes them when coordinate axes
        # change. The presentation helper must read those new captions.
        changed_labels = ["Physical (D/F)", "Raw (Vtg/Vbg)"]
        for index, label in enumerate(changed_labels):
            tabs.setTabText(index, label)
        tabs.resize(420, 260)
        QTest.qWait(20)
        assert_all_labels_visible(changed_labels)
    finally:
        tabs.close()
        tabs.deleteLater()
        drain_qt_deletions()
        app.setStyleSheet(original_style)
