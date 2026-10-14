"""Uncontrolled-light tests must not acquire optical certification implicitly."""
import importlib
import importlib.util
import json

import pytest

from tests.test_winusb_optical_validation import Camera


def diagnostic():
    name='tools.winspec.validate_local_stability'
    assert importlib.util.find_spec(name), 'Bounded stability diagnostic missing'
    return importlib.import_module(name)


def test_repeated_capture_uses_current_native_timing_and_explicit_unknown_light(tmp_path):
    module=diagnostic(); camera=Camera(tmp_path/'native')
    run=module.StabilityRun(camera,tmp_path/'result',limit=2)
    first=run.capture('fast',planned_idle_s=0)
    second=run.capture('after_idle',planned_idle_s=1)
    assert first['native_timing']['start_to_publication_ms']==107
    assert second['native_timing']['previous_publication_to_start_ms']==893
    assert second['illumination']=='unchanged_uncontrolled'
    assert not second['hardware_frame_freshness_verified']
    assert first['native_log_bytes_read']>0 and second['native_log_bytes_read']>0
    assert len(list((tmp_path/'result').glob('frame-*.json')))==2
    with pytest.raises(ValueError):run.capture('fast')
    assert camera.calls==2


def test_early_completion_stops_at_first_frame_without_retry(tmp_path):
    module=diagnostic(); camera=Camera(tmp_path/'native',early=True)
    run=module.StabilityRun(camera,tmp_path/'result')
    with pytest.raises(RuntimeError,match='early'):run.capture('fast')
    assert (tmp_path/'result/frame-000001.bin').exists()
    row=json.loads((tmp_path/'result/frame-000001.json').read_text())
    assert row['accepted'] is False and 'early' in row['error']
    with pytest.raises(RuntimeError):run.capture('fast')
    assert camera.calls==1


def test_changed_process_is_rejected_before_second_frame_can_pass(tmp_path):
    module=diagnostic();camera=Camera(tmp_path/'native')
    run=module.StabilityRun(camera,tmp_path/'result')
    run.capture('fast')
    original=camera.capture
    def wrong_owner(*args,**kwargs):
        meta,payload=original(*args,**kwargs);meta['process_pid']=456;return meta,payload
    camera.capture=wrong_owner
    with pytest.raises(RuntimeError,match='identity'):run.capture('fast')
    assert json.loads((tmp_path/'result/frame-000002.json').read_text())['accepted'] is False


def test_existing_output_is_never_reused(tmp_path):
    module=diagnostic(); camera=Camera(tmp_path/'native')
    directory=tmp_path/'result';directory.mkdir()
    with pytest.raises(FileExistsError):module.StabilityRun(camera,directory)
    assert camera.calls==0


def test_diagnostic_follows_receipt_when_log_rotates_between_frames(tmp_path):
    module = diagnostic(); camera = Camera(tmp_path/'native')
    original = camera.capture
    def capture(*args, **kwargs):
        if camera.calls:
            (camera.directory/'session.jsonl').rename(camera.directory/'old.jsonl')
        meta, payload = original(*args, **kwargs)
        if camera.calls == 2:
            (camera.directory/'session.jsonl').rename(camera.directory/'session-000001.jsonl')
            (camera.directory/'old.jsonl').rename(camera.directory/'session.jsonl')
        meta['native_receipt'] = {'log_segment': camera.calls-1}
        return meta, payload
    camera.capture = capture
    run = module.StabilityRun(camera, tmp_path/'result', limit=2)
    run.capture('first')
    second = run.capture('after_rotation')
    assert second['native_timing']['start_to_publication_ms'] == 107
    assert second['native_timing']['previous_publication_to_start_ms'] == 893
