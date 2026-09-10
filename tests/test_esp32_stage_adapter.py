import json
import unittest

from app.devices.esp32_stage_adapter import ESP32StageAdapter


class FakeSerial:
    def __init__(self, *args, **kwargs):
        self.writes = []
        self.reads = []
        self.closed = False
    def write(self, data):
        self.writes.append(data)
        return len(data)
    def readline(self):
        return self.reads.pop(0) if self.reads else b""
    def close(self):
        self.closed = True


class NeverEndingSerial(FakeSerial):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.in_waiting = 5000
        self.requested = []
    def read(self, size):
        self.requested.append(size)
        return b"x" * size


class TestESP32StageAdapter(unittest.TestCase):
  def test_stage_v2_commands_and_status_are_newline_json(self):
    holder = {}
    def factory(*args, **kwargs):
        holder["serial"] = FakeSerial()
        return holder["serial"]
    adapter = ESP32StageAdapter("COM99", serial_factory=factory)
    adapter.write("jog 0.1")
    adapter.stop_immediate()
    holder["serial"].reads.append((json.dumps({"protocol": "stage-v2", "capabilities": ["scale", "direction", "maximum"], "homed": True, "moving": False, "steps": 20, "limit": 2000, "stepsPerMm": 200, "pulsesPerRev": 200, "directionAwayLevel": True, "frequencyHz": 1000, "message": "OK"}) + "\n").encode())
    self.assertEqual(adapter.read_status()["steps"], 20)
    self.assertEqual(holder["serial"].writes, [b"jog 0.1\n", b"!"])
    adapter.close()
    self.assertTrue(holder["serial"].closed)

  def test_invalid_numeric_status_is_rejected(self):
    adapter = ESP32StageAdapter("COM99", serial_factory=lambda *a, **k: FakeSerial())
    adapter.serial.reads.append(b'{"protocol":"stage-v1","homed":true,"moving":false,"steps":"bad","limit":1,"stepsPerMm":200,"frequencyHz":1000,"message":"OK"}\n')
    self.assertIsNone(adapter.read_status())

  def test_continuous_unterminated_input_uses_bounded_reads(self):
    serial = NeverEndingSerial()
    adapter = ESP32StageAdapter("COM99", serial_factory=lambda *a, **k: serial)
    self.assertIsNone(adapter.read_status())
    self.assertTrue(serial.requested)
    self.assertLessEqual(max(serial.requested), 512)

  def test_supported_pulse_scales_are_validated_and_bounded(self):
    for scale in (200, 3200):
      serial = FakeSerial()
      adapter = ESP32StageAdapter("COM99", serial_factory=lambda *a, **k: serial)
      serial.reads.append((json.dumps({"protocol": "stage-v2", "capabilities": ["scale", "direction", "maximum"], "homed": True, "moving": False,
          "steps": scale * 50, "limit": scale * 100, "stepsPerMm": scale, "pulsesPerRev": scale, "directionAwayLevel": True,
          "frequencyHz": 1000, "message": "OK"}) + "\n").encode())
      state = adapter.read_status()
      self.assertEqual(state["stepsPerMm"], scale)
    serial = FakeSerial()
    adapter = ESP32StageAdapter("COM99", serial_factory=lambda *a, **k: serial)
    serial.reads.append((json.dumps({"protocol": "stage-v2", "capabilities": ["scale", "direction", "maximum"], "homed": True, "moving": False,
        "steps": 1, "limit": 1, "stepsPerMm": 400, "pulsesPerRev": 400, "directionAwayLevel": True, "frequencyHz": 1000, "message": "OK"}) + "\n").encode())
    self.assertIsNone(adapter.read_status())

if __name__ == "__main__":
  unittest.main()
