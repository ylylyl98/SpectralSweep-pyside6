"""A transfer ZIP cannot replace an existing runtime or escape its private directory."""
import hashlib
import importlib
import json
from pathlib import Path
import zipfile

import pytest


def migration_fixture(tmp_path, monkeypatch):
    from app.devices import winusb_binding as binding
    module = importlib.import_module('tools.winspec.migrate_winusb')
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'helper.exe').write_bytes(b'fixture helper')
    (source / 'support').mkdir()
    (source / 'support/library.dll').write_bytes(b'fixture library')
    pins = {str(p.relative_to(source)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in source.rglob('*') if p.is_file()}
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'binding_protocol': 1, 'runtime_directory': 'runtime-v6-portable',
                                   'runtime_files': pins, 'release': 'fixture'}))
    monkeypatch.setattr(binding, 'PORTABLE_MANIFEST', manifest)
    monkeypatch.setattr(module, 'inspect_device', lambda **kw: pytest.fail('copying runtime touched PnP/device'))
    return module, source, tmp_path / 'transfer.zip', tmp_path / 'state'


def test_bundle_round_trip_is_verified_and_preserves_older_installation(tmp_path, monkeypatch):
    module, source, archive, state = migration_fixture(tmp_path, monkeypatch)
    old = state / 'runtime-v5'
    old.mkdir(parents=True)
    (old / 'original').write_bytes(b'keep')
    module.create_bundle(source, archive)
    installed = module.install_bundle(archive, state)
    assert installed == state / 'runtime-v6-portable'
    assert (installed / 'helper.exe').read_bytes() == b'fixture helper'
    assert (installed / 'support/library.dll').read_bytes() == b'fixture library'
    assert (old / 'original').read_bytes() == b'keep'
    assert not (state / 'device-binding.json').exists()
    assert module.install_bundle(archive, state) == installed
    (installed / 'helper.exe').write_bytes(b'changed')
    with pytest.raises(RuntimeError):
        module.install_bundle(archive, state)
    assert (installed / 'helper.exe').read_bytes() == b'changed'


@pytest.mark.parametrize('name,content', [
    ('../escaped.txt', b'escape'), ('runtime/helper.exe', b'tampered duplicate'),
    ('runtime/unexpected.dll', b'extra'), ('manifest.json', b'changed manifest'),
])
def test_unknown_duplicate_or_traversing_zip_member_is_rejected_before_extraction(tmp_path, monkeypatch, name, content):
    module, source, archive, state = migration_fixture(tmp_path, monkeypatch)
    module.create_bundle(source, archive)
    with zipfile.ZipFile(archive, 'a') as stream:
        stream.writestr(name, content)
    with pytest.raises((ValueError, RuntimeError)):
        module.install_bundle(archive, state)
    assert not state.exists()
    assert not (tmp_path / 'escaped.txt').exists()


def test_corrupt_payload_cannot_publish_runtime_or_binding(tmp_path, monkeypatch):
    module, source, archive, state = migration_fixture(tmp_path, monkeypatch)
    module.create_bundle(source, archive)
    damaged = tmp_path / 'damaged.zip'
    with zipfile.ZipFile(archive) as original, zipfile.ZipFile(damaged, 'w') as output:
        for name in original.namelist():
            output.writestr(name, b'changed' if name == 'runtime/helper.exe' else original.read(name))
    with pytest.raises(RuntimeError, match='changed'):
        module.install_bundle(damaged, state)
    assert not (state / 'runtime-v6-portable').exists()
    assert not (state / 'device-binding.json').exists()


def test_enrollment_rejects_non_winusb_device_without_binding(tmp_path, monkeypatch):
    module, source, archive, state = migration_fixture(tmp_path, monkeypatch)
    module.create_bundle(source, archive)
    module.install_bundle(archive, state)
    def unavailable(**kwargs):
        raise RuntimeError('Driver is not WinUSB')
    monkeypatch.setattr(module, 'inspect_device', unavailable)
    with pytest.raises(RuntimeError, match='WinUSB'):
        module.enroll(state, r'USB\VID_0BD7&PID_A010\9&NEWPC&0&3')
    assert not (state / 'device-binding.json').exists()


def test_transfer_manifest_survives_git_checkout_line_endings(tmp_path, monkeypatch):
    from app.devices import winusb_binding as binding
    module, source, archive, state = migration_fixture(tmp_path, monkeypatch)
    module.create_bundle(source, archive)
    content = json.loads(binding.PORTABLE_MANIFEST.read_text())
    binding.PORTABLE_MANIFEST.write_bytes((json.dumps(content, indent=4) + '\n').replace('\n', '\r\n').encode())
    assert module.install_bundle(archive, state).is_dir()


@pytest.mark.parametrize('close_error', [False, True])
def test_detector_test_exports_and_distinguishes_cleanup_from_success(tmp_path, monkeypatch, close_error):
    from tests.test_winusb_persistent import bridge
    from app.devices import winusb_persistent as persistent
    module = importlib.import_module('tools.winspec.migrate_winusb')
    owner, processes = bridge(tmp_path, monkeypatch)
    if close_error:
        script = tmp_path / 'helper.py'
        script.write_text(script.read_text().replace('completed_frames=completed,error=0',
            'completed_frames=completed,error=29') + '\nsys.exit(1)\n')
    monkeypatch.setattr(persistent, 'PersistentCaptureBridge', lambda **kw: owner)
    monkeypatch.setattr(module, 'check', lambda state: {'owner_record_present': False})
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path / 'history.sqlite'))
    output = tmp_path / 'exports'
    if close_error:
        with pytest.raises(RuntimeError, match='closed with error'):
            module.capture(owner.state_dir, output, frames=2)
        assert not (output / 'result.json').exists()
    else:
        result = module.capture(owner.state_dir, output, frames=2)
        assert len(result['frames']) == 2 and result['normal_close_verified']
        assert (output / 'result.json').is_file()
    assert len(list(output.glob('*.csv'))) == 2
    assert all(p.poll() is not None for p in processes)
    assert not owner._claim.exists()


def test_active_owner_blocks_enrollment_even_when_runtime_and_pnp_are_valid(tmp_path, monkeypatch):
    module, source, archive, state = migration_fixture(tmp_path, monkeypatch)
    module.create_bundle(source, archive)
    module.install_bundle(archive, state)
    monkeypatch.setattr(module, 'inspect_device', lambda **kw: {'service': 'WINUSB'})
    (state / 'active.json').write_text('{}')
    with pytest.raises(RuntimeError, match='owner'):
        module.enroll(state, r'USB\VID_0BD7&PID_A010\9&NEWPC&0&3')
    assert not (state / 'device-binding.json').exists()
