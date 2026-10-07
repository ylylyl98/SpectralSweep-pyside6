"""Validate profiled intervals. Pause subtraction is NOT native execution time."""
from __future__ import division


def pause_ticks(pauses, begin, end):
    if end < begin:
        raise ValueError('Clock reversed')
    observed = uncertainty = 0
    last = None
    for pause in pauses:
        a, b, c = [pause[k] for k in ('received', 'resume_before', 'resume_after')]
        if not a <= b <= c or (last is not None and a < last):
            raise ValueError('Invalid pause clock or overlapping pauses')
        last = c
        observed += max(0, min(end, b) - max(begin, a))
        uncertainty += max(0, min(end, c) - max(begin, b))
    return observed, uncertainty


def intervals(records, pauses, frequency):
    if frequency <= 0:
        raise ValueError('Invalid QPC frequency')
    pending, result, previous = {}, [], None
    for row in records:
        if len([p for p in pauses if p.get('code')==1 and p.get('tid')==row['tid'] and p['received']==row['qpc']])!=1:
            raise ValueError('Missing or ambiguous pause evidence')
        if 'read_error' in row or (previous is not None and row['qpc'] < previous):
            raise ValueError('Read failure or clock reversed')
        previous = row['qpc']
        function, phase = row['name'].rsplit('_', 1)
        key = (row['tid'], function)
        if phase == 'entry':
            if key in pending:
                raise ValueError('Nested call cannot be paired')
            pending[key] = row
        elif phase == 'exit':
            if key not in pending:
                raise ValueError('Exit without entry')
            entry = pending.pop(key)
            if (row['esp'] != entry['esp'] - 4 or row['ebp'] != row['esp'] or
                    row['stack'][:2] != [entry['ebp'], entry['stack'][0]] or
                    row['eax'] & 0xffff != 1):
                raise ValueError('Call stack or successful return does not match')
            a, b = entry['qpc'], row['qpc']
            held, uncertain = pause_ticks(pauses, a, b)
            result.append(dict(function=function, tid=row['tid'], entry_qpc=a, exit_qpc=b,
                raw_s=(b-a)/frequency, observed_pause_s=held/frequency,
                resume_call_uncertainty_s=uncertain/frequency,
                minus_observed_pause_s=(b-a-held)/frequency))
        else:
            raise ValueError('Unknown breakpoint phase')
    if pending:
        raise ValueError('Entry without exit')
    return result


def in_start(calls, begin, end):
    setups = [c for c in calls if c['function']=='initialize' and
              begin <= c['entry_qpc'] <= c['exit_qpc'] <= end]
    touching=[c for c in calls if c['function']=='initialize' and c['entry_qpc']<=end and c['exit_qpc']>=begin]
    if len(setups) != 2 or len(touching)!=2 or len(set(c['tid'] for c in setups)) != 1:
        raise ValueError('Expected exactly two initializer calls within COM Start')
    return setups
