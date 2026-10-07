"""Empirical full-detector model with separate center and edge validation.

Evidence contains assigned lamp peaks, not raw spectra. Assignment must be
established separately; a small residual alone does not prove line identity.
"""
import copy
import uuid
import re
from datetime import datetime, timezone
import numpy as np


def _design(center, pixel):
    z = (np.asarray(center)-1300.)/400.
    x = (np.asarray(pixel)-256.5)/255.5
    return np.column_stack([x**i*z**j for i, j in
                           [(0,0),(1,0),(0,1),(2,0),(1,1),(0,2),(3,0),(2,1),(1,2),(0,3)]])


def _rows(rows):
    a = np.asarray([[r['center_nm'], r['pixel'], r['wavelength_nm']] for r in rows], dtype=float)
    if a.ndim != 2 or a.shape[1:] != (3,) or not np.all(np.isfinite(a)):
        raise ValueError('Finite assigned peak evidence is required')
    if np.any((a[:,1]<1)|(a[:,1]>512)|(a[:,2]<=0)):
        raise ValueError('Invalid detector pixel or wavelength')
    if len(set(map(tuple, a[:,:2]))) != len(a):
        raise ValueError('Duplicate peak evidence')
    return a


def coverage_gaps(rows):
    """Each 100 nm center band needs both detector edges and interior evidence."""
    a = _rows(rows)
    gaps = []
    for low in range(900,1700,100):
        use = (a[:,0]>=low)&(a[:,0] <= low+100 if low==1600 else a[:,0]<low+100)
        p = a[use,1]
        for label, present in [('pixels 1-32', np.any(p<=32)),
                               ('pixels 129-384', np.any((p>=129)&(p<=384))),
                               ('pixels 481-512', np.any(p>=481))]:
            if not present: gaps.append(f'{low}-{low+100} nm center: {label}')
    return gaps


def _monotonic_rectangle(c):
    # Exact extrema of d(wavelength)/dx, a quadratic on [-1,1]^2.
    points = [(x,z) for x in (-1.,1.) for z in (-1.,1.)]
    for edge in (-1.,1.):
        if c[6]: points.append((-(2*c[3]+2*c[7]*edge)/(6*c[6]),edge))
        if c[8]: points.append((edge,-(c[4]+2*c[7]*edge)/(2*c[8])))
    h = np.array([[6*c[6],2*c[7]],[2*c[7],2*c[8]]])
    if abs(np.linalg.det(h))>1e-20:
        points.append(tuple(np.linalg.solve(h,[-2*c[3],-c[4]])))
    values = [c[1]+2*c[3]*x+c[4]*z+3*c[6]*x*x+2*c[7]*x*z+c[8]*z*z
              for x,z in points if -1<=x<=1 and -1<=z<=1]
    return min(values)>0 or max(values)<0


def fit_full_detector_calibration(training, checks, context, tolerance_nm=.2):
    from app.wavelength_calibration import valid_context
    if not valid_context(context) or context['geometry'] != [512,1]:
        raise ValueError('A complete 512 x 1 detector identity is required')
    grating = str(context['grating'])
    match = re.search(r',\s*(\d+)\s*\]', grating)
    grooves = float(match.group(1)) if match else float(grating) if grating.isdigit() else None
    if not grooves or 2e6/grooves<=1700:
        raise ValueError('This grating cannot establish a first-order 900-1700 nm calibration')
    if not np.isfinite(tolerance_nm) or tolerance_nm<=0:
        raise ValueError('Tolerance must be positive')
    a, b = _rows(training), _rows(checks)
    if set(a[:,0]) & set(b[:,0]):
        raise ValueError('Check centers must be independent of training centers')
    if a[:,0].min()>900 or a[:,0].max()<1700:
        raise ValueError('Training center coverage must bracket 900-1700 nm')
    for name, rows in [('training', training), ('check', checks)]:
        gaps = coverage_gaps(rows)
        if gaps: raise ValueError(name+' coverage missing: '+'; '.join(gaps))
    matrix = _design(a[:,0],a[:,1])
    coef, _, rank, _ = np.linalg.lstsq(matrix, a[:,2]-a[:,0], rcond=None)
    if rank != 10 or np.linalg.cond(matrix)>1e5:
        raise ValueError('Insufficient independent geometry to fit full detector model')
    residual = matrix@coef+a[:,0]-a[:,2]
    errors = _design(b[:,0],b[:,1])@coef+b[:,0]-b[:,2]
    if max(np.max(abs(residual)),np.max(abs(errors)))>tolerance_nm:
        raise ValueError('Full detector fit/check error exceeds tolerance')
    if not _monotonic_rectangle(coef):
        raise ValueError('Full detector mapping must be monotonic at every center')
    # Check the entire operating rectangle, including pixels outside 900-1700 nm.
    centers = np.repeat(np.linspace(900,1700,321),512)
    pixels = np.tile(np.arange(1,513),321)
    grid = (centers+_design(centers,pixels)@coef).reshape(321,512)
    delta = np.diff(grid,axis=1)
    if np.any(grid<=0) or not (np.all(delta>0) or np.all(delta<0)):
        raise ValueError('Full detector wavelength axis is not positive and monotonic')
    if np.any(grid>=2e6/grooves):
        raise ValueError('Full detector wavelengths exceed the grating first-order limit')
    return dict(kind='full_detector_surface', id=str(uuid.uuid4()),
                created_utc=datetime.now(timezone.utc).isoformat(), context=copy.deepcopy(context),
                center_range=[900.,1700.], pixel_range=[1,512],
                model='total-degree cubic in normalized center and pixel; wavelength=center+surface',
                coefficients=coef.tolist(), training=copy.deepcopy(training), checks=copy.deepcopy(checks),
                tolerance_nm=float(tolerance_nm), rms_nm=float(np.sqrt(np.mean(errors**2))),
                max_check_error_nm=float(np.max(abs(errors))), check_residual_nm=errors.tolist(),
                edge_validation_pixels=32, source='Assigned lamp peaks; independent centers and detector edges')


def full_detector_axis(record, context):
    from app.wavelength_calibration import valid_context, _fixed_identity
    if not valid_context(context) or _fixed_identity(record['context']) != _fixed_identity(context):
        return None
    c = float(context['center_nm'])
    if not 900<=c<=1700: return None
    verified = fit_full_detector_calibration(record['training'], record['checks'],
                                             record['context'], record['tolerance_nm'])
    p = np.arange(1,513)
    axis = c+_design(np.full(512,c),p)@verified['coefficients']
    if not np.all(np.isfinite(axis)) or np.any(axis<=0) or not (np.all(np.diff(axis)>0) or np.all(np.diff(axis)<0)):
        raise ValueError('Requested wavelength axis must be positive and monotonic')
    return axis, np.ones(512,dtype=bool)
