"""The windowed UI must use a console helper for JSON pipe workers."""
import json
import subprocess
import sys

import pytest

from app.subprocess_command import module_command


def test_source_worker_round_trip():
    result = subprocess.run(module_command("app.history_reader"),
                            input='{"operation":"probe"}', text=True,
                            encoding="utf-8", capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"error": "Unknown history operation"}


def test_frozen_worker_uses_sibling_exe_and_preserves_arguments(tmp_path, monkeypatch):
    helper = tmp_path / "SpectralSweep-worker.exe"
    helper.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "SpectralSweep.exe"))
    assert module_command("app.process_watchdog", "--state", "a path.json") == [
        str(helper), "-m", "app.process_watchdog", "--state", "a path.json"]
    helper.unlink()
    with pytest.raises(FileNotFoundError, match="worker is missing"):
        module_command("app.history_reader")


def test_worker_rejects_arbitrary_modules():
    with pytest.raises(ValueError, match="Unsupported worker"):
        module_command("os")
