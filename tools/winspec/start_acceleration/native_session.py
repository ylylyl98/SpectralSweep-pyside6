"""Owned native-helper lifecycle; the monitor only reads target memory.

All uncertain states retain the probe, process handle, DLL and remote storage.
Retirement archives committed evidence before reset and never retries an unknown
remote completion. The same helper is reused across verified session rotations.
"""
from __future__ import division
import hashlib,json,os,threading,time
from .manager import Unavailable

ROOT=os.path.dirname(os.path.abspath(__file__))
clock=getattr(time,'monotonic',None) or time.clock


def save(path,value):
    """Exclusive durable evidence with a separately durable digest receipt."""
    raw=(json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode('utf-8')
    for destination,data in ((path,raw),(path+'.sha256',(hashlib.sha256(raw).hexdigest()+'\n').encode('ascii'))):
        fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_BINARY',0),0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
    return hashlib.sha256(raw).hexdigest()


class NativeSession(object):
    def __init__(self,manager,probe_factory=None,identity=None):
        if probe_factory is None:
            from .native_probe import NativeProbe
            probe_factory=NativeProbe
        if identity is None:
            from .transport import identity
        self.manager=manager;self.factory=probe_factory;self.identity=identity
        self.probe=None;self.active=False;self.group=None;self.modules=None;self.pid=None
        self.monitor=None;self.monitor_stop=threading.Event();self.error=None

    def _fail(self,error):
        # Latch before formatting: exception __str__ is diagnostic code too.
        self.manager.failed=True
        if self.error is None:
            try:self.error=str(error)
            except BaseException:self.error='Native fault (error text unavailable)'
        self.manager.fail(self.error)

    def _check(self):
        if self.error is not None or self.manager.failed:
            raise RuntimeError('Native Start recovery required; resources retained')

    def _health(self):
        self._check()
        state=self.probe.gate_state()
        if any(state[k] for k in ('fatal','hold','uncertain')):
            raise RuntimeError('Native gate fault; owner retained')
        return state

    def _monitor(self):
        while not self.monitor_stop.wait(.05):
            try:self._health()
            except BaseException as error:
                self._fail(error);return

    def start(self,group):
        self._check()
        if self.active:raise RuntimeError('Native session is already active')
        try:
            pid,loaded=self.identity()
            if self.probe is not None:
                if not self.probe.process_alive():
                    self.probe.close_exited();self.probe=None
                elif pid!=self.pid or loaded!=self.modules:
                    raise RuntimeError('Owned WinSpec identity changed while process remains alive')
                elif hasattr(self.probe,'identity_matches') and not self.probe.identity_matches(pid,loaded):
                    raise RuntimeError('Owned WinSpec process identity no longer matches')
            if self.probe is None:
                # The constructor performs only preflight reads and closes its
                # handle if they fail. Assign before install can mutate anything.
                self.probe=self.factory(pid,os.path.join(ROOT,'batch_probe.dll'),os.path.join(ROOT,'binary-audit.json'))
                self.pid=pid;self.modules=loaded
            self.group=os.path.realpath(group)
            self.probe.install()
            self.active=True
            self._health()
            self.monitor_stop.clear();self.monitor=threading.Thread(target=self._monitor)
            self.monitor.daemon=True;self.monitor.start()
        except BaseException as error:
            if self.probe is None or not self.probe.mutation_attempted:
                raise Unavailable('Native Start preflight unavailable: '+str(error))
            self._fail(error);raise

    def ready(self):
        """The last confirmed disarm count is exact while unarmed."""
        try:
            if self.probe.uncertain:raise RuntimeError('Native remote completion uncertain')
            state=self._health()
            if any(state[k] for k in ('armed','phase','permit','buffered')):
                raise RuntimeError('Native helper not between frames')
            status=self.probe.status
            if status['row_size']!=140 or not 0<=status['records']<=16384:
                raise RuntimeError('Native record capacity invalid')
            return status['records']<=16384-1500
        except BaseException as error:self._fail(error);raise

    def _quiescent(self):
        deadline=clock()+3.
        while True:
            if self.probe.uncertain:raise RuntimeError('Native remote completion uncertain')
            state=self._health()
            if any(state[k] for k in ('armed','phase','permit','buffered')):
                raise RuntimeError('Native helper cannot retire an incomplete frame')
            if not state['active']:return state
            if clock()>=deadline:raise RuntimeError('Native callbacks remained active')
            time.sleep(.01)

    def _snapshot(self):
        self._quiescent()
        value=self.probe.snapshot();status=value['status'];rows=value['rows'];gate=value['gate']
        if (any(status[k] for k in ('fatal','active','uncertain')) or status['row_size']!=140 or
                not 0<=status['records']<=16384 or len(rows)!=status['records'] or
                any(r['committed']!=1 or r['id']!=i for i,r in enumerate(rows)) or
                any(gate[k] for k in ('fatal','active','uncertain','armed','phase','permit','hold','buffered'))):
            raise RuntimeError('Native evidence is incomplete or not quiescent')
        return value

    def retire(self):
        if not self.active:
            self._check();return
        try:
            self._check()
            before=self._snapshot()
            save(os.path.join(self.group,'native-before-restore.json'),before)
            self.probe.restore()
            final=self._snapshot()
            digest=save(os.path.join(self.group,'native-final.json'),final)
            receipt=self.probe.reset_archived(final['status'],final['gate'])
            save(os.path.join(self.group,'native-reset-receipt.json'),
                 dict(archive_sha256=digest,reset=receipt,calls=list(self.probe.calls)))
            # No monitor is stopped and no log discarded until restoration,
            # reset acknowledgement, and both durable archives are confirmed.
            self.monitor_stop.set()
            if self.monitor is not None:
                self.monitor.join(1.)
                if self.monitor.is_alive():raise RuntimeError('Native fault monitor did not stop')
            self._health()
            self.probe.calls[:]=[]
            self.monitor=None;self.active=False;self.group=None
        except BaseException as error:self._fail(error);raise
