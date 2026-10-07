import pytest
from ui.detector_wavelength_advice import wavelength_advice, sequence_wavelength_warning


@pytest.mark.parametrize('identity,center,warn', [
    ({'backend':'lightfield'},1001,True),
    ({'backend':'lightfield'},1000,False),
    ({'backend':'winspec_ingaas'},999,True),
    ({'backend':'winspec_ingaas'},1000,False),
    ({'backend':'andor_sdk2','camera_role':'si'},1100,True),
    ({'backend':'andor_sdk2','camera_role':'ingaas'},900,True),
    ({},1100,False),
])
def test_detector_thresholds(identity,center,warn):
    assert bool(wavelength_advice(identity,[center])) == warn


def test_whole_scan_returns_one_advice():
    message=wavelength_advice({'backend':'winspec_ingaas'},[900,950,1000,1200])
    assert '900' in message and '950' in message


def test_sequence_preserves_detector_center_pairing_instead_of_cross_checking_lists():
    contexts = [
        {'Measurement setup': 'lightfield', 'Center Wavelength (nm)': 730},
        {'Measurement setup': 'winspec_ingaas', 'Center Wavelength (nm)': 1100},
    ]
    assert not sequence_wavelength_warning(contexts, {'backend': 'winspec_ingaas'}, 730)


def test_sequence_uses_fixed_center_and_active_detector_when_no_setup_row():
    assert '1100' in sequence_wavelength_warning([{}], {'backend': 'lightfield'}, 1100)
    assert '730' in sequence_wavelength_warning([{}], {'backend': 'winspec_ingaas'}, 730)
    assert not sequence_wavelength_warning([], {'backend': 'lightfield'}, 1100)


def test_repeated_gate_points_do_not_duplicate_detector_reminders():
    message = sequence_wavelength_warning([
        {'Measurement setup': 'lightfield', 'Center Wavelength (nm)': 1100}
    ] * 100, {}, 730)
    assert message.count('1100') == 1
