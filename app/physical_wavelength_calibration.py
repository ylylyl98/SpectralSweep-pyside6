"""Application-only grating/camera geometry fit; never writes instrument settings.

This is our empirical geometry calibration, not Princeton's proprietary IntelliCal.
The measured check error is not a guarantee for unmeasured/extrapolated locations.
"""
import copy
import re
import uuid
from datetime import datetime,timezone
import numpy as np
from scipy.optimize import least_squares
from app.full_detector_calibration import _rows


def geometry_axis(center, pixel, parameters, grooves):
    focal_pixels,p0,half_angle,tilt,zero=parameters
    spacing=1e6/grooves
    ratio=np.asarray(center)/(2*spacing*np.cos(half_angle))
    if np.any(abs(ratio)>=1):raise ValueError('Center exceeds grating geometry limit')
    theta=np.arcsin(ratio)+zero
    u=-(np.asarray(pixel)-p0)
    phi=np.arctan2(u*np.cos(tilt),focal_pixels+u*np.sin(tilt))
    return spacing*(np.sin(theta-half_angle)+np.sin(theta+half_angle+phi))


def fit_physical_calibration(training,checks,context,tolerance_nm=.2):
    from app.wavelength_calibration import valid_context
    if not valid_context(context) or context['geometry']!=[512,1]:
        raise ValueError('Complete 512 x 1 detector context required')
    s=str(context['grating']);match=re.search(r',\s*(\d+)\s*\]',s)
    grooves=float(match.group(1)) if match else float(s) if s.isdigit() else 0
    if grooves<=0 or 2e6/grooves<=1700:raise ValueError('Grating cannot cover center range 900-1700 nm')
    a,b=_rows(training),_rows(checks)
    if set(a[:,0])&set(b[:,0]):raise ValueError('Independent check centers required')
    if len(set(a[:,0]))<4 or np.ptp(a[:,0])<400 or np.ptp(a[:,1])<300:
        raise ValueError('Need four distributed training centers and detector-spanning peaks')
    if len(set(b[:,0]))<2 or len(b)<6:raise ValueError('Need at least two independent check centers and six peaks')
    if not np.isfinite(tolerance_nm) or tolerance_nm<=0:raise ValueError('Positive RMS tolerance required')
    fit=least_squares(lambda t:geometry_axis(a[:,0],a[:,1],t,grooves)-a[:,2],
                      [6000.,256.5,.2,0.,0.],bounds=([4000.,1.,-.6,-.5,-.02],[10000.,512.,.6,.5,.02]),
                      loss='soft_l1',f_scale=.1,x_scale='jac',max_nfev=3000)
    if not fit.success or not np.all(np.isfinite(fit.x)):
        raise ValueError('Physical fit did not converge')
    train_error=geometry_axis(a[:,0],a[:,1],fit.x,grooves)-a[:,2]
    error=geometry_axis(b[:,0],b[:,1],fit.x,grooves)-b[:,2]
    rms=float(np.sqrt(np.mean(error**2)))
    if rms>tolerance_nm or np.sqrt(np.mean(train_error**2))>tolerance_nm:
        raise ValueError('Physical fit/check RMS exceeds tolerance: %.4f nm'%rms)
    centers=np.repeat(np.linspace(900,1700,321),512);pixels=np.tile(np.arange(1,513),321)
    axes=geometry_axis(centers,pixels,fit.x,grooves).reshape(321,512)
    if not np.all(np.isfinite(axes)) or np.any(axes<=0) or not np.all(np.diff(axes,axis=1)<0):
        raise ValueError('Invalid physical wavelength mapping')
    return dict(kind='physical_grating_model',id=str(uuid.uuid4()),created_utc=datetime.now(timezone.utc).isoformat(),
                context=copy.deepcopy(context),parameters=fit.x.tolist(),grooves_per_mm=grooves,
                parameter_names=['effective_focal_length_pixels','reference_pixel','half_inclusion_rad','detector_tilt_rad','rotation_offset_rad'],
                training=copy.deepcopy(training),checks=copy.deepcopy(checks),center_range=[900.,1700.],pixel_range=[1,512],
                measured_training_center_range=[float(a[:,0].min()),float(a[:,0].max())],
                checked_centers_nm=sorted(set(b[:,0].tolist())),rms_nm=rms,max_check_error_nm=float(max(abs(error))),
                check_residual_nm=error.tolist(),fit_rms_nm=float(np.sqrt(np.mean(train_error**2))),
                tolerance_nm=float(tolerance_nm),source='Application-only physical grating model; independent checks',
                accuracy_scope='RMS describes check peaks only; other centers/pixels use physical-model prediction')


def physical_axis(record,context):
    from app.wavelength_calibration import valid_context,_fixed_identity
    if not valid_context(context) or _fixed_identity(record['context'])!=_fixed_identity(context):return None
    c=float(context['center_nm'])
    if not 900<=c<=1700:return None
    r=fit_physical_calibration(record['training'],record['checks'],record['context'],record['tolerance_nm'])
    axis=geometry_axis(c,np.arange(1,513),r['parameters'],r['grooves_per_mm'])
    if np.any(axis<=0) or not np.all(np.isfinite(axis)) or not np.all(np.diff(axis)<0):
        raise ValueError('Requested physical axis is invalid')
    return axis,np.ones(512,dtype=bool)
