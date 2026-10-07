import pytest
from app.devices.winspec_adapter import WinSpecSetup
from tests.test_winspec_adapter import Camera,Optics


class SupportedCamera(Camera):
    def request(self,command,parameters=None,**kwargs):
        r,p=super().request(command,parameters,**kwargs)
        r.update(start_acceleration_version=1,acquisition_settings_version=2)
        if command=='ACQUIRE_GUARDED':
            options=parameters['start_acceleration']
            r['start_acceleration']=dict(version=1,requested=options['enabled'],mode='baseline' if options['enabled'] else 'disabled',optimized=False,session=options['session'])
            r['temporary_spe_cleanup']='complete'
        return r,p


def setup(monkeypatch,enabled=True):
    monkeypatch.setattr('app.devices.winspec_adapter.ensure_output_route',lambda *a,**kw:{})
    c=SupportedCamera();s=WinSpecSetup(Optics(),client=c,start_acceleration=enabled)
    return s,c


def test_opt_in_uses_managed_wire_and_release_and_fresh_session(monkeypatch):
    s,c=setup(monkeypatch);s.acquire();options=c.calls[-1][1]['start_acceleration']
    assert options['enabled'] is True and len(options['session'])==32
    assert s.read_metadata_snapshot()['observed']['last_frame']['start_acceleration']['mode']=='baseline'
    s.close();assert c.calls[-1]==('RELEASE_START_ACCELERATION',{'session':options['session']})
    other,_=setup(monkeypatch);other.acquire();assert other._start_session!=options['session']


def test_default_off_does_not_require_new_bridge_or_release(monkeypatch):
    monkeypatch.setattr('app.devices.winspec_adapter.ensure_output_route',lambda *a,**kw:{})
    c=Camera();s=WinSpecSetup(Optics(),client=c);s.acquire();s.close()
    assert all(cmd!='RELEASE_START_ACCELERATION' for cmd,p in c.calls)
    assert s.identity.get('start_acceleration_requested') is False


def test_explicit_opt_in_with_old_bridge_fails_before_writes():
    c=Camera()
    with pytest.raises(RuntimeError,match='upgrade'):
        WinSpecSetup(Optics(),client=c,start_acceleration=True)
    assert all(cmd=='GET_SETTINGS' for cmd,p in c.calls)


@pytest.mark.parametrize('bad',['missing','foreign_session','false_optimization','cleanup','missing_cleanup'])
def test_invalid_acceleration_or_cleanup_report_stops_publish(monkeypatch,bad):
    s,c=setup(monkeypatch);request=c.request
    def altered(command,*a,**kw):
        r,p=request(command,*a,**kw)
        if command=='ACQUIRE_GUARDED':
            if bad=='missing':r.pop('start_acceleration')
            if bad=='foreign_session':r['start_acceleration']['session']='b'*32
            if bad=='false_optimization':r['start_acceleration']['optimized']=True
            if bad=='cleanup':r['temporary_spe_cleanup']='unconfirmed'
            if bad=='missing_cleanup':r.pop('temporary_spe_cleanup')
        return r,p
    c.request=altered
    with pytest.raises(RuntimeError):s.acquire()
    assert s._abort.is_set()
    with pytest.raises(RuntimeError,match='stopped'):s.acquire()
