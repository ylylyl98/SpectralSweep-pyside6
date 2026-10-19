from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from app.devices.aps100_attodry1000_adapter import (
    APS100AttoDry1000Adapter, APS100SafetyError, MockAPS100Adapter,
)
from app.devices.aps100_field_policy import APS100FieldLimits
from controllers.magnet_controller import MagnetController, _MagnetWorker
from tests.test_aps100_adapter import _InstantSweepAPS100Resource
from tests.test_mcd_workflow import (
    _EmptyController, _MagnetController, _PanelMagnetController, _workflow_params,
)
from ui.mcd_panel import MCDPanel, _ContinuousMCDWorker
from utils.config import AppConfig, MagnetConfig, cfg


class APS100FieldLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config_patch = patch.object(cfg, "magnet", MagnetConfig())
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def real_adapter(self, field=0.0, heater=True):
        resource = _InstantSweepAPS100Resource()
        resource.field_kg = field * 10
        resource.output_kg = field * 10 if heater else 0.0
        resource.heater = int(heater)
        adapter = APS100AttoDry1000Adapter(
            visa_resource=resource, heater_warm_s=0.0, heater_cool_s=0.0,
        )
        adapter.connect()
        return adapter, resource

    def panel(self, field=0.0, heater=True):
        controller = _PanelMagnetController()
        controller.is_connected = True
        panel = MCDPanel(controller, _EmptyController(), _EmptyController(), _EmptyController())
        self.addCleanup(panel.close)
        adapter = MockAPS100Adapter()
        adapter.connect()
        adapter._field_t = field
        adapter._output_t = field if heater else 0.0
        adapter._heater = heater
        panel._on_snapshot(adapter.read_snapshot())
        return panel, controller, adapter

    def test_policy_boundaries_and_invalid_targets(self):
        limits = APS100FieldLimits()
        for sign in (-1, 1):
            self.assertEqual(limits.validate_target(sign * 9, "persistent"), sign * 9)
            self.assertEqual(limits.validate_target(sign * 8, "driven"), sign * 8)
            for target, mode in ((8.0001, "driven"), (9.0001, "persistent")):
                with self.subTest(sign=sign, target=target, mode=mode):
                    with self.assertRaises(ValueError):
                        limits.validate_target(sign * target, mode)
        for target in (float("nan"), float("inf"), -float("inf"), True):
            with self.assertRaises(ValueError):
                limits.validate_target(target, "persistent")
        with self.assertRaises(ValueError):
            limits.validate_target(1, "unknown")
        self.assertEqual(APS100FieldLimits(20, 20, 20, 20).mcd_limit, 8)
        self.assertEqual(APS100FieldLimits(20, 20, 20, 20).manual_limit("persistent"), 9)
        self.assertEqual(APS100FieldLimits(7, 6, 6.5).manual_limit("persistent"), 6.5)

    def test_old_config_and_new_persistent_limit_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"magnet": {"safe_control_max_field_t": 7}}))
            config = AppConfig()
            config.load(path)
            limits = APS100FieldLimits.from_config(config.magnet)
            self.assertEqual(limits.manual_limit("driven"), 7)
            self.assertEqual(limits.manual_limit("persistent"), 9)
            self.assertTrue(config.magnet.fast_leads_enabled)
            config.magnet.persistent_control_max_field_t = 8.5
            config.magnet.fast_leads_enabled = False
            config.save(path)
            restored = AppConfig()
            restored.load(path)
            self.assertEqual(restored.magnet.persistent_control_max_field_t, 8.5)
            self.assertFalse(restored.magnet.fast_leads_enabled)

    def test_real_adapter_reaches_both_nine_t_persistent_then_zeros_leads(self):
        for target in (-9.0, 9.0):
            with self.subTest(target=target):
                adapter, resource = self.real_adapter()
                result = adapter.safe_move_to_field(
                    target, final_mode="persistent", settle_s=0,
                    max_magnet_voltage_v=0.1, timeout_s=2,
                )
                self.assertEqual(result.field_t, target)
                self.assertFalse(result.heater_on)
                self.assertEqual(result.output_field_t, 0)
                ramp = "SWEEP DOWN SLOW" if target < 0 else "SWEEP UP SLOW"
                self.assertLess(resource.commands.index(ramp), resource.commands.index("PSHTR OFF"))
                self.assertLess(resource.commands.index("PSHTR OFF"), resource.commands.index("SWEEP ZERO SLOW"))
                self.assertFalse(any(c.startswith("RATE ") for c in resource.commands))

    def test_invalid_manual_move_is_rejected_before_hardware_writes(self):
        for target, mode in ((9, "driven"), (-9, "driven"), (9.01, "persistent")):
            adapter, resource = self.real_adapter()
            before = list(resource.commands)
            with self.assertRaises(APS100SafetyError):
                adapter.safe_move_to_field(target, final_mode=mode)
            self.assertEqual(resource.commands, before)
            mock = MockAPS100Adapter()
            mock.connect()
            with self.assertRaises(APS100SafetyError):
                mock.safe_move_to_field(target, final_mode=mode)
            self.assertFalse(mock._remote)

    def test_standalone_driven_at_nine_t_rejected_but_controlled_return_allowed(self):
        for sign in (-1, 1):
            adapter, resource = self.real_adapter(sign * 9, heater=False)
            with self.assertRaises(APS100SafetyError):
                adapter.enter_driven_mode()
            self.assertNotIn("PSHTR ON", resource.commands)
            result = adapter.safe_move_to_field(
                sign * 8, final_mode="driven", persistent_field_confirmed=True,
                settle_s=0, timeout_s=2,
            )
            self.assertEqual(result.field_t, sign * 8)
            self.assertTrue(result.heater_on)
            match = "SWEEP DOWN SLOW" if sign < 0 else "SWEEP UP SLOW"
            self.assertLess(resource.commands.index(match), resource.commands.index("PSHTR ON"))

    def test_return_from_nine_t_to_persistent_zero(self):
        for target in (-9, 9):
            adapter, _ = self.real_adapter(target, heater=False)
            result = adapter.safe_move_to_field(
                0, final_mode="persistent", persistent_field_confirmed=True,
                max_magnet_voltage_v=0.1, settle_s=0, timeout_s=2,
            )
            self.assertFalse(result.heater_on)
            self.assertEqual(result.field_t, 0)
            self.assertEqual(result.output_field_t, 0)

    def test_stop_during_cooling_does_not_report_completion_or_zero_leads(self):
        adapter, resource = self.real_adapter()
        worker = _MagnetWorker()
        worker.adapter = adapter
        errors, finished, snapshots, audits = [], [], [], []
        worker.error.connect(errors.append)
        worker.operation_finished.connect(finished.append)
        worker.snapshot_updated.connect(snapshots.append)
        worker.safe_move_audit.connect(audits.append)
        worker.transition_progress.connect(
            lambda label, value: worker.request_stop() if label == "heater cooling" else None
        )
        worker.safe_move_to_field(9, "persistent", True, False, 0, 2)
        self.assertEqual(finished, [])
        self.assertIn("incomplete", errors[0])
        self.assertEqual(audits[0]["outcome"], "failed")
        self.assertEqual(snapshots[-1].field_t, 9)
        self.assertFalse(snapshots[-1].heater_on)
        self.assertEqual(snapshots[-1].output_field_t, 9)
        self.assertFalse(any(c.startswith("SWEEP ZERO") for c in resource.commands))

    def test_stop_during_high_field_warming_leaves_observed_driven_state(self):
        adapter, resource = self.real_adapter(9, heater=False)
        stop = threading.Event()
        with self.assertRaisesRegex(APS100SafetyError, "stopped"):
            adapter.safe_move_to_field(
                8, final_mode="driven", persistent_field_confirmed=True,
                stop_event=stop, settle_s=0, timeout_s=2,
                progress=lambda label, value: stop.set() if label == "heater warming" else None,
            )
        self.assertEqual(adapter.read_snapshot().field_t, 9)
        self.assertTrue(adapter.read_snapshot().heater_on)
        self.assertNotIn("PSHTR OFF", resource.commands)

    def test_controller_rejects_driven_nine_but_dispatches_persistent_nine(self):
        controller = MagnetController()
        self.addCleanup(controller.shutdown)
        controller._safe_move_requested.disconnect()
        requests, errors = [], []
        controller._safe_move_requested.connect(lambda *args: requests.append(args))
        controller.error.connect(errors.append)
        for target in (-9, 9):
            controller.safe_move_to_field(target, final_mode="driven")
            controller.safe_move_to_field(target, final_mode="persistent")
        self.assertEqual(len(errors), 2)
        self.assertEqual([(r[0], r[1]) for r in requests], [(-9, "persistent"), (9, "persistent")])

    def test_panel_mode_changes_target_range_and_keeps_mcd_at_eight(self):
        panel, controller, _ = self.panel()
        self.assertEqual(panel._safe_target.maximum(), 9)
        panel._safe_target.setValue(9)
        with patch("ui.mcd_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            panel._start_safe_magnet_move()
        self.assertEqual(controller.safe_move_call[0], 9)
        self.assertFalse(panel._driven_btn.isEnabled())
        panel._on_magnet_operation("safe_move:persistent:+9.000000")
        panel._safe_final_mode.setCurrentIndex(panel._safe_final_mode.findData("driven"))
        self.assertEqual(panel._safe_target.maximum(), 8)
        self.assertIn("Target adjusted", panel._safe_limit.text())
        self.assertEqual(panel._start_t.minimum(), -8)
        self.assertEqual(panel._stop_t.maximum(), 8)

    def test_panel_blocks_enter_driven_at_nine_and_explains_return(self):
        panel, controller, _ = self.panel(9, heater=False)
        with patch("ui.mcd_panel.QMessageBox.warning") as warning:
            panel._enter_driven()
        warning.assert_called_once()
        self.assertFalse(hasattr(controller, "safe_move_call"))
        self.assertIn("return within", panel._mode_notice.text())

    def test_restore_persistent_nine_from_driven_ui_preserves_target(self):
        panel, _, _ = self.panel()
        panel._safe_final_mode.setCurrentIndex(panel._safe_final_mode.findData("driven"))
        panel.restore_session_state({"safe_final_mode": "persistent", "safe_target_t": 9})
        self.assertEqual(panel._safe_target.value(), 9)
        self.assertEqual(panel._safe_final_mode.currentData(), "persistent")

    def test_final_field_cannot_exceed_mode_limit_within_settling_tolerance(self):
        for mode, field, target in (("driven", 8.001, 8), ("persistent", 9.001, 9)):
            adapter, _ = self.real_adapter(field, heater=mode == "driven")
            with self.assertRaises(APS100SafetyError):
                adapter.safe_move_to_field(target, final_mode=mode, settle_s=0, zero_leads=False)

    def test_worker_revalidates_before_mutating_instrument(self):
        adapter, resource = self.real_adapter()
        worker = _MagnetWorker()
        worker.adapter = adapter
        errors, completed = [], []
        worker.error.connect(errors.append)
        worker.operation_finished.connect(completed.append)
        worker.safe_move_to_field(9, "driven", False, False, 0, 2)
        self.assertEqual(completed, [])
        self.assertIn("exceeds", errors[0])
        self.assertFalse(any(c.startswith(("SWEEP ", "PSHTR ", "REMOTE")) for c in resource.commands))

    def test_periodic_snapshot_preserves_incomplete_transition(self):
        panel, _, adapter = self.panel(9)
        panel._safe_magnet_operation_active = True
        panel._on_magnet_error("User stopped during warming")
        panel._on_snapshot(adapter.read_snapshot())
        self.assertIn("INCOMPLETE", panel._safe_state.text())
        self.assertIn("Driven +9", panel._safe_state.text())
        self.assertNotIn("ready", panel._mode_notice.text().lower())
        panel._on_magnet_operation("safe_move:persistent:+9.000000")
        self.assertEqual(panel._safe_move_incomplete, "")

    def test_mcd_worker_rejects_targets_and_live_field_above_eight(self):
        adapter = MockAPS100Adapter()
        adapter.connect()
        with tempfile.TemporaryDirectory() as directory:
            for target in (-8.0001, 8.0001, float("nan")):
                params = _workflow_params(directory)
                params["stop_t"] = target
                worker = _ContinuousMCDWorker(
                    params, _MagnetController(adapter), _EmptyController(),
                    _EmptyController(), _EmptyController(),
                )
                with self.assertRaisesRegex(ValueError, "MCD fields"):
                    worker._run()
                self.assertFalse(adapter._remote)
            for field in (-8.01, 8.01):
                adapter._field_t = adapter._output_t = field
                snapshot = adapter.read_snapshot()
                with self.assertRaisesRegex(RuntimeError, "exceeds"):
                    worker._validate_magnet_snapshot(snapshot)
                with self.assertRaisesRegex(RuntimeError, "exceeds"):
                    worker._check_field_faults(field, snapshot.status, heater_on=True)


if __name__ == "__main__":
    unittest.main()
