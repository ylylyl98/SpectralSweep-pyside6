import json,sys
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
root=Path.cwd();sys.path.insert(0,str(root))
from app.physical_wavelength_calibration import geometry_axis
base=root/'calibrations/ingaas-2026-09-27'
out=base/'joint-analysis/stitched-spectrum';out.mkdir(exist_ok=True)
m=json.loads((base/'joint-analysis/ingaas-300-physical-900-1700.json').read_text())
names=sorted({v['file'] for key in ('training','checks') for v in m[key]})
grid=np.arange(900.,1700.0001,.25); total=np.zeros_like(grid); weights=total.copy();coverage=total.copy();sources=[]
fig,(ax,ax2)=plt.subplots(2,1,figsize=(15,6.8),sharex=True,gridspec_kw={'height_ratios':[4,1]})
for name in names:
    paths=list(base.rglob(name));assert len(paths)==1
    p=paths[0]; d=json.loads(p.read_text()); y=np.asarray(d['counts'],float)
    assert y.shape==(512,) and np.isfinite(y).all() and d['context']['grating']==m['context']['grating']
    x=geometry_axis(d['context']['center_nm'],np.arange(1,513),m['parameters'],300.)
    order=np.argsort(x);x=x[order];y=y[order]
    baseline=float(np.percentile(y,10));rate=(y-baseline)/(d['exposure_ms']/1000.)
    # Blend measured overlap only, tapering detector edges. No wavelength extrapolation.
    w=np.sin(np.linspace(0,np.pi,512))**2+.02
    valid=(grid>=x[0])&(grid<=x[-1]); wi=np.interp(grid[valid],x,w)
    total[valid]+=np.interp(grid[valid],x,rate)*wi;weights[valid]+=wi;coverage[valid]+=1
    sources.append({'file':str(p.relative_to(base)),'center_nm':d['context']['center_nm'],'exposure_ms':d['exposure_ms'],'estimated_baseline_counts':baseline,'range_nm':[float(x[0]),float(x[-1])],'saturated_pixels':int((y>=65535).sum())})
valid=weights>0; merged=np.full_like(grid,np.nan);merged[valid]=total[valid]/weights[valid]
scale=float(np.nanmax(merged));relative=merged/scale
ax.plot(grid,relative,color='#146b93',lw=1.05)
ax.set_yscale('symlog',linthresh=.002);ax.set_ylim(-.003,1.3)
ax.set_ylabel('Relative signal (symlog scale)');ax.grid(alpha=.2)
ax.set_title('Ne / Ar calibration lamp | InGaAs | 300 lines/mm\nMeasured spectra stitched over 900-1700 nm',loc='left',fontsize=15,pad=14)
ax2.fill_between(grid,0,coverage,step='mid',color='#64a8b8',alpha=.6);ax2.set_ylabel('Spectra\ncovering bin');ax2.set_xlabel('Calibrated wavelength (nm; first-order-equivalent axis)');ax2.set_xlim(900,1700);ax2.set_xticks(np.arange(900,1701,50));ax2.grid(alpha=.2)
fig.text(.07,.015,'13 measured frames | exposure-normalized | estimated baseline removed | overlap blended | no response correction; higher orders may overlap',fontsize=9,color='#555555')
fig.tight_layout(rect=[0,.045,1,1]);fig.savefig(out/'ingaas-near-900-1700.png',dpi=180);fig.savefig(out/'ingaas-near-900-1700.pdf')
record={'method':'300 lines/mm physical axis; per-frame 10th-percentile baseline subtraction; counts/second; sine-squared edge-weighted overlap; 0.25nm display grid (not resolution); normalized to merged maximum. No extrapolation of intensity. No measured dark or spectral-response correction. First-order-equivalent axis includes higher-order light.','model_id':m['id'],'sources':sources,'wavelength_nm':grid.tolist(),'relative_signal':[float(v) if np.isfinite(v) else None for v in relative],'coverage':coverage.astype(int).tolist()}
(out/'stitched-data.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
print(json.dumps({'frames':len(sources),'uncovered_grid_bins':int((~valid).sum()),'saturated_pixels':sum(s['saturated_pixels'] for s in sources),'png':str(out/'ingaas-near-900-1700.png')}))
