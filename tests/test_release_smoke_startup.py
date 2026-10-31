import builtins
import json
from types import SimpleNamespace

from app import release_smoke


def test_startup_import_failure_cannot_report_success(tmp_path, monkeypatch):
    monkeypatch.setenv("SPECTRALSWEEP_SMOKE_STARTUP_IMPORT", "1")
    real_import = builtins.__import__

    def fail_main_window(name, *args, **kwargs):
        if name == "ui.main_window":
            raise ImportError("missing startup dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_main_window)
    assert release_smoke.run(tmp_path) == 1
    assert json.loads((tmp_path / "smoke-result.json").read_text())["passed"] is False
    assert "missing startup dependency" in (tmp_path / "smoke-error.txt").read_text()
    assert not (tmp_path / "startup-import.json").exists()


def test_startup_probe_precedes_preview_and_requires_child_success(tmp_path, monkeypatch):
    monkeypatch.delenv("SPECTRALSWEEP_SMOKE_STARTUP_IMPORT", raising=False)
    events = []

    def fail_probe(output):
        events.append("startup")
        raise RuntimeError("startup failed")

    monkeypatch.setattr(release_smoke, "verify_startup_import", fail_probe)
    assert release_smoke.run(tmp_path) == 1
    assert events == ["startup"]
    assert json.loads((tmp_path / "smoke-result.json").read_text())["passed"] is False
    assert "startup failed" in (tmp_path / "smoke-error.txt").read_text()


def test_startup_probe_uses_executable_and_rejects_child_failure(tmp_path, monkeypatch):
    calls = []

    def child(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=1, stderr="missing nitypes metadata")

    monkeypatch.setattr(release_smoke.sys, "frozen", True, raising=False)
    monkeypatch.setattr(release_smoke.subprocess, "run", child)
    try:
        release_smoke.verify_startup_import(tmp_path)
    except RuntimeError as error:
        assert "missing nitypes metadata" in str(error)
    else:
        raise AssertionError("failed startup probe was accepted")
    command, kwargs = calls[0]
    assert command[0] == release_smoke.sys.executable
    assert command[1] == "--smoke-test"
    assert kwargs["env"]["SPECTRALSWEEP_SMOKE_STARTUP_IMPORT"] == "1"
    config = tmp_path / "startup-import/profile/config.json"
    assert kwargs["env"]["SPECTRALSWEEP_CONFIG_PATH"] == str(config)
    assert json.loads(config.read_text()) == {}
