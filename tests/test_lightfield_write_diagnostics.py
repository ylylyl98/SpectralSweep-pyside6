"""Persist read-only SDK evidence for both working and rejected writes."""
import json
from types import SimpleNamespace

import pytest

from lf6_automation import LF6Setup, SpectrometerSettings
from tests.test_lightfield_acquisition_validation import CENTER, EXIT, setup


@pytest.mark.parametrize('ignored', [False, True])
def test_center_diagnostics_keep_before_write_and_final_evidence(setup, tmp_path, ignored):
    setup._center_diagnostics_path = tmp_path / 'lightfield-center.jsonl'
    setup.experiment.Name = '2100'
    setup.experiment.IsRelevant = lambda key: True
    setup.experiment.IsValid = lambda key, value: value <= 1500.
    setup.experiment.GetCurrentRange = lambda key: SimpleNamespace(Minimum=0., Maximum=1500., Increment=.001)
    setup.experiment.values[SpectrometerSettings.GratingSelected] = '300 grooves/mm'
    if ignored:
        setup.experiment.ignored.add(CENTER)
        with pytest.raises(TimeoutError):
            setup.set_center_wavelength_when_ready(720.)
    else:
        setup.set_center_wavelength_when_ready(720.)
    record = json.loads(setup._center_diagnostics_path.read_text(encoding='utf-8'))
    assert record['requested_nm'] == 720.
    assert record['before']['center_nm'] == 1097.3969261940897
    assert record['before']['exit_port'] == 'SideExit'
    assert record['before']['IsRelevant'] is True
    assert record['before']['IsValid'] is True
    assert record['before']['range_nm']['Maximum'] == 1500.
    assert record['stats']['before_write']['center_nm'] == 1097.3969261940897
    assert record['stats']['attempts'] == 1
    assert record['after']['center_nm'] == (1097.3969261940897 if ignored else 720.)
    assert record['stats']['result'] == ('timeout' if ignored else 'succeeded')
    assert [v for key, v in setup.experiment.writes if key == CENTER] == [720.]
    assert all(key != EXIT for key, _ in setup.experiment.writes)


def test_diagnostic_read_failures_do_not_replace_acquisition_result(setup, tmp_path):
    setup._center_diagnostics_path = tmp_path / 'lightfield-center.jsonl'
    def unavailable(*_):
        raise RuntimeError('Diagnostic API unavailable')
    setup.experiment.IsValid = unavailable
    setup.experiment.GetCurrentRange = unavailable
    setup.set_center_wavelength_when_ready(730.)
    record = json.loads(setup._center_diagnostics_path.read_text(encoding='utf-8'))
    assert record['stats']['result'] == 'succeeded'
    assert 'Diagnostic API unavailable' in record['before']['read_errors']['IsValid']


def test_failed_diagnostic_file_write_does_not_hide_setting_failure(setup, tmp_path):
    setup._center_diagnostics_path = tmp_path  # A directory cannot be opened as a log.
    setup.experiment.ignored.add(CENTER)
    with pytest.raises(TimeoutError, match='1097'):
        setup.set_center_wavelength_when_ready(720.)
    assert setup.center_wavelength_write_stats['result'] == 'timeout'


def test_trace_preserves_a_transient_matching_center_that_later_reverts(setup, tmp_path):
    setup._center_diagnostics_path = tmp_path / 'lightfield-center.jsonl'
    setup.experiment.ignored.add(CENTER)
    # Full diagnostic snapshots read the normal center. Start transient SDK
    # observations only after the real SetValue call has been made.
    original = setup.experiment.SetValue
    def set_value(key, value):
        original(key, value)
        setup.experiment.center_reads = [720., 1097.3969261940897]
    setup.experiment.SetValue = set_value
    with pytest.raises(TimeoutError):
        setup.set_center_wavelength_when_ready(720.)
    record = json.loads(setup._center_diagnostics_path.read_text(encoding='utf-8'))
    samples = record['stats']['readback_changes']
    assert [sample['value'] for sample in samples] == [720., 1097.3969261940897]
    assert len(samples) <= 32


def test_invalid_request_does_not_reuse_previous_success_diagnostics(setup, tmp_path):
    setup._center_diagnostics_path = tmp_path / 'lightfield-center.jsonl'
    setup.set_center_wavelength_when_ready(720.)
    with pytest.raises(ValueError):
        setup.set_center_wavelength_when_ready(float('nan'))
    records = [json.loads(line) for line in setup._center_diagnostics_path.read_text(encoding='utf-8').splitlines()]
    assert records[0]['stats']['result'] == 'succeeded'
    assert records[1]['stats'] == {}
    assert records[1]['error'].startswith('ValueError:')


def test_invalid_diagnostic_path_does_not_prevent_center_write(setup):
    setup._center_diagnostics_path = object()
    setup.set_center_wavelength_when_ready(730.)
    assert setup.experiment.values[CENTER] == 730.
    assert setup.center_wavelength_write_stats['result'] == 'succeeded'


@pytest.mark.parametrize('ignored', [False, True])
def test_source_context_does_not_leak_to_later_direct_writes(setup, tmp_path, ignored):
    from app.lightfield_diagnostics import center_write_context
    setup._center_diagnostics_path = tmp_path / 'lightfield-center.jsonl'
    def write():
        with center_write_context(setup, source='Spectrum Apply', backend='lightfield'):
            setup.set_center_wavelength_when_ready(720.)
    if ignored:
        setup.experiment.ignored.add(CENTER)
        with pytest.raises(TimeoutError):
            write()
        setup.experiment.ignored.clear()
    else:
        write()
    setup.set_center_wavelength_when_ready(730.)
    records = [json.loads(line) for line in setup._center_diagnostics_path.read_text(encoding='utf-8').splitlines()]
    assert records[0]['context']['source'] == 'Spectrum Apply'
    assert records[1]['context'] == {}
