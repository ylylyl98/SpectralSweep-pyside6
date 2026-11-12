"""Exercise real Windows shortcuts with an inert launcher, never the hardware app."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcuts")
ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) /
                 "System32/WindowsPowerShell/v1.0/powershell.exe")


def powershell(*args, env=None):
    return subprocess.run(
        [POWERSHELL, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=40, env=env,
    )


@pytest.mark.parametrize("update_code", [0, 1])
def test_shortcut_updates_then_launches_same_folder_even_when_update_skipped(tmp_path, update_code):
    project = tmp_path / "source 空格 & folder"
    tools = project / "tools"
    tools.mkdir(parents=True)
    icons = project / "assets/icons"
    icons.mkdir(parents=True)
    shutil.copyfile(ROOT / "assets/icons/spectralsweep.ico", icons / "spectralsweep.ico")
    for name in ("create_source_shortcut.ps1", "start_source.ps1", "windows_shortcut.cs"):
        shutil.copyfile(ROOT / "tools" / name, tools / name)
    # A deterministic, inert update step followed by a stub app. The marker proves order.
    (tools / "update_source.ps1").write_text(
        "[IO.File]::WriteAllText((Join-Path (Split-Path -Parent $PSScriptRoot) 'updated.txt'), 'checked')\n"
        f"exit {update_code}\n", encoding="ascii",
    )
    (project / "launch.bat").write_text(
        "@echo off\nif not exist updated.txt exit /b 9\necho started>started.txt\nexit /b 0\n",
        encoding="ascii",
    )
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    # Idempotent recreation, without writing to the user's real Desktop.
    for _ in range(2):
        result = powershell("-File", str(tools / "create_source_shortcut.ps1"),
                            "-DestinationDirectory", str(desktop))
        assert result.returncode == 0, result.stdout + result.stderr
    shortcut = desktop / "SpectralSweep (Source).lnk"
    # The real creator is already exercised above. Stub automatic recreation so
    # executing the shortcut cannot modify the user's Desktop.
    (tools / "create_source_shortcut.ps1").write_text("exit 0\n", encoding="ascii")
    env = dict(os.environ, SPECTRAL_TEST_SHORTCUT=str(shortcut))
    result = powershell("-Command", """[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($env:SPECTRAL_TEST_SHORTCUT)
@{Target=$link.TargetPath} | ConvertTo-Json -Compress
exit $shell.Run(('"' + $env:SPECTRAL_TEST_SHORTCUT + '"'), 0, $true)
""", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    metadata = json.loads(result.stdout.strip())
    assert Path(metadata["Target"]) == Path(POWERSHELL)
    # WScript's property reader is ANSI too. Verify the persisted Unicode values
    # directly, as well as executing the actual .lnk through Windows above.
    saved = shortcut.read_bytes()
    for value in (f'-File "{tools / "start_source.ps1"}"', str(project), str(icons / "spectralsweep.ico")):
        assert value.encode("utf-16-le") in saved
    assert (project / "started.txt").read_text().strip() == "started"
