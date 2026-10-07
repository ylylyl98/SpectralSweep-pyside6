"""Exercise XP dispatch without importing Windows XP COM libraries."""
import ast
import json
import math
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest


def dispatch_namespace():
    source = Path('tools/winspec/camera_server.py').read_text()
    tree = ast.parse(source)
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'execute')
    calls = []
    namespace = dict(CAMERA_LOCK=threading.Lock(), create_experiment=lambda: object(),
                     acquire=lambda exp: (calls.append('guarded') or ({}, b'')),
                     read_settings=lambda exp: {}, read_temperature_status=lambda exp: {},
                     TEMPERATURE_GUARD_VERSION=1, ACQUISITION_SETTINGS_VERSION=1,
                     ProtocolError=RuntimeError)
    bridge_functions(namespace,'execute')
    return namespace, calls


def test_guarded_command_is_dispatched():
    ns, calls = dispatch_namespace()
    ns['execute']('ACQUIRE_GUARDED', {})
    assert calls == ['guarded']


@pytest.mark.parametrize('command', ['GET_SETTINGS', 'GET_STATUS'])
def test_status_advertises_guard_version(command):
    ns, _ = dispatch_namespace()
    metadata, _ = ns['execute'](command, {})
    assert metadata['temperature_guard_version'] == 1


def bridge_functions(namespace, *names):
    """Load actual bridge functions with fake dependencies, without COM imports."""
    tree = ast.parse(Path('tools/winspec/camera_server.py').read_text())
    from tools.winspec.acquisition_owner import Owner
    from tools.winspec.stop_owner_guard import confirmed_stop as owned_stop
    namespace.setdefault('Owner',Owner);namespace.setdefault('owned_stop',owned_stop)
    namespace.setdefault('START_ACCELERATOR',None)
    namespace.setdefault('ACQUISITION_STATE_LOCK',threading.Lock())
    namespace.setdefault('TRANSFER_STATE',SimpleNamespace())
    for key in ('OWNER_PENDING','TRANSFER_PENDING','CLEANUP_FAILED','STOP_REQUESTED','STOP_IN_FLIGHT','ACQUISITION_ACTIVE','NATIVE_START_COMMITTED','TEMPERATURE_MONITOR_UNHEALTHY'):
        namespace.setdefault(key,threading.Event())
    helpers={'confirmed_stop','require_idle_owner','get_accelerator','mark_native_start','mark_native_complete','owner_recovery_required','rejected_capture_stop','wait_for_stop_completion'}
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in set(names)|helpers]
    assert set(names)<=set(n.name for n in functions)
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<XP lifecycle>', 'exec'), namespace)
    return namespace


@pytest.mark.parametrize('reuse', [False, True])
def test_settings_reads_each_parameter_once_and_reuses_boundary_temperature(reuse):
    calls = []
    values = {'EXP_EXPOSURE': 800, 'EXP_ACTUAL_TEMP': -101, 'EXP_TEMP_STATUS': True}
    def read(exp, key):
        calls.append(key)
        return values[key]
    ns = bridge_functions(dict(
        PARAMETERS={'exposure_ms': ('EXP_EXPOSURE', float, float)},
        READ_ONLY_PARAMETERS={'actual_temperature_c': ('EXP_ACTUAL_TEMP', float),
                              'temperature_locked': ('EXP_TEMP_STATUS', bool)},
        const=lambda key: key, read_parameter=read,
        is_readable=lambda exp, key: read(exp, key) is not None,
        read_roi=lambda exp: {}, ACQUISITION_ACTIVE=threading.Event(),
    ), 'read_settings')
    kwargs = {'temperature_status': {'actual_temperature_c': -102., 'temperature_locked': False}} if reuse else {}
    result = ns['read_settings'](object(), **kwargs)
    assert calls == (['EXP_EXPOSURE'] if reuse else ['EXP_EXPOSURE', 'EXP_ACTUAL_TEMP', 'EXP_TEMP_STATUS'])
    assert result['actual_temperature_c'] == (-102 if reuse else -101)
    assert result['temperature_locked'] is (not reuse)


def test_watchdog_uses_total_deadline_not_temperature_age():
    now = [10.]
    stops = []

    class AdvancingEvent:
        def wait(self, seconds):
            now[0] += seconds
            assert now[0] < 21, 'watchdog failed to enforce heartbeat timeout'
            return False

    result = {'guard': SimpleNamespace(last=10.)}
    timed_out = threading.Event()
    ns = bridge_functions(dict(
        time=SimpleNamespace(monotonic=lambda: now[0]),
        pythoncom=SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None),
        create_experiment=lambda: SimpleNamespace(Stop=lambda: stops.append(now[0]) or True),
        com_return_value=lambda value, name: value, server_log=lambda *args: None,
    ), 'acquisition_watchdog')
    ns['acquisition_watchdog'](10, AdvancingEvent(), timed_out, result)
    assert timed_out.is_set()
    assert len(stops) == 1 and 10 <= stops[0] - 10 < 10.2
    assert 'error' not in result


@pytest.mark.parametrize('failure', [None, 'preflight', 'wait', 'save', 'watchdog', 'post_temperature',
                                   'display_set', 'display_restore', 'unsupported_display', 'display_off'])
@pytest.mark.parametrize('compact', [False, True])
def test_serial_acquisition_lifecycle_keeps_documents_and_never_calls_blocking_wait(failure, compact):
    from tools.winspec.temperature_guard import TemperatureGuard
    if not compact and failure in ('display_set', 'display_restore', 'unsupported_display', 'display_off'):
        pytest.skip('Display control applies only to managed acquisition')
    calls=[]; release=threading.Event(); threads=[]; display=[0 if failure=='display_off' else -1]; writes=[]
    class BoundedThread(threading.Thread):
        def __init__(self,*a,**kw):
            super().__init__(*a,**kw);threads.append(self)
        def join(self,timeout=None):super().join(.02)
    def watchdog(timeout,finished,timed_out,result):
        if failure=='watchdog':release.wait(2)
        else:finished.wait(2)
    def save(*args):
        calls.append('save');return failure!='save'
    doc=SimpleNamespace(SaveAs=save,Close=lambda:pytest.fail('must not open save prompt'))
    def start():
        assert len(threads)==1
        if compact and failure != 'unsupported_display':
            assert display[0] == 0, 'Managed capture must hide data window before Start'
        calls.append('start');return doc
    def set_parameter(key, value):
        writes.append((key,value))
        if key == 'EXP_BSHOWWINDOW':
            if failure == 'display_set' and value == 0:
                return 1
            if failure == 'display_restore' and value != 0:
                return 1
            display[0] = value
        return 0
    def get_parameter(key):
        if failure == 'unsupported_display':
            raise RuntimeError('Unsupported display parameter')
        return (display[0],0)
    exp=SimpleNamespace(SetParam=set_parameter,GetParam=get_parameter,
                        Start2=start,Stop=lambda:calls.append('stop') or True,
                        WaitForExperiment=lambda:pytest.fail('blocking wait called'))
    def wait(exp,doc,guard,*args):
        calls.append('wait')
        if failure=='wait':raise RuntimeError('rejected frame')
    unhealthy=threading.Event()
    def temperature(e):
        calls.append('temperature')
        return dict(actual_temperature_c=-90. if failure=='post_temperature' and 'wait' in calls else -101.,temperature_locked=False)
    def settings(e, **kw):
        assert not compact, 'compact capture must not read full settings'
        return dict(kw.get('temperature_status', {}))
    def preflight(e, expected, temperature):
        if failure == 'preflight':
            raise RuntimeError('settings changed')
        return dict(sequential_frames=1)
    def parameter(e, key):
        if compact and key != 'EXP_AUTOSAVE':
            pytest.fail('managed capture reread a camera configuration parameter')
        if failure == 'preflight' and key == 'EXP_SEQUENTS':
            raise RuntimeError('settings changed')
        return 1
    ns=bridge_functions(dict(time=time,json=json,threading=SimpleNamespace(Event=threading.Event,Thread=BoundedThread),
        disable_acquisition_autosave=lambda e:None,TemperatureGuard=TemperatureGuard,validate_temperature=lambda s:-101.,
        read_temperature_status=temperature,
        read_parameter=parameter,const=lambda s:s,
        unique_spe_path=lambda:'test.spe',acquisition_watchdog_timeout=lambda e, **kw:30.,
        start_new_document=lambda e, **kw:e.Start2(),wait_for_guarded_frame=wait,acquisition_watchdog=watchdog,
        com_return_value=lambda v,n:v[0] if isinstance(v,(tuple,list)) else v,server_log=lambda *a:None,DT_SPE=1,
        read_spe_frames=lambda p:(512,1,1,3,b'data',{}),read_settings=settings,
        read_acquisition_settings=lambda e, **kw:pytest.fail('managed capture queried acquisition settings'),
        validate_acquisition_request=lambda expected: preflight(None, expected, None),
        settings_from_spe=lambda expected, hardware, width, height, frames, temperature:dict(temperature),
        TEMPERATURE_MONITOR_UNHEALTHY=unhealthy,STOP_REQUESTED=threading.Event(),
        ACQUISITION_ACTIVE=threading.Event(),CLEANUP_FAILED=threading.Event(),
        TRANSFER_STATE=SimpleNamespace(),TRANSFER_PENDING=threading.Event()),
        'get_param','read_acquisition_display','set_acquisition_display','acquire_active','acquire')
    try:
        if failure and failure not in ('unsupported_display','display_off'):
            with pytest.raises(RuntimeError):ns['acquire'](exp, compact_settings=compact)
        else:
            metadata,data=ns['acquire'](exp, compact_settings=compact)
            assert data==b'data' and metadata['temperature_guard']['passed']
            assert calls[:5]==['temperature','start','wait','temperature','save']
            assert metadata['settings']['actual_temperature_c'] == -101.
            assert calls.count('temperature') == 2
            assert metadata['settings_scope'] == ('configured_plus_spe' if compact else 'full')
            assert metadata['data_window_hidden'] is (compact and failure != 'unsupported_display')
            assert display[0] == (0 if failure == 'display_off' else -1)
            timing = metadata['bridge_timing_s']
            expected_phases = {'prepare', 'start_document', 'exposure_wait', 'after_exposure',
                               'save_spe', 'read_spe', 'settings_after', 'total_before_transfer'}
            if compact:
                expected_phases.update(('display_prepare','display_restore'))
            assert expected_phases == timing.keys()
            assert all(value >= 0 for value in timing.values())
        if failure in ('wait','post_temperature'):assert 'save' not in calls
        if failure == 'preflight':assert 'start' not in calls and 'save' not in calls
        if failure=='watchdog':
            assert unhealthy.is_set()
            assert writes == ([('EXP_BSHOWWINDOW',0)] if compact else [])
            with pytest.raises(RuntimeError,match='restart'):ns['acquire'](exp)
        if failure == 'display_set':
            assert 'start' not in calls
            assert writes == [('EXP_BSHOWWINDOW',0),('EXP_BSHOWWINDOW',-1),('EXP_AUTOSAVE',True)]
        if failure == 'display_restore':
            assert unhealthy.is_set()
            assert writes[-1] == ('EXP_AUTOSAVE',True), 'Auto-save must restore independently'
            assert not ns['TRANSFER_PENDING'].is_set(), 'Rejected data must not be acknowledged'
            with pytest.raises(RuntimeError,match='restart'):ns['acquire'](exp)
        if failure in ('unsupported_display','display_off'):
            assert not any(key == 'EXP_BSHOWWINDOW' for key,value in writes)
        assert not ns['ACQUISITION_ACTIVE'].is_set()
        if failure not in ('watchdog','display_restore'):
            assert display[0] == (0 if failure == 'display_off' else -1)
    finally:
        release.set()
        for t in threads:threading.Thread.join(t,2)


@pytest.mark.parametrize('duration', [3, 300])
def test_wait_never_queries_temperature_or_document(duration):
    calls=[]
    exp=SimpleNamespace(WaitForExperiment=lambda:calls.append(duration) or True)
    ns=bridge_functions(dict(STOP_REQUESTED=threading.Event(),com_return_value=lambda v,n:v),
        'wait_for_guarded_frame')
    guard=SimpleNamespace(check=lambda:pytest.fail('temperature read during exposure'))
    ns['wait_for_guarded_frame'](exp,object(),guard,threading.Event(),{},1)
    assert calls==[duration]


def test_serial_wait_rejects_stop_before_accepting_frame():
    stopped=threading.Event();stopped.set()
    ns=bridge_functions(dict(STOP_REQUESTED=stopped), 'wait_for_guarded_frame')
    with pytest.raises(RuntimeError,match='stopped'):
        ns['wait_for_guarded_frame'](None,None,None,threading.Event(),{},1)


def test_start_passes_a_new_empty_document_each_time():
    created=[];received=[]
    def create():
        doc=object();created.append(doc);return doc
    exp=SimpleNamespace(Start=lambda doc:received.append(doc) or True,
                        Start2=lambda:pytest.fail('Start2 reuses existing window'))
    ns=bridge_functions(dict(create_document=create,com_return_value=lambda v,n:v), 'start_new_document')
    assert ns['start_new_document'](exp) is created[0]
    assert ns['start_new_document'](exp) is created[1]
    assert created[0] is not created[1] and received==created


def test_rejected_start_does_not_return_document():
    ns=bridge_functions(dict(create_document=lambda:object(),com_return_value=lambda v,n:v), 'start_new_document')
    with pytest.raises(RuntimeError,match='start'):
        ns['start_new_document'](SimpleNamespace(Start=lambda doc:False))


def test_start_disables_automatic_saving_before_acquisition():
    calls=[]
    exp=SimpleNamespace(SetParam=lambda key,v:calls.append((key,v)) or 0)
    ns=bridge_functions(dict(const=lambda n:n,read_parameter=lambda e,k:False,
                            com_return_value=lambda v,n:v),'disable_acquisition_autosave')
    ns['disable_acquisition_autosave'](exp)
    assert calls==[('EXP_AUTOSAVE',False)]


def test_start_uses_returned_byref_document():
    empty=object()
    actual=SimpleNamespace(SaveAs=lambda *a:True,Close=lambda:None)
    ns=bridge_functions(dict(create_document=lambda:empty,com_return_value=lambda v,n:v[0],
        COM_TUPLE_METHODS_LOGGED=set(),server_log=lambda *a:None), 'start_new_document','start2_document')
    assert ns['start_new_document'](SimpleNamespace(Start=lambda doc:(True,actual))) is actual


def test_experiment_activation_is_not_repeated_for_dynamic_wrapper():
    activations = []
    interface = object()
    def generate(progid):
        activations.append(progid)
        return SimpleNamespace(_oleobj_=interface)
    def dynamic_dispatch(value):
        if isinstance(value, str):
            activations.append(value)
            return SimpleNamespace(_oleobj_=object())
        return SimpleNamespace(_oleobj_=value)
    client = SimpleNamespace(gencache=SimpleNamespace(EnsureDispatch=generate),
                             dynamic=SimpleNamespace(Dispatch=dynamic_dispatch))
    ns = bridge_functions({'win32com': SimpleNamespace(client=client)}, 'create_experiment')
    experiment = ns['create_experiment']()
    assert experiment._oleobj_ is interface
    assert activations == ['WinX32.ExpSetup']


def test_start_timing_distinguishes_document_creation_and_hardware_start():
    now = [10.]
    empty = object()
    actual = SimpleNamespace(SaveAs=lambda *a:True, Close=lambda:True)
    def create():
        now[0] += .05
        return empty
    def start(document):
        assert document is empty
        now[0] += .65
        return True, actual
    ns = bridge_functions(dict(create_document=create, com_return_value=lambda value,name:value[0],
                               time=SimpleNamespace(monotonic=lambda:now[0]),
                               COM_TUPLE_METHODS_LOGGED=set(), server_log=lambda *a:None),
                          'start_new_document', 'start2_document')
    timings = {}
    assert ns['start_new_document'](SimpleNamespace(Start=start), timings=timings) is actual
    assert timings['create_document'] == pytest.approx(.05)
    assert timings['start_experiment'] == pytest.approx(.65)


def test_failed_start_is_not_retried_by_timing_instrumentation():
    calls = []
    def start(document):
        calls.append(document)
        return False
    ns = bridge_functions(dict(create_document=lambda:object(), com_return_value=lambda value,name:value,
                               time=SimpleNamespace(monotonic=lambda:1.)), 'start_new_document')
    with pytest.raises(RuntimeError, match='start'):
        ns['start_new_document'](SimpleNamespace(Start=start), timings={})
    assert len(calls) == 1


@pytest.mark.parametrize('reuse', [False, True])
def test_compact_settings_read_live_safety_fields_without_optional_queries(reuse):
    calls = []
    values = dict(EXP_EXPOSURE=.8, EXP_ACCUMS=4, EXP_SEQUENTS=1,
                  EXP_TIMING_MODE=1, EXP_ACTUAL_TEMP=-101., EXP_TEMP_STATUS=True,
                  EXP_XDIMDET=512, EXP_YDIMDET=1, EXP_XDIM=512, EXP_YDIM=1,
                  EXP_USEROI=False, EXP_READOUT_TIME=.5, EXP_ADC_RATE=11, EXP_GAIN=2)
    def read(exp, key):
        calls.append(key)
        return values[key]
    ns = bridge_functions(dict(const=lambda key: key, read_parameter=read,
                               ACQUISITION_ACTIVE=threading.Event()), 'read_acquisition_settings')
    temperature = {'actual_temperature_c': -102., 'temperature_locked': False} if reuse else None
    settings = ns['read_acquisition_settings'](object(), temperature_status=temperature)
    assert settings['exposure_ms'] == 800.
    assert settings['accumulations'] == 4 and settings['roi_enabled'] is False
    assert settings['detector_width'] == settings['output_width'] == 512
    assert settings['detector_height'] == settings['output_height'] == 1
    assert settings['actual_temperature_c'] == (-102. if reuse else -101.)
    assert settings['temperature_locked'] is (not reuse)
    assert set(calls) <= set(values) and len(calls) == len(set(calls))
    assert ('EXP_ACTUAL_TEMP' in calls) is (not reuse)


def test_compact_settings_missing_geometry_fails_closed():
    def read(exp, key):
        if key == 'EXP_XDIMDET':
            raise RuntimeError('geometry unavailable')
        return 1
    ns = bridge_functions(dict(const=lambda key: key, read_parameter=read,
                               ACQUISITION_ACTIVE=threading.Event()), 'read_acquisition_settings')
    with pytest.raises(RuntimeError, match='geometry unavailable'):
        ns['read_acquisition_settings'](object())


def test_compact_status_dispatch_and_busy_response_do_not_touch_owned_camera():
    ns, _ = dispatch_namespace()
    ns['ACQUISITION_SETTINGS_VERSION'] = 1
    ns['read_acquisition_settings'] = lambda exp: {'exposure_ms': 800.}
    reply, _ = ns['execute']('GET_ACQUISITION_SETTINGS', {})
    assert reply['settings']['exposure_ms'] == 800.
    assert reply['acquisition_settings_version'] == 1
    ns['CAMERA_LOCK'].acquire()
    try:
        ns['create_experiment'] = lambda: pytest.fail('busy request must not touch COM')
        reply, _ = ns['execute']('GET_ACQUISITION_SETTINGS', {})
        assert reply['camera_busy'] is True
    finally:
        ns['CAMERA_LOCK'].release()


@pytest.mark.parametrize('changed', [None, 'exposure_ms', 'accumulations', 'timing_mode', 'roi_enabled', 'output_width'])
def test_managed_request_validation_requires_no_com_queries(changed):
    expected = dict(exposure_ms=800., accumulations=4, sequential_frames=1, timing_mode=1,
                    detector_width=512, detector_height=1, output_width=512, output_height=1,
                    roi_enabled=False)
    if changed:
        expected[changed] = True if changed == 'roi_enabled' else 0
    ns = bridge_functions(dict(math=math), 'validate_acquisition_request')
    if changed:
        with pytest.raises(RuntimeError):
            ns['validate_acquisition_request'](expected)
    else:
        assert ns['validate_acquisition_request'](expected) == expected


def test_compact_capture_dispatch_forwards_expected_settings():
    ns, _ = dispatch_namespace()
    calls = []
    ns['acquire'] = lambda exp, **kw: (calls.append(kw) or ({}, b''))
    ns['execute']('ACQUIRE_GUARDED', {'settings_mode': 'managed',
                                     'expected_settings': {'accumulations': 4}})
    assert calls == [{'compact_settings': True, 'expected_settings': {'accumulations': 4},'acceleration':None}]


def test_watchdog_uses_verified_requested_values_without_duplicate_com_reads():
    ns = bridge_functions(dict(MIN_ACQUISITION_WATCHDOG_S=30., MAX_ACQUISITION_WATCHDOG_S=600.,
                               read_parameter=lambda *a: pytest.fail('duplicate COM read'),
                               const=lambda key: key), 'acquisition_watchdog_timeout')
    timeout = ns['acquisition_watchdog_timeout'](object(), settings=dict(
        exposure_ms=20000., accumulations=4, sequential_frames=1, readout_time_s=.5, timing_mode=1))
    assert timeout == pytest.approx((20. * 4 + .5) * 1.5 + 15.)


@pytest.mark.parametrize('changed', [None, 'exposure_ms', 'accumulations', 'adc_rate'])
def test_spe_settings_validate_actual_frame_without_com(changed):
    expected = dict(exposure_ms=800., accumulations=4, sequential_frames=1, timing_mode=1,
                    detector_width=512, detector_height=1, output_width=512, output_height=1,
                    roi_enabled=False, adc_rate=11)
    hardware = dict(exposure_ms=800., accumulations=4, adc_rate=11)
    if changed:
        hardware[changed] = 2
    ns = bridge_functions(dict(math=math), 'settings_from_spe')
    if changed:
        with pytest.raises(RuntimeError, match='SPE'):
            ns['settings_from_spe'](expected, hardware, 512, 1, 1, {})
    else:
        result = ns['settings_from_spe'](expected, hardware, 512, 1, 1,
                                       {'actual_temperature_c': -101., 'temperature_locked': True})
        assert result['accumulations'] == 4 and result['exposure_ms'] == 800.
        assert result['actual_temperature_c'] == -101.


def test_spe_header_reads_exposure_and_accumulations_from_frame(tmp_path):
    import struct
    header = bytearray(4100)
    struct.pack_into('<f', header, 10, .8)
    struct.pack_into('<H', header, 42, 512)
    struct.pack_into('<h', header, 108, 3)
    struct.pack_into('<H', header, 656, 1)
    struct.pack_into('<l', header, 668, 4)
    struct.pack_into('<I', header, 1422, 4)
    struct.pack_into('<l', header, 1446, 1)
    path = tmp_path/'frame.spe'
    path.write_bytes(header + bytes(1024))
    ns = bridge_functions(dict(struct=struct, SPE_HEADER_SIZE=4100, MAX_FRAME_BYTES=4096), 'read_spe_frames')
    width, height, frames, datatype, raw, hardware = ns['read_spe_frames'](str(path))
    assert (width, height, frames, datatype, len(raw)) == (512, 1, 1, 3, 1024)
    assert hardware['exposure_ms'] == pytest.approx(800., rel=1e-6)
    assert hardware['accumulations'] == 4


def test_spe_accumulation_comparison_is_exact_even_for_large_counts():
    ns = bridge_functions(dict(math=math), 'settings_from_spe')
    with pytest.raises(RuntimeError, match='SPE accumulation'):
        ns['settings_from_spe']({'exposure_ms': 800., 'accumulations': 1000000},
                                {'exposure_ms': 800., 'accumulations': 1000001}, 512, 1, 1, {})
