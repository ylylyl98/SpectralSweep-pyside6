"""Independent behavioral checks for nested Dual Gate acquisition."""
import csv
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from ui.presets_panel import _build_nested_execution_schedule, _build_plan, _RunWorker, _validate_safe_jumps
from tests import test_smu_resilience as fixtures
from tests.test_smu_resilience import _HealthyRunDevice, _FakeLFController
from utils.config import cfg

class _Axis:
    def __init__(self, name, events):
        self.name, self.events, self.position = name, events, 0.
    def move_to(self, value):
        self.position = value
        self.events.append((self.name, value))
    def get_position(self):
        return self.position

class _Device(_HealthyRunDevice):
    def __init__(self, events):
        super().__init__()
        self.events, self.gates = events, (0., 0.)
    def set_gates(self, **kwargs):
        self.gates = kwargs['Vbg'], kwargs['Vtg']
        self.events.append(('gate', self.gates))
    def read_current_gates(self, **kwargs):
        return self.gates

class DualGateInterleavingRegressionTests(unittest.TestCase):
    def plan(self, *, repeats=2, order=None, measure_power=False, angles='0, 90'):
        loops = pd.DataFrame([
            dict(Enable=True, Parameter='Stage Position', Values='1, 2', Group=1),
            dict(Enable=True, Parameter='Rotation1 Angle (deg)', Values=angles, Group=2),
        ])
        batch = pd.DataFrame([fixtures.SMUResilienceTests._batch_row(frames=3, repeat=repeats, Vbg_stop=2, MeasurePower=measure_power)])
        schedule = _build_nested_execution_schedule(loops, batch, mode='Customized',
            execution_order=order or ['group:1', 'conditions', 'points', 'group:2'])
        sequence, batch, _ = _build_plan(loops, batch, 'Customized')
        return sequence, batch, schedule

    def run_worker(self, folder, *, cancel_after=None, connected=True, order=None, measure_power=False, angles='0, 90'):
        sequence, batch, schedule = self.plan(order=order, measure_power=measure_power, angles=angles)
        events, finished, progress, frames = [], [], [], []
        stop = threading.Event()
        stage, rotation = _Axis('stage', events), _Axis('rotation', events)
        device = _Device(events)
        lf = _FakeLFController()
        lf.configure_for_acquisition = lambda **settings:events.append(('configure',settings))
        original_acquire = lf.adapter.acquire
        acquired = []
        def acquire():
            events.append(('acquire', (stage.position, device.gates, rotation.position)))
            acquired.append(events[-1][1])
            data = original_acquire()
            if cancel_after is not None and len(acquired) >= cancel_after:
                stop.set()
            return data
        lf.adapter.acquire = acquire
        metadata = fixtures.SMUResilienceTests._run_meta()
        metadata.update(initial_voltage_settle_s=0., voltage_settle_s=0., power_correction_factor=1., measurement_mode='Ref')
        worker = _RunWorker(sequence, batch, lf6_ctrl=lf,
            smu_ctrl=SimpleNamespace(is_connected=True, device=device),
            rotation_ctrl=SimpleNamespace(is_connected=lambda slot:connected, adapter=lambda slot:rotation),
            stage_ctrl=SimpleNamespace(is_connected=True, adapter=stage),
            pm_ctrl=SimpleNamespace(is_connected=True, adapter=SimpleNamespace(get_power=lambda:(rotation.position+1.)*1e-6)),
            out_dir=Path(folder), run_meta=metadata,
            filename_parts=['stage_position','rotation1'], stop_event=stop,
            acquisition_schedule=schedule)
        worker.finished.connect(lambda *args:finished.append(args))
        worker.progress.connect(lambda *args:progress.append(args))
        worker.frame_progress.connect(lambda *args:frames.append(args))
        with patch.object(cfg.ramp,'delay_s',0.), patch.object(cfg.ramp,'settle_s',0.):
            worker.run()
        return events, finished, progress, frames, acquired

    def test_interleaving_preserves_sweep_files_repetitions_and_axis_positions(self):
        with tempfile.TemporaryDirectory() as folder:
            events, finished, progress, frames, acquired = self.run_worker(folder)
            self.assertTrue(finished and finished[-1][0], finished)
            paths = list(Path(folder).glob('*.csv'))
            self.assertEqual(len(paths), 8)
            for path in paths:
                with path.open(newline='', encoding='utf-8-sig') as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual([float(row['Vbg_set']) for row in rows], [0.,1.,2.], path.name)
            expected = [(stage, (gate,0.), angle)
                for stage in (1.,2.) for repeat in range(2)
                for gate in (0.,1.,2.) for angle in (0.,90.)]
            self.assertEqual(acquired, expected)
            self.assertEqual([event[1] for event in events if event[0]=='stage'], [1.,2.])
            self.assertEqual(len([event for event in events if event[0]=='gate']), 12)
            self.assertEqual(frames[-1], (24,24))
            self.assertEqual(progress[-1], (8,8))

    def test_inner_angle_is_applied_after_gate_point(self):
        with tempfile.TemporaryDirectory() as folder:
            events, finished, *_ = self.run_worker(folder)
            self.assertTrue(finished and finished[-1][0], finished)
            first_stage = next(i for i,e in enumerate(events) if e[0]=='stage')
            first_gate = next(i for i,e in enumerate(events) if e[0]=='gate')
            first_rotation = next(i for i,e in enumerate(events) if e[0]=='rotation')
            first_acquire = next(i for i,e in enumerate(events) if e[0]=='acquire')
            self.assertLess(first_stage, first_gate)
            self.assertLess(first_gate, first_rotation)
            self.assertLess(first_rotation, first_acquire)

    def test_group_order_is_respected_on_both_sides_of_gate_points(self):
        orders = [
            ['group:1','group:2','conditions','points'],
            ['conditions','points','group:1','group:2'],
        ]
        for order in orders:
            with self.subTest(order=order), tempfile.TemporaryDirectory() as folder:
                events, finished, *_ = self.run_worker(folder, order=order)
                self.assertTrue(finished and finished[-1][0], finished)
                first_stage = next(i for i,e in enumerate(events) if e[0]=='stage')
                first_rotation = next(i for i,e in enumerate(events) if e[0]=='rotation')
                self.assertLess(first_stage, first_rotation)

    def test_ref_measured_power_belongs_to_the_applied_inner_angle(self):
        with tempfile.TemporaryDirectory() as folder:
            events, finished, *_ = self.run_worker(folder, measure_power=True)
            self.assertTrue(finished and finished[-1][0], finished)
            paths = list(Path(folder).glob('*.csv'))
            self.assertEqual(len(paths), 8)
            for path in paths:
                expected = 91. if 'RotIn90deg' in path.name else 1.
                self.assertIn(f'{expected:.3f}uW', path.name)
                with path.open(newline='', encoding='utf-8-sig') as stream:
                    rows = list(csv.DictReader(stream))
                self.assertTrue(rows)
                for row in rows:
                    self.assertAlmostEqual(float(row['Power_uW']), expected)

    def test_repeated_equal_angle_values_keep_distinct_sweep_files(self):
        with tempfile.TemporaryDirectory() as folder:
            events, finished, *_ = self.run_worker(folder, angles='0, 90, 0')
            self.assertTrue(finished and finished[-1][0], finished)
            paths = list(Path(folder).glob('*.csv'))
            self.assertEqual(len(paths), 12)
            for path in paths:
                with path.open(newline='', encoding='utf-8-sig') as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual([float(row['Vbg_set']) for row in rows], [0.,1.,2.], path.name)

    def test_nested_plan_without_spectrometer_loop_prepares_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            events, finished, *_ = self.run_worker(folder)
            self.assertTrue(finished and finished[-1][0], finished)
            prepared = [i for i,event in enumerate(events) if event[0]=='configure']
            self.assertTrue(prepared, 'Captured/default spectrometer settings were never prepared')
            self.assertLess(prepared[0], next(i for i,event in enumerate(events) if event[0]=='acquire'))

    def test_safety_validation_allows_ramped_repeat_resets_but_rejects_large_point_steps(self):
        for frames, expected_safe in [(5, True), (2, False)]:
            with self.subTest(frames=frames):
                loop = pd.DataFrame()
                batch = pd.DataFrame([fixtures.SMUResilienceTests._batch_row(
                    frames=frames, repeat=2, Vbg_start=-1, Vbg_stop=1)])
                schedule = _build_nested_execution_schedule(loop,batch,
                    execution_order=['conditions','points'])
                issues = _validate_safe_jumps([{}],batch,1.,schedule)
                self.assertEqual(not bool(issues), expected_safe, issues)

    def test_disconnected_requested_axis_prevents_acquisition(self):
        with tempfile.TemporaryDirectory() as folder:
            events, finished, progress, frames, acquired = self.run_worker(folder, connected=False)
            self.assertTrue(finished and not finished[-1][0], finished)
            self.assertEqual(acquired, [])
            self.assertEqual(list(Path(folder).glob('*.csv')), [])

    def test_cancellation_closes_interleaved_files_without_false_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            events, finished, progress, frames, acquired = self.run_worker(folder, cancel_after=5)
            self.assertEqual(len(acquired), 5)
            self.assertTrue(finished)
            self.assertIn('stop', finished[-1][1].lower())
            self.assertTrue(not frames or frames[-1][0] < frames[-1][1])
            for path in Path(folder).glob('*.csv'):
                with path.open('a') as stream:
                    stream.write('')

if __name__ == '__main__':
    unittest.main()
