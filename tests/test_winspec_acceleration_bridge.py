import ast,threading,time
from pathlib import Path
from types import SimpleNamespace
import pytest
from tools.winspec.acquisition_owner import Owner
from tools.winspec.stop_owner_guard import confirmed_stop as owned_stop

def bridge():
    tree=ast.parse(Path('tools/winspec/camera_server.py').read_text())
    names=('execute','acquire','confirmed_stop','require_idle_owner','get_accelerator','mark_native_start','mark_native_complete','owner_recovery_required','rejected_capture_stop','wait_for_stop_completion')
    functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    assert len(functions)==len(names),'Formal bridge ownership hooks missing'
    events=[];exp=SimpleNamespace(Start=lambda d:events.append('start') or True,Stop=lambda:events.append('stop') or True)
    ns=dict(threading=threading,time=time,os=SimpleNamespace(path=__import__('os').path),SPE_PATH='C:/owned/temporary.spe',Owner=Owner,
        CAMERA_LOCK=threading.Lock(),ACQUISITION_STATE_LOCK=threading.Lock(),
        TRANSFER_STATE=threading.local(),START_ACCELERATOR=None,
        owned_stop=owned_stop,com_return_value=lambda v,n:v,create_experiment=lambda:exp,ProtocolError=RuntimeError,
        read_parameter=lambda *a:False,const=lambda n:n,server_log=lambda *a:None,
        read_settings=lambda e:{},read_temperature_status=lambda e:{},apply_settings=lambda e,p:events.append('settings') or p,
        TEMPERATURE_GUARD_VERSION=4,ACQUISITION_SETTINGS_VERSION=2)
    for n in ('OWNER_PENDING','TRANSFER_PENDING','CLEANUP_FAILED','STOP_REQUESTED','STOP_IN_FLIGHT','ACQUISITION_ACTIVE','NATIVE_START_COMMITTED','TEMPERATURE_MONITOR_UNHEALTHY'):ns[n]=threading.Event()
    def capture(e,**kw):e.Start(object());return {},b'data'
    ns['acquire_active']=capture
    exec(compile(ast.Module(body=functions,type_ignores=[]),'<owned bridge>','exec'),ns)
    return ns,events,exp

def test_stop_during_debugger_preparation_cannot_be_cleared_before_start():
    ns,events,exp=bridge()
    class Manager:
        failed=False
        def capture(self,e,s,session,acquire):
            reply,_=ns['execute']('STOP',{})
            assert reply['stop_requested']
            return acquire(e)
        def request_invalidate(self,reason):pass
    ns['START_ACCELERATOR']=Manager()
    with pytest.raises(RuntimeError,match='stopped'):
        ns['acquire'](exp,compact_settings=True,expected_settings={},acceleration={'enabled':True,'session':'a'*32})
    assert not events and not ns['ACQUISITION_ACTIVE'].is_set()

@pytest.mark.parametrize('state',['OWNER_PENDING','TRANSFER_PENDING','CLEANUP_FAILED','TEMPERATURE_MONITOR_UNHEALTHY','STOP_IN_FLIGHT'])
@pytest.mark.parametrize('command',['SET_SETTINGS','RELEASE_START_ACCELERATION','ACQUIRE_GUARDED'])
def test_pending_or_uncertain_owner_blocks_mutating_requests(state,command):
    ns,events,_=bridge();ns[state].set()
    with pytest.raises(RuntimeError):ns['execute'](command,{'exposure_ms':800})
    assert not events

def test_stop_latches_cancellation_before_com_stop_and_retains_uncertain_proxy():
    ns,events,exp=bridge();ns['ACQUISITION_ACTIVE'].set();ns['NATIVE_START_COMMITTED'].set()
    def stop():
        assert ns['STOP_REQUESTED'].is_set() and ns['STOP_IN_FLIGHT'].is_set()
        raise RuntimeError('COM Stop failed')
    exp.Stop=stop
    with pytest.raises(RuntimeError):ns['execute']('STOP',{})
    assert ns['TEMPERATURE_MONITOR_UNHEALTHY'].is_set() and ns['TRANSFER_STATE'].stop_owner is exp
    assert ns['owner_recovery_required']()

def test_settings_change_invalidates_baseline_before_writing():
    ns,events,_=bridge()
    ns['START_ACCELERATOR']=SimpleNamespace(failed=False,invalidate=lambda reason:events.append('invalidate'))
    ns['execute']('SET_SETTINGS',{'exposure_ms':800})
    assert events==['invalidate','settings']


def test_stop_between_commit_and_start_is_reconfirmed_after_start_returns():
    ns,events,exp=bridge();ns['ACQUISITION_ACTIVE'].set()
    def commit_then_stop():
        ns['mark_native_start']()
        reply,_=ns['execute']('STOP',{})
        assert reply['completion_pending'] is True
    checks=[False,False,True]  # Force cancellation immediately after the final pre-Start check.
    owner=Owner(exp,lambda:checks.pop(0),commit_then_stop,lambda v,n:v)
    with pytest.raises(RuntimeError,match='during Start'):owner.Start(object())
    ns['rejected_capture_stop'](owner)
    assert events==['stop','start','stop']
    assert not ns['owner_recovery_required']()


def test_blocked_start_does_not_prevent_stop_and_rejection_waits_for_stop():
    ns,events,exp=bridge();ns['ACQUISITION_ACTIVE'].set()
    entered=threading.Event();start_returns=threading.Event();stop_entered=threading.Event();stop_returns=threading.Event()
    errors=[];stops=[0]
    def start(doc):
        entered.set();assert start_returns.wait(2);return True
    def stop():
        stops[0]+=1
        if stops[0]==1:
            stop_entered.set();assert stop_returns.wait(2)
        return True
    exp.Start=start;exp.Stop=stop
    owner=Owner(exp,ns['STOP_REQUESTED'].is_set,ns['mark_native_start'],lambda v,n:v)
    def capture():
        try:owner.Start(object())
        except RuntimeError:ns['rejected_capture_stop'](owner)
        except BaseException as e:errors.append(e)
    t=threading.Thread(target=capture);t.start();assert entered.wait(2)
    stopper=threading.Thread(target=lambda:ns['execute']('STOP',{}));stopper.start()
    assert stop_entered.wait(2)  # STOP reached COM while Start is still blocked.
    start_returns.set();time.sleep(.03)
    assert stops[0]==1 and not ns['owner_recovery_required']()
    stop_returns.set();stopper.join(2);t.join(2)
    assert not errors and not t.is_alive() and not stopper.is_alive()
    assert stops[0]==2 and not ns['owner_recovery_required']()
    assert not ns['NATIVE_START_COMMITTED'].is_set()


def test_stop_after_native_completion_only_cancels_delivery():
    ns,events,exp=bridge();ns['ACQUISITION_ACTIVE'].set();ns['NATIVE_START_COMMITTED'].set()
    ns['mark_native_complete']()
    reply,_=ns['execute']('STOP',{})
    assert reply['stop_requested'] and not events and ns['STOP_REQUESTED'].is_set()


def test_native_completion_with_stalled_stop_latches_recovery():
    ns,events,exp=bridge();ns['STOP_IN_FLIGHT'].set();ns['NATIVE_START_COMMITTED'].set()
    now=[0.]
    ns['time']=SimpleNamespace(monotonic=lambda:now[0],sleep=lambda seconds:now.__setitem__(0,now[0]+1.))
    with pytest.raises(RuntimeError,match='uncertain'):ns['mark_native_complete']()
    assert ns['owner_recovery_required']() and not events and not ns['NATIVE_START_COMMITTED'].is_set()


def test_stop_after_capture_before_baseline_completion_archives_without_receipt(tmp_path):
    from tests.test_winspec_start_acceleration import setup_manager,settings
    from tests.test_winspec_bridge_guard import bridge_functions
    ns,events,exp=bridge();m,_,_,_,_,_=setup_manager(tmp_path)
    ns['START_ACCELERATOR']=m
    doc=SimpleNamespace(SaveAs=lambda p,t:events.append('archive') or True,
                        Save=lambda:True,Close=lambda:events.append('close') or True)
    ns.update(unique_spe_path=lambda:'rejected.spe',recv_exact=lambda *a:pytest.fail('Unsent frame has no receipt'))
    bridge_functions(ns,'finish_transfer')
    factory=m.factory
    def transport(*a,**kw):
        p=factory(*a,**kw);original=p.build_baseline
        def baseline(begin,end):
            ns['execute']('STOP',{})
            return original(begin,end)
        p.build_baseline=baseline;return p
    m.factory=transport
    def captured(e,**kw):
        e.Start(doc);ns['TRANSFER_STATE'].pending=(doc,'temporary.spe');ns['TRANSFER_PENDING'].set()
        return {'receipt_required':True},b'data'
    ns['acquire_active']=captured
    ns['TRANSFER_STATE'].delivery_ready=False
    with pytest.raises(RuntimeError,match='stopped'):
        ns['acquire'](exp,True,settings(),{'enabled':True,'session':'a'*32})
    assert ns['finish_transfer'](None,True) is None
    assert events==['start','stop','archive','close']
    assert not ns['owner_recovery_required']() and not ns['OWNER_PENDING'].is_set()
    assert ns['TRANSFER_STATE'].pending is None
    ns['require_idle_owner']()
