import hashlib
from pathlib import Path

import pytest

from tools.winspec import pvcam_startup_probe as probe


class FakeSDK:
    def __init__(self):
        self.events=[]; self.unsafe=False; self.size=2048; self.active=False
        self.temperature=-100.; self.partial=False; self.changed=False
        self.fail_start=False; self.fail_abort=False; self.fail_status=False; self.fail_finish=False
        self.original=dict(ser_size=512,par_size=1,bit_depth=16,exp_res=0,
            exp_time=800,temp_setpoint=-10000,gain_index=2,spdtab_index=11,exposure_mode=0)
    def snapshot(self):
        assert not self.active, 'No configuration reads while acquiring'
        result=dict(self.original)
        if self.changed:result['gain_index']=1
        return result
    def cold(self):
        assert not self.active, 'No temperature polling while acquiring'
        if self.temperature > -100:raise RuntimeError('Temperature unsafe')
        return self.temperature
    def setup(self,frames,exposure):
        self.events.append(('setup',frames,exposure))
        return self.size if frames==2 else 1024
    def pin(self,size):self.events.append('pin')
    def start(self):
        self.events.append('start');self.active=True
        if self.fail_start:raise RuntimeError('Start failed')
    def status(self):
        if self.fail_status:raise RuntimeError('Status failed')
        self.active=False
        return 3,2040 if self.partial else self.size
    def raw(self):return b'\x01\x00'*1024
    def finish(self):
        self.events.append('finish')
        if self.fail_finish:
            self.unsafe=True
            raise probe.RecoveryRequired('Finish stalled')
    def abort(self):
        self.events.append('abort')
        if self.fail_abort:
            self.unsafe=True
            raise probe.RecoveryRequired('Abort failed')
        self.active=False
    def close(self):self.events.append('close')


def run(sdk,tmp_path):
    return probe.run_probe(sdk,str(tmp_path),lambda report:None,
        dict(exposure_ms=800.,accumulations=1,sequential_frames=1,
             temperature_setpoint_c=-100.,detector_width=512,detector_height=1))


def test_one_setup_repeated_start_and_original_native_recipe_restoration(tmp_path):
    sdk=FakeSDK(); report=run(sdk,tmp_path)
    assert report['status']=='complete' and report['native_restore_ok']
    assert sdk.events.count('start')==6
    assert [e for e in sdk.events if isinstance(e,tuple)]==[('setup',2,500),('setup',1,800)]
    assert sdk.events.index('finish') < sdk.events.index(('setup',1,800)) < sdk.events.index('close')
    assert len(report['frames'])==6 and all(f['archive_verified'] for f in report['frames'])
    for frame in report['frames']:
        content=Path(frame['raw_archive']).read_bytes()
        assert hashlib.sha256(content).hexdigest()==frame['sha256']
        assert frame['mean_counts_per_exposure']==1.


@pytest.mark.parametrize('field,value',[('ser_size',1024),('bit_depth',32),('exp_res',1)])
def test_geometry_bits_and_exposure_units_reject_before_setup(tmp_path,field,value):
    sdk=FakeSDK();sdk.original[field]=value
    report=run(sdk,tmp_path)
    assert report['status']=='failed'
    assert 'start' not in sdk.events and not any(isinstance(e,tuple) for e in sdk.events)


def test_hot_camera_does_not_start_or_configure(tmp_path):
    sdk=FakeSDK();sdk.temperature=-99.99
    report=run(sdk,tmp_path)
    assert report['status']=='failed' and 'start' not in sdk.events
    assert not any(isinstance(e,tuple) for e in sdk.events)


def test_unexpected_buffer_size_rejects_before_start_but_restores_recipe(tmp_path):
    sdk=FakeSDK();sdk.size=1024
    report=run(sdk,tmp_path)
    assert report['status']=='failed' and 'start' not in sdk.events
    assert ('setup',1,800) in sdk.events


def test_partial_readout_is_not_archived_as_valid_data(tmp_path):
    sdk=FakeSDK();sdk.partial=True
    report=run(sdk,tmp_path)
    assert report['status']=='failed' and not report['frames']
    assert 'abort' in sdk.events and report['native_restore_ok']


def test_abort_failure_retains_buffer_and_skips_finish_restore_close(tmp_path):
    sdk=FakeSDK();sdk.fail_start=True;sdk.fail_abort=True
    report=run(sdk,tmp_path)
    assert report['status']=='failed' and report['recovery_required']
    assert 'finish' not in sdk.events and 'close' not in sdk.events
    assert ('setup',1,800) not in sdk.events


def test_native_configuration_change_invalidates_probe(tmp_path):
    sdk=FakeSDK();sdk.changed=True
    # Make the change appear after baseline snapshot.
    first=[True];snapshot=sdk.snapshot
    def delayed_change():
        if first[0]:
            first[0]=False
            return dict(sdk.original)
        return snapshot()
    sdk.snapshot=delayed_change
    report=run(sdk,tmp_path)
    assert report['status']=='failed' and not report['native_restore_ok']


def test_archive_is_exclusive_and_binary(tmp_path):
    target=tmp_path/'counts.raw'; data=b'\x0a\x00\x1a\x00\x0d\x00'
    probe.write_raw(str(target),data)
    assert target.read_bytes()==data
    with pytest.raises(OSError):probe.write_raw(str(target),data)


def test_temperature_units_and_region_abi():
    import ctypes
    assert probe.temperature_c(-10000)==-100.
    assert ctypes.sizeof(probe.Region)==12


def test_status_failure_aborts_and_does_not_accept_frame(tmp_path):
    sdk=FakeSDK();sdk.fail_status=True
    report=run(sdk,tmp_path)
    assert report['status']=='failed' and not report['frames']
    assert 'abort' in sdk.events and report['native_restore_ok']


def test_extra_transfer_is_rejected(tmp_path):
    sdk=FakeSDK();sdk.status=lambda:(3,2050)
    report=run(sdk,tmp_path)
    assert report['status']=='failed' and not report['frames']


def test_finish_failure_keeps_buffer_and_skips_restore_close(tmp_path):
    sdk=FakeSDK();sdk.fail_finish=True
    report=run(sdk,tmp_path)
    assert report['recovery_required'] and report['status']=='failed'
    assert ('setup',1,800) not in sdk.events and 'close' not in sdk.events


def test_archive_failure_stops_further_starts_but_restores(tmp_path,monkeypatch):
    sdk=FakeSDK()
    monkeypatch.setattr(probe,'write_raw',lambda *args:(_ for _ in ()).throw(OSError('Disk full')))
    report=run(sdk,tmp_path)
    assert report['status']=='failed' and not report['frames']
    assert sdk.events.count('start')==1 and report['native_restore_ok']


def test_native_call_timeout_blocks_subsequent_dispatch():
    import queue
    sdk=probe.NativePVCAM.__new__(probe.NativePVCAM)
    sdk.unsafe=False;sdk.tasks=queue.Queue()
    with pytest.raises(probe.RecoveryRequired,match='stalled'):
        sdk.call('pl_exp_start_seq',timeout=.001)
    assert sdk.unsafe and sdk.tasks.qsize()==1
    with pytest.raises(probe.RecoveryRequired,match='no more'):
        sdk.call('pl_cam_close',timeout=.001)
    assert sdk.tasks.qsize()==1


def test_report_write_failure_prevents_next_capture_and_retains_raw(tmp_path):
    sdk=FakeSDK();calls=[0]
    def persist(report):
        calls[0]+=1
        if calls[0]==2:raise OSError('Report disk full')
    report=probe.run_probe(sdk,str(tmp_path),persist,
        dict(exposure_ms=800.,accumulations=1,sequential_frames=1,temperature_setpoint_c=-100.))
    assert report['status']=='failed' and report['native_restore_ok']
    assert sdk.events.count('start')==1
    assert (tmp_path/'frame-00.raw').exists()


def test_interrupted_native_wait_latches_uncertainty(monkeypatch):
    import queue
    class InterruptedEvent:
        def wait(self,timeout):raise KeyboardInterrupt()
    sdk=probe.NativePVCAM.__new__(probe.NativePVCAM)
    sdk.unsafe=False;sdk.tasks=queue.Queue()
    monkeypatch.setattr(probe.threading,'Event',InterruptedEvent)
    with pytest.raises(probe.RecoveryRequired):sdk.call('pl_exp_start_seq')
    assert sdk.unsafe and sdk.tasks.qsize()==1


def test_unsafe_main_holds_even_when_report_persistence_raises(tmp_path,monkeypatch):
    import json,time
    sdk=FakeSDK();held=[]
    class Lease:
        closed=False
        def close(self):self.closed=True
    lease=Lease()
    (tmp_path/'preflight.json').write_text(json.dumps(dict(saved_unix=time.time(),settings={},v12_summary={})))
    monkeypatch.setattr(probe,'__file__',str(tmp_path/'probe.py'))
    monkeypatch.setattr(probe.ct,'sizeof',lambda *args:4)
    monkeypatch.setattr(probe.subprocess,'check_output',lambda *a,**k:b'')
    monkeypatch.setattr(probe,'reserve_bridge_port',lambda:lease)
    monkeypatch.setattr(probe.os.path,'isfile',lambda path:True)
    monkeypatch.setattr(probe.os,'makedirs',lambda *args:None)
    monkeypatch.setattr(probe,'NativePVCAM',lambda path:sdk)
    environment_events=[]
    class Environment:
        def __init__(self,path):self.details={}
        def activate(self):environment_events.append('activate')
        def restore(self):environment_events.append('restore')
    monkeypatch.setattr(probe,'WinSpecDriverEnvironment',Environment)
    def failed(*args):
        sdk.unsafe=True
        raise OSError('Report storage failed')
    def hold(s,l):
        held.append((s,l))
        raise RuntimeError('Recovery process retained')
    monkeypatch.setattr(probe,'run_probe',failed)
    monkeypatch.setattr(probe,'hold_for_recovery',hold)
    with pytest.raises(RuntimeError,match='retained'):probe.main()
    assert held==[(sdk,lease)] and not lease.closed
    assert environment_events==['activate']


def test_snapshot_does_not_suppress_failed_read_of_available_parameter():
    sdk=probe.NativePVCAM.__new__(probe.NativePVCAM)
    def get(name):
        if name=='gain_index':raise RuntimeError('Available parameter type mismatch')
        return 0
    sdk.get=get
    with pytest.raises(RuntimeError,match='type mismatch'):sdk.snapshot()


def test_original_exposure_mode_must_be_known(tmp_path):
    sdk=FakeSDK();del sdk.original['exposure_mode']
    report=run(sdk,tmp_path)
    assert report['status']=='failed' and 'start' not in sdk.events
    assert not any(isinstance(e,tuple) for e in sdk.events)


def test_timed_restore_uses_cached_winspec_exposure_not_variable_mode_parameter(tmp_path):
    sdk=FakeSDK();sdk.original['exp_time']=123
    report=run(sdk,tmp_path)
    assert report['status']=='complete'
    assert ('setup',1,800) in sdk.events and ('setup',1,123) not in sdk.events


@pytest.mark.parametrize('command',[
    r'"C:\Program Files\PI Acton\WinSpec\Winspec.exe" /Automation',
    r'C:\Program Files\PI Acton\WinSpec\Winspec.exe /Automation',
])
def test_dll_discovery_uses_registered_winspec_directory(monkeypatch,command):
    import sys,types
    registry=types.SimpleNamespace(HKEY_CLASSES_ROOT=object())
    entries={r'WinX32.ExpSetup\CLSID':'{example}',
             r'CLSID\{example}\LocalServer32':command}
    def query(root,key):
        if key not in entries:raise OSError('Missing registry key')
        return entries[key]
    registry.QueryValue=query
    monkeypatch.setitem(sys.modules,'_winreg',registry)
    wanted=r'C:\Program Files\PI Acton\WinSpec\Pvcam32.dll'
    monkeypatch.setattr(probe.os.path,'isfile',lambda path:path==wanted)
    path,source=probe.find_pvcam_dll(r'C:\Probe')
    assert path==wanted and source=='registered WinSpec installation'


def test_installed_dll_preferred_over_existing_backup(monkeypatch,tmp_path):
    installed=tmp_path/'installed';installed.mkdir()
    (installed/'Pvcam32.dll').write_bytes(b'installed')
    monkeypatch.setattr(probe,'registered_winspec_dirs',lambda:[str(installed)])
    folder=tmp_path/'pvcam-startup-probe';folder.mkdir()
    backup=tmp_path/'local_bundle'/'LicensedBackup'/'WinSpec'
    backup.mkdir(parents=True);(backup/'Pvcam32.dll').write_bytes(b'backup')
    path,source=probe.find_pvcam_dll(str(folder))
    assert Path(path).read_bytes()==b'installed'
    assert source=='registered WinSpec installation'


def test_existing_licensed_backup_found_without_copying_or_installing(monkeypatch,tmp_path):
    monkeypatch.setattr(probe,'registered_winspec_dirs',lambda:[])
    folder=tmp_path/'pvcam-startup-probe';folder.mkdir()
    backup=tmp_path/'local_bundle'/'LicensedBackup'/'WinSpec'
    backup.mkdir(parents=True);dll=backup/'Pvcam32.dll';dll.write_bytes(b'existing')
    before=set(tmp_path.rglob('*'))
    path,source=probe.find_pvcam_dll(str(folder))
    assert Path(path)==dll and source=='existing licensed WinSpec backup'
    assert set(tmp_path.rglob('*'))==before and dll.read_bytes()==b'existing'


def test_missing_dll_error_lists_checked_paths(monkeypatch):
    monkeypatch.setattr(probe,'registered_winspec_dirs',lambda:[r'C:\Actual WinSpec'])
    monkeypatch.setattr(probe.os.path,'isfile',lambda path:False)
    with pytest.raises(RuntimeError,match='Checked paths') as failure:
        probe.find_pvcam_dll(r'C:\Probe')
    assert r'C:\Actual WinSpec\Pvcam32.dll' in str(failure.value)
    assert 'LicensedBackup' in str(failure.value)


@pytest.mark.parametrize('version',[0x0270,0x027f,0x028b])
def test_native_open_accepts_legacy_27_and_installed_2811(version):
    import ctypes
    sdk=probe.NativePVCAM.__new__(probe.NativePVCAM)
    sdk.initialized=False;sdk.opened=False;sdk.handle=ctypes.c_short()
    events=[]
    def call(name,*args,**kwargs):
        events.append(name)
        if name=='pl_pvcam_get_ver':args[0]._obj.value=version
        elif name=='pl_cam_get_total':args[0]._obj.value=1
        elif name=='pl_cam_get_name':args[1].value=b'ExistingCamera'
        elif name=='pl_cam_open':args[1]._obj.value=42
    sdk.call=call
    sdk.open()
    assert sdk.initialized and sdk.opened and sdk.handle.value==42
    assert sdk.version==version and sdk.camera_name=='ExistingCamera'
    assert events[-1]=='pl_cam_get_diags'


@pytest.mark.parametrize('version',[0x028a,0x029b,0x0301])
def test_other_native_versions_rejected_before_camera_open(version):
    sdk=probe.NativePVCAM.__new__(probe.NativePVCAM)
    sdk.initialized=False;sdk.opened=False
    events=[]
    def call(name,*args,**kwargs):
        events.append(name)
        if name=='pl_pvcam_get_ver':args[0]._obj.value=version
    sdk.call=call
    with pytest.raises(RuntimeError,match='PVCAM'):sdk.open()
    assert sdk.initialized and not sdk.opened
    assert 'pl_cam_open' not in events and 'pl_cam_get_total' not in events


class FakeSearchKernel:
    def __init__(self):
        self.directory=r'C:\PreviousSearch';self.reject=False;self.set_calls=[]
        def get_directory(size,buffer):
            if buffer is not None:buffer.value=self.directory
            return len(self.directory)
        def set_directory(directory):
            self.set_calls.append(directory)
            if self.reject:return 0
            self.directory=directory or ''
            return 1
        self.GetDllDirectoryW=get_directory;self.SetDllDirectoryW=set_directory


def test_driver_environment_supplies_and_restores_installed_directory(tmp_path,monkeypatch):
    import os
    kernel=FakeSearchKernel()
    monkeypatch.setattr(probe.ct,'WinDLL',lambda *a,**k:kernel)
    monkeypatch.chdir(tmp_path)
    installed=tmp_path/'WinSpec';installed.mkdir()
    (installed/'contrman.dll').write_bytes(b'present')
    environment=probe.WinSpecDriverEnvironment(str(installed/'Pvcam32.dll'))
    try:
        environment.activate()
        assert Path(os.getcwd())==installed
        assert kernel.directory==str(installed)
        assert environment.details['dependency_files']['contrman.dll'] is True
        assert environment.details['dependency_files']['usbdrvd.dll'] is False
    finally:
        environment.restore()
    assert Path(os.getcwd())==tmp_path and kernel.directory==r'C:\PreviousSearch'


def test_failed_search_directory_setup_does_not_change_working_directory(tmp_path,monkeypatch):
    import os
    kernel=FakeSearchKernel();kernel.reject=True
    monkeypatch.setattr(probe.ct,'WinDLL',lambda *a,**k:kernel)
    monkeypatch.chdir(tmp_path)
    environment=probe.WinSpecDriverEnvironment(str(tmp_path/'Pvcam32.dll'))
    with pytest.raises(RuntimeError,match='SetDllDirectory'):environment.activate()
    environment.restore()
    assert Path(os.getcwd())==tmp_path and kernel.directory==r'C:\PreviousSearch'


def test_working_directory_failure_restores_previous_dll_search(tmp_path,monkeypatch):
    kernel=FakeSearchKernel()
    monkeypatch.setattr(probe.ct,'WinDLL',lambda *a,**k:kernel)
    environment=probe.WinSpecDriverEnvironment(str(tmp_path/'absent'/'Pvcam32.dll'))
    with pytest.raises(OSError):environment.activate()
    environment.restore()
    assert kernel.directory==r'C:\PreviousSearch'


@pytest.mark.parametrize('access',[3,4])
def test_available_but_unreadable_parameter_is_not_read(access):
    sdk=probe.NativePVCAM.__new__(probe.NativePVCAM)
    sdk.parameter_diagnostics={}
    sdk.handle=probe.ct.c_short(42);attributes=[]
    def call(name,handle,pid,attribute,pointer):
        assert name=='pl_get_param' and pid==0x06020061
        attributes.append(attribute)
        if attribute==8:pointer._obj.value=1
        elif attribute==7:pointer._obj.value=access
        else:pytest.fail('Must not request type/current of an unreadable parameter')
    sdk.call=call
    with pytest.raises(probe.UnsupportedParameter):sdk.get('clear_cycles')
    assert attributes==[8,7]
    assert sdk.parameter_diagnostics['clear_cycles']['access']==access


def test_readable_type_mismatch_reports_expected_actual_and_stops_before_value():
    sdk=probe.NativePVCAM.__new__(probe.NativePVCAM)
    sdk.parameter_diagnostics={}
    sdk.handle=probe.ct.c_short(42);attributes=[]
    def call(name,handle,pid,attribute,pointer):
        attributes.append(attribute)
        pointer._obj.value={8:1,7:1,2:1}[attribute]
    sdk.call=call
    with pytest.raises(RuntimeError,match='expected 6, actual 1'):sdk.get('clear_cycles')
    assert attributes==[8,7,2]
    details=sdk.parameter_diagnostics['clear_cycles']
    assert details['actual_type']==1 and details['expected_type']==6


def test_readable_parameter_value_and_metadata_are_captured():
    sdk=probe.NativePVCAM.__new__(probe.NativePVCAM)
    sdk.parameter_diagnostics={}
    sdk.handle=probe.ct.c_short(42)
    def call(name,handle,pid,attribute,pointer):
        pointer._obj.value={8:1,7:2,2:6,0:5}[attribute]
    sdk.call=call
    assert sdk.get('clear_cycles')==5
    assert sdk.parameter_diagnostics['clear_cycles']['value']==5


def test_snapshot_reports_all_synchronous_parameter_errors_in_one_pass():
    sdk=probe.NativePVCAM.__new__(probe.NativePVCAM);names=[]
    def get(name):
        names.append(name)
        if name in ('clear_cycles','gain_index'):raise RuntimeError(name+' type mismatch')
        return 0
    sdk.get=get
    with pytest.raises(RuntimeError) as failure:sdk.snapshot()
    assert 'clear_cycles' in str(failure.value) and 'gain_index' in str(failure.value)
    assert set(names)==set(probe.PARAMS)-{'temp'}


def test_snapshot_stops_immediately_on_uncertain_native_call():
    sdk=probe.NativePVCAM.__new__(probe.NativePVCAM);names=[]
    def get(name):
        names.append(name)
        raise probe.RecoveryRequired('Native wait stalled')
    sdk.get=get
    with pytest.raises(probe.RecoveryRequired):sdk.snapshot()
    assert len(names)==1


def test_observed_2811_clear_cycles_read_uses_reported_signed_16_bit_type():
    import ctypes
    sdk=probe.NativePVCAM.__new__(probe.NativePVCAM)
    sdk.version=0x028b;sdk.handle=ctypes.c_short(42);sdk.parameter_diagnostics={}
    def call(name,handle,pid,attribute,pointer):
        assert pid==0x06020061
        if attribute==0:assert isinstance(pointer._obj,ctypes.c_short)
        pointer._obj.value={8:1,7:2,2:1,0:3}[attribute]
    sdk.call=call
    assert sdk.get('clear_cycles')==3
    assert sdk.parameter_diagnostics['clear_cycles']['actual_type']==1


@pytest.mark.parametrize('version',[0x027b,0x028a])
def test_signed_clear_cycles_exception_does_not_apply_to_other_versions(version):
    sdk=probe.NativePVCAM.__new__(probe.NativePVCAM)
    sdk.version=version;sdk.handle=probe.ct.c_short(42);sdk.parameter_diagnostics={}
    def call(name,handle,pid,attribute,pointer):
        assert attribute!=0
        pointer._obj.value={8:1,7:2,2:1}[attribute]
    sdk.call=call
    with pytest.raises(RuntimeError,match='type mismatch'):sdk.get('clear_cycles')


def test_observed_driver_exception_does_not_relax_other_parameter_types():
    sdk=probe.NativePVCAM.__new__(probe.NativePVCAM)
    sdk.version=0x028b;sdk.handle=probe.ct.c_short(42);sdk.parameter_diagnostics={}
    def call(name,handle,pid,attribute,pointer):
        assert attribute!=0
        pointer._obj.value={8:1,7:2,2:6}[attribute]
    sdk.call=call
    with pytest.raises(RuntimeError,match='type mismatch'):sdk.get('gain_index')


def test_negative_clear_cycles_are_rejected_even_for_observed_driver():
    sdk=probe.NativePVCAM.__new__(probe.NativePVCAM)
    sdk.version=0x028b;sdk.handle=probe.ct.c_short(42);sdk.parameter_diagnostics={}
    def call(name,handle,pid,attribute,pointer):
        pointer._obj.value={8:1,7:2,2:1,0:-1}[attribute]
    sdk.call=call
    with pytest.raises(RuntimeError,match='Negative'):sdk.get('clear_cycles')
