import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QObject, QEvent
from PySide6.QtWidgets import QApplication, QWidget
from ui.instrument_panel import _Expander
from ui.megasweep_panel import _CollapsibleSection
from ui.power_sweep_panel import PowerSweepPanel


class _WindowShows(QObject):
    def __init__(self):
        super().__init__()
        self.windows = []

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Show and isinstance(watched, QWidget) and watched.isWindow():
            self.windows.append(watched)
        return False


class InstrumentExpanderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_expanded_content_never_shows_as_an_independent_window(self):
        shows = _WindowShows()
        self.app.installEventFilter(shows)
        content = QWidget()
        section = None
        try:
            section = _Expander("Instrument", content, collapsed=False)
            self.assertEqual(shows.windows, [])
            section.show()
            self.app.processEvents()
            self.assertTrue(content.isVisible())
            self.assertEqual(shows.windows, [section])
            section._btn.click()
            self.assertFalse(content.isVisible())
            section._btn.click()
            self.assertTrue(content.isVisible())
            self.assertEqual(shows.windows, [section])
        finally:
            self.app.removeEventFilter(shows)
            if section is not None:
                section.close()
                section.deleteLater()

    def test_collapsed_content_stays_hidden_until_expanded(self):
        content = QWidget()
        section = _Expander("Instrument", content, collapsed=True)
        try:
            section.show()
            self.app.processEvents()
            self.assertFalse(content.isVisible())
            section._btn.click()
            self.assertTrue(content.isVisible())
            self.assertFalse(content.isWindow())
        finally:
            section.close()
            section.deleteLater()

    def test_megasweep_content_is_parented_before_becoming_visible(self):
        shows = _WindowShows()
        self.app.installEventFilter(shows)
        content = QWidget()
        section = None
        try:
            section = _CollapsibleSection("Metadata", content, expanded=True)
            self.assertEqual(shows.windows, [])
            section.show()
            self.app.processEvents()
            self.assertTrue(content.isVisible())
            section._header.click()
            self.assertFalse(content.isVisible())
            section._header.click()
            self.assertTrue(content.isVisible())
            self.assertEqual(shows.windows, [section])
        finally:
            self.app.removeEventFilter(shows)
            if section is not None:
                section.close()
                section.deleteLater()

    def test_positions_label_never_opens_a_window_on_startup_or_mode_change(self):
        shows = _WindowShows()
        self.app.installEventFilter(shows)
        panel = None
        try:
            panel = PowerSweepPanel()
            self.assertEqual(shows.windows, [])
            panel.show()
            self.app.processEvents()
            panel._update_plan_visibility()
            self.assertTrue(panel._pos_input.isVisible())
            self.assertEqual(shows.windows, [panel])
        finally:
            self.app.removeEventFilter(shows)
            if panel is not None:
                panel.close()
                panel.deleteLater()
