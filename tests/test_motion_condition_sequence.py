import csv
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path
import numpy as np
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from tests.test_motion_sweep import _FakeStageController, _FakeRotationController, _FakeLF6Controller


class _FakeSMUDevice:
    def __init__(self):
        self.vtg = self.vbg = self.vbias = 0.0
        self.calls = []

    def set_gates(self, **kwargs):
        self.calls.append(("gates", kwargs))
        self.vtg, self.vbg = float(kwargs["Vtg"]), float(kwargs["Vbg"])

    def set_bias(self, **kwargs):
        self.calls.append(("bias", kwargs))
        self.vbias = float(kwargs["Vbias"])

    def read_current_gates(self):
        return self.vbg, self.vtg

    def read_current_bias(self):
        return self.vbias

    def ramp_all_to_zero(self, **kwargs):
        self.calls.append(("zero", kwargs))


class _FakeSMU:
    is_connected = True

    def __init__(self):
        self.device = _FakeSMUDevice()


class _FailingLF6(_FakeLF6Controller):
    class _Spectrum:
        def calibration_wavelengths(self, force=False):
            return np.array([700.0, 701.0, 702.0])

        def acquire(self):
            raise RuntimeError("camera failure")

    def __init__(self):
        super().__init__()
        self.adapter = self._Spectrum()
from ui.power_sweep_panel import _PowerSweepWorker, _StopRequested
from utils.motion_conditions import expand_sequence

class MotionConditionSequenceTests(unittest.TestCase):
    def test_invalid_restore_target_fails_before_transitions(self):
        for value in (-0.2, 3600.2, float("nan")):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                stage, smu = _FakeStageController(), _FakeSMU()
                stage.adapter.position = value
                p = self._params(tmp, return_motion_to_start=True)
                with self.assertRaisesRegex(RuntimeError, "initial motion positions"):
                    _PowerSweepWorker(p, stage, None, None, _FakeLF6Controller(), smu)._run_sweep(p)
                self.assertEqual(stage.adapter.moves, [])
                self.assertEqual(smu.device.calls, [])

    def test_normalized_restore_is_recorded_and_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = _FakeStageController()
            stage.adapter.position = -0.001
            stage.adapter.normalize_restore_position = lambda value: 0.0
            p = self._params(tmp, return_motion_to_start=True)
            _PowerSweepWorker(p, stage, None, None, _FakeLF6Controller(), _FakeSMU())._run_sweep(p)
            manifest = json.loads(next(Path(tmp).glob("*manifest.json")).read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["initial_motion_positions"], {"stage": -0.001})
            self.assertEqual(manifest["restore_targets"], {"stage": 0.0})
            self.assertEqual(stage.adapter.moves[-1], 0.0)

    def test_rotation_retries_read_exception(self):
        rot = _FakeRotationController()
        rot.adapter("rot2").get_position = Mock(side_effect=[20.0, 20.0, OSError("temporary read failure"), 23.9992, 23.9992])
        worker = _PowerSweepWorker({}, None, rot, None, None, None)
        last = worker._sequence_apply_rotations({"rotation_settle_s": 0}, {"rot2": 24.0}, {})
        self.assertEqual(last["rot2_actual"], 23.9992)
        self.assertEqual(rot.adapter("rot2").moves, [24.0])

    def test_gate_readback_recovers_without_reramping(self):
        for bad in (float("nan"), float("inf"), 2.63):
            with self.subTest(bad=bad), patch("ui.power_sweep_panel._cancelable_sleep"):
                smu = _FakeSMU()
                worker = _PowerSweepWorker({}, None, None, None, None, smu)
                worker._sequence_gate_readback = Mock(side_effect=[
                    {"Vbg_observed": bad, "Vtg_observed": 0.0, "Vbias_observed": 0.0},
                    {"Vbg_observed": 2.633, "Vtg_observed": 0.0, "Vbias_observed": 0.0},
                ])
                result = worker._sequence_verify_gates((2.63333333333, 0.0, 0.0))
                self.assertEqual(result["Vbg_observed"], 2.633)
                self.assertEqual(smu.device.calls, [])

    def test_gate_invalid_readback_exhausts_retries(self):
        worker = _PowerSweepWorker({}, None, None, None, None, _FakeSMU())
        worker._sequence_gate_readback = Mock(return_value={})
        with patch("ui.power_sweep_panel._cancelable_sleep"):
            with self.assertRaisesRegex(RuntimeError, "nonfinite.*5 readback retries"):
                worker._sequence_verify_gates((0.0, 0.0, 0.0))
        self.assertEqual(worker._sequence_gate_readback.call_count, 6)

    def test_gate_readback_stop_interrupts_retry(self):
        worker = _PowerSweepWorker({}, None, None, None, None, _FakeSMU())
        worker._sequence_gate_readback = Mock(return_value={})
        worker.log.connect(lambda message: worker._stop.set())
        with self.assertRaises(_StopRequested):
            worker._sequence_verify_gates((0.0, 0.0, 0.0))
        self.assertEqual(worker._sequence_gate_readback.call_count, 1)

    def test_unmapped_zero_bias_does_not_require_readback(self):
        smu = _FakeSMU()
        smu.device.has_role = lambda role: role != "Vbias"
        smu.device.read_current_bias = lambda: None
        worker = _PowerSweepWorker({}, None, None, None, None, smu)
        worker._sequence_verify_gates((0.0, 0.0, 0.0))
        with patch("ui.power_sweep_panel._cancelable_sleep"):
            with self.assertRaisesRegex(RuntimeError, "Vbias_observed is unavailable"):
                worker._sequence_verify_gates((0.0, 0.0, 1.0))

    def test_rotation_accepts_small_readback_error_and_preserves_actual(self):
        rot = _FakeRotationController()
        rot.adapter("rot2").get_position = lambda: 23.9992
        worker = _PowerSweepWorker({}, None, rot, None, None, None)
        last = worker._sequence_apply_rotations(
            {"rotation_settle_s": 0}, {"rot2": 24.0}, {}
        )
        self.assertEqual(last["rot2"], 24.0)
        self.assertEqual(last["rot2_actual"], 23.9992)
        self.assertEqual(rot.adapter("rot2").moves, [24.0])

    def test_rotation_rejects_large_or_nonfinite_readback_error(self):
        for observed in (23.98, 24.02, float("nan"), float("inf")):
            with self.subTest(observed=observed):
                rot = _FakeRotationController()
                rot.adapter("rot2").get_position = lambda: observed
                worker = _PowerSweepWorker({}, None, rot, None, None, None)
                last = {}
                with self.assertRaisesRegex(RuntimeError, "tolerance 0.01|no genuine readback"):
                    worker._sequence_apply_rotations(
                        {"rotation_settle_s": 0}, {"rot2": 24.0}, last
                    )
                self.assertEqual(rot.adapter("rot2").moves, [24.0])
                self.assertEqual(last, {})

    def test_rotation_read_glitch_recovers_without_reissuing_move(self):
        rot = _FakeRotationController()
        readings = iter([20.0, 20.0, OSError("read glitch"), 23.9992, 23.9992])
        def read():
            value = next(readings)
            if isinstance(value, Exception):
                raise value
            return value
        rot.adapter("rot2").get_position = read
        worker = _PowerSweepWorker({}, None, rot, None, None, None)
        last = worker._sequence_apply_rotations(
            {"rotation_settle_s": 0}, {"rot2": 24.0}, {}
        )
        self.assertEqual(rot.adapter("rot2").moves, [24.0])
        self.assertEqual(last, {"rot2": 24.0, "rot2_actual": 23.9992})

    def test_stop_during_readback_prevents_further_moves_and_restore(self):
        rot = _FakeRotationController()
        adapter = rot.adapter("rot2")
        worker = _PowerSweepWorker({}, None, rot, None, None, None)
        def read():
            if adapter.moves:
                worker._stop.set()
            return 23.98
        adapter.get_position = read
        with self.assertRaises(_StopRequested):
            worker._sequence_apply_rotations(
                {"rotation_settle_s": 0}, {"rot2": 24.0}, {}
            )
        worker._sequence_cleanup_errors = []
        worker._restore_motion_positions({"rot2": 12.0})
        self.assertEqual(adapter.moves, [24.0])
        self.assertIn("previous motion is unconfirmed", worker._sequence_cleanup_errors[-1])

    def _params(self, tmp, **overrides):
        params = {
            "positions": [1.0], "motion_key": "stage", "motion_settle_s": 0,
            "rotation_settle_s": 0, "conditions": [{"vtg_v": 1.0, "vbg_v": 2.0, "vbias_v": 0.0,
            "doping_v": 3.0, "efield_v": -1.0}], "condition_sequence": [{"condition_index": 0}],
            "center_nm": 730, "exp_ms": 10, "frames": 1, "pm_wl_nm": 730,
            "out_path": tmp, "base_name": "batch", "apply_gates": True,
            "ramp_step_V": 0.1, "settle_s": 0, "return_to_zero": False,
            "return_motion_to_start": False, "rotation_axes": [], "rotation_values": {},
        }
        params.update(overrides)
        return params

    def test_keep_current_axes_need_no_rotation_controller(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._params(tmp)
            stage, lf6, smu = _FakeStageController(), _FakeLF6Controller(), _FakeSMU()
            _PowerSweepWorker(p, stage, None, None, lf6, smu)._run_sweep(p)
            self.assertEqual(len(list(x for x in os.scandir(tmp) if x.name.endswith(".csv"))), 1)

    def test_apply_gates_off_is_rejected_before_hardware(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._params(tmp, apply_gates=False)
            stage = _FakeStageController()
            with self.assertRaises(ValueError):
                _PowerSweepWorker(p, stage, None, None, _FakeLF6Controller(), None)._run_sweep(p)
            self.assertEqual(stage.adapter.moves, [])

    def test_sequence_accepts_voltage_command_rounding(self):
        for requested in (2.633333333333, -2.633333333333, 0.00049, 2.63351):
            with self.subTest(requested=requested), tempfile.TemporaryDirectory() as tmp:
                condition = {"vtg_v": requested, "vbg_v": requested, "vbias_v": requested}
                p = self._params(tmp, conditions=[condition])
                smu = _FakeSMU()
                smu.device.read_current_gates = lambda: (
                    float("%.3f" % smu.device.vbg), float("%.3f" % smu.device.vtg)
                )
                smu.device.read_current_bias = lambda: float("%.3f" % smu.device.vbias)
                _PowerSweepWorker(p, _FakeStageController(), None, None,
                                  _FakeLF6Controller(), smu)._run_sweep(p)
                manifest = json.loads(next(Path(tmp).glob("*manifest.json")).read_text(encoding="utf-8"))
                self.assertEqual(manifest["status"], "complete")
                self.assertEqual(condition["vbg_v"], requested)

    def test_sequence_rejects_real_voltage_mismatch_before_acquisition(self):
        for role in ("Vbg", "Vtg", "Vbias"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                p = self._params(tmp, conditions=[{
                    "vtg_v": 2.633333333333, "vbg_v": 2.633333333333, "vbias_v": 2.633333333333
                }])
                smu = _FakeSMU()
                smu.device.read_current_gates = lambda: (
                    2.63 if role == "Vbg" else 2.633,
                    2.63 if role == "Vtg" else 2.633,
                )
                smu.device.read_current_bias = lambda: 2.63 if role == "Vbias" else 2.633
                with self.assertRaisesRegex(RuntimeError, f"{role}_observed.*commanded 2.633 V"):
                    _PowerSweepWorker(p, _FakeStageController(), None, None,
                                      _FakeLF6Controller(), smu)._run_sweep(p)
                manifest = json.loads(next(Path(tmp).glob("*manifest.json")).read_text(encoding="utf-8"))
                self.assertEqual(manifest["status"], "failed")
                self.assertEqual(list(Path(tmp).glob("*.csv")), [])

    def test_acquisition_failure_marks_manifest_failed_and_links_partial_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._params(tmp, condition_sequence=[{"condition_index": 0}, {"condition_index": 0}],
                             base_name="failure")
            with self.assertRaises(RuntimeError):
                _PowerSweepWorker(p, _FakeStageController(), None, None, _FailingLF6(), _FakeSMU())._run_sweep(p)
            manifest = json.loads(next(Path(tmp).glob("*manifest.json")).read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertTrue(manifest["conditions"][0]["file"].endswith(".csv"))
            self.assertEqual(manifest["conditions"][1]["status"], "not_started")

    def test_initial_motion_snapshot_failure_finalizes_manifest_before_transitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._params(tmp, base_name="snapshot")
            stage = _FakeStageController()

            def fail_snapshot():
                raise RuntimeError("position read failed")

            stage.adapter.get_position = fail_snapshot
            with self.assertRaisesRegex(RuntimeError, "initial motion positions"):
                _PowerSweepWorker(p, stage, None, None, _FakeLF6Controller(), _FakeSMU())._run_sweep(p)
            manifest = json.loads(next(Path(tmp).glob("*manifest.json")).read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertTrue(all(row["status"] == "not_started" for row in manifest["conditions"]))
            self.assertEqual(stage.adapter.moves, [])

    def test_two_axis_repeats_preserve_order_and_cap_before_expansion(self):
        rows = expand_sequence([{}], {"rot1": [10.0, 20.0], "rot2": [1.0, 2.0]}, repeats=2)
        self.assertEqual(len(rows), 8)
        self.assertEqual([(r["rot1_index"], r["rot2_index"], r["repeat"]) for r in rows[:4]],
                         [(0, 0, 1), (0, 0, 2), (0, 1, 1), (0, 1, 2)])
        with self.assertRaises(ValueError):
            expand_sequence([{}] * 101, {"rot1": list(range(100)), "rot2": [1]}, maximum_entries=100)

    def test_sequence_writes_each_condition_and_prepopulates_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            p={'positions':[1.0,2.0],'motion_key':'stage','motion_settle_s':0,'rotation_settle_s':0,
               'rotation_axis':'rot1','rotation_values':[10.0,20.0],'conditions':[{'vtg_v':0,'vbg_v':0,'vbias_v':0,'doping_v':0,'efield_v':0}],
               'condition_sequence':[{'condition_index':0,'rotation_index':0,'repeat':1},{'condition_index':0,'rotation_index':1,'repeat':1}],
               'center_nm':730,'exp_ms':10,'frames':1,'pm_wl_nm':730,'out_path':tmp,'base_name':'batch',
               'apply_gates':True,'return_to_zero':False,'return_motion_to_start':True,
               'ramp_step_V':0.1,'settle_s':0}
            p['power_correction_factor'] = 1.0
            stage=_FakeStageController(); rot=_FakeRotationController(); lf6=_FakeLF6Controller(); smu = _FakeSMU()
            _PowerSweepWorker(p,stage,rot,None,lf6,smu)._run_sweep(p)
            manifest_path=next(x.path for x in os.scandir(tmp) if x.name.endswith('manifest.json'))
            with open(manifest_path, encoding='utf-8') as fh: manifest=json.load(fh)
            self.assertEqual(manifest['status'],'complete'); self.assertEqual(len(manifest['conditions']),2)
            self.assertTrue(all(e['file'] for e in manifest['conditions']))
            self.assertEqual(len(list(x for x in os.scandir(tmp) if x.name.endswith('.csv'))),2)
            self.assertEqual([x[0] for x in smu.device.calls], ['gates', 'bias'])
            self.assertEqual(stage.adapter.moves, [1.0, 2.0, 1.0, 2.0, 7.0])

if __name__=='__main__': unittest.main()
