"""Original endpoints plus helper-only authorization; uncertain events stay held."""
import ctypes as ct,json,os,sys
from .resident_trace import ResidentTrace
from .combined_gate import EndpointGuard,FIELDS,WRITABLE
from .winspec_debug_trace import U

ERRORS=('experiment_errors','resident_errors','forwarded_exceptions','module_unloaded','telemetry_errors')

class CombinedTrace(ResidentTrace):
    def __init__(self,config,output):
        ResidentTrace.__init__(self,config,output)
        self.k.WriteProcessMemory.argtypes=[U,U,ct.c_void_p,U,ct.POINTER(U)]
        self.k.WriteProcessMemory.restype=ct.c_int
        self.batch_guard=EndpointGuard(config['native_batch'],self.read,self.write_helper)
        self.report['combined_native_gate']=dict(config['native_batch'])

    def write_helper(self,address,raw):
        cfg=self.config['native_batch'];offset=address-cfg['address']
        if (self.pending is None or len(raw)!=4 or offset%4 or
                offset<0 or offset>=120 or FIELDS[offset//4] not in WRITABLE):
            raise RuntimeError('Only helper gate words under a debug event may be written')
        count=U();self.report['helper_memory_write_attempted']=True
        self.check(self.k.WriteProcessMemory(self.process,address,raw,4,ct.byref(count)),'Helper WriteProcessMemory')
        if count.value!=4:raise RuntimeError('Partial helper memory write')
        self.report['process_memory_written']=True

    def retain(self,reason):
        self.batch_guard.retain(reason)
        self.report['combined_recovery_required']=str(reason)
        self.request_stop=True

    def timestamp(self):
        result=ResidentTrace.timestamp(self)
        if result is None and hasattr(self,'batch_guard'):
            self.retain('Debugger clock coverage lost')
            if self.pending is not None:
                raise RuntimeError('Clock failure while debug event suspended; retain before Continue')
            # resume_after occurs after Continue. It cannot undo that release:
            # request_stop obtains the next event and held forbids its release.
            self.report['clock_failed_after_resume']=True
        return result

    def record(self,event,context,index):
        try:
            action=ResidentTrace.record(self,event,context,index)
            self.native_endpoint(event,index)
            return action
        except BaseException as error:
            self.retain(error);raise

    def native_endpoint(self,event,index):
        if any(self.report.get(k) for k in ERRORS):raise RuntimeError('Endpoint coverage failed')
        p=self.programs[-1];name=self.config['targets'][index]['name']
        redirects=self.report.get('redirects_applied',[])[self.mark['redirects']:]
        if name=='program_entry':self.batch_guard.entry(p)
        elif name=='body_entry':self.batch_guard.body(p,self.gate,redirects,bool(self.stepping))
        elif name=='program_exit':self.batch_guard.exit(p,redirects,bool(self.stepping))
        else:raise RuntimeError('Unexpected combined endpoint')

    def continue_event(self,status):
        if self.batch_guard.in_frame and any(self.report.get(k) for k in ERRORS):
            self.retain('Debugger coverage lost while native frame open')
        if self.batch_guard.held:
            raise RuntimeError('Recovery required: debug event must remain suspended')
        return ResidentTrace.continue_event(self,status)

    def instruction_step_completed(self,event,step,context):
        if self.batch_guard.exit_step_pending:
            p=self.programs[-1]
            if (p['ordinal']!=2 or event.tid!=p['tid'] or
                    self.config['targets'][step['index']]['name']!='program_exit'):
                self.retain('Final program exit step identity changed')
                raise RuntimeError('Final program exit step identity changed')
            self.batch_guard.finish_exit_step()
            self.report.setdefault('native_completed',[]).append(self.batch_guard.result)

    def restore(self):
        if self.batch_guard.in_frame:self.retain('Cleanup attempted before verified program completion')
        self.batch_guard.release_check()
        return ResidentTrace.restore(self)

    def detach(self):
        if self.batch_guard.in_frame:self.retain('Detach attempted before verified program completion')
        self.batch_guard.release_check()
        return ResidentTrace.detach(self)

    def frame_view(self):
        result=ResidentTrace.frame_view(self)
        if self.batch_guard.held or self.batch_guard.in_frame or self.batch_guard.result is None:
            raise RuntimeError('Combined native frame was not validated')
        result['native_batch']=self.batch_guard.result
        return result

if __name__=='__main__':
    with open(sys.argv[1],'rb') as handle:config=json.load(handle)
    CombinedTrace(config,os.path.dirname(os.path.abspath(sys.argv[1]))).run()
