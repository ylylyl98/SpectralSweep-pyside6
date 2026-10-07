"""Acquire WinSpec evidence; never write calibration settings or capture PIXIS."""
from pathlib import Path
import sys,json,time
from datetime import datetime,timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
sys.stdout.reconfigure(encoding='utf-8')
from lf6_automation import LF6Setup, SpectrometerSettings, ExperimentSettings
from app.devices.lightfield_optics import read_optics,apply_optics
from app.devices.winspec_adapter import WinSpecSetup
from app.auto_wavelength_calibration import peaks

folder=Path(__file__).resolve().parent/'model-checks'
folder.mkdir(exist_ok=True)
lf=LF6Setup();original=None
def read_calibration():
    out={}
    for cls,names in [(SpectrometerSettings,['CalibrationInformationFocalLength','CalibrationInformationInclusionAngle','CalibrationInformationDetectorAngle']),
                      (ExperimentSettings,['WavelengthCalibrationFocalLength','WavelengthCalibrationInclusionAngle','WavelengthCalibrationDetectorAngle'])]:
        for name in names:
            try:out[name]=str(lf.experiment.GetValue(getattr(cls,name)))
            except Exception as exc:out[name]={'unavailable':str(exc)}
    return out
try:
    if not lf.is_ready:lf.load_experiment('2100')
    deadline=time.monotonic()+60
    while not lf.is_ready or lf.is_busy:
        if time.monotonic()>deadline:raise RuntimeError('LightField not ready')
        time.sleep(.5)
    original=read_optics(lf)
    before=read_calibration()
    (folder/'settings-before.json').write_text(json.dumps(dict(optics=original,calibration=before),indent=2))
    print('Initial settings '+json.dumps(before),flush=True)
    camera=WinSpecSetup(lf,output_route='side')
    for density,centers in [(300,[925,1175,1425,1675]),(600,[1000,1200,1400,1600,1100,1300,1500,1700])]:
        matches=[r['index'] for r in original['grating_infos'] if ',%d]'%density in r['index']]
        if len(matches)!=1:raise RuntimeError('Grating not uniquely identified')
        apply_optics(lf,{'grating':matches[0]})
        for c in centers:
            if (folder/'STOP').exists():raise RuntimeError('Stopped by operator')
            path=folder/('g%d-center%d.json'%(density,c))
            if path.exists():continue
            exposure=30000 if c>=1500 else 10000
            camera.configure_for_acquisition(center_nm=c,exposure_ms=exposure,frames=1)
            _,counts=camera.acquire()
            if not camera.last_calibration_context:raise RuntimeError('Optics identity changed')
            frame=dict(counts=counts.tolist(),context=camera.last_calibration_context,exposure_ms=exposure,
                       captured_utc=datetime.now(timezone.utc).isoformat(),
                       winspec_datatype=camera._last_frame['winspec_datatype'],
                       temperature_guard=camera._last_frame['temperature_guard'])
            path.write_text(json.dumps(frame,indent=2),encoding='utf-8')
            print(dict(grating=density,center=c,peaks=peaks(counts).tolist()),flush=True)
finally:
    try:
        if original:
            apply_optics(lf,{k:original[k] for k in ['grating','wavelength_nm','output_port']})
            after=read_calibration()
            (folder/'settings-after.json').write_text(json.dumps(dict(optics=read_optics(lf),calibration=after),indent=2))
            print('Calibration readback unchanged: '+str(before==after),flush=True)
    finally:lf.close()
