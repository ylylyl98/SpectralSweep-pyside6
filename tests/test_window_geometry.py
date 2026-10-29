from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QByteArray, QRect, Qt

from ui.main_window import MainWindow, _clamp_window_rect


class WindowGeometryTests(unittest.TestCase):
    def test_oversized_window_is_clamped_inside_available_screen(self):
        available = QRect(0, 0, 1024, 728)
        restored = QRect(-120, -80, 1400, 900)

        bounded = _clamp_window_rect(restored, available, margin=8)

        self.assertEqual(bounded, QRect(8, 8, 1008, 712))
        self.assertTrue(available.contains(bounded))

    def test_valid_window_geometry_is_preserved(self):
        available = QRect(0, 0, 1920, 1040)
        restored = QRect(150, 90, 1400, 900)

        self.assertEqual(
            _clamp_window_rect(restored, available, margin=8),
            restored,
        )


@pytest.mark.parametrize("saved_geometry", [None, QByteArray(b"invalid geometry")])
def test_quick_first_launch_or_unusable_geometry_starts_maximized(monkeypatch, saved_geometry):
    import ui.main_window as main_window

    settings = Mock()
    settings.value.side_effect = lambda key: saved_geometry if key == "geometry" else None
    factory = Mock(return_value=settings)
    timer = Mock()
    monkeypatch.setattr(main_window, "QSettings", factory)
    monkeypatch.setattr(main_window, "QTimer", timer)
    current_state = Qt.WindowState.WindowActive
    window = SimpleNamespace(
        _ui_mode="quick", restoreGeometry=Mock(return_value=False),
        windowState=Mock(return_value=current_state), setWindowState=Mock(),
        restoreState=Mock(), _clamp_to_available_screen=Mock())

    MainWindow._restore_geometry(window)

    window.setWindowState.assert_called_once_with(current_state | Qt.WindowState.WindowMaximized)
    assert window.restoreGeometry.call_count == int(saved_geometry is not None)
    window.restoreState.assert_not_called()
    window._clamp_to_available_screen.assert_called_once_with()
    timer.singleShot.assert_called_once_with(0, window._clamp_to_available_screen)
    factory.assert_called_once_with(main_window._ORG, main_window._APP)


@pytest.mark.parametrize("ui_mode", ["widgets", None])
def test_widgets_first_launch_retains_its_existing_window_state(monkeypatch, ui_mode):
    import ui.main_window as main_window

    settings = Mock()
    settings.value.return_value = None
    monkeypatch.setattr(main_window, "QSettings", Mock(return_value=settings))
    monkeypatch.setattr(main_window, "QTimer", Mock())
    window = SimpleNamespace(
        restoreGeometry=Mock(), windowState=Mock(), setWindowState=Mock(),
        restoreState=Mock(), _clamp_to_available_screen=Mock())
    if ui_mode is not None:
        window._ui_mode = ui_mode

    MainWindow._restore_geometry(window)

    window.restoreGeometry.assert_not_called()
    window.setWindowState.assert_not_called()
    window._clamp_to_available_screen.assert_called_once_with()


def test_quick_valid_saved_geometry_and_dock_state_take_precedence(monkeypatch):
    import ui.main_window as main_window

    saved_geometry = QByteArray(b"saved geometry")
    saved_state = QByteArray(b"saved dock state")
    settings = Mock()
    settings.value.side_effect = {"geometry": saved_geometry, "windowState": saved_state}.get
    monkeypatch.setattr(main_window, "QSettings", Mock(return_value=settings))
    monkeypatch.setattr(main_window, "QTimer", Mock())
    window = SimpleNamespace(
        _ui_mode="quick", restoreGeometry=Mock(return_value=True),
        windowState=Mock(), setWindowState=Mock(), restoreState=Mock(),
        _clamp_to_available_screen=Mock())

    MainWindow._restore_geometry(window)

    window.restoreGeometry.assert_called_once_with(saved_geometry)
    window.restoreState.assert_called_once_with(saved_state)
    window.setWindowState.assert_not_called()
    window.windowState.assert_not_called()
    window._clamp_to_available_screen.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
