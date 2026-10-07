"""Known binary identity inspection on XP; no target writes."""
import ctypes as ct,hashlib

def modules(pid):
    class Module(ct.Structure):
        _fields_=[(n,ct.c_uint32) for n in ('size id pid global_count process_count base image_size handle').split()]+[('name',ct.c_char*256),('path',ct.c_char*260)]
    k=ct.WinDLL('kernel32',use_last_error=True)
    snapshot=k.CreateToolhelp32Snapshot(8,pid)
    if snapshot==-1:raise RuntimeError('Module snapshot failed')
    value=Module();value.size=ct.sizeof(value);result={}
    try:
        ok=k.Module32First(snapshot,ct.byref(value))
        while ok:
            name=value.name.decode('ascii').lower()
            if name in ('pvcam32.dll','contrman.dll','winspec.exe','pipp32.dll','pidc32.dll'):
                with open(value.path,'rb') as handle:raw=handle.read()
                result[name]=dict(base=value.base,size=value.image_size,path=value.path.decode('mbcs'),sha256=hashlib.sha256(raw).hexdigest())
            ok=k.Module32Next(snapshot,ct.byref(value))
    finally:k.CloseHandle(snapshot)
    return result

