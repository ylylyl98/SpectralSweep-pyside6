"""Pure-Python session policy. COM proxies never survive a capture call here."""
from __future__ import division
import json,math,os,re,shutil,struct,tempfile,threading,time
try:STRING_TYPES=(basestring,)
except NameError:STRING_TYPES=(str,)


class Unavailable(RuntimeError):
    """Known ineligible configuration before any omission; ordinary capture is safe."""


class TimedExperiment(object):
    def __init__(self,exp,counter,native=None,manager=None):
        self.exp=exp;self.counter=counter;self.begin=None;self.end=None
        self.native=native;self.manager=manager
    def __getattr__(self,name):return getattr(self.exp,name)
    def Start(self,document):
        if self.begin is not None:raise RuntimeError('One Start per capture required')
        self.begin=self.counter()
        try:
            if self.native is not None or (self.manager is not None and self.manager.native is not None and self.manager.native.active):
                try:
                    owner=self.exp.acquisition_owner
                    owner.retain(document)
                except BaseException as error:
                    self.manager.fail(error);raise
            if self.manager is not None:self.manager._check()
            if self.native is None:return self.exp.Start(document)
            try:
                self.manager._check()
                self.native.arm(True)
                self.manager._check()
            except BaseException as error:
                self.manager.fail(error);raise
            try:return self.exp.Start(document)
            finally:
                try:
                    self.manager._check()
                    unstarted=not any(row['attempted'] for row in owner.documents)
                    if unstarted:self.native.cancel_unstarted()
                    else:self.native.disarm()
                except BaseException as error:
                    self.manager.fail(error)
                    raise error  # Preserve the native failure on Python 2 as well.
        finally:self.end=self.counter()


class Manager(object):
    def __init__(self,root,factory=None,clock=None,counter=None,frame_limit=30,native_factory=None):
        if isinstance(frame_limit,bool) or not isinstance(frame_limit,int) or not 1<=frame_limit<=30:
            raise ValueError('Frame limit must be between one and thirty')
        if factory is None:
            from .transport import Transport
            factory=Transport
            if native_factory is None:
                from .native_session import NativeSession
                native_factory=NativeSession
        if counter is None:
            from .winspec_debug_trace import qpc
            counter=qpc
        self.root=os.path.realpath(root);self.factory=factory
        self.native_factory=native_factory;self.native=None;self.failure_reason=None
        self.clock=clock or getattr(time,'monotonic',None) or time.clock;self.counter=counter
        self.limit=frame_limit;self.profile=None;self.baseline=None;self.key=None;self.session=None
        self.failed=False;self.in_capture=False;self.cancelled=threading.Event()
        self.frame=0;self.group=None;self.completed=[];self.unavailable_key=None;self.reason=None
        self.ineligible_baselines=0

    @staticmethod
    def eligible(settings):
        try:
            values=[float(settings[k]) for k in ('exposure_ms','accumulations','sequential_frames','timing_mode','detector_width','detector_height','output_width','output_height')]
            if any(math.isnan(v) or math.isinf(v) for v in values):return False
            e,n,s,t,w,h,x,y=values
            return e>0 and n>=1 and int(n)==n and e*n<=1000 and (s,t,w,h,x,y)==(1,1,512,1,512,1) and settings.get('roi_enabled') is False
        except (TypeError,ValueError,KeyError):return False

    def request_invalidate(self,reason):
        self.reason=str(reason);self.cancelled.set()

    def _check(self):
        if self.failed:raise RuntimeError('Start acceleration recovery required; owner retained')

    def fail(self,reason):
        # The independent native monitor must latch before any diagnostic I/O.
        self.failed=True
        if self.failure_reason is None:self.failure_reason=str(reason)

    def _retire(self,successful=True):
        if self.profile is not None:
            try:self.profile.stop()
            except BaseException:
                self.failed=True;raise
            if not self.profile.detached:
                self.failed=True;raise RuntimeError('Debugger detach unconfirmed')
            self.profile=None
        if self.native is not None:self.native.retire()
        self.baseline=None;self.frame=0
        if self.group is not None:
            if successful:self.completed.append(self.group)
            self.group=None
        # Delete only this instance's successfully retired, exclusively created groups.
        while len(self.completed)>4:
            path=self.completed[0];real=os.path.realpath(path)
            if os.path.dirname(real)!=self.root or not os.path.basename(real).startswith('session-'):
                self.failed=True;raise RuntimeError('Evidence directory escaped its owned root')
            shutil.rmtree(real);self.completed.pop(0)

    def invalidate(self,reason):
        self._check()
        if self.in_capture:raise RuntimeError('Capture owns acceleration state')
        self._retire();self.key=None;self.unavailable_key=None;self.ineligible_baselines=0;self.reason=str(reason);self.cancelled.clear()

    def release(self,session_id):
        self._check()
        if self.session is not None and session_id!=self.session:raise RuntimeError('Acceleration session belongs to another client')
        self.invalidate('client released');self.session=None

    def _new_profile(self,resident):
        if self.group is None:
            if not os.path.isdir(self.root):os.makedirs(self.root)
            self.group=tempfile.mkdtemp(prefix='session-',dir=self.root)
        output=os.path.join(self.group,'resident' if resident else 'baseline')
        options=dict(baseline=self.baseline if resident else None,frame_limit=self.limit)
        if self.native_factory is not None:
            if self.native is None:self.native=self.native_factory(self)
            if not self.native.active:self.native.start(self.group)
            options['native']=self.native.probe
        # Assign before launching, so an uncertain launch remains owned.
        self.profile=self.factory(output,**options)
        self.profile.start();self.started_at=self.clock()

    def capture(self,exp,settings,session_id,acquire):
        self._check()
        if self.in_capture:raise RuntimeError('Concurrent accelerated capture')
        if not isinstance(session_id,STRING_TYPES) or re.match(r'^[a-f0-9]{32}$',session_id) is None:
            raise ValueError('Invalid acceleration client session')
        configured=dict(settings)
        configured.pop('readout_time_s',None)  # Observed duration, not an applied setting.
        if 'exposure_ms' in configured:
            # Match the SPE exposure representation; the native Gate still checks
            # the full unrounded controller state before every omission.
            configured['exposure_ms']=struct.unpack('<f',struct.pack('<f',float(configured['exposure_ms'])/1000.))[0]
        key=json.dumps(configured,sort_keys=True,separators=(',',':'),allow_nan=False)
        if self.session!=session_id or self.key!=key or self.cancelled.is_set():
            self.invalidate(self.reason or 'session or settings changed')
            self.session=session_id;self.key=key
        if self.profile is not None and (not self.profile.running() or self.frame>=self.limit or self.clock()-self.started_at>=60.):
            self._retire()
        if self.native is not None and self.native.active and not self.native.ready():self._retire()
        self.in_capture=True
        acquiring=False
        native_analysis=None
        try:
            mode='baseline' if self.baseline is None else 'optimized';reason='verified matching state'
            if not self.eligible(settings) or self.unavailable_key==key:
                self._retire();mode='fallback';reason='settings outside acceleration eligibility'
                acquiring=True;result=acquire(exp);acquiring=False
            else:
                try:
                    if self.profile is None:self._new_profile(self.baseline is not None)
                except Unavailable as error:
                    self._retire();self.unavailable_key=key
                    acquiring=True;result=acquire(exp);acquiring=False;mode='fallback';reason=str(error)
                else:
                    native=self.native.probe if self.native is not None and self.baseline is not None else None
                    wrapped=TimedExperiment(exp,self.counter,native,self)
                    if self.baseline is not None:self.profile.open_frame(self.frame)
                    acquiring=True;result=acquire(wrapped);acquiring=False
                    if wrapped.begin is None or wrapped.end is None:raise RuntimeError('Start was not observed')
                    if self.baseline is None:
                        try:self.baseline=self.profile.build_baseline(wrapped.begin,wrapped.end)
                        except Unavailable as error:
                            self._retire();self.ineligible_baselines+=1
                            # A parameter transition can settle during the first
                            # complete Start. Learn once more on the next frame,
                            # never repeat the current acquisition.
                            if self.ineligible_baselines>=2:self.unavailable_key=key
                            mode='fallback';reason=str(error)
                        else:
                            self.profile=None;self.ineligible_baselines=0
                            reason='full Start established a fresh baseline'
                    else:
                        analysis=self.profile.close_frame(self.frame,wrapped.begin,wrapped.end);self.frame+=1
                        native_analysis=analysis.get('native_batch')
                        if not analysis['redirect_applied']:
                            mode='fallback';reason=analysis.get('reason','state differs');self._retire()
            if self.cancelled.is_set():raise RuntimeError('WinSpec acquisition stopped during acceleration completion')
            self._check()
            metadata,raw=result
            metadata['start_acceleration']=dict(version=1,requested=True,mode=mode,optimized=mode=='optimized',reason=reason,session=session_id)
            if native_analysis is not None:metadata['start_acceleration']['native_batch']=native_analysis
            return metadata,raw
        except BaseException:
            # The bridge owns Stop/temperature/transfer certainty. A rejected
            # capture can start again explicitly after a verified debugger retire.
            if not acquiring and not self.cancelled.is_set():self.failed=True
            try:self._retire(successful=False)
            except BaseException:pass
            raise
        finally:self.in_capture=False
