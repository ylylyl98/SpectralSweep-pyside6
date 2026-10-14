import hashlib
import importlib
import importlib.util
from pathlib import Path

import pytest


def installer():
    assert importlib.util.find_spec('tools.winspec.install_local_winusb') is not None, 'Runtime installer missing'
    return importlib.import_module('tools.winspec.install_local_winusb')


def test_private_install_checks_source_and_is_idempotent_without_device_io(tmp_path, monkeypatch):
    module = installer()
    import app.devices.winusb_runtime as runtime
    source = tmp_path/'source'; source.mkdir()
    (source/'raw_data_diagnostic.exe').write_bytes(b'fixture executable')
    pins = {'raw_data_diagnostic.exe': hashlib.sha256(b'fixture executable').hexdigest()}
    monkeypatch.setattr(runtime, 'manifest', lambda: {'runtime_files': pins})
    monkeypatch.setattr(runtime, 'inspect_device', lambda: pytest.fail('installation touched device'))
    target = tmp_path/'private/runtime-v1'
    assert module.install_runtime(source, target) == target.resolve()
    assert (target/'raw_data_diagnostic.exe').read_bytes() == b'fixture executable'
    assert module.install_runtime(source, target) == target.resolve()
    (source/'raw_data_diagnostic.exe').write_bytes(b'changed')
    with pytest.raises(RuntimeError): module.install_runtime(source, target)
    assert (target/'raw_data_diagnostic.exe').read_bytes() == b'fixture executable'


def test_persistent_install_uses_separate_pins_and_preserves_v1(tmp_path, monkeypatch):
    module = installer()
    import app.devices.winusb_persistent as persistent
    source=tmp_path/'source';source.mkdir()
    (source/'persistent_ingaas.exe').write_bytes(b'persistent fixture')
    pins={'persistent_ingaas.exe':hashlib.sha256(b'persistent fixture').hexdigest()}
    monkeypatch.setattr(persistent,'manifest',lambda:{'runtime_files':pins})
    old=tmp_path/'private/runtime-v1';old.mkdir(parents=True)
    (old/'old.exe').write_bytes(b'old')
    assert 'persistent' in __import__('inspect').signature(module.install_runtime).parameters
    target=tmp_path/'private/runtime-v2'
    assert module.install_runtime(source,target,persistent=True)==target.resolve()
    assert (old/'old.exe').read_bytes()==b'old'
    assert module.install_runtime(source,target,persistent=True)==target.resolve()


def test_persistent_default_destination_follows_release_manifest(tmp_path, monkeypatch):
    module=installer()
    import app.devices.winusb_persistent as persistent
    source=tmp_path/'source';source.mkdir();(source/'helper.exe').write_bytes(b'new')
    state=tmp_path/'state';old=state/'runtime-v2';old.mkdir(parents=True)
    (old/'helper.exe').write_bytes(b'previous')
    monkeypatch.setattr(module,'default_state_dir',lambda:state)
    monkeypatch.setattr(persistent,'manifest',lambda:{'runtime_directory':'runtime-v3',
        'runtime_files':{'helper.exe':hashlib.sha256(b'new').hexdigest()}})
    assert module.install_runtime(source,persistent=True)==(state/'runtime-v3').resolve()
    assert (old/'helper.exe').read_bytes()==b'previous'
