"""Single-ROI, same-process eligibility; no target-memory writes or state replay."""
import binascii,copy,struct
from .input_state import full_snapshot,region
from .communication_state import check_pair


def capture(read,controller):
    result=full_snapshot(read,controller);raw=binascii.unhexlify(result['regions']['controller']['hex']);roi={}
    ranges=[(v['address'],v['address']+len(v['hex'])//2) for v in result['regions'].values()]
    for offset in (0x7190,0x7194):
        address=struct.unpack_from('<I',raw,offset)[0]
        if any(address<end and address+0x20>start for start,end in ranges):raise ValueError('ROI allocation overlaps another known region')
        ranges.append((address,address+0x20))
        node=region(read,address,0x20);words=struct.unpack('<8I',binascii.unhexlify(node['hex']))
        if words[0]!=0 or words[1]!=0:raise ValueError('Experiment requires one isolated ROI node per list')
        roi[hex(offset)]=dict(address=address,hex=node['hex'],values=list(words[2:]))
    result['roi']=roi
    return result


def normalized(snapshot):
    result=copy.deepcopy(snapshot);raw=bytearray(binascii.unhexlify(result['regions']['controller']['hex']))
    for offset in (0x7190,0x7194):struct.pack_into('<I',raw,offset,0)
    result['regions']['controller']['hex']=binascii.hexlify(raw).decode('ascii')
    result['roi']=dict((key,value['values']) for key,value in result['roi'].items())
    return result


def tail_state(snapshot):
    result=normalized(snapshot);raw=bytearray(binascii.unhexlify(result['regions']['controller']['hex']))
    struct.pack_into('<I',raw,0x6c20,0)
    struct.pack_into('<I',raw,0x6e34,struct.unpack_from('<I',raw,0x6e34)[0]|0x2000)
    struct.pack_into('<I',raw,0x6f2c,1)
    result['regions']['controller']['hex']=binascii.hexlify(raw).decode('ascii')
    return result


class BaselineIneligible(ValueError):
    """Two complete valid programs have different configuration state."""


class Gate(object):
    def __init__(self,baseline,base):
        if len(baseline)!=2:raise ValueError('Exactly two complete baseline programs required')
        previous=None;states=[]
        for p in baseline:
            end=p['return_row'];body=p['body'];check_pair(p,end)
            if p['stack'][0]!=base+0xde32 or p['stack'][1]!=p['controller'] or end['eax']!=1:raise ValueError('Baseline caller or return differs')
            if not p['qpc']<body['qpc']<end['qpc'] or body['tid']!=p['tid']:raise ValueError('Baseline body clock/thread differs')
            if body['ebp']!=p['esp']-4 or body['esp']!=body['ebp']-0x64:raise ValueError('Baseline body frame differs')
            if previous is not None and previous>=p['qpc']:raise ValueError('Baseline programs overlap')
            previous=end['qpc']
            if tail_state(body['full_state'])!=normalized(end['full_state']):raise ValueError('Baseline has state changes outside natural tail')
            states.append(normalized(body['full_state']))
        if len(set(p['controller'] for p in baseline))!=1 or len(set(p['tid'] for p in baseline))!=1:raise ValueError('Baseline program identity differs')
        if states[0]!=states[1]:raise BaselineIneligible('Baseline program states differ')
        self.expected=states[0];self.controller=baseline[0]['controller'];self.tid=baseline[0]['tid'];self.used=False

    def decide(self,ordinal,controller,tid,current):
        if self.used or ordinal!=1:return dict(redirect=False,reason='Not the first unused program')
        if controller!=self.controller or tid!=self.tid:return dict(redirect=False,reason='Controller/thread changed')
        if normalized(current)!=self.expected:return dict(redirect=False,reason='Baseline configuration/state differs')
        self.used=True
        return dict(redirect=True,reason='One same-process first-body experiment')
