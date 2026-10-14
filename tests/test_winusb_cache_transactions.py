"""Combine cache maintenance commits without weakening admission or retention."""
import json

import pytest

from app.devices import winusb_retention as target
from tests.test_winusb_retention import cache,session,frame,save


def test_record_can_reclaim_saved_old_frame_in_same_transaction(tmp_path,monkeypatch):
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH',str(tmp_path/'history.sqlite'))
    owner=cache(tmp_path);current=session(owner)
    first=frame(owner,current,1);save(owner,first,tmp_path/'exports')
    second=frame(owner,current,2)
    # Recreate only the index entry to exercise the real record+cleanup operation.
    with owner._db() as db:db.execute('DELETE FROM frames WHERE sequence=2')
    traced=[];original=target.sqlite3.connect
    def connect(*args,**kwargs):
        db=original(*args,**kwargs);db.set_trace_callback(traced.append);return db
    monkeypatch.setattr(target.sqlite3,'connect',connect)
    owner.record_frame(second['native_capture'],cleanup=True)
    assert not (current[0]/'native/frame-000001').exists()
    assert (current[0]/'native/frame-000002').exists()
    assert sum(s=='COMMIT' for s in traced)==1


def test_deferred_housekeeping_still_reclaims_before_capacity_backpressure(tmp_path,monkeypatch):
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH',str(tmp_path/'history.sqlite'))
    owner=cache(tmp_path,max_frames=3);current=session(owner)
    first=frame(owner,current,1);save(owner,first,tmp_path/'exports')
    frame(owner,current,2);frame(owner,current,3)
    owner.before_capture(current[1],defer_cleanup=True)
    assert not (current[0]/'native/frame-000001').exists()
    assert (current[0]/'native/frame-000002').exists()
    assert (current[0]/'native/frame-000003').exists()


def test_deferred_housekeeping_does_not_admit_unsaved_over_capacity(tmp_path):
    owner=cache(tmp_path,max_frames=2);current=session(owner)
    frame(owner,current,1);frame(owner,current,2)
    with pytest.raises(target.CacheCapacityError):owner.before_capture(current[1],defer_cleanup=True)
    assert len(list((current[0]/'native').glob('frame-*')))==2
