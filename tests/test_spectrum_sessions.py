import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from controllers.lf6_controller import _LF6Worker, LF6Controller


class SpectrumSessionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_switch_retains_both_connections_and_closes_all_on_shutdown(self):
        worker = _LF6Worker()
        worker.connect_instrument(True, "lightfield")
        lf = worker.setup
        lf.close = Mock()
        worker.connect_instrument(True, "andor_si")
        andor = worker.setup
        andor.close = Mock()
        lf.close.assert_not_called()
        worker.connect_instrument(True, "lightfield")
        self.assertIs(worker.setup, lf)
        andor.close.assert_not_called()
        self.assertEqual(set(worker.connected_backends), {"lightfield", "andor_si"})
        worker.disconnect_all()
        lf.close.assert_called_once()
        andor.close.assert_called_once()

    def test_switch_to_pixis_routes_front_before_connected_signal(self):
        worker = _LF6Worker()
        worker.connect_instrument(True, 'lightfield')
        lf = worker.setup
        worker._identity = {'backend': 'lightfield', 'output_route': 'front'}
        worker.connect_instrument(True, 'andor_si')
        events = []
        worker.connected.connect(lambda _: events.append('connected'))
        worker.spectrograph_status_ready.connect(lambda status: events.append(status['output_port']))
        with patch('app.devices.lightfield_optics.ensure_output_route',
                   side_effect=lambda setup, route: events.append((setup, route)) or {'output_port': 'FrontExit'}):
            worker.connect_instrument(False, 'lightfield')
        self.assertEqual(events, [(lf, 'front'), 'connected', 'FrontExit'])
        worker.disconnect_all()

    def test_failed_route_keeps_previous_device_connected(self):
        worker = _LF6Worker()
        worker.connect_instrument(True, 'lightfield')
        worker._identity = {'backend': 'lightfield', 'output_route': 'front'}
        worker.connect_instrument(True, 'andor_si')
        original = worker.setup
        errors = []
        worker.error.connect(errors.append)
        with patch('app.devices.lightfield_optics.ensure_output_route', side_effect=RuntimeError('port readback failed')):
            worker.connect_instrument(False, 'lightfield')
        self.assertIs(worker.setup, original)
        self.assertEqual(worker.backend, 'andor_si')
        self.assertIn('port readback failed', errors[-1])
        self.assertIn('lightfield', worker.connected_backends)
        worker.disconnect_all()

    def test_failed_new_connection_restores_existing_backend(self):
        worker = _LF6Worker()
        worker.connect_instrument(True, "lightfield")
        original = worker.setup
        original.close = Mock()
        with patch("utils.mock_lf6.MockLF6Setup", side_effect=RuntimeError("unavailable")):
            worker.connect_instrument(True, "andor_si")
        self.assertIs(worker.setup, original)
        self.assertEqual(worker.backend, "lightfield")
        original.close.assert_not_called()
        worker.disconnect_all()

    def test_disconnect_active_does_not_close_parked_device(self):
        worker = _LF6Worker()
        worker.connect_instrument(True, "andor_si")
        andor = worker.setup
        andor.close = Mock()
        worker.connect_instrument(True, "lightfield")
        worker.disconnect_instrument()
        andor.close.assert_not_called()
        worker.connect_instrument(True, "andor_si")
        self.assertIs(worker.setup, andor)
        worker.disconnect_all()

    def test_shared_shamrock_cannot_open_two_andor_roles(self):
        worker = _LF6Worker()
        worker.connect_instrument(True, "andor_si")
        original = worker.setup
        errors = []
        worker.error.connect(errors.append)
        worker.connect_instrument(True, "andor_ingaas")
        self.assertIs(worker.setup, original)
        self.assertTrue(errors)
        worker.disconnect_all()

    def test_scan_lock_rejects_switch_and_exit_checks_inactive_camera(self):
        controller = LF6Controller()
        try:
            worker = controller._worker
            worker.connect_instrument(True, "andor_si")
            camera = worker.setup
            camera.get_disconnect_safety_snapshot = Mock(return_value={"temperature_c": -70, "cooler_on": True})
            worker.connect_instrument(True, "lightfield")
            controller.set_temperature_monitor_paused("sweep", True)
            requests = []
            controller._connect_requested.connect(lambda *args: requests.append(args))
            controller._shamrock_connection_requested.connect(lambda *args: requests.append(args))
            controller.connect_instrument(True, "andor_si")
            controller.set_shamrock_connected(False)
            self.assertEqual(requests, [])
            self.assertEqual(controller.andor_disconnect_safety_snapshot()["temperature_c"], -70)
        finally:
            controller.shutdown()

    def test_inactive_andor_temperature_is_polled_with_lightfield_active(self):
        worker = _LF6Worker()
        worker.connect_instrument(True, "andor_si")
        worker.setup.get_temperature_snapshot = Mock(return_value={"temperature_c": -70, "cooler_on": True})
        worker.connect_instrument(True, "lightfield")
        snapshots = []
        worker.backend_temperature_snapshot.connect(snapshots.append)
        worker.read_temperature_snapshot(0)
        self.assertEqual(snapshots, [("andor_si", {"temperature_c": -70, "cooler_on": True})])
        worker.disconnect_all()

    def test_instrument_selector_remains_available_with_one_connection(self):
        from ui.instrument_panel import _LF6Section
        controller = LF6Controller()
        section = _LF6Section(controller)
        try:
            controller._worker.connect_instrument(True, "lightfield")
            self.assertTrue(section._backend.isEnabled())
            self.assertIn("LightField", section._connections_status.text())
            section._backend.setCurrentIndex(section._backend.findData("andor_si"))
            self.assertTrue(section._connect_btn.isEnabled())
            self.assertEqual(section._connect_btn.text(), "Connect required devices")
            controller._worker.connect_instrument(True, "andor_si")
            section._backend.setCurrentIndex(section._backend.findData("lightfield"))
            self.assertEqual(section._connect_btn.text(), "Switch to this setup")
            controller.set_temperature_monitor_paused("test_scan", True)
            self.assertFalse(section._backend.isEnabled())
            self.assertFalse(section._connect_btn.isEnabled())
            controller.set_temperature_monitor_paused("test_scan", False)
            self.assertTrue(section._backend.isEnabled())
        finally:
            section.close()
            controller.shutdown()
