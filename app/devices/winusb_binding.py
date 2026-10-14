"""Local opt-in binding for the portable ST133 runtime; never device discovery."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re

from app.devices.winusb_evidence import read_receipt

PORTABLE_MANIFEST = Path(__file__).with_name('winusb_portable_runtime.json')
INSTANCE_ENV = 'SPECTRALSWEEP_ST133_INSTANCE'
INTERFACE_GUID = '{42D96BC8-048E-4A31-BD2E-16903596F637}'


def normalize_instance(value):
    if not isinstance(value, str) or not re.fullmatch(
            r'USB\\VID_0BD7&PID_A010\\[A-Z0-9&_.-]{1,200}', value, re.ASCII | re.IGNORECASE):
        raise ValueError('An exact ST133 A010 USB instance is required; PIXIS and wildcards are rejected')
    return value.upper()


def portable_release():
    data = json.loads(PORTABLE_MANIFEST.read_text(encoding='utf-8'))
    if (type(data.get('binding_protocol')) is not int or data['binding_protocol'] != 1
            or data.get('runtime_directory') != 'runtime-v6-portable'):
        raise RuntimeError('Unsupported portable runtime manifest')
    return data


def portable_manifest_bytes():
    # Git may check out the same JSON with LF or CRLF on another computer.
    # Bind its content, retaining every key and runtime hash, not formatting.
    return json.dumps(portable_release(), sort_keys=True, separators=(',', ':'),
                      allow_nan=False, ensure_ascii=True).encode('ascii')


def manifest_fingerprint():
    return hashlib.sha256(portable_manifest_bytes()).hexdigest()


def _binding_path(state_dir):
    path = Path(state_dir).absolute() / 'device-binding.json'
    if path.resolve() != path or any(
            p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction())
            for p in (path, *path.parents)):
        raise RuntimeError('Device binding path is redirected')
    return path


def select_release(state_dir, fallback):
    path = _binding_path(state_dir)
    if not path.exists():
        return copy.deepcopy(fallback)
    try:
        binding = read_receipt(path)
        if (set(binding) != {'schema_version', 'device_instance', 'manifest_sha256'}
                or type(binding['schema_version']) is not int or binding['schema_version'] != 1):
            raise ValueError('unsupported binding schema')
        instance = normalize_instance(binding['device_instance'])
        if binding['manifest_sha256'] != manifest_fingerprint():
            raise RuntimeError('Device binding manifest changed; review and enroll this installation again')
        release = portable_release()
        release['device_instance'] = instance
        return release
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError('Invalid device binding; no fallback camera will be opened') from exc


def save_binding(state_dir, instance):
    """Publish a binding after the installer has verified runtime and PnP state.

    Existing different bindings and unresolved capture ownership are retained.
    This function does not enumerate/open hardware or change a driver.
    """
    instance = normalize_instance(instance)
    path = _binding_path(state_dir)
    if (path.parent / 'active.json').exists():
        raise RuntimeError('Capture owner is active or unresolved; binding unchanged')
    portable_release()
    value = {'schema_version': 1, 'device_instance': instance,
             'manifest_sha256': manifest_fingerprint()}
    if path.exists():
        if read_receipt(path) != value:
            raise RuntimeError('A different binding already exists; keep it and review the device change')
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation never overwrites another installer. A partial record
    # after interruption fails closed instead of reverting to the old camera.
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    return path
