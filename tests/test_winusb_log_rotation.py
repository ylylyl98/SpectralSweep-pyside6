"""Per-frame log association and real native rotation against fake USB only."""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

import pytest


def log_module():
    assert importlib.util.find_spec('app.devices.winusb_logs'), 'Segment-aware log reader is missing'
    return importlib.import_module('app.devices.winusb_logs')


def test_frame_log_reader_uses_receipt_segment_and_rejects_path_escape(tmp_path):
    target = log_module()
    meta = dict(session_evidence_directory=str(tmp_path), native_receipt=dict(log_segment=2))
    (tmp_path/'session-000002.jsonl').write_text('{"stage":"request_sequence","value":7}\n')
    assert target.frame_log_path(meta) == tmp_path/'session-000002.jsonl'
    assert target.read_frame_log(meta)[0]['value'] == 7
    meta['native_receipt']['log_segment'] = '../foreign'
    with pytest.raises(ValueError): target.frame_log_path(meta)


def test_legacy_log_reader_preserves_existing_evidence(tmp_path):
    target = log_module()
    (tmp_path/'session.jsonl').write_text('{"stage":"configure","value":1}\n')
    meta = dict(session_evidence_directory=str(tmp_path))
    assert target.frame_log_path(meta) == tmp_path/'session.jsonl'
    assert target.read_frame_log(meta)[0]['stage'] == 'configure'


def native_runtime():
    value = os.environ.get('SPECTRALSWEEP_WINUSB_ROTATION_RUNTIME')
    if not value:
        pytest.skip('Explicit candidate native fake-transport runtime required')
    runtime = Path(value).resolve()
    assert runtime.name == 'offline-runtime', 'Never execute physical transport in tests'
    return runtime


def test_native_many_rotations_preserve_one_init_every_frame_and_balanced_handles(tmp_path):
    runtime = native_runtime()
    native = tmp_path/'native'
    session = str(uuid.uuid4())
    requests = [str(uuid.uuid4()) for _ in range(80)]
    commands = ''.join(f'C {i+1} {r}\n' for i,r in enumerate(requests))+'Q\n'
    result = subprocess.run([str(runtime/'persistent_ingaas.exe'), '--offline-session', str(runtime),
                             str(native), session, '4096', '13'], input=commands,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, 'Rotation-capable helper required: '+result.stdout+result.stderr
    ready = json.loads((native/'ready.json').read_text())
    assert ready['log_protocol'] == 1
    segments = sorted(native.glob('session-*.sealed.json'))
    assert len(segments) >= 20
    all_rows = []
    seen = []
    for index, seal_path in enumerate(segments):
        seal = json.loads(seal_path.read_text())
        assert seal['segment'] == index and seal['session_id'] == session and seal['error'] == 0
        path = native/('session.jsonl' if index == 0 else f'session-{index:06d}.jsonl')
        data = path.read_bytes()
        assert len(data) == seal['bytes'] and data.endswith(b'\n')
        rows = [json.loads(line) for line in data.splitlines()]
        current = [r['value'] for r in rows if r['stage'] == 'request_sequence']
        assert current == list(range(seal['first_sequence'], seal['last_sequence']+1))
        seen.extend(current); all_rows.extend(rows)
    assert seen == list(range(1,81))
    assert sum(r['stage'] == 'configure' for r in all_rows) == 1
    assert sum(r['stage'] == 'identity' for r in all_rows) == 1
    for i, request in enumerate(requests, 1):
        folder = native/f'frame-{i:06d}'
        proof = json.loads((folder/'frame.json').read_text())
        assert proof['session_id'] == session and proof['request_id'] == request and proof['sequence'] == i
        rows = log_module().read_frame_log(dict(session_evidence_directory=str(native), native_receipt=proof))
        assert sum(r['stage'] == 'request_sequence' and r['value'] == i for r in rows) == 1
        assert (folder/'frame-1024.bin').read_bytes() == bytes((7*j+31+i)&255 for j in range(1024))
    closed = json.loads((native/'closed.json').read_text())
    assert closed['completed_frames'] == 80 and closed['cleanup_verified'] and closed['error'] == 0
    resource = next(json.loads(line) for line in result.stdout.splitlines() if 'persistent_native_fake' in line)
    assert resource['starts'] == 80 and resource['reads'] == 80
    assert resource['handles_before'] == resource['handles_after']
    assert len(result.stdout) < 4096, 'Per-frame stdout mirror would bypass rotation and grow forever'


def test_bridge_reclaims_sealed_logs_without_reconnecting(tmp_path, monkeypatch):
    import hashlib
    import struct
    import threading
    from app.devices import winusb_persistent as target
    from app.devices.winusb_retention import CachePolicy
    from app.experiment_integration import persist_output_snapshot
    runtime = native_runtime()
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path/'history.sqlite'))
    original = subprocess.Popen
    def launch(command, **kwargs):
        mode = '--offline-recipe-session' if command[1] == '--recipe-session' else '--offline-session'
        return original([command[0], mode, *command[2:], '13'], **kwargs)
    monkeypatch.setattr(target.subprocess, 'Popen', launch)
    monkeypatch.setattr(target, 'inspect_device', lambda: {'present': True, 'service': 'WINUSB'})
    release = target.manifest()
    release['log_protocol'] = 1
    release['runtime_files'] = {p.relative_to(runtime).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in runtime.rglob('*') if p.is_file()}
    monkeypatch.setattr(target, 'manifest', lambda: release)
    owner = target.PersistentCaptureBridge(runtime_dir=runtime, state_dir=tmp_path/'state')
    owner._cache.policy = CachePolicy(keep_frames=2, max_frames=8, min_free_bytes=0, max_session_log_bytes=4096)
    records = []
    try:
        for i in range(36):
            meta, payload = owner.capture(threading.Event())
            records.append(meta)
            assert payload == bytes((7*j+32+i)&255 for j in range(1024))
            snapshot = dict(native_capture=meta, raw_accumulated_counts=list(struct.unpack('<512H', payload)))
            output = tmp_path/f'export-{i}.csv'
            output.write_text('pixel,counts\n'+''.join(f'{j+1},{x}\n' for j,x in enumerate(snapshot['raw_accumulated_counts'])))
            sidecar = persist_output_snapshot(output, experiment_type='spectrum_preview', device_id='test',
                                             settings={'acquisition_time_snapshot': snapshot})
            assert owner._cache.acknowledge_saved(snapshot, output, sidecar)
            assert len(list((owner._output/'native').glob('session*.jsonl'))) <= 4
        assert records[-1]['native_receipt']['log_segment'] >= 12
        assert len({m['process_pid'] for m in records}) == 1
        assert sum(m['cold_start'] for m in records) == 1
    finally:
        owner.close()
    assert not owner._claim.exists()
    assert len(list((owner._output/'native').glob('frame-*'))) == 2
    assert (owner._output/'native/session.jsonl').exists()
    assert json.loads((owner._output/'native/closed.json').read_text())['completed_frames'] == 36


def test_native_log_write_fault_immediately_before_start_issues_no_start(tmp_path):
    runtime = native_runtime()
    native = tmp_path/'native'
    result = subprocess.run([str(runtime/'persistent_ingaas.exe'), '--offline-session', str(runtime),
                             str(native), str(uuid.uuid4()), '4096', '24'],
                            input=f'C 1 {uuid.uuid4()}\nQ\n', capture_output=True, text=True, timeout=20)
    assert (native/'ready.json').exists(), 'Fault must occur after successful initialization'
    closed = json.loads((native/'closed.json').read_text())
    assert result.returncode == 1 and closed['cleanup_verified'] and closed['error']
    assert closed['completed_frames'] == 0 and not list(native.glob('frame-*/frame.json'))
    resource = next(json.loads(line) for line in result.stdout.splitlines() if 'persistent_native_fake' in line)
    assert resource['starts'] == resource['reads'] == 0
    assert resource['handles_before'] == resource['handles_after']


@pytest.mark.parametrize('blocked', ['next_log', 'state_receipt', 'seal_receipt'])
def test_native_rotation_failure_preserves_prior_frame_without_starting_another(tmp_path, blocked):
    runtime = native_runtime()
    native = tmp_path/'native'
    process = subprocess.Popen([str(runtime/'persistent_ingaas.exe'), '--offline-session', str(runtime),
                                str(native), str(uuid.uuid4()), '4096', '13'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    def wait_json(path):
        deadline = time.monotonic()+15
        while not path.exists():
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(.005)
        return json.loads(path.read_text())
    count = 0
    try:
        wait_json(native/'ready.json')
        # Admit whole frames until the active segment requires rotation.
        while True:
            count += 1
            process.stdin.write(f'C {count} {uuid.uuid4()}\n'); process.stdin.flush()
            proof = wait_json(native/f'frame-{count:06d}/frame.json')
            segment = proof['log_segment']
            logpath = log_module().frame_log_path(dict(session_evidence_directory=str(native), native_receipt=proof))
            if logpath.stat().st_size >= 4096: break
            assert count < 10
        prior = (native/f'frame-{count:06d}/frame-1024.bin').read_bytes()
        name = (f'session-{segment+1:06d}.jsonl' if blocked == 'next_log' else
                'log-state.json.partial' if blocked == 'state_receipt' else f'session-{segment:06d}.sealed.json.partial')
        (native/name).mkdir()
        stdout, stderr = process.communicate(f'C {count+1} {uuid.uuid4()}\nQ\n', timeout=20)
    finally:
        if process.poll() is None:
            process.communicate('Q\n', timeout=20)
    assert process.returncode == 1, stdout+stderr
    closed = json.loads((native/'closed.json').read_text())
    assert closed['cleanup_verified'] and closed['error'] and closed['completed_frames'] == count
    assert not (native/f'frame-{count+1:06d}/frame.json').exists()
    assert (native/f'frame-{count:06d}/frame-1024.bin').read_bytes() == prior
    resource = next(json.loads(line) for line in stdout.splitlines() if 'persistent_native_fake' in line)
    assert resource['starts'] == resource['reads'] == count
    assert resource['handles_before'] == resource['handles_after']
