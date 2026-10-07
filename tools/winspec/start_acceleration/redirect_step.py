"""Validate before changing context; the destination instruction executes normally."""
import struct


def prepare(context,index,target,action,read,addresses):
    if context.EFlags & 0x100:raise ValueError('Foreign trap flag')
    if context.Eip!=target['address']:raise ValueError('Source instruction differs')
    instruction=target
    if action is not None:
        instruction=target['redirect_to']
        if instruction['address'] in addresses:raise ValueError('Destination is another active breakpoint')
        if context.Ebp!=action['entry_esp']-4 or context.Esp!=context.Ebp-0x64:raise ValueError('Program frame differs')
        word=lambda address:struct.unpack('<I',read(address,4))[0]
        if word(context.Ebp-0x14)!=1:raise ValueError('Program local does not indicate success')
        if word(context.Ebp+4)!=action['return_address'] or word(context.Ebp+8)!=action['controller']:raise ValueError('Program arguments differ')
        expected=instruction['first_bytes']
        if list(bytearray(read(instruction['address'],len(expected))))!=expected:raise ValueError('Destination instruction bytes differ')
    step=dict(index=index,esp=context.Esp,expected_eip=instruction['address']+instruction['step_length'],
        expected_esp=context.Esp+instruction['stack_delta'],redirected=action is not None)
    context.Eip=instruction['address']
    context.Dr7 &= ~(3 << (index*2));context.Dr6=0;context.EFlags|=0x100
    return step
