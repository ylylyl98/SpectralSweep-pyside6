from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QEventLoop, QThread, QTimer
from PySide6.QtWidgets import QApplication

from controllers.rotation_controller import RotationController
from utils.config import AppConfig


class RotationControllerDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.controller = RotationController()
        self.addCleanup(self.controller.shutdown)
        self.finished = []
        self.errors = []
        self.results = []
        self.controller.operation_finished.connect(
            lambda slot, operation: self.finished.append((slot, operation))
        )
        self.controller.error.connect(self.errors.append)
        self.controller.axes_scanned.connect(
            lambda slot, axes: self.results.append((slot, axes))
        )

    def pump_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
            time.sleep(0.002)
        self.assertTrue(predicate(), "Timed out waiting for queued rotation operation")

    def test_slow_scan_returns_promptly_and_ui_heartbeat_continues(self):
        started = threading.Event()
        release = threading.Event()
        worker_threads = []

        def scan(*args, **kwargs):
            worker_threads.append(QThread.currentThread())
            started.set()
            release.wait(2)
            return [1, 2, 3]

        ticks = []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start()
        self.addCleanup(timer.stop)
        with patch("app.devices.esp300_shared.scan_shared_esp300_axes", side_effect=scan):
            try:
                begin = time.monotonic()
                self.controller.scan_axes("rot1", "ASRL5::INSTR", model="esp301", baud=921600)
                self.assertLess(time.monotonic() - begin, 0.1)
                self.pump_until(lambda: started.is_set() and len(ticks) >= 3)
                self.assertEqual(self.results, [])
                self.assertEqual(worker_threads, [self.controller._thread])
            finally:
                release.set()
            self.pump_until(lambda: ("rot1", "scan") in self.finished)
        self.assertEqual(self.results, [("rot1", [1, 2, 3])])

    def test_scan_error_and_empty_discovery_never_invent_axis_one(self):
        with patch("app.devices.esp300_shared.scan_shared_esp300_axes", side_effect=OSError("no reply")):
            self.controller.scan_axes("rot2", "ASRL5::INSTR", model="esp301", baud=19200)
            self.pump_until(lambda: len(self.finished) == 1)
        self.assertEqual(self.results, [("rot2", [])])
        self.assertIn("rot2", self.errors[0])
        with patch("app.devices.esp300_shared.scan_shared_esp300_axes", return_value=[]):
            self.controller.scan_axes("rot1", "ASRL5::INSTR")
            self.pump_until(lambda: len(self.finished) == 2)
        self.assertEqual(self.results[-1], ("rot1", []))

    def test_model_baud_and_motion_profile_reach_scan_and_connection(self):
        connected = []
        profiles = []
        self.controller.connected.connect(lambda slot, model: connected.append((slot, model)))
        self.controller.motion_profile_ready.connect(lambda slot, value: profiles.append((slot, value)))
        adapter = SimpleNamespace(motion_profile="profile", close=lambda: None)
        with patch("app.devices.esp300_shared.scan_shared_esp300_axes", return_value=[1]) as scan:
            self.controller.scan_axes("rot1", "ASRL7::INSTR", model="esp301", baud=115200)
            self.pump_until(lambda: len(self.finished) == 1)
            scan.assert_called_once_with("ASRL7::INSTR", model="esp301", baud=115200)
        with patch("app.devices.rotation_esp300_shared_adapter.SharedESP300Rotation", return_value=adapter) as factory:
            self.controller.connect_esp300(
                "rot1", "ASRL7::INSTR", 1, 0.7, 0.3, model="esp301", baud=115200
            )
            self.pump_until(lambda: len(self.finished) == 2)
            factory.assert_called_once_with(
                "ASRL7::INSTR", axis=1, role="rot1", velocity_fraction=0.7,
                acceleration_fraction=0.3, model="esp301", baud=115200,
            )
        self.assertEqual(connected, [("rot1", "esp301")])
        self.assertEqual(profiles, [("rot1", "profile")])

    def test_duplicate_axis_is_rejected_before_existing_mount_is_closed(self):
        closed = []
        created = []

        def factory(resource, **kwargs):
            axis = kwargs["axis"]
            adapter = SimpleNamespace(
                address=resource, axis=axis, motion_profile=None,
                close=lambda: closed.append(axis),
            )
            created.append(adapter)
            return adapter

        with patch("app.devices.rotation_esp300_shared_adapter.SharedESP300Rotation", side_effect=factory):
            self.controller.connect_esp300("rot1", "ASRL7::INSTR", 1, model="esp301")
            self.controller.connect_esp300("rot2", "ASRL7::INSTR", 2, model="esp301")
            self.pump_until(lambda: len(self.finished) == 2)
            self.controller.connect_esp300("rot2", "asrl7::instr", 1, model="esp301")
            self.pump_until(lambda: len(self.finished) == 3)
        self.assertEqual(len(created), 2)
        self.assertEqual(closed, [])
        self.assertIs(self.controller.adapter("rot2"), created[1])
        self.assertIn("already assigned to rot1", self.errors[-1])

    def test_legacy_connect_defaults_and_failed_connect_complete_the_slot(self):
        with patch(
            "app.devices.rotation_esp300_shared_adapter.SharedESP300Rotation",
            side_effect=OSError("axis unavailable"),
        ) as factory:
            self.controller.connect_esp300("rot2", "ASRL7::INSTR", 2)
            self.pump_until(lambda: len(self.finished) == 1)
            factory.assert_called_once_with(
                "ASRL7::INSTR", axis=2, role="rot2", velocity_fraction=1.0,
                acceleration_fraction=0.5, model="esp300", baud=None,
            )
        self.assertEqual(self.finished, [("rot2", "connect")])
        self.assertIn("axis unavailable", self.errors[-1])
        self.assertFalse(self.controller.is_connected("rot2"))

    def test_disconnect_waits_for_pending_connection(self):
        started = threading.Event()
        release = threading.Event()
        events = []

        def factory(*args, **kwargs):
            started.set()
            release.wait(2)
            events.append("opened")
            return SimpleNamespace(motion_profile=None, close=lambda: events.append("closed"))

        with patch("app.devices.rotation_esp300_shared_adapter.SharedESP300Rotation", side_effect=factory):
            try:
                begin = time.monotonic()
                self.controller.connect_esp300("rot1", "ASRL7::INSTR", model="esp301")
                self.assertLess(time.monotonic() - begin, 0.1)
                self.pump_until(started.is_set)
                self.controller.disconnect("rot1")
                self.assertEqual(events, [])
            finally:
                release.set()
            self.pump_until(lambda: len(self.finished) == 2)
        self.assertEqual(events, ["opened", "closed"])
        self.assertEqual(self.finished, [("rot1", "connect"), ("rot1", "disconnect")])
        self.assertFalse(self.controller.is_connected("rot1"))

    def test_shutdown_drains_pending_connection_and_closes_before_returning(self):
        started = threading.Event()
        release = threading.Event()
        closed = threading.Event()

        def factory(*args, **kwargs):
            started.set()
            release.wait(2)
            return SimpleNamespace(motion_profile=None, close=closed.set)

        with patch("app.devices.rotation_esp300_shared_adapter.SharedESP300Rotation", side_effect=factory):
            self.controller.connect_esp300("rot1", "ASRL7::INSTR", model="esp301")
            self.pump_until(started.is_set)
            release_timer = threading.Timer(0.05, release.set)
            release_timer.start()
            try:
                self.controller.shutdown()
            finally:
                release.set()
                release_timer.join()
        self.assertTrue(closed.is_set())
        self.assertFalse(self.controller._thread.isRunning())
        self.assertFalse(self.controller.is_connected("rot1"))

    def test_esp301_baud_config_round_trip_and_old_config_defaults(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"rotation": {"rot1": {"backend": "esp300", "esp300_axis": 3}}}))
            config = AppConfig()
            config.load(path)
            self.assertEqual(config.rotation.rot1.esp301_baud, 921600)
            self.assertEqual(config.rotation.rot1.esp300_axis, 3)
            config.rotation.rot2.esp301_baud = 115200
            config.save(path)
            restored = AppConfig()
            restored.load(path)
            self.assertEqual(restored.rotation.rot2.esp301_baud, 115200)
            self.assertEqual(restored.rotation.rot1.backend, "esp300")


if __name__ == "__main__":
    unittest.main()
