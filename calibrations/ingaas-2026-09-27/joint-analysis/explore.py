"""Offline hypotheses only; never enables a camera calibration."""
from pathlib import Path
import sys,json
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
import numpy as np
from scipy.signal import find_peaks
from scipy.optimize import least_squares
from app.wavelength_calibration import reference_lines

root=Path(__file__).resolve().parent
frames={}
for f in sorted((root.parent/'direct-session').glob('g0-center*-try*.json')):
    d=json.loads(f.read_text());frames[d['context']['center_nm']]=d
ref=np.unique([r[0] for r in reference_lines('calibrations/ingaas-2026-09-26/LightField-SourceSpectra.xml')])
def features(frame):
    y=np.array(frame['counts']);noise=max(1.,np.median(abs(np.diff(y)-np.median(np.diff(y))))/.6745/2**.5)
    ids,props=find_peaks(y,prominence=max(10*noise,(y.max()-np.median(y))*.10),distance=6)
    ids=sorted(ids,key=lambda i:y[i],reverse=True)[:10]
    out=[]
    for i in ids:
        den=y[i-1]-2*y[i]+y[i+1]
        x=i+1+np.clip(.5*(y[i-1]-y[i+1])/den if den else 0,-.5,.5)
        out.append(x)
    return out
train=[];check=[]
for c,f in frames.items():
    for p in features(f): (train if c%100==0 else check).append((c,p))
train=np.array(train);check=np.array(check)
def nearest(v):
    j=np.searchsorted(ref,v).clip(1,len(ref)-1)
    j-=abs(v-ref[j-1])<abs(v-ref[j])
    return ref[j]
def design(rows):
    c=rows[:,0];x=(rows[:,1]-256.5)/256.;z=(c-1200)/400
    return np.column_stack([np.ones(len(c)),x,z,x*x,x*z,z*z])
A=design(train);C=train[:,0]
# Coarse signed dispersion and offset search uses training centers only.
seeds=[]
for slope in np.r_[np.arange(-.60,-.40,.002),np.arange(.40,.60,.002)]:
    bs=np.arange(-65,65,.25)
    v=C[None,:]+bs[:,None]+slope*(train[:,1]-256.5)[None,:]
    e=abs(v-nearest(v));score=np.exp(-(e/.7)**2).sum(axis=1)
    for k in np.argsort(score)[-3:]:seeds.append((score[k],bs[k],slope))
seeds=sorted(seeds,reverse=True)[:80]
candidates=[]
for _,b,s in seeds:
    par=np.array([b,s*256,0,0,0,0.])
    for _ in range(8):
        predicted=C+A@par;target=nearest(predicted)
        use=(abs(predicted-target)<1.5)&(predicted>=ref.min())&(predicted<=ref.max())
        if use.sum()<10:break
        result=least_squares(lambda t:(C+A@t-target)[use],par,loss='soft_l1',f_scale=.25,
                             bounds=([-70,-160,-15,-10,-15,-10],[70,160,15,10,15,10]))
        par=result.x
    pred=C+A@par;e=pred-nearest(pred);valid=(pred>=ref.min())&(pred<=ref.max())
    score=int(((abs(e)<.3)&valid).sum());rms=float(np.sqrt(np.mean(e[valid]**2)))
    if not any(np.max(abs(A@(par-r['coefficients'])))<.2 for r in candidates):
        candidates.append(dict(score=score,train_rms_all=rms,coefficients=par.tolist()))
candidates.sort(key=lambda r:(-r['score'],r['train_rms_all']))
best=candidates[0];par=np.array(best['coefficients'])
def report(rows):
    v=rows[:,0]+design(rows)@par;t=nearest(v)
    return [dict(center=float(c),pixel=float(p),predicted_nm=float(w),nearest_nm=float(n),error_nm=float(w-n)) for (c,p),w,n in zip(rows,v,t)]
out=dict(status='diagnostic_only',model='center + b + s*x + k*z + q*x*x + r*x*z + t*z*z',
         candidates=candidates[:12],training=report(train),independent_checks=report(check))
(root/'hypotheses.json').write_text(json.dumps(out,indent=2),encoding='utf-8')
print('features',len(train),len(check));print('candidates',candidates[:4])
for c in sorted(set(check[:,0])):
    r=[x for x in out['independent_checks'] if x['center']==c]
    print(c,'check residuals',[(round(x['pixel'],2),round(x['error_nm'],3)) for x in r])
