"""Inspect held-out frames; export visibly provisional axes, never activate them."""
import json
from pathlib import Path
import numpy as np
from scipy.signal import savgol_filter
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

OUT=Path(__file__).resolve().parent
m=json.loads((OUT/'trial-model.json').read_text())
a,b,c,d,e,f,sigma=m['parameters']
def axis(p,center):
    q=(np.asarray(p)-256.5)/256;t=(center-1200)/400
    return center+a+b*t+c*t*t+(d+e*t)*q+f*q*q

rows=[]
# Previously identified candidate correspondences; fixed before checking residuals.
# These are held-out EXPOSURES, not novel reference species or lines.
for center,peak,reference in [(950,196,965.7786),(950,302,912.2967),(1050,394,965.7786)]:
    frame=next(r for r in m['frames'] if r['center_nm']==center)
    assert frame['role']=='check'
    raw=json.loads(Path(frame['file']).read_text())
    y=savgol_filter(raw['counts'],11,2);i=peak-1;v=y[i-1:i+2]
    offset=.5*(v[0]-v[2])/(v[0]-2*v[1]+v[2]);pixel=peak+offset
    measured=float(axis(pixel,center))
    rows.append(dict(center_nm=center,pixel=float(pixel),reference_nm=reference,
                     trial_nm=measured,residual_nm=measured-reference))
same=np.array([r['residual_nm'] for r in rows if r['center_nm']==950])
pixis=json.loads((OUT.parent/'rms-diagnostic.json').read_text())
result=dict(status='FAIL_0.2_NM_TARGET_NOT_ACTIVATED',line_checks=rows,
    inga_as_950_two_candidate_line_rms_nm=float(np.sqrt(np.mean(same**2))),
    pixis_950_same_two_candidate_line_rms_nm=pixis['rms_nm'],
    all_three_candidate_observations_rms_nm=float(np.sqrt(np.mean([r['residual_nm']**2 for r in rows]))),
    note='Provisional line identities; InGaAs maxima smoothed across 11 pixels because of broad/noisy features; PIXIS maxima interpolated across 3 pixels. Blend/line-shape and wavelength-convention biases remain. Not IntelliCal certification. Checks hold out centers, not reference line identities.')
(OUT/'validation.json').write_text(json.dumps(result,indent=2),encoding='utf8')
for frame in m['frames']:
    center=frame['center_nm']
    if not 900<=center<=1400:continue
    raw=json.loads(Path(frame['file']).read_text());p=np.arange(1,513);wl=axis(p,center)
    assert np.isfinite(wl).all() and (np.diff(wl)<0).all()
    np.savetxt(OUT/f'UNVALIDATED-center-{center:g}nm.csv',np.c_[p,wl,raw['counts']],
               delimiter=',',header='original_pixel,UNVALIDATED_trial_wavelength_nm,intensity_counts',comments='')
text=f'''# WinSpec full-spectrum trial — NOT ACTIVATED

Implemented and run offline against the existing long-exposure dataset. No new
acquisition, instrument configuration, LightField calibration, or application
calibration setting was changed.

## Method

LightField VISNIR PI Neon reference line list; Gaussian broadened template;
multi-center polynomial wavelength map; per-frame positive Ar/Ne amplitudes and
linear background. This borrows the whole-spectrum-fitting idea, not the proprietary
IntelliCal implementation or manufacturer physical model. Broadening is fitted,
not removed from measured data. Three starting offsets converged to similar costs.
Reference wavelengths retain their stored numerical convention.

Training centers: 900, 1000, 1100, 1200, 1300, 1400 nm.
Held-out centers: 950, 1050, 1150, 1250, 1350 nm.
Center range is NOT a claim of fully validated wavelength coverage.

## Validation

Held-out whole-spectrum registration shift RMS: {m['check_spectral_registration_rms_nm']:.3f} nm.
This allows a diagnostic shift to measure mismatch; that shift is NOT applied
to the saved model. It is not independent line-position RMS or IntelliCal RMS.

Same two provisional reference peaks at 950 nm center:
- InGaAs trial axis residual RMS: {result['inga_as_950_two_candidate_line_rms_nm']:.3f} nm.
- PIXIS existing axis residual RMS: {pixis['rms_nm']:.3f} nm.

|Center nm|Reference nm|InGaAs trial nm|Residual nm|
|---|---|---|---|
'''+''.join(f"|{r['center_nm']}|{r['reference_nm']:.5f}|{r['trial_nm']:.5f}|{r['residual_nm']:+.5f}|\n" for r in rows)+'''
The trial fails the previously selected 0.2 nm target. It is NOT enabled in
SpectralSweep. The two-camera figures are limited local diagnostics: candidate
line identities, broad/blended InGaAs shapes, differing peak estimators and
unverified air/vacuum conventions prevent treating them as vendor calibration RMS.

## Deliverables and limits

`trial-model.json`: fitted parameters, split, hashes, quality metrics.
`validation.json`: explicitly held-out candidate peak errors.
`trial-spectrum-fits.png`: measured versus predicted counts, original pixels.
`UNVALIDATED-center-*.csv`: diagnostic trial axes, only for 900–1400 nm centers;
original pixel/count pairs preserved, never silently activated or extrapolated.
Centers above 1400 nm were excluded from the fitted domain because useful line
coverage becomes sparse. No 900–1700 nm accuracy claim is made.

Run `fit_trial.py` then `validate_trial.py` with the project Python to reproduce.
Raw files remain at the paths recorded in `trial-model.json`.
'''
(OUT/'README.md').write_text(text,encoding='utf8')
fig,ax=plt.subplots(figsize=(8,4))
ax.bar(['PIXIS 950 / 912','PIXIS 950 / 966','InGaAs 950 / 912','InGaAs 950 / 966','InGaAs 1050 / 966'],
       [pixis['lines'][0]['error_nm'],pixis['lines'][1]['error_nm'],rows[1]['residual_nm'],rows[0]['residual_nm'],rows[2]['residual_nm']],
       color=['tab:blue']*2+['tab:orange']*3)
ax.axhline(.2,color='red',ls='--');ax.axhline(-.2,color='red',ls='--');ax.axhline(0,color='black',lw=.7)
ax.set(ylabel='Measured minus reference (nm)',title='Provisional candidate-line residuals — NOT IntelliCal RMS')
ax.tick_params(axis='x',rotation=20);fig.tight_layout();fig.savefig(OUT/'trial-validation.png',dpi=150)
print(json.dumps(result,indent=2))
