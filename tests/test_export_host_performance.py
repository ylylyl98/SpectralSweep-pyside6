"""Reduce optional index work while keeping portable export writes durable."""
import json
from pathlib import Path
import sqlite3

import pytest

from app import experiment_metadata as metadata
from app.experiment_integration import persist_output_snapshot
from tests.test_winusb_retention import cache, session, frame


def export(root, number, settings=None):
    output=root/f'frame-{number}.csv'
    output.write_text('pixel,counts\n1,3\n',encoding='utf-8')
    sidecar=persist_output_snapshot(output,experiment_type='spectrum_preview',device_id='sample',settings=settings or {})
    return output,sidecar


def test_export_keeps_both_durable_states_but_indexes_only_completion(tmp_path,monkeypatch):
    traced=[];states=[];original=sqlite3.connect;write=metadata._atomic_json
    def connect(*args,**kwargs):
        db=original(*args,**kwargs);db.set_trace_callback(traced.append);return db
    def atomic(path,value):
        result=write(path,value);states.append(json.loads(Path(path).read_text())['status']);return result
    monkeypatch.setattr(sqlite3,'connect',connect)
    monkeypatch.setattr(metadata,'_atomic_json',atomic)
    for i in range(3):
        _,sidecar=export(tmp_path,i)
        assert metadata.ExperimentMetadataService(tmp_path).load_metadata(sidecar)['status']=='completed'
    assert states==['running','completed']*3
    assert sum(s.startswith('INSERT INTO experiments') for s in traced)==3
    assert sum(s=='PRAGMA journal_mode=WAL' for s in traced)==1


def test_reused_export_service_isolates_history_changes_and_recovers_deleted_index(tmp_path,monkeypatch):
    first=tmp_path/'a.sqlite';second=tmp_path/'b.sqlite'
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH',str(first))
    export(tmp_path,1)
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH',str(second))
    export(tmp_path,2)
    assert len(metadata.ExperimentHistory(first).query('sample','spectrum_preview'))==1
    assert len(metadata.ExperimentHistory(second).query('sample','spectrum_preview'))==1
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH',str(first))
    first.unlink()
    export(tmp_path,3)
    assert len(metadata.ExperimentHistory(first).query('sample','spectrum_preview'))==1


def test_overwrite_reopened_sidecar_indexes_only_updated_completion(tmp_path,monkeypatch):
    export(tmp_path,1,{'exposure_ms':100})
    traced=[];original=sqlite3.connect
    def connect(*args,**kwargs):
        db=original(*args,**kwargs);db.set_trace_callback(traced.append);return db
    monkeypatch.setattr(sqlite3,'connect',connect)
    _,sidecar=export(tmp_path,1,{'exposure_ms':300})
    saved=metadata.ExperimentMetadataService.load_metadata(sidecar)
    assert saved['settings']['requested']['exposure_ms']==300 and saved['status']=='completed'
    assert sum(s.startswith('INSERT INTO experiments') for s in traced)==1


def test_failed_final_sidecar_keeps_running_record_and_native_evidence(tmp_path,monkeypatch):
    owner=cache(tmp_path);current=session(owner);snap=frame(owner,current,1)
    write=metadata._atomic_json
    def fail(path,value):
        if value.get('status')=='completed':raise OSError('final durable write failed')
        return write(path,value)
    monkeypatch.setattr(metadata,'_atomic_json',fail)
    with pytest.raises(OSError,match='durable write'):
        export(tmp_path,1,{'acquisition_time_snapshot':snap})
    output=tmp_path/'frame-1.csv';sidecar=Path(str(output)+'.experiment.metadata.json')
    assert json.loads(sidecar.read_text())['status']=='running'
    with pytest.raises(ValueError,match='incomplete'):owner.acknowledge_saved(snap,output,sidecar)
    frame(owner,current,2);owner.cleanup(current[1])
    assert Path(snap['native_capture']['evidence_directory']).exists()


def test_unavailable_history_still_preserves_completed_portable_export(tmp_path,monkeypatch):
    unavailable=tmp_path/'history-directory';unavailable.mkdir()
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH',str(unavailable))
    _,sidecar=export(tmp_path,1)
    assert metadata.ExperimentMetadataService(tmp_path).load_metadata(sidecar)['status']=='completed'
