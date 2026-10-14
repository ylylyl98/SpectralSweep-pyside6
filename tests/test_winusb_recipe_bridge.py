"""Real helper pipes validate exposure handshakes and orderly session renewal."""
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from app.devices import winusb_persistent as target
from tests.test_winusb_persistent import HELPER
from tests.winusb_synthetic import write_capture, EEPROM_PINS


def bridge(tmp_path,monkeypatch,damage='none'):
    fixture=write_capture(tmp_path/'synthetic')
    release=target.manifest();release.update(recipe_protocol=1,log_protocol=0,eeprom_files=EEPROM_PINS,
        exposure_min_ms=10,exposure_max_ms=1000,max_accumulations=100)
    monkeypatch.setattr(target,'manifest',lambda:release)
    helper=HELPER.replace('root.mkdir()', 'exposure=int(sys.argv[5])\nroot.mkdir()')
    helper=helper.replace('exposure_ms=100','exposure_ms=exposure,recipe_protocol=1,exposure_readback_verified=True')
    script=tmp_path/'helper.py';script.write_text(helper)
    original=subprocess.Popen; processes=[]
    def launch(command,**kwargs):
        if Path(command[0]).name != 'persistent_ingaas.exe':
            return original(command, **kwargs)
        exposure=command[6] if command[1]=='--recipe-session' else '100'
        process=original([sys.executable,str(script),command[3],command[4],str(fixture.resolve()),damage,exposure],**kwargs)
        processes.append(process);return process
    monkeypatch.setattr(target.subprocess,'Popen',launch)
    monkeypatch.setattr(target,'verify_runtime',lambda path,**kwargs:Path(path).resolve())
    monkeypatch.setattr(target,'inspect_device',lambda:{'present':True,'service':'WINUSB'})
    return target.PersistentCaptureBridge(runtime_dir=tmp_path,state_dir=tmp_path/'state'),processes


def test_exposure_change_closes_old_session_and_same_exposure_reuses_process(tmp_path,monkeypatch):
    owner,processes=bridge(tmp_path,monkeypatch)
    frames=[]
    try:
        for exposure in (10,10,300,300):
            frames.append(owner.capture(threading.Event(),exposure_ms=exposure)[0])
        assert len(processes)==2 and processes[0].poll()==0 and processes[1].poll() is None
        assert [r['request_sequence'] for r in frames]==[1,2,1,2]
        assert [r['exposure_ms'] for r in frames]==[10,10,300,300]
        assert frames[0]['bridge_session_id']!=frames[2]['bridge_session_id']
        for frame in frames:
            assert frame['native_receipt']['exposure_readback_verified'] is True
    finally: owner.close()
    assert not owner._claim.exists()


@pytest.mark.parametrize('damage',[{'recipe_protocol':True},{'recipe_protocol':0},
    {'exposure_ms':100},{'exposure_readback_verified':False}])
def test_recipe_handshake_mismatch_sends_no_capture(tmp_path,monkeypatch,damage):
    owner,processes=bridge(tmp_path,monkeypatch)
    original=owner._wait_receipt
    def read(path,*args,**kwargs):
        value=original(path,*args,**kwargs)
        if path.name=='ready.json': value.update(damage)
        return value
    monkeypatch.setattr(owner,'_wait_receipt',read)
    with pytest.raises(RuntimeError):owner.capture(threading.Event(),exposure_ms=300)
    assert owner._sequence==0 and processes[0].poll()==0
    assert not owner._claim.exists()


def test_failed_close_during_recipe_change_retains_cleanup_owner(tmp_path,monkeypatch):
    owner,processes=bridge(tmp_path,monkeypatch,'close')
    owner.capture(threading.Event(),exposure_ms=10)
    session=owner._session_id
    with pytest.raises(RuntimeError,match='cleanup|ownership'):
        owner.capture(threading.Event(),exposure_ms=300)
    assert len(processes)==1 and owner._session_id==session and owner._claim.exists()
    with pytest.raises(RuntimeError,match='cleanup|ownership'): owner.close()


def test_returned_timing_accounts_for_cache_registration(tmp_path,monkeypatch):
    import time
    owner,processes=bridge(tmp_path,monkeypatch)
    original=owner._cache.record_frame
    def record(meta,**kwargs):
        time.sleep(.03);return original(meta,**kwargs)
    monkeypatch.setattr(owner._cache,'record_frame',record)
    try:
        meta,_=owner.capture(threading.Event(),exposure_ms=10)
        timing=meta['bridge_timing_s']
        assert timing['cache_record']>=.03
        assert timing['total']>=timing['cache_record']+timing['receipt_wait']
        assert all(timing[k]>=0 for k in ('preflight','cache_cleanup_gate','initialization','evidence_validation'))
    finally:owner.close()


def test_error_close_during_exposure_change_never_launches_next_helper(tmp_path,monkeypatch):
    owner,processes=bridge(tmp_path,monkeypatch)
    script=tmp_path/'helper.py'
    source=script.read_text().replace('completed_frames=completed,error=0','completed_frames=completed,error=5')
    script.write_text(source+'\nsys.exit(1)\n')
    owner.capture(threading.Event(),exposure_ms=10)
    with pytest.raises(RuntimeError,match='error|fault|failure'):
        owner.capture(threading.Event(),exposure_ms=300)
    assert len(processes)==1 and processes[0].poll()==1
    assert not owner.is_ready and not owner._claim.exists()
