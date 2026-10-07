import numpy as np
import pytest
from app.wavelength_calibration import fit_calibration, fit_broad_calibration, calibrated_axis

def local(center,shift=0):
    ctx={'profile':'bench','grating':'300','center_nm':center,'output_port':'SideExit',
         'detector':'camera','geometry':[512,1],'spectrometer':'sp'}
    p=np.array([20,200,490]); cp=np.array([300])
    return fit_calibration(p,center+.1*(p-256)+shift,cp,center+.1*(cp-256)+shift,ctx,1,.1)

def test_broad_interpolates_only_validated_segments():
    anchors=[local(c) for c in [900,1000,1100]]
    result=fit_broad_calibration(anchors,[local(950)],.2)
    axis,mask=calibrated_axis(result,local(975)['context'])
    assert axis[0]==pytest.approx(951.4)
    assert mask.sum()==471
    assert calibrated_axis(result,local(1050)['context']) is None
    assert calibrated_axis(result,local(850)['context']) is None
    assert calibrated_axis(result,{**local(975)['context'],'grating':'600'}) is None

def test_broad_rejects_failed_independent_center_and_mixed_devices():
    anchors=[local(c) for c in [900,1000,1100]]
    with pytest.raises(ValueError): fit_broad_calibration(anchors,[local(950,2)],.2)
    bad=local(950); bad['context']['output_port']='FrontExit'
    with pytest.raises(ValueError): fit_broad_calibration(anchors,[bad],.2)

def test_training_center_cannot_validate_itself():
    anchors=[local(c) for c in [900,1000,1100]]
    with pytest.raises(ValueError): fit_broad_calibration(anchors,[local(1000)],.2)
