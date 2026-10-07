"""Read-only call pairing. One record retains both entry and return contexts."""
import binascii,struct
from .timing_analysis import pause_ticks

NAMES=('program_entry','program_exit','output_entry','output_exit')
FIELDS=dict(count=0x67d8,flags=0x6c20,adjustment=0x6cb0,status=0x6e34,port=0x6e58,stop_active=0x6f94)


def snapshot(read,controller):
    if not 0x10000<=controller<controller+0x7530<0x80000000:raise ValueError('Invalid controller address')
    data=read(controller+0x67d8,0x7c0)
    if len(data)!=0x7c0:raise ValueError('Short controller read')
    return dict((name,struct.unpack_from('<I',data,offset-0x67d8)[0]) for name,offset in FIELDS.items())


def check_pair(entry,row):
    if (row['name']!=entry['name'].replace('_entry','_exit') or row['tid']!=entry['tid'] or
        row['esp']!=entry['esp']-4 or row['ebp']!=row['esp'] or row['stack'][:2]!=[entry['ebp'],entry['stack'][0]] or
        row['qpc']<=entry['qpc']):raise ValueError('Return frame or call order mismatch')


def read_word(read,address):
    if not 0x10000<=address<address+4<0x80000000:raise ValueError('Invalid word pointer')
    return struct.unpack('<I',read(address,4))[0]


def transport_snapshot(read,controller):
    obj=read_word(read,controller+0x74cc)
    if not 0x10000<=obj<obj+0x1144<0x80000000:raise ValueError('Invalid PIDC object pointer')
    slots={}
    for offset in (0x10f0,0x1140):
        target=read_word(read,obj+offset)
        if not 0x10000<=target<target+8<0x80000000:raise ValueError('Invalid PIDC function pointer')
        raw=read(target,8)
        if len(raw)!=8:raise ValueError('Short PIDC function read')
        slots[hex(offset)]=dict(target=target,bytes_hex=binascii.hexlify(raw).decode('ascii'))
    return dict(object=obj,buffer=read_word(read,obj+0xa0),parameter_1b=read_word(read,obj+0x8c),slots=slots)


class CommunicationTracker(object):
    def __init__(self,contrman_base=None,pvcam_base=None,capture_transport=False):
        self.base=contrman_base;self.pvcam_base=pvcam_base;self.capture_transport=capture_transport
        self.active={};self.controller=None

    def record(self,row,read,records):
        name=row['name'];tid=row['tid'];pending=self.active.get(tid,[])
        if name not in NAMES:raise ValueError('Unknown measured function')
        if name.endswith('_exit'):
            if not pending:raise ValueError('Return without owned entry')
            entry=pending[-1];check_pair(entry,row)
            if name=='program_exit':
                row['state']=snapshot(read,entry['controller'])
                if self.capture_transport:row['transport']=transport_snapshot(read,entry['controller'])
            entry['return']=row
            pending.pop()
            if not pending:del self.active[tid]
            return
        if any(r['name']==name for r in pending):raise ValueError('Recursive measured call unsupported')
        controller=None;source='unknown'
        if name=='program_entry':controller=row['stack'][1];source='program_argument'
        else:
            row.update(port=row['stack'][1],command=row['stack'][2],value=row['stack'][3])
            chain=row.get('caller_chain',[])
            for i,frame in enumerate(chain[:-1]):
                if (self.base is not None and self.pvcam_base is not None and frame['return_address']==self.base+0xc58d and
                    chain[i+1]['return_address']==self.pvcam_base+0x19250):
                    parent=chain[i+1]
                    if frame['saved_frame']!=parent['frame'] or frame['argument0']!=parent['argument0']:
                        raise ValueError('Broken exposure caller link or controller disagreement')
                    controller=parent['argument0'];source='exposure_frame'
                    for offset,rva in ((0x20,0xc4f4),(0x1a0,0xc7d8c)):
                        if read_word(read,controller+offset)!=self.base+rva:raise ValueError('Exposure dispatch identity differs')
                    break
                if self.base is not None and frame['return_address']==self.base+0xe31e:
                    parent=chain[i+1]
                    if frame['saved_frame']!=parent['frame'] or frame['argument0']!=parent['argument0']:
                        raise ValueError('Broken Stop caller link or controller disagreement')
                    origin=None
                    if self.pvcam_base is not None and parent['return_address']==self.pvcam_base+0x19360:
                        origin='pvcam_setup'
                    elif self.pvcam_base is not None and parent['return_address']==self.base+0xde05 and i+2<len(chain):
                        grandparent=chain[i+2]
                        if (parent['saved_frame']!=grandparent['frame'] or grandparent['argument0']!=parent['argument0'] or
                            grandparent['return_address']!=self.pvcam_base+0x193e7):raise ValueError('Broken initializer Stop ancestry')
                        origin='native_initializer'
                    row['stop_origin']=origin or 'other_stop'
                    if origin is not None:controller=parent['argument0'];source='stop_frame'
                    break
            parents=[p for p in pending if p['name']=='program_entry']
            row['parent_program_qpc']=parents[0]['qpc'] if parents else None
        if controller is not None:
            if self.controller is not None and controller!=self.controller:raise ValueError('Controller identity changed')
            if self.base is not None:
                for offset,rva in ((0xc,0xe30d),(0x4dc,0xc7141),(0x618,0xc65fa)):
                    if read_word(read,controller+offset)!=self.base+rva:raise ValueError('Native dispatch identity differs')
            self.controller=controller
        elif self.controller is not None:controller=self.controller;source='previous_verified_controller'
        row.update(controller=controller,controller_source=source,state=None)
        if controller is not None:
            row['state']=snapshot(read,controller)
            if name=='output_entry' and row['state']['port']!=row['port']:raise ValueError('Physical port identity differs')
            if name=='program_entry' and self.capture_transport:row['transport']=transport_snapshot(read,controller)
        self.active.setdefault(tid,[]).append(row)
        records.append(row)


def analyze_calls(records,begin,end):
    programs=[];outputs=[];previous=None
    for row in records:
        if 'read_error' in row or row['name'] not in ('program_entry','output_entry') or 'return' not in row:
            raise ValueError('Missing complete call record')
        if previous is not None and row['qpc']<previous:raise ValueError('Entry clock reversed')
        previous=row['qpc'];check_pair(row,row['return'])
        if row['name']=='program_entry':programs.append(row)
        else:outputs.append(row)
    if len(programs)!=2 or len(set(p['tid'] for p in programs))!=1 or len(set(p['controller'] for p in programs))!=1:
        raise ValueError('Expected two program calls on one controller/thread')
    if not all(begin<=p['qpc']<p['return']['qpc']<=end and p['return']['eax']==1 for p in programs):
        raise ValueError('Program not successful or outside Start')
    if programs[0]['return']['qpc']>=programs[1]['qpc']:raise ValueError('Overlapping program calls')
    controller=programs[0]['controller'];port=programs[0]['state']['port']
    if any(p['state']['port']!=port or p['return']['state']['port']!=port for p in programs):
        raise ValueError('Program port changed')
    classified=[]
    for o in outputs:
        a,b=o['qpc'],o['return']['qpc'];overlap=[]
        for i,p in enumerate(programs):
            if a<p['return']['qpc'] and b>p['qpc']:
                if not p['qpc']<a<b<p['return']['qpc'] or o.get('parent_program_qpc')!=p['qpc'] or o['tid']!=p['tid']:
                    raise ValueError('Output overlaps program without a matching nested frame')
                overlap.append(i)
        if len(overlap)>1:raise ValueError('Output spans both programs')
        if overlap:phase='program_%d'%(overlap[0]+1)
        elif b<=begin or a>=end:phase='outside_start'
        elif not begin<=a<b<=end:raise ValueError('Output crosses Start boundary')
        elif b<programs[0]['qpc']:phase='before_program_1'
        elif programs[0]['return']['qpc']<a<b<programs[1]['qpc']:phase='between_programs'
        elif a>programs[1]['return']['qpc']:phase='after_program_2'
        else:raise ValueError('Unclassified output boundary')
        if phase!='outside_start' and (o['controller']!=controller or o['port']!=port or o['state'] is None or o['state']['port']!=port):
            raise ValueError('Output controller/state/port attribution missing or inconsistent')
        classified.append(dict(phase=phase,entry_qpc=a,exit_qpc=b,tid=o['tid'],port=o['port'],command=o['command'],value=o['value'],
            return_value=o['return']['eax'],state=o['state'],controller=o['controller'],controller_source=o['controller_source'],
            caller=o['stack'][0],caller_chain=o.get('caller_chain',[]),stop_origin=o.get('stop_origin')))
    return dict(programs=programs,outputs=classified,program_returns=[p['return']['eax'] for p in programs],
        output_returns=[o['return']['eax'] for o in outputs])


def validate_capture(trace,begin,end):
    result=analyze_calls(trace['records'],begin,end)
    endpoints=[e for row in trace['records'] for e in (row,row['return'])]
    if not 1<=len(trace['records'])<=512:raise ValueError('Call-record bound exceeded')
    pause_ticks(trace['pauses'],min(e['qpc'] for e in endpoints),max(e['qpc'] for e in endpoints))
    hits={}
    for e in endpoints:
        hits[e['name']]=hits.get(e['name'],0)+1
        if len([p for p in trace['pauses'] if p['code']==1 and p['tid']==e['tid'] and p['received']==e['qpc']])!=1:
            raise ValueError('Missing or duplicate endpoint pause')
    if trace['target_hits']!=hits or trace['breakpoint_hits']!=len(endpoints) or trace['completed_calls']!=len(trace['records']):
        raise ValueError('Hit/completion coverage differs')
    if trace['buffered_log'].count('owned_step_completed')!=len(endpoints):raise ValueError('Instruction step coverage differs')
    return result
