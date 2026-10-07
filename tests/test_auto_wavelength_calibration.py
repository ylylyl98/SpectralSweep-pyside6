import numpy as np
import pytest
from app.auto_wavelength_calibration import match_frame


def frame():
    pixels=np.array([35.,83.,147.,214.,283.,359.,421.,476.])
    wavelengths=1180-.5*pixels
    x=np.arange(1,513)
    y=np.ones(512)*300
    for i,p in enumerate(pixels): y+= (2000+i*150)*np.exp(-.5*((x-p)/1.1)**2)
    context=dict(profile='bench',grating='[750nm,300][1][0]',center_nm=1050.,
                 output_port='SideExit',detector='test',geometry=[512,1],spectrometer='sp')
    return {'counts':y.tolist(),'context':context}, [(w,'Ne I',100.) for w in wavelengths]


def test_blind_matching_negative_dispersion_with_independent_peaks():
    f,refs=frame(); r=match_frame(f,refs)
    assert r['degree']==1
    assert r['coefficients'][0]==pytest.approx(-.5,abs=.001)
    assert len(r['check_pixels'])>=2
    assert not set(r['fit_pixels'])&set(r['check_pixels'])
    assert max(abs(x) for x in r['check_residual_nm'])<.02


def test_noise_and_ambiguous_reference_fail_closed():
    f,refs=frame()
    with pytest.raises(ValueError,match='ambiguous'):
        match_frame(f,refs+[(w+10,s,i) for w,s,i in refs])
    f['counts']=[300.]*512
    with pytest.raises(ValueError,match='peaks'):
        match_frame(f,refs)


def test_failed_check_cannot_be_removed_to_enable_its_interval(monkeypatch):
    from app.auto_wavelength_calibration import build_broad
    from app.wavelength_calibration import fit_calibration
    def local(center, shift=0):
        ctx=frame()[0]['context'].copy(); ctx['center_nm']=center
        p=np.array([20,200,490]); cp=np.array([300])
        return fit_calibration(p,center+.1*(p-256)+shift,cp,center+.1*(cp-256)+shift,ctx,1,.1)
    records=[local(c,2 if c==950 else 0) for c in [900,950,1050,1100,1300]]
    monkeypatch.setattr('app.auto_wavelength_calibration.match_frame',lambda f,r,t:f)
    with pytest.raises(ValueError,match='Independent center error'):
        build_broad(records,[])


def test_exposure_retries_are_bounded_and_clipping_reduces_exposure():
    from app.auto_wavelength_calibration import exposure_retry
    f={'counts':[0.]*512}
    assert exposure_retry(f,1000,0)==4000
    assert exposure_retry(f,4000,1)==10000
    assert exposure_retry(f,10000,2) is None
    assert exposure_retry(f,1000,4) is None
    f={'counts':[65000.]*512,'winspec_datatype':3}
    assert exposure_retry(f,1000,0)==250


def test_positive_dispersion_is_supported():
    f,refs=frame(); f['counts']=f['counts'][::-1]
    result=match_frame(f,refs)
    assert result['coefficients'][0]==pytest.approx(.5,abs=.001)
