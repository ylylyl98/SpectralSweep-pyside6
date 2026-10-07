import importlib.util
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest


def load_audit():
    spec = importlib.util.spec_from_file_location('startup_readonly_audit',
                                                Path('tools/winspec/startup_readonly_audit.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bridge(busy=False, changes=False):
    exposure_reads = [0]
    calls = []
    def get_param(exp, name):
        calls.append(name)
        if name == 'EXP_EXPOSURE':
            exposure_reads[0] += 1
            return .9 if changes and exposure_reads[0] > 1 else .8
        if name == 'EXP_DRIVERVERSION':
            raise RuntimeError('Unavailable on this controller')
        return {'EXP_DELAY_TIME': 0., 'EXP_NUMBER_OF_CLEANS': 2,
                'EXP_CONTROLLER_NAME': 'ST-133', 'EXP_RUNNING': busy,
                'EXP_RUNNING_EXPERIMENT': False, 'EXP_ACTUAL_TEMP': -100.,
                'EXP_TEMP_STATUS': True}.get(name, 1)
    br = SimpleNamespace(create_experiment=lambda: object(), get_param=get_param,
                         const=lambda name:name,
                         SERVER_BUILD='test')
    return br, calls


def test_audit_reads_delay_and_identity_preserving_unsupported_evidence():
    module = load_audit()
    br, calls = bridge()
    report = module.run_audit(br)
    assert report['status'] == 'complete'
    assert report['settings_unchanged'] is True
    assert report['parameters']['EXP_DELAY_TIME']['value'] == 0.
    assert report['parameters']['EXP_CONTROLLER_NAME']['value'] == 'ST-133'
    assert 'Unavailable' in report['parameters']['EXP_DRIVERVERSION']['error']
    assert calls  # The double exposes only reads: mutations would raise AttributeError.


def test_busy_camera_rejected_before_extended_reads():
    module = load_audit()
    br, calls = bridge(busy=True)
    with pytest.raises(RuntimeError, match='busy'):
        module.run_audit(br)
    assert 'EXP_DELAY_TIME' not in calls


def test_settings_change_during_read_is_reported_as_failure():
    module = load_audit()
    br, _ = bridge(changes=True)
    report = module.run_audit(br)
    assert report['status'] == 'failed'
    assert report['settings_unchanged'] is False


def test_missing_enum_is_not_guessed_or_read_as_zero():
    module = load_audit()
    br, calls = bridge()
    br.const = lambda name: None if name == 'EXP_DELAY_TIME' else name
    report = module.run_audit(br)
    assert report['parameters']['EXP_DELAY_TIME']['status'] == 'unsupported'
    assert 'EXP_DELAY_TIME' not in calls


def test_unknown_idle_state_rejected_before_extended_reads():
    module = load_audit()
    br, calls = bridge()
    original = br.get_param
    def get_param(exp, name):
        if name in ('EXP_RUNNING', 'EXP_RUNNING_EXPERIMENT'):
            raise RuntimeError('No running status')
        return original(exp, name)
    br.get_param = get_param
    with pytest.raises(RuntimeError, match='Cannot verify'):
        module.run_audit(br)
    assert 'EXP_DELAY_TIME' not in calls


def test_nonfinite_parameter_does_not_become_valid_measurement():
    module = load_audit()
    br, _ = bridge()
    original = br.get_param
    br.get_param = lambda exp, name: float('nan') if name == 'EXP_DELAY_TIME' else original(exp, name)
    report = module.run_audit(br)
    assert report['parameters']['EXP_DELAY_TIME']['status'] == 'unsupported'
    assert 'value' not in report['parameters']['EXP_DELAY_TIME']


def test_reservation_rejects_running_bridge_before_com_import():
    module = load_audit()
    with socket.socket() as server:
        try:
            server.bind(('0.0.0.0', 5000))
        except OSError:
            pytest.skip('Port 5000 already occupied on host')
        server.listen()
        with pytest.raises(RuntimeError, match='port 5000 is occupied'):
            module.main()
