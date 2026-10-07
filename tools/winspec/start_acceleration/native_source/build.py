"""Reproducible native experiment build and relocation-aware runtime audit."""
from pathlib import Path
import hashlib,json,subprocess,sys
ROOT=Path(__file__).resolve().parent
BASE=ROOT.parent
sys.path.insert(0,str(BASE/'driver-analysis-deps'))
import pefile
compiler=BASE/'driver-command-trace-build/tcc/tcc.exe'
commands=[]
for name,flags in [('batch_probe',['-shared','-nostdlib']),('test_batch',[]),('replay_test',[]),('debug_fixture',[]),('lifecycle_test',[])]:
    out=ROOT/(name+('.dll' if name=='batch_probe' else '.exe'))
    args=[str(compiler),*flags,'-o',str(out),str(ROOT/(name+'.c'))]
    if name=='batch_probe':args+=['-lkernel32','-lmsvcrt']
    subprocess.run(args,check=True);commands.append(args)
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
dll=pefile.PE(str(ROOT/'batch_probe.dll'))
exports={e.name.decode():e.address for e in dll.DIRECTORY_ENTRY_EXPORT.symbols}
audit=dict(dll_sha256=digest(ROOT/'batch_probe.dll'),machine=dll.FILE_HEADER.Machine,
           subsystem_major=dll.OPTIONAL_HEADER.MajorSubsystemVersion,exports=exports,
           dll_image_size=dll.OPTIONAL_HEADER.SizeOfImage,
           control_prefix=dll.get_data(exports['_BatchControl@4'],16).hex(),
           driver_sha256={},driver_bytes={},preferred_bases={},
           compiler_sha256=digest(compiler),commands=commands,
           source_sha256={p.name:digest(p) for p in [ROOT/'build.py',*ROOT.glob('*.c')]})
assert not any(exports['_BatchControl@4']<=r.rva<exports['_BatchControl@4']+16
               for b in getattr(dll,'DIRECTORY_ENTRY_BASERELOC',[]) for r in b.entries if r.type)
ranges={'contrman.dll':[(0xc65fa,0x800),(0xdde6,0x339),(0x39181,0x427)],
        'pipp32.dll':[(0x11778,0x2e),(0x1199d,0x2d),(0xdafb,0x2de)],
        'usbdrvd.dll':[(0x2ab0,0x2e),(0x2dc0,0xf8)]}
for name in ('pidc32.dll','usbdrvd.dll','pvcam32.dll','contrman.dll','pipp32.dll','winspec.exe'):
    folder='installed-driver-evidence' if name=='usbdrvd.dll' else 'winspec-static-20261003'
    files=list((BASE/folder).rglob(name));assert len(files)==1,(name,files)
    path=files[0];pe=pefile.PE(str(path));audit['driver_sha256'][name]=digest(path)
    audit['preferred_bases'][name]=pe.OPTIONAL_HEADER.ImageBase;chunks={}
    relocs=[r for b in getattr(pe,'DIRECTORY_ENTRY_BASERELOC',[]) for r in b.entries if r.type]
    for start,size in ranges.get(name,[]):
        offsets=[]
        for r in relocs:
            if start-3<=r.rva<start+size:
                assert r.type==3 and start<=r.rva and r.rva+4<=start+size,(name,start,r.rva,r.type)
                offsets.append(r.rva-start)
        chunks[hex(start)]={'hex':pe.get_data(start,size).hex(),'relocation_offsets':offsets}
    audit['driver_bytes'][name]=chunks
(ROOT/'binary-audit.json').write_text(json.dumps(audit,indent=2)+'\n')
print(json.dumps({'dll_sha256':audit['dll_sha256'],'exports':exports,'relocations':sum(len(c['relocation_offsets']) for chunks in audit['driver_bytes'].values() for c in chunks.values())}))
