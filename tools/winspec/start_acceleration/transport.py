"""Hidden, bounded debugger child. No camera Start is issued by this transport."""
import hashlib,json,os,subprocess,sys,time
from .native_identity import modules
from .resident_protocol import publish,analyze_light
from .experiment_analysis import analyze_trace
from .experiment_state import BaselineIneligible
from .winspec_debug_trace import save
from .manager import Unavailable

ROOT=os.path.dirname(os.path.abspath(__file__))
HASHES={
 'pvcam32.dll':'e4bd7810e3ac07599ab2756d97adb6b453e09fd39aff9a3dfb31d96ff50338cc',
 'contrman.dll':'7fedc5ed1c04f7e216610ac7a87c2d47f72e2056bc720c16dfafd00c98448dd4',
 'winspec.exe':'97da37aca86d85df94d0ad8d1e30955d72e86857add570952da5b2066c9c288e',
 'pipp32.dll':'65ae35d89c3b8a105da01f4e63631ddf348292223367674141a3ed92b9b20cb5',
 'pidc32.dll':'8d3a33c41089f6ec0aa03160f336deaf6f02ed08ed5bb0eb4b3eeefb1d5f6547'}
clock=getattr(time,'monotonic',None) or time.clock


def identity():
    import win32com.client
    service=win32com.client.GetObject('winmgmts:')
    pids=[int(p.ProcessId) for p in service.ExecQuery("SELECT ProcessId FROM Win32_Process WHERE Name='WinSpec.exe'")]
    if len(pids)!=1:raise Unavailable('WinSpec process identity is ambiguous')
    loaded=modules(pids[0])
    if set(loaded)!=set(HASHES) or any(loaded[n]['sha256']!=h for n,h in HASHES.items()):
        raise Unavailable('Installed WinSpec binaries do not match verified acceleration build')
    return pids[0],loaded


class Transport(object):
    def __init__(self,output,baseline=None,frame_limit=30,native=None):
        self.output=output;self.baseline=baseline;self.frame_limit=frame_limit
        self.child=None;self.detached=True;self.trace=None;self.command_seq=0
        self.native=native

    def start(self):
        pid,self.modules=identity()
        if self.baseline is not None and (pid!=self.baseline['pid'] or self.modules!=self.baseline['modules']):
            raise Unavailable('WinSpec process or modules changed; full Start required')
        with open(os.path.join(ROOT,'targets.json'),'rb') as handle:targets=json.load(handle)
        if self.baseline is not None:targets=[t for t in targets if t['name']!='output_entry']
        for target in targets:
            base=self.modules[target['module']]['base'];target['address']=base+target['rva']
            if 'redirect_to' in target:target['redirect_to']['address']=base+target['redirect_to']['rva']
        os.makedirs(self.output)
        config=dict(pid=pid,targets=targets,modules=self.modules,module_bases=[m['base'] for m in self.modules.values()],baseline=self.baseline)
        kind='experiment_trace'
        if self.native is not None:config['native_batch']=self.native.debug_config()
        if self.baseline is not None:config.update(frame_limit=self.frame_limit,event_wait_ms=5);kind='resident_trace'
        if self.baseline is not None and self.native is not None:
            kind='combined_trace'
        save(os.path.join(self.output,'config.json'),config)
        info=subprocess.STARTUPINFO();info.dwFlags|=subprocess.STARTF_USESHOWWINDOW;info.wShowWindow=0
        self.detached=False
        with open(os.path.join(self.output,'console.log'),'wb') as console:
            self.child=subprocess.Popen([sys.executable,'-m','start_acceleration.'+kind,os.path.join(self.output,'config.json')],
                cwd=os.path.dirname(ROOT),startupinfo=info,stdout=console,stderr=console)
        deadline=clock()+20
        while not os.path.isfile(os.path.join(self.output,'ready.json')):
            if not self.running() or clock()>deadline:raise RuntimeError('Acceleration debugger not ready')
            time.sleep(.01)

    def running(self):return self.child is not None and self.child.poll() is None

    def stop(self):
        if self.detached:return
        if self.child is None:raise RuntimeError('Debugger launch outcome unknown; retain owner')
        if self.running():
            with open(os.path.join(self.output,'stop'),'wb') as handle:handle.write(b'stop')
            deadline=clock()+20
            while self.running() and clock()<deadline:time.sleep(.01)
        with open(os.path.join(self.output,'trace.json'),'rb') as handle:self.trace=json.load(handle)
        self.detached=(not self.running() and self.trace.get('detached') is True and self.trace.get('debugger_attached_at_return') is False)
        if not self.detached:raise RuntimeError('Debugger detach/restoration unconfirmed; retain owner')
        if self.trace.get('status')!='complete' or self.trace.get('stop_reason') not in ('requested_file','diagnostic_deadline'):
            raise RuntimeError('Debugger session ended abnormally')
        if not self.trace.get('thread_restore_verified') or any(self.trace.get(k) for k in
            ('error','cleanup_error','experiment_errors','telemetry_errors','forwarded_exceptions','module_unloaded','resident_errors')):
            raise RuntimeError('Debugger coverage or restoration failed')

    def build_baseline(self,begin,end):
        self.stop()
        try:a=analyze_trace(self.trace,begin,end,self.modules)
        except BaselineIneligible as error:raise Unavailable(str(error))
        streams=a['output_streams']
        if len(streams['program_1'])!=157 or streams['program_1']!=streams['program_2']:
            raise Unavailable('Full programs are not an eligible matching baseline')
        if self.native is not None:
            nested=[r for r in self.trace['records'] if r['name']=='output_entry' and r['parent_program_qpc'] is not None]
            if len(nested)!=314 or not all(r.get('original_frame_verified') is True for r in nested):
                raise Unavailable('Native original-frame ancestry not observed in full baseline')
        return dict(pid=self.trace['pid'],modules=self.modules,programs=a['programs'],start_begin=begin,start_end=end)

    def command(self,value,expected):
        if not self.running():raise RuntimeError('Debugger coverage ended before frame boundary')
        publish(os.path.join(self.output,'command-%03d.json'%self.command_seq),value)
        path=os.path.join(self.output,'ack-%03d.json'%self.command_seq);deadline=clock()+5
        while not os.path.isfile(path):
            if not self.running() or clock()>deadline:raise RuntimeError('Debugger boundary acknowledgement missing')
            time.sleep(.005)
        with open(path,'rb') as handle:ack=json.load(handle)
        if not self.running() or ack['status']!=expected or ack['frame_id']!=value['frame_id']:
            raise RuntimeError('Debugger boundary acknowledgement differs')
        self.command_seq+=1;return ack

    def open_frame(self,index):return self.command(dict(op='open',frame_id=index),'opened')

    def close_frame(self,index,begin,end):
        result=self.command(dict(op='close',frame_id=index,start_begin=begin,start_end=end),'closed')['analysis']
        # Independent parent-side validation of the durable child evidence.
        with open(os.path.join(self.output,'frame-%03d.json'%index),'rb') as handle:evidence=json.load(handle)
        actual=analyze_light(evidence['trace'],begin,end,self.modules,self.baseline,allow_fallback=True)
        if result!=actual:raise RuntimeError('Parent/child frame decisions differ')
        if self.native is not None:
            batch=evidence['trace']['native_batch'];first,second=batch['programs']
            expected='combined' if second['started'] else ('first-only' if actual['redirect_applied'] else 'ordinary')
            if batch['mode']!=expected or batch['generation']<=0 or batch['batches']!=(5 if second['started'] else 0):
                raise RuntimeError('Native frame decision differs')
            for ordinal,state in enumerate((first,second),1):
                p=evidence['trace']['programs'][ordinal-1]
                if any(state[k] for k in ('fatal','hold','active','uncertain','buffered')):
                    raise RuntimeError('Native frame contains unfinished work')
                if (state['generation'],state['frame'],state['phase'],state['observed_phase'],state['tid'],state['entry_esp'])!=(
                        batch['generation'],batch['frame'],ordinal,ordinal,p['tid'],p['esp']):
                    raise RuntimeError('Native endpoint ownership differs')
                if (state['controller'],state['caller_ebp'],state['armed'],state['mode'])!=(
                        p['controller'],p['ebp'],1,second['mode']):
                    raise RuntimeError('Native endpoint identity or mode differs')
            if actual['redirect_applied']:
                if (first['outputs'],first['headers'],first['others'],first['serial'],first['data'],first['batches'],first['prefix_ok'])!=(2,0,0,0,0,0,1):
                    raise RuntimeError('First skipped prefix differs')
            elif (first['outputs'],first['headers'],first['others'],first['serial'],first['data'],first['batches'])!=(157,2,2,128,0,0):
                raise RuntimeError('Full first program coverage differs')
            if (second['outputs'],second['headers'],second['others'],second['serial'])!=(157,2,2,128):
                raise RuntimeError('Second native output coverage differs')
            if second['started'] and (not actual['redirect_applied'] or second['data']!=120 or second['permit']!=batch['generation']):
                raise RuntimeError('Native packets lack first skip or current authorization')
            live=self.native.gate_state()
            if any(live[k] for k in ('armed','phase','permit','fatal','hold','active','uncertain','buffered')) or live['completed']!=3:
                raise RuntimeError('Native frame is not quiescent after Start')
            if any(live[k]!=second[k] for k in ('generation','frame','outputs','headers','others','serial','data','batches','started')):
                raise RuntimeError('Native live counters differ from suspended exit evidence')
            actual['native_batch']=dict((k,batch[k]) for k in ('mode','generation','frame','batches','outputs_verified'))
        return actual
