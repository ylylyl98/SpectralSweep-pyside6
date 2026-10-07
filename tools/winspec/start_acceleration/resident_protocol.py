"""Explicit bounded Start boundaries; errors permanently disable this session."""
import copy, json, os
from .experiment_state import Gate, normalized
from .experiment_trace import verify_caller
from .experiment_analysis import analyze_trace
from .winspec_debug_trace import save


def publish(path, value):
    # Readers see either no command or its complete flushed contents.
    temporary = path + '.partial'
    save(temporary, value)
    os.rename(temporary, path)


def analyze_light(trace, begin, end, modules, baseline, allow_fallback=False):
    if any(trace.get(k) for k in ('experiment_errors','telemetry_errors','forwarded_exceptions','module_unloaded','resident_errors')):
        raise ValueError('Trace has errors or lost coverage')
    if any(r['name'] != 'program_entry' for r in trace['records']):
        raise ValueError('Light trace contains unexpected records')
    if trace['target_hits'] != dict(program_entry=2,body_entry=2,program_exit=2):
        raise ValueError('Light endpoint coverage differs')
    view = dict(trace)
    view['target_hits'] = dict(trace['target_hits'], output_entry=0)
    result = analyze_trace(view, begin, end, modules, trial=True)
    first, second = result['programs']
    gate = Gate(baseline['programs'], modules['contrman.dll']['base'])
    decision = gate.decide(1,first['controller'],first['tid'],first['body']['full_state'])
    if decision['redirect'] != result['redirect_applied']:
        raise ValueError('Recorded redirect differs from independently evaluated state gate')
    if not result['redirect_applied'] and not allow_fallback:
        raise ValueError('Actual first omission required; fallback ends sequence')
    if result['redirect_applied'] and normalized(second['body']['full_state']) != gate.expected:
        raise ValueError('Second program configuration changed')
    return dict(redirect_applied=result['redirect_applied'],reason=decision['reason'],program_returns=[p['return_row']['eax'] for p in (first,second)],
                output_stream_observed=False, endpoint_hits=6)


class Boundary(object):
    def __init__(self, baseline, pid, modules, limit, allow_fallback=False):
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1 <= limit <= 30:
            raise ValueError('Frame limit must be 1 through 30')
        if baseline['pid'] != pid or baseline['modules'] != modules:
            raise ValueError('Baseline process/modules changed')
        for ordinal,p in enumerate(baseline['programs'],1):
            verify_caller(p,ordinal,modules)
            if not baseline['start_begin'] <= p['qpc'] < p['return_row']['qpc'] <= baseline['start_end']:
                raise ValueError('Baseline outside Start')
        Gate(baseline['programs'], modules['contrman.dll']['base'])
        self.baseline=copy.deepcopy(baseline);self.modules=copy.deepcopy(modules)
        self.limit=limit;self.next_id=0;self.current=None;self.failed=None
        self.allow_fallback=allow_fallback

    def disable(self, reason):
        self.failed=self.failed or str(reason)

    def open(self, frame_id, now, active, stepping):
        try:
            if self.failed or self.current is not None or active or stepping or frame_id != self.next_id or frame_id >= self.limit:
                raise ValueError('Invalid or unsafe frame opening')
            self.current=dict(frame_id=frame_id,opened_qpc=now)
            return Gate(self.baseline['programs'], self.modules['contrman.dll']['base'])
        except BaseException as error:
            self.disable(error);raise

    def close(self, frame_id, trace, begin, end, now, active, stepping):
        try:
            if self.failed or self.current is None or frame_id != self.current['frame_id'] or active or stepping:
                raise ValueError('Invalid or unsafe frame closing')
            if not self.current['opened_qpc'] <= begin < end <= now:
                raise ValueError('Start outside acknowledged frame boundary')
            result=analyze_light(trace,begin,end,self.modules,self.baseline,allow_fallback=self.allow_fallback)
            self.current=None;self.next_id+=1
            return result
        except BaseException as error:
            self.disable(error);raise
