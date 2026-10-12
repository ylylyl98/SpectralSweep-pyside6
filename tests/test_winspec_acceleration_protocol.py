import copy,json
from pathlib import Path
import pytest
from tools.winspec.start_acceleration.resident_protocol import analyze_light


def fixture():
    path=Path(__file__).parent / 'fixtures' / 'winspec-resident-fixture.json'
    f=json.loads(path.read_text());return f


def full_fallback(f):
    t=copy.deepcopy(f['trial']);t['redirects_applied']=[];t['control_flow_modified']=False
    for p in t['programs']:
        p['body']['decision']={'redirect':False,'reason':'Baseline configuration/state differs'}
        for r in (p,p['body'],p['return_row']):
            region=r['full_state']['regions']['settings'];region['hex']='ff'+region['hex'][2:]
    t['records']=t['programs']
    return t


def test_pure_state_mismatch_is_verified_as_full_execution_fallback():
    f=fixture();t=full_fallback(f)
    a=analyze_light(t,f['start_begin'],f['start_end'],f['baseline']['modules'],f['baseline'],allow_fallback=True)
    assert a['redirect_applied'] is False and a['program_returns']==[1,1]


@pytest.mark.parametrize('bad',['read','missing_step','tail','false_redirect','second_redirect'])
def test_allowing_fallback_does_not_hide_hard_trace_errors(bad):
    f=fixture();t=full_fallback(f)
    if bad=='read':t['experiment_errors']=['read failed']
    if bad=='missing_step':t['buffered_log'].pop()
    if bad=='tail':t['programs'][0]['return_row']['eax']=0
    if bad=='false_redirect':t['programs'][0]['body']['decision']['redirect']=True
    if bad=='second_redirect':t['programs'][1]['body']['decision']['redirect']=True
    with pytest.raises(ValueError):analyze_light(t,f['start_begin'],f['start_end'],f['baseline']['modules'],f['baseline'],allow_fallback=True)


def test_deadline_waits_for_frame_boundary_and_does_not_retire_mid_step():
    from tools.winspec.start_acceleration.resident_trace import ResidentTrace
    from types import SimpleNamespace
    t=ResidentTrace.__new__(ResidentTrace);t.boundary=SimpleNamespace(current={'frame_id':0});t.active={};t.stepping={}
    assert t.deadline_ready() is False
    t.boundary.current=None;t.stepping={7:{}}
    assert t.deadline_ready() is False
    t.stepping={};assert t.deadline_ready() is True


def test_command_sharing_error_waits_without_faulting_or_processing_twice(tmp_path,monkeypatch):
    import builtins,errno
    from types import SimpleNamespace
    from tools.winspec.start_acceleration import resident_trace as module
    from tools.winspec.start_acceleration import resident_protocol as protocol
    path=tmp_path/'command-000.json';path.write_text('{"op":"open","frame_id":0}')
    opened=[];disabled=[];real_open=builtins.open;attempts=[0]
    def locked_once(name,*args,**kwargs):
        if str(name)==str(path):
            attempts[0]+=1
            if attempts[0]==1:raise IOError(errno.EACCES,'Permission denied',str(path))
        return real_open(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'open',locked_once)
    monkeypatch.setattr(module,'qpc',lambda:100)
    t=module.ResidentTrace.__new__(module.ResidentTrace)
    t.boundary=SimpleNamespace(failed=None,current=None,open=lambda *a:opened.append(a) or 'gate',disable=disabled.append)
    t.pending=None;t.stepping={};t.active={};t.output=str(tmp_path);t.command_seq=0
    t.config={'targets':[]};t.request_stop=False;t.records=[];t.buffered_log=[]
    t.report={'pauses':[],'breakpoint_hits':0,'target_hits':{}}
    t.command_reader=protocol.PublishedReader(clock=lambda:0.)
    t.service()
    assert not disabled and not t.request_stop and not opened and t.command_seq==0
    t.service()
    assert not disabled and len(opened)==1 and t.command_seq==1
    assert json.loads((tmp_path/'ack-000.json').read_text())['status']=='opened'


def test_persistent_file_denial_and_corrupt_json_still_fail(tmp_path,monkeypatch):
    import builtins,errno
    from tools.winspec.start_acceleration.resident_protocol import PublishedReader,READ_PENDING
    clock=[0.];reader=PublishedReader(clock=lambda:clock[0])
    real_open=builtins.open
    def deny(*args,**kwargs):raise IOError(errno.EACCES,'Permission denied')
    monkeypatch.setattr(builtins,'open',deny)
    assert reader.read(str(tmp_path/'command.json')) is READ_PENDING
    clock[0]=1.01
    with pytest.raises(IOError):reader.read(str(tmp_path/'command.json'))
    monkeypatch.setattr(builtins,'open',real_open)
    path=tmp_path/'corrupt.json';path.write_text('{')
    with pytest.raises(ValueError):reader.read(str(path))


def test_ack_sharing_error_does_not_publish_command_twice(tmp_path,monkeypatch):
    import builtins,errno
    from tools.winspec.start_acceleration import transport as module
    from tools.winspec.start_acceleration.resident_protocol import PublishedReader
    ack=tmp_path/'ack-000.json';ack.write_text('{"status":"opened","frame_id":0}')
    real_open=builtins.open;real_publish=module.publish;attempts=[0];published=[]
    def locked_once(name,*args,**kwargs):
        if str(name)==str(ack):
            attempts[0]+=1
            if attempts[0]==1:raise IOError(errno.EACCES,'Permission denied')
        return real_open(name,*args,**kwargs)
    def record_publish(path,value):published.append(path);real_publish(path,value)
    monkeypatch.setattr(builtins,'open',locked_once)
    monkeypatch.setattr(module,'publish',record_publish)
    t=module.Transport.__new__(module.Transport);t.output=str(tmp_path);t.command_seq=0
    t.file_reader=PublishedReader();t.running=lambda:True
    assert t.open_frame(0)=={'status':'opened','frame_id':0}
    assert len(published)==1 and attempts[0]==2 and t.command_seq==1


def test_completed_evidence_retry_survives_idle_debugger_exit(tmp_path,monkeypatch):
    import builtins,errno
    from tools.winspec.start_acceleration import transport as module
    from tools.winspec.start_acceleration.resident_protocol import PublishedReader
    path=tmp_path/'frame-000.json';path.write_text('{"frame_id":0}')
    real_open=builtins.open;attempts=[0]
    def locked_once(*args,**kwargs):
        attempts[0]+=1
        if attempts[0]==1:raise IOError(errno.EACCES,'Permission denied')
        return real_open(*args,**kwargs)
    monkeypatch.setattr(builtins,'open',locked_once)
    t=module.Transport.__new__(module.Transport);t.file_reader=PublishedReader();t.running=lambda:False
    assert t.read_boundary_file(str(path),module.clock()+5,require_running=False)=={'frame_id':0}
    assert attempts[0]==2
