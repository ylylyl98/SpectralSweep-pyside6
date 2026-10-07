"""Apply only audited PE HIGHLOW fixups to a frozen x86 code excerpt."""
import struct

def relocate_chunk(raw, offsets, preferred, actual):
    data=bytearray(raw)
    if len(set(offsets))!=len(offsets):raise ValueError('Duplicate relocation')
    occupied=set()
    for offset in offsets:
        if type(offset) is not int or offset<0 or offset+4>len(data):raise ValueError('Partial relocation')
        slots=set(range(offset,offset+4))
        if occupied&slots:raise ValueError('Overlapping relocations')
        occupied.update(slots)
        value=struct.unpack_from('<I',data,offset)[0]
        struct.pack_into('<I',data,offset,(value+actual-preferred)&0xffffffff)
    return bytes(data) if bytes is not str else str(data)
