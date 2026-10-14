"""Transfer the reviewed ST133 runtime privately and enroll one exact USB instance.

Run from the checkout with ``python -m tools.winspec.migrate_winusb --help``.
Copy/install/check never open the camera. Only the explicit capture command does.
Driver selection stays in Windows Device Manager; this tool never rebinds it.
"""
import argparse
from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import struct
import subprocess
import sys
import threading
import uuid
import zipfile

from app.devices import winusb_binding as binding
from app.devices.winusb_runtime import default_state_dir, inspect_device, verify_runtime

MUTEX = 'Local\\Codex.WinSpec.Recognition.Startup.v1'
MAX_BUNDLE_BYTES = 64 * 1024 * 1024


def safe_path(path):
    path = Path(path).absolute()
    if path.resolve() != path or any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise RuntimeError(f'Redirected installation path: {path}')
    return path


def release_files():
    release = binding.portable_release()
    pins = release['runtime_files']
    if not isinstance(pins, dict) or not pins:
        raise RuntimeError('Empty portable runtime inventory')
    for name, digest in pins.items():
        parts = PurePosixPath(name).parts
        if (not parts or '/'.join(parts) != name or any(p in ('.', '..') for p in parts)
                or '\\' in name or ':' in name or name.startswith('/')
                or not isinstance(digest, str) or len(digest) != 64
                or any(c not in '0123456789abcdef' for c in digest)):
            raise RuntimeError('Invalid portable runtime inventory')
    if len({name.casefold() for name in pins}) != len(pins):
        raise RuntimeError('Ambiguous portable runtime inventory')
    return release, pins


def create_bundle(source, archive):
    _, pins = release_files()
    source = verify_runtime(source, pins=pins)
    archive = safe_path(archive)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr('manifest.json', binding.portable_manifest_bytes())
        for name in sorted(pins):
            payload = (source / name).read_bytes()
            if hashlib.sha256(payload).hexdigest() != pins[name]:
                raise RuntimeError(f'Runtime changed during packaging: {name}')
            output.writestr('runtime/' + name, payload)
    return archive


def install_bundle(archive, state_dir):
    release, pins = release_files()
    state_dir = safe_path(state_dir)
    destination = safe_path(state_dir / release['runtime_directory'])
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        expected = {'manifest.json', *('runtime/' + name for name in pins)}
        if len(entries) != len(expected) or {i.filename for i in entries} != expected:
            raise RuntimeError('Transfer ZIP inventory has missing, duplicate or unexpected files')
        if (sum(i.file_size for i in entries) > MAX_BUNDLE_BYTES
                or any(i.file_size > 16 * 1024 * 1024 or i.flag_bits & 1
                       or stat.S_ISLNK(i.external_attr >> 16) for i in entries)
                or source.getinfo('manifest.json').file_size > 16384):
            raise RuntimeError('Transfer ZIP size/type is unsupported')
        if source.read('manifest.json') != binding.portable_manifest_bytes():
            raise RuntimeError('Transfer ZIP does not match this checkout release manifest')
        if destination.exists():
            verify_runtime(destination, pins=pins)
            return destination  # Never replace an installed runtime.
        stage = state_dir / ('runtime-staging-' + uuid.uuid4().hex)
        stage.mkdir(parents=True, exist_ok=False)
        for name in pins:
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open('runtime/' + name) as incoming, target.open('xb') as output:
                shutil.copyfileobj(incoming, output, length=65536)
                output.flush()
                os.fsync(output.fileno())
        verify_runtime(stage, pins=pins)
        # rename does not replace an existing directory on Windows. Interrupted
        # or failed installs leave their unique staging directory for inspection.
        stage.rename(destination)
    verify_runtime(destination, pins=pins)
    return destination


def require_platform():
    if sys.platform != 'win32' or platform.machine().upper() not in ('AMD64', 'X86_64') or struct.calcsize('P') != 8:
        raise RuntimeError('Use 64-bit Python on Windows Intel/AMD 64-bit')


@contextmanager
def installation_guard(state_dir):
    require_platform()
    state_dir = safe_path(state_dir)
    if (state_dir / 'active.json').exists():
        raise RuntimeError('Capture owner is active or unresolved; installation binding unchanged')
    kernel = ctypes.WinDLL('kernel32.dll', use_last_error=True, winmode=0x00000800)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.CreateMutexW(None, False, MUTEX)
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    try:
        if error == 183:  # ERROR_ALREADY_EXISTS; same guard as the native helper.
            raise RuntimeError('A camera helper or installer is active; no changes made')
        yield
    finally:
        kernel.CloseHandle(handle)


def interface_values(instance):
    import winreg
    path = 'SYSTEM\\CurrentControlSet\\Enum\\' + binding.normalize_instance(instance) + '\\Device Parameters'
    values = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ) as key:
            for name in ('DeviceInterfaceGUID', 'DeviceInterfaceGUIDs'):
                try:
                    value, kind = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                if ((name.endswith('GUIDs') and (kind != winreg.REG_MULTI_SZ or not isinstance(value, list)))
                        or (not name.endswith('GUIDs') and (kind != winreg.REG_SZ or not isinstance(value, str)))):
                    raise RuntimeError('Unexpected device interface registry type; unchanged')
                values[name] = {'value': value, 'type': kind}
    except FileNotFoundError:
        pass
    return path, values


def require_interface(instance):
    _, values = interface_values(instance)
    guids = []
    for item in values.values():
        guids.extend(item['value'] if isinstance(item['value'], list) else [item['value']])
    if binding.INTERFACE_GUID.upper() not in [v.upper() for v in guids]:
        raise RuntimeError('ST133 interface GUID is missing; run register-interface as administrator, then unplug/replug ST133')
    return guids


def register_interface(state_dir, instance):
    import winreg
    require_platform()
    instance = binding.normalize_instance(instance)
    shell = ctypes.WinDLL('shell32.dll', winmode=0x00000800)
    shell.IsUserAnAdmin.restype = ctypes.c_int
    if not shell.IsUserAnAdmin():
        raise RuntimeError('Run only register-interface in an Administrator PowerShell')
    with installation_guard(state_dir):
        inspect_device(instance=instance)
        path, before = interface_values(instance)
        values = list(before.get('DeviceInterfaceGUIDs', {}).get('value', []))
        single = before.get('DeviceInterfaceGUID', {}).get('value')
        if single and single.upper() not in [v.upper() for v in values]:
            values.append(single)
        if binding.INTERFACE_GUID.upper() not in [v.upper() for v in values]:
            values.append(binding.INTERFACE_GUID)
        backup = safe_path(state_dir) / ('interface-backup-' + uuid.uuid4().hex + '.json')
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open('x', encoding='utf-8') as output:
            json.dump({'instance': instance, 'registry_path': path, 'before': before,
                       'after_DeviceInterfaceGUIDs': values}, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        inspect_device(instance=instance)
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, 'DeviceInterfaceGUIDs', 0, winreg.REG_MULTI_SZ, values)
        require_interface(instance)
    return {'backup': str(backup), 'next': 'Unplug/replug only ST133 in the SAME USB port, then bind/check'}


def enroll(state_dir, instance):
    instance = binding.normalize_instance(instance)
    release, pins = release_files()
    verify_runtime(safe_path(state_dir) / release['runtime_directory'], pins=pins)
    inspect_device(instance=instance)
    with installation_guard(state_dir):
        inspect_device(instance=instance)
        require_interface(instance)
        return binding.save_binding(state_dir, instance)


def check(state_dir):
    require_platform()
    if not (safe_path(state_dir) / 'device-binding.json').exists():
        raise RuntimeError('No portable device binding; run devices and bind first')
    release = binding.select_release(state_dir, {})
    runtime = verify_runtime(Path(state_dir) / release['runtime_directory'], pins=release['runtime_files'])
    device = inspect_device(instance=release['device_instance'])
    require_interface(device['instance'])
    return {'runtime': str(runtime), 'device': device, 'release': release['release'],
            'owner_record_present': (Path(state_dir) / 'active.json').exists(),
            'camera_opened': False, 'physical_capture_verified': False}


def list_devices():
    require_platform()
    script = ("@(Get-PnpDevice -PresentOnly -ErrorAction Stop | Where-Object { "
              "$_.InstanceId -like 'USB\\VID_0BD7&PID_A010\\*' } | "
              "Select-Object InstanceId,FriendlyName,Status) | ConvertTo-Json -Compress")
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(executable), '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, text=True, check=True, timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    return json.loads(result.stdout) if result.stdout.strip() else []


def capture(state_dir, output, frames=3, exposure_ms=100):
    """Explicit, bounded detector-only test; normal cleanup, no retry or kill."""
    from app.devices.winusb_persistent import PersistentCaptureBridge
    from app.experiment_integration import persist_output_snapshot
    from app.devices.winusb_recipe import recipe
    if type(frames) is not int or not 1 <= frames <= 10:
        raise ValueError('Choose 1..10 test frames')
    recipe(exposure_ms, 1)
    checked = check(state_dir)
    if checked['owner_record_present']:
        raise RuntimeError('Capture owner is unresolved; inspect active.json before proceeding')
    output = safe_path(output)
    if output.is_relative_to(safe_path(state_dir)):
        raise ValueError('Save test exports outside the runtime/cache directory')
    output.mkdir(parents=True, exist_ok=False)
    bridge = PersistentCaptureBridge(state_dir=state_dir)
    rows = []
    try:
        for number in range(1, frames + 1):
            metadata, payload = bridge.capture(threading.Event(), exposure_ms=exposure_ms,
                                               context={'migration_test': True, 'optics_commanded': False})
            values = list(struct.unpack('<512H', payload))
            snapshot = {'native_capture': metadata, 'raw_accumulated_counts': values}
            exported = output / f'frame-{number:03d}.csv'
            with exported.open('x', encoding='utf-8', newline='') as stream:
                stream.write('pixel,intensity_counts\n' + ''.join(f'{i+1},{v}\n' for i, v in enumerate(values)))
                stream.flush()
                os.fsync(stream.fileno())
            sidecar = persist_output_snapshot(exported, experiment_type='spectrum_preview',
                device_id='st133-migration-test', settings={'acquisition_time_snapshot': snapshot})
            if not bridge._cache.acknowledge_saved(snapshot, exported, sidecar):
                raise RuntimeError('Export acknowledgement failed')
            rows.append({'frame': number, 'payload_sha256': metadata['payload_sha256'],
                         'zero_count': values.count(0), 'csv': str(exported), 'sidecar': str(sidecar)})
    finally:
        bridge.close()
    if bridge._fault:
        # A proven cleanup barrier releases ownership even if final logging or
        # shutdown failed. It must not turn that failed test into a success.
        raise RuntimeError(bridge._fault)
    result = {'frames': rows, 'normal_close_verified': not (Path(state_dir) / 'active.json').exists(),
              'temperature_verified': False, 'optical_freshness_verified': False}
    (output / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=default_state_dir())
    commands = parser.add_subparsers(dest='command', required=True)
    bundle = commands.add_parser('bundle', help='Package only pinned runtime files for PRIVATE transfer')
    bundle.add_argument('--source', type=Path, required=True)
    bundle.add_argument('--zip', type=Path, required=True)
    install = commands.add_parser('install', help='Verify and extract the private transfer ZIP; no hardware access')
    install.add_argument('--zip', type=Path, required=True)
    commands.add_parser('devices', help='List present ST133 A010 devices, without opening them')
    for name in ('register-interface', 'bind'):
        commands.add_parser(name).add_argument('--instance', required=True)
    commands.add_parser('check', help='Read-only runtime, binding, PnP and interface checks')
    acquire = commands.add_parser('capture', help='Explicitly acquire 1..10 frames, export, acknowledge and close')
    acquire.add_argument('--output', type=Path, required=True)
    acquire.add_argument('--frames', type=int, default=3)
    acquire.add_argument('--exposure-ms', type=int, default=100)
    args = parser.parse_args()
    if args.command == 'bundle':
        result = create_bundle(args.source, args.zip)
    elif args.command == 'install':
        result = install_bundle(args.zip, args.state_dir)
    elif args.command == 'devices':
        result = list_devices()
    elif args.command == 'register-interface':
        result = register_interface(args.state_dir, args.instance)
    elif args.command == 'bind':
        result = enroll(args.state_dir, args.instance)
    elif args.command == 'check':
        result = check(args.state_dir)
    else:
        result = capture(args.state_dir, args.output, args.frames, args.exposure_ms)
    print(json.dumps(result, indent=2, default=str))


if __name__ == '__main__':
    main()
