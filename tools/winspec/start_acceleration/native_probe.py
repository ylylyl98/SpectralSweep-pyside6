"""Native observer/batch loader for an exclusively owned XP process.

No FreeLibrary or VirtualFreeEx; every allocation stays until owned process exit.
Unknown remote-call completion pins the COM owner and forbids another operation.
"""
from __future__ import print_function
import binascii,ctypes as ct,hashlib,json,os,struct,threading,time
from .native_runtime import Reader,modules
from .relocation import relocate_chunk

FIELDS='operation cm pipp controller port pipe handle mode frame result fatal active records row_size rows programs batches uncertain'.split()
ROW_FIELDS='committed id api frame program thread caller command value result error length begin end'.split()
class NativeRejected(RuntimeError):
    def __init__(self,status):
        self.status=status;RuntimeError.__init__(self,'Native state rejected operation: '+str(status))

class NativeProbe(Reader):
    def __init__(self,pid,dll_path,audit_path):
        Reader.__init__(self,pid)
        self.k.CloseHandle.argtypes=[ct.c_void_p];self.k.CloseHandle.restype=ct.c_int
        self.k.CloseHandle(self.handle);self.handle=self.k.OpenProcess(0x10043a,False,pid)
        if not self.handle:raise RuntimeError('Cannot open owned process for diagnostic')
        self.pid=pid;self.dll_path=dll_path;self.dll=None;self.control_buffer=None
        self.installed=False;self.mutation_attempted=False
        self.lock=threading.Lock();self.uncertain=False;self.calls=[];self.allocations=[];self.frame=0
        try:self._preflight(audit_path)
        except BaseException:
            self.k.CloseHandle(self.handle);self.handle=None
            raise

    def _preflight(self,audit_path):
        with open(audit_path,'rb') as h:self.audit=json.load(h)
        with open(self.dll_path,'rb') as h:
            if hashlib.sha256(h.read()).hexdigest()!=self.audit['dll_sha256']:raise RuntimeError('Native DLL identity changed')
        self.loaded=modules(self.pid)
        for name,digest in self.audit['driver_sha256'].items():
            if self.loaded[name]['sha256']!=digest:raise RuntimeError('Driver differs: '+name)
        self.cm=self.loaded['contrman.dll']['base'];self.pb=self.loaded['pipp32.dll']['base']
        self.controller=self.controllers(self.cm,self.pb);self.port=self.u32(self.controller+0x6e58)
        self.pipe=self.u32(self.port+0x4f0);self.command_handle=self.u32(self.pipe+4)
        self.runtime=dict(controller=self.controller,port=self.port,pipe=self.pipe,
                          pipe_words=list(struct.unpack('<4I',self.read(self.pipe,16))),
                          multiple_target=self.u32(self.port+0x48),output_target=self.u32(self.port+0x18),
                          persistent=ord(self.read(self.port+0x367,1)))
        frequency=ct.c_longlong()
        if not self.k.QueryPerformanceFrequency(ct.byref(frequency)) or frequency.value<=0:raise RuntimeError('Performance timer frequency unavailable')
        self.runtime['qpc_frequency']=frequency.value
        if (self.runtime['persistent']!=1 or self.runtime['multiple_target']!=self.pb+0xdc92 or
                not self.runtime['pipe_words'][2]&0x40000000 or self.runtime['pipe_words'][3]&0xffff!=0x4950):
            raise RuntimeError('Persistent overlapped command-pipe identity unavailable')
        for name,chunks in self.audit['driver_bytes'].items():
            for rva,excerpt in chunks.items():
                raw=relocate_chunk(binascii.unhexlify(excerpt['hex']),excerpt['relocation_offsets'],
                                   self.audit['preferred_bases'][name],self.loaded[name]['base'])
                if self.read(self.loaded[name]['base']+int(rva,16),len(raw))!=raw:raise RuntimeError('Live driver code differs')
        k=self.k
        k.VirtualAllocEx.argtypes=[ct.c_void_p,ct.c_void_p,ct.c_size_t,ct.c_uint32,ct.c_uint32];k.VirtualAllocEx.restype=ct.c_void_p
        k.WriteProcessMemory.argtypes=[ct.c_void_p,ct.c_void_p,ct.c_void_p,ct.c_size_t,ct.POINTER(ct.c_size_t)];k.WriteProcessMemory.restype=ct.c_int
        k.CreateRemoteThread.argtypes=[ct.c_void_p,ct.c_void_p,ct.c_size_t,ct.c_void_p,ct.c_void_p,ct.c_uint32,ct.POINTER(ct.c_uint32)];k.CreateRemoteThread.restype=ct.c_void_p
        k.WaitForSingleObject.argtypes=[ct.c_void_p,ct.c_uint32];k.WaitForSingleObject.restype=ct.c_uint32
        k.GetExitCodeThread.argtypes=[ct.c_void_p,ct.POINTER(ct.c_uint32)];k.GetExitCodeThread.restype=ct.c_int
        k.GetModuleHandleA.argtypes=[ct.c_char_p];k.GetModuleHandleA.restype=ct.c_void_p
        k.GetProcAddress.argtypes=[ct.c_void_p,ct.c_char_p];k.GetProcAddress.restype=ct.c_void_p
        # XP maps kernel32 at the same address; require its live entry bytes to match.
        load=k.GetProcAddress(k.GetModuleHandleA(b'kernel32.dll'),b'LoadLibraryA')
        if self.read(load,32)!=ct.string_at(load,32):raise RuntimeError('Remote LoadLibrary entry differs')
        self.load_entry=load

    def identity_matches(self,pid,loaded=None):
        if pid!=self.pid or not self.process_alive():return False
        current=modules(pid)
        return current==self.loaded and (loaded is None or all(current.get(n)==m for n,m in loaded.items()))

    def _live_identity(self):
        if not self.identity_matches(self.pid):raise RuntimeError('Native process/module identity changed')
        if (self.u32(self.controller+0x618)!=self.cm+0xc65fa or
            self.u32(self.controller+0x6e58)!=self.port or self.u32(self.port+0x4f0)!=self.pipe or
            list(struct.unpack('<4I',self.read(self.pipe,16)))!=self.runtime['pipe_words'] or
            self.u32(self.port+0x18)!=self.pb+0xdafb or self.u32(self.port+0x48)!=self.pb+0xdc92 or
            ord(self.read(self.port+0x367,1))!=1):raise RuntimeError('Native controller/pipe identity changed')
        for name,chunks in self.audit['driver_bytes'].items():
            for rva,excerpt in chunks.items():
                raw=relocate_chunk(binascii.unhexlify(excerpt['hex']),excerpt['relocation_offsets'],
                                   self.audit['preferred_bases'][name],self.loaded[name]['base'])
                if self.read(self.loaded[name]['base']+int(rva,16),len(raw))!=raw:raise RuntimeError('Live driver code differs')

    def install(self):
        if self.uncertain or self.installed:raise RuntimeError('Native install requires a confirmed idle helper')
        self._live_identity()
        if self.dll is None:
            self.mutation_attempted=True
            path=self.dll_path.encode('mbcs')+b'\0'
            self.dll=self.invoke(self.load_entry,self.allocate(path),15000)
            if not self.dll:raise RuntimeError('Remote DLL did not load')
            self.control_entry=self.dll+self.audit['exports']['_BatchControl@4']
            if self.read(self.control_entry,16)!=binascii.unhexlify(self.audit['control_prefix']):raise RuntimeError('Remote helper code differs')
        self.status=self.control(1)
        self.installed=True
        self.fault_address=self.invoke(self.dll+self.audit['exports']['_BatchFaultAddress@4'],0)
        if not self.fault_address or self.u32(self.fault_address):raise RuntimeError('Native fault state unavailable')
        self.gate_address=self.invoke(self.dll+self.audit['exports']['_BatchGateAddress@4'],0)
        self.debug_config()

    def process_alive(self):
        if self.handle is None:return False
        result=self.k.WaitForSingleObject(self.handle,0)
        if result not in (0,258):raise RuntimeError('Owned process lifetime unknown')
        return result==258

    def close_exited(self):
        if self.process_alive():raise RuntimeError('Cannot close a living native owner')
        if self.handle is not None:
            if not self.k.CloseHandle(self.handle):raise RuntimeError('Owned process handle close failed')
            self.handle=None

    def gate_state(self):
        from .combined_gate import FIELDS,MAGIC
        result=dict(zip(FIELDS,struct.unpack('<30I',self.read(self.gate_address,120))))
        if (result['magic'],result['version'],result['size'],result['controller'])!=(MAGIC,1,120,self.controller):
            raise RuntimeError('Native shared gate ABI/identity changed')
        return result

    def debug_config(self):
        size=self.audit['dll_image_size']
        if not self.dll<=self.gate_address<self.gate_address+120<=self.dll+size:
            raise RuntimeError('Shared gate outside loaded helper')
        state=self.gate_state()
        if any(state[k] for k in ('armed','phase','permit','hold','fatal','active','uncertain')):
            raise RuntimeError('Debugger requires an idle native helper')
        return dict(address=self.gate_address,dll_base=self.dll,dll_size=size,
                    controller=self.controller,generation=state['generation'])

    def allocate(self,raw):
        address=self.k.VirtualAllocEx(self.handle,None,len(raw),0x3000,4)
        if not address:raise RuntimeError('Remote allocation failed')
        self.allocations.append(address)
        self.write_checked(address,raw)
        return address

    def write_checked(self,address,raw):
        written=ct.c_size_t()
        if not self.k.WriteProcessMemory(self.handle,address,raw,len(raw),ct.byref(written)) or written.value!=len(raw):
            raise RuntimeError('Incomplete diagnostic allocation write')
        if self.read(address,len(raw))!=raw:raise RuntimeError('Remote allocation readback differs')

    def invoke(self,function,argument,timeout=5000):
        if self.uncertain:raise RuntimeError('Previous remote completion unknown')
        self.uncertain=True;tid=ct.c_uint32();h=self.k.CreateRemoteThread(self.handle,None,0,function,argument,0,ct.byref(tid))
        self.calls.append(dict(function=function,argument=argument,thread=tid.value,handle=h))
        if not h or self.k.WaitForSingleObject(h,timeout)!=0:raise RuntimeError('Remote helper completion unknown; owner retained')
        result=ct.c_uint32()
        if not self.k.GetExitCodeThread(h,ct.byref(result)):raise RuntimeError('Remote helper result unknown')
        self.calls[-1]['result']=result.value;self.uncertain=False
        self.k.CloseHandle(h);return result.value

    def control(self,operation,mode=0,frame=0,allow_fault=False,archive=None):
        with self.lock:
            if self.uncertain:raise RuntimeError('Previous remote completion unknown')
            cfg=dict.fromkeys(FIELDS,0)
            cfg.update(operation=operation,cm=self.cm,pipp=self.pb,controller=self.controller,
                       port=self.port,pipe=self.pipe,handle=self.command_handle,mode=mode,frame=frame)
            if archive is not None:
                cfg.update((k,archive[k]) for k in ('records','rows','row_size'))
            raw=struct.pack('<18I',*[cfg[k] for k in FIELDS])
            try:
                if self.control_buffer is None:self.control_buffer=self.allocate(raw)
                else:self.write_checked(self.control_buffer,raw)
                result=self.invoke(self.control_entry,self.control_buffer)
                status=dict(zip(FIELDS,struct.unpack('<18I',self.read(self.control_buffer,72))))
            except BaseException:
                self.uncertain=True
                raise
            if operation==3:status['reject_reasons']=status['mode']
            self.status=status
            if result!=1 or status['result']!=1 or (status['fatal'] or status['uncertain']) and not (operation==0 and allow_fault):
                raise NativeRejected(status)
            return status

    def arm(self,enabled):
        self.frame+=1
        return self.control(2,int(enabled),self.frame)

    def disarm(self):
        clock=getattr(time,'monotonic',None) or time.clock;deadline=clock()+3.
        while True:
            try:return self.control(3)
            except NativeRejected as error:
                s=error.status
                reasons=s.get('reject_reasons',0)
                # A completed, rejected control call changed nothing. Only wait
                # for native callbacks to leave; never retry unknown completion.
                if (self.uncertain or s['fatal'] or s['uncertain'] or s['row_size']!=140 or
                    s['programs']>2 or not reasons or reasons&~(1|16|32) or clock()>=deadline):raise
                time.sleep(.01)
    def cancel_unstarted(self):return self.control(5)

    def snapshot(self,allow_active=False):
        clock=getattr(time,'monotonic',None) or time.clock;deadline=clock()+3.
        while True:
            status=self.control(0,allow_fault=allow_active)
            gate=self.gate_state()
            if allow_active or not (status['active'] or gate['active']):break
            if any(gate[k] for k in ('armed','phase','permit','buffered','fatal','hold','uncertain')) or clock()>=deadline:
                raise RuntimeError('Native records remained active')
            time.sleep(.01)
        if status['active'] and not allow_active or status['records']>16384 or status['row_size']!=140:raise RuntimeError('Native records not quiescent')
        raw=self.read(status['rows'],status['records']*140) if status['records'] else b''
        rows=[]
        for i in range(status['records']):
            values=struct.unpack_from('<12I2q76s',raw,i*140);row=dict(zip(ROW_FIELDS,values[:-1]))
            if row['committed']!=1 and not allow_active or row['id']!=i or row['length']>76:raise RuntimeError('Uncommitted native record')
            row['bytes_hex']=binascii.hexlify(values[-1][:row['length']]).decode('ascii');rows.append(row)
        return dict(status=status,gate=gate,runtime=self.runtime,modules=self.loaded,rows=rows,calls=list(self.calls),
                    allocations=list(self.allocations),control_buffer=self.control_buffer,
                    dll_sha256=self.audit['dll_sha256'],installed=self.installed)

    def reset_archived(self,status,gate):
        return self.control(6,mode=gate['generation'],frame=gate['frame'],archive=status)

    def restore(self):
        self.control(4)
        if (self.u32(self.cm+0xf41e0)!=self.pb+0x1199d or self.u32(self.cm+0xf41e8)!=self.pb+0x11778 or
                self.u32(self.controller+0x618)!=self.cm+0xc65fa):raise RuntimeError('Native pointers not restored')
        self.installed=False
