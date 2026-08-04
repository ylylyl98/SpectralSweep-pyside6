from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from app.devices.iv_adapter import IVDevice
from controllers.smu_controller import _SMUWorker
from ui.instrument_panel import (
    InstrumentPanel,
    _ManualControlSection,
    _SMUSection,
    _select_or_insert_combo_text,
)


class _FakeSMUController(QObject):
    connected = Signal(list)
    disconnected = Signal()
    error = Signal(str)
    readings_ready = Signal(object)
    manual_finished = Signal(str, str, float)
    manual_error = Signal(str)
    limits_result = Signal(str, str, object)
    limits_error = Signal(str, str, str)
    limits_state_changed = Signal()

    def __init__(self):
        super().__init__()
        self.is_connected = False
        self.available_roles: set[str] = set()
        self.connect_calls = []
        self.manual_calls = []
        self.limit_apply_calls = []
        self.limit_read_calls = []
        self.dirty_limit_addresses = []

    def connect_instrument(self, visa_addrs, role_map, termination, compliance):
        self.connect_calls.append((visa_addrs, role_map, termination, compliance))

    def disconnect_instrument(self):
        self.is_connected = False
        self.disconnected.emit()

    def role_is_available(self, role):
        return role in self.available_roles

    def manual_control(self, action, role, value, **kwargs):
        self.manual_calls.append((action, role, value, kwargs))

    def apply_smu_limits(self, address, curr, curr_range, volt):
        self.limit_apply_calls.append((address, curr, curr_range, volt))

    def read_smu_limits(self, address):
        self.limit_read_calls.append(address)

    def mark_smu_limits_dirty(self, address):
        self.dirty_limit_addresses.append(address)
        self.limits_state_changed.emit()


class _ManualDevice:
    def __init__(self, measured=1.2, read_error=None, roles=("Vbg",)):
        self.measured = measured
        self.read_error = read_error
        self.roles = set(roles)
        self.pre_read_calls = []
        self.snapshot_calls = []
        self.ramp_calls = []
        self.fast_set_calls = []

    def role_is_available(self, role):
        return role in self.roles

    def has_role(self, role):
        return role in self.roles

    def read_role_voltage(self, role, *, strict=False):
        self.pre_read_calls.append(role)
        if self.read_error is not None:
            raise self.read_error
        return self.measured

    def read_role_snapshot(self, role, *, strict=False):
        self.snapshot_calls.append(role)
        return self.measured, 1e-9

    def ramp_to(self, role, target, step, delay, *, start_value=None):
        self.ramp_calls.append((role, target, step, delay, start_value))
        return True

    def set_role_voltage_fast(self, role, target, delay_s=0.0):
        self.fast_set_calls.append((role, target, delay_s))
        return True


class KeithleyControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _select(combo, address):
        _select_or_insert_combo_text(combo, address)

    def test_connection_includes_safe_default_limits_per_address(self):
        ctrl = _FakeSMUController()
        section = _SMUSection(ctrl)
        self._select(section._role_vbg, "GPIB0::9::INSTR")
        self._select(section._role_vtg, "GPIB0::11::INSTR")
        section._role_vbias.setCurrentText("<none>")
        section._curr_comp_by_role["Vbg"].setValue(600.0)
        section._volt_comp_by_role["Vbg"].setValue(20.0)
        section._curr_comp_by_role["Vtg"].setValue(850.0)
        section._volt_comp_by_role["Vtg"].setValue(30.0)

        section._on_connect()

        self.assertEqual(len(ctrl.connect_calls), 1)
        visa_addrs, role_map, _termination, compliance = ctrl.connect_calls[0]
        self.assertEqual(visa_addrs, ["GPIB0::9::INSTR", "GPIB0::11::INSTR"])
        self.assertEqual(role_map["Vbg"], "GPIB0::9::INSTR")
        self.assertAlmostEqual(compliance["GPIB0::9::INSTR"]["curr"], 600e-9)
        self.assertAlmostEqual(
            compliance["GPIB0::9::INSTR"]["curr_range"], 1e-6
        )
        self.assertAlmostEqual(compliance["GPIB0::9::INSTR"]["volt"], 20.0)
        self.assertAlmostEqual(compliance["GPIB0::11::INSTR"]["curr"], 850e-9)
        self.assertAlmostEqual(
            compliance["GPIB0::11::INSTR"]["curr_range"], 1e-6
        )
        self.assertAlmostEqual(compliance["GPIB0::11::INSTR"]["volt"], 30.0)
        self.assertEqual(ctrl.limit_apply_calls, [])

    def test_compact_sections_fit_the_existing_sidebar(self):
        ctrl = _FakeSMUController()
        self.assertLessEqual(_SMUSection(ctrl).minimumSizeHint().width(), 340)
        self.assertLessEqual(
            _ManualControlSection(ctrl).minimumSizeHint().width(), 340
        )

    def test_live_compliance_apply_auto_selects_range_for_500_na(self):
        ctrl = _FakeSMUController()
        ctrl.is_connected = True
        section = _SMUSection(ctrl)
        address = "GPIB0::9::INSTR"
        self._select(section._role_vbg, address)
        section._curr_comp_by_role["Vbg"].setValue(500.0)
        range_combo = section._curr_range_by_role["Vbg"]
        section._volt_comp_by_role["Vbg"].setValue(20.0)

        section._apply_role_limits("Vbg")

        self.assertEqual(len(ctrl.limit_apply_calls), 1)
        applied_address, compliance, current_range, voltage_range = (
            ctrl.limit_apply_calls[0]
        )
        self.assertEqual(applied_address, address)
        self.assertAlmostEqual(compliance, 500e-9)
        self.assertAlmostEqual(current_range, 1e-6)
        self.assertAlmostEqual(voltage_range, 20.0)

        with patch("ui.instrument_panel.cfg.save"):
            ctrl.limits_result.emit(
                "apply",
                address,
                {"curr": 500e-9, "curr_range": 1e-6, "volt": 20.0},
            )
        status = section._limit_status_by_role["Vbg"]
        self.assertEqual(status.text(), "Applied")
        self.assertIn("500 nA", status.toolTip())
        self.assertEqual(range_combo.currentText(), "1 µA")

    def test_connect_waits_for_default_limit_verification(self):
        ctrl = _FakeSMUController()
        section = _SMUSection(ctrl)
        address = "GPIB0::9::INSTR"
        self._select(section._role_vbg, address)
        ctrl.is_connected = True

        with patch("ui.instrument_panel.cfg.save"):
            ctrl.connected.emit([address])

        self.assertEqual(ctrl.limit_read_calls, [])
        self.assertEqual(
            section._limit_status_by_role["Vbg"].text(),
            "Verifying...",
        )

    def test_connected_limit_status_does_not_expand_the_sidebar_cards(self):
        ctrl = _FakeSMUController()
        section = _SMUSection(ctrl)
        addresses = {
            "Vbg": "GPIB0::9::INSTR",
            "Vtg": "GPIB0::10::INSTR",
            "Vbias": "GPIB0::11::INSTR",
        }
        for role, address in addresses.items():
            self._select(section._role_combos[role], address)
        before = section.sizeHint().height()
        ctrl.is_connected = True

        with patch("ui.instrument_panel.cfg.save"):
            ctrl.connected.emit(list(addresses.values()))
            for address in addresses.values():
                ctrl.limits_result.emit(
                    "apply",
                    address,
                    {"curr": 500e-9, "curr_range": 1e-6, "volt": 21.0},
                )

        self.assertLessEqual(section.sizeHint().height(), before + 6)
        for label in section._limit_status_by_role.values():
            self.assertFalse(label.wordWrap())
            self.assertEqual(label.text(), "Applied")

    def test_compliance_follows_the_physical_address_when_selection_changes(self):
        section = _SMUSection(_FakeSMUController())
        combo = section._role_vbg
        self._select(combo, "GPIB0::9::INSTR")
        section._curr_comp_by_role["Vbg"].setValue(601.0)
        self._select(combo, "GPIB0::10::INSTR")
        section._curr_comp_by_role["Vbg"].setValue(902.0)

        combo.setCurrentText("GPIB0::9::INSTR")

        self.assertAlmostEqual(section._curr_comp_by_role["Vbg"].value(), 601.0)

    def test_manual_panel_uses_fast_step_then_reads_all_keithleys(self):
        ctrl = _FakeSMUController()
        ctrl.is_connected = True
        ctrl.available_roles = {"Vbg", "Vtg"}
        panel = _ManualControlSection(ctrl)

        self.assertAlmostEqual(panel._step_spn.value(), 0.1)
        self.assertFalse(panel._up_btn["Vbias"].isEnabled())
        ctrl.readings_ready.emit({
            "Vbg_meas": 1.2,
            "Vtg_meas": -0.2,
            "Ibg": 12.4e-9,
            "Itg": 3.1e-9,
        })
        panel._on_step("Vbg", 1.0)

        action, role, value, kwargs = ctrl.manual_calls[-1]
        self.assertEqual((action, role), ("set_fast", "Vbg"))
        self.assertAlmostEqual(value, 1.3)
        self.assertIn("ramp_step_V", kwargs)
        self.assertEqual(kwargs["delay_s"], 0.0)
        self.assertIn("+1.300 V", panel._voltage_lbl["Vbg"].text())
        self.assertTrue(panel._up_btn["Vbg"].isEnabled())

        ctrl.manual_finished.emit("set_fast", "Vbg", 1.3)

        self.assertIn("12.4 nA", panel._current_lbl["Vbg"].text())
        self.assertTrue(panel._readback_timer.isActive())

        vtg_before = panel._voltage_lbl["Vtg"].text()
        ctrl.readings_ready.emit({"Vbg_meas": 1.4, "Ibg": 13e-9})
        self.assertEqual(panel._voltage_lbl["Vtg"].text(), vtg_before)

        panel._on_debounced_readback()
        self.assertEqual(ctrl.manual_calls[-1][:2], ("read_role", "Vbg"))
        ctrl.readings_ready.emit({"Vbg_meas": 1.3, "Ibg": 14e-9})
        ctrl.manual_finished.emit("read_role", "Vbg", float("nan"))
        self.assertEqual(ctrl.manual_calls[-1][:2], ("read_role", "Vtg"))
        ctrl.readings_ready.emit({"Vtg_meas": -0.25, "Itg": 4e-9})
        ctrl.manual_finished.emit("read_role", "Vtg", float("nan"))

        self.assertIsNone(panel._background_read_active)
        self.assertEqual(panel._background_read_queue, [])
        self.assertIn("14 nA", panel._current_lbl["Vbg"].text())
        self.assertIn("4 nA", panel._current_lbl["Vtg"].text())
        self.assertTrue(panel._up_btn["Vbg"].isEnabled())

    def test_new_step_cancels_remaining_background_keithley_reads(self):
        ctrl = _FakeSMUController()
        ctrl.is_connected = True
        ctrl.available_roles = {"Vbg", "Vtg", "Vbias"}
        panel = _ManualControlSection(ctrl)
        ctrl.readings_ready.emit({
            "Vbg_meas": 1.0,
            "Vtg_meas": -0.2,
            "Vbias_meas": 0.05,
            "Ibg": 1e-9,
            "Itg": 2e-9,
            "Ibias": 3e-9,
        })

        panel._on_step("Vbg", 1.0)
        ctrl.manual_finished.emit("set_fast", "Vbg", 1.1)
        panel._on_debounced_readback()
        ctrl.manual_finished.emit("read_role", "Vbg", float("nan"))
        self.assertEqual(panel._background_read_active, "Vtg")
        self.assertEqual(panel._background_read_queue, ["Vbias"])

        panel._on_step("Vbg", 1.0)

        self.assertEqual(panel._background_read_queue, [])
        self.assertEqual(ctrl.manual_calls[-1][:2], ("set_fast", "Vbg"))
        self.assertAlmostEqual(ctrl.manual_calls[-1][2], 1.2)
        self.assertTrue(panel._up_btn["Vbg"].isEnabled())
        ctrl.manual_finished.emit("read_role", "Vtg", float("nan"))
        background_roles = [
            call[1] for call in ctrl.manual_calls if call[0] == "read_role"
        ]
        self.assertEqual(background_roles, ["Vbg", "Vtg"])

    def test_rapid_clicks_coalesce_to_latest_target_without_graying_buttons(self):
        ctrl = _FakeSMUController()
        ctrl.is_connected = True
        ctrl.available_roles = {"Vbg"}
        panel = _ManualControlSection(ctrl)
        ctrl.readings_ready.emit({"Vbg_meas": 1.0, "Ibg": 1e-9})

        panel._on_step("Vbg", 1.0)
        panel._on_step("Vbg", 1.0)
        panel._on_step("Vbg", 1.0)

        fast_calls = [call for call in ctrl.manual_calls if call[0] == "set_fast"]
        self.assertEqual(len(fast_calls), 1)
        self.assertAlmostEqual(fast_calls[0][2], 1.1)
        self.assertIn("+1.300 V", panel._voltage_lbl["Vbg"].text())
        self.assertTrue(panel._up_btn["Vbg"].isEnabled())
        ctrl.manual_finished.emit("set_fast", "Vbg", 1.1)

        fast_calls = [call for call in ctrl.manual_calls if call[0] == "set_fast"]
        self.assertEqual(len(fast_calls), 2)
        self.assertAlmostEqual(fast_calls[1][2], 1.3)
        self.assertTrue(panel._down_btn["Vbg"].isEnabled())

    def test_manual_worker_fast_set_uses_one_write_and_no_measurement(self):
        worker = _SMUWorker()
        device = _ManualDevice(measured=1.2)
        worker._device = device
        finished = []
        worker.manual_finished.connect(lambda *args: finished.append(args))

        worker.manual_control("set_fast", "Vbg", 1.3, 0.1, 0.0)

        self.assertEqual(device.pre_read_calls, [])
        self.assertEqual(device.snapshot_calls, [])
        self.assertEqual(device.ramp_calls, [])
        self.assertEqual(device.fast_set_calls, [("Vbg", 1.3, 0.0)])
        self.assertEqual(finished, [("set_fast", "Vbg", 1.3)])

    def test_manual_worker_steps_from_live_voltage(self):
        worker = _SMUWorker()
        device = _ManualDevice(measured=1.2)
        worker._device = device
        finished = []
        worker.manual_finished.connect(lambda *args: finished.append(args))

        with patch.object(worker, "_emit_role_reading") as refresh:
            worker.manual_control("step", "Vbg", 0.1, 0.05, 0.0)

        self.assertEqual(device.ramp_calls, [("Vbg", 1.3, 0.05, 0.0, 1.2)])
        refresh.assert_called_once_with("Vbg", strict=True)
        self.assertEqual(finished[0][:2], ("step", "Vbg"))

    def test_step_refreshes_only_selected_keithley_immediately(self):
        worker = _SMUWorker()
        device = _ManualDevice(roles=("Vbg", "Vtg"))
        worker._device = device
        readings = []
        worker.readings_ready.connect(readings.append)

        worker.manual_control("step", "Vbg", 0.1, 0.1, 0.0)

        self.assertEqual(device.pre_read_calls, ["Vbg"])
        self.assertEqual(device.snapshot_calls, ["Vbg"])
        self.assertEqual(len(readings), 1)
        self.assertEqual(set(readings[0]), {"Vbg_meas", "Ibg"})

    def test_read_all_refreshes_each_connected_keithley_once(self):
        worker = _SMUWorker()
        device = _ManualDevice(roles=("Vbg", "Vtg"))
        worker._device = device

        worker.manual_control("read", "", 0.0, 0.1, 0.0)

        self.assertEqual(device.snapshot_calls, ["Vbg", "Vtg"])

    def test_point_one_volt_ramp_sends_one_write_without_final_duplicate(self):
        device = object.__new__(IVDevice)
        writes = []
        device.has_role = lambda _role: True
        device._set_x_fast = lambda _role, value: writes.append(value) or True
        device._check_stop = lambda *_args: None
        device.role_map = {"Vbg": "GPIB0::9::INSTR"}
        device._operation_context = {}

        moved = device._ramp_axis_stopaware(
            "Vbg", 1.3, 0.1, 0.0, start_value=1.2
        )

        self.assertTrue(moved)
        self.assertEqual(writes, [1.3])

    def test_role_snapshot_gets_voltage_and_current_from_one_instrument_query(self):
        class _Instrument:
            address = "GPIB0::9::INSTR"

            def __init__(self):
                self.read_calls = 0

            def read_y(self):
                self.read_calls += 1

        instrument = _Instrument()
        values = {"measured_Vbg": 1.25, "Vbg_leakage": 12e-9}
        y_channels = SimpleNamespace(
            get_instrument=lambda _key: instrument,
            receive_y=lambda _key: None,
        )
        device = object.__new__(IVDevice)
        device.setup = SimpleNamespace(
            y_channel_collection=y_channels,
            get_single_y_value=lambda key: values[key],
        )
        device.has_role = lambda role: role == "Vbg"
        device._execute_io = lambda **kwargs: kwargs["action"]()
        device._update_x_cache = lambda *_args: None

        voltage, current = device.read_role_snapshot("Vbg", strict=True)

        self.assertEqual(instrument.read_calls, 1)
        self.assertAlmostEqual(voltage, 1.25)
        self.assertAlmostEqual(current, 12e-9)

    def test_manual_worker_never_writes_when_live_read_fails(self):
        worker = _SMUWorker()
        device = _ManualDevice(read_error=TimeoutError("read timed out"))
        worker._device = device
        errors = []
        worker.manual_error.connect(errors.append)

        worker.manual_control("step", "Vbg", -0.1, 0.05, 0.0)

        self.assertEqual(device.ramp_calls, [])
        self.assertIn("read timed out", errors[0])

    def test_session_restores_step_and_per_address_compliance_without_io(self):
        ctrl = _FakeSMUController()
        panel = InstrumentPanel(smu_ctrl=ctrl)
        smu = panel._sections["smu"]
        manual = panel._sections["manual_smu"]
        self._select(smu._role_vbg, "GPIB0::9::INSTR")
        smu._curr_comp_by_role["Vbg"].setValue(700.0)
        smu._volt_comp_by_role["Vbg"].setValue(25.0)
        manual._step_spn.setValue(0.25)
        state = panel.capture_session_state()

        restored_ctrl = _FakeSMUController()
        restored = InstrumentPanel(smu_ctrl=restored_ctrl)
        restored.restore_session_state(state)
        restored_smu = restored._sections["smu"]
        restored_manual = restored._sections["manual_smu"]

        self.assertAlmostEqual(
            restored_smu._curr_comp_by_role["Vbg"].value(), 700.0
        )
        self.assertAlmostEqual(
            restored_smu._volt_comp_by_role["Vbg"].value(), 25.0
        )
        self.assertAlmostEqual(
            restored_smu._curr_range_by_role["Vbg"].currentData(), 1e-6
        )
        self.assertAlmostEqual(restored_manual._step_spn.value(), 0.25)
        self.assertEqual(restored_ctrl.connect_calls, [])
        self.assertEqual(restored_ctrl.manual_calls, [])


if __name__ == "__main__":
    unittest.main()
