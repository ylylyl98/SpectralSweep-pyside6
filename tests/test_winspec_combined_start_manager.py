"""Combined orchestration tests preserve the original Manager policy."""
import itertools
from pathlib import Path
import pytest
from tools.winspec.start_acceleration import manager as acceleration_manager

def module():
    return acceleration_manager

def settings():
    return dict(exposure_ms=500,accumulations=2,sequential_frames=1,timing_mode=1,
                detector_width=512,detector_height=1,output_width=512,output_height=1,roi_enabled=False)

def setup(tmp_path,limit=30):
    events=[];starts=[];created=[]
    class Native:
        def __init__(self,manager):self.manager=manager;self.active=False;self.probe=self;self.capacity=True;self.bad=None;created.append(self)
        def start(self,group):self.active=True;events.append('install')
        def retire(self):
            if self.active:events.append('restore');self.active=False
        def ready(self):return self.capacity
        def arm(self,enabled):
            events.append('arm')
            if self.bad=='arm':raise RuntimeError('native arm failure')
        def disarm(self):
            events.append('disarm')
            if self.bad=='disarm':raise RuntimeError('native disarm failure')
        def cancel_unstarted(self):events.append('cancel_unstarted')
    class Transport:
        def __init__(self,output,baseline=None,frame_limit=30,native=None):
            self.output=output;self.baseline=baseline;self.detached=True;self.native=native
        def start(self):Path(self.output).mkdir();self.detached=False;events.append('resident' if self.baseline else 'baseline')
        def running(self):return not self.detached
        def stop(self):self.detached=True;events.append('detach')
        def build_baseline(self,begin,end):self.stop();return {'verified':True}
        def open_frame(self,index):events.append('open')
        def close_frame(self,index,begin,end):events.append('close');return dict(redirect_applied=True,native_batch={'batches':5})
    class Owner:
        def __init__(self):self.documents=[];self.cancelled=False;self.error=False
        @property
        def acquisition_owner(self):return self
        def retain(self,doc):
            if not self.documents:self.documents.append(dict(doc=doc,attempted=False))
            events.append('retain')
        def Start(self,doc):
            self.retain(doc)
            if self.cancelled:raise RuntimeError('stopped before Start')
            self.documents[0]['attempted']=True;starts.append(doc);events.append('Start')
            if self.error:raise RuntimeError('stopped during Start')
            return True
    def acquire(exp):exp.Start('doc');return {},b'raw'
    manager=module().Manager(str(tmp_path/'evidence'),factory=Transport,native_factory=Native,
                             counter=lambda c=itertools.count():next(c),clock=lambda:0.,frame_limit=limit)
    return manager,Owner,acquire,events,starts,created

def test_baseline_unarmed_and_combined_start_retains_document_before_arm(tmp_path):
    m,Owner,acq,events,starts,created=setup(tmp_path)
    assert m.capture(Owner(),settings(),'a'*32,acq)[0]['start_acceleration']['mode']=='baseline'
    assert 'arm' not in events and events[:2]==['install','baseline']
    boundary=len(events)
    result=m.capture(Owner(),settings(),'a'*32,acq)
    assert result[0]['start_acceleration']['native_batch']=={'batches':5}
    assert events[boundary:]==['resident','open','retain','arm','retain','Start','disarm','close']
    m.release('a'*32)
    assert events[-2:]==['detach','restore'] and not m.native.active and len(created)==1

@pytest.mark.parametrize('cause',['limit','capacity','stop'])
def test_retirement_reuses_session_and_requires_new_full_baseline(tmp_path,cause):
    m,Owner,acq,events,starts,created=setup(tmp_path,limit=1 if cause=='limit' else 30)
    m.capture(Owner(),settings(),'a'*32,acq);m.capture(Owner(),settings(),'a'*32,acq)
    if cause=='capacity':m.native.capacity=False
    if cause=='stop':m.request_invalidate('stop')
    result=m.capture(Owner(),settings(),'a'*32,acq)
    assert result[0]['start_acceleration']['mode']=='baseline' and len(starts)==3
    assert len(created)==1 and events.count('restore')==1 and events.count('install')==2
    m.release('a'*32)

@pytest.mark.parametrize('when',['before','during'])
def test_stop_uses_confirmed_unstarted_cancel_or_completed_disarm(tmp_path,when):
    m,Owner,acq,events,starts,created=setup(tmp_path);m.capture(Owner(),settings(),'a'*32,acq)
    owner=Owner();owner.cancelled=when=='before';owner.error=when=='during'
    with pytest.raises(RuntimeError,match='stopped'):m.capture(owner,settings(),'a'*32,acq)
    assert ('cancel_unstarted' in events)==(when=='before')
    assert ('disarm' in events)==(when=='during')
    assert len(starts)==(1 if when=='before' else 2) and not m.failed
    assert m.capture(Owner(),settings(),'a'*32,acq)[0]['start_acceleration']['mode']=='baseline'
    m.release('a'*32)

@pytest.mark.parametrize('when',['arm','disarm'])
def test_native_failure_latches_before_retry_and_retains_owner(tmp_path,when):
    m,Owner,acq,events,starts,created=setup(tmp_path);m.capture(Owner(),settings(),'a'*32,acq)
    m.native.bad=when;owner=Owner()
    with pytest.raises(RuntimeError,match='native'):m.capture(owner,settings(),'a'*32,acq)
    assert m.failed and owner.documents and len(starts)==(1 if when=='arm' else 2)
    before=len(starts)
    with pytest.raises(RuntimeError,match='recovery'):m.capture(Owner(),settings(),'a'*32,acq)
    assert len(starts)==before

def test_injected_transport_keeps_native_disabled_unless_explicit(tmp_path):
    class Transport:
        def __init__(self,output,baseline=None,frame_limit=30):self.detached=True
        def start(self):pass
        def build_baseline(self,*args):return {'verified':True}
    m=module().Manager(str(tmp_path),factory=Transport,counter=lambda c=itertools.count():next(c))
    class Exp:
        def Start(self,doc):return True
    def acquire(exp):exp.Start('doc');return {},b'raw'
    assert m.capture(Exp(),settings(),'a'*32,acquire)[0]['start_acceleration']['mode']=='baseline'
    assert m.native is None

def test_monitor_failure_before_baseline_start_blocks_camera_call(tmp_path):
    m,Owner,acq,events,starts,created=setup(tmp_path)
    owner=Owner()
    def fault_then_start(exp):
        m.fail('native read failed')
        return acq(exp)
    with pytest.raises(RuntimeError,match='recovery'):m.capture(owner,settings(),'a'*32,fault_then_start)
    assert not starts and owner.documents and not owner.documents[0]['attempted']

def test_monitor_failure_during_arm_blocks_camera_call_without_cleanup_retry(tmp_path):
    m,Owner,acq,events,starts,created=setup(tmp_path)
    m.capture(Owner(),settings(),'a'*32,acq)
    def arm(enabled):m.fail('native monitor detected hold')
    m.native.arm=arm
    with pytest.raises(RuntimeError,match='recovery'):m.capture(Owner(),settings(),'a'*32,acq)
    assert len(starts)==1 and 'disarm' not in events and 'cancel_unstarted' not in events
