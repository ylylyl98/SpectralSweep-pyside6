"""Launcher contracts; never run pip, launch the application, or contact devices."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import launch_app


@pytest.fixture(autouse=True)
def isolated_launch_log(tmp_path, monkeypatch):
    monkeypatch.setattr(launch_app, "launch_log_path", lambda: tmp_path / "logs" / "launch.log")


def test_successful_install_is_cached_and_changed_requirements_reinstall(tmp_path, monkeypatch):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("PySide6>=6.7\n", encoding="utf-8")
    cache = tmp_path / "venv" / "fingerprint"
    calls = []
    monkeypatch.setattr(launch_app, "REQUIREMENTS", requirements)
    monkeypatch.setattr(launch_app, "dependency_cache_path", lambda: cache)
    monkeypatch.setattr(launch_app.subprocess, "run", lambda command, **kwargs: calls.append(command) or SimpleNamespace(returncode=0))
    assert launch_app.ensure_dependencies() == 0
    assert launch_app.ensure_dependencies() == 0
    assert len(calls) == 1
    requirements.write_text("PySide6>=6.8\n", encoding="utf-8")
    assert launch_app.ensure_dependencies() == 0
    assert len(calls) == 2


def test_failed_install_does_not_cache_success_or_start_application(tmp_path, monkeypatch):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("PySide6\n", encoding="utf-8")
    cache = tmp_path / "fingerprint"
    calls = []
    monkeypatch.setattr(launch_app, "REQUIREMENTS", requirements)
    monkeypatch.setattr(launch_app, "dependency_cache_path", lambda: cache)
    monkeypatch.setattr(launch_app.subprocess, "run", lambda command, **kwargs: calls.append(command) or SimpleNamespace(returncode=3))
    cache.write_text(launch_app.dependency_fingerprint(requirements), encoding="ascii")
    assert launch_app.main(["--watch-styles", "--update-deps"]) == 3
    assert len(calls) == 1
    assert not cache.exists()


def test_launcher_flags_are_consumed_and_app_arguments_are_forwarded(monkeypatch):
    dependency_calls, app_calls = [], []
    monkeypatch.setattr(launch_app, "ensure_dependencies", lambda **kwargs: dependency_calls.append(kwargs) or 0)
    monkeypatch.setattr(launch_app.subprocess, "run", lambda command, **kwargs: app_calls.append(command) or SimpleNamespace(returncode=7))
    assert launch_app.main(["--update-deps", "--watch-styles", "--preview-spectrum"]) == 7
    assert dependency_calls == [{"refresh": False, "upgrade": True}]
    assert app_calls[0][-2:] == ["--watch-styles", "--preview-spectrum"]
    assert "--update-deps" not in app_calls[0]
    assert launch_app.main(["--install"]) == 0
    assert dependency_calls[-1] == {"refresh": True, "upgrade": False}
    assert len(app_calls) == 1


def test_native_crash_output_is_retained_and_reported(monkeypatch, capsys):
    def crash(command, **kwargs):
        assert command[1:3] == ["-X", "faulthandler"]
        assert command[-1] == "--ui=quick"
        kwargs["stderr"].write(b"Windows fatal exception: access violation\n")
        return SimpleNamespace(returncode=0xC0000005)

    monkeypatch.setattr(launch_app.subprocess, "run", crash)
    assert launch_app.run_application(["--ui=quick"]) == 0xC0000005
    path = launch_app.launch_log_path()
    assert "access violation" in path.read_text()
    diagnostic = capsys.readouterr().err
    assert "access violation" in diagnostic
    assert str(path) in diagnostic


def test_unwritable_log_does_not_prevent_application_launch(monkeypatch, tmp_path):
    unavailable = tmp_path / "file-instead-of-directory"
    unavailable.write_text("keep")
    monkeypatch.setattr(launch_app, "launch_log_path", lambda: unavailable / "launch.log")
    calls = []
    monkeypatch.setattr(launch_app.subprocess, "run", lambda *args, **kwargs:
                        calls.append((args, kwargs)) or SimpleNamespace(returncode=0))
    assert launch_app.run_application([]) == 0
    assert len(calls) == 1 and "stderr" not in calls[0][1]
    assert unavailable.read_text() == "keep"
