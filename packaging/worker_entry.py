"""Console entry point: retain pipes even when the desktop EXE is windowed."""
import runpy
import sys

from app.subprocess_command import WORKER_MODULES


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] != "-m" or sys.argv[2] not in WORKER_MODULES:
        raise SystemExit("Expected -m and a supported SpectralSweep worker module")
    module = sys.argv[2]
    sys.argv = [module, *sys.argv[3:]]
    runpy.run_module(module, run_name="__main__", alter_sys=True)
