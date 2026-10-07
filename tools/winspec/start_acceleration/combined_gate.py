"""Debugger-owned authorization of a fixed-build, helper-owned gate only.

No camera/controller memory is written. All mutations occur under a suspended
debug event. An uncertain write is terminal; retention precedes best-effort I/O.
"""
import struct
from .experiment_state import normalized,tail_state

MAGIC=0x42534754
FIELDS=('magic version size generation frame armed mode phase tid entry_esp caller_ebp '
        'controller permit completed hold observed_phase programs outputs headers others '
        'serial data buffered batches started candidate fatal active uncertain prefix_ok').split()
WRITABLE=set('phase tid entry_esp caller_ebp permit completed hold'.split())

class EndpointGuard(object):
    def __init__(self,config,read,write):
        self.config=config;self.read=read;self.write=write;self.held=False
        self.reason=None;self.in_frame=False;self.token=None;self.first_skipped=False
        self.last_generation=config['generation'];self.result=None;self.program_states=[]
        self.exit_step_pending=False
        a=config['address'];lo=config['dll_base'];hi=lo+config['dll_size']
        if not 0x10000<=lo<=a<a+120<=hi<0x80000000 or a%4:
            raise ValueError('Helper gate address outside verified DLL')

    def state(self):
        s=dict(zip(FIELDS,struct.unpack('<30I',self.read(self.config['address'],120))))
        if (s['magic'],s['version'],s['size'],s['controller'])!=(MAGIC,1,120,self.config['controller']):
            raise ValueError('Helper gate identity differs')
        if s['fatal'] or s['hold'] or s['uncertain'] or s['active']:
            raise ValueError('Helper is not quiescent and healthy')
        return s

    def change(self,**fields):
        if self.held:raise RuntimeError('Retained debug event')
        if not set(fields)<=WRITABLE:raise ValueError('Attempt to write native-owned field')
        # Publish phase last and permission only after all other checks. The OS
        # suspends every target thread throughout this transaction.
        names=sorted(fields,key=lambda k:(k in ('phase','permit'),k))
        try:
            for k in names:
                a=self.config['address']+FIELDS.index(k)*4;raw=struct.pack('<I',fields[k])
                self.write(a,raw)
                if self.read(a,4)!=raw:raise RuntimeError('Helper write readback differs')
        except BaseException:
            self.retain('Helper write completion/readback uncertain');raise

    def retain(self,reason):
        # Never allow a failed marker write to reach ordinary debugger cleanup.
        self.held=True;self.reason=self.reason or str(reason)
        try:
            a=self.config['address']+FIELDS.index('hold')*4
            self.write(a,struct.pack('<I',1))
        except BaseException:pass

    def release_check(self):
        if self.held or self.in_frame:
            raise RuntimeError('Combined Start recovery required; retain debug event and owner')

    def checked(self,p):
        s=self.state()
        if (not self.in_frame or s['generation']!=self.token or s['armed']!=1 or
                s['phase']!=p['ordinal'] or s['observed_phase']!=p['ordinal'] or
                s['programs']!=p['ordinal'] or s['tid']!=p['tid'] or
                s['entry_esp']!=p['esp'] or s['caller_ebp']!=p['ebp']):
            raise ValueError('Native program scope differs')
        return s

    @staticmethod
    def prefix(s):
        if (s['outputs'],s['headers'],s['others'],s['serial'],s['data'],s['buffered'],
                s['batches'],s['started'],s['prefix_ok'])!=(2,0,0,0,0,0,0,0,1):
            raise ValueError('Exactly two successful handshake outputs required')

    def entry(self,p):
        s=self.state();ordinal=p['ordinal']
        if ordinal==1:
            if (self.in_frame or not self.last_generation<s['generation']<0xffffffff or
                    s['frame']==0 or s['armed']!=1 or s['mode'] not in (0,1) or
                    any(s[k] for k in ('phase','permit','completed','observed_phase','programs','outputs',
                                      'headers','others','serial','data','buffered','batches','started'))):
                raise ValueError('Fresh, nonzero, unpermitted native frame required')
            self.token=s['generation'];self.last_generation=self.token;self.in_frame=True
            self.first_skipped=False;self.result=None;self.program_states=[]
        elif ordinal==2:
            if (not self.in_frame or s['generation']!=self.token or s['armed']!=1 or
                    s['phase']!=0 or s['completed']!=1 or s['permit']):
                raise ValueError('First program exit not validated')
        else:raise ValueError('Unexpected native program count')
        if p['controller']!=s['controller']:raise ValueError('Native controller differs')
        self.change(tid=p['tid'],entry_esp=p['esp'],caller_ebp=p['ebp'],phase=ordinal)

    def body(self,p,gate,redirects,stepping):
        s=self.checked(p);self.prefix(s)
        if stepping:raise ValueError('Previous instruction step incomplete')
        if p['ordinal']==1:return
        if self.first_skipped:
            if (len(redirects)!=1 or redirects[0]['tid']!=p['tid'] or gate is None or
                    normalized(p['body']['full_state'])!=gate.expected):
                raise ValueError('Second body lacks confirmed first skip or matching current state')
            if s['mode']==1:self.change(permit=self.token)
        elif s['permit']:raise ValueError('Permission without first skip')

    def exit(self,p,redirects,stepping):
        s=self.checked(p);body=p['body'];end=p['return_row']
        if (stepping or end['eax']!=1 or
                normalized(end['full_state'])!=tail_state(body['full_state'])):
            raise ValueError('Program did not reach its successful natural tail')
        if p['ordinal']==1:
            self.first_skipped=body['decision']['redirect']
            if self.first_skipped:
                if len(redirects)!=1 or redirects[0]['tid']!=p['tid']:
                    raise ValueError('First redirect was not applied and completed')
                self.prefix(s)
            elif redirects:raise ValueError('Unexpected redirect')
        if p['ordinal']==2 or not self.first_skipped:
            if (s['outputs'],s['headers'],s['others'],s['serial'],s['buffered'])!=(157,2,2,128,0):
                raise ValueError('Incomplete current program output sequence')
        if s['started']:
            if (p['ordinal']!=2 or not self.first_skipped or s['mode']!=1 or
                    s['permit']!=self.token or (s['data'],s['batches'])!=(120,5)):
                raise ValueError('Incomplete or unauthorized batch program')
        elif s['data'] or s['batches'] or s['buffered']:
            raise ValueError('Partial batch without started state')
        self.program_states.append(dict(s))
        self.change(permit=0,completed=1 if p['ordinal']==1 else 3,phase=0)
        if p['ordinal']==2:
            self.exit_step_pending=True
            self.result=dict(mode='combined' if s['started'] else ('first-only' if self.first_skipped else 'ordinary'),
                             generation=self.token,frame=s['frame'],batches=s['batches'],
                             programs=list(self.program_states),outputs_verified=True)
        return self.result

    def finish_exit_step(self):
        if self.held or not self.in_frame or not self.exit_step_pending:
            raise RuntimeError('Final exit instruction completion without pending frame')
        self.exit_step_pending=False;self.in_frame=False
