"""Compile and verify the per-user Windows installer without touching an existing install."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def load_metadata(bundle: Path) -> dict:
    metadata = json.loads((bundle / "version.json").read_text(encoding="utf-8"))
    if not re.fullmatch(r"[A-Za-z0-9._-]+", metadata.get("version", "")):
        raise ValueError("Bundle version must be safe for installer filenames")
    if not re.fullmatch(r"[0-9a-f]{40}", metadata.get("commit", "")):
        raise ValueError("Bundle must identify an exact Git commit")
    return metadata


def find_compiler(explicit: str | None = None) -> Path:
    if explicit:
        compiler = Path(explicit).resolve()
        if not compiler.is_file():
            raise FileNotFoundError(f"Inno Setup compiler not found: {compiler}")
        return compiler
    candidates = [os.environ.get("ISCC"), shutil.which("ISCC.exe")]
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs")):
        if base:
            candidates.extend(str(Path(base) / f"Inno Setup {version}" / "ISCC.exe")
                              for version in (7, 6))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise FileNotFoundError("Install Inno Setup 6.3+ or pass --compiler PATH; Windows Actions images include ISCC")


def installer_file_version(metadata: dict) -> str:
    build = metadata.get("build_number", 0)
    if type(build) is not int or not 0 <= build < 2**32:
        raise ValueError("build_number must be an integer from 0 to 4294967295")
    return f"0.0.{build >> 16}.{build & 65535}"


def run_logged(command: list[str], log: Path, *, env=None, cwd=None, timeout=300):
    with log.open("w", encoding="utf-8") as stream:
        subprocess.run(command, check=True, timeout=timeout, env=env, cwd=cwd,
                       stdout=stream, stderr=subprocess.STDOUT)


def verify_payload(bundle: Path, installed: Path):
    for source in bundle.rglob("*"):
        if source.is_file():
            target = installed / source.relative_to(bundle)
            if not target.is_file() or target.stat().st_size != source.stat().st_size:
                raise RuntimeError(f"Installed payload missing or changed: {source.relative_to(bundle)}")


def verify_busy_guard(install_args, installed, scratch, report, env):
    """An owned, idle watchdog proves Setup refuses a running worker, without killing it."""
    command = [str(installed / "SpectralSweep-worker.exe"), "-m", "app.process_watchdog",
               "--state", str(scratch / "absent-verification-state.json"),
               "--token=installer-verification", "--pid", str(os.getpid()),
               "--url", "invalid-verification-url"]
    with (report / "busy-worker.log").open("w", encoding="utf-8") as log:
        worker = subprocess.Popen(command, env=env, cwd=scratch, stdout=log, stderr=subprocess.STDOUT)
        try:
            try:
                worker.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            else:
                raise RuntimeError("Verification worker exited before the installer busy check")
            try:
                run_logged([*install_args, f"/LOG={report / 'busy-install.log'}"],
                           report / "busy-install-process.log", env=env, cwd=scratch, timeout=60)
            except subprocess.CalledProcessError:
                message = (report / "busy-install.log").read_text(encoding="utf-8-sig", errors="replace")
                if "background worker is running" not in message:
                    raise RuntimeError("Setup failed for a reason other than the running-worker guard")
            else:
                raise RuntimeError("Setup accepted installation while its worker was running")
            if worker.poll() is not None:
                raise RuntimeError("Setup terminated the running verification worker")
        finally:
            # This is our synthetic, idle test worker, never an acquisition process.
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=10)


def verify_installer(installer: Path, bundle: Path, report: Path) -> dict:
    metadata = load_metadata(bundle)
    payload = [path.relative_to(bundle) for path in bundle.rglob("*") if path.is_file()]
    report.mkdir(parents=True, exist_ok=True)
    result_file = report / "installer-result.json"
    result_file.unlink(missing_ok=True)
    identity = str(uuid.uuid4())
    with tempfile.TemporaryDirectory(prefix="SpectralSweep-installer-") as directory:
        scratch = Path(directory).resolve()
        installed = scratch / "installed"
        profile = scratch / "profile"
        profile.mkdir()
        marker = profile / "SpectralSweep" / "config.json"
        marker.parent.mkdir()
        marker.write_text('{"installer_verification":"preserve-user-settings"}', encoding="utf-8")
        marker_bytes = marker.read_bytes()
        env = dict(os.environ, APPDATA=str(profile), LOCALAPPDATA=str(profile / "local"),
                   SPECTRALSWEEP_CONFIG_PATH=str(marker),
                   QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
        env.pop("PYTHONPATH", None)
        install_args = [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-",
                        "/NORESTART", "/NOCLOSEAPPLICATIONS", "/NORESTARTAPPLICATIONS",
                        "/NOICONS", "/TASKS=", f"/DIR={installed}",
                        f"/VerificationId={identity}"]
        uninstaller = installed / "unins000.exe"
        try:
            run_logged([*install_args, f"/LOG={report / 'install.log'}"],
                       report / "install-process.log", env=env, cwd=scratch)
            for filename in ("SpectralSweep.exe", "SpectralSweep-worker.exe", "version.json", "README.txt"):
                if not (installed / filename).is_file():
                    raise RuntimeError(f"Installed file missing: {filename}")
            installed_metadata = json.loads((installed / "version.json").read_text(encoding="utf-8"))
            if installed_metadata != metadata or not (installed / "_internal").is_dir():
                raise RuntimeError("Installed bundle metadata or dependency directory differs")
            verify_payload(bundle, installed)
            smoke = report / "installed-smoke"
            smoke.mkdir(exist_ok=True)
            for filename in ("smoke-result.json", "smoke-error.txt"):
                (smoke / filename).unlink(missing_ok=True)
            run_logged([str(installed / "SpectralSweep.exe"), "--smoke-test", str(smoke)],
                       report / "installed-smoke-process.log", env=env, cwd=scratch, timeout=240)
            if not json.loads((smoke / "smoke-result.json").read_text(encoding="utf-8"))["passed"]:
                raise RuntimeError("Installed application smoke did not pass")
            verify_busy_guard(install_args, installed, scratch, report, env)
            # Reinstall the same bundle over the same identity to exercise the
            # upgrade path and existing uninstall-log handling without mutation.
            run_logged([*install_args, f"/LOG={report / 'upgrade.log'}"],
                       report / "upgrade-process.log", env=env, cwd=scratch)
            if json.loads((installed / "version.json").read_text(encoding="utf-8")) != metadata:
                raise RuntimeError("Upgrade changed the installed build identity")
            verify_payload(bundle, installed)
        finally:
            if uninstaller.is_file():
                run_logged([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                            f"/LOG={report / 'uninstall.log'}"],
                           report / "uninstall-process.log", env=env, cwd=scratch)
                # Inno may return after dispatching its temporary uninstaller.
                deadline = time.monotonic() + 30
                while any((installed / path).exists() for path in payload) and time.monotonic() < deadline:
                    time.sleep(.1)
        if (installed / "SpectralSweep.exe").exists() or (installed / "SpectralSweep-worker.exe").exists():
            raise RuntimeError("Uninstall retained application executable files")
        if any((installed / path).is_file() for path in payload):
            raise RuntimeError("Uninstall retained installed payload files")
        if marker.read_bytes() != marker_bytes:
            raise RuntimeError("Install/upgrade/uninstall changed the isolated user configuration")
        result = {"passed": True, "version": metadata["version"], "commit": metadata["commit"],
                  "installed_smoke": True, "upgrade": True, "uninstalled": True,
                  "running_worker_blocks_install": True, "worker_not_terminated_by_setup": True,
                  "user_configuration_preserved": True, "verification_identity": identity}
        result_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=ROOT / "dist/SpectralSweep")
    parser.add_argument("--compiler")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Compile and validate the Windows installer on Windows")
    bundle = args.bundle.resolve()
    metadata = load_metadata(bundle)
    compiler = find_compiler(args.compiler)
    report = ROOT / "tmp/installer-smoke"
    report.mkdir(parents=True, exist_ok=True)
    (report / "installer-result.json").unlink(missing_ok=True)
    output = ROOT / "dist"
    installer = output / f"SpectralSweep-{metadata['version']}-windows-x64-Setup.exe"
    installer.unlink(missing_ok=True)
    run_logged([str(compiler), f"/DSourceDir={bundle}", f"/DOutputDir={output}",
                f"/DReleaseVersion={metadata['version']}",
                f"/DFileVersion={installer_file_version(metadata)}",
                str(ROOT / "packaging/SpectralSweep.iss")], report / "compiler.log", timeout=600)
    if not installer.is_file():
        raise RuntimeError("Inno Setup did not produce the expected installer")
    verify_installer(installer, bundle, report)
    with installer.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    installer.with_suffix(".exe.sha256").write_text(f"{digest}  {installer.name}\n", encoding="ascii")
    print(f"Verified Windows installer: {installer} ({installer.stat().st_size / 1024**2:.1f} MiB)")


if __name__ == "__main__":
    main()
