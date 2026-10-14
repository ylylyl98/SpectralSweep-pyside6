"""Serial, bounded diagnostic with unchanged/uncontrolled illumination.

Uses the installed bridge without altering native initialization or recipe.
Evidence is read incrementally so validation cost does not grow per frame.
"""
import hashlib
import json
import math
from pathlib import Path
import statistics
import struct
import threading
import time

from tools.winspec.validate_local_optical import frame_timing, save
from app.devices.winusb_logs import frame_log_path


class StabilityRun:
    def __init__(self, bridge, output, *, limit=1000):
        if type(limit) is not int or not 1<=limit<=1000: raise ValueError('Frame limit must be 1..1000')
        self.bridge=bridge; self.output=Path(output); self.output.mkdir(exist_ok=False)
        self.limit=limit; self.frames=[]; self.owner=None; self.fault=None
        self.log_path=None; self.log_offset=0; self.previous_publication=None
        self.request_ids=set(); self.hash_first_index={}

    def capture(self, phase, *, planned_idle_s=0):
        if self.fault: raise RuntimeError('Diagnostic already failed: '+self.fault)
        if len(self.frames)>=self.limit: raise ValueError('Bounded frame allowance exhausted')
        if not math.isfinite(planned_idle_s) or not 0<=planned_idle_s<=30:
            raise ValueError('Diagnostic idle must be 0..30 seconds')
        number=len(self.frames)+1
        row={'index':number,'phase':phase,'planned_idle_s':planned_idle_s,
             'illumination':'unchanged_uncontrolled','hardware_frame_freshness_verified':False,
             'physical_exposure_duration_verified':False,'temperature_verified':False,'accepted':False}
        started=time.perf_counter()
        try:
            meta,payload=self.bridge.capture(threading.Event(),context=dict(row,optics_commanded=False))
            row['native']=meta; row['bridge_capture_s']=time.perf_counter()-started
            with (self.output/f'frame-{number:06d}.bin').open('xb') as stream: stream.write(payload)
            save(self.output/f'frame-{number:06d}.json',row)
            if len(payload)!=1024: raise RuntimeError('Unexpected frame length')
            values=struct.unpack('<512H',payload)
            row.update(mean_counts=statistics.mean(values),minimum=min(values),maximum=max(values),
                       zero_count=values.count(0),full_scale_count=values.count(65535))
            owner=(meta['bridge_session_id'],meta['process_pid'])
            if self.owner is None: self.owner=owner
            digest=hashlib.sha256(payload).hexdigest()
            if (owner!=self.owner or meta['request_sequence']!=number or meta['request_id'] in self.request_ids
                    or meta['payload_sha256']!=digest or meta['frame_cleanup_verified'] is not True):
                raise RuntimeError('Native frame identity or payload mismatch')
            self.request_ids.add(meta['request_id'])
            row['exact_duplicate_of']=self.hash_first_index.get(digest)
            self.hash_first_index.setdefault(digest,number)
            log=frame_log_path(meta)
            if log!=self.log_path:
                self.log_offset=0
                self.log_path=log
            with log.open('rb') as stream:
                stream.seek(self.log_offset); data=stream.read(); position=stream.tell()
            if not data.endswith(b'\n'): raise RuntimeError('Incomplete native log')
            rows=[json.loads(line) for line in data.splitlines()]
            row['native_log_bytes_read']=len(data)
            row['native_timing']=frame_timing(rows,number)
            start=next(r['ms'] for r in rows if r['stage']=='start_call_before')
            publication=next(r['ms'] for r in rows if r['stage']=='publication_observed')
            if self.previous_publication is not None:
                row['native_timing']['previous_publication_to_start_ms']=start-self.previous_publication
            if row['full_scale_count'] or row['zero_count']==512:
                raise RuntimeError('Full-scale or all-zero frame; diagnostic stopped')
            self.previous_publication=publication; self.log_offset=position
            row['accepted']=True; row['diagnostic_total_s']=time.perf_counter()-started
            self.frames.append(row);save(self.output/f'frame-{number:06d}.json',row)
            return row
        except BaseException as exc:
            self.fault=str(exc);row['error']=str(exc)
            save(self.output/f'frame-{number:06d}.json',row)
            save(self.output/'failure.json',dict(index=number,error=str(exc),automatic_retry=False))
            raise
