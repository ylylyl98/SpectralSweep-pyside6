"""Install the exact experimental x86 runtime without changing any driver.

Run with --source-runtime <verified rawdiag001/runtime>. Vendor files stay in a
private local directory and are not distributed in the application repository.
"""
import argparse
from pathlib import Path
import shutil
import sys
import uuid

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.devices.winusb_runtime import default_runtime_dir, default_state_dir, verify_runtime


def install_runtime(source, destination=None, *, persistent=False):
    pins = None
    default = default_runtime_dir()
    if persistent:
        from app.devices.winusb_persistent import manifest
        release = manifest()
        pins = release['runtime_files']
        default = default_state_dir()/release.get('runtime_directory','runtime-v2')
    source = verify_runtime(source, pins=pins)
    destination = Path(destination or default).absolute()
    if destination.exists():
        return verify_runtime(destination, pins=pins)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Only this new directory is renamed; existing installations are never replaced.
    staging = destination.parent/('runtime-staging-'+uuid.uuid4().hex[:8])
    shutil.copytree(source, staging)
    verify_runtime(staging, pins=pins)
    staging.rename(destination)
    return verify_runtime(destination, pins=pins)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-runtime', type=Path, required=True)
    parser.add_argument('--persistent', action='store_true', help='Install the separately pinned reusable session runtime')
    args = parser.parse_args()
    result = install_runtime(args.source_runtime, persistent=args.persistent)
    print(f'Local WinUSB runtime verified and installed: {result}')
    print('No device opened. No drivers or application configuration changed.')


if __name__ == '__main__':
    main()
