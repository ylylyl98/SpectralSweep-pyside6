"""Bounded optical diagnostic around the unchanged persistent camera bridge.

No device is opened by importing this module. Optical labels are operator
reports, not measured shutter states. Signal classification is a diagnostic
gate, never a claim of independently measured physical exposure duration.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
import struct
import threading
import time


def save(path, value):
    temporary = path.with_suffix(path.suffix+'.partial')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def frame_timing(log, sequence):
    indices = [i for i,r in enumerate(log) if r['stage']=='request_sequence' and r['value']==sequence]
    if len(indices)!=1: raise RuntimeError('Missing or duplicate native request timing')
    block=[]
    for row in log[indices[0]+1:]:
        if row['stage']=='request_sequence': break
        block.append(row)
    selected=[]
    for stage in ('start_call_before','start_call_after','publication_observed'):
        found=[row for row in block if row['stage']==stage]
        if len(found)!=1: raise RuntimeError('Missing or ambiguous native timing stage: '+stage)
        selected.append(found[0])
    before,after,publication=selected
    times=[row['ms'] for row in selected]
    if (any(not isinstance(v,(float,int)) or not math.isfinite(v) for v in times)
            or not times[0]<=times[1]<=times[2] or after['value']!=1
            or publication['value']!=1024 or publication['error']!=0):
        raise RuntimeError('Invalid native timing evidence')
    elapsed=times[2]-times[0]
    # 5 ms tolerance is a diagnostic cutoff, not an exposure calibration.
    if elapsed<95: raise RuntimeError(f'Native early completion: {elapsed:.3f} ms for nominal 100 ms')
    return {'start_to_publication_ms':elapsed, 'native_start_call_ms':times[1]-times[0],
            'start_return_to_publication_ms':times[2]-times[1],
            'minimum_duration_gate_ms':95, 'physical_exposure_measured':False}


def optical_reference(dark, light):
    if len(dark)<20 or len(light)<20: raise ValueError('Need twenty dark and twenty light frames')
    d=[r['mean_counts'] for r in dark]; l=[r['mean_counts'] for r in light]
    dm,lm=statistics.mean(d),statistics.mean(l)
    noise=max(statistics.stdev(d),statistics.stdev(l))
    difference=lm-dm
    clipped=any(r['maximum']>=65535 for r in dark+light)
    required=max(5.0,5*noise)
    return {'passed':bool(difference>required and not clipped),
            'dark_mean':dm,'light_mean':lm,'difference_counts':difference,
            'max_within_state_sd':noise,'required_difference_counts':required,
            'threshold_counts':(dm+lm)/2,'clipping_observed':clipped,
            'roi_pixels_inclusive':[1,512], 'physical_freshness_verified':False,
            'policy':'Positive light-minus-dark > max(5 mean ADU, 5 within-state SD); no full-scale samples'}


def classify_frame(row, reference):
    if not reference['passed']: raise RuntimeError('Optical reference is not separable')
    level=(row['mean_counts']-reference['dark_mean'])/reference['difference_counts']
    expected=0 if row['label']=='dark' else 1
    return {'normalized_light_level':level,'expected_level':expected,
            'matches':bool(abs(level-expected)<=.35 and row['maximum']<65535),
            'maximum_deviation':.35}


class OpticalRun:
    def __init__(self, bridge, output, *, max_frames=160):
        self.bridge=bridge; self.output=Path(output); self.output.mkdir(exist_ok=False)
        self.max_frames=max_frames; self.frames=[]; self.reference=None
        self.fault=None; self.batches=0; self.owner=None

    def capture_batch(self, label, count, confirmation):
        if self.fault: raise RuntimeError('Diagnostic stopped after failure: '+self.fault)
        if label not in ('dark','light') or type(count) is not int or not 1<=count<=20:
            raise ValueError('Explicit dark/light batch of 1..20 frames required')
        if not isinstance(confirmation,str) or not confirmation.strip():
            raise ValueError('Operator confirmation of stable illumination required')
        if len(self.frames)+count>self.max_frames: raise ValueError('Bounded frame allowance exhausted')
        self.batches+=1
        context={'optical_test_batch':self.batches,'operator_reported_illumination':label,
                 'operator_confirmation':confirmation,'confirmed_utc':datetime.now(timezone.utc).isoformat(),
                 'optics_commanded':False,'temperature_verified':False,'exposure_ms':100}
        save(self.output/f'batch-{self.batches:03d}.json',dict(context,requested_frames=count))
        batch=[]
        try:
            for index in range(count):
                started=time.perf_counter()
                metadata,payload=self.bridge.capture(threading.Event(),context=dict(context,index_in_batch=index+1))
                number=len(self.frames)+1
                with (self.output/f'frame-{number:06d}.bin').open('xb') as stream: stream.write(payload)
                values=struct.unpack('<512H',payload)
                row={'index':number,'batch':self.batches,'index_in_batch':index+1,'first_in_batch':index==0,
                     'label':label,'mean_counts':statistics.mean(values),'maximum':max(values),
                     'zero_count':values.count(0),'full_scale_count':values.count(65535),
                     'host_capture_s':time.perf_counter()-started,'native':metadata,'accepted':False}
                self.frames.append(row); batch.append(row)
                save(self.output/'frames.json',self.frames)
                owner=(metadata['bridge_session_id'],metadata['process_pid'])
                if self.owner is None: self.owner=owner
                if (owner!=self.owner or metadata['request_sequence']!=number
                        or metadata['payload_sha256']!=hashlib.sha256(payload).hexdigest()
                        or metadata['frame_cleanup_verified'] is not True):
                    raise RuntimeError('Frame identity or payload validation failed')
                native=Path(metadata['session_evidence_directory'])
                from app.devices.winusb_logs import read_frame_log
                log=read_frame_log(metadata)
                row['timing']=frame_timing(log,metadata['request_sequence'])
                if self.reference:
                    row['optical_classification']=classify_frame(row,self.reference)
                    if not row['optical_classification']['matches']:
                        raise RuntimeError('Unexpected optical state; first mismatching frame retained')
                if row['full_scale_count']: raise RuntimeError('Full-scale frame; optical test stopped')
                row['accepted']=True
                save(self.output/'frames.json',self.frames)
            return {'batch':self.batches,'label':label,'frames':count,
                    'first_mean_counts':batch[0]['mean_counts'],
                    'mean_counts':statistics.mean(r['mean_counts'] for r in batch),
                    'sd_counts':statistics.stdev(r['mean_counts'] for r in batch) if count>1 else 0,
                    'minimum_start_to_publication_ms':min(r['timing']['start_to_publication_ms'] for r in batch)}
        except Exception as exc:
            self.fault=str(exc)
            save(self.output/'frames.json',self.frames)
            save(self.output/'failure.json',{'error':str(exc),'frames':len(self.frames),'automatic_retry':False})
            raise
