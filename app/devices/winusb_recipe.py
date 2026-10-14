"""Bounded manual recipes and lossless software accumulation evidence."""
import hashlib
import math
import struct
import uuid


def recipe(exposure_ms, accumulations):
    values = []
    for value, low, high, label in ((exposure_ms,10,1000,'exposure (ms)'),
                                   (accumulations,1,100,'accumulations')):
        if (isinstance(value, bool) or not isinstance(value, (int,float))
                or not math.isfinite(value) or int(value) != value or not low <= value <= high):
            raise ValueError(f'Local WinUSB {label} must be an integer from {low} to {high}')
        values.append(int(value))
    return tuple(values)


def raw_payload(counts):
    if (not isinstance(counts, list) or len(counts) != 512
            or any(isinstance(x, bool) or not isinstance(x,(int,float)) or not math.isfinite(x)
                   or int(x) != x or not 0 <= x <= 65535 for x in counts)):
        raise ValueError('Saved export is missing raw detector counts')
    return struct.pack('<512H', *(int(x) for x in counts))


def saved_members(snapshot):
    """Validate every constituent before allowing any save acknowledgement."""
    native = snapshot['native_capture']
    if native.get('acquisition_backend') != 'winusb_local':
        raise ValueError('Not a local WinUSB export')
    if 'batch_protocol' not in native:
        payload = raw_payload(snapshot['raw_accumulated_counts'])
        if hashlib.sha256(payload).hexdigest() != native['payload_sha256']:
            raise ValueError('Saved raw detector counts do not match the acquired frame')
        return [native]
    if type(native['batch_protocol']) is not int or native['batch_protocol'] != 1:
        raise ValueError('Unsupported accumulation evidence')
    exposure, n = recipe(native['exposure_ms'], native['accumulations'])
    batch = native['batch_id']
    if str(uuid.UUID(batch)) != batch: raise ValueError('Invalid acquisition batch ID')
    frames, counts = native['frames'], native['raw_frames']
    if not isinstance(frames,list) or not isinstance(counts,list) or len(frames) != n or len(counts) != n:
        raise ValueError('Incomplete accumulation evidence')
    sums, ids, session, previous = [0]*512, set(), None, None
    for i, (frame, raw) in enumerate(zip(frames,counts), 1):
        payload = raw_payload(raw)
        request = frame['request_id']; sequence = frame['request_sequence']
        if (frame.get('acquisition_backend') != 'winusb_local' or frame.get('frame_cleanup_verified') is not True
                or str(uuid.UUID(request)) != request or request in ids
                or type(sequence) is not int or sequence < 1
                or (previous is not None and sequence != previous+1)
                or (session is not None and frame['bridge_session_id'] != session)
                or frame.get('exposure_ms') != exposure
                or hashlib.sha256(payload).hexdigest() != frame['payload_sha256']):
            raise ValueError('Accumulation subframe identity, exposure or payload mismatch')
        context = frame['request_context']
        if (context.get('batch_id'),context.get('accumulation_index'),context.get('accumulations')) != (batch,i,n):
            raise ValueError('Accumulation subframe belongs to another batch')
        ids.add(request); session=frame['bridge_session_id']; previous=sequence
        sums=[a+int(b) for a,b in zip(sums,raw)]
    if snapshot['raw_accumulated_counts'] != sums:
        raise ValueError('Saved accumulated counts do not match all raw subframes')
    return frames
