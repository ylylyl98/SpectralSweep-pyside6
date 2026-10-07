import struct,threading
import pytest
from tools.winspec.start_acceleration.native_probe import NativeProbe,FIELDS,Reader

def probe():
    p=object.__new__(NativeProbe);p.lock=threading.Lock();p.uncertain=False
    p.cm=1;p.pb=2;p.controller=3;p.port=4;p.pipe=5;p.command_handle=6;p.control_entry=7
    p.control_buffer=None;p.allocations=[];p.calls=[];memory={};invokes=[]
    def allocate(raw):
        address=0x10000+len(p.allocations)*4096;p.allocations.append(address);memory[address]=raw;return address
    p.allocate=allocate;p.write_checked=lambda a,raw:memory.__setitem__(a,raw)
    def invoke(fn,a):
        invokes.append(a);cfg=dict(zip(FIELDS,struct.unpack('<18I',memory[a])))
        cfg.update(result=1,row_size=140,records=0,rows=0x90000)
        memory[a]=struct.pack('<18I',*[cfg[k] for k in FIELDS]);return 1
    p.invoke=invoke;p.read=lambda a,n:memory[a][:n]
    return p,memory,invokes

def test_one_control_buffer_reused_for_thousands_of_confirmed_calls():
    p,memory,invokes=probe()
    for i in range(3000):assert p.control(0)['result']==1
    assert len(p.allocations)==1 and len(set(invokes))==1

def test_uncertain_control_readback_blocks_buffer_reuse():
    p,memory,invokes=probe();p.control(0)
    p.read=lambda *a:b'bad'
    with pytest.raises(Exception):p.control(0)
    assert p.uncertain
    with pytest.raises(RuntimeError):p.control(0)
    assert len(invokes)==2 and len(p.allocations)==1

def test_uncertain_write_blocks_future_call_and_preserves_allocation():
    p,memory,invokes=probe();p.control(0)
    def fail(*a):raise RuntimeError('partial control buffer write')
    p.write_checked=fail
    with pytest.raises(RuntimeError):p.control(0)
    assert p.uncertain and len(invokes)==1 and len(p.allocations)==1
    with pytest.raises(RuntimeError):p.control(0)
    assert len(invokes)==1

def test_reset_passes_exact_archived_ack_and_does_not_clear_calls():
    p,memory,invokes=probe();p.calls.append({'evidence':'must retain'})
    status=dict(records=456,rows=0x90000,row_size=140)
    gate=dict(generation=17,frame=19)
    p.reset_archived(status,gate)
    cfg=dict(zip(FIELDS,struct.unpack('<18I',memory[invokes[-1]])))
    assert (cfg['operation'],cfg['mode'],cfg['frame'])==(6,17,19)
    assert p.calls==[{'evidence':'must retain'}]

def test_preflight_failure_closes_only_its_handles_before_mutation(monkeypatch):
    from types import SimpleNamespace
    closed=[];opened=[]
    k=SimpleNamespace(CloseHandle=lambda h:closed.append(h) or 1,
                      OpenProcess=lambda rights,inherit,pid:opened.append((rights,pid)) or 600)
    def reader(p,pid):p.k=k;p.handle=500
    def reject(p,path):
        assert not p.mutation_attempted and p.dll is None and not p.allocations
        raise RuntimeError('preflight identity mismatch')
    monkeypatch.setattr(Reader,'__init__',reader)
    monkeypatch.setattr(NativeProbe,'_preflight',reject)
    with pytest.raises(RuntimeError,match='preflight'):NativeProbe(123,'dll','audit')
    assert closed==[500,600] and opened==[(0x10043a,123)]

def test_process_handle_is_retained_until_confirmed_exit():
    from types import SimpleNamespace
    p=object.__new__(NativeProbe);p.handle=123;closed=[];state=[258]
    p.k=SimpleNamespace(WaitForSingleObject=lambda h,n:state[0],CloseHandle=lambda h:closed.append(h) or 1)
    with pytest.raises(RuntimeError,match='living'):p.close_exited()
    state[0]=0xffffffff
    with pytest.raises(RuntimeError,match='unknown'):p.close_exited()
    assert closed==[]
    state[0]=0;p.close_exited();assert closed==[123] and p.handle is None

def test_reset_uses_gate_nonce_and_exact_row_descriptor():
    p=object.__new__(NativeProbe);seen=[]
    p.control=lambda *a,**kw:seen.append((a,kw))
    status=dict(frame=0,records=456,rows=0x90000,row_size=140)
    p.reset_archived(status,dict(generation=17,frame=19))
    assert seen==[((6,),dict(mode=17,frame=19,archive=status))]
