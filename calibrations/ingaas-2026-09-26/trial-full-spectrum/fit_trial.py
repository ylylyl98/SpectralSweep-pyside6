"""Offline experimental full-spectrum fit. Never writes instrument/app settings."""
import json, os, hashlib
from pathlib import Path
import xml.etree.ElementTree as ET
import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.optimize import least_squares, minimize_scalar
from scipy.signal import find_peaks, savgol_filter
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

OUT=Path(__file__).resolve().parent
ROOT=OUT.parent
RAW=Path(os.environ['APPDATA'])/'SpectralSweep/winspec-calibration-captures'
files=sorted(RAW.glob('*.json'))
# Explicitly select the completed long-exposure run, excluding the preceding runs.
files=[f for f in files if '20260926T210501' <= f.name[5:] <= '20260926T210846']
frames=[dict(json.loads(f.read_text()), file=str(f),sha256=hashlib.sha256(f.read_bytes()).hexdigest()) for f in files]
assert len(frames)==17, len(frames)
assert [f['context']['center_nm'] for f in frames]==list(range(900,1701,50))
ref=ROOT/'LightField-SourceSpectra.xml'
source=next(s for s in ET.parse(ref).getroot().findall('source') if s.get('name')=='VISNIR PI Neon')
lines=[(float(l.text),float(l.get('rel',0)),l.get('s','')) for l in source.findall('line') if l.text and 650<float(l.text)<1800 and float(l.get('rel',0))>0]
grid=np.arange(600,1850,.1)
sticks=np.zeros((2,len(grid)))
for w,a,s in lines:
    sticks[0 if 'Ar' in s else 1,int(round((w-grid[0])/.1))]+=a
sticks/=sticks.max()
p=np.arange(1,513); q=(p-256.5)/256
def axis(theta,c):
    t=(c-1200)/400
    a,b,cc,d,e,f=theta[:6]
    return c+a+b*t+cc*t*t+(d+e*t)*q+f*q*q
def templates(theta):
    return gaussian_filter1d(sticks,theta[6]/.1,axis=1)
def project(theta,frame,shift=0):
    c=frame['context']['center_nm']; wl=axis(theta,c)+shift
    curves=templates(theta)
    ar,ne=[np.interp(wl,grid,s) for s in curves]
    # Per-frame background + independently fitted species amplitudes.
    A=np.array([np.ones(512),q,ar,ne]).T
    y=np.array(frame['counts'],float)
    coef=np.linalg.lstsq(A,y,rcond=None)[0]
    # A negative species amplitude cannot represent emitted light.
    if min(coef[2:])<0:
        from scipy.optimize import lsq_linear
        coef=lsq_linear(A,y,bounds=([-np.inf,-np.inf,0,0],np.inf)).x
    pred=A@coef
    scale=max(np.percentile(y,95)-np.percentile(y,10),500)
    return (pred-y)/scale,pred,coef

# Split BEFORE fitting. Never include check centers in the optimization.
train=[f for f in frames if f['context']['center_nm'] in [900,1000,1100,1200,1300,1400]]
checks=[f for f in frames if f['context']['center_nm'] in [950,1050,1150,1250,1350]]
initial=np.array([-14,0,0,-129,0,0,3.5])
lower=[-40,-20,-15,-150,-20,-10,1]
upper=[10,20,15,-95,20,10,9]
def residual(theta):
    return np.concatenate([project(theta,f)[0] for f in train])
fits=[]
for offset in [-3,0,3]:
    start=initial.copy();start[0]+=offset
    fit=least_squares(residual,start,bounds=(lower,upper),loss='soft_l1',f_scale=.15,max_nfev=160)
    fits.append(fit)
best=min(fits,key=lambda a:a.cost);theta=best.x
results=[]
for frame in frames:
    c=frame['context']['center_nm'];err,pred,coef=project(theta,frame)
    y=np.array(frame['counts']); noise=np.median(abs(np.diff(y)-np.median(np.diff(y))))/.6745/np.sqrt(2)
    peak,_=find_peaks(savgol_filter(y,11,2),prominence=max(6*noise,1200),distance=14)
    check=c in [f['context']['center_nm'] for f in checks]
    # Independent registration diagnostic: freeze wavelength map, vary ONLY shift.
    shift=minimize_scalar(lambda s:np.mean(project(theta,frame,s)[0]**2),bounds=(-8,8),method='bounded') if check else None
    results.append(dict(center_nm=c,role='train' if frame in train else ('check' if check else 'outside_fitted_centers'),
        normalized_intensity_rmse=float(np.sqrt(np.mean(err**2))),strong_peak_count=len(peak),
        check_registration_shift_nm=float(shift.x) if shift else None,
        species_amplitudes=coef[2:].tolist(),file=frame['file'],sha256=frame['sha256']))
shifts=np.array([r['check_registration_shift_nm'] for r in results if r['role']=='check'])
summary=dict(status='EXPERIMENTAL_NOT_ACTIVATED',method='Multi-center polynomial wavelength map, Gaussian-broadened full reference spectrum, variable-projection nonnegative species amplitudes and linear background',
    reference='VISNIR PI Neon',reference_sha256=hashlib.sha256(ref.read_bytes()).hexdigest(),parameters=theta.tolist(),
    parameter_names=['center_offset_nm','center_linear_nm','center_quadratic_nm','pixel_linear_nm','pixel_center_cross_nm','pixel_quadratic_nm','gaussian_sigma_nm'],
    formula='lambda_nm = center + a + b*t + c*t^2 + (d+e*t)*q + f*q^2; t=(center-1200)/400; q=(pixel-256.5)/256',
    training_centers=[900,1000,1100,1200,1300,1400],check_centers=[950,1050,1150,1250,1350],
    check_spectral_registration_rms_nm=float(np.sqrt(np.mean(shifts**2))),
    accepted_as_calibration=False,acceptance_target_nm=.2,
    note='Registration RMS is the shift needed to best align held-out spectra, NOT independent atomic-line wavelength RMS or IntelliCal reported error. No held-out correction applied. No global extrapolation allowed. Full-template mismatch, line blending and reference intensity variations can bias the model. Air/vacuum convention unverified.',
    optimizer_success=bool(best.success),multistart_costs=[float(f.cost) for f in fits],frames=results)
(OUT/'trial-model.json').write_text(json.dumps(summary,indent=2),encoding='utf8')
fig,axs=plt.subplots(6,2,figsize=(13,16))
for ax,frame in zip(axs.flat,frames[:12]):
    c=frame['context']['center_nm'];_,pred,_=project(theta,frame)
    ax.plot(p,frame['counts'],lw=.7,label='Measured');ax.plot(p,pred,lw=1,label='Template fit')
    role=next(r['role'] for r in results if r['center_nm']==c)
    ax.set_title(f'{c:g} nm center / {role}');ax.set_xlabel('Original pixel');ax.set_ylabel('Counts')
axs[0,0].legend();fig.suptitle('Experimental full-spectrum fit — not an activated wavelength calibration')
fig.tight_layout(rect=(0,0,1,.98));fig.savefig(OUT/'trial-spectrum-fits.png',dpi=130)
print(json.dumps({k:v for k,v in summary.items() if k!='frames'},indent=2))
print(json.dumps(results,indent=2))
