import importlib
import importlib.util
import json
import shutil
import subprocess
import threading
import sys
import uuid
from pathlib import Path

import pytest

from tests.winusb_synthetic import write_capture, EEPROM_PINS


@pytest.fixture(autouse=True)
def synthetic_identity(monkeypatch):
    target = runtime()
    release = target.manifest()
    release['eeprom_files'] = EEPROM_PINS
    monkeypatch.setattr(target, 'manifest', lambda: release)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows packaged-process environment')
def test_runtime_location_is_shared_with_unpacked_app_not_packaged_environment(tmp_path, monkeypatch):
    module = runtime()
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'packaged-private-cache'))
    assert not module.default_runtime_dir().is_relative_to(tmp_path)


def runtime():
    assert importlib.util.find_spec('app.devices.winusb_runtime') is not None, 'Local process bridge missing'
    return importlib.import_module('app.devices.winusb_runtime')


def capture_copy(tmp_path):
    target = tmp_path/'capture'
    write_capture(target)
    return target


def test_observed_frame_requires_both_transport_equality_and_safe_lifecycle(tmp_path):
    module = runtime()
    capture = capture_copy(tmp_path)
    metadata, payload = module.read_capture(capture)
    assert len(payload) == 1024
    assert metadata['zero_count'] == 114
    assert metadata['temperature_verified'] is False
    assert metadata['cleanup_verified'] is True


@pytest.mark.parametrize('damage', ['short', 'different', 'missing_exit', 'stop_failed', 'not_joined', 'wrong_eeprom'])
def test_partial_or_unverified_native_frame_is_rejected(tmp_path, damage):
    module = runtime()
    capture = capture_copy(tmp_path)
    if damage == 'short': (capture/'frame-1024.bin').write_bytes(b'\0'*1023)
    if damage == 'different': (capture/'data82-transport.bin').write_bytes(b'\0'*1024)
    if damage == 'wrong_eeprom': (capture/'eeprom.bin').write_bytes(b'wrong detector')
    if damage in {'missing_exit', 'stop_failed'}:
        rows = [json.loads(line) for line in (capture/'session.jsonl').read_text().splitlines()]
        if damage == 'missing_exit': rows = [row for row in rows if row['stage'] != 'safe_process_exit']
        else:
            for row in rows:
                if row['stage'] == 'stop_reply_valid': row['value'] = 0
        (capture/'session.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
    if damage == 'not_joined':
        path = capture/'raw-comparison.json'; value = json.loads(path.read_text())
        value['request_worker_joined'] = False; path.write_text(json.dumps(value))
    with pytest.raises(RuntimeError): module.read_capture(capture)


def test_changed_runtime_is_rejected_before_process_start(tmp_path):
    module = runtime()
    root = tmp_path/'runtime'; root.mkdir()
    (root/'raw_data_diagnostic.exe').write_bytes(b'untrusted')
    with pytest.raises(RuntimeError, match='runtime'):
        module.verify_runtime(root)


@pytest.mark.parametrize('timeout', [False, True])
def test_bridge_retains_unknown_owner_and_never_retries_or_kills(tmp_path, monkeypatch, timeout):
    module = runtime()
    root = tmp_path/'runtime'; root.mkdir()
    monkeypatch.setattr(module, 'verify_runtime', lambda path: Path(path))
    monkeypatch.setattr(module, 'inspect_device', lambda: {'present': True, 'service': 'WinUSB'})
    processes = []
    class Process:
        pid = 98765
        def __init__(self, command, **kwargs):
            processes.append(self); self.command = command; self.kwargs = kwargs
            if not timeout: write_capture(Path(command[-1]))
        def wait(self, timeout=None):
            if timeout and globals_timeout: raise subprocess.TimeoutExpired(self.command, timeout)
            return 0
        def poll(self): return None if globals_timeout else 0
        def kill(self): pytest.fail('A live native owner must never be killed')
        def terminate(self): pytest.fail('A live native owner must never be terminated')
    globals_timeout = timeout
    monkeypatch.setattr(module.subprocess, 'Popen', Process)
    bridge = module.LocalCaptureBridge(runtime_dir=root, state_dir=tmp_path/'state')
    if timeout:
        with pytest.raises(RuntimeError, match='ownership'):
            bridge.capture(threading.Event())
        assert bridge.is_busy
        assert (tmp_path/'state/active.json').exists()
        with pytest.raises(RuntimeError): bridge.capture(threading.Event())
    else:
        metadata, payload = bridge.capture(threading.Event())
        assert metadata['cleanup_verified'] is True
        assert len(payload) == 1024
        assert not (tmp_path/'state/active.json').exists()
    assert len(processes) == 1
    assert processes[0].kwargs['stdin'] == subprocess.DEVNULL


def test_existing_owner_receipt_blocks_new_bridge(tmp_path, monkeypatch):
    module = runtime()
    state = tmp_path/'state'; state.mkdir()
    (state/'active.json').write_text('{"pid":98765}')
    bridge = module.LocalCaptureBridge(runtime_dir=tmp_path/'runtime', state_dir=state)
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: pytest.fail('duplicate process'))
    with pytest.raises(RuntimeError, match='ownership'):
        bridge.capture(threading.Event())


def test_pnp_query_checks_current_device_without_launching_powershell(monkeypatch):
    module = runtime()
    assert hasattr(module, 'query_device'), 'Direct PnP query missing'
    connected = True
    def query(instance):
        if not connected: raise OSError('device removed')
        return {'instance': instance, 'present': True, 'status': 'OK', 'service': 'WINUSB'}
    monkeypatch.setattr(module, 'query_device', query)
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: pytest.fail('PowerShell started'))
    result = module.inspect_device()
    assert result['service'] == 'WINUSB'
    assert result['present'] is True
    connected = False
    with pytest.raises(RuntimeError, match='ST133'): module.inspect_device()


def test_missing_device_gives_actionable_error_without_native_start(monkeypatch):
    module = runtime()
    assert hasattr(module, 'query_device'), 'Direct PnP query missing'
    def absent(*args, **kwargs):
        raise OSError('device absent')
    monkeypatch.setattr(module, 'query_device', absent)
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    with pytest.raises(RuntimeError, match='ST133'):
        module.inspect_device()


@pytest.mark.parametrize('change', [{'present': False}, {'service': 'PIUSB'}, {'instance': 'USB\\OTHER'}])
def test_unexpected_device_or_driver_is_rejected(monkeypatch, change):
    module = runtime()
    device = {'instance': module.manifest()['device_instance'], 'present': True, 'status': 'OK', 'service': 'WinUSB'}
    device.update(change)
    monkeypatch.setattr(module, 'query_device', lambda instance: device)
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    with pytest.raises(RuntimeError, match='ST133'): module.inspect_device()


@pytest.mark.parametrize('bad_field', ['value', 'error'])
def test_failed_exit_barrier_retains_claim_even_when_process_has_exited(tmp_path, monkeypatch, bad_field):
    module = runtime()
    monkeypatch.setattr(module, 'verify_runtime', lambda path: Path(path))
    monkeypatch.setattr(module, 'inspect_device', lambda: {'present': True, 'service': 'WINUSB'})
    class Process:
        pid = 98765
        def __init__(self, command, **kwargs):
            output = Path(command[-1]); write_capture(output)
            rows = [json.loads(line) for line in (output/'session.jsonl').read_text().splitlines()]
            for row in rows:
                if row['stage'] == 'safe_process_exit': row[bad_field] = 1
            (output/'session.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
        def wait(self, timeout=None): return 0
        def poll(self): return 0
    monkeypatch.setattr(module.subprocess, 'Popen', Process)
    bridge = module.LocalCaptureBridge(runtime_dir=tmp_path/'runtime', state_dir=tmp_path/'state')
    with pytest.raises(RuntimeError, match='safe_process_exit'): bridge.capture(threading.Event())
    assert (tmp_path/'state/active.json').exists()
    with pytest.raises(RuntimeError, match='ownership'): bridge.capture(threading.Event())


def test_separate_requests_bind_output_process_and_saved_frame_without_claiming_hardware_freshness(tmp_path, monkeypatch):
    module = runtime()
    root = tmp_path/'runtime'; root.mkdir()
    monkeypatch.setattr(module, 'verify_runtime', lambda path: Path(path))
    monkeypatch.setattr(module, 'inspect_device', lambda: {'present': True, 'service': 'WINUSB'})
    assert 'request_id' in __import__('inspect').signature(module.LocalCaptureBridge.capture).parameters
    class Process:
        pid = 98765
        def __init__(self, command, **kwargs):
            self.directory = Path(command[-1])
            # The request must exist before the native process is launched.
            request = json.loads((self.directory.parent/'request.json').read_text())
            assert request['context']['center_nm'] == 1300
            write_capture(self.directory)
        def wait(self, timeout=None): return 0
        def poll(self): return 0
    monkeypatch.setattr(module.subprocess, 'Popen', Process)
    bridge = module.LocalCaptureBridge(runtime_dir=root, state_dir=tmp_path/'state')
    frames = []
    for _ in range(2):
        request_id = str(uuid.uuid4())
        metadata, payload = bridge.capture(threading.Event(), request_id=request_id, context={'center_nm': 1300})
        assert metadata['request_id'] == request_id
        assert metadata['hardware_frame_freshness_verified'] is False
        parent = Path(metadata['evidence_directory']).parent
        assert json.loads((parent/'request.json').read_text())['request_id'] == request_id
        process = json.loads((parent/'process.json').read_text())
        assert process['request_id'] == request_id
        assert metadata['process_pid'] == process['pid']
        assert json.loads((parent/'validated-frame.json').read_text()) == metadata
        assert metadata['bridge_timing_s']['total'] >= metadata['bridge_timing_s']['helper_process'] >= 0
        frames.append(metadata)
    assert frames[0]['request_sequence'] == 1 and frames[1]['request_sequence'] == 2
    assert frames[0]['bridge_session_id'] == frames[1]['bridge_session_id']
    assert frames[0]['evidence_directory'] != frames[1]['evidence_directory']
    # Identical bytes alone must not reject a valid dark/flat frame.
    assert frames[0]['payload_sha256'] == frames[1]['payload_sha256']
    with pytest.raises(RuntimeError, match='already used'):
        bridge.capture(threading.Event(), request_id=request_id)
