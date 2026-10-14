"""Cache deletion requires an actual saved export, never just frame delivery."""
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import struct
import uuid

import pytest


def module():
    assert importlib.util.find_spec('app.devices.winusb_retention'), 'Safe capture retention is missing'
    return importlib.import_module('app.devices.winusb_retention')


def cache(tmp_path, **settings):
    target = module()
    policy = target.CachePolicy(keep_frames=1, min_free_bytes=0, **settings)
    return target.CaptureCache(tmp_path/'state', policy=policy)


def session(owner, number=1, *, rotating=False):
    sid = str(uuid.uuid4())
    path = owner.root/'sessions'/f'20261014T120000-{number:08x}'
    (path/'native').mkdir(parents=True)
    for name in ('ready.json', 'eeprom.bin', 'detector-eeprom.bin', 'session.jsonl'):
        (path/'native'/name).write_text('{}')
    (path/'process.json').write_text(json.dumps({'session_id': sid}))
    if rotating:
        (path/'native/log-state.json').write_text(json.dumps(dict(
            protocol=1, session_id=sid, segment=0, first_sequence=1, rotation_bytes=4096)))
    owner.begin_session(path, sid, **({'log_protocol': 1} if rotating else {}))
    return path, sid


def frame(owner, current, sequence):
    path, sid = current
    folder = path/'native'/f'frame-{sequence:06d}'
    folder.mkdir()
    counts = [sequence]*512
    payload = struct.pack('<512H', *counts)
    proof = dict(protocol=1, session_id=sid, request_id=str(uuid.uuid4()), sequence=sequence,
                 drained=True, request_worker_joined=True, delivery_worker_joined=True,
                 stop_reply_valid=True, stop_write_completed=True, transport_equal=True, error=0)
    if (path/'native/log-state.json').exists():
        proof['log_segment'] = json.loads((path/'native/log-state.json').read_text())['segment']
    meta = dict(acquisition_backend='winusb_local', bridge_session_id=sid,
                request_sequence=sequence, request_id=proof['request_id'],
                evidence_directory=str(folder), payload_sha256=hashlib.sha256(payload).hexdigest(),
                frame_cleanup_verified=True, native_receipt=proof)
    for name in ('frame-1024.bin', 'data82-transport.bin'):
        (folder/name).write_bytes(payload)
    (folder/'frame.json').write_text(json.dumps(proof))
    (path/f'request-{sequence:06d}.json').write_text(json.dumps(proof))
    (path/f'validated-frame-{sequence:06d}.json').write_text(json.dumps(meta))
    owner.record_frame(meta)
    return {'native_capture': meta, 'raw_accumulated_counts': counts}


def save(owner, snapshot, directory, name='spectrum'):
    from app.experiment_integration import persist_output_snapshot
    directory.mkdir(exist_ok=True)
    output = directory/f'{name}.csv'
    output.write_text('pixel,intensity_counts\n'+''.join(
        f'{i+1},{x}\n' for i, x in enumerate(snapshot['raw_accumulated_counts'])))
    sidecar = persist_output_snapshot(output, experiment_type='spectrum_preview', device_id='test',
                                     settings={'acquisition_time_snapshot': snapshot})
    assert owner.acknowledge_saved(snapshot, output, sidecar)
    return output, sidecar


def test_only_saved_old_frames_removed_and_ack_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path/'history.sqlite'))
    owner = cache(tmp_path)
    current = session(owner)
    old = frame(owner, current, 1)
    unsaved = frame(owner, current, 2)
    newest = frame(owner, current, 3)
    save(owner, old, tmp_path/'exports')
    save(owner, newest, tmp_path/'exports', 'newest')
    restarted = module().CaptureCache(owner.root, policy=owner.policy)
    restarted.before_capture(current[1])
    assert not Path(old['native_capture']['evidence_directory']).exists()
    assert Path(unsaved['native_capture']['evidence_directory']).exists()
    assert Path(newest['native_capture']['evidence_directory']).exists()
    assert not (current[0]/'request-000001.json').exists()
    assert (current[0]/'native/session.jsonl').exists()


@pytest.mark.parametrize('damage', ['raw', 'metadata', 'output', 'identity'])
def test_invalid_or_changed_saved_artifact_cannot_authorize_deletion(tmp_path, monkeypatch, damage):
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path/'history.sqlite'))
    owner = cache(tmp_path)
    current = session(owner)
    old = frame(owner, current, 1)
    frame(owner, current, 2)
    output, sidecar = save(owner, old, tmp_path/'exports')
    if damage == 'output':
        output.write_text('replacement measurement')
    else:
        saved = json.loads(sidecar.read_text())
        snap = saved['settings']['requested']['acquisition_time_snapshot']
        if damage == 'raw': snap['raw_accumulated_counts'][0] += 1
        if damage == 'identity': snap['native_capture']['request_id'] = str(uuid.uuid4())
        if damage == 'metadata': saved['status'] = 'failed'
        sidecar.write_text(json.dumps(saved))
    owner.before_capture(current[1])
    assert Path(old['native_capture']['evidence_directory']).exists()


def test_failed_session_and_unfinished_frame_remain_protected(tmp_path, monkeypatch):
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path/'history.sqlite'))
    owner = cache(tmp_path)
    current = session(owner)
    old = frame(owner, current, 1)
    save(owner, old, tmp_path/'exports')
    partial = current[0]/'native/frame-000002'
    partial.mkdir(); (partial/'frame.json.partial').write_text('unfinished')
    owner.finish_session(current[1], failed=True)
    other = session(owner, 2)
    frame(owner, other, 1)
    owner.before_capture(other[1])
    assert Path(old['native_capture']['evidence_directory']).exists()
    assert (partial/'frame.json.partial').read_text() == 'unfinished'


def test_no_saved_candidates_blocks_at_limit_and_keeps_all_data(tmp_path):
    owner = cache(tmp_path, max_frames=2)
    current = session(owner)
    old = frame(owner, current, 1); new = frame(owner, current, 2)
    with pytest.raises(module().CacheCapacityError, match='cache'):
        owner.before_capture(current[1])
    assert Path(old['native_capture']['evidence_directory']).exists()
    assert Path(new['native_capture']['evidence_directory']).exists()


def test_other_active_owner_prevents_any_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path/'history.sqlite'))
    owner = cache(tmp_path)
    current = session(owner)
    old = frame(owner, current, 1); frame(owner, current, 2)
    save(owner, old, tmp_path/'exports')
    (owner.root/'active.json').write_text(json.dumps({'session_id': str(uuid.uuid4())}))
    with pytest.raises(module().CacheCapacityError, match='owner'):
        owner.before_capture(current[1])
    assert Path(old['native_capture']['evidence_directory']).exists()


def test_unknown_historical_session_is_never_adopted_or_deleted(tmp_path):
    owner = cache(tmp_path)
    old = owner.root/'sessions/historical/native/frame-000001'
    old.mkdir(parents=True); (old/'frame.json').write_text('original evidence')
    owner.before_capture()
    assert (old/'frame.json').read_text() == 'original evidence'


def test_session_log_budget_stops_before_next_capture(tmp_path):
    owner = cache(tmp_path, max_session_log_bytes=32)
    current = session(owner)
    (current[0]/'native/session.jsonl').write_bytes(b'x'*33)
    with pytest.raises(module().CacheCapacityError, match='log'):
        owner.before_capture(current[1])
    assert (current[0]/'native/session.jsonl').stat().st_size == 33


def close_session(owner, current, completed):
    path, sid = current
    if (path/'native/log-state.json').exists():
        seal_log(current, completed)
    (path/'native/closed.json').write_text(json.dumps(dict(
        protocol=1, session_id=sid, cleanup_verified=True, completed_frames=completed, error=0)))
    owner.finish_session(sid, failed=False)


def test_old_fully_saved_closed_session_logs_removed_but_recent_session_kept(tmp_path, monkeypatch):
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path/'history.sqlite'))
    owner = cache(tmp_path, keep_sessions=1)
    first = session(owner)
    old = frame(owner, first, 1)
    save(owner, old, tmp_path/'exports')
    close_session(owner, first, 1)
    second = session(owner, 2)
    frame(owner, second, 1)
    close_session(owner, second, 1)
    owner.before_capture()
    assert not first[0].exists()
    assert (second[0]/'native/session.jsonl').exists()


def test_cleanup_skips_locked_frame_and_processes_next_eligible_frame(tmp_path, monkeypatch):
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path/'history.sqlite'))
    owner = cache(tmp_path)
    current = session(owner)
    first = frame(owner, current, 1); second = frame(owner, current, 2)
    frame(owner, current, 3)
    save(owner, first, tmp_path/'exports', 'first')
    save(owner, second, tmp_path/'exports', 'second')
    locked = Path(first['native_capture']['evidence_directory'])/'frame-1024.bin'
    original = Path.unlink
    def unlink(path, *args, **kwargs):
        if path == locked: raise PermissionError('external reader')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', unlink)
    owner.before_capture(current[1])
    assert locked.exists()
    assert not Path(second['native_capture']['evidence_directory']).exists()


def test_junction_or_symlink_does_not_allow_cleanup_outside_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path/'history.sqlite'))
    owner = cache(tmp_path)
    current = session(owner)
    old = frame(owner, current, 1); frame(owner, current, 2)
    save(owner, old, tmp_path/'exports')
    folder = Path(old['native_capture']['evidence_directory'])
    outside = tmp_path/'external-frame'; folder.rename(outside)
    try:
        folder.symlink_to(outside, target_is_directory=True)
    except OSError:
        import sys
        if sys.platform != 'win32':
            pytest.skip('OS does not permit creating test symlink')
        import _winapi
        _winapi.CreateJunction(str(outside), str(folder))
    owner.before_capture(current[1])
    assert (outside/'frame-1024.bin').exists()


def test_panel_save_acknowledges_frozen_acquisition_only_after_metadata_success(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import numpy as np
    from ui.spectrum_panel import SpectrumPanel
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path/'history.sqlite'))
    owner = cache(tmp_path)
    monkeypatch.setattr(module(), 'default_state_dir', lambda: owner.root)
    current = session(owner)
    old = frame(owner, current, 1); newer = frame(owner, current, 2)
    panel = SpectrumPanel.__new__(SpectrumPanel)
    panel._ctrl = SimpleNamespace(identity={})
    panel._last_data_kind = 'spectrum_1d'
    panel._last_data = np.array(old['raw_accumulated_counts'])
    panel._last_wavelength = np.arange(1, 513)
    panel._last_acquisition_snapshot = old
    panel._acquisition_settings_snapshot = newer
    panel.save_current(tmp_path/'saved.csv')
    owner.before_capture(current[1])
    assert not Path(old['native_capture']['evidence_directory']).exists()
    assert Path(newer['native_capture']['evidence_directory']).exists()


def test_metadata_failure_does_not_acknowledge_or_remove_frame(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import numpy as np
    from ui.spectrum_panel import SpectrumPanel
    import app.experiment_integration as persistence
    owner = cache(tmp_path)
    monkeypatch.setattr(module(), 'default_state_dir', lambda: owner.root)
    current = session(owner)
    old = frame(owner, current, 1); frame(owner, current, 2)
    panel = SpectrumPanel.__new__(SpectrumPanel)
    panel._ctrl = SimpleNamespace(identity={})
    panel._last_data_kind = 'spectrum_1d'
    panel._last_data = np.array(old['raw_accumulated_counts'])
    panel._last_wavelength = np.arange(1, 513)
    panel._last_acquisition_snapshot = old
    def failed(*args, **kwargs): raise OSError('metadata disk full')
    monkeypatch.setattr(persistence, 'persist_output_snapshot', failed)
    with pytest.raises(OSError, match='metadata disk full'):
        panel.save_current(tmp_path/'failed.csv')
    owner.before_capture(current[1])
    assert Path(old['native_capture']['evidence_directory']).exists()


def test_cleanup_is_batched_and_locked_candidate_does_not_starve_later_frames(tmp_path, monkeypatch):
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path/'history.sqlite'))
    owner = cache(tmp_path, batch_size=1)
    current = session(owner)
    first = frame(owner, current, 1); second = frame(owner, current, 2)
    third = frame(owner, current, 3); frame(owner, current, 4)
    output, _ = save(owner, first, tmp_path/'exports', 'first')
    save(owner, second, tmp_path/'exports', 'second')
    save(owner, third, tmp_path/'exports', 'third')
    output.write_text('overwritten export')
    owner.before_capture(current[1])
    assert Path(second['native_capture']['evidence_directory']).exists()
    owner.before_capture(current[1])
    assert not Path(second['native_capture']['evidence_directory']).exists()
    assert Path(first['native_capture']['evidence_directory']).exists()
    assert Path(third['native_capture']['evidence_directory']).exists()


@pytest.mark.parametrize('name', ['frame-1024.bin', 'frame.json'])
def test_changed_native_evidence_is_not_deleted_using_an_old_save(tmp_path, monkeypatch, name):
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path/'history.sqlite'))
    owner = cache(tmp_path)
    current = session(owner)
    old = frame(owner, current, 1); frame(owner, current, 2)
    save(owner, old, tmp_path/'exports')
    folder = Path(old['native_capture']['evidence_directory'])
    (folder/name).write_bytes(b'other evidence')
    owner.before_capture(current[1])
    assert (folder/name).read_bytes() == b'other evidence'
    assert (folder/'data82-transport.bin').exists()


@pytest.mark.parametrize('budget', ['max_bytes', 'max_files', 'disk_free'])
def test_all_capacity_limits_protect_unsaved_data(tmp_path, monkeypatch, budget):
    from collections import namedtuple
    settings = {budget: 1} if budget != 'disk_free' else {}
    owner = cache(tmp_path, **settings)
    current = session(owner)
    old = frame(owner, current, 1)
    if budget == 'disk_free':
        usage = namedtuple('usage', 'total used free')(1000, 1000, 0)
        monkeypatch.setattr(module().shutil, 'disk_usage', lambda path: usage)
    with pytest.raises(module().CacheCapacityError):
        owner.before_capture(current[1])
    assert Path(old['native_capture']['evidence_directory']).exists()


def test_configured_policy_applies_and_invalid_policy_fails_closed(tmp_path):
    target = module()
    tmp_path.mkdir(exist_ok=True)
    (tmp_path/'retention-policy.json').write_text(json.dumps({'keep_frames': 20, 'max_frames': 200}))
    assert target.CaptureCache(tmp_path).policy.keep_frames == 20
    (tmp_path/'retention-policy.json').write_text(json.dumps({'max_frames': 1}))
    with pytest.raises(ValueError): target.CaptureCache(tmp_path)


def test_inventory_failure_cannot_publish_partial_usage_and_allow_next_request(tmp_path, monkeypatch):
    owner = cache(tmp_path, max_bytes=2*1024*1024)
    blocked = owner.root/'sessions/historical/large.bin'
    blocked.parent.mkdir(parents=True); blocked.write_bytes(b'x'*(3*1024*1024))
    original = Path.stat
    def stat(path, *args, **kwargs):
        if path == blocked: raise PermissionError('cannot inspect evidence')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', stat)
    for _ in range(2):
        with pytest.raises((PermissionError, module().CacheCapacityError)):
            owner.before_capture()


def test_legacy_captures_count_towards_capacity_and_are_never_deleted(tmp_path):
    owner = cache(tmp_path, max_bytes=2*1024*1024)
    legacy = owner.root/'captures/old-run/raw.bin'
    legacy.parent.mkdir(parents=True); legacy.write_bytes(b'x'*(3*1024*1024))
    with pytest.raises(module().CacheCapacityError): owner.before_capture()
    assert legacy.stat().st_size == 3*1024*1024


def test_unreadable_subdirectory_cannot_be_silently_omitted(tmp_path, monkeypatch):
    owner = cache(tmp_path)
    blocked = owner.root/'sessions/blocked'
    blocked.mkdir(parents=True)
    original = module().os.scandir
    def scandir(path):
        if Path(path) == blocked: raise PermissionError('directory inaccessible')
        return original(path)
    monkeypatch.setattr(module().os, 'scandir', scandir)
    with pytest.raises((PermissionError, module().CacheCapacityError)):
        owner.before_capture()


def test_old_unsaved_session_cannot_starve_cleanup_of_other_closed_logs(tmp_path, monkeypatch):
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path/'history.sqlite'))
    owner = cache(tmp_path, keep_sessions=1, batch_size=1)
    first = session(owner)
    old = frame(owner, first, 1)
    save(owner, old, tmp_path/'exports'); close_session(owner, first, 1)
    second = session(owner, 2)
    frame(owner, second, 1); close_session(owner, second, 1)
    third = session(owner, 3)
    frame(owner, third, 1); close_session(owner, third, 1)
    owner.before_capture()
    assert second[0].exists()
    assert not first[0].exists()


def test_capacity_check_recovers_after_user_archives_historical_cache(tmp_path):
    owner = cache(tmp_path, max_frames=2)
    legacy = owner.root/'sessions/historical/native/frame-000001/raw.bin'
    legacy.parent.mkdir(parents=True); legacy.write_bytes(b'x'*(3*1024*1024))
    (legacy.parent.parent/'frame-000002').mkdir()
    with pytest.raises(module().CacheCapacityError): owner.before_capture()
    legacy.parent.rename(tmp_path/'archived-frame')
    owner.before_capture()
    assert (tmp_path/'archived-frame/raw.bin').exists()


def test_legacy_capture_directories_count_towards_frame_limit(tmp_path):
    owner = cache(tmp_path, max_frames=2)
    for number in range(2):
        folder = owner.root/f'captures/old-{number}/capture'
        folder.mkdir(parents=True); (folder/'frame-1024.bin').write_bytes(b'original')
    with pytest.raises(module().CacheCapacityError): owner.before_capture()
    assert (owner.root/'captures/old-0/capture/frame-1024.bin').read_bytes() == b'original'


def test_incomplete_session_close_proof_keeps_logs(tmp_path, monkeypatch):
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path/'history.sqlite'))
    owner = cache(tmp_path, keep_sessions=1)
    first = session(owner)
    saved = frame(owner, first, 1)
    save(owner, saved, tmp_path/'exports')
    owner.finish_session(first[1], failed=False)
    second = session(owner, 2)
    frame(owner, second, 1); close_session(owner, second, 1)
    owner.before_capture()
    assert (first[0]/'native/session.jsonl').exists()


def seal_log(current, completed):
    path, sid = current
    state = json.loads((path/'native/log-state.json').read_text())
    segment = state['segment']
    filename = 'session.jsonl' if segment == 0 else f'session-{segment:06d}.jsonl'
    seal = dict(protocol=1, session_id=sid, segment=segment, first_sequence=state['first_sequence'],
                last_sequence=completed, bytes=(path/'native'/filename).stat().st_size, error=0, closed=True)
    (path/'native'/f'session-{segment:06d}.sealed.json').write_text(json.dumps(seal))


def rotate_log(current, completed):
    seal_log(current, completed)
    path, sid = current
    state = json.loads((path/'native/log-state.json').read_text())
    segment = state['segment']
    state.update(segment=segment+1, first_sequence=completed+1)
    (path/'native/log-state.json').write_text(json.dumps(state))
    (path/'native'/f'session-{segment+1:06d}.jsonl').write_text('{}\n')


def test_rotation_capable_session_does_not_stop_at_old_log_threshold(tmp_path):
    owner = cache(tmp_path, max_session_log_bytes=4096)
    current = session(owner, rotating=True)
    (current[0]/'native/session.jsonl').write_bytes(b'x'*4097)
    owner.before_capture(current[1])
    assert (current[0]/'native/session.jsonl').stat().st_size == 4097


def test_sealed_log_waits_for_every_frame_to_be_saved_and_outside_recent_window(tmp_path, monkeypatch):
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path/'history.sqlite'))
    owner = cache(tmp_path)
    current = session(owner, rotating=True)
    rotate_log(current, 0)
    first = frame(owner, current, 1); second = frame(owner, current, 2)
    rotate_log(current, 2)
    frame(owner, current, 3)
    save(owner, first, tmp_path/'exports', 'first')
    owner.before_capture(current[1])
    sealed = current[0]/'native/session-000001.jsonl'
    assert sealed.exists(), 'Unsaved second frame needs its log segment'
    save(owner, second, tmp_path/'exports', 'second')
    owner.before_capture(current[1])
    assert not sealed.exists()
    assert not (current[0]/'native/session-000001.sealed.json').exists()
    assert (current[0]/'native/session.jsonl').exists(), 'Initialization evidence must remain'
    assert (current[0]/'native/session-000002.jsonl').exists(), 'Active segment must remain'


@pytest.mark.parametrize('damage', ['missing_seal', 'wrong_sequence', 'wrong_session', 'truncated'])
def test_invalid_rotation_evidence_stops_admission_and_preserves_logs(tmp_path, damage):
    owner = cache(tmp_path)
    current = session(owner, rotating=True)
    frame(owner, current, 1)
    rotate_log(current, 1)
    seal_path = current[0]/'native/session-000000.sealed.json'
    if damage == 'missing_seal': seal_path.unlink()
    elif damage == 'truncated': (current[0]/'native/session.jsonl').write_bytes(b'')
    else:
        seal = json.loads(seal_path.read_text())
        if damage == 'wrong_sequence': seal['last_sequence'] = 2
        else: seal['session_id'] = str(uuid.uuid4())
        seal_path.write_text(json.dumps(seal))
    with pytest.raises((OSError, ValueError, RuntimeError)):
        owner.before_capture(current[1])
    assert (current[0]/'native/session.jsonl').exists()


def test_interrupted_segment_deletion_can_finish_after_cache_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path/'history.sqlite'))
    owner = cache(tmp_path)
    current = session(owner, rotating=True)
    rotate_log(current, 0)
    old = frame(owner, current, 1)
    rotate_log(current, 1)
    frame(owner, current, 2)
    save(owner, old, tmp_path/'exports')
    logfile = current[0]/'native/session-000001.jsonl'
    sealfile = current[0]/'native/session-000001.sealed.json'
    original = Path.unlink
    def unlink(path, *args, **kwargs):
        if path == sealfile: raise PermissionError('locked sealed receipt')
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', unlink)
        owner.before_capture(current[1])
        assert not logfile.exists() and sealfile.exists()
    restarted = module().CaptureCache(owner.root, policy=owner.policy)
    restarted.before_capture(current[1])
    assert not sealfile.exists()
    assert (current[0]/'native/session.jsonl').exists()
    assert (current[0]/'native/session-000002.jsonl').exists()


def test_old_rotating_session_prunes_only_after_normal_close_and_saved_frames(tmp_path, monkeypatch):
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path/'history.sqlite'))
    owner = cache(tmp_path, keep_sessions=1)
    first = session(owner, rotating=True)
    rotate_log(first, 0)
    saved = frame(owner, first, 1)
    save(owner, saved, tmp_path/'exports')
    close_session(owner, first, 1)
    second = session(owner, 2, rotating=True)
    frame(owner, second, 1)
    close_session(owner, second, 1)
    owner.before_capture()
    assert not first[0].exists()
    assert (second[0]/'native/session.jsonl').exists()


def test_failed_rotating_session_keeps_saved_frames_and_every_segment(tmp_path, monkeypatch):
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path/'history.sqlite'))
    owner = cache(tmp_path)
    current = session(owner, rotating=True)
    rotate_log(current, 0)
    saved = frame(owner, current, 1)
    save(owner, saved, tmp_path/'exports')
    rotate_log(current, 1)
    frame(owner, current, 2)
    owner.finish_session(current[1], failed=True)
    owner.cleanup()
    assert Path(saved['native_capture']['evidence_directory']).exists()
    assert len(list((current[0]/'native').glob('session*.jsonl'))) == 3


@pytest.mark.parametrize('batch_size', [1,4])
def test_late_save_is_reclaimed_during_uninterrupted_new_frame_arrivals(tmp_path, monkeypatch, batch_size):
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path/'history.sqlite'))
    owner = cache(tmp_path, batch_size=batch_size)
    current = session(owner)
    delayed = frame(owner, current, 1)
    for sequence in range(2, 7):
        saved = frame(owner, current, sequence)
        save(owner, saved, tmp_path/'exports', f'frame-{sequence}')
        owner.before_capture(current[1])
    save(owner, delayed, tmp_path/'exports', 'late')
    for sequence in range(7, 12):
        saved = frame(owner, current, sequence)
        save(owner, saved, tmp_path/'exports', f'frame-{sequence}')
        owner.before_capture(current[1])
    assert not Path(delayed['native_capture']['evidence_directory']).exists()


def test_unlocked_old_log_is_revisited_while_new_segments_keep_arriving(tmp_path, monkeypatch):
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path/'history.sqlite'))
    owner = cache(tmp_path, batch_size=1)
    current = session(owner, rotating=True)
    rotate_log(current, 0)
    saved = frame(owner, current, 1); save(owner, saved, tmp_path/'exports', 'first')
    rotate_log(current, 1)
    saved = frame(owner, current, 2); save(owner, saved, tmp_path/'exports', 'second')
    blocked = current[0]/'native/session-000001.sealed.json'
    original = Path.unlink
    def unlink(path, *args, **kwargs):
        if path == blocked: raise PermissionError('temporary log lock')
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', unlink)
        owner.before_capture(current[1])
    assert blocked.exists()
    for sequence in range(3,9):
        rotate_log(current, sequence-1)
        saved = frame(owner, current, sequence)
        save(owner, saved, tmp_path/'exports', f'frame-{sequence}')
        owner.before_capture(current[1])
    assert not blocked.exists()
