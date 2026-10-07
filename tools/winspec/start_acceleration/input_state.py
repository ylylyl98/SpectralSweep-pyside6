"""Read-only input-call pairing and bounded snapshots of known allocations."""
import binascii
from .communication_state import read_word,check_pair,snapshot,transport_snapshot
from .timing_analysis import pause_ticks

NAMES=('program_entry','program_exit','input_entry','input_exit')


def region(read,address,size):
    if not 0x10000<=address<address+size<0x80000000:raise ValueError('Invalid region')
    chunks=[]
    for offset in range(0,size,2048):
        count=min(2048,size-offset);raw=read(address+offset,count)
        if len(raw)!=count:raise ValueError('Short region read')
        chunks.append(raw)
    return dict(address=address,hex=binascii.hexlify(b''.join(chunks)).decode('ascii'))


def full_snapshot(read,controller):
    settings=read_word(read,controller+0x67c);mirror=read_word(read,settings+0x48)
    port=read_word(read,controller+0x6e58);slots={}
    for offset in (0x14,0x18,0x114,0x11c):
        target=read_word(read,port+offset)
        slots[hex(offset)]=dict(target=target,bytes_hex=region(read,target,8)['hex'])
    return dict(regions=dict(controller=region(read,controller,0x7530),settings=region(read,settings,0x4a8),mirror=region(read,mirror,0x3c)),
        pipp=dict(object=port,slots=slots),pidc=transport_snapshot(read,controller))


class InputTracker(object):
    def __init__(self,base=None,capture_full=False):
        self.base=base;self.capture_full=capture_full;self.controller=None;self.port=None;self.active={}
        self.outside=dict(entered=0,completed=0,groups={},endpoint_clocks=[])

    def record(self,row,read,records):
        name=row['name'];tid=row['tid'];pending=self.active.get(tid,[])
        if name not in NAMES:raise ValueError('Unknown measured function')
        if name.endswith('_exit'):
            if not pending:raise ValueError('Return without entry')
            entry=pending[-1];check_pair(entry,row)
            if name=='program_exit':
                row['state']=snapshot(read,entry['controller'])
                if self.capture_full:row['full_state']=full_snapshot(read,entry['controller'])
            if entry.get('outside'):
                key='%d:%d:%d'%(entry['port'],entry['command'],row['eax'])
                self.outside['groups'][key]=self.outside['groups'].get(key,0)+1
                self.outside['completed']+=1
                self.outside['endpoint_clocks'].append([entry['tid'],entry['qpc'],row['qpc']])
            else:entry['return']=row
            pending.pop()
            if not pending:del self.active[tid]
            return
        if any(p['name']==name for p in pending):raise ValueError('Recursive measured call')
        if name=='program_entry':
            controller=row['stack'][1]
            if self.controller is not None and controller!=self.controller:raise ValueError('Controller changed')
            if pending:raise ValueError('Program nested in input')
            if self.base is not None:
                for offset,rva in ((0xc,0xe30d),(0x618,0xc65fa)):
                    if read_word(read,controller+offset)!=self.base+rva:raise ValueError('Dispatch identity differs')
            state=snapshot(read,controller)
            if self.port is not None and self.port!=state['port']:raise ValueError('Program port changed')
            self.controller=controller;self.port=state['port']
            row.update(controller=controller,state=state)
            if self.capture_full:row['full_state']=full_snapshot(read,controller)
        else:
            row.update(port=row['stack'][1],command=row['stack'][2])
            parent=[p for p in pending if p['name']=='program_entry']
            row['outside']=not parent
            if parent:
                if row['port']!=self.port:raise ValueError('Nested input port changed')
                row.update(parent_program_qpc=parent[0]['qpc'],controller=self.controller)
            else:
                if self.outside['entered']>=4096:raise ValueError('Outside input bound exceeded')
                self.outside['entered']+=1
        self.active.setdefault(tid,[]).append(row)
        if not row.get('outside'):row['index']=len(records);records.append(row)


def analyze_calls(records,begin,end):
    programs=[];inputs=[];previous=None
    for row in records:
        if 'read_error' in row or row['name'] not in ('program_entry','input_entry') or 'return' not in row:raise ValueError('Incomplete measured call')
        if previous is not None and row['qpc']<previous:raise ValueError('Entry clock reversed')
        previous=row['qpc'];check_pair(row,row['return'])
        (programs if row['name']=='program_entry' else inputs).append(row)
    if len(programs)!=2 or len(set(p['tid'] for p in programs))!=1 or len(set(p['controller'] for p in programs))!=1:raise ValueError('Expected two programs')
    if not all(begin<=p['qpc']<p['return']['qpc']<=end and p['return']['eax']==1 for p in programs):raise ValueError('Program failed or outside Start')
    if programs[0]['return']['qpc']>=programs[1]['qpc']:raise ValueError('Program overlap')
    for row in inputs:
        parents=[p for p in programs if p['qpc']==row['parent_program_qpc'] and p['tid']==row['tid'] and p['qpc']<row['qpc']<row['return']['qpc']<p['return']['qpc']]
        if len(parents)!=1 or row['port']!=parents[0]['state']['port'] or row['controller']!=parents[0]['controller']:raise ValueError('Input nesting or identity invalid')
    return dict(programs=programs,inputs=inputs)


def validate_capture(trace,begin,end):
    result=analyze_calls(trace['records'],begin,end)
    outside=trace['outside_inputs'];count=outside['completed']
    if trace['active_calls'] or count!=outside['entered'] or count!=sum(outside['groups'].values()) or count!=len(outside['endpoint_clocks']):raise ValueError('Outside call coverage differs')
    endpoints=[(e['tid'],e['qpc']) for row in trace['records'] for e in (row,row['return'])]
    for tid,a,b in outside['endpoint_clocks']:
        if a>=b:raise ValueError('Outside clock reversed')
        endpoints.extend([(tid,a),(tid,b)])
    if len(set(endpoints))!=len(endpoints):raise ValueError('Duplicate endpoint')
    if not 1<=len(trace['records'])<=512 or not 0<=count<=4096:raise ValueError('Capture bound exceeded')
    pause_ticks(trace['pauses'],min(q for _,q in endpoints),max(q for _,q in endpoints))
    for tid,q in endpoints:
        if len([p for p in trace['pauses'] if p['code']==1 and p['tid']==tid and p['received']==q])!=1:raise ValueError('Missing endpoint pause')
    hits=dict(program_entry=2,program_exit=2,input_entry=len(result['inputs'])+count,input_exit=len(result['inputs'])+count)
    if trace['target_hits']!=hits or trace['breakpoint_hits']!=len(endpoints) or trace['completed_calls']!=len(endpoints)//2:raise ValueError('Hit coverage differs')
    if trace['buffered_log'].count('owned_step_completed')!=len(endpoints):raise ValueError('Step coverage differs')
    return result
