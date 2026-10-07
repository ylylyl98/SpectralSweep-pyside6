import json
import threading

import numpy as np
import pytest

from tools.winspec import validate_backend as validation
from tools.winspec import pvcam_camera_server as server
from tests.test_pvcam_camera_server import bridge
from app.devices.winspec_adapter import WinSpecClient


def test_native_idle_preflight_is_read_only_and_records_current_source(tmp_path):
    native, sdk = bridge(tmp_path)
    native.execute('SET_SETTINGS', dict(exposure_ms=800, accumulations=1, sequential_frames=1))
    baseline = tmp_path/'baseline.json'
    baseline.write_text(json.dumps(dict(status='complete', native_restore_ok=True,
        original_native_settings=sdk.original)))
    calls = []
    class Client:
        def request(self, command, parameters=None):
            calls.append(command)
            return native.execute(command, parameters)
    result = validation.prepare_preflight(Client(), baseline, tmp_path/'preflight.json', backend='pvcam')
    assert calls == ['GET_SETTINGS']
    assert result['source_backend'] == 'pvcam'
    assert result['settings']['exposure_ms'] == 800
    assert result['expected_native'] == sdk.original
    assert result['settings']['actual_temperature_c'] == -100


def test_native_preflight_adds_new_optional_readback_without_ignoring_baseline(tmp_path):
    native, sdk = bridge(tmp_path)
    native.execute('SET_SETTINGS', dict(exposure_ms=800, accumulations=1, sequential_frames=1))
    native.settings['native_settings']['pix_time'] = 1000
    baseline = tmp_path/'baseline.json'
    baseline.write_text(json.dumps(dict(status='complete', native_restore_ok=True,
        original_native_settings=sdk.original)))
    class Client:
        def request(self, command, parameters=None):
            return native.execute(command, parameters)
    result = validation.prepare_preflight(Client(), baseline, tmp_path/'preflight.json', backend='pvcam')
    assert result['expected_native'] == dict(sdk.original, pix_time=1000)


@pytest.mark.parametrize('mismatch', ['native', 'frames', 'unhealthy'])
def test_native_preflight_rejects_unverified_restoration_state(tmp_path, mismatch):
    native, sdk = bridge(tmp_path)
    if mismatch != 'frames':
        native.execute('SET_SETTINGS', dict(exposure_ms=800, accumulations=1, sequential_frames=1))
    if mismatch == 'unhealthy': native.unhealthy = True
    expected = dict(sdk.original)
    if mismatch == 'native': expected['spdtab_index'] = 1
    baseline = tmp_path/'baseline.json'
    baseline.write_text(json.dumps(dict(status='complete', native_restore_ok=True,
        original_native_settings=expected)))
    class Client:
        def request(self, command, parameters=None):
            return native.execute(command, parameters)
    destination = tmp_path/'preflight.json'
    with pytest.raises(RuntimeError):
        validation.prepare_preflight(Client(), baseline, destination, backend='pvcam')
    assert not destination.exists()


@pytest.mark.parametrize('mode', ['default', 'finish', 'placebo'])
def test_capture_tool_uses_real_protocol_and_restores_recipe(tmp_path, mode):
    native, sdk=bridge(tmp_path, diagnostic_finish_each=mode=='finish',
                       diagnostic_archive_placebo=mode=='placebo')
    original=native.settings['exposure_ms'],native.settings['accumulations']
    service=server.Server(('127.0.0.1',0),server.Handler);service.bridge=native
    thread=threading.Thread(target=service.serve_forever,daemon=True);thread.start()
    try:
        report=validation.capture_batch(WinSpecClient('127.0.0.1',service.server_address[1]),
            'pvcam',tmp_path/'host',samples=3,exposure_ms=10,frames=2,
            kind='light',condition_id='fixed1',optics_note='Fixed grating and stable source')
        assert report['status']=='complete' and report['restore_ok']
        with np.load(report['counts_archive']) as a:
            np.testing.assert_array_equal(a['counts'], np.full((3,512),65000.5))
        assert (native.settings['exposure_ms'],native.settings['accumulations'])==original
        assert len(report['samples'])==3 and report['warmup']['metadata']['acquisition_backend']=='pvcam'
        clocks = report['samples'][0]['metadata']['client_timing_s']
        assert clocks['request_finished_unix'] >= clocks['request_started_unix']
        assert clocks['request_elapsed_wall_s'] >= 0
        if mode != 'default':
            for row in [report['warmup']]+report['samples']:
                assert row['metadata']['diagnostic_sequence_cleanup']['completed'] is True
                assert row['metadata']['diagnostic_sequence_cleanup']['native_finish_called'] is (mode=='finish')
                assert row['metadata']['server_build'] == (server.FINISH_EACH_BUILD if mode=='finish' else server.ARCHIVE_PLACEBO_BUILD)
            assert sdk.events.count('finish') == (4 if mode=='finish' else 1)
    finally:
        service.shutdown();service.server_close();thread.join(2);native.close()


def test_malformed_batch_restore_is_not_reported_success(tmp_path):
    class Client:
        def request(self,*a,**kw): return {'settings':{},'camera_busy':True},b''
    with pytest.raises(RuntimeError):
        validation.capture_batch(Client(),'pvcam',tmp_path,samples=3,exposure_ms=10,frames=2,
            kind='light',condition_id='fixed1',optics_note='Fixed')


def fixture_batch(tmp_path,name,kind,backend,counts):
    path=tmp_path/(name+'.json'); archive=tmp_path/(name+'.npz')
    np.savez(archive,counts=np.asarray(counts,dtype=float))
    import hashlib
    data={'status':'complete','restore_ok':True,'kind':kind,'backend':backend,
          'condition_id':'same','optics_note':'same','exposure_ms':500,'frames':2,
          'counts_archive':str(archive),'counts_sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}
    path.write_text(json.dumps(data));return path


def test_comparison_subtracts_separate_backgrounds_and_reports_centroid(tmp_path):
    base=np.full((4,512),100.)
    signal=np.zeros((4,512));signal[:,199:202]=[10,40,10]
    wd=fixture_batch(tmp_path,'wd','dark','winspec',base)
    pd=fixture_batch(tmp_path,'pd','dark','pvcam',base+20)
    wl=fixture_batch(tmp_path,'wl','light','winspec',base+signal)
    pl=fixture_batch(tmp_path,'pl','light','pvcam',base+20+signal*1.01)
    report=validation.compare_batches(wl,pl,wd,pd,peak_roi=(195,205))
    assert report['integral_difference_percent']==pytest.approx(1)
    assert report['peak_centroid_difference_pixels']==pytest.approx(0)
    assert report['winspec']['peak_centroid_pixel']==pytest.approx(201)
    assert report['verdict']=='measured_only'


def test_changed_source_condition_prevents_equivalence_comparison(tmp_path):
    a=np.ones((4,512))
    paths=[fixture_batch(tmp_path,str(i),kind,backend,a) for i,(kind,backend) in enumerate([
        ('light','winspec'),('light','pvcam'),('dark','winspec'),('dark','pvcam')])]
    changed=json.loads(paths[1].read_text());changed['condition_id']='different';paths[1].write_text(json.dumps(changed))
    with pytest.raises(ValueError,match='condition'):validation.compare_batches(*paths,peak_roi=(195,205))


class StopInterrupted(BaseException):
    pass


@pytest.mark.parametrize('stop_error',[TimeoutError('Stop stalled'),StopInterrupted('Stop interrupted')])
def test_failed_stop_prohibits_any_followup_driver_reads_or_restoration(tmp_path,stop_error):
    from tests.test_winspec_adapter import Camera
    class Client(Camera):
        def request(self,command,*args,**kwargs):
            if command=='ACQUIRE_GUARDED':
                self.calls.append((command,{}));raise TimeoutError('Capture uncertain')
            if command=='STOP':
                self.calls.append((command,{}));raise stop_error
            metadata,payload=super().request(command,*args,**kwargs)
            metadata['acquisition_settings_version']=2
            metadata['settings'].update(running=False,winspec_reported_running=False,controller_running=False)
            return metadata,payload
    client=Client()
    with pytest.raises(BaseException):
        validation.capture_batch(client,'winspec',tmp_path,samples=3,exposure_ms=500,frames=2,
            kind='light',condition_id='fixed1',optics_note='Fixed')
    assert client.calls[-1][0]=='STOP'
    report=json.loads((tmp_path/'report.json').read_text())
    assert report['recovery_required'] and not report['restore_ok']
