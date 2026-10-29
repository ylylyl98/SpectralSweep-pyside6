"""Rotation discovery preserves axis choices and leaves the GUI usable."""
import copy
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from ui.instrument_panel import _RotationBlock
from ui.quick_instrument_motion import configure_motion_section
from utils.config import cfg


class RotationStub(QObject):
    connected = Signal(str, str)
    disconnected = Signal(str)
    error = Signal(str)
    axes_scanned = Signal(str, list)
    operation_finished = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.calls = []
        self.online = False

    def is_connected(self, _slot):
        return self.online

    def scan_axes(self, *args, **kwargs):
        self.calls.append(("scan", args, kwargs))

    def connect_esp300(self, *args, **kwargs):
        self.calls.append(("connect", args, kwargs))


class RotationInstrumentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.saved = copy.deepcopy(cfg.rotation)
        cfg.rotation.rot2.backend = "esp301"
        cfg.rotation.rot2.esp300_axis = 2
        cfg.rotation.rot2.visa_resource = "ASRL12::INSTR"
        cfg.rotation.rot2.esp301_baud = 921600
        self.ctrl = RotationStub()
        self.block = _RotationBlock("rot2", self.ctrl)

    def tearDown(self):
        self.block.close()
        self.block.deleteLater()
        self.app.processEvents()
        cfg.rotation = self.saved

    def test_esp301_visible_and_transport_reaches_scan(self):
        self.assertEqual(self.block._newport_model(), "esp301")
        self.assertFalse(self.block._serial_row_widget.isHidden())
        self.block._on_scan_axes()
        self.assertEqual(self.ctrl.calls, [(
            "scan", ("rot2", "ASRL12::INSTR"), {"model": "esp301", "baud": 921600}
        )])
        for widget in (self.block._scan_axes_btn, self.block._connect_btn,
                       self.block._type_combo, self.block._addr_combo):
            self.assertFalse(widget.isEnabled())
        self.block._on_scan_axes()
        self.assertEqual(len(self.ctrl.calls), 1)
        self.ctrl.axes_scanned.emit("rot2", [1])
        self.ctrl.operation_finished.emit("rot2", "scan")
        self.assertTrue(self.block._connect_btn.isEnabled())
        self.assertEqual(self.block._axis_combo.currentText(), "1")
        self.assertEqual(cfg.rotation.rot2.esp300_axis, 1)
        self.assertIn("1 usable axis", self.block._status.text())

    def test_three_axis_scan_preserves_selected_axis(self):
        self.ctrl.axes_scanned.emit("rot2", [1, 2, 3])
        self.assertEqual(self.block._axis_combo.currentText(), "2")
        self.assertEqual(cfg.rotation.rot2.esp300_axis, 2)
        self.assertEqual(self.block._axis_combo.count(), 3)

    def test_scan_failure_does_not_invent_axis_or_allow_connect(self):
        self.block._on_scan_axes()
        self.ctrl.error.emit("Rotation rot2 axis scan failed: port busy")
        self.ctrl.axes_scanned.emit("rot2", [])
        self.ctrl.operation_finished.emit("rot2", "scan")
        self.assertEqual(self.block._axis_combo.count(), 0)
        self.assertFalse(self.block._connect_btn.isEnabled())
        self.assertTrue(self.block._scan_axes_btn.isEnabled())
        self.assertIn("port busy", self.block._status.text())
        self.block._on_connect()
        self.assertEqual(len(self.ctrl.calls), 1)
        self.ctrl.error.emit("Rotation rot1 axis scan failed: another port")
        self.assertNotIn("another port", self.block._status.text())

    def test_rs232_setting_and_axis_reach_connect(self):
        self.block._baud_combo.setCurrentIndex(self.block._baud_combo.findData(19200))
        self.ctrl.axes_scanned.emit("rot2", [1])
        self.block._on_connect()
        name, args, kwargs = self.ctrl.calls[-1]
        self.assertEqual(name, "connect")
        self.assertEqual(args[:3], ("rot2", "ASRL12::INSTR", 1))
        self.assertEqual(kwargs, {"model": "esp301", "baud": 19200})
        self.assertEqual(cfg.rotation.rot2.esp301_baud, 19200)
        self.ctrl.error.emit("Rotation rot2 (ESP301) connect failed: timed out")
        self.ctrl.operation_finished.emit("rot2", "connect")
        self.assertTrue(self.block._connect_btn.isEnabled())

    def test_quick_regrouping_keeps_esp301_transport_and_profile(self):
        groups = configure_motion_section(self.block)
        self.assertIs(groups["profile"], self.block._profile_group)
        self.assertFalse(self.block._profile_group.isHidden())
        self.assertFalse(self.block._serial_row_widget.isHidden())
        self.block._type_combo.setCurrentText("Newport ESP300 (shared)")
        self.assertTrue(self.block._serial_row_widget.isHidden())
        self.assertEqual(self.block._newport_options(), {"model": "esp300", "baud": None})
        self.block._type_combo.setCurrentText("Thorlabs Elliptec")
        self.assertTrue(self.block._profile_group.isHidden())
        self.assertTrue(self.block._axis_row_widget.isHidden())

    def test_gpib_does_not_display_serial_settings(self):
        self.block._addr_combo.addItem("GPIB0::6::INSTR")
        self.block._addr_combo.setCurrentText("GPIB0::6::INSTR")
        self.assertTrue(self.block._serial_row_widget.isHidden())

    def test_connected_mount_locks_mapping_until_disconnected(self):
        self.ctrl.online = True
        with patch.object(cfg, "save"), patch.object(self.block, "_launch_rot"):
            self.ctrl.connected.emit("rot2", "esp301")
        self.assertFalse(self.block._axis_combo.isEnabled())
        self.assertFalse(self.block._type_combo.isEnabled())
        self.assertFalse(self.block._addr_combo.isEnabled())
        self.assertTrue(self.block._disconnect_btn.isEnabled())
        self.assertFalse(self.block._scan_axes_btn.isEnabled())
        self.assertIn("Motor ON · verified", self.block._status.text())
        self.assertIn("ESP301 at ASRL12::INSTR, axis 2", self.block._mapping_hint.text())
        self.assertNotIn("Scan Axes to verify", self.block._mapping_hint.text())
        self.block._on_scan_axes()
        self.assertEqual(self.ctrl.calls, [])
        self.ctrl.axes_scanned.emit("rot2", [1])
        self.assertEqual(self.block._axis_combo.currentText(), "2")

    def test_motion_error_keeps_full_diagnostic_and_connected_controls(self):
        self.ctrl.online = True
        with patch.object(cfg, "save"), patch.object(self.block, "_launch_rot"):
            self.ctrl.connected.emit("rot2", "esp301")
        self.assertEqual(self.block._status.property("spectralTone"), "success")
        message = (
            "move to 95 readback 0 differs by 95 "
            "(tolerance 0.25, device status stopped) after 3 correction retries"
        )
        self.block._set_ctrl_enabled(False)
        self.block._on_rot_error(message)
        self.block._on_rot_done()
        self.assertEqual(self.block._status.text(), "Rotation error\n" + message)
        self.assertEqual(self.block._status.toolTip(), message)
        self.assertEqual(self.block._status.accessibleDescription(), message)
        self.assertEqual(self.block._status.property("spectralTone"), "danger")
        self.assertTrue(self.block._status.wordWrap())
        self.assertTrue(self.ctrl.is_connected("rot2"))
        self.assertTrue(self.block._disconnect_btn.isEnabled())
        self.assertTrue(self.block._read_pos_btn.isEnabled())
        self.assertTrue(self.block._move_btn.isEnabled())
        self.assertFalse(self.block._connect_btn.isEnabled())
        self.assertFalse(self.block._axis_combo.isEnabled())
        self.assertIn("axis 2", self.block._mapping_hint.text())
        self.assertNotIn("Scan Axes to verify", self.block._mapping_hint.text())

    def test_controller_error_keeps_details_accessible_without_showing_traceback(self):
        message = (
            "Rotation rot2 (ESP301) connect failed: controller reports axis < 1\n"
            "Traceback (most recent call last):\n"
            "OSError: controller did not reply to the stage identification query"
        )
        self.ctrl.error.emit(message)
        self.assertIn(message.splitlines()[0], self.block._status.text())
        self.assertNotIn("Traceback", self.block._status.text())
        self.assertEqual(self.block._status.toolTip(), message)
        self.assertEqual(self.block._status.accessibleDescription(), message)
        self.assertEqual(self.block._status.property("spectralTone"), "danger")

    def test_reconnect_clears_stale_error_details(self):
        self.block._on_rot_error("controller fault detail")
        self.ctrl.online = True
        with patch.object(cfg, "save"), patch.object(self.block, "_launch_rot"):
            self.ctrl.connected.emit("rot2", "esp301")
        self.assertEqual(self.block._status.property("spectralTone"), "success")
        self.assertEqual(self.block._status.toolTip(), "")
        self.assertEqual(self.block._status.accessibleDescription(), "")
        self.ctrl.online = False
        self.block._on_rot_error("another fault")
        self.ctrl.disconnected.emit("rot2")
        self.assertEqual(self.block._status.property("spectralTone"), "muted")
        self.assertEqual(self.block._status.toolTip(), "")

    def test_resource_enumeration_preserves_saved_selection_and_busy_gate(self):
        self.block._begin_connection_operation("resources")
        self.block._fast_asrl = ["ASRL1::INSTR", "ASRL12::INSTR"]
        self.block._on_connect()
        self.block._on_scan_axes()
        self.assertEqual(self.ctrl.calls, [])
        self.block._on_rot_scan_done(["GPIB0::1::INSTR"])
        self.assertEqual(self.block._addr_combo.currentText(), "ASRL12::INSTR")
        self.assertEqual(cfg.rotation.rot2.visa_resource, "ASRL12::INSTR")
        self.assertTrue(self.block._connect_btn.isEnabled())

    def test_late_resource_result_cannot_change_pending_connection(self):
        self.block._on_connect()
        self.block._fast_asrl = ["ASRL1::INSTR"]
        self.block._on_rot_scan_done(["GPIB0::1::INSTR"])
        self.assertEqual(self.block._addr_combo.currentText(), "ASRL12::INSTR")
        self.assertFalse(self.block._connect_btn.isEnabled())
        self.assertFalse(self.block._refresh_btn.isEnabled())
