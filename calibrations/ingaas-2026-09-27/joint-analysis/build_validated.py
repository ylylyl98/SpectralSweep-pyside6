"""Reproduce the accepted interval; failed intervals remain excluded."""
from pathlib import Path
import sys,json,hashlib
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from app.wavelength_calibration import fit_calibration,fit_broad_calibration
root=Path(__file__).resolve().parent
d=json.loads((root/'hypotheses.json').read_text());records={};evidence=[]
for c in [1300,1350,1400,1450,1500]:
    rows=sorted([x for x in d['training']+d['independent_checks'] if x['center']==c],key=lambda x:x['pixel'])
    path=sorted((root.parent/'direct-session').glob('g0-center%d-try*.json'%c))[-1]
    frame=json.loads(path.read_text())
    checks=rows[1:-1:3];fit=[r for r in rows if r not in checks]
    records[c]=fit_calibration([r['pixel'] for r in fit],[r['nearest_nm'] for r in fit],
                              [r['pixel'] for r in checks],[r['nearest_nm'] for r in checks],frame['context'],2,.2)
    records[c]['source']='Joint Ne/Ar assignment; local quadratic; separate center validation in broad model'
    evidence.append(dict(file=path.name,sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
record=fit_broad_calibration([records[c] for c in [1300,1400,1500]],[records[c] for c in [1350,1450]],.2)
record['source']='Joint Ne/Ar matching; centers 1350 and 1450 withheld from hypothesis search; other intervals not enabled'
record['joint_analysis']=dict(evidence=evidence,training_score=49,runner_up_score=35,
    reference_sha256=hashlib.sha256(Path('calibrations/ingaas-2026-09-26/LightField-SourceSpectra.xml').read_bytes()).hexdigest(),
    hypotheses_sha256=hashlib.sha256((root/'hypotheses.json').read_bytes()).hexdigest())
(root/'ingaas-300-validated-1300-1500.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
print(record['rms_nm'])
