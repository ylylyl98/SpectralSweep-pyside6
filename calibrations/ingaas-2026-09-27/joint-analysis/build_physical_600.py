"""Model-assisted Ne/Ar assignments; preserves rejected peaks in report."""
from pathlib import Path
import sys,json,xml.etree.ElementTree as E
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
import numpy as np
from scipy.optimize import least_squares
from app.physical_wavelength_calibration import geometry_axis,fit_physical_calibration
from app.auto_wavelength_calibration import peaks
root=Path(__file__).resolve().parent
params=json.loads((root/'ingaas-300-physical-900-1700.json').read_text())['parameters']
refs=[]
for s in E.parse('calibrations/ingaas-2026-09-26/LightField-SourceSpectra.xml').getroot().findall('source'):
    if s.get('name') not in ['PI Neon','Neon','Argon','PI HgNeAr','NIR PI Neon']:continue
    for l in s.findall('line'):
        if l.text and l.get('s','').startswith(('Ne','Ar')):
            refs.extend((float(l.text)*o,o,float(l.text)) for o in [1,2] if 700<float(l.text)*o<1900)
refs=sorted(set(refs));v=np.array([a[0] for a in refs]);rows=[]
for c in [1000,1200,1400,1600]:
    f=root/'model-checks'/('g600-center%d.json'%c);d=json.loads(f.read_text())
    for p in peaks(d['counts']):rows.append([c,p])
rows=np.array(rows);prior=geometry_axis(rows[:,0],rows[:,1],params,600)
seeds=[]
for b in np.arange(-.6,.31,.05):
    for k in np.arange(-.0025,.0001,.0001):
        pred=prior+b+k*(rows[:,0]-1000)
        error=np.min(abs(pred[:,None]-v),axis=1)
        seeds.append((float(np.exp(-(error/.12)**2).sum()),b,k))
_,b,k=max(seeds);pred=prior+b+k*(rows[:,0]-1000)
ids=np.argmin(abs(pred[:,None]-v),axis=1);use=abs(pred-v[ids])<.6
# Freeze training assignments before looking at independent check spectra.
train=[dict(center_nm=float(c),pixel=float(p),wavelength_nm=float(v[j]),order=refs[j][1],source_wavelength_nm=refs[j][2])
       for (c,p),j,yes in zip(rows,ids,use) if yes]
a=np.array([[r['center_nm'],r['pixel'],r['wavelength_nm']] for r in train])
sol=least_squares(lambda t:geometry_axis(a[:,0],a[:,1],t,600)-a[:,2],params,
                  bounds=([4000,1,-.6,-.5,-.02],[10000,512,.6,.5,.02]),loss='soft_l1',f_scale=.1,x_scale='jac',max_nfev=3000)
checks=[];unmatched=[]
for c in [1100,1300,1500,1700]:
    file=root/'model-checks'/('g600-center%d.json'%c);frame=json.loads(file.read_text())
    for p in peaks(frame['counts']):
        pred=float(geometry_axis(c,p,sol.x,600));j=np.argmin(abs(v-pred))
        r=dict(center_nm=c,pixel=float(p),wavelength_nm=float(v[j]),order=refs[j][1],source_wavelength_nm=refs[j][2],file=file.name,error_nm=pred-float(v[j]))
        (checks if abs(r['error_nm'])<=.75 else unmatched).append(r)
evidence=dict(training=train,checks=checks,context=frame['context'],unmatched_checks=unmatched,
              training_unmatched=[dict(center_nm=float(c),pixel=float(p)) for (c,p),yes in zip(rows,use) if not yes],
              assignment_seed=dict(offset_nm=b,center_slope=k))
(root/'physical-600-evidence.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
print('training',len(train),'checks',len(checks),'unmatched',len(unmatched),'check rms',np.mean([r['error_nm']**2 for r in checks])**.5)
record=fit_physical_calibration(train,checks,frame['context'])
record['provenance']=dict(assignment='Model-assisted mixed-order Ne/Ar; training assignments frozen before checks; check identification window 0.75 nm',
                          unmatched_checks=unmatched,training_unmatched=evidence['training_unmatched'])
(root/'ingaas-600-physical-900-1700.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
print('MODEL RMS',record['rms_nm'],'MAX',record['max_check_error_nm'])
