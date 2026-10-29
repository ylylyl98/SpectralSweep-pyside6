"""Build, relocate, smoke-test and archive the Windows portable application."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="local")
    parser.add_argument("--clean", action="store_true", help="Recompute PyInstaller's dependency cache")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Build the Windows EXE on Windows")
    if not args.version or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in args.version):
        parser.error("Version must contain only letters, digits, dot, underscore or hyphen")
    # Native helper tools on a developer's PATH can contain incompatible ICU
    # and Windows CRT DLLs. Only resolve build dependencies from Python/Windows.
    windows = Path(os.environ.get("WINDIR", "C:/Windows"))
    build_env = dict(os.environ)
    build_env["PATH"] = os.pathsep.join(map(str, [Path(sys.executable).parent,
        Path(sys.base_prefix), Path(sys.base_prefix) / "DLLs", windows / "System32", windows]))
    clean = ["--clean"] if args.clean else []
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", *clean,
                    "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"),
                    str(ROOT / "packaging/SpectralSweep.spec")], cwd=ROOT, env=build_env, check=True)
    bundle = ROOT / "dist/SpectralSweep"
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    (bundle / "version.json").write_text(json.dumps({
        "version": args.version, "commit": commit, "python": sys.version,
    }, indent=2), encoding="utf-8")
    shutil.copy2(ROOT / "packaging/PORTABLE_README.txt", bundle / "README.txt")
    # Launch outside the checkout. A successful import from the source tree must
    # never conceal a missing module/resource in the EXE.
    report = ROOT / "tmp/release-smoke"
    report.mkdir(parents=True, exist_ok=True)
    for filename in ("smoke-result.json", "smoke-error.txt", "process.log"):
        (report / filename).unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="SpectralSweep-release-") as scratch:
        relocated = Path(scratch) / "SpectralSweep"
        shutil.copytree(bundle, relocated)
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
        env.pop("PYTHONPATH", None)
        result = subprocess.run([str(relocated / "SpectralSweep.exe"), "--smoke-test", str(report)],
                                cwd=scratch, env=env, timeout=240, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        (report / "process.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        if result.returncode:
            error = report / "smoke-error.txt"
            if error.exists():
                print(error.read_text(encoding="utf-8"), file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            raise RuntimeError(f"Packaged smoke test failed ({result.returncode}); inspect {report}")
        if not json.loads((report / "smoke-result.json").read_text(encoding="utf-8"))["passed"]:
            raise RuntimeError("Packaged smoke test did not report success")
    archive = ROOT / "dist" / f"SpectralSweep-{args.version}-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as target:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                target.write(path, path.relative_to(bundle.parent))
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(f"Verified portable release: {archive} ({archive.stat().st_size / 1024**2:.1f} MiB)")


if __name__ == "__main__":
    main()
