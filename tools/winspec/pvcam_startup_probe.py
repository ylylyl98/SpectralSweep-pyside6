from __future__ import print_function

"""Throwaway XP/PVCAM feasibility probe. Does not replace the v12 bridge.

ABI/sequence contract: Roper master.h and PVCAM 2.7 manual pp.70,78-81,125-126.
Two raw uint16 frames are NOT WinSpec DeviceEPF2 hardware accumulation.
"""
import ctypes as ct
import hashlib
import json
import ntpath
import os
import re
import struct
import subprocess
import sys
import threading
import time
try:
    import Queue as queue
except ImportError:
    import queue
try:
    from .startup_reuse_probe import clock, median, reserve_bridge_port, persist_reports
except (ImportError, ValueError):
    from startup_reuse_probe import clock, median, reserve_bridge_port, persist_reports


class RecoveryRequired(RuntimeError):
    pass


class UnsupportedParameter(RuntimeError):
    pass


class Region(ct.Structure):
    _fields_ = [(name, ct.c_ushort) for name in ('s1','s2','sbin','p1','p2','pbin')]


# Stable PVCAM parameter definitions; no numeric WinSpec enum guesses.
# TYPE_INT16=1, TYPE_UNS16=6, TYPE_ENUM=9; CLASS2=2, CLASS3=3.
PARAMS = {
    'ser_size': (6,2,58), 'par_size': (6,2,57), 'bit_depth': (1,2,511),
    'temp': (1,2,525), 'temp_setpoint': (1,2,526),
    'gain_index': (1,2,512), 'spdtab_index': (1,2,513),
    'clear_cycles': (6,2,97), 'clear_mode': (9,2,523),
    'shutter_mode': (9,2,521), 'exposure_mode': (9,2,535),
    # PARAM_EXP_TIME is for VARIABLE_TIMED_MODE; it is not a timed-mode getter.
    'exp_res': (9,3,2),
    # PVCAM 2.7 manual p.62 / manufacturer's pvcam.h constants.
    'pix_time': (6,2,516), 'adc_offset': (1,2,195),
}


def temperature_c(raw):
    return raw / 100.


def write_raw(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os,'O_BINARY',0), 0o600)
    with os.fdopen(fd,'wb') as handle:
        handle.write(raw); handle.flush(); os.fsync(handle.fileno())


def registered_winspec_dirs():
    """Read COM registration without creating a WinSpec object/process."""
    try:
        import _winreg as registry
    except ImportError:
        import winreg as registry
    directories=[]
    for progid in ('WinX32.ExpSetup','WinX32.DocFile','WinX32.WinXApp'):
        try:
            clsid=registry.QueryValue(registry.HKEY_CLASSES_ROOT,progid+'\\CLSID')
            command=registry.QueryValue(registry.HKEY_CLASSES_ROOT,
                                        'CLSID\\'+clsid+'\\LocalServer32')
        except OSError:
            continue
        command=os.path.expandvars(command.strip())
        if command.startswith('"'):
            match=re.match(r'^"([^"\r\n]+\.exe)"(?:\s|$)',command,re.I)
        else:
            match=re.match(r'^([^"\r\n]+?\.exe)(?:\s|$)',command,re.I)
        if match:
            directory=ntpath.dirname(match.group(1))
            if ntpath.isabs(directory) and directory not in directories:
                directories.append(directory)
    return directories


def find_pvcam_dll(folder):
    candidates=[(ntpath.join(directory,'Pvcam32.dll'),'registered WinSpec installation')
                for directory in registered_winspec_dirs()]
    candidates.extend([(r'C:\WinSpec\Pvcam32.dll','legacy WinSpec installation'),
                       (ntpath.join(os.environ.get('SystemRoot',r'C:\Windows'),
                                    'System32','Pvcam32.dll'),'existing system installation')])
    # The licensed backup already exists in XP_TRANSFER. Load it directly; never
    # copy/install it or modify the installed camera driver.
    candidates.extend([(os.path.normpath(os.path.join(folder,'..','local_bundle',
                           'LicensedBackup','WinSpec','Pvcam32.dll')),
                        'existing licensed WinSpec backup'),
                       (r'\\vmware-host\Shared Folders\XP_TRANSFER\local_bundle\LicensedBackup\WinSpec\Pvcam32.dll',
                        'existing licensed WinSpec backup')])
    for path,source in candidates:
        if os.path.isfile(path):return path,source
    raise RuntimeError('Existing Pvcam32.dll not found; do not install another driver. '
                       'Checked paths: '+'; '.join(path for path,source in candidates))


class WinSpecDriverEnvironment(object):
    """Process-local search path for legacy, dynamically loaded controller DLLs."""
    def __init__(self,dll_path):
        self.directory=os.path.abspath(os.path.dirname(dll_path))
        if not isinstance(self.directory,type(u'')):
            self.directory=self.directory.decode(sys.getfilesystemencoding() or 'mbcs')
        self.original_cwd=os.getcwd();self.active=False
        self.kernel=ct.WinDLL('kernel32',use_last_error=True)
        self.kernel.GetDllDirectoryW.argtypes=[ct.c_uint32,ct.c_wchar_p]
        self.kernel.GetDllDirectoryW.restype=ct.c_uint32
        self.kernel.SetDllDirectoryW.argtypes=[ct.c_wchar_p]
        self.kernel.SetDllDirectoryW.restype=ct.c_int
        previous=ct.create_unicode_buffer(32768)
        ct.set_last_error(0)
        length=self.kernel.GetDllDirectoryW(len(previous),previous)
        if length>=len(previous) or (length==0 and ct.get_last_error()):
            raise RuntimeError('GetDllDirectory failed; driver environment unchanged')
        self.previous_search=previous.value
        names=('contrman.dll','nvram.dll','PICHIPDB.dll','pidc32.dll','usbdrvd.dll')
        self.details=dict(original_cwd=self.original_cwd,dll_directory=self.directory,
                          previous_dll_directory=self.previous_search,
                          dependency_files=dict((name,os.path.isfile(os.path.join(self.directory,name)))
                                                for name in names))

    def activate(self):
        if not self.kernel.SetDllDirectoryW(self.directory):
            raise RuntimeError('SetDllDirectory failed; no PVCAM initialization attempted')
        self.active=True
        os.chdir(self.directory)
        self.details['effective_cwd']=os.getcwd()
        print('WinSpec driver environment: '+json.dumps(self.details,sort_keys=True))

    def restore(self):
        if not self.active:return
        try:os.chdir(self.original_cwd)
        finally:
            if not self.kernel.SetDllDirectoryW(self.previous_search or None):
                raise RuntimeError('SetDllDirectory restoration failed')
            self.active=False


class NativePVCAM(object):
    """One persistent native thread; never queue another call behind a stalled one."""
    def __init__(self, dll_path):
        self.dll = ct.WinDLL(dll_path)
        self.dll_path = dll_path
        self.unsafe = False; self.initialized = False; self.opened = False
        self.sequence_initialized = False; self.buffer = None; self.pinned = False
        self.handle = ct.c_short(); self.last_call_s = 0.
        self.parameter_diagnostics={}
        p16=ct.POINTER(ct.c_short); pu16=ct.POINTER(ct.c_ushort); pu32=ct.POINTER(ct.c_uint32)
        signatures = {
            'pl_pvcam_init': [], 'pl_pvcam_uninit': [], 'pl_pvcam_get_ver': [pu16],
            'pl_cam_get_total': [p16], 'pl_cam_get_name': [ct.c_short,ct.c_char_p],
            'pl_cam_open': [ct.c_char_p,p16,ct.c_short], 'pl_cam_close': [ct.c_short],
            'pl_cam_get_diags': [ct.c_short],
            'pl_get_param': [ct.c_short,ct.c_uint32,ct.c_short,ct.c_void_p],
            'pl_exp_init_seq': [], 'pl_exp_uninit_seq': [],
            'pl_exp_setup_seq': [ct.c_short,ct.c_ushort,ct.c_ushort,ct.POINTER(Region),ct.c_short,ct.c_uint32,pu32],
            'pl_exp_start_seq': [ct.c_short,ct.c_void_p],
            'pl_exp_check_status': [ct.c_short,p16,pu32],
            'pl_exp_finish_seq': [ct.c_short,ct.c_void_p,ct.c_short],
            'pl_exp_abort': [ct.c_short,ct.c_short],
        }
        for name,args in signatures.items():
            fn=getattr(self.dll,name); fn.argtypes=args; fn.restype=ct.c_ushort
        self.dll.pl_error_code.argtypes=[]; self.dll.pl_error_code.restype=ct.c_short
        self.dll.pl_error_message.argtypes=[ct.c_short,ct.c_char_p]
        self.dll.pl_error_message.restype=ct.c_ushort
        self.tasks=queue.Queue()
        self.worker=threading.Thread(target=self._worker); self.worker.daemon=True; self.worker.start()
        self.kernel=ct.WinDLL('kernel32')
        for name in ('VirtualLock','VirtualUnlock'):
            fn=getattr(self.kernel,name); fn.argtypes=[ct.c_void_p,ct.c_size_t]; fn.restype=ct.c_int

    def _worker(self):
        while True:
            name,args,done,result=self.tasks.get()
            started=clock()
            try:
                if not getattr(self.dll,name)(*args):
                    code=self.dll.pl_error_code(); message=ct.create_string_buffer(256)
                    self.dll.pl_error_message(code,message)
                    raise RuntimeError('%s: %d %s' % (name,code,message.value.decode('ascii','replace')))
            except BaseException as error:
                result['error']=error
            finally:
                result['native_s']=clock()-started; done.set()

    def call(self,name,*args,**kwargs):
        if self.unsafe:raise RecoveryRequired('Native operation is uncertain; no more driver calls')
        done=threading.Event();result={}
        self.tasks.put((name,args,done,result))
        try:
            done.wait(kwargs.get('timeout',10.))
        except BaseException:
            # The native thread may still hold pointers supplied by this call.
            # Do not enqueue abort/restore/close behind an interrupted wait.
            self.unsafe=True
            raise RecoveryRequired('%s wait interrupted; retain native resources' % name)
        if not done.is_set():
            self.unsafe=True
            raise RecoveryRequired('%s stalled; retain DLL, handle and pinned buffer' % name)
        self.last_call_s=result['native_s']
        if 'error' in result:raise result['error']

    def open(self):
        self.call('pl_pvcam_init',timeout=30.); self.initialized=True
        version=ct.c_ushort();self.call('pl_pvcam_get_ver',ct.byref(version))
        self.version=version.value
        version_parts=(self.version>>8,(self.version>>4)&15,self.version&15)
        # The registered XP WinSpec DLL reports 2.8.11 (0x028b). It exports
        # the legacy sequence API bound above. Admit this observed build only;
        # do not assume arbitrary newer drivers share this probe's contract.
        if version_parts[:2]!=(2,7) and self.version!=0x028b:
            raise RuntimeError('Expected existing PVCAM 2.7.x or 2.8.11; got 0x%04x' % self.version)
        self.version_text='%d.%d.%d' % version_parts
        print('PVCAM API version: '+self.version_text)
        count=ct.c_short();self.call('pl_cam_get_total',ct.byref(count))
        if count.value!=1:raise RuntimeError('Require exactly one XP PVCAM camera; got %d' % count.value)
        name=ct.create_string_buffer(32); self.call('pl_cam_get_name',0,name)
        self.camera_name=name.value.decode('ascii','replace')
        self.call('pl_cam_open',name,ct.byref(self.handle),0,timeout=30.);self.opened=True
        self.call('pl_cam_get_diags',self.handle)

    def get(self,name):
        typ,cls,index=PARAMS[name]; pid=(typ<<24)+(cls<<16)+index
        details=dict(param_id=pid,expected_type=typ)
        self.parameter_diagnostics[name]=details
        try:
            available=ct.c_ushort();self.call('pl_get_param',self.handle,pid,8,ct.byref(available))
            details['available']=bool(available.value)
            if not available.value:raise UnsupportedParameter('Unsupported PVCAM parameter: '+name)
            # PVCAM manual pp.45,48-49: availability does not establish read
            # access. ATTR_ACCESS is uns16; only READ_ONLY/READ_WRITE allow
            # ATTR_CURRENT. EXIST_CHECK_ONLY/WRITE_ONLY are not readable.
            access=ct.c_ushort();self.call('pl_get_param',self.handle,pid,7,ct.byref(access))
            details['access']=access.value
            if access.value in (3,4):
                raise UnsupportedParameter('PVCAM parameter is not readable: %s (access %d)' % (name,access.value))
            if access.value not in (1,2):
                raise RuntimeError('Invalid PVCAM parameter access: %s (%d)' % (name,access.value))
            actual_type=ct.c_ushort();self.call('pl_get_param',self.handle,pid,2,ct.byref(actual_type))
            details['actual_type']=actual_type.value
            read_type=typ
            if actual_type.value!=typ:
                # Observed on the registered XP WinSpec PVCAM 2.8.11 DLL:
                # CLEAR_CYCLES retains its UNS16 parameter ID, but ATTR_TYPE
                # reports INT16. Follow that reported value type for this exact
                # build/parameter only; no setters or alternative IDs are used.
                if name=='clear_cycles' and getattr(self,'version',None)==0x028b and actual_type.value==1:
                    read_type=1
                    details['compatibility']='Observed PVCAM 2.8.11 clear_cycles INT16 readback'
                else:
                    raise RuntimeError('PVCAM parameter type mismatch: %s (expected %d, actual %d, id 0x%08x)' %
                                       (name,typ,actual_type.value,pid))
            details['read_type']=read_type
            value={1:ct.c_short,6:ct.c_ushort,9:ct.c_int32}[read_type]()
            self.call('pl_get_param',self.handle,pid,0,ct.byref(value))
            details['value']=value.value
            if name=='clear_cycles' and value.value<0:
                raise RuntimeError('Negative PVCAM clear_cycles readback')
            return value.value
        except Exception as error:
            details['error']='%s: %s' % (type(error).__name__,error)
            raise

    def snapshot(self):
        values={}; self.unsupported_parameters=[];errors=[]
        for name in PARAMS:
            if name=='temp':continue
            try:values[name]=self.get(name)
            except UnsupportedParameter as error:
                if name in ('ser_size','par_size','bit_depth','temp_setpoint','exp_res','exposure_mode'):
                    errors.append('%s: %s' % (name,error))
                else:self.unsupported_parameters.append(name)
            except RecoveryRequired:raise
            except RuntimeError as error:
                # Gather all synchronous preflight failures before refusing any
                # setup/exposure. An uncertain native wait still stops at once.
                errors.append('%s: %s' % (name,error))
        if errors:raise RuntimeError('Native parameter preflight failed: '+'; '.join(errors))
        return values

    def cold(self):
        value=temperature_c(self.get('temp'))
        if value > -100.:raise RuntimeError('PVCAM temperature %.2f C is above -100 C; lock unavailable' % value)
        return value

    def setup(self,frames,exposure):
        if not self.sequence_initialized:
            self.call('pl_exp_init_seq');self.sequence_initialized=True
        region=Region(0,511,1,0,0,1);size=ct.c_uint32()
        self.call('pl_exp_setup_seq',self.handle,frames,1,ct.byref(region),0,exposure,ct.byref(size),timeout=30.)
        self.last_setup_native_s=self.last_call_s
        if self.get('exposure_mode')!=0 or self.get('exp_res')!=0:
            raise RuntimeError('PVCAM timed exposure mode/unit readback mismatch')
        return size.value

    def pin(self,size):
        self.size=size; self.buffer=ct.create_string_buffer(size)
        if not self.kernel.VirtualLock(ct.addressof(self.buffer),size):
            raise RuntimeError('VirtualLock failed; acquisition prohibited')
        self.pinned=True

    def start(self):
        self.call('pl_exp_start_seq',self.handle,self.buffer,timeout=30.)
        self.last_start_native_s=self.last_call_s

    def status(self):
        status=ct.c_short();count=ct.c_uint32()
        self.call('pl_exp_check_status',self.handle,ct.byref(status),ct.byref(count),timeout=3.)
        return status.value,count.value

    def raw(self):return ct.string_at(self.buffer,self.size)

    def release_buffer(self):
        """Only the caller that finished an idle sequence may release its buffer."""
        if self.unsafe:raise RecoveryRequired('Retain uncertain native buffer')
        if self.pinned:
            if not self.kernel.VirtualUnlock(ct.addressof(self.buffer),self.size):
                self.unsafe=True
                raise RecoveryRequired('VirtualUnlock failed; retain native buffer')
            self.pinned=False
        self.buffer=None

    def finish(self):
        try:self.call('pl_exp_finish_seq',self.handle,self.buffer,0)
        except BaseException:
            self.unsafe=True
            raise RecoveryRequired('finish_seq failed/stalled; retained native resources')

    def abort(self):
        try:self.call('pl_exp_abort',self.handle,1,timeout=3.)  # CCS_HALT, no shutter/clear change
        except BaseException:
            self.unsafe=True
            raise RecoveryRequired('Abort failed/stalled; retained native resources')

    def close(self):
        try:
            if self.sequence_initialized:self.call('pl_exp_uninit_seq')
            if self.opened:self.call('pl_cam_close',self.handle)
            if self.initialized:self.call('pl_pvcam_uninit')
            if self.pinned and not self.kernel.VirtualUnlock(ct.addressof(self.buffer),self.size):
                raise RuntimeError('VirtualUnlock failed')
            self.pinned=False
        except BaseException:
            self.unsafe=True
            raise RecoveryRequired('Native cleanup failed; recovery required')


def run_probe(sdk,output_dir,persist,expected):
    report=dict(status='running',frames=[],native_restore_ok=False,recovery_required=False,
        original_winspec_settings=expected,
        scope='PVCAM 2 raw frames at 500 ms, software mean; not WinSpec hardware accumulation; no optics/SMU',
        native_temperature_policy='Before/after only, <= -100 C; no native lock status assumed')
    original=None;configured=False;sequence_may_run=False;finished=False
    try:
        if hasattr(sdk,'open'):sdk.open()
        original=sdk.snapshot();report['original_native_settings']=original
        report['native_validation_scope']=sorted(original)
        report['unsupported_native_parameters']=getattr(sdk,'unsupported_parameters',[])
        for key,value in (('ser_size',512),('par_size',1),('bit_depth',16),('exp_res',0)):
            if original.get(key)!=value:raise RuntimeError('Required native '+key+' differs')
        if expected.get('accumulations')!=1 or expected.get('sequential_frames')!=1:
            raise RuntimeError('Restoration probe supports original one-frame/one-accumulation recipe only')
        if original['temp_setpoint']!=int(expected['temperature_setpoint_c']*100):
            raise RuntimeError('Native temperature setpoint changed on open; do not change cooling')
        if original.get('exposure_mode')!=0:raise RuntimeError('Original native mode is not known to be internally timed')
        restore_exposure=expected.get('exposure_ms')
        if (isinstance(restore_exposure,bool) or not isinstance(restore_exposure,(int,float)) or
            not 1<=restore_exposure<=65535 or int(restore_exposure)!=restore_exposure):
            raise RuntimeError('Cached WinSpec exposure must be an integer millisecond for this probe')
        restore_exposure=int(restore_exposure)
        report['temperature_before_c']=sdk.cold();persist(report)
        configured=True;started=clock();size=sdk.setup(2,500)
        report['setup_once_s']=clock()-started
        report['setup_native_s']=getattr(sdk,'last_setup_native_s',report['setup_once_s'])
        if size!=2048:raise RuntimeError('Two raw 512x1 uint16 frames require exactly 2048 bytes, got %d' % size)
        sdk.pin(size)
        for index in range(6):
            before=sdk.cold();point_started=clock();sequence_may_run=True
            started=clock();sdk.start();start_s=clock()-started
            deadline=point_started+30.
            while True:
                status,arrived=sdk.status()
                if status==4:raise RuntimeError('PVCAM READOUT_FAILED')
                if arrived>size:raise RuntimeError('PVCAM returned extra bytes')
                if status==3:
                    sequence_may_run=False
                    if arrived!=size:raise RuntimeError('PVCAM incomplete byte count')
                    break
                if status not in (1,2,5):raise RuntimeError('Unexpected PVCAM status %r' % status)
                if clock()>deadline:raise RuntimeError('PVCAM acquisition deadline exceeded')
                time.sleep(.002)
            after=sdk.cold();duration=clock()-point_started
            raw=sdk.raw()
            if len(raw)!=size:raise RuntimeError('Raw buffer length mismatch')
            archive=os.path.join(output_dir,'frame-%02d.raw' % index)
            write_raw(archive,raw)
            values=struct.unpack('<1024H',raw)
            frame=dict(index=index,warmup=index==0,start_call_s=start_s,
                start_native_s=getattr(sdk,'last_start_native_s',start_s),capture_s=duration,
                temperature_before_c=before,temperature_after_c=after,raw_archive=archive,
                sha256=hashlib.sha256(raw).hexdigest(),bytes=size,frames=2,width=512,height=1,
                counts_per_frame=[list(values[:512]),list(values[512:])],
                mean_counts_per_exposure=sum(values)/1024.,archive_verified=False)
            with open(archive,'rb') as handle:
                if hashlib.sha256(handle.read()).hexdigest()!=frame['sha256']:
                    raise RuntimeError('Raw archive verification failed')
            frame['archive_verified']=True;report['frames'].append(frame);persist(report)
            print('%s: native Start %.6f s; capture %.6f s' %
                  ('warmup' if index==0 else 'sample %d' % index,frame['start_native_s'],duration))
        sdk.finish();finished=True
        for frame in report['frames']:
            with open(frame['raw_archive'],'rb') as handle:
                if hashlib.sha256(handle.read()).hexdigest()!=frame['sha256']:
                    raise RuntimeError('Archived frame changed')
        frames=report['frames'][1:]
        report['summary']=dict(start_native_median_s=median([f['start_native_s'] for f in frames]),
                               capture_median_s=median([f['capture_s'] for f in frames]))
        report['status']='complete'
    except BaseException as error:
        report['status']='failed';report['error']='%s: %s' % (type(error).__name__,error)
        if isinstance(error,RecoveryRequired):sdk.unsafe=True
        if configured and not sdk.unsafe and (sequence_may_run or not finished):
            try:sdk.abort();sequence_may_run=False
            except BaseException as abort_error:
                sdk.unsafe=True;report['abort_error']=str(abort_error)
    finally:
        if sdk.unsafe:
            report['recovery_required']=True
            report['restore_error']='Native state uncertain; no restoration/finish/close/unpin'
        else:
            try:
                if configured:
                    sdk.setup(1,restore_exposure)
                    report['timed_restore_setup_accepted']=dict(exposure_ms=restore_exposure,raw_frames=1)
                    report['timed_exposure_restore_verification']='Cached WinSpec recipe accepted by setup_seq; no independent timed exposure getter'
                    current=sdk.snapshot();report['restored_native_settings']=current
                    if current!=original:raise RuntimeError('Native configuration restoration mismatch')
                    report['temperature_after_c']=sdk.cold()
                report['native_restore_ok']=True
            except BaseException as error:
                report['status']='failed';report['restore_error']=str(error)
                if isinstance(error,RecoveryRequired):sdk.unsafe=True
            if not sdk.unsafe:
                try:sdk.close()
                except BaseException as error:
                    sdk.unsafe=True;report['cleanup_error']=str(error)
            report['recovery_required']=bool(sdk.unsafe)
        if report['recovery_required'] or not report['native_restore_ok']:report['status']='failed'
        report['winspec_recipe_recheck_required']=True
        persist(report)
    return report


def hold_for_recovery(sdk,lease):
    # Keep strong references even when report storage or console output failed.
    try:print('RECOVERY REQUIRED: process retains DLL/buffer/port. Do not restart WinSpec/bridge.')
    except BaseException:pass
    while True:
        try:time.sleep(1)
        except BaseException:pass


def main():
    print('Probe runtime: '+sys.version.replace('\n',' '))
    if os.name!='nt' or ct.sizeof(ct.c_void_p)!=4:
        raise RuntimeError('Run with XP C:\\Python27\\python.exe (32 bit)')
    # Fail before loading/initializing PVCAM if WinSpec still owns the camera.
    print('Checking XP WinSpec process and camera-server port')
    info=subprocess.STARTUPINFO();info.dwFlags|=subprocess.STARTF_USESHOWWINDOW
    processes=subprocess.check_output(['tasklist','/FO','CSV','/NH'],startupinfo=info)
    if b'"winspec.exe"' in processes.lower():
        raise RuntimeError('Close XP WinSpec window and camera server first; keep controller powered')
    lease=reserve_bridge_port()
    sdk=None;environment=None
    try:
        folder=os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(folder,'preflight.json'),'r') as handle:preflight=json.load(handle)
        if time.time()-preflight['saved_unix']>3600:
            raise RuntimeError('Preflight snapshot older than one hour; refresh before testing')
        dll_path,dll_source=find_pvcam_dll(folder)
        print('Loading DLL (%s): %s' % (dll_source,dll_path))
        environment=WinSpecDriverEnvironment(dll_path);environment.activate()
        sdk=NativePVCAM(dll_path)
        stamp=time.strftime('%Y%m%d-%H%M%S')+'-%d' % os.getpid()
        output=os.path.join(r'C:\WinSpecRemote','pvcam-probe-'+stamp);os.makedirs(output)
        shared=os.path.join(folder,'pvcam-result-'+stamp+'.json')
        def persist(report):
            report['dll_path']=sdk.dll_path
            report['dll_source']=dll_source
            report['driver_environment']=environment.details
            report['pvcam_version_raw']=getattr(sdk,'version',None)
            report['pvcam_version']=getattr(sdk,'version_text',None)
            report['native_parameter_diagnostics']=getattr(sdk,'parameter_diagnostics',{})
            report['camera_name']=getattr(sdk,'camera_name',None)
            report['previous_v12_summary']=preflight['v12_summary']
            persist_reports(report,output,shared)
        print('Initializing PVCAM; local report directory: '+output)
        report=run_probe(sdk,output,persist,preflight['settings'])
        print(json.dumps(dict((k,report.get(k)) for k in
            ('status','summary','setup_once_s','setup_native_s','native_restore_ok','recovery_required',
             'error','restore_error','cleanup_error')),indent=2))
        print('Shared report: '+shared)
        return 0 if report['status']=='complete' else 1
    finally:
        if sdk is not None and sdk.unsafe:hold_for_recovery(sdk,lease)
        else:
            try:
                if environment is not None:environment.restore()
            finally:lease.close()


if __name__=='__main__':
    try:sys.exit(main())
    except Exception as error:
        import traceback
        print('PVCAM PROBE ABORTED: '+str(error));traceback.print_exc();sys.exit(1)
