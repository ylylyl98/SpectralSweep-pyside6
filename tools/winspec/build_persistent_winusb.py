"""Build a separate pinned persistent candidate and run fake-transport tests only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SOURCE = HERE/'persistent'


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def portable_device_source(source):
    marker = '#include <wchar.h>'
    assert source.count(marker) == 1
    source = source.replace(marker, marker+'\n#include "portable_binding.h"\n'
                            '#undef WS_A010_INSTANCE\n#define WS_A010_INSTANCE ws_bound_instance()')
    marker = 'SetLastError(0);list=a->devices(&dedicated_guid,NULL,NULL,0x12);'
    assert source.count(marker) == 2
    return source.replace(marker, 'if(!ws_bound_instance()[0])return fail(ERROR_INVALID_PARAMETER);'+marker)


def build(baseline_root, output, *, portable=False):
    usb, output = Path(baseline_root).resolve(), Path(output).resolve()
    acq = usb/'winusb_acquisition'
    frozen = acq/'build/physical_freerun_frozen'
    raw = acq/'build/rawdiag001'
    assert sha(frozen/'evidence.json') == 'b1c53163206e8e6d40d6f406c0e1cc735338c4cb403480caf7229f03ccdf611b'
    assert sha(raw/'evidence.json') == '66ddac044398630797eef650544f4dea99dd5a26cc5d16a75e2c0050565d22e1'
    baseline = json.loads((frozen/'evidence.json').read_text())
    raw_evidence = json.loads((raw/'evidence.json').read_text())
    for mapping in (baseline['sources'], raw_evidence['sources']):
        for path, digest in mapping.items(): assert sha(path) == digest, path
    for name, digest in raw_evidence['artifacts'].items(): assert sha(raw/name) == digest, name
    sys.path.insert(0, str(acq))
    import build_physical_native as base
    cc = [str(base.CC), '-m32', '-Wall', '-Werror', '-I', str(acq), '-I', str(usb/'winusb_session'), '-I', str(SOURCE)]
    if portable:
        cc.append('-DWS_PORTABLE_BINDING=1')
        # The physical helper is compiled but never executed by this builder.
        # Offline lifecycle tests still need an explicit process binding.
        os.environ['SPECTRALSWEEP_ST133_INSTANCE'] = r'USB\VID_0BD7&PID_A010\9&NEWPC&0&3'
    output.mkdir(parents=True, exist_ok=False)
    report = {'passed': False, 'hardware_calls': 0, 'physical_mode_executed': False, 'runs': [],
              'sources': {str(p): sha(p) for p in SOURCE.iterdir() if p.is_file()},
              'baseline_manifest_sha256': sha(frozen/'evidence.json')}
    report['sources'][str(Path(__file__).resolve())] = sha(__file__)
    def run(name, command):
        result = subprocess.run(list(map(str, command)), capture_output=True, text=True)
        (output/(name+'.txt')).write_text(result.stdout+result.stderr, encoding='utf-8')
        report['runs'].append({'name': name, 'exit_code': result.returncode})
        print(name, result.returncode, result.stdout[-700:], result.stderr[-1600:], flush=True)
        assert result.returncode == 0, name
    core = [SOURCE/'legacy.c', SOURCE/'frame.c', SOURCE/'session_reuse.c',
            acq/'frame.c', acq/'acquisition_control.c', usb/'control_eeprom.c', usb/'controller_image.c']
    try:
        device_source = usb/'winusb_session/device_open.c'
        if portable:
            device_source = output/'portable_device_open.c'
            device_source.write_text(portable_device_source((usb/'winusb_session/device_open.c').read_text()))
            run('portable-binding-build', cc+['-o', output/'binding-test.exe', SOURCE/'portable_binding_test.c'])
            for number, (value, expected) in enumerate([
                (r'usb\vid_0bd7&pid_a010\9&newpc&0&3', r'USB\VID_0BD7&PID_A010\9&NEWPC&0&3'),
                ('missing', 'reject'), (r'USB\VID_0BD7&PID_A026\OTHER', 'reject'),
                (r'USB\VID_0BD7&PID_A010\*', 'reject'), (r'USB\VID_0BD7&PID_A010\a\b', 'reject'),
                ('USB\\VID_0BD7&PID_A010\\'+'X'*201, 'reject'),
            ]):
                run(f'portable-binding-{number}', [output/'binding-test.exe', value, expected])
            fixture = (usb/'winusb_session/device_open_test.c').read_text().replace(
                '#include "device_open_api.h"', '#include "device_open_api.h"\n'
                '#undef WS_A010_INSTANCE\n#define WS_A010_INSTANCE "USB\\\\VID_0BD7&PID_A010\\\\9&NEWPC&0&3"')
            (output/'portable_opener_test.c').write_text(fixture)
            run('portable-opener-build', cc+['-o', output/'opener-test.exe', output/'portable_opener_test.c',
                device_source, usb/'winusb_session/winusb_provider.c', usb/'winusb_session/session.c'])
            run('portable-opener', [output/'opener-test.exe'])
        # Patch only this private generated copy. The frozen source stays intact.
        native = (acq/'physical_native.c').read_text()
        old = 'if(((DoubleSetter)FIELD(p->object,0x20))(p->object,0x7f,0.1)!=1||!info_ok(p,"exposure_100ms"))return FALSE;'
        assert native.count(old) == 1
        native = native.replace(old, '''{
   double expected=(double)ws_exposure_ms/1000.0,observed=-1.0;
   if(((DoubleSetter)FIELD(p->object,0x20))(p->object,0x7f,expected)!=1||!info_ok(p,"exposure_configured"))return FALSE;
   if(((Three)FIELD(p->object,0x30))(p->object,0x7f,(DWORD)&observed)!=1||observed!=expected)return failure(ERROR_INVALID_DATA);
   log_line(p,"exposure_readback_before_init_us",(DWORD)(observed*1000000.0+0.5),0);
  }''')
        old = 'if(result!=1||!p->info.initialized||FIELD(p->port,0x374)!=1024'
        assert native.count(old) == 1
        native = native.replace(old, '''{
   double expected=(double)ws_exposure_ms/1000.0,observed=-1.0;
   if(((Three)FIELD(p->object,0x30))(p->object,0x7f,(DWORD)&observed)!=1||observed!=expected)return failure(ERROR_INVALID_DATA);
   log_line(p,"exposure_readback_after_init_us",(DWORD)(observed*1000000.0+0.5),0);
  }
  '''+old)
        old = 'DWORD event_count,event_closed,native_fault;'
        assert native.count(old) == 1
        native = native.replace(old, 'DWORD event_count,event_closed,event_live,native_fault;')
        begin = native.index('static BOOL is_event(')
        end = native.index('static HANDLE WINAPI database_open', begin)
        native = native[:begin]+'#include "persistent_events.h"\n'+native[end:]
        old = 'if(p->event_count!=p->event_closed)'
        assert native.count(old) == 1
        native = native.replace(old, 'if(p->event_live||p->event_count!=p->event_closed)')
        old = 'static void log_line(physical*p,const char*stage,DWORD value,DWORD error){'
        assert native.count(old) == 1
        native = native.replace(old, '#include "persistent_log.h"\n'+old)
        old = 'char line[512];DWORD n,w,saved=GetLastError();'
        assert native.count(old) == 1
        native = native.replace(old, 'char line[512];DWORD n,saved=GetLastError();')
        old = 'fputs(line,stdout);fflush(stdout);n=(DWORD)length;if(p->log&&(!WriteFile(p->log,line,n,&w,NULL)||w!=n)&&!p->native_fault)p->native_fault=ERROR_WRITE_FAULT;'
        assert native.count(old) == 1
        native = native.replace(old, '''
#if WS_INJECTED_TRANSPORT
 if(injected_mode==24&&!strcmp(stage,"start_call_before"))persistent_log.fault=ERROR_DISK_FULL;
#endif
 n=(DWORD)length;if(p->log&&!pl_write(line,n)&&!p->native_fault)p->native_fault=ERROR_WRITE_FAULT;''')
        old = 'if(p->started)return failure(ERROR_ALREADY_EXISTS);p->started=TRUE;log_line(p,"start_call_before",0,0);'
        assert native.count(old) == 1
        native = native.replace(old, 'if(p->started)return failure(ERROR_ALREADY_EXISTS);log_line(p,"start_call_before",0,0);if(p->native_fault)return failure(p->native_fault);p->started=TRUE;')
        (output/'persistent_physical_native.c').write_text(native)
        run('events-build', cc+['-o', output/'events-test.exe', SOURCE/'events_test.c'])
        run('events-test', [output/'events-test.exe'])
        run('unit-object', cc+['-Dmemcpy=ws_pipp_test_copy', '-Dws_frame_destroy=ws_rearm_test_destroy',
            '-Dws_frame_status=ws_rearm_test_status', '-c', '-o', output/'rearm.o', SOURCE/'frame.c'])
        run('unit-build', cc+['-o', output/'unit.exe', SOURCE/'frame_test.c', output/'rearm.o',
            SOURCE/'session_reuse.c', acq/'frame.c', acq/'acquisition_control.c', usb/'control_eeprom.c'])
        run('unit', [output/'unit.exe'])
        runtime = output/'runtime'; shutil.copytree(raw/'runtime', runtime)
        # The old executables remain in v1; this new release carries one helper.
        for exe in runtime.glob('*.exe'): exe.unlink()
        run('physical-usb-build', cc+['-DWL_BUILD_DLL', '-shared', '-o', output/'usbdrvd.raw.dll',
            *core, usb/'winusb_session/winusb_provider.c', device_source])
        base.export_table.EXPECTED += [{'name': 'WsPippFrameV1', 'ordinal': 105, 'stack_pop_bytes': 20},
                                      {'name': 'WsReadDetectorImageV1', 'ordinal': 106, 'stack_pop_bytes': 20}]
        base.export_table.finalize(output/'usbdrvd.raw.dll', runtime/'usbdrvd.dll')
        header = (frozen/'physical_build.h').read_text()
        old = sha(frozen/'runtime/usbdrvd.dll'); assert header.count(old) == 1
        (output/'physical_build.h').write_text(header.replace(old, sha(runtime/'usbdrvd.dll')))
        run('physical-helper-build-only', cc+['-I', output, '-o', runtime/'persistent_ingaas.exe',
            SOURCE/'helper.c', SOURCE/'observer.c', usb/'controller_image.c', '-ladvapi32'])
        offline = output/'offline-runtime'; shutil.copytree(runtime, offline)
        offline_build = output/'offline-build'; offline_build.mkdir()
        (offline_build/'persistent_physical_native.c').write_text(native)
        fake = (raw/'offline-build/offline_usb.c').read_text()
        fake = fake.replace('nf.starts==1&&nf.begun[0]==1', 'nf.starts==nf.begun[0]&&nf.starts>=1')
        fake = fake.replace('(BYTE)(7*i+31)', '(BYTE)(7*i+31+nf.starts)')
        fake = fake.replace('mode<=21&&', 'mode<=24&&')
        fake = fake.replace('(mode_==1||mode_==19)?ERROR_CRC:0', '(mode_==22?ERROR_DEVICE_NOT_CONNECTED:((mode_==1||mode_==19)?ERROR_CRC:0))')
        marker = '__declspec(dllexport) DWORD WINAPI WsOfflineVerifyV1(void)'
        fake = fake[:fake.index(marker)]+r'''
__declspec(dllexport) DWORD WINAPI WsOfflineVerifyV1(void){
 DWORD w,c,after;BOOL(WINAPI*count)(HANDLE,PDWORD)=(void*)GetProcAddress(GetModuleHandleA("kernel32.dll"),"GetProcessHandleCount");
 if(delayed_){CHECK(WaitForSingleObject(delayed_,INFINITE)==WAIT_OBJECT_0&&CloseHandle(delayed_));delayed_=NULL;}
 wl_test_live(&w,&c);CHECK(!w&&!c&&nf.freed==1&&eeprom_query_==295&&detector_query_==282&&patterns_==1);
 CHECK(count(GetCurrentProcess(),&after)&&live_before_==after);
 printf("{\"suite\":\"persistent_native_fake\",\"starts\":%lu,\"reads\":%ld,\"handles_before\":%lu,\"handles_after\":%lu,\"hardware_calls\":0}\n",nf.starts,nf.begun[0],live_before_,after);return 1;
}
'''
        (offline_build/'offline_usb.c').write_text(fake)
        offline_header = (raw/'offline-build/physical_build.h').read_text()
        (offline_build/'physical_build.h').write_text(offline_header)
        run('offline-usb-build', cc+['-DWL_BUILD_DLL', '-DWL_TEST', '-I', offline_build, '-shared',
            '-o', offline_build/'usbdrvd.raw.dll', offline_build/'offline_usb.c', *core])
        base.export_table.EXPECTED += [dict(name=name, ordinal=ordinal, stack_pop_bytes=pop) for name, ordinal, pop in [
            ('WsOfflineInstallV1', 206, 4), ('WsOfflinePublishV1', 207, 0), ('WsOfflineVerifyV1', 208, 0),
            ('WinUsb_ReadPipe', 209, 24), ('WinUsb_GetOverlappedResult', 210, 16)]]
        base.export_table.finalize(offline_build/'usbdrvd.raw.dll', offline/'usbdrvd.dll')
        old_offline = sha(raw/'offline-runtime/usbdrvd.dll'); assert offline_header.count(old_offline) == 1
        (offline_build/'physical_build.h').write_text(offline_header.replace(old_offline, sha(offline/'usbdrvd.dll')))
        run('offline-helper-build', cc+['-I', offline_build, '-o', offline/'persistent_ingaas.exe',
            SOURCE/'helper.c', SOURCE/'observer.c', usb/'controller_image.c', '-ladvapi32'])
        sys.path.insert(0, str(SOURCE)); import offline_test
        for mode in (13, 16, 18, 19, 20, 21, 22, 23, 24):
            offline_test.check(offline, output/f'test-{mode}', mode, 3 if mode == 13 else 1)
        offline_test.check(offline, output/'test-eof', 13, 2, eof=True)
        offline_test.check(offline, output/'test-no-frames', 13, 0)
        offline_test.ready_write_failure(offline, output/'test-ready-write-failure')
        offline_test.partial_command_eof(offline, output/'test-partial-eof')
        offline_test.check(offline, output/'test-long-1000', 13, 1000)
        pins = {p.relative_to(runtime).as_posix(): sha(p) for p in runtime.rglob('*') if p.is_file()}
        manifest = json.loads((REPO/'app/devices/winusb_ingaas_runtime.json').read_text())
        manifest.update(release='experimental-persistent-recipes-v5', runtime_files=pins, runtime_directory='runtime-v5', log_protocol=1,
                        recipe_protocol=1, exposure_min_ms=10, exposure_max_ms=1000, max_accumulations=100,
                        scope='Manual integer 10..1000 ms, 512 x 1, uint16, fixed exposure per persistent session; host accumulation; no cooling writes or automated scans.',
                        source_evidence_sha256=None, baseline_manifest_sha256=sha(frozen/'evidence.json'))
        if portable:
            manifest.update(release='experimental-persistent-portable-v6', runtime_directory='runtime-v6-portable',
                            binding_protocol=1, device_instance=None)
        (output/'runtime-manifest.json').write_text(json.dumps(manifest, indent=2))
        report['passed'] = True
    finally:
        report['artifacts'] = {str(p.relative_to(output)): sha(p) for p in output.rglob('*') if p.is_file()}
        (output/'evidence.json').write_text(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--portable', action='store_true', help='Require an explicitly enrolled A010 instance per computer')
    args = parser.parse_args(); build(args.baseline_root, args.output, portable=args.portable)
