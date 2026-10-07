"""Supervised diagnostic captures only; does not save/enable calibration."""
from pathlib import Path
import sys, json, time
from datetime import datetime, timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
sys.stdout.reconfigure(encoding='utf-8')
from lf6_automation import LF6Setup
from app.devices.lightfield_optics import read_optics, apply_optics
from app.devices.winspec_adapter import WinSpecSetup
from app.auto_wavelength_calibration import peaks

folder=Path(__file__).resolve().parent/'longwave-probe'
folder.mkdir(exist_ok=True)
lf=LF6Setup()
try:
    if not lf.is_ready: lf.load_experiment('2100')
    deadline=time.monotonic()+60
    while not lf.is_ready or lf.is_busy:
        if time.monotonic()>deadline: raise RuntimeError('LightField not ready')
        time.sleep(.5)
    optics=read_optics(lf)
    print(optics,flush=True)
    gratings=[r['index'] for r in optics['grating_infos'] if ',300]' in r['index']]
    if len(gratings)!=1: raise RuntimeError('300 lines/mm grating not uniquely identified')
    apply_optics(lf,{'grating':gratings[0]})
    camera=WinSpecSetup(lf,output_route='side')
    for center,exposure in [(1500,10000),(1650,30000),(1700,30000)]:
        camera.configure_for_acquisition(center_nm=center,exposure_ms=exposure,frames=1)
        _,counts=camera.acquire()
        if not camera.last_calibration_context: raise RuntimeError('Optics identity changed')
        frame=dict(counts=counts.tolist(),context=camera.last_calibration_context,
                   exposure_ms=exposure,captured_utc=datetime.now(timezone.utc).isoformat(),
                   winspec_datatype=camera._last_frame['winspec_datatype'],
                   temperature_guard=camera._last_frame['temperature_guard'])
        name=datetime.now(timezone.utc).strftime('%H%M%S')+'-center%d.json'%center
        (folder/name).write_text(json.dumps(frame,indent=2),encoding='utf-8')
        print({'file':name,'peaks':peaks(counts).tolist(),'maximum':float(max(counts))},flush=True)
finally:
    lf.close()
