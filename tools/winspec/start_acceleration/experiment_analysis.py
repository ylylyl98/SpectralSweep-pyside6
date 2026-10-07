from .communication_state import check_pair
from .experiment_state import Gate,tail_state,normalized
from .experiment_trace import verify_caller
from .timing_analysis import pause_ticks


def compare_runs(baseline_trace,trial_trace,baseline,trial,modules):
    if baseline_trace['pid']!=trial_trace['pid']:raise ValueError('Trial process changed')
    first=trial['programs'][0]
    decision=Gate(baseline['programs'],modules['contrman.dll']['base']).decide(1,first['controller'],first['tid'],first['body']['full_state'])
    if decision['redirect']!=first['body']['decision']['redirect'] or decision['redirect']!=trial['redirect_applied']:raise ValueError('Recorded trial decision differs from independent baseline comparison')
    a=baseline['output_streams'];b=trial['output_streams']
    for phase in ('before_program_1','between_programs','program_2','after_program_2','outside_start'):
        if a[phase]!=b[phase]:raise ValueError('Preserved output stream changed: '+phase)
    p=baseline['programs'][0]
    prefix=[(r['command'],r['value']) for r in baseline_trace['records'] if r['name']=='output_entry' and r['parent_program_qpc']==p['qpc'] and r['qpc']<p['body']['qpc']]
    if prefix!=[(0x40,0x55),(0x40,0xd5)]:raise ValueError('Baseline handshake prefix differs')
    if b['program_1']!=(prefix if decision['redirect'] else a['program_1']):raise ValueError('First program output suppression differs')
    return dict(redirect_applied=decision['redirect'],reason=decision['reason'],first_outputs_removed=len(a['program_1'])-len(b['program_1']),preserved_other_streams=True)


def analyze_trace(trace,begin,end,modules,trial=False):
    if trace.get('experiment_errors') or trace['active_calls']:raise ValueError('Incomplete/error experiment trace')
    programs=trace['programs'];outputs=[]
    if len(programs)!=2 or [r for r in trace['records'] if r['name']=='program_entry']!=programs:raise ValueError('Program record coverage differs')
    if any('read_error' in r for r in trace['records']):raise ValueError('Read error in capture')
    points=[]
    for ordinal,p in enumerate(programs,1):
        verify_caller(p,ordinal,modules);check_pair(p,p['return_row'])
        body=p['body'];ret=p['return_row']
        if not begin<=p['qpc']<body['qpc']<ret['qpc']<=end or ret['eax']!=1 or body['local_success']!=1:raise ValueError('Program/body did not succeed in Start')
        if body['tid']!=p['tid'] or body['ebp']!=p['esp']-4 or body['esp']!=body['ebp']-0x64:raise ValueError('Body frame/thread differs')
        if body['frame_header']!=[p['ebp'],p['stack'][0],p['controller']]:raise ValueError('Body arguments differ')
        if tail_state(body['full_state'])!=normalized(ret['full_state']):raise ValueError('Unexpected post-program state')
        points.extend((p,body,ret))
    if programs[0]['return_row']['qpc']>=programs[1]['qpc'] or programs[0]['controller']!=programs[1]['controller'] or programs[0]['tid']!=programs[1]['tid']:raise ValueError('Program identity/order differs')
    for row in trace['records']:
        if row['name']=='program_entry':continue
        if row['name']!='output_entry':raise ValueError('Unexpected root record')
        if row['port']!=programs[0]['body']['full_state']['pipp']['object']:raise ValueError('Output addressed a different port')
        points.append(row);q=row['qpc'];parent=row['parent_program_qpc']
        enclosing=[i for i,p in enumerate(programs) if p['qpc']<q<p['return_row']['qpc']]
        if enclosing:
            p=programs[enclosing[0]]
            if parent!=p['qpc'] or row['tid']!=p['tid'] or row['port']!=p['body']['full_state']['pipp']['object']:raise ValueError('Output program attribution differs')
            phase='program_%d'%(enclosing[0]+1)
        elif parent is not None:raise ValueError('Output parent outside program')
        elif q<begin or q>end:phase='outside_start'
        elif q<programs[0]['qpc']:phase='before_program_1'
        elif q<programs[1]['qpc']:phase='between_programs'
        else:phase='after_program_2'
        outputs.append(dict(phase=phase,command=row['command'],value=row['value'],qpc=q))
    clocks=[(p['tid'],p['qpc']) for p in points]
    if len(set(clocks))!=len(clocks):raise ValueError('Repeated endpoint clock')
    pause_ticks(trace['pauses'],min(p['qpc'] for p in points),max(p['qpc'] for p in points))
    for p in points:
        if len([x for x in trace['pauses'] if x['code']==1 and x['tid']==p['tid'] and x['received']==p['qpc']])!=1:raise ValueError('Missing endpoint pause')
    expected=dict(program_entry=2,body_entry=2,program_exit=2,output_entry=len(outputs))
    if trace['target_hits']!=expected or trace['breakpoint_hits']!=len(points) or trace['buffered_log'].count('owned_step_completed')!=len(points):raise ValueError('Breakpoint/step coverage differs')
    decisions=[p['body']['decision']['redirect'] for p in programs];applied=trace.get('redirects_applied',[])
    if not trial:
        if any(decisions) or applied or trace['control_flow_modified']:raise ValueError('Baseline modified control flow')
        Gate(programs,modules['contrman.dll']['base'])
    else:
        if decisions[1] or len(applied)!=int(decisions[0]) or trace['control_flow_modified']!=decisions[0]:raise ValueError('Unexpected redirect count')
        if applied and (applied[0]['tid']!=programs[0]['tid'] or applied[0]['destination']!=modules['contrman.dll']['base']+0xc6d89):raise ValueError('Unexpected redirect target')
    streams={phase:[(r['command'],r['value']) for r in outputs if r['phase']==phase]
        for phase in ('before_program_1','program_1','between_programs','program_2','after_program_2','outside_start')}
    return dict(programs=programs,output_streams=streams,redirect_applied=bool(applied))
