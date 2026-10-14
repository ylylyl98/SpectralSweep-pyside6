"""Portable installations must bind one exact A010, never a VID/PID wildcard."""
import hashlib
import importlib
import json
from pathlib import Path
import threading

import pytest

INSTANCE = r'USB\VID_0BD7&PID_A010\9&NEWPC&0&3'


def binding_module():
    return importlib.import_module('app.devices.winusb_binding')


def release_fixture(tmp_path, monkeypatch):
    module = binding_module()
    manifest = tmp_path / 'portable.json'
    manifest.write_text(json.dumps({
        'binding_protocol': 1, 'release': 'experimental-persistent-portable-v6',
        'runtime_directory': 'runtime-v6-portable',
        'runtime_files': {'helper.exe': hashlib.sha256(b'helper').hexdigest()},
        'eeprom_files': {'eeprom.bin': 'a' * 64},
    }))
    monkeypatch.setattr(module, 'PORTABLE_MANIFEST', manifest)
    state = tmp_path / 'state'
    state.mkdir()
    return module, state, manifest


@pytest.mark.parametrize('instance', [
    r'USB\VID_0BD7&PID_A026\9&NEWPC&0&3',
    r'USB\VID_0BD7&PID_A010\*', 'USB\\VID_0BD7&PID_A010\\',
    r'USB\VID_0BD7&PID_A010\a\b', r'USB\VID_0BD7&PID_A010\abc def',
    'USB\\VID_0BD7&PID_A010\\' + 'a' * 240,
])
def test_invalid_target_never_becomes_a_binding(tmp_path, instance):
    module = binding_module()
    with pytest.raises(ValueError):
        module.normalize_instance(instance)
    assert not list(tmp_path.iterdir())


def test_unbound_existing_installation_preserves_current_release(tmp_path):
    module = binding_module()
    original = {'device_instance': 'original', 'runtime_directory': 'runtime-v5'}
    assert module.select_release(tmp_path, original) == original


def test_binding_selects_portable_release_and_exact_new_pc_instance(tmp_path, monkeypatch):
    module, state, manifest = release_fixture(tmp_path, monkeypatch)
    module.save_binding(state, INSTANCE.lower())
    selected = module.select_release(state, {'runtime_directory': 'runtime-v5'})
    assert selected['device_instance'] == INSTANCE
    assert selected['runtime_directory'] == 'runtime-v6-portable'
    saved = json.loads((state / 'device-binding.json').read_text())
    assert saved['manifest_sha256'] == module.manifest_fingerprint()
    assert module.save_binding(state, INSTANCE) == state / 'device-binding.json'
    with pytest.raises(RuntimeError, match='different'):
        module.save_binding(state, r'USB\VID_0BD7&PID_A010\OTHER')


def test_changed_manifest_or_active_owner_blocks_binding(tmp_path, monkeypatch):
    module, state, manifest = release_fixture(tmp_path, monkeypatch)
    (state / 'active.json').write_text('{}')
    with pytest.raises(RuntimeError, match='owner'):
        module.save_binding(state, INSTANCE)
    assert not (state / 'device-binding.json').exists()
    (state / 'active.json').unlink()
    module.save_binding(state, INSTANCE)
    changed = json.loads(manifest.read_text())
    changed['release'] = 'changed-release'
    manifest.write_text(json.dumps(changed))
    with pytest.raises(RuntimeError, match='manifest'):
        module.select_release(state, {})


def test_binding_survives_git_line_ending_and_json_formatting_changes(tmp_path, monkeypatch):
    module, state, manifest = release_fixture(tmp_path, monkeypatch)
    module.save_binding(state, INSTANCE)
    manifest.write_bytes((json.dumps(json.loads(manifest.read_text()), indent=4) + '\n').replace('\n', '\r\n').encode())
    assert module.select_release(state, {})['device_instance'] == INSTANCE


def test_invalid_binding_never_falls_back_to_original_camera(tmp_path, monkeypatch):
    module, state, _ = release_fixture(tmp_path, monkeypatch)
    (state / 'device-binding.json').write_text('{broken')
    with pytest.raises(RuntimeError, match='binding'):
        module.select_release(state, {'device_instance': 'original'})


@pytest.mark.parametrize('wrong_readback', [False, True])
def test_portable_bridge_passes_exact_binding_and_checks_native_readback(tmp_path, monkeypatch, wrong_readback):
    from tests.test_winusb_persistent import bridge
    from app.devices import winusb_persistent as persistent
    module = binding_module()
    original, processes = bridge(tmp_path, monkeypatch)
    portable = persistent.manifest()
    portable.update(binding_protocol=1, runtime_directory='runtime-v6-portable')
    manifest = tmp_path / 'portable.json'
    manifest.write_text(json.dumps(portable))
    monkeypatch.setattr(module, 'PORTABLE_MANIFEST', manifest)
    module.save_binding(original.state_dir, INSTANCE)
    script = tmp_path / 'helper.py'
    source = script.read_text().replace('import json,sys,shutil', 'import json,sys,shutil,os')
    selected = repr(r'USB\VID_0BD7&PID_A010\WRONG') if wrong_readback else "os.environ['SPECTRALSWEEP_ST133_INSTANCE']"
    source = source.replace('initialized=True,exposure_ms=100)',
        f'initialized=True,exposure_ms=100,binding_protocol=1,device_instance={selected})')
    script.write_text(source)
    observed = []
    def inspect_device(*, instance):
        observed.append(instance)
        return {'instance': instance, 'service': 'WINUSB', 'present': True}
    monkeypatch.setattr(persistent, 'inspect_device', inspect_device)
    monkeypatch.setenv(module.INSTANCE_ENV, r'USB\VID_0BD7&PID_A026\AMBIENT')
    owner = persistent.PersistentCaptureBridge(runtime_dir=tmp_path, state_dir=original.state_dir)
    try:
        if wrong_readback:
            with pytest.raises(RuntimeError, match='binding handshake'):
                owner.capture(threading.Event())
            assert owner._sequence == 0
        else:
            meta, payload = owner.capture(threading.Event())
            assert meta['device_instance'] == INSTANCE and len(payload) == 1024
        assert observed == [INSTANCE]
    finally:
        owner.close()
    assert all(p.poll() == 0 for p in processes)
