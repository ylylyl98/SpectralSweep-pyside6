"""THROWAWAY XP x86 call-order diagnostic. No camera commands or code patches.

Hardware execution breakpoints pause the debuggee: timings/failure rates are NOT
representative. Run the camera-free XP self-test and review before live use.
"""
from __future__ import print_function
import ctypes as ct
import json
import os
import struct
import sys
import threading
import time
from .redirect_step import prepare as prepare_instruction_step

U = ct.c_uint32
DR_NAMES = ('Dr0', 'Dr1', 'Dr2', 'Dr3', 'Dr6', 'Dr7')


class Context(ct.Structure):
    _fields_ = ([('ContextFlags', U)] + [(n, U) for n in DR_NAMES] +
        [('FloatSave', ct.c_byte * 112)] + [(n, U) for n in
        ('SegGs SegFs SegEs SegDs Edi Esi Ebx Edx Ecx Eax Ebp Eip SegCs EFlags Esp SegSs').split()] +
        [('ExtendedRegisters', ct.c_byte * 512)])


class DebugEvent(ct.Structure):
    _fields_ = [('code', U), ('pid', U), ('tid', U), ('data', U * 21)]


def arm_context(context, addresses):
    if context.Dr7 & 0xff:
        raise RuntimeError('Thread already has enabled debug breakpoints')
    saved = tuple(getattr(context, n) for n in DR_NAMES)
    for i, address in enumerate(addresses):
        setattr(context, 'Dr%d' % i, address)
    context.Dr6, context.Dr7 = 0, sum(1 << (i * 2) for i in range(len(addresses)))
    return saved


def restore_context(context, saved):
    for name, value in zip(DR_NAMES, saved):
        setattr(context, name, value)


def owned_hit(context, addresses):
    # Do not consume trap-flag, task-switch or debug-register-access exceptions.
    if context.Dr6 & 0xe000:
        return None
    for i, address in enumerate(addresses):
        if context.Eip == address and context.Dr6 & 0xf == 1 << i:
            return i
    return None


def begin_step(context, index):
    if context.EFlags & 0x100:
        raise RuntimeError('Foreign trap flag already enabled')
    context.Dr7 &= ~(3 << (index*2))
    context.Dr6 = 0
    context.EFlags |= 0x100


def end_step(context, index):
    context.Dr7 |= 1 << (index*2)
    context.Dr6 = 0
    context.EFlags &= ~0x100


def save(path, value):
    raw = json.dumps(value, indent=2, allow_nan=False).encode('utf-8')
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def qpc():
    value = ct.c_int64()
    if not ct.windll.kernel32.QueryPerformanceCounter(ct.byref(value)):
        raise RuntimeError('QPC unavailable')
    return value.value


class Trace(object):
    def __init__(self, config, output):
        if ct.sizeof(ct.c_void_p) != 4 or ct.sizeof(Context) != 716:
            raise RuntimeError('Requires native XP x86 Python')
        self.config, self.output = config, output
        self.addresses = [t['address'] for t in config['targets']]
        if not 1 <= len(self.addresses) <= 4 or len(set(self.addresses)) != len(self.addresses):
            raise ValueError('One to four distinct execution addresses required')
        self.k = ct.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'DebugActiveProcess': ([U], ct.c_int),
            'DebugActiveProcessStop': ([U], ct.c_int),
            'DebugSetProcessKillOnExit': ([ct.c_int], ct.c_int),
            'DebugBreakProcess': ([U], ct.c_int),
            'WaitForDebugEvent': ([ct.POINTER(DebugEvent), U], ct.c_int),
            'ContinueDebugEvent': ([U, U, U], ct.c_int),
            'GetThreadContext': ([U, ct.POINTER(Context)], ct.c_int),
            'SetThreadContext': ([U, ct.POINTER(Context)], ct.c_int),
            'ReadProcessMemory': ([U, U, ct.c_void_p, U, ct.POINTER(U)], ct.c_int),
            'CloseHandle': ([U], ct.c_int),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.k, name)
            fn.argtypes, fn.restype = args, result
        self.threads, self.saved = {}, {}
        self.stepping = {}
        self.break_thread = None
        self.break_result = None
        self.thread_starts = {}
        self.break_seen = False
        self.breakpoints_restored = False
        ntdll = ct.WinDLL('ntdll')
        self.break_address = ct.cast(ntdll.DbgBreakPoint, ct.c_void_p).value
        self.break_entry = ct.cast(ntdll.DbgUiRemoteBreakin, ct.c_void_p).value
        self.process = None
        self.attached = False
        self.initial_break = False
        self.pending = None
        self.records = []
        self.report = dict(status='running', pid=config['pid'], targets=config['targets'],
            records=self.records, initial_break_seen=False, detached=False,
            thread_restore_verified=[], debugger_pauses_invalidate_performance=True,
            camera_commands_issued=0, process_memory_written=False, events=0)
        self.logfile = open(os.path.join(output, 'events.log'), 'wb', 0)

    def log(self, text):
        try:
            self.logfile.write(('%s %s\n' % (time.time(), text)).encode('ascii'))
        except BaseException:
            self.report['event_log_failed'] = True

    def check(self, ok, name):
        if not ok:
            raise RuntimeError('%s failed: %s' % (name, ct.get_last_error()))

    def context(self, tid):
        result = Context()
        result.ContextFlags = 0x10017  # control/integer/segments/debug registers
        self.check(self.k.GetThreadContext(self.threads[tid], ct.byref(result)), 'GetThreadContext')
        return result

    def set_context(self, tid, context, control=False):
        context.ContextFlags = 0x10011 if control else 0x10010
        self.check(self.k.SetThreadContext(self.threads[tid], ct.byref(context)), 'SetThreadContext')

    def arm(self, tid):
        context = self.context(tid)
        if context.EFlags & 0x100: raise RuntimeError('Existing thread trap flag')
        saved = arm_context(context, self.addresses)
        self.saved[tid] = saved  # retain recovery inputs even on uncertain API failure
        self.set_context(tid, context)
        actual = self.context(tid)
        if tuple(getattr(actual, n) for n in DR_NAMES[:4]) != tuple(getattr(context, n) for n in DR_NAMES[:4]):
            raise RuntimeError('Hardware breakpoint readback mismatch')
        if actual.Dr7 & 0xffff23ff != context.Dr7 & 0xffff23ff:
            raise RuntimeError('Hardware breakpoint enable readback mismatch')

    def read(self, address, size):
        if not 0x10000 <= address < 0x80000000 or not 0 < size <= 2048 or address+size >= 0x80000000:
            raise ValueError('Read outside bounded user-memory range')
        buffer, count = ct.create_string_buffer(size), U()
        self.check(self.k.ReadProcessMemory(self.process, address, buffer, size, ct.byref(count)), 'ReadProcessMemory')
        if count.value != size:
            raise RuntimeError('Partial process-memory read')
        return buffer.raw

    def restore(self):
        # Caller holds a debug event: all debuggee threads are suspended by OS.
        for tid, saved in list(self.saved.items()):
            if tid not in self.threads:
                continue
            context = self.context(tid)
            restore_context(context, saved)
            if tid in self.stepping:
                context.EFlags &= ~0x100
            self.set_context(tid, context, control=tid in self.stepping)
            actual = self.context(tid)
            if tid in self.stepping and actual.EFlags & 0x100:
                raise RuntimeError('Owned trap flag not restored on thread %d' % tid)
            # Dr6 reserved bits are normalized by OS; verify address/control registers.
            if tuple(getattr(actual, n) for n in ('Dr0','Dr1','Dr2','Dr3','Dr7')) != saved[:4] + saved[5:]:
                raise RuntimeError('Debug registers not restored on thread %d' % tid)
            self.report['thread_restore_verified'].append(tid)
        self.saved.clear()
        self.stepping.clear()
        self.breakpoints_restored = True

    def request_break(self):
        # XP DebugBreakProcess can block waiting for the remote break-in thread;
        # the debug owner MUST keep pumping CREATE_THREAD/EXCEPTION events.
        self.break_result = bool(self.k.DebugBreakProcess(self.process))
        self.report['break_helper_error'] = ct.get_last_error() if not self.break_result else 0

    def remote_break(self, event):
        return (event.code == 1 and event.data[0] == 0x80000003 and event.data[20] == 1
            and event.data[3] == self.break_address
            and self.thread_starts.get(event.tid) == self.break_entry)

    def detach(self):
        self.check(self.k.DebugActiveProcessStop(self.config['pid']), 'DebugActiveProcessStop')
        self.attached = False
        self.report['detached'] = True
        self.log('detached')

    def record(self, event, context, index):
        row = dict(index=len(self.records), name=self.config['targets'][index]['name'],
            tid=event.tid, eip=context.Eip, unix=time.time(), debug_clock=time.clock(), qpc=qpc())
        try:
            stack = list(struct.unpack('<8I', self.read(context.Esp, 32)))
            row['stack'] = stack
            row['return_address'] = stack[0]
            if self.config.get('controller_fields'):
                controller = stack[1]
                row['controller'] = controller
                row['fields'] = dict((hex(offset), struct.unpack('<I', self.read(controller+offset,4))[0])
                    for offset in self.config['controller_fields'])
        except Exception as error:
            row['read_error'] = str(error)
        self.records.append(row)

    def continue_event(self, status):
        event = self.pending
        self.check(self.k.ContinueDebugEvent(event.pid, event.tid, status), 'ContinueDebugEvent')
        self.pending = None

    def timestamp(self):
        return qpc()

    def service(self):
        pass

    def deadline_ready(self):
        return True

    def instruction_step_completed(self,event,step,context):
        pass

    def run(self):
        self.check(self.k.DebugActiveProcess(self.config['pid']), 'DebugActiveProcess')
        self.attached = True
        started, stopping, break_requested = time.clock(), False, False
        try:
            self.check(self.k.DebugSetProcessKillOnExit(False), 'DebugSetProcessKillOnExit(FALSE)')
            self.log('kill_on_exit_false')
            while self.attached:
                if not stopping:
                    if os.path.exists(os.path.join(self.output, 'stop')):
                        stopping=True;self.report['stop_reason']='requested_file'
                    elif time.clock()-started > 90 and self.deadline_ready():
                        stopping=True;self.report['stop_reason']='diagnostic_deadline'
                    elif len(self.records) >= 512:
                        stopping=True;self.report['stop_reason']='record_limit'
                    if not stopping:
                        self.service()
                        if getattr(self, 'request_stop', False):
                            stopping=True;self.report['stop_reason']='service_error'
                event = DebugEvent()
                if not self.k.WaitForDebugEvent(ct.byref(event), self.config.get('event_wait_ms',100)):
                    if ct.get_last_error() != 121:
                        raise RuntimeError('WaitForDebugEvent failed: %d' % ct.get_last_error())
                    if stopping and not break_requested and self.process:
                        self.log('requesting_break')
                        self.break_thread = threading.Thread(target=self.request_break)
                        self.break_thread.daemon = True
                        self.break_thread.start()
                        self.log('requested_break')
                        break_requested = True
                    if self.break_seen and self.break_thread is not None and not self.break_thread.is_alive():
                        self.check(self.break_result, 'Break helper result')
                        self.report['break_helper_completed'] = True
                        self.detach()
                        break
                    continue
                self.pending = event
                status = 0x80010001 if event.code == 1 else 0x10002
                self.event_received = self.timestamp()
                self.log('event %d tid %d exception %x stop %s' % (event.code,event.tid,event.data[0],stopping))
                self.report['events'] += 1
                if event.code == 3:
                    self.process = event.data[1]
                    self.threads[event.tid] = event.data[2]
                    if event.data[0]: self.k.CloseHandle(event.data[0])
                elif event.code == 2:
                    self.threads[event.tid] = event.data[0]
                    self.thread_starts[event.tid] = event.data[2]
                    if self.initial_break and not stopping: self.arm(event.tid)
                elif event.code == 4:
                    self.threads.pop(event.tid, None)
                    self.saved.pop(event.tid, None)
                    self.stepping.pop(event.tid, None)
                    self.thread_starts.pop(event.tid, None)
                elif event.code == 6:
                    if event.data[0]: self.k.CloseHandle(event.data[0])
                elif event.code == 7 and event.data[0] in self.config.get('module_bases',[self.config.get('module_base')]):
                    stopping = True
                    self.report['module_unloaded'] = True
                elif event.code == 5:
                    self.report['target_exit_code'] = event.data[0]
                    self.saved.clear()
                    self.continue_event(status)
                    self.attached = False
                    break
                elif event.code == 1:
                    exception = event.data[0]
                    if self.remote_break(event) and not self.initial_break:
                        status = 0x10002
                        for target in self.config['targets']:
                            if 'first_bytes' in target and list(bytearray(self.read(target['address'], len(target['first_bytes'])))) != target['first_bytes']:
                                raise RuntimeError('Loaded function bytes differ from frozen binary')
                        self.initial_break = True
                        self.report['initial_break_seen'] = True
                        for tid in list(self.threads): self.arm(tid)
                        self.continue_event(status)
                        save(os.path.join(self.output, 'ready.json'), dict(pid=self.config['pid'], threads=list(self.threads)))
                        continue
                    elif exception == 0x80000004:
                        context = self.context(event.tid)
                        if event.tid in self.stepping and context.Dr6 & 0xe00f == 0x4000:
                            status = 0x10002  # positively owned TF, including validation failure cleanup
                            step = self.stepping[event.tid]
                            target = self.config['targets'][step['index']]
                            if context.Eip != step['expected_eip'] or context.Esp != step['expected_esp']:
                                raise RuntimeError('Owned single-step did not reach verified next instruction')
                            end_step(context, step['index'])
                            self.set_context(event.tid, context, control=True)
                            actual = self.context(event.tid)
                            if actual.EFlags & 0x100 or actual.Dr7 & 0xffff23ff != context.Dr7 & 0xffff23ff:
                                raise RuntimeError('Single-step restoration readback differs')
                            self.stepping.pop(event.tid)
                            self.instruction_step_completed(event,step,actual)
                            self.log('owned_step_completed')
                            index = None
                        elif event.tid in self.stepping:
                            raise RuntimeError('Unexpected exception during owned instruction step')
                        else:
                            index = owned_hit(context, self.addresses)
                            if index is None: status = 0x80010001
                        if index is None and status == 0x80010001:
                            status = 0x80010001
                        elif index is not None:
                            status = 0x10002
                            self.log('owned_hit %d' % index)
                            action = self.record(event, context, index)
                            if stopping: action = None
                            step = prepare_instruction_step(context,index,self.config['targets'][index],action,self.read,self.addresses)
                            self.stepping[event.tid] = step
                            self.set_context(event.tid, context, control=True)
                            if step['redirected']:
                                self.report['control_flow_modified'] = True
                                actual = self.context(event.tid)
                                if actual.Eip != context.Eip or actual.Esp != context.Esp or actual.Ebp != context.Ebp or not actual.EFlags & 0x100:
                                    raise RuntimeError('Redirect context readback differs')
                                self.report.setdefault('redirects_applied',[]).append(dict(tid=event.tid,destination=context.Eip,step=step))
                            self.log('owned_step_started')
                    elif self.remote_break(event) and break_requested:
                        status = 0x10002
                        self.break_seen = True
                    else:
                        status = 0x80010001
                        self.report.setdefault('forwarded_exceptions', []).append(dict(code=exception, first_chance=event.data[20]))
                if stopping and not self.breakpoints_restored and (not break_requested or self.break_seen):
                    self.log('restoring')
                    self.restore()
                    self.continue_event(status)
                    if not break_requested:
                        self.detach()
                        break
                    continue  # pump until the break helper has actually returned
                self.continue_event(status)
                self.log('continued')
            self.report['status'] = 'complete' if self.report['detached'] else 'target_exited'
        except BaseException as error:
            self.report['status'] = 'failed'
            self.report['error'] = '%s: %s' % (type(error).__name__, error)
            # API failures inside an event still permit restoration while all threads
            # are suspended. Never terminate the target or swallow its own exception.
            if self.pending is not None:
                try:
                    self.restore()
                    # Keep the status selected for this exact event above; never
                    # automatically swallow a foreign breakpoint/single-step.
                    self.continue_event(status)
                    if break_requested and (not self.break_seen or self.break_thread.is_alive() or not self.break_result):
                        raise RuntimeError('Pending break helper on failure; retain debugger owner')
                    self.check(self.k.DebugActiveProcessStop(self.config['pid']), 'Failure detach')
                    self.attached = False
                    self.report['detached'] = True
                except BaseException as cleanup:
                    self.report['cleanup_error'] = str(cleanup)
            elif not self.saved:
                if break_requested and (not self.break_seen or self.break_thread.is_alive() or not self.break_result):
                    self.report['cleanup_error'] = 'Pending break helper on failure; retain debugger owner'
                else:
                    self.report['detached'] = bool(self.k.DebugActiveProcessStop(self.config['pid']))
                    self.attached = not self.report['detached']
        finally:
            self.report['debugger_attached_at_return'] = self.attached
            try:
                self.report['finished_qpc'] = qpc()
                save(os.path.join(self.output, 'trace.json'), self.report)
            finally:
                if self.attached:
                    # Retention must not depend on successful logging or archiving.
                    self.log('RECOVERY_REQUIRED: retaining debugger owner')
                    while True:
                        try: time.sleep(1)
                        except BaseException: pass
                self.logfile.close()
        return self.report


if __name__ == '__main__':
    with open(sys.argv[1], 'rb') as handle:
        config = json.load(handle)
    Trace(config, os.path.dirname(os.path.abspath(sys.argv[1]))).run()
