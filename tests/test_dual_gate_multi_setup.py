"""Dual Gate setup recipes: simulated hardware only."""
import csv
import json
import os
import threading
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import numpy as np
import pandas as pd
import pytest
from PySide6.QtWidgets import QApplication, QCheckBox

from ui import presets_panel as panel
from tests.test_smu_resilience import _HealthyRunDevice
from tests import test_smu_resilience as fixtures
from tests.test_winspec_scans import scan
from utils.config import cfg


@pytest.fixture(scope='module', autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def loops():
    return pd.DataFrame([
        dict(Enable=True, Parameter=name, Values=values, Group=1)
        for name, values in [
            ('Measurement setup', 'PIXIS, WinSpec'),
            ('Center Wavelength (nm)', '730, 1150'),
            ('Exposure Time (ms)', '100, 500'),
            ('Accumulations (EPF)', '2, 3'),
        ]
    ])


def batch():
    return pd.DataFrame([fixtures.SMUResilienceTests._batch_row(frames=2, Vbg_start=1., Vbg_stop=2.)])


def test_setup_wavelength_exposure_and_accumulations_are_paired():
    sequence, _, _ = panel._build_plan(loops(), batch(), 'Customized')
    assert sequence == [
        {'Measurement setup': 'lightfield', 'Center Wavelength (nm)': 730.,
         'Exposure Time (ms)': 100., 'Accumulations (EPF)': 2.},
        {'Measurement setup': 'winspec_ingaas', 'Center Wavelength (nm)': 1150.,
         'Exposure Time (ms)': 500., 'Accumulations (EPF)': 3.},
    ]
    definition = loops()
    definition.loc[0, 'Values'] = 'PIXIS, PIXIS'
    assert [ctx['Measurement setup'] for ctx in panel._build_plan(definition, batch(), 'Customized')[0]] == ['lightfield'] * 2


def test_invalid_setup_cannot_silently_run_current_detector():
    definition = loops()
    definition.loc[0, 'Values'] = 'PIXIS, typo'
    with pytest.raises(ValueError, match='setup'):
        panel._build_plan(definition, batch(), 'Customized')


def test_recipe_is_editable_and_roundtrips_without_changing_gate_rows():
    widget, restored = panel.PresetsPanel(), panel.PresetsPanel()
    try:
        before = panel._read_batch_table(widget._batch_table).to_dict('records')
        widget._load_dual_setup_recipe()
        assert panel._read_batch_table(widget._batch_table).to_dict('records') == before
        for row, values in enumerate(('PIXIS, WinSpec', '730, 1150', '100, 500', '2, 3')):
            widget._loop_table.item(row, 2).setText(values)
        assert not widget._draft_validation_issues()
        restored.restore_session_state(widget.capture_session_state())
        definition = panel._read_loop_table(restored._loop_table)
        sequence, _, _ = panel._build_plan(definition, batch(), 'Customized')
        assert sequence[0]['Center Wavelength (nm)'] == 730.
        assert sequence[1]['Exposure Time (ms)'] == 500.
        assert sequence[1]['Measurement setup'] == 'winspec_ingaas'
        assert restored._execution_order[0]['kind'] == 'group'
    finally:
        widget.close()
        restored.close()


def test_recipe_checkboxes_refresh_draft_and_preview():
    widget = panel.PresetsPanel()
    try:
        widget._load_dual_setup_recipe()
        widget._on_apply()
        assert not widget._tables_dirty
        widget._loop_table.cellWidget(2, 0).findChild(QCheckBox).setChecked(False)
        assert widget._tables_dirty
        # The existing editor requires reviewing/resetting order after regrouping.
        assert any('Reset execution order' in issue for issue in widget._draft_validation_issues())
    finally:
        widget.close()


def test_preview_uses_recognizable_setup_labels():
    from ui.preview_widget import _fmt_ctx
    assert _fmt_ctx({'Measurement setup': 'winspec_ingaas', 'Center Wavelength (nm)': 1150}) == 'Setup=WinSpec, CW=1150'


def test_loading_recipe_preserves_motion_group_order_and_inner_nesting():
    widget = panel.PresetsPanel()
    try:
        definition = pd.DataFrame([
            dict(Enable=True, Parameter='Rotation1 Angle (deg)', Values='0, 90', Group=5),
            dict(Enable=True, Parameter='Stage Position', Values='1, 2', Group=2),
        ])
        state = widget.capture_session_state()
        order = panel._normalize_execution_order(['group:2', 'conditions', 'points', 'group:5'], definition, 'Customized')
        state.update(draft_loop=definition.to_dict('records'), loop_mode='Customized',
                     execution_order=order, nested_schedule_enabled=True)
        widget.restore_session_state(state)
        widget._load_dual_setup_recipe()
        updated = widget._execution_order_entries()
        assert [entry['kind'] for entry in updated] == ['group', 'group', 'conditions', 'points', 'group']
        assert updated[1]['parameters'] == ['Stage Position']
        assert updated[-1]['parameters'] == ['Rotation1 Angle (deg)']
        retained = panel._read_loop_table(widget._loop_table)
        assert retained.loc[retained.Parameter == 'Rotation1 Angle (deg)', 'Values'].item() == '0, 90'
    finally:
        widget.close()


class Device(_HealthyRunDevice):
    def __init__(self, events, fail_zero=False):
        super().__init__()
        self.events, self.fail_zero = events, fail_zero
        self.gates = (0., 0.)

    def set_gates(self, *, Vbg, Vtg, **kwargs):
        self.gates = Vbg, Vtg
        self.events.append(('gates', self.gates))

    def read_current_gates(self, **kwargs):
        return self.gates

    def ramp_all_to_zero_report(self, **kwargs):
        self.events.append(('zero',))
        if self.fail_zero:
            return {'Vbg': {'status': 'failed', 'error': 'zero failed'}}
        self.gates = (0., 0.)
        return super().ramp_all_to_zero_report(**kwargs)


class Detector:
    is_connected = True
    backend = 'lightfield'
    connected_backends = ('lightfield', 'winspec_ingaas')

    def __init__(self, events, device, fail_preflight=False):
        self.events, self.device, self.fail_preflight = events, device, fail_preflight
        self.settings = (730., 100., 2)
        self.adapter = self

    def configure_for_acquisition(self, *, center_nm, exposure_ms, frames):
        self.settings = center_nm, exposure_ms, frames

    def prepare_scan_condition(self, backend, center, exposure, frames, reduction, stop):
        self.events.append(('prepare', backend, center, exposure, frames, self.device.gates))
        if self.fail_preflight and backend == 'winspec_ingaas':
            raise RuntimeError('No matching WinSpec calibration')
        self.backend = backend
        self.settings = center, exposure, frames
        return self

    def acquire(self):
        self.events.append(('acquire', self.backend, *self.settings, self.device.gates))
        center, _, _ = self.settings
        return np.array([center - 1, center, center + 1]), np.ones(3)


def run_worker(tmp_path, monkeypatch, *, inner=False, fail_zero=False, fail_preflight=False, missing=False, stop_after=None, fail_acquire=False):
    events = []
    device = Device(events, fail_zero)
    detector = Detector(events, device, fail_preflight)
    if missing:
        detector.connected_backends = ('lightfield',)
    sequence, rows, _ = panel._build_plan(loops(), batch(), 'Customized')
    order = ['conditions', 'points', 'group:1'] if inner else ['group:1', 'conditions', 'points']
    schedule = panel._build_nested_execution_schedule(loops(), batch(), mode='Customized', execution_order=order)
    metadata = fixtures.SMUResilienceTests._run_meta()
    metadata.update(initial_voltage_settle_s=0., voltage_settle_s=0.)
    stop = threading.Event()
    raw_acquire = detector.acquire
    def acquire():
        data = raw_acquire()
        if fail_acquire:
            raise RuntimeError('Detector acquisition failed')
        if stop_after and len([e for e in events if e[0] == 'acquire']) >= stop_after:
            stop.set()
        return data
    detector.acquire = acquire
    worker = panel._RunWorker(sequence, rows, lf6_ctrl=detector,
        smu_ctrl=SimpleNamespace(is_connected=True, device=device), out_dir=tmp_path,
        run_meta=metadata, filename_parts=['center', 'exposure'],
        stop_event=stop, acquisition_schedule=schedule)
    result = []
    worker.finished.connect(lambda *args: result.append(args))
    monkeypatch.setattr(cfg.ramp, 'delay_s', 0.)
    worker.run()
    return events, result


def test_full_sweeps_switch_detector_after_zero_and_write_separate_correct_spectra(tmp_path, monkeypatch):
    events, result = run_worker(tmp_path, monkeypatch)
    assert result[-1][0], result
    acquisitions = [e for e in events if e[0] == 'acquire']
    assert acquisitions == [
        ('acquire', 'lightfield', 730., 100., 2, (1., 0.)),
        ('acquire', 'lightfield', 730., 100., 2, (2., 0.)),
        ('acquire', 'winspec_ingaas', 1150., 500., 3, (1., 0.)),
        ('acquire', 'winspec_ingaas', 1150., 500., 3, (2., 0.)),
    ]
    assert [e[5] for e in events if e[0] == 'prepare'] == [(0., 0.)] * 4
    assert events.count(('zero',)) == 2
    files = list((tmp_path / 'PL').glob('*.csv'))
    assert len(files) == 2
    assert any('PIXIS' in f.name and '730' in f.name for f in files)
    assert any('WinSpec' in f.name and '1150' in f.name for f in files)
    for path in files:
        with path.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        assert [float(r['Vbg_set']) for r in rows] == [1., 2.]


@pytest.mark.parametrize('failure', ['fail_zero', 'fail_preflight', 'missing'])
def test_failure_blocks_remaining_setup_and_preserves_failure_handling(tmp_path, monkeypatch, failure):
    events, result = run_worker(tmp_path, monkeypatch, **{failure: True})
    assert not result[-1][0], result
    assert not any(e[:2] == ('acquire', 'winspec_ingaas') for e in events)
    if failure != 'fail_zero':
        assert not any(e[0] in ('gates', 'zero', 'acquire') for e in events)
    else:
        assert events[-1] == ('zero',)


def test_inner_setup_group_reuses_gate_point_and_keeps_output_streams_separate(tmp_path, monkeypatch):
    events, result = run_worker(tmp_path, monkeypatch, inner=True)
    assert result[-1][0], result
    assert [(e[1], e[-1]) for e in events if e[0] == 'acquire'] == [
        ('lightfield', (1., 0.)), ('winspec_ingaas', (1., 0.)),
        ('lightfield', (2., 0.)), ('winspec_ingaas', (2., 0.)),
    ]
    assert events.count(('zero',)) == 1
    assert len([e for e in events if e[0] == 'gates']) == 2
    assert len(list((tmp_path / 'PL').glob('*.csv'))) == 2


@pytest.mark.parametrize('stop_run', [True, False])
def test_stop_or_acquisition_error_does_not_continue_to_next_setup(tmp_path, monkeypatch, stop_run):
    events, result = run_worker(tmp_path, monkeypatch, stop_after=1 if stop_run else None, fail_acquire=not stop_run)
    assert not result[-1][0]
    assert len([e for e in events if e[0] == 'acquire']) == 1
    assert events[-1][0] == ('zero' if stop_run else 'acquire')


@pytest.mark.parametrize('parameter, value', [
    ('Center Wavelength (nm)', '730, nan'), ('Exposure Time (ms)', '100, -1'),
    ('Accumulations (EPF)', '2, 1.5'), ('Measurement setup', 'PIXIS, WinSpec,'),
    ('Exposure Time (ms)', '100'),
])
def test_invalid_or_incomplete_recipes_are_rejected_before_execution(parameter, value):
    definition = loops()
    definition.loc[definition.Parameter == parameter, 'Values'] = value
    with pytest.raises(ValueError):
        panel._build_plan(definition, batch(), 'Customized')


def test_real_controller_routes_and_calibrates_both_setups_under_dual_gate_lock(scan, tmp_path, monkeypatch):
    from app.experiment_metadata import ExperimentMetadataService
    controller, winspec, camera, live = scan
    events = []
    device = Device(events)
    optics = winspec.lightfield

    def configure(**settings):
        if live['output_port'] != 'FrontExit':
            routes.append(True)
        live['output_port'] = 'FrontExit'
        live['wavelength_nm'] = settings['center_nm']
        events.append(('configure_pixis', settings))

    optics.configure_for_acquisition = configure
    pixis = SimpleNamespace(invalidate_wavelengths=lambda: None,
        acquire=lambda: (np.array([729., 730., 731.]), np.ones(3)))
    controller._worker._parked['lightfield'] = (optics, pixis, {'backend': 'mock_lightfield'}, [])
    routes = []
    def route(setup, output, **kwargs):
        if live['output_port'] != 'SideExit':
            routes.append(False)
        live['output_port'] = 'SideExit'
        return dict(live)
    monkeypatch.setattr('app.devices.winspec_adapter.ensure_output_route', route)
    definition = loops()
    definition.loc[1, 'Values'] = '730, 1050'
    sequence, rows, _ = panel._build_plan(definition, batch(), 'Customized')
    schedule = panel._build_nested_execution_schedule(definition, batch(), mode='Customized')
    metadata = fixtures.SMUResilienceTests._run_meta()
    metadata.update(initial_voltage_settle_s=0., voltage_settle_s=0.)
    run = ExperimentMetadataService(tmp_path, tmp_path / 'history.sqlite').begin('dual_gate_sweep', 'sample')
    worker = panel._RunWorker(sequence, rows, lf6_ctrl=controller,
        smu_ctrl=SimpleNamespace(is_connected=True, device=device), out_dir=tmp_path,
        run_meta=metadata, filename_parts=['center'], stop_event=threading.Event(),
        acquisition_schedule=schedule, metadata_run=run)
    finished = []
    worker.finished.connect(lambda *args: finished.append(args))
    controller.set_temperature_monitor_paused('presets', True)
    monkeypatch.setattr(cfg.ramp, 'delay_s', 0.)
    try:
        worker.run()
        assert finished[-1][0], finished
        assert controller.switching_locked and controller.temperature_monitor_paused
        assert routes == [True, False, True, False]
        path = next((tmp_path / 'PL').glob('*WinSpec.csv'))
        with path.open(encoding='utf-8-sig', newline='') as stream:
            exported = list(csv.DictReader(stream))
        assert [float(row['Vbg_set']) for row in exported] == [1., 2.]
        np.testing.assert_allclose(np.array(list(exported[0].values())[9:], dtype=float), np.arange(512)[19:490] / 3)
        captures = [json.loads(line) for line in run.event_path.read_text(encoding='utf-8').splitlines()
                    if json.loads(line)['event'] == 'capture_started']
        assert len(captures) == 2
        assert all(c['context']['measurement_setup'] == 'winspec_ingaas' for c in captures)
        assert all(c['context']['center_nm'] == 1050 and c['context']['exposure_ms'] == 500 for c in captures)
        assert all(c['context']['accumulations'] == 3 for c in captures)
    finally:
        controller.set_temperature_monitor_paused('presets', False)
    assert not controller.switching_locked
