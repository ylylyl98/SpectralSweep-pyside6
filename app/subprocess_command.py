"""Launch internal workers with Python or the packaged console helper."""
from pathlib import Path
import sys


WORKER_MODULES = frozenset({"app.history_reader", "app.process_watchdog"})


def module_command(module: str, *args: str) -> list[str]:
    if module not in WORKER_MODULES:
        raise ValueError(f"Unsupported worker module: {module}")
    if getattr(sys, "frozen", False):
        helper = Path(sys.executable).with_name("SpectralSweep-worker.exe")
        if not helper.is_file():
            raise FileNotFoundError(f"Packaged worker is missing: {helper}")
        return [str(helper), "-m", module, *args]
    return [sys.executable, "-m", module, *args]
