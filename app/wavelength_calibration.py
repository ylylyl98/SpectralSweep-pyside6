"""Fixed-position wavelength fits. No hardware access; never extrapolate."""
from datetime import datetime, timezone
import copy
import uuid
import numpy as np
from app.full_detector_calibration import fit_full_detector_calibration, full_detector_axis
from app.physical_wavelength_calibration import fit_physical_calibration, physical_axis

KEYS = ('profile', 'grating', 'center_nm', 'output_port', 'detector', 'geometry', 'spectrometer')

def valid_context(context):
    return (isinstance(context, dict) and all(context.get(k) not in (None, '', []) for k in KEYS)
            and np.isfinite(float(context['center_nm'])))

def fit_calibration(pixels, wavelengths, check_pixels, check_wavelengths, context, degree=1, tolerance_nm=.2):
    if not valid_context(context):
        raise ValueError('Missing live optics/detector identity; acquire a new calibration frame')
    p, w, cp, cw = [np.asarray(v, dtype=float) for v in (pixels, wavelengths, check_pixels, check_wavelengths)]
    if degree not in (1, 2) or len(p) < degree+2 or len(cp) < 1 or p.shape != w.shape or cp.shape != cw.shape:
        raise ValueError('Use at least degree + 2 fit lines and one separate validation line')
    if not all(v.ndim == 1 and np.all(np.isfinite(v)) for v in (p,w,cp,cw)):
        raise ValueError('All pairs must be finite numbers')
    if not np.isfinite(tolerance_nm) or tolerance_nm <= 0:
        raise ValueError('Tolerance must be positive')
    if len(set(np.r_[p,cp])) != len(p)+len(cp) or len(set(np.r_[w,cw])) != len(w)+len(cw):
        raise ValueError('Fit and validation must use distinct peaks and wavelengths')
    if min(p.min(),cp.min()) < 1 or max(p.max(),cp.max()) > 512 or min(w.min(),cw.min()) <= 0:
        raise ValueError('Pixels must be 1–512 and wavelengths positive')
    if cp.min() < p.min() or cp.max() > p.max():
        raise ValueError('Validation peaks must lie within the fitted pixel range')
    coeff = np.polyfit(p, w, degree)
    fit_error, check_error = np.polyval(coeff,p)-w, np.polyval(coeff,cp)-cw
    axis = np.polyval(coeff,np.linspace(p.min(),p.max(),513))
    if not (np.all(np.diff(axis)>0) or np.all(np.diff(axis)<0)) or np.any(axis<=0):
        raise ValueError('Wavelength mapping must be positive and monotonic')
    if max(np.max(np.abs(fit_error)),np.max(np.abs(check_error))) > tolerance_nm:
        raise ValueError('Fit/validation error exceeds tolerance; inspect peak assignments')
    return dict(id=str(uuid.uuid4()), created_utc=datetime.now(timezone.utc).isoformat(),
                context=copy.deepcopy(context), degree=degree, coefficients=coeff.tolist(),
                pixel_range=[float(p.min()),float(p.max())], fit_pixels=p.tolist(), fit_nm=w.tolist(),
                check_pixels=cp.tolist(), check_nm=cw.tolist(), fit_residual_nm=fit_error.tolist(),
                check_residual_nm=check_error.tolist(), rms_nm=float(np.sqrt(np.mean(fit_error**2))),
                tolerance_nm=float(tolerance_nm), source='USB-Hg_NeAr; manually confirmed line assignments')

def calibrated_axis(record, context):
    if record.get('kind') == 'physical_grating_model':
        return physical_axis(record, context)
    if record.get('kind') == 'full_detector_surface':
        return full_detector_axis(record, context)
    if record.get('kind') == 'broad_piecewise':
        return broad_axis(record, context)
    if not valid_context(context) or record.get('context') != context:
        return None
    # Revalidate saved coefficients from their original evidence, not arbitrary JSON coefficients.
    checked = fit_calibration(record['fit_pixels'],record['fit_nm'],record['check_pixels'],record['check_nm'],
                              context,record['degree'],record['tolerance_nm'])
    pixels = np.arange(1,513)
    lo,hi = checked['pixel_range']
    mask=(pixels>=lo)&(pixels<=hi)
    return np.polyval(checked['coefficients'],pixels[mask]),mask

def reference_lines(path):
    import xml.etree.ElementTree as ET
    root=ET.parse(path).getroot()
    source=next(s for s in root.findall('source') if s.get('name')=='NIR PI Neon')
    return sorted([(float(x.text), x.get('s',''), float(x.get('rel',0))) for x in source.findall('line')
                   if x.text and 850<=float(x.text)<=1800])

def _fixed_identity(context):
    return {key:context[key] for key in KEYS if key!='center_nm'}

def validate_imported_calibration(record):
    """Recompute evidence; never trust coefficients/errors supplied by a file."""
    if not isinstance(record, dict):
        raise ValueError('Calibration must be a JSON object')
    if record.get('kind') == 'physical_grating_model':
        verified = fit_physical_calibration(record['training'], record['checks'], record['context'], record['tolerance_nm'])
    elif record.get('kind') == 'full_detector_surface':
        verified = fit_full_detector_calibration(record['training'], record['checks'], record['context'], record['tolerance_nm'])
    elif record.get('kind') == 'broad_piecewise':
        verified = fit_broad_calibration(record['anchors'], record['checks'], record['tolerance_nm'])
    elif record.get('kind') is None and 'fit_pixels' in record:
        verified = _refit(record)
    else:
        raise ValueError('Unsupported calibration format; raw captures are not calibration models')
    verified['imported_from_id'] = record.get('id')
    verified['source'] = record.get('source', verified['source'])
    if 'provenance' in record:
        verified['provenance'] = copy.deepcopy(record['provenance'])
    return verified

def _refit(record):
    return fit_calibration(record['fit_pixels'],record['fit_nm'],record['check_pixels'],record['check_nm'],
                           record['context'],record['degree'],record['tolerance_nm'])

def fit_broad_calibration(anchors, checks, tolerance_nm=.2):
    """Empirical center interpolation, with independent center evidence for every usable segment."""
    if len(anchors)<3 or not checks or not np.isfinite(tolerance_nm) or tolerance_nm<=0:
        raise ValueError('Broad calibration needs ≥3 anchor centers, independent check centers and positive tolerance')
    a=sorted([_refit(r) for r in anchors],key=lambda r:r['context']['center_nm'])
    v=[_refit(r) for r in checks]
    identity=_fixed_identity(a[0]['context'])
    centers=[r['context']['center_nm'] for r in a]
    if len(set(centers))!=len(centers) or any(r['context']['center_nm'] in centers for r in v):
        raise ValueError('Anchor centers must be distinct; validation centers must be independent')
    if any(_fixed_identity(r['context'])!=identity for r in a+v):
        raise ValueError('All records must use the same grating, detector, profile and exit')
    segments=[]
    for left,right in zip(a,a[1:]):
        low,high=left['context']['center_nm'],right['context']['center_nm']
        validation=[r for r in v if low<r['context']['center_nm']<high]
        if not validation: continue
        # Coverage is limited to the pixel interval independently measured at every center.
        lo=max(r['pixel_range'][0] for r in [left,right]+validation)
        hi=min(r['pixel_range'][1] for r in [left,right]+validation)
        if hi-lo<20: continue
        pixels=np.linspace(lo,hi,513)
        yl=np.polyval(left['coefficients'],pixels); yr=np.polyval(right['coefficients'],pixels)
        if not ((np.all(np.diff(yl)>0) and np.all(np.diff(yr)>0)) or
                (np.all(np.diff(yl)<0) and np.all(np.diff(yr)<0))):
            raise ValueError('Inconsistent dispersion direction across centers')
        residual=[]
        for r in validation:
            p=np.array(r['fit_pixels']+r['check_pixels']); w=np.array(r['fit_nm']+r['check_nm'])
            use=(p>=lo)&(p<=hi)
            if use.sum()<3: raise ValueError('Each independent center requires ≥3 validation peaks in shared pixel range')
            t=(r['context']['center_nm']-low)/(high-low)
            predicted=(1-t)*np.polyval(left['coefficients'],p[use])+t*np.polyval(right['coefficients'],p[use])
            residual.extend((predicted-w[use]).tolist())
        maximum=max(abs(e) for e in residual)
        if maximum>tolerance_nm: raise ValueError(f'Independent center error {maximum:.4f} nm exceeds tolerance in {low}–{high} nm; add closer anchors')
        segments.append({'center_range':[low,high],'pixel_range':[lo,hi],
                         'left_coefficients':left['coefficients'],'right_coefficients':right['coefficients'],
                         'max_check_error_nm':maximum,'check_residual_nm':residual})
    if not segments: raise ValueError('No center interval has sufficient independent validation')
    return {'kind':'broad_piecewise','id':str(uuid.uuid4()),'created_utc':datetime.now(timezone.utc).isoformat(),
            'context':copy.deepcopy(a[0]['context']), 'target_nm':[900.,1700.],
            'anchors':copy.deepcopy(anchors),'checks':copy.deepcopy(checks),'segments':segments,
            'tolerance_nm':float(tolerance_nm),
            'rms_nm':float(np.sqrt(np.mean([e*e for s in segments for e in s['check_residual_nm']]))),
            'source':'WinSpec-only empirical center interpolation; independently checked center positions'}

def broad_axis(record,context):
    if not valid_context(context) or _fixed_identity(record['context'])!=_fixed_identity(context): return None
    checked=fit_broad_calibration(record['anchors'],record['checks'],record['tolerance_nm'])
    center=context['center_nm']; p=np.arange(1,513)
    for s in checked['segments']:
        low,high=s['center_range']
        if not low<=center<=high: continue
        t=(center-low)/(high-low)
        axis=(1-t)*np.polyval(s['left_coefficients'],p)+t*np.polyval(s['right_coefficients'],p)
        lo,hi=s['pixel_range']; mask=(p>=lo)&(p<=hi)&(axis>=900)&(axis<=1700)
        if mask.sum()<2: return None
        return axis[mask],mask
    return None
