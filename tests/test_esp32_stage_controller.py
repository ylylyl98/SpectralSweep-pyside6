"""Protocol/controller tests use a deterministic clock and fake serial owner."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import QApplication, QDialogButtonBox
from controllers.esp32_stage_controller import _ESP32Worker
from ui.instrument_panel import _ESP32ImagingStageSection
from utils.config import cfg


def frame(*, homed=False, moving=False, steps=0, limit=4000, frequency=1000, message="OK"):
    return (json.dumps({"protocol": "stage-v2", "capabilities": ["scale", "direction", "maximum"], "homed": homed, "moving": moving,
                        "steps": steps, "limit": limit, "stepsPerMm": 200, "pulsesPerRev": 200, "directionAwayLevel": True,
                        "frequencyHz": frequency, "message": message}) + "\n").encode()


class Clock:
    def __init__(self):
        self.value = 0.0
    def __call__(self):
        return self.value
    def advance(self, seconds):
        self.value += seconds


class FakeAdapter:
    def __init__(self, port):
        self.port = port
        self.writes = []
        self.reads = []
        self.closed = False
    def flush_input(self):
        self.reads.clear()
    def stop_immediate(self):
        self.writes.append(b"!")
    def write(self, command):
        self.writes.append((command + "\n").encode())
    def read_status(self):
        raw = self.reads.pop(0) if self.reads else None
        return json.loads(raw) if raw else None
    def close(self):
        self.closed = True


class RaisingAdapter(FakeAdapter):
    def read_status(self):
        raise OSError("USB removed")


class ESP32WorkerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.adapters = []
        def factory(port):
            adapter = FakeAdapter(port)
            self.adapters.append(adapter)
            return adapter
        self.worker = _ESP32Worker(factory, clock=self.clock)
        self.worker.connect_port("COM9")
        self.adapter = self.adapters[0]
        self.clock.advance(.36)
        self.adapter.reads.append(frame())
        self.worker._tick()  # post-STOP unhomed idle handshake + first status
        self.adapter.writes.clear()

    def test_duplicate_is_rejected_and_mailbox_is_bounded(self):
        self.assertTrue(self.worker.post_distance("jog", 1))
        self.assertFalse(self.worker.post_distance("jog", 2))
        self.worker._service_mailbox()
        self.assertEqual(self.adapter.writes, [b"jog 1\n"])

    def test_generic_idle_status_does_not_release_pending_command(self):
        self.assertTrue(self.worker.post_distance("jog", 1))
        self.worker._service_mailbox()
        self.adapter.reads.append(frame(message="OK"))
        self.worker._tick()
        self.assertIsNotNone(self.worker._pending)
        self.assertFalse(any(w == b"jog 1\n" for w in self.adapter.writes[1:]))

    def test_stop_cancels_unsent_command_and_has_no_delayed_motion(self):
        self.assertTrue(self.worker.post_distance("jog", 1))
        self.worker.request_stop()
        self.worker._tick()
        self.clock.advance(.36)
        self.worker._tick()
        self.assertNotIn(b"jog 1\n", self.adapter.writes)
        self.assertGreaterEqual(self.adapter.writes.count(b"!"), 1)

    def test_post_stop_homed_buffer_is_rejected_until_unhomed_handshake(self):
        self.worker.request_stop(); self.worker._tick(); self.clock.advance(.36)
        self.adapter.reads.append(frame(homed=True, moving=False, steps=200, message="OK"))
        self.worker._tick()
        self.assertIsNone(self.worker._last_valid)
        self.assertFalse(self.worker._state)
        self.adapter.reads.append(frame(homed=False, moving=False, steps=0, message="Set zero again before normal moves"))
        self.worker._tick()
        self.assertIsNotNone(self.worker._last_valid)
        self.assertFalse(self.worker._state["homed"])

    def test_motion_heartbeat_is_sent_and_completion_cools_down(self):
        self.worker._state.update(valid=True, homed=True, moving=False, limit=4000, steps=0)
        self.worker._last_valid = self.clock()
        self.assertTrue(self.worker.post_distance("move", 1)); self.worker._service_mailbox()
        self.adapter.reads.append(frame(homed=True, moving=True, steps=0, message="Moving"))
        self.worker._tick()
        self.clock.advance(.51); self.worker._tick()
        self.assertIn(b"status\n", self.adapter.writes)
        self.adapter.reads.append(frame(homed=True, moving=False, steps=200, message="Move complete"))
        self.worker._tick()
        before = len(self.adapter.writes)
        self.clock.advance(.2); self.worker._tick()
        self.assertEqual(len(self.adapter.writes), before)

    def test_completion_with_wrong_integer_target_does_not_release_pending(self):
        self.worker._state.update(valid=True, homed=True, moving=False, limit=4000, steps=0)
        self.worker._last_valid = self.clock()
        self.assertTrue(self.worker.post_distance("move", 1)); self.worker._service_mailbox()
        self.adapter.reads.append(frame(homed=True, moving=True, steps=0, message="Moving")); self.worker._tick()
        self.adapter.reads.append(frame(homed=True, moving=False, steps=199, message="Move complete")); self.worker._tick()
        self.assertTrue(self.worker._stop_event.is_set())
        self.assertIsNone(self.worker._pending)

    def test_read_failure_closes_serial(self):
        clock = Clock(); holder = []
        def factory(port):
            a = RaisingAdapter(port); holder.append(a); return a
        worker = _ESP32Worker(factory, clock=clock)
        worker.connect_port("COM10"); clock.advance(.36); worker._tick()
        self.assertTrue(holder[0].closed)
        self.assertIsNone(worker._adapter)

    def test_startup_idle_and_moving_status_each_expire(self):
        for mode in ("startup", "idle", "moving"):
            with self.subTest(mode=mode):
                clock = Clock(); holder = []
                def factory(port):
                    a = FakeAdapter(port); holder.append(a); return a
                worker = _ESP32Worker(factory, clock=clock)
                worker.connect_port("COM11")
                clock.advance(.36)
                if mode != "startup":
                    holder[0].reads.append(frame(homed=(mode == "moving"), moving=(mode == "moving"), message="Moving" if mode == "moving" else "OK"))
                worker._tick()
                if mode == "startup":
                    # no response to the first status request
                    pass
                clock.advance(1.6)
                worker._tick()
                self.assertIsNone(worker._adapter)

    def test_lost_initial_ack_fences_before_any_second_command(self):
        self.assertTrue(self.worker.post_distance("jog", 1)); self.worker._service_mailbox()
        self.clock.advance(3.1)
        self.worker._tick()
        self.assertTrue(self.worker._stop_event.is_set())
        self.assertIsNone(self.worker._pending)
        self.worker._tick()
        self.assertNotIn(b"jog 2\n", self.adapter.writes)


class SidebarController(QObject):
    connected = Signal(str)
    disconnected = Signal()
    state_changed = Signal(dict)
    error = Signal(str)
    def __init__(self):
        super().__init__()
        self.calls = []
    def jog(self, value): pass
    def move_relative(self, value): pass
    def goto(self, value): pass
    def set_frequency(self, value): pass
    def send(self, value): self.calls.append(("send", (value,)))
    def stop(self): pass
    def connect_instrument(self, value): pass
    def disconnect_instrument(self): pass


class ESP32SidebarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def test_gui_gates_normal_moves_but_keeps_unhomed_jog_and_stop(self):
        ctrl = SidebarController()
        widget = _ESP32ImagingStageSection(ctrl)
        self.assertFalse(widget._stop.isEnabled())
        ctrl.connected.emit("COM1")
        ctrl.state_changed.emit({"valid": True, "protocol": "stage-v2", "homed": False, "moving": False,
                                 "pending": False, "cooldown": False, "steps": 0,
                                 "limit": 0, "stepsPerMm": 200, "pulsesPerRev": 200, "directionAwayLevel": True, "frequencyHz": 1000, "message": "OK"})
        self.assertTrue(widget._stop.isEnabled())
        self.assertTrue(widget._jog_plus.isEnabled())
        self.assertTrue(widget._zero.isEnabled())
        ctrl.state_changed.emit({"valid": True, "protocol": "stage-v2", "homed": True, "moving": False,
                                 "pending": False, "cooldown": False, "steps": 200,
                                 "limit": 4000, "stepsPerMm": 200, "pulsesPerRev": 200, "directionAwayLevel": True, "frequencyHz": 1200, "message": "OK"})
        self.assertTrue(widget._jog_plus.isEnabled())
        self.assertIn("20", widget._limit.text())
        self.assertIn("1200", widget._frequency_readout.text())

    def _ready_imaging_widget(self):
        ctrl = SidebarController()
        widget = _ESP32ImagingStageSection(ctrl)
        ctrl.connected.emit("COM1")
        ctrl.state_changed.emit({"valid": True, "protocol": "stage-v2", "homed": False, "moving": False,
                                 "pending": False, "cooldown": False, "steps": 0, "limit": 0,
                                 "stepsPerMm": 200, "pulsesPerRev": 200, "directionAwayLevel": True,
                                 "frequencyHz": 1000, "message": "OK"})
        return ctrl, widget

    def test_reference_dialog_cancel_does_not_send_zero(self):
        ctrl, widget = self._ready_imaging_widget()
        widget._zero.click()
        dialog = widget._reference_dialog
        self.assertIsNotNone(dialog)
        self.assertEqual(dialog.windowModality(), Qt.NonModal)
        dialog.reject()
        self.assertIsNone(widget._reference_dialog)
        self.assertNotIn(("send", ("zero",)), getattr(ctrl, "calls", []))
        widget.deleteLater()

    def test_reference_dialog_confirm_sends_zero_once(self):
        ctrl, widget = self._ready_imaging_widget()
        widget._zero.click()
        dialog = widget._reference_dialog
        button = next(b for b in dialog.findChildren(QDialogButtonBox)[0].buttons()
                      if b.text() == "Set current position to 0")
        button.click()
        self.assertEqual(getattr(ctrl, "calls", []).count(("send", ("zero",))), 1)
        self.assertIsNone(widget._reference_dialog)
        widget.deleteLater()

    def test_reference_dialog_is_invalidated_when_stage_starts_moving(self):
        ctrl, widget = self._ready_imaging_widget()
        widget._zero.click()
        self.assertIsNotNone(widget._reference_dialog)
        ctrl.state_changed.emit({"valid": True, "protocol": "stage-v2", "homed": False, "moving": True,
                                 "pending": False, "cooldown": False, "steps": 0, "limit": 0,
                                 "stepsPerMm": 200, "frequencyHz": 1000})
        self.assertIsNone(widget._reference_dialog)
        self.assertNotIn(("send", ("zero",)), getattr(ctrl, "calls", []))
        widget.deleteLater()

    def test_imaging_stage_preferences_round_trip(self):
        old = (cfg.imaging_stage.com_port, cfg.imaging_stage.jog_mm, cfg.imaging_stage.frequency_hz)
        try:
            with tempfile.TemporaryDirectory() as folder:
                path = os.path.join(folder, "config.json")
                cfg.imaging_stage.com_port = "COM77"; cfg.imaging_stage.jog_mm = 0.75; cfg.imaging_stage.frequency_hz = 1500
                cfg.save(path)
                cfg.imaging_stage.com_port = ""; cfg.imaging_stage.jog_mm = 0.1; cfg.imaging_stage.frequency_hz = 1000
                cfg.load(path)
                self.assertEqual(cfg.imaging_stage.com_port, "COM77")
                self.assertAlmostEqual(cfg.imaging_stage.jog_mm, 0.75)
                self.assertEqual(cfg.imaging_stage.frequency_hz, 1500)
        finally:
            cfg.imaging_stage.com_port, cfg.imaging_stage.jog_mm, cfg.imaging_stage.frequency_hz = old


if __name__ == "__main__":
    unittest.main()
