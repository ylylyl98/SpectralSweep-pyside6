import json
from pathlib import Path
import pytest
from app.wavelength_calibration import validate_imported_calibration

def test_import_recomputes_broad_coefficients_and_errors():
    path=Path('calibrations/ingaas-2026-09-27/joint-analysis/ingaas-300-validated-1300-1500.json')
    record=json.loads(path.read_text())
    record['segments'][0]['left_coefficients']=[0,0,1]
    record['rms_nm']=0
    verified=validate_imported_calibration(record)
    assert .07 < verified['rms_nm'] < .08
    assert verified['segments'][0]['left_coefficients'] != [0,0,1]

def test_import_rejects_failed_independent_check():
    path=Path('calibrations/ingaas-2026-09-27/joint-analysis/ingaas-300-validated-1300-1500.json')
    record=json.loads(path.read_text())
    record['checks'][0]['check_nm'][0]+=5
    with pytest.raises(ValueError):validate_imported_calibration(record)
