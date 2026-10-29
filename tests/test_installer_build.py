"""Installer release gates and isolated lifecycle orchestration; real Inno runs in Windows CI."""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tools import build_installer as installer
from tools.build_exe import build_metadata


@pytest.mark.parametrize("version,channel", [("build-7-abcdef01", "preview"), ("v1.2.3", "stable"), ("local", "local")])
def test_release_metadata_exposes_update_identity_and_utc_time(version, channel):
    value = build_metadata(version, "a" * 40, build_number=7)
    assert value["schema_version"] == 1 and value["version"] == version
    assert value["build_number"] == 7 and value["channel"] == channel
    assert value["commit"] == "a" * 40
    assert value["repository"] == "ylylyl98/SpectralSweep-pyside6"
    assert datetime.fromisoformat(value["built_at"]).utcoffset() == timezone.utc.utcoffset(None)


@pytest.mark.parametrize("number", [-1, True, "7"])
def test_release_metadata_rejects_invalid_build_numbers(number):
    with pytest.raises(ValueError, match="nonnegative integer"):
        build_metadata("local", "a" * 40, build_number=number)


@pytest.mark.parametrize("number,expected", [(0, "0.0.0.0"), (65535, "0.0.0.65535"), (65536, "0.0.1.0")])
def test_inno_file_version_keeps_components_within_windows_limits(number, expected):
    assert installer.installer_file_version({"build_number": number}) == expected


def test_missing_compiler_fails_with_actionable_message(tmp_path):
    with pytest.raises(FileNotFoundError, match="Inno Setup compiler not found"):
        installer.find_compiler(str(tmp_path / "missing.exe"))


@pytest.fixture
def bundle(tmp_path):
    folder = tmp_path / "bundle"
    folder.mkdir()
    for name in ("SpectralSweep.exe", "SpectralSweep-worker.exe", "README.txt", "_internal/needed.dll"):
        path = folder / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"packaged fixture")
    (folder / "version.json").write_text(json.dumps(build_metadata("build-7-abcdef01", "a" * 40, build_number=7)), encoding="utf-8")
    return folder


@pytest.mark.parametrize("value", ["../escaped", "version with spaces", "", "a/b"])
def test_installer_rejects_unsafe_asset_names_before_compiling(bundle, value):
    data = installer.load_metadata(bundle)
    data["version"] = value
    (bundle / "version.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="safe for installer filenames"):
        installer.load_metadata(bundle)


def fake_lifecycle(monkeypatch, bundle, *, fault=None):
    calls = []
    state = {}

    def run(command, log, *, env=None, cwd=None, timeout=300):
        calls.append(command)
        log.write_text("fixture", encoding="utf-8")
        if command[0].endswith("unins000.exe"):
            for path in state["installed"].rglob("*"):
                if path.is_file():
                    path.unlink()
            return
        if "--smoke-test" in command:
            report = Path(command[-1])
            (report / "smoke-result.json").write_text(json.dumps({"passed": fault != "smoke"}), encoding="utf-8")
            return
        destination = Path(next(arg[5:] for arg in command if arg.startswith("/DIR=")))
        state["installed"] = destination
        state["profile"] = Path(env["APPDATA"])
        shutil.copytree(bundle, destination, dirs_exist_ok=True)
        (destination / "unins000.exe").touch()
        if fault == "metadata":
            (destination / "version.json").write_text("{}", encoding="utf-8")
        if fault == "payload":
            (destination / "_internal/needed.dll").unlink()

    monkeypatch.setattr(installer, "run_logged", run)
    monkeypatch.setattr(installer, "verify_busy_guard", lambda *args: None)
    return calls, state


def test_installer_verifies_install_upgrade_uninstall_with_isolated_identity(bundle, tmp_path, monkeypatch):
    calls, state = fake_lifecycle(monkeypatch, bundle)
    result = installer.verify_installer(tmp_path / "release-Setup.exe", bundle, tmp_path / "report")
    assert result["passed"] and result["installed_smoke"] and result["upgrade"] and result["uninstalled"]
    installs = [command for command in calls if any(arg.startswith("/VerificationId=") for arg in command)]
    assert len(installs) == 2
    assert installs[0][1:-1] == installs[1][1:-1]
    assert "/NOICONS" in installs[0] and "/TASKS=" in installs[0]
    assert "/NORESTART" in installs[0] and "/NOCLOSEAPPLICATIONS" in installs[0]
    assert state["installed"] != bundle and state["profile"].parent == state["installed"].parent
    assert (tmp_path / "report/installer-result.json").is_file()


@pytest.mark.parametrize("fault", ["metadata", "smoke", "payload"])
def test_installer_failure_still_uninstalls_and_cannot_report_success(bundle, tmp_path, monkeypatch, fault):
    calls, _ = fake_lifecycle(monkeypatch, bundle, fault=fault)
    with pytest.raises(RuntimeError):
        installer.verify_installer(tmp_path / "release-Setup.exe", bundle, tmp_path / "report")
    assert calls[-1][0].endswith("unins000.exe")
    assert not (tmp_path / "report/installer-result.json").exists()


@pytest.mark.parametrize("outcome", ["refused", "accepted", "terminated", "unrelated-error"])
def test_busy_guard_requires_refusal_and_preserves_worker(tmp_path, monkeypatch, outcome):
    class Worker:
        stopped = False

        def wait(self, timeout):
            if not self.stopped:
                raise subprocess.TimeoutExpired("owned verification worker", timeout)
            return 0

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

    worker = Worker()
    monkeypatch.setattr(installer.subprocess, "Popen", lambda *args, **kwargs: worker)

    def setup(command, log, **kwargs):
        if outcome == "accepted":
            return
        if outcome == "terminated":
            worker.stopped = True
        (tmp_path / "busy-install.log").write_text(
            "background worker is running" if outcome != "unrelated-error" else "disk full", encoding="utf-8")
        raise subprocess.CalledProcessError(7, command)

    monkeypatch.setattr(installer, "run_logged", setup)
    if outcome == "refused":
        installer.verify_busy_guard(["release-Setup.exe"], tmp_path, tmp_path, tmp_path, {})
    else:
        with pytest.raises(RuntimeError):
            installer.verify_busy_guard(["release-Setup.exe"], tmp_path, tmp_path, tmp_path, {})
    assert worker.stopped  # The harness cleans up only its owned synthetic worker.
