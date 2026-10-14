"""Real pipe/process transport; only the external native helper is substituted."""
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

import pytest

from tests.winusb_synthetic import write_capture, EEPROM_PINS
HELPER = r'''
import json,sys,shutil
from pathlib import Path
root,session,fixture,damage=Path(sys.argv[1]),sys.argv[2],Path(sys.argv[3]),sys.argv[4]
root.mkdir()
for name in ('eeprom.bin','detector-eeprom.bin'): shutil.copyfile(fixture/name,root/name)
(root/'ready.json').write_text(json.dumps(dict(protocol=1,session_id=session,initialized=True,exposure_ms=100)))
completed=0
for line in sys.stdin:
 if line.strip()=='Q': break
 _,sequence,request=line.split();sequence=int(sequence)
 frame=root/f'frame-{sequence:06d}';frame.mkdir()
 for name in ('frame-1024.bin','data82-transport.bin'):shutil.copyfile(fixture/name,frame/name)
 value=dict(protocol=1,session_id=session,request_id=request,sequence=sequence,exposure_ms=100,
 native_frame_count=1,bytes=1024,data_reads=1,copies=1,matched_results=1,drained=True,
 request_worker_joined=True,delivery_worker_joined=True,stop_reply_valid=True,
 stop_write_completed=True,transport_equal=True,error=0,session_elapsed_ms=10*sequence)
 if damage=='id':value['request_id']='previous'
 if damage=='sequence':value['sequence']=sequence+1
 if damage=='short':(frame/'frame-1024.bin').write_bytes(b'short')
 if damage=='worker':value['request_worker_joined']=False
 (frame/'frame.json').write_text(json.dumps(value));completed+=1
if damage!='close':
 (root/'closed.json').write_text(json.dumps(dict(protocol=1,session_id=session,cleanup_verified=True,completed_frames=completed,error=0)))
'''


def module():
    assert importlib.util.find_spec('app.devices.winusb_persistent'), 'Persistent app bridge missing'
    return importlib.import_module('app.devices.winusb_persistent')


def test_manifest_selects_new_runtime_without_replacing_previous_release(tmp_path, monkeypatch):
    target=module();release=target.manifest();release['runtime_directory']='runtime-v3'
    monkeypatch.setattr(target,'manifest',lambda:release)
    old=tmp_path/'runtime-v2';old.mkdir();(old/'preserved').write_text('p007')
    owner=target.PersistentCaptureBridge(state_dir=tmp_path)
    assert owner.runtime_dir==tmp_path/'runtime-v3'
    assert (old/'preserved').read_text()=='p007'


def bridge(tmp_path, monkeypatch, damage='none'):
    target = module()
    fixture = write_capture(tmp_path / 'synthetic')
    release = target.manifest(); release.update(log_protocol=0, recipe_protocol=0, eeprom_files=EEPROM_PINS)
    monkeypatch.setattr(target, 'manifest', lambda: release)
    script = tmp_path/'helper.py'; script.write_text(HELPER)
    processes = []; real_popen = subprocess.Popen
    def launch(command, **kwargs):
        if Path(command[0]).name != 'persistent_ingaas.exe':
            return real_popen(command, **kwargs)
        proc = real_popen([sys.executable, str(script), command[3], command[4], str(fixture.resolve()), damage], **kwargs)
        processes.append(proc); return proc
    monkeypatch.setattr(target.subprocess, 'Popen', launch)
    monkeypatch.setattr(target, 'verify_runtime', lambda path, **kwargs: Path(path).resolve())
    monkeypatch.setattr(target, 'inspect_device', lambda: {'service': 'WINUSB', 'present': True})
    return target.PersistentCaptureBridge(runtime_dir=tmp_path, state_dir=tmp_path/'state'), processes


@pytest.mark.parametrize('ready_fields', [{}, {'log_protocol': True, 'rotation_bytes': 67108864},
                                        {'log_protocol': 1, 'rotation_bytes': 4096}])
def test_log_handshake_mismatch_closes_before_sending_any_capture(tmp_path, monkeypatch, ready_fields):
    owner, processes = bridge(tmp_path, monkeypatch)
    owner._log_protocol = 1
    original = owner._wait_receipt
    def read(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path.name == 'ready.json': value.update(ready_fields)
        return value
    monkeypatch.setattr(owner, '_wait_receipt', read)
    with pytest.raises(RuntimeError, match='log rotation handshake'):
        owner.capture(threading.Event())
    assert owner._sequence == 0 and len(processes) == 1 and processes[0].poll() == 0
    assert not owner._claim.exists()


def test_invalid_native_rotation_threshold_starts_no_helper(tmp_path, monkeypatch):
    from app.devices.winusb_retention import CachePolicy
    owner, processes = bridge(tmp_path, monkeypatch)
    owner._log_protocol = 1
    owner._cache.policy = CachePolicy(max_session_log_bytes=4095)
    with pytest.raises(RuntimeError, match='rotation threshold'):
        owner.capture(threading.Event())
    assert not processes and not owner._claim.exists()


def test_two_frames_share_process_but_keep_distinct_requests_and_close_receipt(tmp_path, monkeypatch):
    owner, processes = bridge(tmp_path, monkeypatch)
    frames=[]
    try:
        for i in range(2):
            request = str(uuid.uuid4())
            meta, payload = owner.capture(threading.Event(), request_id=request, context={'requested_center_nm':1300})
            assert meta['request_id']==request and meta['request_sequence']==i+1
            assert len(payload)==1024 and meta['frame_cleanup_verified'] is True
            assert meta['session_cleanup_verified'] is False
            assert meta['hardware_frame_freshness_verified'] is False
            assert not owner.is_busy and (owner.state_dir/'active.json').exists()
            frames.append(meta)
        assert len(processes)==1 and processes[0].poll() is None
        assert frames[0]['evidence_directory']!=frames[1]['evidence_directory']
    finally: owner.close()
    assert processes[0].poll()==0 and not (owner.state_dir/'active.json').exists()


@pytest.mark.parametrize('damage',['id','sequence','short','worker'])
def test_invalid_response_is_never_published_or_retried(tmp_path, monkeypatch, damage):
    owner, processes=bridge(tmp_path, monkeypatch,damage)
    with pytest.raises(RuntimeError): owner.capture(threading.Event())
    with pytest.raises(RuntimeError): owner.capture(threading.Event())
    owner.close()
    assert len(processes)==1


def test_missing_close_proof_retains_shared_owner_receipt(tmp_path, monkeypatch):
    owner, processes=bridge(tmp_path,monkeypatch,'close')
    owner.capture(threading.Event())
    with pytest.raises(RuntimeError,match='ownership|cleanup'): owner.close()
    assert processes[0].poll()==0 and (owner.state_dir/'active.json').exists()


def test_cancel_before_start_opens_no_camera_process(tmp_path, monkeypatch):
    owner, processes=bridge(tmp_path,monkeypatch)
    cancel=threading.Event();cancel.set()
    with pytest.raises(RuntimeError,match='cancel'):owner.capture(cancel)
    owner.close();assert not processes


def test_cache_limit_sends_no_additional_request_and_keeps_session_usable(tmp_path, monkeypatch):
    from app.devices.winusb_retention import CachePolicy, CacheCapacityError
    owner, processes = bridge(tmp_path, monkeypatch)
    assert hasattr(owner, '_cache'), 'Capture bridge does not enforce cache budget'
    owner._cache.policy = CachePolicy(keep_frames=1, max_frames=2, min_free_bytes=0)
    try:
        owner.capture(threading.Event()); owner.capture(threading.Event())
        with pytest.raises(CacheCapacityError): owner.capture(threading.Event())
        assert owner._sequence == 2 and owner.is_ready
        assert len(processes) == 1 and processes[0].poll() is None
    finally:
        owner.close()
    assert not (owner.state_dir/'active.json').exists()


def test_unreadable_cache_index_sends_no_new_request_and_normal_close_still_works(tmp_path, monkeypatch):
    from app.devices.winusb_retention import CacheCapacityError
    owner, processes = bridge(tmp_path, monkeypatch)
    try:
        meta, data = owner.capture(threading.Event())
        (owner.state_dir/'retention.sqlite3').write_bytes(b'broken index')
        with pytest.raises(CacheCapacityError): owner.capture(threading.Event())
        assert owner._sequence == 1 and owner.is_ready and processes[0].poll() is None
        assert Path(meta['evidence_directory']).joinpath('frame-1024.bin').read_bytes() == data
    finally:
        owner.close()
    assert processes[0].poll() == 0 and not (owner.state_dir/'active.json').exists()


def test_cancel_during_request_discards_frame_and_closes_session(tmp_path, monkeypatch):
    owner, processes=bridge(tmp_path,monkeypatch)
    cancel=threading.Event()
    def cancel_on_send():
        deadline=time.monotonic()+5
        while owner._sequence<1 and time.monotonic()<deadline:time.sleep(.001)
        cancel.set()
    worker=threading.Thread(target=cancel_on_send);worker.start()
    try:
        with pytest.raises(RuntimeError,match='cancel'):owner.capture(cancel)
    finally:worker.join();owner.close()
    assert len(processes)==1 and processes[0].poll()==0
    assert not (owner.state_dir/'active.json').exists()


def test_timeout_retains_owner_and_never_launches_again_or_kills(tmp_path, monkeypatch):
    target=module();processes=[]
    class Process:
        pid=98765
        returncode=None
        def __init__(self,*args,**kwargs):self.stdin=io.BytesIO();processes.append(self)
        def poll(self):return None
        def wait(self,timeout=None):raise subprocess.TimeoutExpired('retained test owner',timeout)
        def kill(self):pytest.fail('must never kill native owner')
        def terminate(self):pytest.fail('must never terminate native owner')
    monkeypatch.setattr(target.subprocess,'Popen',Process)
    monkeypatch.setattr(target,'verify_runtime',lambda path,**kwargs:Path(path).resolve())
    monkeypatch.setattr(target,'inspect_device',lambda:{'present':True})
    owner=target.PersistentCaptureBridge(runtime_dir=tmp_path,state_dir=tmp_path/'state')
    def timeout(*args,**kwargs):raise TimeoutError('native timeout')
    monkeypatch.setattr(owner,'_wait_receipt',timeout)
    with pytest.raises(RuntimeError,match='ownership'):owner.capture(threading.Event())
    assert owner.is_busy and (owner.state_dir/'active.json').exists()
    with pytest.raises(RuntimeError):owner.capture(threading.Event())
    assert len(processes)==1


@pytest.mark.skipif(not os.environ.get('SPECTRALSWEEP_WINUSB_OFFLINE_RUNTIME'), reason='Built native fake transport required')
def test_actual_native_helper_reuses_one_initialized_session(tmp_path, monkeypatch):
    target=module()
    runtime=Path(os.environ['SPECTRALSWEEP_WINUSB_OFFLINE_RUNTIME']).resolve()
    assert runtime.name=='offline-runtime', 'Never run this test against physical transport'
    actual_popen=subprocess.Popen
    def launch(command,**kwargs):
        if Path(command[0]).name != 'persistent_ingaas.exe':
            return actual_popen(command, **kwargs)
        mode = '--offline-recipe-session' if command[1] == '--recipe-session' else '--offline-session'
        command=[command[0],mode,*command[2:],'13']
        return actual_popen(command,**kwargs)
    monkeypatch.setattr(target.subprocess,'Popen',launch)
    monkeypatch.setattr(target,'inspect_device',lambda:{'present':True,'service':'WINUSB'})
    pins={str(p.relative_to(runtime)).replace('\\','/'):__import__('hashlib').sha256(p.read_bytes()).hexdigest()
          for p in runtime.rglob('*') if p.is_file()}
    release=target.manifest();release['runtime_files']=pins
    monkeypatch.setattr(target,'manifest',lambda:release)
    owner=target.PersistentCaptureBridge(runtime_dir=runtime,state_dir=tmp_path/'state')
    frames=[]
    try:
        for i in range(3):
            meta,data=owner.capture(threading.Event());frames.append(meta)
            assert data==bytes((7*j+32+i)&255 for j in range(1024))
        assert len({m['process_pid'] for m in frames})==1
        assert [m['cold_start'] for m in frames]==[True,False,False]
    finally:owner.close()
    rows=[json.loads(line) for line in (Path(frames[0]['session_evidence_directory'])/'session.jsonl').read_text().splitlines()]
    assert sum(r['stage']=='configure' for r in rows)==1
    assert not (owner.state_dir/'active.json').exists()


@pytest.mark.skipif(not os.environ.get('SPECTRALSWEEP_WINUSB_OFFLINE_RUNTIME'), reason='Built native fake transport required')
def test_actual_native_helper_keeps_acquiring_while_saved_cache_is_bounded(tmp_path, monkeypatch):
    import hashlib
    import struct
    from app.devices.winusb_retention import CachePolicy
    from app.experiment_integration import persist_output_snapshot
    target = module()
    runtime = Path(os.environ['SPECTRALSWEEP_WINUSB_OFFLINE_RUNTIME']).resolve()
    assert runtime.name == 'offline-runtime'
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path/'history.sqlite'))
    actual_popen = subprocess.Popen
    def launch(command, **kwargs):
        if Path(command[0]).name != 'persistent_ingaas.exe':
            return actual_popen(command, **kwargs)
        mode = '--offline-recipe-session' if command[1] == '--recipe-session' else '--offline-session'
        return actual_popen([command[0], mode, *command[2:], '13'], **kwargs)
    monkeypatch.setattr(target.subprocess, 'Popen', launch)
    monkeypatch.setattr(target, 'inspect_device', lambda: {'present': True, 'service': 'WINUSB'})
    release = target.manifest()
    release['runtime_files'] = {str(p.relative_to(runtime)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in runtime.rglob('*') if p.is_file()}
    monkeypatch.setattr(target, 'manifest', lambda: release)
    owner = target.PersistentCaptureBridge(runtime_dir=runtime, state_dir=tmp_path/'state')
    owner._cache.policy = CachePolicy(keep_frames=5, max_frames=20, batch_size=4, min_free_bytes=0)
    exports = tmp_path/'exports'; exports.mkdir()
    records = []
    try:
        for i in range(60):
            meta, payload = owner.capture(threading.Event())
            assert payload == bytes((7*j+32+i)&255 for j in range(1024))
            snapshot = dict(native_capture=meta, raw_accumulated_counts=list(struct.unpack('<512H', payload)))
            output = exports/f'frame-{i+1}.csv'
            output.write_text('pixel,intensity_counts\n'+''.join(f'{j+1},{x}\n' for j,x in enumerate(snapshot['raw_accumulated_counts'])))
            sidecar = persist_output_snapshot(output, experiment_type='spectrum_preview', device_id='test',
                                             settings={'acquisition_time_snapshot': snapshot})
            assert owner._cache.acknowledge_saved(snapshot, output, sidecar)
            assert len(list((owner._output/'native').glob('frame-*'))) <= 6
            records.append(meta)
    finally:
        owner.close()
    assert len({r['process_pid'] for r in records}) == 1
    assert [r['request_sequence'] for r in records] == list(range(1,61))
    assert len(list((owner._output/'native').glob('frame-*'))) == 5
    rows = [json.loads(line) for line in (owner._output/'native/session.jsonl').read_text().splitlines()]
    assert sum(r['stage'] == 'configure' for r in rows) == 1
    assert json.loads((owner._output/'native/closed.json').read_text())['completed_frames'] == 60
    assert not (owner.state_dir/'active.json').exists()
