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

from app.devices.iv_adapter import SMUCommunicationError
from app.experiment_metadata import ExperimentMetadataService
from ui import presets_panel as panel
from tests.test_smu_resilience import _HealthyRunDevice
from tests import test_smu_resilience as fixtures
from tests.test_winspec_scans import scan
from tests.setup_sequence_helpers import set_paired_setup_draft
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


def batch(**overrides):
    values = dict(frames=2, Vbg_start=1., Vbg_stop=2.)
    values.update(overrides)
    return pd.DataFrame([fixtures.SMUResilienceTests._batch_row(**values)])


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
        set_paired_setup_draft(widget)
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
        set_paired_setup_draft(widget)
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


@pytest.mark.parametrize('backend, label', [
    ('andor_ingaas', 'Andor InGaAs'),
    ('andor_si', 'Andor Silicon'),
])
def test_andor_recipe_preview_filename_and_connection_check(backend, label):
    from ui.preview_widget import _fmt_ctx
    definition = loops()
    definition.loc[0, 'Values'] = f'PIXIS, {label}'
    sequence, rows, _ = panel._build_plan(definition, batch(), 'Customized')
    ctx = sequence[1]
    assert ctx['Measurement setup'] == backend
    assert _fmt_ctx(ctx) == f'Setup={label}, CW=1150, Exp=500, EPF=3'
    base, _, tokens = panel._build_run_filename_base(
        fixtures.SMUResilienceTests._run_meta(), ctx, rows.iloc[0].to_dict())
    assert base.endswith(f'_{label}')
    assert ('setup', label) in tokens
    schedule = panel._build_nested_execution_schedule(definition, batch(), mode='Customized')
    assert panel._setup_readiness_issues(SimpleNamespace(connected_backends=('lightfield',)), schedule) == [
        f'Connect all requested setups before running: {label}']
    assert not panel._setup_readiness_issues(
        SimpleNamespace(connected_backends=('lightfield', backend)), schedule)


def test_paired_setup_filename_omits_redundant_setup_label():
    sequence, rows, _ = panel._build_plan(loops(), batch(), 'Customized')
    row = rows.iloc[0].to_dict()
    meta = fixtures.SMUResilienceTests._run_meta()
    single, _, _ = panel._build_run_filename_base(meta, sequence[0], row)
    paired, _, tokens = panel._build_run_filename_base(
        meta, dict(sequence[0], **{'Measurement setup': 'pixis_andor_ingaas'}), row)
    assert paired == single.removesuffix('_PIXIS')
    assert not any(key == 'setup' for key, _ in tokens)


def test_single_value_is_held_fixed_while_the_group_steps():
    definition = loops()
    definition.loc[definition.Parameter == 'Exposure Time (ms)', 'Values'] = '100'
    sequence, _, _ = panel._build_plan(definition, batch(), 'Customized')
    assert [ctx['Exposure Time (ms)'] for ctx in sequence] == [100, 100]
    assert [ctx['Center Wavelength (nm)'] for ctx in sequence] == [730, 1150]


def test_paired_andor_file_is_named_by_its_own_recipe():
    sequence, rows, _ = panel._build_plan(loops(), batch(), 'Customized')
    row, meta = rows.iloc[0].to_dict(), fixtures.SMUResilienceTests._run_meta()
    ctx = dict(sequence[0], **{'Measurement setup': 'pixis_andor_ingaas', 'Center Wavelength (nm)': 650,
                               'Exposure Time (ms)': 50, 'Accumulations (EPF)': 1,
                               'Andor Center Wavelength (nm)': 1100, 'Andor Exposure Time (ms)': 1000,
                               'Andor Accumulations (EPF)': 4})
    parts = ['center', 'exposure']
    name = lambda context: panel._build_run_filename_base(
        meta, context, row, enabled_parts=parts, andor_token=False)[0]
    pixis, andor = name(ctx), name(panel._andor_filename_ctx(ctx))
    assert '650nmc' in pixis and '0.05sx1' in pixis and '1100nmc' not in pixis and 'Andor' not in pixis
    assert '1100nmc' in andor and '1sx4' in andor and '650nmc' not in andor and 'Andor' not in andor
    # The shared pair journal still names both recipes.
    journal = panel._build_run_filename_base(meta, ctx, row, enabled_parts=parts)[0]
    assert '650nmc' in journal and 'Andor_1100nmc_1sx4' in journal


class Device(_HealthyRunDevice):
    def __init__(self, events, *, zero_failure=None):
        super().__init__(("Vbg", "Vtg", "Vbias2"))
        self.events, self.zero_failure = events, zero_failure
        self.zero_calls = 0
        self.gates = (0., 0.)
        self.bias2 = 0.75

    def set_gates(self, *, Vbg, Vtg, **kwargs):
        self.gates = Vbg, Vtg
        self.events.append(('gates', self.gates))

    def read_current_gates(self, **kwargs):
        self.events.append(("read_gates", self.gates))
        return self.gates

    def read_currents(self, **kwargs):
        self.events.append(("read_currents", self.gates))
        return super().read_currents(**kwargs)

    def ramp_to(self, role, value, *args, **kwargs):
        assert role == "Vbias2"
        self.bias2 = value
        return True

    def read_current_bias2(self, **kwargs):
        return self.bias2

    def read_bias2_current(self, **kwargs):
        return 1e-9

    def ramp_all_to_zero_report(self, **kwargs):
        self.zero_calls += 1
        self.events.append(("zero", dict(kwargs), self.gates, self.bias2))
        failure = self.zero_failure[1] if self.zero_failure and self.zero_failure[0] == self.zero_calls else None
        if failure == "transport":
            self.last_communication_error = SMUCommunicationError(
                "Gate return timed out", role="Vbg", operation="ramp_to_zero")
        if failure in ("partial", "transport"):
            return {"Vbg": {"status": "failed", "error": "zero failed"},
                    "Vtg": {"status": "skipped_frozen_after_error"}}
        if failure == "missing":
            return {"Vbg": {"status": "reached_zero"}}
        if failure == "missing_bias2":
            return {role: {"status": "reached_zero"} for role in ("Vbg", "Vtg")}
        if failure == "exception":
            raise RuntimeError("zero exception")
        self.gates = (0., 0.)
        if kwargs.get("include_bias2"):
            self.bias2 = 0.0
        report = super().ramp_all_to_zero_report(**kwargs)
        if not kwargs.get("include_bias2"):
            report.pop("Vbias2")
        return report


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


def run_worker(tmp_path, monkeypatch, *, inner=False, zero_failure=None, fail_preflight=False,
               missing=False, stop_after=None, fail_acquire=False, bias2=False):
    events = []
    device = Device(events, zero_failure=zero_failure)
    detector = Detector(events, device, fail_preflight)
    if missing:
        detector.connected_backends = ('lightfield',)
    conditions = batch(bias2_mode="independent", Vbias2_start=0.25, Vbias2_stop=0.5) if bias2 else batch()
    sequence, rows, _ = panel._build_plan(loops(), conditions, 'Customized')
    order = ['conditions', 'points', 'group:1'] if inner else ['group:1', 'conditions', 'points']
    schedule = panel._build_nested_execution_schedule(loops(), conditions, mode='Customized', execution_order=order)
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
    monkeypatch.setattr(cfg.ramp, 'delay_s', 0.001)
    worker.run()
    return events, result


def zero_requests(events, *, include_bias2=False):
    commands = [event for event in events if event[0] == "zero"]
    for _, options, _, _ in commands:
        assert options["coordinated_gates"] is True
        assert bool(options.get("include_bias2")) is include_bias2
        assert options["ramp_step"] == pytest.approx(max(cfg.ramp.step_V * 0.5, 1e-6))
        assert options["delay_s"] == pytest.approx(cfg.ramp.delay_s * 2.0)
    return commands


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
    assert [event[2:] for event in zero_requests(events)] == [((2., 0.), 0.75)] * 2
    files = list((tmp_path / 'PL').glob('*.csv'))
    assert len(files) == 2
    assert any('PIXIS' in f.name and '730' in f.name for f in files)
    assert any('WinSpec' in f.name and '1150' in f.name for f in files)
    for path in files:
        with path.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        assert [float(r['Vbg_set']) for r in rows] == [1., 2.]


@pytest.mark.parametrize("outcome", ["complete", "partial", "stop", "wrong_station"])
def test_explicit_dual_setup_uses_one_gate_point_for_both_detectors(tmp_path, monkeypatch, outcome):
    station = "default" if outcome == "wrong_station" else "attodry2100"
    monkeypatch.setattr(cfg.lf6, "station_id", station)
    definition = loops()
    definition["Values"] = ["PIXIS + Andor InGaAs", "730", "100", "2"]
    definition = pd.concat([definition, pd.DataFrame([
        dict(Enable=True, Parameter=parameter, Values=value, Group=1)
        for parameter, value in zip(panel._ANDOR_PARAMS, ["1150", "225", "4"])
    ])], ignore_index=True)
    condition_rows = batch()
    sequence, rows, _ = panel._build_plan(definition, condition_rows, "Customized")
    schedule = panel._build_nested_execution_schedule(definition, condition_rows, mode="Customized")
    calls = []
    device = Device(calls)

    class PairDetector(Detector):
        connected_backends = ("lightfield", "andor_ingaas")

        def validate_station_pair(self, backend, recipes):
            calls.append(("validate", backend, self.device.gates))

        def acquire_station_pair(self, backend, recipes, stop, *, owner):
            assert owner == "presets"
            assert backend == "andor_ingaas"
            assert recipes == {
                "lightfield": {"center_nm": 730, "exposure_ms": 100, "frames": 2},
                "andor_ingaas": {"center_nm": 1150, "exposure_ms": 225, "frames": 4},
            }
            calls.append(("paired_capture", self.device.gates))
            result = {name: {"wavelengths": np.arange(3) + values["center_nm"],
                             "counts": np.ones(3), "settings": values}
                      for name, values in recipes.items()}
            if outcome in {"partial", "stop"}:
                result[backend] = {"error": "capture interrupted"}
            if outcome == "stop":
                stop.set()
            return result

    meta = fixtures.SMUResilienceTests._run_meta()
    meta.update(initial_voltage_settle_s=0., voltage_settle_s=0.)
    if outcome != "wrong_station":
        meta["paired_station_required"] = "attodry2100"
    worker = panel._RunWorker(
        sequence, rows, lf6_ctrl=PairDetector(calls, device),
        smu_ctrl=SimpleNamespace(is_connected=True, device=device), out_dir=tmp_path,
        run_meta=meta, filename_parts=["center"], stop_event=threading.Event(),
        acquisition_schedule=schedule,
        startup_metadata={"safe_jump_V": 2.0, "nested": True, "instruments": {}},
    )
    summaries = []
    metadata_runs = []
    worker.finished.connect(lambda *result: summaries.append(result))
    worker.metadata_ready.connect(metadata_runs.append)
    worker.run()
    assert not any(call[0] in {"prepare", "acquire"} for call in calls)
    if outcome == "wrong_station":
        assert calls == []
        assert not list(tmp_path.rglob("*.jsonl"))
        assert "AttoDRY2100" in str(summaries)
        return
    gates = [(1., 0.), (2., 0.)] if outcome == "complete" else [(1., 0.)]
    assert [entry[1] for entry in calls if entry[0] == "gates"] == gates
    assert [entry[1] for entry in calls if entry[0] == "paired_capture"] == gates
    assert calls[0] == ("validate", "andor_ingaas", (0., 0.))
    manifest, = tmp_path.rglob("*_pairs.jsonl")
    entries = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
    assert [item["status"] for item in entries if item["status"] != "writing"] == (
        ["complete", "complete"] if outcome == "complete" else ["incomplete"])
    spectra = list(tmp_path.rglob("*.csv"))
    assert len(spectra) == (2 if outcome == "complete" else 1)
    run, = metadata_runs
    terminal = ExperimentMetadataService.load_metadata(run.path)
    assert terminal["status"] == {"complete": "completed", "partial": "failed", "stop": "cancelled"}[outcome]
    assert ExperimentMetadataService.preview_settings(terminal)["loadable"]["paired_station_required"] == "attodry2100"
    assert {manifest.name, *(path.name for path in spectra)} <= {
        str(record["path"]).replace("\\", "/").rsplit("/", 1)[-1] for record in terminal["files"]}
    for csv_path in spectra:
        payload = panel.load_last_dual_gate_acquisition(csv_path)
        assert len(payload["data"]) == len(payload["wavelengths"]) == 3


def test_restored_dual_gate_draft_on_other_station_is_rejected(monkeypatch):
    monkeypatch.setattr(cfg.lf6, "station_id", "attodry2100")
    widget = panel.PresetsPanel()
    try:
        set_paired_setup_draft(widget)
        widget._loop_table.item(0, 2).setText("PIXIS + Andor InGaAs")
        state = widget.capture_session_state()
        monkeypatch.setattr(cfg.lf6, "station_id", "default")
        widget.restore_session_state(state)
        assert any("AttoDRY2100" in error for error in widget._draft_validation_issues())
        assert "pixis_andor_ingaas" not in panel._setup_choices()
        with pytest.raises(ValueError, match="AttoDRY2100"):
            widget.apply_saved_experiment_settings({"paired_station_required": "attodry2100"})
    finally:
        widget.close()


@pytest.mark.parametrize('failure', ['fail_preflight', 'missing'])
def test_failure_blocks_remaining_setup_and_preserves_failure_handling(tmp_path, monkeypatch, failure):
    events, result = run_worker(tmp_path, monkeypatch, **{failure: True})
    assert not result[-1][0], result
    assert not any(e[:2] == ('acquire', 'winspec_ingaas') for e in events)
    assert not any(e[0] in ('gates', 'zero', 'acquire') for e in events)


@pytest.mark.parametrize("failure, detail", [
    ("partial", "zero failed"), ("missing", "Vtg"), ("exception", "zero exception"),
])
@pytest.mark.parametrize("phase", ["switch", "cleanup"])
def test_failed_zero_blocks_switch_or_marks_cleanup_failed(tmp_path, monkeypatch, failure, detail, phase):
    events, result = run_worker(tmp_path, monkeypatch,
                                zero_failure=(1 if phase == "switch" else 2, failure))
    assert not result[-1][0], result
    assert detail in result[-1][1]
    assert "return-to-zero failed" in result[-1][1].lower()
    commands = zero_requests(events)
    assert len(commands) == (1 if phase == "switch" else 2)
    assert events[-1] == commands[-1]
    assert events[-1][2] == (2., 0.)
    files = list((tmp_path / "PL").glob("*.csv"))
    assert len(files) == (1 if phase == "switch" else 2)
    assert any("PIXIS" in path.name for path in files)
    if phase == "switch":
        assert not any(event[:2] == ("acquire", "winspec_ingaas") for event in events)
    else:
        assert any("WinSpec" in path.name for path in files)


def test_setup_switch_and_successful_cleanup_include_active_second_bias(tmp_path, monkeypatch):
    events, result = run_worker(tmp_path, monkeypatch, bias2=True)
    assert result[-1][0], result
    assert [event[2:] for event in zero_requests(events, include_bias2=True)] == [((2., 0.), 0.5)] * 2


def test_missing_active_bias2_result_blocks_setup_switch(tmp_path, monkeypatch):
    events, result = run_worker(tmp_path, monkeypatch, bias2=True, zero_failure=(1, "missing_bias2"))
    assert not result[-1][0]
    assert "Vbias2" in result[-1][1]
    assert zero_requests(events, include_bias2=True) == [events[-1]]
    assert not any(event[:2] == ("acquire", "winspec_ingaas") for event in events)


def test_setup_switch_transport_failure_preserves_role_report(tmp_path, monkeypatch):
    events, result = run_worker(tmp_path, monkeypatch, zero_failure=(1, "transport"))
    assert not result[-1][0]
    assert "Gate return timed out" in result[-1][1]
    assert zero_requests(events) == [events[-1]]
    incident = json.loads((tmp_path / "hardware_incidents.jsonl").read_text(encoding="utf-8"))
    assert incident["hardware"]["role"] == "Vbg"
    switch = incident["run_context"]["setup_switch_zero"]
    assert switch["roles"] == {"Vbg": {"status": "failed", "error": "zero failed"},
                               "Vtg": {"status": "skipped_frozen_after_error"}}
    assert set(switch["errors"]) == {"Vbg: zero failed", "Vtg: skipped_frozen_after_error"}
    assert not incident["cleanup"]["attempted"]


def test_gate_communication_failure_preserves_last_commanded_state(tmp_path, monkeypatch):
    set_gates = Device.set_gates

    def fail_after_write(device, **kwargs):
        set_gates(device, **kwargs)
        raise SMUCommunicationError("Gate write timed out", role="Vtg", operation="set_gates")

    monkeypatch.setattr(Device, "set_gates", fail_after_write)
    events, result = run_worker(tmp_path, monkeypatch)
    assert not result[-1][0]
    assert "Gate write timed out" in result[-1][1]
    assert events[-1] == ("gates", (1., 0.))
    assert zero_requests(events) == []
    incident = json.loads((tmp_path / "hardware_incidents.jsonl").read_text(encoding="utf-8"))
    assert incident["hardware"]["role"] == "Vtg"
    assert not incident["cleanup"]["attempted"]
    assert incident["cleanup"]["status"] == "skipped_preserve_last_smu_state"


def test_inner_setup_group_reuses_gate_point_and_keeps_output_streams_separate(tmp_path, monkeypatch):
    events, result = run_worker(tmp_path, monkeypatch, inner=True)
    assert result[-1][0], result
    assert [(e[1], e[-1]) for e in events if e[0] == 'acquire'] == [
        ('lightfield', (1., 0.)), ('winspec_ingaas', (1., 0.)),
        ('lightfield', (2., 0.)), ('winspec_ingaas', (2., 0.)),
    ]
    assert len(zero_requests(events)) == 1
    assert len([e for e in events if e[0] == 'gates']) == 2
    assert len(list((tmp_path / 'PL').glob('*.csv'))) == 2


@pytest.mark.parametrize('stop_run', [True, False])
def test_stop_or_acquisition_error_does_not_continue_to_next_setup(tmp_path, monkeypatch, stop_run):
    events, result = run_worker(tmp_path, monkeypatch, stop_after=1 if stop_run else None, fail_acquire=not stop_run)
    assert not result[-1][0]
    assert len([e for e in events if e[0] == 'acquire']) == 1
    if stop_run:
        assert zero_requests(events) == [events[-1]]
        assert events[-1][2] == (1., 0.)
    else:
        assert zero_requests(events) == []
        assert events[-1] == ('acquire', 'lightfield', 730., 100., 2, (1., 0.))


@pytest.mark.parametrize('parameter, value', [
    ('Center Wavelength (nm)', '730, nan'), ('Exposure Time (ms)', '100, -1'),
    ('Accumulations (EPF)', '2, 1.5'), ('Measurement setup', 'PIXIS, WinSpec,'),
    ('Exposure Time (ms)', '100, 200, 300'),
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
        journal = [json.loads(line) for line in run.event_path.read_text(encoding='utf-8').splitlines()]
        starts = [event for event in journal if event['event'] == 'file_started']
        assert len(starts) == 2
        winspec_start = next(event for event in starts
                             if event['context']['measurement_setup'] == 'winspec_ingaas')
        assert winspec_start['context']['center_nm'] == 1050
        assert winspec_start['context']['exposure_ms'] == 500
        assert winspec_start['context']['accumulations'] == 3
        assert not any(event['event'].startswith('capture_') for event in journal)
        saved = run.service.load_metadata(run.path)
        snapshot = next(item['values'] for item in saved['settings']['snapshots']
                        if item['settings_id'] == winspec_start['settings_id'])
        assert snapshot['calibration']['axis_unit'] == 'nm'
        assert snapshot['calibration']['record'] == cfg.lf6.winspec_wavelength_calibrations[0]
        assert snapshot['calibration']['context'] == winspec.last_calibration_context
        np.testing.assert_array_equal(snapshot['calibration']['wavelength_axis_nm'],
                                      controller.adapter.calibration_wavelengths())
        assert snapshot['observed']['winspec']['exposure_ms'] == 500
        assert snapshot['observed']['winspec']['accumulations'] == 3
        assert 'last_frame' not in snapshot['observed']
        completed = [event for event in journal if event['event'] == 'file_completed']
        assert len(completed) == 2
        assert all(event['completed_points'] == event['csv_rows'] == 2 for event in completed)
    finally:
        controller.set_temperature_monitor_paused('presets', False)
    assert not controller.switching_locked
