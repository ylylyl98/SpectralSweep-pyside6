import copy
import numpy as np
import pytest
from app import wavelength_calibration as wc


def evidence():
    context = dict(profile='bench', grating='300', center_nm=900.,
                   output_port='SideExit', detector='ingaas', geometry=[512, 1], spectrometer='sp')
    def rows(centers):
        return [dict(center_nm=float(c), pixel=float(p), wavelength_nm=float(
            c - 20 - .5*(p-256.5) + .00001*(c-1300)*(p-256.5)))
            for c in centers for p in [5, 24, 128, 256, 384, 490, 508]]
    return context, rows(range(900, 1701, 50)), rows(range(925, 1700, 50))


def make_model():
    context, train, checks = evidence()
    assert hasattr(wc, 'fit_full_detector_calibration'), 'Full detector fitting is missing'
    return wc.fit_full_detector_calibration(train, checks, context)


def test_arbitrary_center_preserves_all_pixels_including_outside_target_nm():
    record = make_model()
    for c in [900, 937.25, 1321.7, 1700]:
        axis, mask = wc.calibrated_axis(record, {**record['context'], 'center_nm': c})
        assert len(axis) == 512 and mask.all()
        p = np.arange(1, 513)
        np.testing.assert_allclose(axis, c-20-.5*(p-256.5)+.00001*(c-1300)*(p-256.5), atol=1e-8)
    assert wc.calibrated_axis(record, {**record['context'], 'grating': '600'}) is None
    assert wc.calibrated_axis(record, {**record['context'], 'center_nm': 1701}) is None


def test_import_recomputes_model_and_rejects_changed_validation():
    record = make_model()
    record['coefficients'] = [0] * 10
    verified = wc.validate_imported_calibration(record)
    assert verified['coefficients'] != record['coefficients']
    bad = copy.deepcopy(record)
    bad['checks'][0]['wavelength_nm'] += 2
    with pytest.raises(ValueError, match='error'):
        wc.validate_imported_calibration(bad)


def test_missing_longwave_or_edge_checks_cannot_be_full_calibration():
    make_model()
    ctx, train, checks = evidence()
    for incomplete in [[r for r in checks if r['center_nm'] < 1600],
                       [r for r in checks if 32 < r['pixel'] < 481]]:
        with pytest.raises(ValueError, match='coverage'):
            wc.fit_full_detector_calibration(train, incomplete, ctx)
    with pytest.raises(ValueError, match='independent'):
        wc.fit_full_detector_calibration(train, train, ctx)


def test_rejects_dispersion_reversal_between_grid_centers():
    make_model()
    ctx, train, checks = evidence()
    for r in train+checks:
        c, x = r['center_nm'], r['pixel']-256.5
        r['wavelength_nm'] = c+x*(1e-6*(c-901.25)**2-1e-7)+1e-9*x*x
    with pytest.raises(ValueError, match='monotonic'):
        wc.fit_full_detector_calibration(train, checks, ctx)


def test_1200_grating_cannot_claim_first_order_1700_nm():
    ctx, train, checks = evidence()
    ctx['grating'] = '[500nm,1200][0][0]'
    with pytest.raises(ValueError, match='grating'):
        wc.fit_full_detector_calibration(train, checks, ctx)
