"""A durable batch export must authorize every exact member, or none."""
import copy
import json
from pathlib import Path
import uuid

import pytest

from app.experiment_integration import persist_output_snapshot
from tests.test_winusb_retention import cache,session,frame


def batch(owner,current):
    frames=[frame(owner,current,i) for i in (1,2)]
    bid=str(uuid.uuid4())
    for i, item in enumerate(frames,1):
        item['native_capture'].update(exposure_ms=300,
            request_context={'batch_id':bid,'accumulation_index':i,'accumulations':2})
    return {'native_capture':{'acquisition_backend':'winusb_local','batch_protocol':1,
        'batch_id':bid,'exposure_ms':300,'accumulations':2,
        'frames':[f['native_capture'] for f in frames],
        'raw_frames':[f['raw_accumulated_counts'] for f in frames]},'raw_accumulated_counts':[3]*512}


def exported(tmp_path,snapshot,monkeypatch):
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH',str(tmp_path/'history.sqlite'))
    output=tmp_path/'spectrum.csv';output.write_text('pixel,intensity_counts\n1,1.5\n')
    sidecar=persist_output_snapshot(output,experiment_type='spectrum_preview',device_id='test',
        settings={'acquisition_time_snapshot':snapshot})
    return output,sidecar


def test_saved_batch_reclaims_all_members_after_recent_window(tmp_path,monkeypatch):
    owner=cache(tmp_path);current=session(owner);snap=batch(owner,current)
    output,sidecar=exported(tmp_path,snap,monkeypatch)
    assert owner.acknowledge_saved(snap,output,sidecar)
    frame(owner,current,3)
    owner.cleanup(current[1])
    assert not (current[0]/'native/frame-000001').exists()
    assert not (current[0]/'native/frame-000002').exists()
    assert (current[0]/'native/frame-000003').exists()


@pytest.mark.parametrize('damage',['missing','duplicate','hash','sum','session','exposure','context','sequence'])
def test_corrupt_saved_batch_never_authorizes_any_member(tmp_path,monkeypatch,damage):
    owner=cache(tmp_path);current=session(owner);snap=batch(owner,current)
    corrupt=copy.deepcopy(snap);native=corrupt['native_capture']
    if damage=='missing':native['raw_frames'].pop()
    if damage=='duplicate':native['frames'][1]['request_id']=native['frames'][0]['request_id']
    if damage=='hash':native['raw_frames'][1][0]=999
    if damage=='sum':corrupt['raw_accumulated_counts'][0]=999
    if damage=='session':native['frames'][1]['bridge_session_id']=str(uuid.uuid4())
    if damage=='exposure':native['frames'][1]['exposure_ms']=100
    if damage=='context':native['frames'][1]['request_context']['batch_id']=str(uuid.uuid4())
    if damage=='sequence':native['frames'][1]['request_sequence']=3
    output,sidecar=exported(tmp_path,corrupt,monkeypatch)
    with pytest.raises((ValueError,KeyError)):owner.acknowledge_saved(snap,output,sidecar)
    with owner._db() as db:
        assert db.execute('SELECT COUNT(*) FROM frames WHERE output IS NOT NULL').fetchone()[0]==0
    frame(owner,current,3);owner.cleanup(current[1])
    assert (current[0]/'native/frame-000001').exists() and (current[0]/'native/frame-000002').exists()


def test_last_member_index_mismatch_rolls_back_entire_acknowledgement(tmp_path,monkeypatch):
    owner=cache(tmp_path);current=session(owner);snap=batch(owner,current)
    output,sidecar=exported(tmp_path,snap,monkeypatch)
    with owner._db() as db:
        db.execute('UPDATE frames SET request_id=? WHERE sequence=2',(str(uuid.uuid4()),))
    with pytest.raises(ValueError):owner.acknowledge_saved(snap,output,sidecar)
    with owner._db() as db:
        assert db.execute('SELECT COUNT(*) FROM frames WHERE output IS NOT NULL').fetchone()[0]==0


@pytest.mark.parametrize('damage',['all_exposures','all_batch_ids','missing_receipt','changed_receipt'])
def test_internally_consistent_rewrite_cannot_replace_original_acquisition_evidence(tmp_path,monkeypatch,damage):
    owner=cache(tmp_path);current=session(owner);snap=batch(owner,current)
    corrupt=copy.deepcopy(snap);native=corrupt['native_capture']
    if damage=='all_exposures':
        native['exposure_ms']=100
        for f in native['frames']: f['exposure_ms']=100
    if damage=='all_batch_ids':
        native['batch_id']=str(uuid.uuid4())
        for f in native['frames']: f['request_context']['batch_id']=native['batch_id']
    if damage=='missing_receipt':
        for f in native['frames']:f.pop('native_receipt')
    if damage=='changed_receipt':native['frames'][1]['native_receipt']['sequence']=500
    output,sidecar=exported(tmp_path,corrupt,monkeypatch)
    with pytest.raises(ValueError):owner.acknowledge_saved(snap,output,sidecar)
    with owner._db() as db:
        assert db.execute('SELECT COUNT(*) FROM frames WHERE output IS NOT NULL').fetchone()[0]==0
