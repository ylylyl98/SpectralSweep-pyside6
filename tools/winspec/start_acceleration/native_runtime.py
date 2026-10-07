"""Owned XP runtime inspection; no Start, Stop, SetParam or pointer writes."""
from __future__ import print_function
import ctypes as ct, hashlib, struct
class Reader(object):
    def __init__(self,pid):
        self.k=ct.WinDLL('kernel32',use_last_error=True)
        self.k.OpenProcess.argtypes=[ct.c_uint32,ct.c_int,ct.c_uint32];self.k.OpenProcess.restype=ct.c_void_p
        self.k.ReadProcessMemory.argtypes=[ct.c_void_p,ct.c_void_p,ct.c_void_p,ct.c_size_t,ct.POINTER(ct.c_size_t)]
        self.k.ReadProcessMemory.restype=ct.c_int
        self.k.VirtualQueryEx.argtypes=[ct.c_void_p,ct.c_void_p,ct.c_void_p,ct.c_size_t]
        self.k.VirtualQueryEx.restype=ct.c_size_t
        self.handle=self.k.OpenProcess(0x410,False,pid)
        if not self.handle:raise RuntimeError('Read-only process open failed')
    def read(self,address,size):
        data=ct.create_string_buffer(size);got=ct.c_size_t()
        if not self.k.ReadProcessMemory(self.handle,address,data,size,ct.byref(got)) or got.value!=size:
            raise RuntimeError('Incomplete process memory read')
        return data.raw
    def u32(self,address):return struct.unpack('<I',self.read(address,4))[0]
    def controllers(self,cm,pipp):
        signature=struct.pack('<4I',*[cm+r for r in (0xe487,0xdde6,0xe1d8,0xe30d)])
        address=0x10000;found=[]
        while address<0x7fff0000:
            mbi=(ct.c_uint32*7)()
            if self.k.VirtualQueryEx(self.handle,address,ct.byref(mbi),28)!=28:break
            start,alloc,ap,size,state,protect,kind=mbi
            if size==0 or start+size<=address:raise RuntimeError('Invalid memory map')
            if state==0x1000 and protect in (4,8) and kind==0x20000:
                for off in range(0,size,262144):
                    try:data=self.read(start+off,min(size-off,262144+32))
                    except RuntimeError:continue
                    pos=data.find(signature)
                    while pos>=0:
                        c=start+off+pos
                        try:
                            port=self.u32(c+0x6e58)
                            if self.u32(c+0x618)==cm+0xc65fa and self.u32(port+0x18)==pipp+0xdafb:
                                found.append(c)
                        except RuntimeError:pass
                        pos=data.find(signature,pos+1)
            address=start+size
        if len(set(found))!=1:raise RuntimeError('Controller identity is not unique: '+str(found))
        return found[0]

def modules(pid):
    from .native_identity import modules as known
    result=known(pid)
    class M(ct.Structure):
        _fields_=[(n,ct.c_uint32) for n in ('size id pid global_count process_count base image_size handle').split()]+[('name',ct.c_char*256),('path',ct.c_char*260)]
    k=ct.windll.kernel32;snap=k.CreateToolhelp32Snapshot(8,pid);m=M();m.size=ct.sizeof(m)
    try:
        ok=k.Module32First(snap,ct.byref(m))
        while ok:
            name=m.name.decode('ascii').lower()
            if name=='usbdrvd.dll':
                with open(m.path,'rb') as h:digest=hashlib.sha256(h.read()).hexdigest()
                result[name]=dict(base=m.base,size=m.image_size,path=m.path.decode('mbcs'),sha256=digest)
            ok=k.Module32Next(snap,ct.byref(m))
    finally:k.CloseHandle(snap)
    return result
