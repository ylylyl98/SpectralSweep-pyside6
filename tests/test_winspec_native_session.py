"""Lifecycle contracts use a process boundary fake; persistence/thread logic is real."""
import json
import threading

import pytest
from tools.winspec.start_acceleration import native_session


def implementation():
    return native_session

class Manager:
    failed=False
    def __init__(self):self.failures=[];self.failure=threading.Event()
    def fail(self,reason):self.failed=True;self.failures.append(str(reason));self.failure.set()

class Probe:
    def __init__(self,pid,dll,audit):
        self.pid=pid;self.calls=[];self.uncertain=False;self.mutation_attempted=False
        self.installed=False;self.alive=True;self.installs=0;self.resets=0;self.restores=0
        self.status=dict(records=2,rows=4096,row_size=140,active=0,fatal=0,uncertain=0)
        self.gate=dict(generation=7,frame=3,armed=0,phase=0,permit=0,hold=0,fatal=0,
                       active=0,uncertain=0,buffered=0)
        self.rows=[dict(committed=1,id=0),dict(committed=1,id=1)]
    def install(self):self.mutation_attempted=True;self.installed=True;self.installs+=1
    def process_alive(self):return self.alive
    def close_exited(self):assert not self.alive;self.closed=True
    def gate_state(self):return dict(self.gate)
    def snapshot(self):
        self.calls.append(dict(operation=0))
        return dict(status=dict(self.status),gate=dict(self.gate),rows=list(self.rows),calls=list(self.calls))
    def restore(self):self.restores+=1;self.installed=False;self.calls.append(dict(operation=4))
    def reset_archived(self,status,gate):
        assert status==self.status and gate==self.gate
        self.resets+=1;self.rows=[];self.status['records']=0;self.calls.append(dict(operation=6))
        return dict(reset=True,archived_records=status['records'],generation=gate['generation'])

def setup(tmp_path,probe_class=Probe):
    manager=Manager();made=[]
    def factory(*args):
        probe=probe_class(*args);made.append(probe);return probe
    session=implementation().NativeSession(manager,probe_factory=factory,identity=lambda:(123,{'module':'same'}))
    group=tmp_path/'session-1';group.mkdir()
    return manager,session,made,group

def test_retire_archives_complete_rows_before_reset_and_reuses_helper(tmp_path):
    manager,session,made,group=setup(tmp_path)
    session.start(str(group));probe=session.probe
    session.retire()
    archives=list(group.glob('native-*.json'))
    assert archives and any(json.loads(p.read_text()).get('rows')==[{'committed':1,'id':0},{'committed':1,'id':1}] for p in archives)
    assert list(group.glob('*reset-receipt*.json')) and list(group.glob('*.sha256'))
    assert not session.active and not manager.failed and not probe.calls
    next_group=tmp_path/'session-2';next_group.mkdir();session.start(str(next_group))
    assert session.probe is probe and len(made)==1 and probe.installs==2
    session.retire()

@pytest.mark.parametrize('field',['fatal','hold','uncertain'])
def test_read_only_monitor_latches_fault_without_remote_calls(tmp_path,field):
    manager,session,made,group=setup(tmp_path);session.start(str(group));probe=session.probe
    probe.gate[field]=1
    assert manager.failure.wait(1), 'independent monitor failed to latch'
    assert not probe.calls and probe.installed
    with pytest.raises(RuntimeError):session.retire()
    assert session.probe is probe and session.active and not probe.resets

def test_monitor_read_failure_is_not_hidden_by_retirement(tmp_path):
    manager,session,made,group=setup(tmp_path);session.start(str(group));probe=session.probe
    def unreadable():raise RuntimeError('process read failed')
    probe.gate_state=unreadable
    assert manager.failure.wait(1)
    with pytest.raises(RuntimeError):session.retire()
    assert probe.installed and session.probe is probe

def test_fault_latches_before_exception_formatting_can_fail(tmp_path):
    manager,session,made,group=setup(tmp_path);session.start(str(group));probe=session.probe
    class UnprintableError(Exception):
        def __str__(self):raise RuntimeError('formatting failed')
    def unreadable():raise UnprintableError()
    probe.gate_state=unreadable
    assert manager.failure.wait(.5), 'monitor exited without setting the permanent latch'
    assert manager.failed and session.error
    with pytest.raises(RuntimeError):session.retire()
    assert session.probe is probe and probe.installed

def test_monitor_does_not_mistake_pending_healthy_control_for_unknown_completion(tmp_path):
    manager,session,made,group=setup(tmp_path);session.start(str(group));probe=session.probe
    probe.uncertain=True  # invoke() owns a live remote call until it confirms return.
    assert not manager.failure.wait(.12)
    probe.uncertain=False;session.retire()

@pytest.mark.parametrize('stage',['archive','reset','receipt'])
def test_failed_evidence_or_reset_retains_resources_and_call_log(tmp_path,monkeypatch,stage):
    module=implementation();manager,session,made,group=setup(tmp_path)
    session.start(str(group));probe=session.probe;original=module.save
    def failing_save(path,data):
        if stage=='archive' or stage=='receipt' and 'reset-receipt' in str(path):raise IOError('disk failure')
        return original(path,data)
    if stage in ('archive','receipt'):monkeypatch.setattr(module,'save',failing_save)
    else:
        def reset(status,gate):raise RuntimeError('reset rejected')
        probe.reset_archived=reset
    with pytest.raises((IOError,RuntimeError)):session.retire()
    assert manager.failed and session.probe is probe and session.active and probe.calls
    if stage=='archive':assert probe.installed and probe.restores==0 and probe.resets==0

def test_capacity_rotation_required_before_1500_record_reserve_is_consumed(tmp_path):
    manager,session,made,group=setup(tmp_path);session.start(str(group))
    session.probe.status['records']=14884;assert session.ready()
    session.probe.status['records']=14885;assert not session.ready()
    session.probe.status['records']=2;session.retire()

def test_retire_waits_for_known_busy_callbacks_before_any_remote_operation(tmp_path):
    manager,session,made,group=setup(tmp_path);session.start(str(group));probe=session.probe
    original=probe.gate_state;reads=[0]
    def busy_then_quiet():
        reads[0]+=1
        state=original();state['active']=int(reads[0]<3)
        return state
    probe.gate_state=busy_then_quiet
    original_snapshot=probe.snapshot
    def snapshot():
        assert reads[0]>=3
        return original_snapshot()
    probe.snapshot=snapshot;session.retire()
    assert not manager.failed and probe.resets==1

@pytest.mark.parametrize('uncertainty',['buffered','armed','phase','permit','remote'])
def test_incomplete_or_unknown_state_never_retries_remote_cleanup(tmp_path,uncertainty):
    manager,session,made,group=setup(tmp_path);session.start(str(group));probe=session.probe
    if uncertainty=='remote':probe.uncertain=True
    else:probe.gate[uncertainty]=1
    with pytest.raises(RuntimeError):session.retire()
    assert manager.failed and probe.installed and not probe.calls and session.probe is probe

def test_retire_rejects_incomplete_record_archive_without_restoration(tmp_path):
    manager,session,made,group=setup(tmp_path);session.start(str(group));probe=session.probe
    probe.rows[1]['committed']=0
    with pytest.raises(RuntimeError,match='incomplete'):session.retire()
    assert manager.failed and probe.installed and not list(group.iterdir())

def test_confirmed_process_exit_allows_new_helper_even_with_recycled_pid(tmp_path):
    manager,session,made,group=setup(tmp_path);session.start(str(group));old=session.probe;session.retire()
    old.alive=False;new_group=tmp_path/'session-2';new_group.mkdir();session.start(str(new_group))
    assert session.probe is not old and old.closed and len(made)==2
    session.retire()

def test_preflight_failure_is_unavailable_but_install_uncertainty_latches(tmp_path):
    module=implementation()
    from tools.winspec.start_acceleration.manager import Unavailable
    def preflight(*args):raise ValueError('unsupported driver')
    manager=Manager();session=module.NativeSession(manager,probe_factory=preflight,identity=lambda:(123,{}))
    with pytest.raises(Unavailable):session.start(str(tmp_path))
    assert not manager.failed and session.probe is None
    class Failure(Probe):
        def install(self):self.mutation_attempted=True;raise RuntimeError('completion unknown')
    manager,session,made,group=setup(tmp_path,Failure)
    with pytest.raises(RuntimeError):session.start(str(group))
    assert manager.failed and session.probe is made[0] and session.probe.mutation_attempted
