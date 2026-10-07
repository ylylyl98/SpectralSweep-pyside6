"""Isolated first-body experiment; no code patches or cached state writes."""
import ctypes as ct,json,os,struct,sys
from .winspec_debug_trace import Trace,qpc
from .experiment_state import capture,Gate
from .communication_state import check_pair,read_word


def frequency():
    v=ct.c_int64()
    if not ct.windll.kernel32.QueryPerformanceFrequency(ct.byref(v)) or v.value<=0:raise RuntimeError('QPC unavailable')
    return v.value


def verify_caller(row,ordinal,modules):
    if ordinal not in (1,2):raise ValueError('Unexpected program count')
    cm=modules['contrman.dll']['base'];pvc=modules['pvcam32.dll']['base'];ws=modules['winspec.exe']['base']
    middle=(0x17cea6,0x17ca6a) if ordinal==1 else (0x17d737,0x17cbcf)
    expected=[pvc+0x193e7,pvc+0x1a97d,ws+0x170bb4,ws+middle[0],ws+middle[1],ws+0x19d374,ws+0xf6e0c,ws+0xf6d5d]
    chain=row['caller_chain']
    if row['stack'][0]!=cm+0xde32 or [x['return_address'] for x in chain]!=expected:raise ValueError('Program caller family differs')
    if chain[0]['argument0']!=row['controller']:raise ValueError('Initializer controller differs')
    pointer=row['ebp']
    for frame in chain:
        if not row['esp']<pointer==frame['frame']<frame['saved_frame']<row['esp']+1048576:raise ValueError('Caller frame link differs')
        pointer=frame['saved_frame']
    if len(set(x['argument0'] for x in chain[3:6]))!=1:raise ValueError('WinSpec adapter ancestry differs')


class ExperimentTrace(Trace):
    def __init__(self,config,output):
        self.buffered_log=[];self.active={};self.programs=[];self.gate=None
        if config.get('baseline') is not None:
            baseline=config['baseline']
            if baseline['pid']!=config['pid'] or baseline['modules']!=config['modules']:raise ValueError('Baseline process/modules changed')
            for i,p in enumerate(baseline['programs'],1):
                verify_caller(p,i,config['modules'])
                if not baseline['start_begin']<=p['qpc']<p['return_row']['qpc']<=baseline['start_end']:raise ValueError('Baseline programs not in one Start')
            self.gate=Gate(baseline['programs'],config['modules']['contrman.dll']['base'])
        Trace.__init__(self,config,output)
        self.report.update(pauses=[],buffered_log=self.buffered_log,qpc_frequency=frequency(),active_calls=self.active,
            breakpoint_hits=0,target_hits={},programs=self.programs,control_flow_modified=False,
            tick_begin=ct.windll.kernel32.GetTickCount(),qpc_begin=qpc())

    def log(self,message):self.buffered_log.append(message)

    def timestamp(self):
        try:return qpc()
        except BaseException as error:
            self.report.setdefault('telemetry_errors',[]).append(str(error));return None

    def record(self,event,context,index):
        name=self.config['targets'][index]['name']
        row=dict(name=name,tid=event.tid,qpc=self.event_received,eip=context.Eip,esp=context.Esp,ebp=context.Ebp,eax=context.Eax)
        self.report['breakpoint_hits']+=1
        self.report['target_hits'][name]=self.report['target_hits'].get(name,0)+1
        try:
            row['stack']=list(struct.unpack('<8I',self.read(context.Esp,32)))
            if name=='program_entry':
                if self.active:raise ValueError('Overlapping programs')
                row.update(controller=row['stack'][1],ordinal=len(self.programs)+1,caller_chain=[])
                pointer=context.Ebp
                for depth in range(8):
                    if not context.Esp<pointer<context.Esp+1048576:raise ValueError('Caller frame outside bound')
                    saved,ret,arg=struct.unpack('<3I',self.read(pointer,12))
                    row['caller_chain'].append(dict(frame=pointer,saved_frame=saved,return_address=ret,argument0=arg));pointer=saved
                verify_caller(row,row['ordinal'],self.config['modules'])
                if read_word(self.read,row['controller']+0x618)!=self.config['modules']['contrman.dll']['base']+0xc65fa:raise ValueError('Program dispatch changed')
                row['full_state']=capture(self.read,row['controller'])
                self.active[event.tid]=row;self.programs.append(row)
            elif name=='body_entry':
                p=self.active[event.tid]
                if 'body' in p:raise ValueError('Repeated body entry')
                row['local_success']=read_word(self.read,context.Ebp-0x14)
                row['frame_header']=list(struct.unpack('<3I',self.read(context.Ebp,12)))
                if context.Ebp!=p['esp']-4 or context.Esp!=context.Ebp-0x64 or row['frame_header']!=[p['ebp'],p['stack'][0],p['controller']]:raise ValueError('Body frame differs')
                if row['local_success']!=1:raise ValueError('Program success local differs')
                row['full_state']=capture(self.read,p['controller']);p['body']=row
                row['decision']=dict(redirect=False,reason='Baseline trace') if self.gate is None else self.gate.decide(p['ordinal'],p['controller'],event.tid,row['full_state'])
                if row['decision']['redirect']:
                    return dict(entry_esp=p['esp'],return_address=p['stack'][0],controller=p['controller'])
                return
            elif name=='program_exit':
                p=self.active[event.tid];check_pair(p,row)
                row['full_state']=capture(self.read,p['controller']);p['return_row']=row;del self.active[event.tid]
                return
            elif name=='output_entry':
                row.update(port=row['stack'][1],command=row['stack'][2],value=row['stack'][3],
                    parent_program_qpc=self.active[event.tid]['qpc'] if event.tid in self.active else None)
                if self.config.get('native_batch') and event.tid in self.active:
                    p=self.active[event.tid];pointer=context.Ebp;target=p['esp']-4
                    for depth in range(64):
                        if pointer%4 or not context.Ebp<=pointer<=target or pointer-context.Ebp>=1048576:
                            raise ValueError('Original program frame ancestry outside native scope bound')
                        saved,ret,arg=struct.unpack('<3I',self.read(pointer,12))
                        if pointer==target:
                            if (saved,ret,arg)!=(p['ebp'],p['stack'][0],p['controller']):
                                raise ValueError('Original program frame ancestry differs')
                            row['original_frame_verified']=True;break
                        if not pointer<saved<=target:raise ValueError('Original program frame ancestry link differs')
                        pointer=saved
                    else:raise ValueError('Original program frame ancestry depth exceeded')
            else:raise ValueError('Unknown target')
        except BaseException as error:
            row['read_error']=str(error)
            # Any read/identity error disables all later omission decisions.
            self.gate=None;self.report.setdefault('experiment_errors',[]).append(str(error))
        row['index']=len(self.records);self.records.append(row)

    def continue_event(self,status):
        row=dict(received=self.event_received,code=self.pending.code,tid=self.pending.tid,resume_before=self.timestamp())
        Trace.continue_event(self,status);row['resume_after']=self.timestamp();self.report['pauses'].append(row)
        self.report.update(tick_end=ct.windll.kernel32.GetTickCount(),qpc_end=row['resume_after'])


if __name__=='__main__':
    with open(sys.argv[1],'rb') as h:config=json.load(h)
    ExperimentTrace(config,os.path.dirname(os.path.abspath(sys.argv[1]))).run()
