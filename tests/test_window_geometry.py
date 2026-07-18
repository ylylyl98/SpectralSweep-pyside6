from __future__ import annotations

import unittest

from PySide6.QtCore import QRect

from ui.main_window import _clamp_window_rect


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


if __name__ == "__main__":
    unittest.main()
