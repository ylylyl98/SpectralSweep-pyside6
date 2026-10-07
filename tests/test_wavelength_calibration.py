import numpy as np
import pytest
from app.wavelength_calibration import fit_calibration, calibrated_axis

CONTEXT = {'profile': 'bench1', 'grating': '300', 'center_nm': 1320., 'output_port': 'SideExit', 'detector': 'camera1', 'geometry': [512, 1], 'spectrometer': 'sp1'}

def test_linear_fit_with_independent_check_and_no_extrapolation():
    r = fit_calibration([20, 200, 490], [1202, 1220, 1249], [300], [1230], CONTEXT, 1, .1)
    x, mask = calibrated_axis(r, CONTEXT)
    assert x[0] == pytest.approx(1202)
    assert x[-1] == pytest.approx(1249)
    assert mask.sum() == 471
    assert calibrated_axis(r, {**CONTEXT, 'grating': '600'}) is None
    assert calibrated_axis(r, {**CONTEXT, 'center_nm':1321}) is None

def test_reversed_dispersion_preserves_pixel_order():
    r = fit_calibration([20, 200, 490], [1398, 1380, 1351], [300], [1370], CONTEXT, 1, .1)
    assert np.all(np.diff(calibrated_axis(r, CONTEXT)[0]) < 0)

def test_rejects_bad_validation_or_missing_independent_line():
    with pytest.raises(ValueError):
        fit_calibration([20, 200, 490], [1202, 1220, 1249], [300], [1300], CONTEXT, 1, .1)
    with pytest.raises(ValueError):
        fit_calibration([20, 200, 490], [1202, 1220, 1249], [], [], CONTEXT, 1, .1)
