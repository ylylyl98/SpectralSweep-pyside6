import copy,json
from pathlib import Path
import pytest
from tools.winspec.start_acceleration.resident_protocol import analyze_light


def fixture():
    path=Path(__file__).parent / 'fixtures' / 'winspec-resident-fixture.json'
    f=json.loads(path.read_text());return f


def full_fallback(f):
    t=copy.deepcopy(f['trial']);t['redirects_applied']=[];t['control_flow_modified']=False
    for p in t['programs']:
        p['body']['decision']={'redirect':False,'reason':'Baseline configuration/state differs'}
        for r in (p,p['body'],p['return_row']):
            region=r['full_state']['regions']['settings'];region['hex']='ff'+region['hex'][2:]
    t['records']=t['programs']
    return t


def test_pure_state_mismatch_is_verified_as_full_execution_fallback():
    f=fixture();t=full_fallback(f)
    a=analyze_light(t,f['start_begin'],f['start_end'],f['baseline']['modules'],f['baseline'],allow_fallback=True)
    assert a['redirect_applied'] is False and a['program_returns']==[1,1]


@pytest.mark.parametrize('bad',['read','missing_step','tail','false_redirect','second_redirect'])
def test_allowing_fallback_does_not_hide_hard_trace_errors(bad):
    f=fixture();t=full_fallback(f)
    if bad=='read':t['experiment_errors']=['read failed']
    if bad=='missing_step':t['buffered_log'].pop()
    if bad=='tail':t['programs'][0]['return_row']['eax']=0
    if bad=='false_redirect':t['programs'][0]['body']['decision']['redirect']=True
    if bad=='second_redirect':t['programs'][1]['body']['decision']['redirect']=True
    with pytest.raises(ValueError):analyze_light(t,f['start_begin'],f['start_end'],f['baseline']['modules'],f['baseline'],allow_fallback=True)


def test_deadline_waits_for_frame_boundary_and_does_not_retire_mid_step():
    from tools.winspec.start_acceleration.resident_trace import ResidentTrace
    from types import SimpleNamespace
    t=ResidentTrace.__new__(ResidentTrace);t.boundary=SimpleNamespace(current={'frame_id':0});t.active={};t.stepping={}
    assert t.deadline_ready() is False
    t.boundary.current=None;t.stepping={7:{}}
    assert t.deadline_ready() is False
    t.stepping={};assert t.deadline_ready() is True
