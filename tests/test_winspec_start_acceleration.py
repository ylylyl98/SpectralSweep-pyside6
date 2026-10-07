import itertools
from pathlib import Path
import pytest


def setup_manager(tmp_path, *, fallback=False, uncertain=False, limit=30):
    try:
        from tools.winspec.start_acceleration.manager import Manager
    except ImportError:
        pytest.fail('Formal acceleration manager is missing')
    events=[];captures=[];now=[0.]
    class Transport:
        def __init__(self,output,baseline=None,frame_limit=30):
            self.output=output;self.baseline=baseline;self.detached=True;self.started_at=now[0];self.count=0
        def start(self):
            Path(self.output).mkdir(parents=True);self.detached=False;events.append('resident' if self.baseline else 'baseline')
        def running(self):return not self.detached
        def stop(self):
            if uncertain and self.baseline:raise RuntimeError('Uncertain detach')
            self.detached=True;events.append('detach')
        def build_baseline(self,begin,end):
            assert begin<end;self.stop();return {'verified':True}
        def open_frame(self,index):assert index==self.count;events.append('open')
        def close_frame(self,index,begin,end):
            assert begin<end;self.count+=1;events.append('close')
            return dict(redirect_applied=not fallback,reason='state differs' if fallback else 'matched')
    class Exp:
        def Start(self,doc):captures.append(doc);return True
    def acquire(exp):
        assert exp.Start('owned document')
        return {'settings':{'accumulations':2}},b'raw'
    manager=Manager(str(tmp_path/'evidence'),factory=Transport,clock=lambda:now[0],counter=lambda c=itertools.count():next(c),frame_limit=limit)
    return manager,Exp(),acquire,events,captures,now


def settings(exposure=500.,accumulations=2):
    return dict(exposure_ms=exposure,accumulations=accumulations,sequential_frames=1,timing_mode=1,
                detector_width=512,detector_height=1,output_width=512,output_height=1,roi_enabled=False,adc_rate=11,controller_gain=2)


def test_baseline_then_resident_then_parameter_change(tmp_path):
    m,e,acq,events,captures,_=setup_manager(tmp_path)
    modes=[m.capture(e,s,'a'*32,acq)[0]['start_acceleration']['mode'] for s in [settings(),settings(),settings(800.,1),settings(800.,1)]]
    assert modes==['baseline','optimized','baseline','optimized']
    assert len(captures)==4 and events.count('baseline')==2 and events.count('resident')==2
    m.release('a'*32);assert m.profile is None


def test_spe_float_precision_and_readout_observation_do_not_rebuild_baseline(tmp_path):
    import struct
    m,e,acq,events,captures,_=setup_manager(tmp_path)
    configured=settings(800,1);configured['readout_time_s']=.5170999999999999
    observed=dict(configured,exposure_ms=struct.unpack('<f',struct.pack('<f',.8))[0]*1000,
                  readout_time_s=.5170999765396118)
    assert m.capture(e,configured,'a'*32,acq)[0]['start_acceleration']['mode']=='baseline'
    assert m.capture(e,observed,'a'*32,acq)[0]['start_acceleration']['mode']=='optimized'


def test_cancelled_evidence_is_not_evicted_as_successful(tmp_path):
    m,e,acq,events,captures,_=setup_manager(tmp_path,limit=1)
    def cancel(exp):exp.Start('owned');m.request_invalidate('stop');raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError):m.capture(e,settings(),'a'*32,cancel)
    failed=list((tmp_path/'evidence').glob('session-*'))
    assert len(failed)==1
    for _ in range(14):m.capture(e,settings(),'a'*32,acq)
    m.release('a'*32)
    assert failed[0].is_dir() and len(m.completed)==4


def test_gate_mismatch_returns_current_full_frame_once_and_rebaselines(tmp_path):
    m,e,acq,events,captures,_=setup_manager(tmp_path,fallback=True)
    results=[m.capture(e,settings(),'a'*32,acq) for _ in range(3)]
    assert [r[0]['start_acceleration']['mode'] for r in results]==['baseline','fallback','baseline']
    assert all(r[1]==b'raw' for r in results) and len(captures)==3


@pytest.mark.parametrize('change',['session','stop','limit','age'])
def test_session_stop_and_rollover_require_new_baseline(tmp_path,change):
    m,e,acq,events,captures,now=setup_manager(tmp_path,limit=1 if change=='limit' else 30)
    m.capture(e,settings(),'a'*32,acq);m.capture(e,settings(),'a'*32,acq)
    if change=='stop':m.request_invalidate('user stop')
    if change=='age':now[0]=61.
    session='b'*32 if change=='session' else 'a'*32
    result=m.capture(e,settings(),session,acq)
    assert result[0]['start_acceleration']['mode']=='baseline' and len(captures)==3


def test_uncertain_detach_blocks_all_future_capture_and_release(tmp_path):
    m,e,acq,events,captures,_=setup_manager(tmp_path,uncertain=True,limit=1)
    m.capture(e,settings(),'a'*32,acq);m.capture(e,settings(),'a'*32,acq)
    with pytest.raises(RuntimeError):m.capture(e,settings(),'a'*32,acq)
    assert m.failed and not m.profile.detached and len(captures)==2
    with pytest.raises(RuntimeError):m.release('a'*32)
    with pytest.raises(RuntimeError):m.capture(e,settings(),'b'*32,acq)
    assert len(captures)==2


def test_unsupported_recipe_does_not_attach(tmp_path):
    m,e,acq,events,captures,_=setup_manager(tmp_path)
    result=m.capture(e,settings(5000.,2),'a'*32,acq)
    assert result[0]['start_acceleration']['mode']=='fallback' and not events and len(captures)==1


def test_ineligible_completed_baseline_returns_full_frame_without_second_start(tmp_path):
    from tools.winspec.start_acceleration.manager import Unavailable
    m,e,acq,events,captures,_=setup_manager(tmp_path)
    factory=m.factory
    def changed(*args,**kwargs):
        transport=factory(*args,**kwargs)
        def baseline(begin,end):
            transport.stop();raise Unavailable('Full programs differ')
        transport.build_baseline=baseline
        return transport
    m.factory=changed
    result=m.capture(e,settings(),'a'*32,acq)
    assert result[0]['start_acceleration']['mode']=='fallback'
    assert result[1]==b'raw' and len(captures)==1 and not m.failed
    for i in range(2):
        result=m.capture(e,settings(),'a'*32,acq)
        assert result[0]['start_acceleration']['mode']=='fallback'
    assert len(captures)==3 and events.count('baseline')==2


def test_parameter_transition_full_frame_can_settle_before_next_baseline(tmp_path):
    from tools.winspec.start_acceleration.manager import Unavailable
    m,e,acq,events,captures,_=setup_manager(tmp_path);factory=m.factory;attempts=[0]
    def changed(*args,**kwargs):
        p=factory(*args,**kwargs);original=p.build_baseline
        def baseline(begin,end):
            attempts[0]+=1
            if attempts[0]==1:
                p.stop();raise Unavailable('Baseline program states differ')
            return original(begin,end)
        p.build_baseline=baseline;return p
    m.factory=changed
    modes=[m.capture(e,settings(),'a'*32,acq)[0]['start_acceleration']['mode'] for _ in range(3)]
    assert modes==['fallback','baseline','optimized'] and len(captures)==3


def test_acquisition_failure_retires_debugger_and_never_retries_start(tmp_path):
    m,e,acq,events,captures,_=setup_manager(tmp_path)
    def fail(exp):exp.Start('owned');raise RuntimeError('Camera failure')
    with pytest.raises(RuntimeError):m.capture(e,settings(),'a'*32,fail)
    assert len(captures)==1 and events[-1]=='detach' and not m.failed
    assert m.capture(e,settings(),'a'*32,acq)[0]['start_acceleration']['mode']=='baseline'


def test_confirmed_user_stop_allows_explicit_new_baseline_without_retry(tmp_path):
    m,e,acq,events,captures,_=setup_manager(tmp_path)
    m.capture(e,settings(),'a'*32,acq)
    def stopped(exp):
        exp.Start('owned');m.request_invalidate('user stop');raise RuntimeError('stopped')
    with pytest.raises(RuntimeError,match='stopped'):m.capture(e,settings(),'a'*32,stopped)
    assert len(captures)==2 and not m.failed
    assert m.capture(e,settings(),'a'*32,acq)[0]['start_acceleration']['mode']=='baseline'


def test_foreign_release_cannot_disable_other_session(tmp_path):
    m,e,acq,events,captures,_=setup_manager(tmp_path)
    m.capture(e,settings(),'a'*32,acq);m.capture(e,settings(),'a'*32,acq)
    with pytest.raises(RuntimeError):m.release('b'*32)
    assert m.profile is not None and not m.profile.detached


def test_completed_evidence_retention_preserves_unowned_directory(tmp_path):
    m,e,acq,events,captures,_=setup_manager(tmp_path,limit=1)
    outside=tmp_path/'evidence'/'manual';outside.mkdir(parents=True);(outside/'keep.spe').write_bytes(b'user data')
    for _ in range(15):m.capture(e,settings(),'a'*32,acq)
    m.release('a'*32)
    assert (outside/'keep.spe').read_bytes()==b'user data'
    assert len(list((tmp_path/'evidence').glob('session-*')))<=4
