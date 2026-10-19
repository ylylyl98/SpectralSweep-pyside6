"""Launcher contracts; never run pip, launch the application, or contact devices."""
from pathlib import Path
from types import SimpleNamespace

from tools import launch_app


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
