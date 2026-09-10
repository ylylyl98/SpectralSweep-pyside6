"""Focused safety checks for the stage-v2 protocol and daily panel."""
import json
import os
import unittest

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from app.devices.esp32_stage_adapter import ESP32StageAdapter
from controllers.esp32_stage_controller import _ESP32Worker
from ui.instrument_panel import _ESP32ImagingStageSection


def v2(**changes):
    state = {"protocol": "stage-v2", "capabilities": ["scale", "direction", "maximum"],
             "homed": False, "moving": False, "steps": 0, "limit": 0,
             "stepsPerMm": 200, "pulsesPerRev": 200, "directionAwayLevel": True,
             "frequencyHz": 100, "message": "OK"}
    state.update(changes)
    return (json.dumps(state) + "\n").encode()


class _Serial:
    def __init__(self, *args, **kwargs): self.reads = []
    def readline(self): return self.reads.pop(0) if self.reads else b""
    def write(self, data): return len(data)
    def close(self): pass


class _Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now


class _Control(QObject):
    connected = Signal(str); disconnected = Signal(); state_changed = Signal(dict); error = Signal(str)
    def __init__(self): super().__init__(); self.calls = []
    def __getattr__(self, name):
        return lambda *args: self.calls.append((name, args))


class TestStageV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def test_v2_readback_validates_capabilities_and_direction(self):
        holder = {}
        adapter = ESP32StageAdapter("COM1", serial_factory=lambda *a, **k: holder.setdefault("s", _Serial()), clock=lambda: 1.0)
        holder["s"].reads.append(v2())
        state = adapter.read_status()
        self.assertFalse(state["legacy"]); self.assertTrue(state["directionAwayLevel"])

    def test_worker_rejects_referenced_jog_without_maximum(self):
        clock = _Clock(); worker = _ESP32Worker(clock=clock)
        worker._connected = True; worker._adapter = object(); worker._last_valid = 0.0
        worker._state = {"protocol": "stage-v2", "legacy": False, "valid": True, "homed": True,
                         "moving": False, "limit": 0, "steps": 0, "stepsPerMm": 200}
        self.assertFalse(worker._can_accept("jog", 0.1))

    def _ready_worker(self):
        clock = _Clock()
        worker = _ESP32Worker(clock=clock)
        worker._connected = True
        worker._adapter = type("A", (), {"write": lambda self, command: None})()
        worker._last_valid = 0.0
        worker._state = {"protocol": "stage-v2", "legacy": False, "valid": True,
                         "homed": True, "moving": False, "limit": 2000,
                         "steps": 200, "stepsPerMm": 200, "pulsesPerRev": 200,
                         "directionAwayLevel": True, "frequencyHz": 100}
        return worker

    def test_scale_ack_requires_readback_and_clears_pending(self):
        worker = self._ready_worker()
        self.assertTrue(worker.post_scale(800)); worker._service_mailbox()
        self.assertIsNotNone(worker._pending)
        worker._handle_state({"protocol": "stage-v2", "legacy": False, "valid": True,
                              "homed": False, "moving": False, "limit": 0,
                              "steps": 0, "stepsPerMm": 800, "pulsesPerRev": 800,
                              "directionAwayLevel": True, "frequencyHz": 100,
                              "message": "Scale updated"})
        self.assertIsNone(worker._pending)

    def test_failed_direction_ack_clears_pending_without_replay(self):
        worker = self._ready_worker()
        self.assertTrue(worker.post_direction(False)); worker._service_mailbox()
        worker._handle_state({"protocol": "stage-v2", "legacy": False, "valid": True,
                              "homed": False, "moving": False, "limit": 0,
                              "steps": 0, "stepsPerMm": 200, "pulsesPerRev": 200,
                              "directionAwayLevel": True, "frequencyHz": 100,
                              "message": "Could not save direction - reference cleared"})
        self.assertIsNone(worker._pending)

    def test_maximum_ack_matches_rounded_target(self):
        worker = self._ready_worker()
        self.assertTrue(worker.post_maximum(10.0)); worker._service_mailbox()
        worker._handle_state({"protocol": "stage-v2", "legacy": False, "valid": True,
                              "homed": True, "moving": False, "limit": 2000,
                              "steps": 200, "stepsPerMm": 200, "pulsesPerRev": 200,
                              "directionAwayLevel": True, "frequencyHz": 100,
                              "message": "Maximum saved"})
        self.assertIsNone(worker._pending)

    def test_setting_mailbox_does_not_queue_a_second_setting(self):
        worker = self._ready_worker()
        self.assertTrue(worker.post_scale(800))
        self.assertFalse(worker.post_direction(False))

    def test_panel_restore_is_control_free_and_legacy_jog_maps_to_coarse(self):
        from utils.config import cfg, ImagingStageConfig
        old = cfg.imaging_stage
        try:
            cfg.imaging_stage = ImagingStageConfig()
            cfg.imaging_stage.jog_mm = 10.0
            ctrl = _Control(); panel = _ESP32ImagingStageSection(ctrl)
            self.assertEqual(panel._preset.currentText(), "Coarse (1 mm)")
            self.assertEqual(ctrl.calls, [])
            ctrl.connected.emit("COM1")
            ctrl.state_changed.emit({"valid": True, "protocol": "stage-v2", "homed": True,
                                     "moving": False, "pending": False, "cooldown": False,
                                     "steps": 0, "limit": 2000, "stepsPerMm": 200,
                                     "pulsesPerRev": 200, "directionAwayLevel": True,
                                     "frequencyHz": 100, "message": "OK"})
            panel._scale.setCurrentIndex(1); panel._scale_dirty = True
            ctrl.state_changed.emit({"valid": True, "protocol": "stage-v2", "homed": True,
                                     "moving": False, "pending": False, "cooldown": False,
                                     "steps": 0, "limit": 2000, "stepsPerMm": 200,
                                     "pulsesPerRev": 200, "directionAwayLevel": True,
                                     "frequencyHz": 100, "message": "OK"})
            self.assertEqual(panel._scale.currentData(), 800)
            ctrl.state_changed.emit({"valid": True, "protocol": "stage-v1", "legacy": True,
                                     "homed": True, "moving": False, "pending": False,
                                     "cooldown": False, "steps": 200, "limit": 2000,
                                     "stepsPerMm": 200, "frequencyHz": 100, "message": "OK"})
            self.assertIn("unavailable until firmware update", panel._position.text())
            ctrl.state_changed.emit({"valid": False, "protocol": "stage-v2", "legacy": False,
                                     "homed": False, "moving": False, "pending": False,
                                     "cooldown": True, "message": "Stopped - position unknown"})
            self.assertNotIn("firmware update", panel._position.text())
            panel.deleteLater()
        finally:
            cfg.imaging_stage = old


if __name__ == "__main__": unittest.main()
