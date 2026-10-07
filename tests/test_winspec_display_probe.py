import importlib.util
import os
from pathlib import Path
import threading

import pytest

from tests.test_winspec_bridge_guard import bridge_functions
from tests.test_winspec_startup_probe import fake_bridge


def load_probe():
    spec = importlib.util.spec_from_file_location('tools.winspec.display_probe',
        Path('tools/winspec/startup_display_probe.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def display_bridge(tmp_path, failure=None):
    br, settings, events, docs, calls = fake_bridge('capture' if failure == 'capture' else None)
    display = {'value': -1, 'restoring': False}
    exp = br.create_experiment()
    br.const = lambda name:name
    br.get_param = lambda e, name: display['value'] if name == 'EXP_BSHOWWINDOW' else 1
    def set_param(name, value):
        assert name == 'EXP_BSHOWWINDOW', 'Only display may be written through SetParam'
        events.append(('display', value))
        if failure == 'set' and not value:
            return 1
        if failure == 'restore' and display['restoring']:
            return 1
        display['value'] = value
        return 0
    exp.SetParam = set_param
    original_apply = br.apply_settings
    def apply(e, requested):
        if requested['exposure_ms'] == 800.:
            display['restoring'] = True
        return original_apply(e, requested)
    br.apply_settings = apply
    original_capture = br.acquire
    hardware = {}
    def acquire(*args, **kwargs):
        if failure == 'stalled':
            br.TEMPERATURE_MONITOR_UNHEALTHY.set()
            raise RuntimeError('Stop stalled')
        metadata, raw = original_capture(*args, **kwargs)
        doc, _ = br.TRANSFER_STATE.pending
        path = tmp_path / ('source-%d.spe' % len(calls))
        path.write_bytes(raw)
        hardware[raw] = dict(metadata['settings'])
        br.TRANSFER_STATE.pending = (doc, str(path))
        return metadata, raw
    br.acquire = acquire
    def read_spe(path):
        raw = Path(path).read_bytes()
        return 512, 1, 1, 3, raw, hardware[raw]
    br.read_spe_frames = read_spe
    br.CLEANUP_FAILED = threading.Event()
    cleanup = bridge_functions(dict(TRANSFER_STATE=br.TRANSFER_STATE,
        TRANSFER_PENDING=br.TRANSFER_PENDING, CLEANUP_FAILED=br.CLEANUP_FAILED,
        CAMERA_LOCK=threading.Lock(), recv_exact=lambda sock,n:sock.recv(n),
        com_return_value=br.com_return_value, server_log=lambda *a:None,
        remove_if_unlocked=lambda path,attempts:os.remove(path) is None), 'finish_transfer')
    br.finish_transfer = cleanup['finish_transfer']
    return br, settings, display, events, docs, calls


def test_display_probe_changes_only_display_uses_fresh_docs_and_restores(tmp_path):
    probe = load_probe()
    br, settings, display, events, docs, calls = display_bridge(tmp_path)
    def persist(report):
        for frame in report['frames']:
            assert Path(frame['spe_archive']).exists()
        events.append(('persist', len(report['frames'])))
    report = probe.run_probe(br, str(tmp_path), persist)
    assert report['status'] == 'complete' and report['restore_ok']
    assert display['value'] == -1
    assert settings['exposure_ms'] == 800. and settings['accumulations'] == 1
    assert len(calls) == 12 and len({id(doc) for doc in calls}) == 12
    assert all(doc.closed for doc in docs)
    assert len(list(tmp_path.glob('archive-*.spe'))) == 12
    assert not list(tmp_path.glob('source-*.spe'))
    assert [frame['mode'] for frame in report['frames'][2:6]] == ['visible','hidden','hidden','visible']
    assert all(frame['display_readback'] == 0 for frame in report['frames'] if frame['mode'] == 'hidden')
    assert events.index(('persist',1)) < events.index(('save',0))


@pytest.mark.parametrize('failure', ['set','capture','persist'])
def test_display_probe_failure_restores_and_preserves_unacknowledged_data(tmp_path, failure):
    probe = load_probe()
    br, settings, display, _, docs, _ = display_bridge(tmp_path, failure)
    def persist(report):
        if failure == 'persist' and report['status'] == 'running' and len(report['frames']) == 1:
            raise OSError('Disk full')
    report = probe.run_probe(br, str(tmp_path), persist)
    assert report['status'] == 'failed' and report['restore_ok']
    assert display['value'] == -1 and settings['exposure_ms'] == 800.
    if failure == 'persist':
        assert not docs[0].closed
        assert list(tmp_path.glob('source-*.spe'))


def test_display_probe_refuses_busy_camera_before_any_write(tmp_path):
    probe = load_probe()
    br, settings, _, events, _, calls = display_bridge(tmp_path)
    settings['controller_running'] = True
    br.apply_settings = lambda *a:pytest.fail('Busy camera must not be configured')
    with pytest.raises(RuntimeError, match='busy'):
        probe.run_probe(br, str(tmp_path), lambda r:None)
    assert not events and not calls


def test_display_restore_failure_invalidates_successful_capture(tmp_path):
    probe = load_probe()
    br, _, _, _, _, _ = display_bridge(tmp_path, 'restore')
    report = probe.run_probe(br, str(tmp_path), lambda r:None)
    assert report['status'] == 'failed' and not report['restore_ok']
    assert 'display' in report['restore_errors']


def test_stalled_stop_blocks_all_restoration_writes(tmp_path):
    probe = load_probe()
    br, settings, _, events, _, _ = display_bridge(tmp_path, 'stalled')
    report = probe.run_probe(br, str(tmp_path), lambda r:None)
    assert report['status'] == 'failed' and not report['restore_ok']
    assert 'stalled' in report['restore_errors']['stalled']
    assert settings['exposure_ms'] == 500.
    assert [e for e in events if e[0]=='display'] == [('display',-1)]


def test_archive_write_failure_keeps_source_and_document(tmp_path, monkeypatch):
    probe = load_probe()
    br, settings, display, _, docs, calls = display_bridge(tmp_path)
    def fail(*args):
        raise OSError('Archive disk full')
    monkeypatch.setattr(probe, 'archive_spe', fail)
    report = probe.run_probe(br, str(tmp_path), lambda r:None)
    assert report['status'] == 'failed' and report['restore_ok']
    assert len(calls) == 1 and not docs[0].closed
    assert list(tmp_path.glob('source-*.spe'))
    assert settings['exposure_ms'] == 800. and display['value'] == -1


def test_successful_set_status_without_readback_change_blocks_hidden_capture(tmp_path):
    probe = load_probe()
    br, _, display, _, _, calls = display_bridge(tmp_path)
    exp = br.create_experiment()
    exp.SetParam = lambda key,value:0
    report = probe.run_probe(br, str(tmp_path), lambda r:None)
    assert report['status'] == 'failed' and 'readback' in report['error']
    assert len(calls) == 1 and display['value'] == -1


def test_cleanup_failure_halts_after_archived_frame(tmp_path):
    probe = load_probe()
    br, _, _, _, docs, calls = display_bridge(tmp_path)
    acquire = br.acquire
    def fail_save(*args, **kwargs):
        frame = acquire(*args, **kwargs)
        br.TRANSFER_STATE.pending[0].Save = lambda:False
        return frame
    br.acquire = fail_save
    report = probe.run_probe(br, str(tmp_path), lambda r:None)
    assert report['status'] == 'failed' and report['restore_ok']
    assert 'cleanup failed' in report['error'] and len(calls) == 1
    assert not docs[0].closed and br.CLEANUP_FAILED.is_set()
    assert list(tmp_path.glob('archive-*.spe')) and list(tmp_path.glob('source-*.spe'))


def test_final_archive_check_rejects_changed_earlier_data(tmp_path):
    probe = load_probe()
    br, _, _, _, _, _ = display_bridge(tmp_path)
    read = br.read_spe_frames
    checked = [False]
    def changed(path):
        width,height,count,datatype,raw,hardware = read(path)
        if Path(path).name.startswith('archive-01-'):
            if checked[0]:
                raw = bytes([0])*len(raw)
            checked[0] = True
        return width,height,count,datatype,raw,hardware
    br.read_spe_frames = changed
    report = probe.run_probe(br, str(tmp_path), lambda r:None)
    assert report['status'] == 'failed' and report['restore_ok']
    assert 'archive changed' in report['error']
    assert 'summary' not in report


def test_exposure_restoration_failure_still_restores_display(tmp_path):
    probe = load_probe()
    br, _, display, _, _, _ = display_bridge(tmp_path)
    apply = br.apply_settings
    def fail_restore(exp, requested):
        if requested['exposure_ms'] == 800.:
            raise RuntimeError('Exposure restore failed')
        return apply(exp, requested)
    br.apply_settings = fail_restore
    report = probe.run_probe(br, str(tmp_path), lambda r:None)
    assert report['status'] == 'failed' and not report['restore_ok']
    assert 'acquisition' in report['restore_errors'] and display['value'] == -1


def test_archive_descriptor_is_binary_on_windows_for_python27(tmp_path, monkeypatch):
    # Python 2 fdopen('wb') does not reliably reset a CRT text-mode descriptor.
    # Missing O_BINARY can translate 0x0a bytes in binary SPE headers/pixels.
    if not hasattr(os, 'O_BINARY'):
        pytest.skip('Windows CRT descriptor mode test')
    probe = load_probe()
    br, _, _, _, _, _ = display_bridge(tmp_path)
    raw = b'\x00\x0a\x0d\x0a\x1a\xff'
    source = tmp_path/'newline-source.spe'
    target = tmp_path/'binary-archive.spe'
    source.write_bytes(raw)
    br.read_spe_frames = lambda path:(512,1,1,3,Path(path).read_bytes(),{})
    br.settings_from_spe = lambda *args:None
    native_open, flags = os.open, []
    def opened(path, mode, *args):
        flags.append(mode)
        return native_open(path, mode, *args)
    monkeypatch.setattr(probe.os, 'open', opened)
    probe.archive_spe(br, str(source), str(target), {'winspec_datatype':3}, raw, {})
    assert flags[0] & os.O_BINARY
    assert target.read_bytes() == raw
