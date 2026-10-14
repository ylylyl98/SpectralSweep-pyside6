"""Actual vendor DLL setters/getters against fake USB only; serial execution."""
import json
import os
from pathlib import Path
import subprocess
import uuid

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get('SPECTRALSWEEP_WINUSB_OFFLINE_RUNTIME'),
    reason='Separately built fake transport required')


@pytest.mark.parametrize('exposure',[10,50,100,300,1000])
def test_native_recipe_reads_back_exposure_before_and_after_init(tmp_path,exposure):
    runtime=Path(os.environ['SPECTRALSWEEP_WINUSB_OFFLINE_RUNTIME']).resolve()
    assert runtime.name=='offline-runtime'
    out=tmp_path/'native'; sid=str(uuid.uuid4()); ids=[str(uuid.uuid4()) for _ in range(2)]
    run=subprocess.run([str(runtime/'persistent_ingaas.exe'),'--offline-recipe-session',str(runtime),
        str(out),sid,'67108864',str(exposure),'13'],
        input=f'C 1 {ids[0]}\nC 2 {ids[1]}\nQ\n',text=True,capture_output=True)
    assert run.returncode==0,run.stdout+run.stderr
    ready=json.loads((out/'ready.json').read_text())
    assert ready['recipe_protocol']==1 and ready['exposure_ms']==exposure
    assert ready['exposure_readback_verified'] is True
    log=[json.loads(line) for line in (out/'session.jsonl').read_text().splitlines()]
    for name in ('exposure_readback_before_init_us','exposure_readback_after_init_us'):
        entries=[r for r in log if r['stage']==name]
        assert len(entries)==1 and entries[0]['value']==exposure*1000 and entries[0]['error']==0
    for i in (1,2):
        proof=json.loads((out/f'frame-{i:06d}'/'frame.json').read_text())
        assert proof['recipe_protocol']==1 and proof['exposure_ms']==exposure
        assert proof['request_id']==ids[i-1] and proof['sequence']==i
    assert json.loads((out/'closed.json').read_text())['completed_frames']==2


@pytest.mark.parametrize('exposure',['9','1001','-100','100x','100.0','0','100000000000000000000',''])
def test_native_invalid_exposure_rejected_before_creating_session(tmp_path,exposure):
    runtime=Path(os.environ['SPECTRALSWEEP_WINUSB_OFFLINE_RUNTIME']).resolve()
    assert runtime.name=='offline-runtime'
    out=tmp_path/'native'
    run=subprocess.run([str(runtime/'persistent_ingaas.exe'),'--offline-recipe-session',str(runtime),
        str(out),str(uuid.uuid4()),'67108864',exposure,'13'],input='Q\n',text=True,capture_output=True)
    assert run.returncode==64 and not out.exists()
