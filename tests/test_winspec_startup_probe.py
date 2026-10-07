import importlib.util
import json
from pathlib import Path
import struct
import socket
import threading
from types import SimpleNamespace

import pytest

from tests.test_winspec_bridge_guard import bridge_functions


def load_probe():
    path = Path('tools/winspec/startup_reuse_probe.py')
    assert path.exists(), 'Startup reuse probe is not implemented'
    spec = importlib.util.spec_from_file_location('startup_reuse_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_bridge(failure=None):
    settings = dict(exposure_ms=800., accumulations=1, sequential_frames=1,
                    timing_mode=1, detector_width=512, detector_height=1,
                    output_width=512, output_height=1, roi_enabled=False,
                    actual_temperature_c=-100., temperature_locked=True,
                    running=False, winspec_reported_running=False)
    events = []
    documents = []
    calls = []
    class Doc:
        def __init__(self):
            self.number = len(documents)
            self.closed = False
            self.saved = False
            self._oleobj_ = self
            documents.append(self)
        def Save(self):
            self.saved = True
            events.append(('save', self.number))
            return True
        def Close(self):
            self.closed = True
            events.append(('close', self.number))
            return True
        def SaveAs(self, *args):
            self.saved = True
            return True
        def QueryInterface(self, iid):
            return self
    def start(doc):
        assert not doc.closed
        if any(old is doc for old in calls):
            assert doc.saved
        calls.append(doc)
        events.append(('start', doc.number))
        doc.saved = False
        if failure == 'replacement' and len(calls) == 3:
            return True, Doc()
        return True, doc
    exp = SimpleNamespace(Start=start, Stop=lambda:events.append(('stop',)) or True)
    br = SimpleNamespace(SPE_PATH='original.spe', TRANSFER_STATE=SimpleNamespace(pending=None),
                         TRANSFER_PENDING=threading.Event(),
                         TEMPERATURE_MONITOR_UNHEALTHY=threading.Event(),
                         pythoncom=SimpleNamespace(IID_IUnknown=object()),
                         create_experiment=lambda:exp, read_settings=lambda e:dict(settings),
                         validate_temperature=lambda s:s['actual_temperature_c'],
                         com_return_value=lambda value,name:value[0] if isinstance(value,tuple) else value)
    startup = bridge_functions(dict(create_document=Doc, time=__import__('time'),
                                   com_return_value=br.com_return_value,
                                   server_log=lambda *a:None, COM_TUPLE_METHODS_LOGGED=set()),
                              'start_new_document', 'start2_document')
    br.start_new_document = startup['start_new_document']
    br.start2_document = startup['start2_document']
    validation = bridge_functions(dict(math=__import__('math')), 'validate_acquisition_request')
    br.validate_acquisition_request = validation['validate_acquisition_request']
    def apply(e, requested):
        settings.update(requested)
        if failure == 'apply' and requested['exposure_ms'] == 500.:
            settings['accumulations'] = 1
        return dict(settings)
    br.apply_settings = apply
    def acquire(e, compact_settings, expected_settings):
        assert compact_settings and settings['exposure_ms'] == 500. and settings['accumulations'] == 2
        assert not br.TRANSFER_PENDING.is_set()
        timings = {}
        doc = br.start_new_document(e, timings=timings)
        index = len(calls)
        if failure == 'capture' and index == 4:
            raise RuntimeError('capture failed')
        metadata = dict(width=512, height=1, frame_count=1, winspec_datatype=3,
                        settings=dict(settings), settings_scope='configured_plus_spe',
                        temperature_guard=dict(version=4, passed=True, sample_count=2),
                        bridge_timing_s=timings)
        if failure == 'header' and index == 4:
            metadata['settings']['accumulations'] = 1
        raw = struct.pack('<512H', *([index] * 512))
        br.TRANSFER_STATE.pending = (doc, str(index)+'.spe')
        br.TRANSFER_PENDING.set()
        return metadata, raw
    br.acquire = acquire
    archives = {}
    original_acquire = br.acquire
    def archived_acquire(*args, **kwargs):
        metadata, raw = original_acquire(*args, **kwargs)
        archives[br.TRANSFER_STATE.pending[1]] = (metadata, raw)
        return metadata, raw
    br.acquire = archived_acquire
    def read_spe(path):
        metadata, raw = archives[path]
        return 512, 1, 1, metadata['winspec_datatype'], raw, metadata['settings']
    br.read_spe_frames = read_spe
    br.settings_from_spe = bridge_functions(dict(math=__import__('math')), 'settings_from_spe')['settings_from_spe']
    return br, settings, events, documents, calls


def test_probe_compares_owned_document_with_fresh_documents_and_restores_settings(tmp_path):
    probe = load_probe()
    br, settings, events, documents, calls = fake_bridge()
    original_start = br.start_new_document
    recorded = []
    def persist(report):
        if report['status'] == 'running':
            assert br.TRANSFER_PENDING.is_set()
            assert br.TRANSFER_STATE.pending is not None
        recorded[:] = report['frames']
        events.append(('persist', len(recorded)))
    report = probe.run_probe(br, str(tmp_path), persist)
    assert report['status'] == 'complete' and report['restore_ok'] is True
    assert len(recorded) == 11
    assert [f['mode'] for f in recorded] == ['warmup'] + ['fresh', 'reuse'] * 5
    assert len(documents) == 6
    assert all(calls[i] is calls[0] for i in (2,4,6,8,10))
    assert len({id(calls[i]) for i in (1,3,5,7,9)}) == 5
    assert all(doc.closed for doc in documents)
    assert len({f['spe_path'] for f in recorded}) == 11
    assert all(len(f['counts']) == 512 for f in recorded)
    assert all(f['mean_counts_per_exposure'] == (i+1)/2 for i,f in enumerate(recorded))
    assert settings['exposure_ms'] == 800. and settings['accumulations'] == 1
    assert br.start_new_document is original_start and br.SPE_PATH == 'original.spe'
    assert not br.TRANSFER_PENDING.is_set()


@pytest.mark.parametrize('failure', ['capture', 'header', 'persist', 'apply'])
def test_probe_stops_at_first_failure_keeps_failed_document_and_restores_settings(tmp_path, failure):
    probe = load_probe()
    br, settings, events, documents, calls = fake_bridge(failure)
    original_start = br.start_new_document
    reports = []
    def persist(report):
        if failure == 'persist' and len(report['frames']) == 4:
            raise OSError('disk full')
        reports.append(dict(report))
    report = probe.run_probe(br, str(tmp_path), persist)
    assert report['status'] == 'failed' and report['restore_ok'] is True
    assert len(calls) == (0 if failure == 'apply' else 4)
    assert settings['exposure_ms'] == 800. and settings['accumulations'] == 1
    assert br.start_new_document is original_start and br.SPE_PATH == 'original.spe'
    if calls:
        assert not calls[-1].closed
    assert 'error' in report
    if failure in ('persist', 'header'):
        assert br.TRANSFER_PENDING.is_set()


def test_probe_refuses_busy_winspec_without_setting_writes(tmp_path):
    probe = load_probe()
    br, settings, _, _, calls = fake_bridge()
    settings['running'] = True
    br.apply_settings = lambda *a:pytest.fail('busy camera must not be configured')
    with pytest.raises(RuntimeError, match='busy'):
        probe.run_probe(br, str(tmp_path), lambda report:None)
    assert not calls


def test_probe_never_restores_settings_while_watchdog_stop_is_stalled(tmp_path):
    probe = load_probe()
    br, settings, _, _, calls = fake_bridge()
    original_apply = br.apply_settings
    def acquire(*a, **kw):
        br.TEMPERATURE_MONITOR_UNHEALTHY.set()
        raise RuntimeError('Stop stalled')
    br.acquire = acquire
    def apply(exp, requested):
        assert not br.TEMPERATURE_MONITOR_UNHEALTHY.is_set()
        return original_apply(exp, requested)
    br.apply_settings = apply
    report = probe.run_probe(br, str(tmp_path), lambda report:None)
    assert report['status'] == 'failed' and report['restore_ok'] is False
    assert 'restore_error' in report and not calls


def test_probe_port_reservation_excludes_a_server_or_second_probe():
    probe = load_probe()
    occupied = socket.socket()
    occupied.bind(('127.0.0.1', 0))
    port = occupied.getsockname()[1]
    try:
        with pytest.raises(RuntimeError, match='SERVER'):
            probe.reserve_bridge_port(port)
    finally:
        occupied.close()
    lease = probe.reserve_bridge_port(port)
    try:
        with pytest.raises(RuntimeError, match='SERVER'):
            probe.reserve_bridge_port(port)
    finally:
        lease.close()
    probe.reserve_bridge_port(port).close()


@pytest.mark.parametrize('value', [float('nan'), float('inf')])
def test_probe_rejects_nonfinite_frame_counts(value):
    probe = load_probe()
    metadata = dict(width=512, height=1, frame_count=1, winspec_datatype=0,
                    temperature_guard=dict(version=4, passed=True, sample_count=2),
                    settings_scope='configured_plus_spe', settings=dict(exposure_ms=500., accumulations=2))
    with pytest.raises(RuntimeError, match='non-finite'):
        probe.validate_frame(metadata, struct.pack('<512f', *([value]*512)),
                             dict(exposure_ms=500., accumulations=2))


def test_probe_checks_previously_saved_spe_files_after_document_reuse(tmp_path):
    probe = load_probe()
    br, _, _, _, _ = fake_bridge()
    read = br.read_spe_frames
    def corrupt(path):
        width, height, frames, datatype, raw, hardware = read(path)
        if path == '1.spe':
            raw = struct.pack('<512H', *([999]*512))
        return width, height, frames, datatype, raw, hardware
    br.read_spe_frames = corrupt
    report = probe.run_probe(br, str(tmp_path), lambda report:None)
    assert report['status'] == 'failed' and report['restore_ok'] is True
    assert 'saved SPE changed' in report['error']


def test_report_write_failure_preserves_previously_durable_snapshot(tmp_path, monkeypatch):
    probe = load_probe()
    shared = tmp_path / 'shared.json'
    first = dict(status='running', frames=[dict(counts=[10])])
    probe.persist_reports(first, str(tmp_path), str(shared))
    def fail_dump(value, handle, **kwargs):
        handle.write('{partial')
        raise OSError('disk full')
    monkeypatch.setattr(probe.json, 'dump', fail_dump)
    with pytest.raises(OSError, match='disk full'):
        probe.persist_reports(dict(status='running', frames=[dict(counts=[10]),dict(counts=[20])]),
                              str(tmp_path), str(shared))
    snapshots = list(tmp_path.glob('frame-001-*.json'))
    assert len(snapshots) == 1
    assert json.loads(snapshots[0].read_text()) == first
    assert json.loads(shared.read_text()) == first


def test_probe_rejects_start_that_returns_a_different_document_in_reuse_mode(tmp_path):
    probe = load_probe()
    br, _, _, _, calls = fake_bridge('replacement')
    report = probe.run_probe(br, str(tmp_path), lambda report:None)
    assert report['status'] == 'failed' and report['restore_ok'] is True
    assert len(calls) == 3
    assert 'different document' in report['error']
