"""Host-only, bounded detector validation; never opens LightField or an SMU.

The user holds optics/source fixed. This measures the bridge and shared count
processing, not full MegaSweep time or independently verified optical state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.devices.winspec_adapter import WinSpecClient, WinSpecSetup
from tools.winspec.temperature_guard import validate_temperature


def _save(path, value):
    path.write_text(json.dumps(value,indent=2,allow_nan=False),encoding='utf-8')


def _idle(client, backend, *, with_metadata=False):
    metadata,_=client.request('GET_SETTINGS')
    if metadata.get('acquisition_backend','winspec') != backend:
        raise RuntimeError('Selected backend does not match running service')
    settings=metadata.get('settings',{})
    if metadata.get('camera_busy') or any(settings.get(k) is not False for k in
            ('running','winspec_reported_running','controller_running')):
        raise RuntimeError('Detector idle state is not confirmed')
    if metadata.get('temperature_guard_version') != 4 or metadata.get('acquisition_settings_version') != 2:
        raise RuntimeError('Guarded managed acquisition service required')
    validate_temperature(settings)
    WinSpecSetup._validate_geometry(settings)
    if settings.get('timing_mode') != 1 or settings.get('sequential_frames') != 1:
        raise RuntimeError('One sequential frame and internal timing required')
    return (settings, metadata) if with_metadata else settings


def prepare_preflight(client, native_baseline, destination, *, backend='winspec'):
    if backend not in ('winspec','pvcam'): raise ValueError('Unsupported preflight backend')
    settings,metadata=_idle(client,backend,with_metadata=True)
    if settings['accumulations'] != 1:
        raise RuntimeError('One original accumulation required before preparing restoration')
    baseline=json.loads(Path(native_baseline).read_text(encoding='utf-8'))
    if (baseline.get('status') != 'complete' or baseline.get('recovery_required')
            or baseline.get('native_restore_ok') is not True):
        raise RuntimeError('Verified native baseline required')
    expected=baseline['original_native_settings']
    if expected.get('gain_index') != settings.get('controller_gain'):
        raise RuntimeError('Gain differs from verified native baseline; do not apply another gain')
    if backend=='pvcam':
        current=settings.get('native_settings')
        if (metadata.get('server')!='pvcam-camera' or metadata.get('native_health')!='ready'
                or not isinstance(current,dict)
                or any(key not in current or current[key]!=value for key,value in expected.items())):
            raise RuntimeError('Idle native baseline/readout state differs; no fresh preflight saved')
        # Keep every verified baseline requirement and additionally pin any
        # supported optional readback added by the current validated service.
        expected=dict(current)
    value={'saved_unix':time.time(),'settings':settings,'expected_native':expected,
           'baseline_sha256':hashlib.sha256(Path(native_baseline).read_bytes()).hexdigest(),
           'source_backend':backend,'source_server_build':metadata.get('server_build'),
           'scope':('Read-only idle PVCAM preflight; accepted setup inputs and current validated native baseline'
                    if backend=='pvcam' else 'Read-only idle WinSpec preflight; native baseline from verified probe')}
    _save(Path(destination),value)
    return value


def capture_batch(client, backend, output, *, samples, exposure_ms, frames,
                  kind, condition_id, optics_note):
    if not 2 <= samples <= 1000 or kind not in ('light','dark','continuous') or not condition_id.strip() or not optics_note.strip():
        raise ValueError('Bounded samples, capture kind, fixed-condition ID and optics note required')
    original=_idle(client,backend)
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    report_path=output/'report.json'
    if report_path.exists(): raise FileExistsError('Use a new output directory')
    report=dict(status='running',backend=backend,kind=kind,condition_id=condition_id,
                optics_note=optics_note,exposure_ms=exposure_ms,frames=frames,
                samples=[],restore_ok=False,original_settings=original,
                scope='Detector bridge + shared normalization; excludes optics/UI/SMU/MegaSweep',
                optics_verification='User-held fixed optics/source; no optical commands or live optical readback')
    _save(report_path,report)
    restore={k:original[k] for k in ('exposure_ms','accumulations','sequential_frames')}
    error=None
    restoration_permitted=True
    try:
        applied,_=client.request('SET_SETTINGS',dict(exposure_ms=exposure_ms,accumulations=frames,sequential_frames=1))
        settings=applied['settings']
        if settings.get('exposure_ms') != exposure_ms or settings.get('accumulations') != frames:
            raise RuntimeError('Requested recipe was not accepted')
        keys=('exposure_ms','accumulations','sequential_frames','timing_mode','detector_width',
              'detector_height','output_width','output_height','roi_enabled','adc_rate',
              'controller_gain','readout_time_s','native_speed_index')
        expected={key:settings[key] for key in keys if key in settings}
        counts=[]
        for index in range(samples+1):
            started=time.perf_counter()
            metadata,payload=client.request('ACQUIRE_GUARDED',dict(settings_mode='managed',expected_settings=expected),
                                           timeout_s=max(40.,exposure_ms/1000.*frames*1.5+30.))
            request_s=time.perf_counter()-started
            if metadata.get('acquisition_backend','winspec') != backend:
                raise RuntimeError('Acquisition service changed during batch')
            actual=metadata.get('settings',{})
            if any(actual.get(k) != expected[k] for k in ('exposure_ms','accumulations','sequential_frames','timing_mode')):
                raise RuntimeError('Acquired recipe changed')
            validate_temperature(actual)
            guard=metadata.get('temperature_guard',{})
            if (guard.get('version') != 4 or guard.get('passed') is not True
                    or guard.get('monitoring_mode') != 'before_after' or guard.get('sample_count',0) < 2
                    or guard.get('policy') != 'cold_or_locked' or guard.get('limit_c') != -100.
                    or not isinstance(guard.get('max_gap_s'),(int,float))
                    or not np.isfinite(guard['max_gap_s']) or not 0 <= guard['max_gap_s'] <= 3.):
                raise RuntimeError('Missing/invalid temperature guard')
            if backend=='pvcam' and guard.get('maximum_c',float('inf')) > -100.:
                raise RuntimeError('Native camera must be <= -100 C')
            values=WinSpecSetup.normalize_frame(metadata,payload,frames)
            if metadata.get('temporary_spe_cleanup') != 'complete':
                raise RuntimeError('Transfer receipt/cleanup was not confirmed')
            row=dict(index=index,request_s=request_s,processed_s=time.perf_counter()-started,
                     metadata=metadata)
            # Preserve each point before issuing the next acquisition.
            with (output/('point-%04d.npz'%index)).open('xb') as handle:
                np.savez(handle,counts=values,raw_payload=np.frombuffer(payload,dtype=np.uint8))
            _save(output/('point-%04d.json'%index),row)
            if index==0: report['warmup']=row
            else: counts.append(values);report['samples'].append(row)
            _save(report_path,report)
            print('%s %s %d/%d: %.3f s, mean %.2f counts' %
                  (backend,kind,index,samples,row['processed_s'],values.mean()),flush=True)
        archive=output/'counts.npz'
        with archive.open('xb') as handle: np.savez(handle,counts=np.stack(counts))
        report.update(status='complete',counts_archive=str(archive.resolve()),
                      counts_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                      steady_median_processed_s=float(np.median([r['processed_s'] for r in report['samples']])))
    except BaseException as exc:
        report.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        error=exc
        restoration_permitted=False  # Stay fail-closed throughout the STOP wait.
        try:
            stopped,_=client.request('STOP',{'reason':'Validation failure'},timeout_s=5)
            if stopped.get('stop_requested') is True:
                restoration_permitted=True
            else:
                report['stop_error']='Stop not acknowledged; manual recovery required'
        except BaseException as stop_error:
            report['stop_error']=str(stop_error)
    finally:
        try:
            if not restoration_permitted:
                report['recovery_required']=True
                raise RuntimeError('Restoration skipped: native stop state unconfirmed; no further driver requests')
            _idle(client,backend)
            restored,_=client.request('SET_SETTINGS',restore)
            final=_idle(client,backend)
            if any(restored['settings'].get(k) != v or final.get(k) != v for k,v in restore.items()):
                raise RuntimeError('Original recipe restoration mismatch')
            report['restore_ok']=True;report['restored_settings']=final
        except Exception as restore_error:
            report.update(status='failed',restore_error=str(restore_error))
        _save(report_path,report)
    if error is not None: raise RuntimeError(f'Validation failed; preserved report: {report_path}') from error
    if not report['restore_ok']: raise RuntimeError(f'Restoration failed; inspect {report_path}')
    return report


def _batch(path,backend,kind):
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('status')!='complete' or data.get('restore_ok') is not True or data.get('backend')!=backend or data.get('kind')!=kind:
        raise ValueError('Complete restored '+backend+' '+kind+' batch required')
    archive=Path(data['counts_archive'])
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=data.get('counts_sha256'):
        raise ValueError('Counts archive checksum differs')
    with np.load(archive,allow_pickle=False) as file: counts=np.asarray(file['counts'],dtype=float)
    if counts.ndim!=2 or counts.shape[1]!=512 or counts.shape[0]<2 or not np.isfinite(counts).all():
        raise ValueError('Expected repeated full 512-pixel spectra')
    return data,counts


def _metrics(light,dark,roi):
    mean=light.mean(0)-dark.mean(0)
    noise=float(np.sqrt(np.mean(dark.var(0,ddof=1))))
    lo,hi=roi
    x=np.arange(lo,hi+1,dtype=float); y=mean[lo-1:hi]
    integral=float(y.sum())
    if integral<=0 or y.max()<=0: raise ValueError('No positive background-subtracted signal in peak ROI')
    positive=np.maximum(y,0)
    centroid=float(np.dot(x,positive)/positive.sum())
    peak=int(np.argmax(y));half=y[peak]/2
    left=np.flatnonzero(y[:peak]<half);right=np.flatnonzero(y[peak+1:]<half)
    width=None
    if left.size and right.size:
        a=left[-1];b=peak+1+right[0]
        xl=x[a]+(half-y[a])/(y[a+1]-y[a])
        xr=x[b-1]+(half-y[b-1])/(y[b]-y[b-1])
        width=float(xr-xl)
    return dict(integral_counts=integral,peak_centroid_pixel=centroid,
                fwhm_pixels=width,dark_temporal_noise_rms_counts=noise,
                peak_over_dark_temporal_noise=float(y.max()/noise) if noise else None,
                background_subtracted_mean=mean.tolist(),
                centroid_method='Positive weights in specified pixel ROI',
                fwhm_method='Nearest half-maximum crossings around largest ROI peak')


def compare_batches(winspec_light,pvcam_light,winspec_dark,pvcam_dark,*,peak_roi):
    pairs=[_batch(path,backend,kind) for path,backend,kind in (
        (winspec_light,'winspec','light'),(pvcam_light,'pvcam','light'),
        (winspec_dark,'winspec','dark'),(pvcam_dark,'pvcam','dark'))]
    original=pairs[0][0]
    for data,_ in pairs[1:]:
        for key in ('condition_id','exposure_ms','frames','optics_note'):
            if not original.get(key) or data.get(key)!=original[key]:
                raise ValueError('Mismatched fixed condition/recipe: '+key)
    lo,hi=peak_roi
    if not 1<=lo<hi<=512: raise ValueError('Peak ROI must be inside pixels 1..512')
    w=_metrics(pairs[0][1],pairs[2][1],peak_roi)
    p=_metrics(pairs[1][1],pairs[3][1],peak_roi)
    return dict(verdict='measured_only',condition_id=original['condition_id'],
                peak_roi=list(peak_roi),winspec=w,pvcam=p,
                integral_difference_percent=100*(p['integral_counts']/w['integral_counts']-1),
                peak_centroid_difference_pixels=p['peak_centroid_pixel']-w['peak_centroid_pixel'],
                fwhm_difference_pixels=(p['fwhm_pixels']-w['fwhm_pixels']) if p['fwhm_pixels'] is not None and w['fwhm_pixels'] is not None else None,
                note='No automatic equivalence verdict; inspect signal strength, repetitions, source drift and scientific tolerances. Pixel metrics do not validate wavelength calibration.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    prep=sub.add_parser('prepare')
    prep.add_argument('--baseline',required=True);prep.add_argument('--destination',required=True)
    prep.add_argument('--backend',choices=('winspec','pvcam'),default='winspec')
    capture=sub.add_parser('capture')
    capture.add_argument('--backend',choices=('winspec','pvcam'),required=True)
    capture.add_argument('--kind',choices=('light','dark','continuous'),required=True)
    capture.add_argument('--output',required=True);capture.add_argument('--condition-id',required=True)
    capture.add_argument('--optics-note',required=True);capture.add_argument('--samples',type=int,default=20)
    capture.add_argument('--exposure-ms',type=int,default=500);capture.add_argument('--frames',type=int,default=2)
    for command in (prep,capture):
        command.add_argument('--host',default='192.168.170.128');command.add_argument('--port',type=int,default=5000)
    compare=sub.add_parser('compare')
    for name in ('winspec-light','pvcam-light','winspec-dark','pvcam-dark','output'):
        compare.add_argument('--'+name,required=True)
    compare.add_argument('--peak-roi',type=int,nargs=2,required=True)
    args=parser.parse_args()
    if args.command=='prepare':
        result=prepare_preflight(WinSpecClient(args.host,args.port),args.baseline,args.destination,backend=args.backend)
        print('Fresh preflight saved: '+args.destination)
    elif args.command=='capture':
        result=capture_batch(WinSpecClient(args.host,args.port),args.backend,args.output,
            samples=args.samples,exposure_ms=args.exposure_ms,frames=args.frames,
            kind=args.kind,condition_id=args.condition_id,optics_note=args.optics_note)
        print('Median bridge + normalization: %.6f s'%result['steady_median_processed_s'])
    else:
        result=compare_batches(args.winspec_light,args.pvcam_light,args.winspec_dark,args.pvcam_dark,peak_roi=args.peak_roi)
        _save(Path(args.output),result);print(json.dumps({k:v for k,v in result.items() if k not in ('winspec','pvcam')},indent=2))


if __name__=='__main__':main()
