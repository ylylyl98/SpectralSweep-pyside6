import numpy as np
import pytest
from app import wavelength_calibration as wc


def fixture():
    ctx=dict(profile='bench',grating='300',center_nm=1200.,output_port='SideExit',
             detector='ingaas',geometry=[512,1],spectrometer='sp')
    def rows(centers):
        result=[]
        for c in centers:
            for p in [10,80,150,230,310,390,460,505]:
                theta=np.arcsin(c/(2*(1e6/300)*np.cos(.25)))+.002
                u=-(p-200)
                phi=np.arctan2(u*np.cos(.03),6000+u*np.sin(.03))
                w=(1e6/300)*(np.sin(theta-.25)+np.sin(theta+.25+phi))
                result.append(dict(center_nm=c,pixel=p,wavelength_nm=w))
        return result
    return ctx,rows([1000,1200,1400,1600]),rows([1100,1500])


def test_physical_model_extends_to_all_pixels_and_operating_centers():
    assert hasattr(wc,'fit_physical_calibration'), 'Physical grating model is missing'
    ctx,train,checks=fixture()
    r=wc.fit_physical_calibration(train,checks,ctx)
    for c in [900.,1234.5,1700.]:
        axis,mask=wc.calibrated_axis(r,{**ctx,'center_nm':c})
        assert len(axis)==512 and mask.all() and np.all(np.diff(axis)<0)
    axis,_=wc.calibrated_axis(r,{**ctx,'center_nm':1100.})
    assert axis[9]==pytest.approx(checks[0]['wavelength_nm'],abs=1e-5)
    assert r['rms_nm']<1e-5
    r['parameters']=[1]*5
    r['provenance']={'assignment':'mixed order','reference_sha256':'test-hash'}
    assert wc.validate_imported_calibration(r)['provenance']==r['provenance']
    assert wc.validate_imported_calibration(r)['parameters'] != r['parameters']
    assert wc.calibrated_axis(r,{**ctx,'output_port':'FrontExit'}) is None


def test_physical_model_rejects_failed_checks():
    assert hasattr(wc,'fit_physical_calibration')
    ctx,train,checks=fixture()
    checks[0]['wavelength_nm']+=5
    with pytest.raises(ValueError,match='RMS'):
        wc.fit_physical_calibration(train,checks,ctx)
