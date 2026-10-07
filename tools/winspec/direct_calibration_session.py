"""One-off supervised calibration collection using existing device adapters."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import json
sys.stdout.reconfigure(encoding="utf-8")
import time
from datetime import datetime, timezone
from lf6_automation import LF6Setup
from app.devices.lightfield_optics import read_optics, apply_optics
from app.devices.winspec_adapter import WinSpecSetup
from app.auto_wavelength_calibration import exposure_retry, build_broad, peaks
from app.wavelength_calibration import reference_lines

folder=Path('calibrations/ingaas-2026-09-27/direct-session')
folder.mkdir(parents=True,exist_ok=True)
def log(value):
    print(value,flush=True)
print('Opening one LightField automation instance',flush=True)
lf=LF6Setup()
try:
    log('Automation connected')
    if not lf.is_ready:
        lf.load_experiment('2100')
    deadline=time.monotonic()+60
    while not lf.is_ready or lf.is_busy:
        if time.monotonic()>deadline:raise RuntimeError('LightField not ready')
        time.sleep(.5)
    optics=read_optics(lf);log(optics)
    camera=WinSpecSetup(lf,output_route='side')
    reference=reference_lines(Path('calibrations/ingaas-2026-09-26/LightField-SourceSpectra.xml'))
    gratings=[x['index'] for x in optics['grating_infos'] if ',1200]' not in x['index']]
    gratings.sort(key=lambda g: ',300]' not in g)
    results=json.loads((folder/"results.json").read_text(encoding="utf-8")) if (folder/"results.json").exists() else []
    for gi,grating in enumerate(gratings):
        if any(r['grating']==grating for r in results):continue
        log('Grating '+grating);apply_optics(lf,{'grating':grating})
        frames=[]
        for center in range(900,1701,50):
            if (folder/'STOP').exists():raise RuntimeError('Stopped by operator')
            exposure=1000.
            for attempt in range(5):
                camera.configure_for_acquisition(center_nm=center,exposure_ms=exposure,frames=1)
                _,counts=camera.acquire()
                if not camera.last_calibration_context:raise RuntimeError('Optics identity changed')
                frame=dict(counts=counts.tolist(),context=camera.last_calibration_context,
                           captured_utc=datetime.now(timezone.utc).isoformat(),exposure_ms=exposure,
                           winspec_datatype=camera._last_frame['winspec_datatype'],
                           temperature_guard=camera._last_frame['temperature_guard'])
                path=folder/('g%d-center%d-try%d.json'%(gi,center,attempt))
                path.write_text(json.dumps(frame,indent=2),encoding='utf-8')
                log({'center':center,'exposure_ms':exposure,'peaks':len(peaks(counts)),'file':str(path)})
                retry=exposure_retry(frame,exposure,attempt)
                if retry is None:break
                exposure=retry
            frames.append(frame)
        try:
            model=build_broad(frames,reference,progress=log)
            path=folder/('grating%d-calibration.json'%gi)
            path.write_text(json.dumps(model,indent=2),encoding='utf-8')
            results.append(dict(grating=grating,model=str(path),rms_nm=model['rms_nm']))
        except ValueError as exc:
            results.append(dict(grating=grating,error=str(exc)))
        (folder/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
        log(results[-1])
finally:
    lf.close()
