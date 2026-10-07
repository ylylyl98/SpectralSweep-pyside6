"""Conservative Ne/Ar matching and independent broad validation; no hardware I/O."""
import copy
import itertools
import re
import numpy as np
from scipy.signal import find_peaks
from app.wavelength_calibration import fit_calibration, fit_broad_calibration, valid_context


def peaks(counts):
    y=np.asarray(counts,dtype=float)
    if y.shape!=(512,) or not np.all(np.isfinite(y)): raise ValueError('Invalid detector frame')
    noise=max(1.,np.median(np.abs(np.diff(y)-np.median(np.diff(y))))/.6745/np.sqrt(2))
    ids,props=find_peaks(y,prominence=max(8*noise,(y.max()-np.median(y))*.025),distance=5)
    ids=sorted(sorted(ids,key=lambda i:y[i],reverse=True)[:16])
    result=[]
    for i in ids:
        denominator=y[i-1]-2*y[i]+y[i+1]
        offset=.5*(y[i-1]-y[i+1])/denominator if denominator else 0
        result.append(i+1+float(np.clip(offset,-.5,.5)))
    return np.asarray(result)


def exposure_retry(frame, exposure_ms, attempt, maximum_ms=10000.):
    """Bounded retries. ADC clipping check only for explicitly known uint16 data."""
    y=np.asarray(frame['counts'],dtype=float)
    if y.shape!=(512,) or not np.all(np.isfinite(y)): raise ValueError('Invalid detector frame')
    if attempt>=4:return None
    if frame.get('winspec_datatype')==3 and y.max()>=62000:
        return max(1.,exposure_ms/4) if exposure_ms>1 else None
    noise=max(1.,np.median(np.abs(np.diff(y)-np.median(np.diff(y))))/.6745)
    if (y.max()-np.median(y)<30*noise or len(peaks(y))<6) and exposure_ms<maximum_ms:
        return min(maximum_ms,exposure_ms*4)
    return None


def match_frame(frame, reference, tolerance=.2):
    context=frame.get('context')
    if not valid_context(context):raise ValueError('Missing acquisition identity')
    y=np.asarray(frame['counts'],dtype=float)
    if frame.get('winspec_datatype')==3 and np.max(y)>=62000:raise ValueError('Saturated spectrum')
    p=peaks(y)
    if len(p)<6:raise ValueError('Insufficient isolated peaks (need at least 6)')
    # Split BEFORE searching: check peaks never select or refine a mapping.
    held=np.arange(1,len(p)-1,3)
    cp=p[held]; train=np.delete(p,held)
    if len(cp)<2 or len(train)<4:raise ValueError('Insufficient independent peaks')
    center=float(context['center_nm'])
    grating=str(context['grating'])
    match=re.search(r',\s*(\d+)\s*\]',grating)
    grooves=float(match.group(1)) if match else float(grating) if grating.isdigit() else None
    if not grooves or grooves<=0:raise ValueError('Cannot determine grating density for automatic search')
    # Broad search prior for this 512 x 50 um OMA installation; not a calibration.
    nominal=.505*300/grooves
    ref=np.unique([float(w) for w,_,_ in reference if abs(float(w)-center)<nominal*512+80])
    if len(ref)<6:raise ValueError('Insufficient reference lines in range')
    def nearest(values):
        idx=np.searchsorted(ref,values).clip(1,len(ref)-1)
        idx-=np.abs(values-ref[idx-1])<np.abs(values-ref[idx])
        return idx,np.abs(values-ref[idx])
    ri,rj=np.triu_indices(len(ref),1)
    seeds=[]
    for i,j in itertools.combinations(range(len(train)),2):
        if train[j]-train[i]<80:continue
        for sign in (-1,1):
            slope=sign*(ref[rj]-ref[ri])/(train[j]-train[i])
            offset=(ref[rj] if sign<0 else ref[ri])-slope*train[i]
            ok=(np.abs(slope)>nominal*.65)&(np.abs(slope)<nominal*1.4)&(np.abs(offset+slope*256.5-center)<65)
            if np.any(ok):seeds.append(np.column_stack((slope[ok],offset[ok])))
    if not seeds:raise ValueError('No reference pattern matches the grating search range')
    hypotheses=np.concatenate(seeds)
    candidates={}
    # Vectorized chunks keep memory bounded even with a dense reference source.
    for chunk in np.array_split(hypotheses,max(1,len(hypotheses)//4000+1)):
        prediction=chunk[:,0,None]*train+chunk[:,1,None]
        ids,error=nearest(prediction)
        score=(error<=tolerance).sum(axis=1)
        for k in np.where(score>=max(4,len(train)-2))[0]:
            use=error[k]<=tolerance
            key=tuple(np.where(use,ids[k],-1))
            if key in candidates or len(np.unique(ids[k,use]))!=use.sum():continue
            coef=np.polyfit(train[use],ref[ids[k,use]],1)
            residual=np.polyval(coef,train[use])-ref[ids[k,use]]
            if np.max(np.abs(residual))<=tolerance:
                candidates[key]=(int(use.sum()),float(np.sqrt(np.mean(residual**2))),coef,use,ids[k])
    ranked=sorted(candidates.values(),key=lambda r:(-r[0],r[1]))
    if not ranked:raise ValueError('No reference pattern passes fitting tolerance')
    best=ranked[0]
    for other in ranked[1:]:
        if other[0]<best[0]-1:break
        if np.max(np.abs(np.polyval(other[2],p)-np.polyval(best[2],p)))>tolerance*2:
            raise ValueError('Reference assignment ambiguous; no calibration enabled')
    cp=cp[(cp>=train[best[3]].min())&(cp<=train[best[3]].max())]
    ids,error=nearest(np.polyval(best[2],cp))
    if len(cp)<2 or np.max(error)>tolerance:raise ValueError('Independent peaks fail validation')
    r=fit_calibration(train[best[3]],ref[best[4][best[3]]],cp,ref[ids],context,1,tolerance)
    r.update(source='Automatic Ne/Ar pattern matching; held-out peaks',raw_counts=frame['counts'],
             captured_utc=frame.get('captured_utc'),temperature_guard=frame.get('temperature_guard'),
             matching_search={'nominal_nm_per_pixel':nominal,'center_offset_bound_nm':65})
    return r


def build_broad(frames, reference, tolerance=.2, cancelled=lambda:False, progress=lambda text:None):
    records=[]; rejected=[]
    for frame in frames:
        if cancelled():raise ValueError('Calibration cancelled')
        center=frame['context']['center_nm']; progress('Matching Ne/Ar at %g nm' % center)
        try:records.append(match_frame(frame,reference,tolerance))
        except ValueError as exc:rejected.append({'center_nm':center,'reason':str(exc)})
    anchors=[r for r in records if int(round(r['context']['center_nm']))%100==0]
    checks=[r for r in records if int(round(r['context']['center_nm']))%100==50]
    # Never discard a known failed check to manufacture a passing interval.
    record=fit_broad_calibration(anchors,checks,tolerance)
    record['automatic_report']={'rejected_centers':rejected,'captured_centers':[f['context']['center_nm'] for f in frames],
                                'matching':'independent peak and center checks; ambiguous matches rejected'}
    return record
