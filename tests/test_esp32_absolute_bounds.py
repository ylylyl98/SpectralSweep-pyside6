"""Absolute destinations must respect the firmware's saved travel window."""
import unittest
import os

from PySide6.QtWidgets import QApplication
from tests.test_esp32_stage_v2 import _Control
from ui.instrument_panel import _ESP32ImagingStageSection

from controllers.esp32_stage_controller import _ESP32Worker


class AbsoluteBoundsTests(unittest.TestCase):
    def test_absolute_bounds_at_each_supported_scale(self):
        for scale in (200, 800, 1600, 3200):
            with self.subTest(scale=scale):
                worker = _ESP32Worker(clock=lambda: 0.0)
                worker._connected = True
                worker._adapter = object()
                worker._last_valid = 0.0
                worker._state = {"protocol": "stage-v2", "homed": True,
                                 "moving": False, "limit": 3 * scale + 1,
                                 "stepsPerMm": scale}
                maximum = worker._state["limit"] / scale
                self.assertTrue(worker._can_accept("goto", 0.0))
                self.assertTrue(worker._can_accept("goto", maximum))
                self.assertFalse(worker._can_accept("goto", maximum + .00001))
                self.assertFalse(worker._can_accept("goto", -0.001))
                worker._state["homed"] = False
                self.assertFalse(worker._can_accept("goto", 0.0))
                worker._state.update(homed=True, limit=0)
                self.assertFalse(worker._can_accept("goto", 0.0))


class AbsolutePanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.ctrl = _Control()
        self.panel = _ESP32ImagingStageSection(self.ctrl)
        self.ctrl.connected.emit("COM-test")
        self.state = dict(valid=True, protocol="stage-v2", homed=True,
                          moving=False, pending=False, cooldown=False,
                          steps=0, limit=9601, stepsPerMm=3200,
                          pulsesPerRev=3200, directionAwayLevel=False)
        self.ctrl.state_changed.emit(self.state)

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()

    def test_destinations_use_saved_state_and_explicit_click_only(self):
        self.assertEqual(self.ctrl.calls, [])
        self.panel._maximum.setValue(50)  # Unapplied draft is not a destination.
        self.panel._goto_zero.click()
        self.panel._goto_maximum.click()
        self.assertEqual(self.ctrl.calls, [("goto", (0.0,)), ("goto", (9601 / 3200,))])

    def test_invalid_target_is_preserved_but_cannot_move(self):
        self.panel._target.setValue(4)
        self.assertFalse(self.panel._move_target.isEnabled())
        self.panel._goto_absolute(4)
        self.assertEqual(self.panel._target.value(), 4)
        self.assertEqual(self.ctrl.calls, [])
        self.panel._target.setValue(2)
        self.panel._move_target.click()
        self.assertEqual(self.ctrl.calls, [("goto", (2.0,))])

    def test_absolute_moves_blocked_until_ready_and_referenced(self):
        for changes in (dict(homed=False), dict(limit=0), dict(valid=False),
                        dict(moving=True), dict(pending=True), dict(cooldown=True),
                        dict(protocol="stage-v1")):
            with self.subTest(changes=changes):
                self.ctrl.state_changed.emit(dict(self.state, **changes))
                for button in (self.panel._goto_zero, self.panel._goto_maximum,
                               self.panel._move_target):
                    self.assertFalse(button.isEnabled())
                self.panel._goto_absolute(0)
                self.panel._goto_saved_maximum()
                self.assertEqual(self.ctrl.calls, [])
                self.assertTrue(self.panel._stop.isEnabled())


if __name__ == "__main__":
    unittest.main()
