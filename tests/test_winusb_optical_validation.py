"""Reject timing, optical-state and archive errors in the diagnostic itself."""
import importlib
import importlib.util
import json
from pathlib import Path
import struct

import pytest


def diagnostic():
    name = 'tools.winspec.validate_local_optical'
    assert importlib.util.find_spec(name), 'Optical diagnostic missing'
    return importlib.import_module(name)


def rows(sequence=1, start=100, publication=207):
    return [dict(stage='request_sequence', value=sequence, error=0, ms=start-1),
            dict(stage='start_call_before', value=0, error=0, ms=start),
            dict(stage='start_call_after', value=1, error=0, ms=start+3),
            dict(stage='publication_observed', value=1024, error=0, ms=publication)]


def test_timing_selects_current_request_and_flags_early_complete():
    module = diagnostic()
    log = rows()+rows(2, 300, 345)
    assert module.frame_timing(log, 1)['start_to_publication_ms'] == 107
    with pytest.raises(RuntimeError, match='early'):
        module.frame_timing(log, 2)


@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'reordered'])
def test_timing_never_borrows_missing_or_ambiguous_stage(damage):
    log = rows()
    if damage == 'missing': log.pop()
    if damage == 'duplicate': log.append(dict(log[-1]))
    if damage == 'reordered': log[-1]['ms'] = 99
    with pytest.raises(RuntimeError): diagnostic().frame_timing(log, 1)


def frame(mean, *, label='dark', maximum=1000):
    return dict(label=label, mean_counts=mean, maximum=maximum)


def test_optical_gate_requires_signal_not_just_unique_noisy_frames():
    module = diagnostic()
    dark = [frame(100+i%2) for i in range(20)]
    weak = [frame(102+i%2, label='light') for i in range(20)]
    assert not module.optical_reference(dark, weak)['passed']
    light = [frame(200+i%2, label='light') for i in range(20)]
    reference = module.optical_reference(dark, light)
    assert reference['passed'] and reference['threshold_counts'] == 150.5
    assert module.classify_frame(frame(199, label='light'), reference)['matches']
    assert not module.classify_frame(frame(101, label='light'), reference)['matches']
    assert not module.optical_reference(dark, [frame(200, maximum=65535)]*20)['passed']


class Camera:
    def __init__(self, directory, *, early=False):
        self.directory=directory; directory.mkdir(); self.calls=0; self.closed=False; self.early=early
    def capture(self, cancelled, *, context):
        self.calls+=1
        data=struct.pack('<512H', *([100+self.calls]*512))
        with (self.directory/'session.jsonl').open('a') as stream:
            for row in rows(self.calls, 1000*self.calls, 1000*self.calls+(40 if self.early else 107)):
                stream.write(json.dumps(row)+'\n')
        return dict(request_sequence=self.calls, session_evidence_directory=str(self.directory),
                    request_id=str(self.calls), bridge_session_id='test', process_pid=123,
                    payload_sha256=__import__('hashlib').sha256(data).hexdigest(),
                    frame_cleanup_verified=True, session_cleanup_verified=False), data
    def close(self): self.closed=True


def test_batch_preserves_first_frame_and_requires_confirmation(tmp_path):
    module=diagnostic(); camera=Camera(tmp_path/'native')
    run=module.OpticalRun(camera,tmp_path/'result',max_frames=3)
    with pytest.raises(ValueError): run.capture_batch('dark',2,'')
    assert camera.calls == 0
    run.capture_batch('dark',2,'User confirmed blocked beam')
    data=json.loads((tmp_path/'result/frames.json').read_text())
    assert len(data)==2 and data[0]['first_in_batch'] and data[0]['native']['request_sequence']==1
    assert (tmp_path/'result/frame-000001.bin').read_bytes()==struct.pack('<512H',*([101]*512))
    with pytest.raises(ValueError): run.capture_batch('light',2,'User confirmed light')
    assert camera.calls == 2


def test_early_frame_is_archived_then_latches_failure(tmp_path):
    module=diagnostic(); camera=Camera(tmp_path/'native',early=True)
    run=module.OpticalRun(camera,tmp_path/'result')
    with pytest.raises(RuntimeError,match='early'):
        run.capture_batch('dark',20,'User confirmed blocked beam')
    assert camera.calls==1 and (tmp_path/'result/frame-000001.bin').exists()
    with pytest.raises(RuntimeError):run.capture_batch('dark',1,'Still blocked')
    assert camera.calls==1


def test_wrong_optical_state_stops_at_first_frame(tmp_path):
    module=diagnostic(); camera=Camera(tmp_path/'native')
    run=module.OpticalRun(camera,tmp_path/'result')
    run.reference=module.optical_reference([frame(100)]*20,[frame(200,label='light')]*20)
    with pytest.raises(RuntimeError,match='optical'):
        run.capture_batch('light',5,'User confirmed light')
    assert camera.calls==1 and (tmp_path/'result/frame-000001.bin').exists()


@pytest.mark.skipif(not (Path(__file__).parents[1]/'tmp/winusb_optical_live.py').exists(),
                    reason='Private lab runner is outside the portable package')
def test_runner_checks_protected_state_even_when_close_fails(tmp_path, monkeypatch):
    import sys
    spec=importlib.util.spec_from_file_location('optical_live_test',Path(__file__).parents[1]/'tmp/winusb_optical_live.py')
    runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
    output=tmp_path/'live'; snapshots=[]
    class RetainedProcess:
        pid=999
        def poll(self):return None
    class Bridge:
        _output=None
        _process=RetainedProcess()
        def preflight(self):pass
        def close(self):raise RuntimeError('Cleanup proof unavailable')
    def snapshot(*args,**kwargs):snapshots.append(kwargs);return {'protected':'same'}
    original=diagnostic().OpticalRun
    def prepare(bridge,directory,**kwargs):
        result=original(bridge,directory,**kwargs)
        (output/'command-001.json').write_text('{"action":"close"}')
        return result
    monkeypatch.setattr(runner,'PersistentCaptureBridge',Bridge)
    monkeypatch.setattr(runner,'snapshot',snapshot)
    monkeypatch.setattr(diagnostic(),'OpticalRun',prepare)
    monkeypatch.setattr(sys,'argv',['diagnostic',str(output)])
    with pytest.raises(RuntimeError,match='Cleanup proof'):runner.main()
    assert len(snapshots)==2, 'Close failure skipped protected-state inspection'
    assert snapshots[-1]=={'retained_pid':999}
    result=json.loads((output/'result.json').read_text())
    assert result['status']=='failed' and result['protected_unchanged']
    assert result['session_close_verified'] is False
