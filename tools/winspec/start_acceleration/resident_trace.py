"""One debugger session, explicit per-Start gates, three endpoint breakpoints."""
import json, os, sys
from .experiment_trace import ExperimentTrace
from .resident_protocol import Boundary, publish
from .winspec_debug_trace import qpc


class ResidentTrace(ExperimentTrace):
    def __init__(self, config, output):
        if [t['name'] for t in config['targets']] != ['program_entry','body_entry','program_exit']:
            raise ValueError('Resident trace requires three frozen endpoints')
        self.boundary=Boundary(config['baseline'],config['pid'],config['modules'],config['frame_limit'],allow_fallback=True)
        self.command_seq=0;self.request_stop=False
        ExperimentTrace.__init__(self,config,output)
        self.gate=None
        self.report.update(frames=[],output_stream_observed=False,resident=True)

    def fault(self, error):
        self.gate=None;self.boundary.disable(error);self.request_stop=True
        self.report.setdefault('resident_errors',[]).append(str(error))

    def deadline_ready(self):
        # An idle session can expire; never silently lose an opened frame's coverage.
        return self.boundary.current is None and not self.active and not self.stepping

    def service(self):
        # Never escape into base failure cleanup without a suspended debug event.
        if self.boundary.failed or self.pending is not None or self.stepping:
            return
        try:
            path=os.path.join(self.output,'command-%03d.json'%self.command_seq)
            if not os.path.isfile(path):return
            with open(path,'rb') as handle:command=json.load(handle)
            frame_id=command['frame_id'];op=command['op'];now=qpc()
            if isinstance(frame_id,bool) or not isinstance(frame_id,int):raise ValueError('Invalid frame ID')
            if op=='open':
                if self.report.get('experiment_errors') or self.report.get('forwarded_exceptions') or self.report.get('module_unloaded'):
                    raise ValueError('Debugger coverage lost')
                for target in self.config['targets']:
                    for endpoint in [target]+([target['redirect_to']] if 'redirect_to' in target else []):
                        if list(bytearray(self.read(endpoint['address'],len(endpoint['first_bytes'])))) != endpoint['first_bytes']:
                            raise ValueError('Loaded endpoint bytes changed')
                self.gate=self.boundary.open(frame_id,now,bool(self.active),bool(self.stepping))
                self.programs=[];self.report['programs']=self.programs
                self.mark=dict(records=len(self.records),pauses=len(self.report['pauses']),logs=len(self.buffered_log),
                    redirects=len(self.report.get('redirects_applied',[])),hits=self.report['breakpoint_hits'],
                    target_hits=dict(self.report['target_hits']))
                result=dict(status='opened',frame_id=frame_id,opened_qpc=now)
            elif op=='close':
                view=self.frame_view()
                analysis=self.boundary.close(frame_id,view,command['start_begin'],command['start_end'],now,bool(self.active),bool(self.stepping))
                self.gate=None
                evidence=dict(frame_id=frame_id,trace=view,start_begin=command['start_begin'],
                    start_end=command['start_end'],closed_qpc=now,analysis=analysis)
                publish(os.path.join(self.output,'frame-%03d.json'%frame_id),evidence)
                self.report['frames'].append(evidence)
                result=dict(status='closed',frame_id=frame_id,closed_qpc=now,analysis=analysis)
            else:raise ValueError('Unknown boundary command')
            publish(os.path.join(self.output,'ack-%03d.json'%self.command_seq),result)
            self.command_seq+=1
        except BaseException as error:
            self.fault(error)

    def frame_view(self):
        mark=self.mark
        result=dict((key,self.report[key]) for key in ('pid','qpc_frequency') if key in self.report)
        for key in ('experiment_errors','telemetry_errors','forwarded_exceptions','module_unloaded','resident_errors'):
            if self.report.get(key):result[key]=self.report[key]
        redirects=self.report.get('redirects_applied',[])[mark['redirects']:]
        result.update(programs=self.programs,active_calls=dict(self.active),records=self.records[mark['records']:],
            pauses=self.report['pauses'][mark['pauses']:],buffered_log=self.buffered_log[mark['logs']:],
            breakpoint_hits=self.report['breakpoint_hits']-mark['hits'],
            target_hits=dict((k,v-mark['target_hits'].get(k,0)) for k,v in self.report['target_hits'].items() if v!=mark['target_hits'].get(k,0)),
            redirects_applied=redirects,control_flow_modified=bool(redirects))
        return result

    def record(self,event,context,index):
        if self.boundary.failed:return None
        if self.boundary.current is None:
            self.fault('Program outside an explicitly opened Start');return None
        action=ExperimentTrace.record(self,event,context,index)
        if self.report.get('experiment_errors'):
            self.fault('Endpoint validation failed');return None
        return action

    def continue_event(self,status):
        ExperimentTrace.continue_event(self,status)
        if self.report.get('forwarded_exceptions') or self.report.get('module_unloaded') or self.report.get('telemetry_errors'):
            self.fault('Exception, module unload, or clock coverage lost')


if __name__=='__main__':
    with open(sys.argv[1],'rb') as handle:config=json.load(handle)
    ResidentTrace(config,os.path.dirname(os.path.abspath(sys.argv[1]))).run()
