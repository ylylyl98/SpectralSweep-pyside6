"""Prepare dependencies only when needed, then forward arguments to main.py."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"


def dependency_fingerprint(requirements: Path) -> str:
    digest = hashlib.sha256(requirements.read_bytes())
    # A recreated/upgraded interpreter must establish its own install record.
    digest.update(str(Path(sys.executable).resolve()).encode())
    digest.update(sys.version.encode())
    return digest.hexdigest()


def dependency_cache_path() -> Path:
    if sys.prefix != sys.base_prefix:
        return Path(sys.prefix) / ".spectralsweep-requirements.sha256"
    # A plain system Python may live in a read-only installation directory.
    cache_root = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache"))
    interpreter = hashlib.sha256(str(Path(sys.executable).resolve()).encode()).hexdigest()[:16]
    return cache_root / "SpectralSweep" / "launcher" / f"{interpreter}.sha256"


def ensure_dependencies(*, refresh: bool = False, upgrade: bool = False) -> int:
    fingerprint = dependency_fingerprint(REQUIREMENTS)
    cache = dependency_cache_path()
    try:
        cached = cache.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        cached = ""
    if cached == fingerprint and not (refresh or upgrade):
        return 0
    # An interrupted/failed install may have partially changed the environment.
    # Remove the old success record before asking pip to make any changes.
    try:
        cache.unlink(missing_ok=True)
    except OSError as exc:
        print(f"Could not clear dependency cache: {exc}", file=sys.stderr)
        return 1
    print("Checking SpectralSweep dependencies...", flush=True)
    command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input"]
    if upgrade:
        command.append("--upgrade")
    command.extend(["-r", str(REQUIREMENTS)])
    result = subprocess.run(command, cwd=PROJECT_ROOT)
    if result.returncode:
        return result.returncode
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
        temporary.write_text(fingerprint + "\n", encoding="ascii")
        temporary.replace(cache)
    except OSError as exc:
        print(f"Dependency installation succeeded; could not cache it: {exc}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    install_only = "--install" in args
    upgrade = "--update-deps" in args
    app_args = [arg for arg in args if arg not in {"--install", "--update-deps"}]
    result = ensure_dependencies(refresh=install_only, upgrade=upgrade)
    if result or install_only:
        return result
    return subprocess.run([sys.executable, str(PROJECT_ROOT / "main.py"), *app_args], cwd=PROJECT_ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
