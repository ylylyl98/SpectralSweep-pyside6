import hashlib
import json
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from app.update_install import InstallHandoff


class FakeProcess:
    def poll(self):
        return None


def setup_handoff(tmp_path, monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'cache'))
    monkeypatch.setenv('WINDIR', str(tmp_path / 'Windows'))
    calls = []
    monkeypatch.setattr('app.update_install.subprocess.Popen', lambda *a, **kw: calls.append((a, kw)) or FakeProcess())
    install = tmp_path / 'program files' / 'SpectralSweep'
    install.mkdir(parents=True)
    (install / 'SpectralSweep.exe').write_bytes(b'app')
    installer = tmp_path / 'update setup.exe'
    installer.write_bytes(b'setup')
    return app, InstallHandoff(), installer, install, calls


def test_ready_arm_and_shutdown_preserve_helper(tmp_path, monkeypatch):
    app, handoff, installer, install, calls = setup_handoff(tmp_path, monkeypatch)
    ready = []
    handoff.ready.connect(lambda: ready.append(True))
    assert handoff.prepare(installer, hashlib.sha256(b'setup').hexdigest(), install, ['--safe'])
    directory = handoff._directory
    request = json.loads((directory / 'request.json').read_text(encoding='utf-8'))
    assert request['restart_args'] == ['--safe']
    assert calls[0][1]['close_fds'] is True
    assert str(install) not in str(directory)
    (directory / 'ready').write_text(request['token'], encoding='utf-8-sig')
    handoff._poll()
    assert ready == [True]
    assert handoff.arm()
    handoff.shutdown()
    assert (directory / 'arm').read_text() == request['token']
    assert not (directory / 'cancel').exists()


def test_close_veto_cancels(tmp_path, monkeypatch):
    app, handoff, installer, install, _ = setup_handoff(tmp_path, monkeypatch)
    assert handoff.prepare(installer, hashlib.sha256(b'setup').hexdigest(), install)
    directory = handoff._directory
    (directory / 'ready').write_text(handoff._token)
    handoff._poll()
    handoff.cancel()
    assert (directory / 'cancel').read_text()
    assert not handoff.arm()
    assert handoff.prepare(installer, hashlib.sha256(b'setup').hexdigest(), install)


def test_bad_digest_format_and_bad_ready_fail_closed(tmp_path, monkeypatch):
    app, handoff, installer, install, calls = setup_handoff(tmp_path, monkeypatch)
    failures = []
    handoff.failed.connect(failures.append)
    assert not handoff.prepare(installer, 'bad', install)
    assert not calls
    assert failures
    assert handoff.prepare(installer, hashlib.sha256(b'setup').hexdigest(), install)
    directory = handoff._directory
    (directory / 'ready').write_text('wrong')
    handoff._deadline = 0
    handoff._poll()
    assert (directory / 'cancel').exists()
    assert not handoff.arm()


def test_dead_helper_cannot_arm(tmp_path, monkeypatch):
    app, handoff, installer, install, calls = setup_handoff(tmp_path, monkeypatch)
    assert handoff.prepare(installer, hashlib.sha256(b'setup').hexdigest(), install)
    directory = handoff._directory
    (directory / 'ready').write_text(handoff._token)
    handoff._poll()
    monkeypatch.setattr(handoff._process, 'poll', lambda: 1)
    assert not handoff.arm()
    assert (directory / 'cancel').exists()


import os
import subprocess

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell required')
@pytest.mark.parametrize('scenario,expected,launches', [
    ('cancel', 'cancelled', 0),
    ('initial_bad_hash', 'failed', 0),
    ('tamper', 'failed', 0),
    ('setup_failure', 'failed', 1),
    ('success', 'success', 2),
])
def test_powershell_behavior_without_real_processes(tmp_path, scenario, expected, launches):
    work = tmp_path / '测试 space'
    work.mkdir()
    install = work / 'program files'
    install.mkdir()
    (install / 'SpectralSweep.exe').write_bytes(b'app')
    setup = work / 'setup file.exe'
    setup.write_bytes(b'good installer')
    token = 'test-token'
    digest = hashlib.sha256(b'good installer').hexdigest()
    if scenario == 'initial_bad_hash':
        digest = '0' * 64
    request = work / 'request.json'
    request.write_text(json.dumps({'parent_pid': 42, 'setup_file': str(setup),
        'install_dir': str(install), 'sha256': digest, 'token': token,
        'restart_args': ['--name', '测试 value', 'quote"x', 'tail\\']}, ensure_ascii=False), encoding='utf-8')
    (work / ('cancel' if scenario == 'cancel' else 'arm')).write_text(token)
    driver = work / 'driver.ps1'
    driver.write_text(r'''
param($Script, $Request, $Scenario, $Calls)
Import-Module Microsoft.PowerShell.Utility
$global:scenario = $Scenario
$global:calls = $Calls
$global:tampered = $false
function Get-Process {
    param($Id, $Name)
    if ($global:scenario -eq 'tamper' -and !$global:tampered) {
        $r = Get-Content -LiteralPath $Request -Raw -Encoding UTF8 | ConvertFrom-Json
        [IO.File]::WriteAllText($r.setup_file, 'tampered')
        $global:tampered = $true
    }
}
function Start-Process {
    param($FilePath, $ArgumentList, [switch]$Wait, [switch]$PassThru)
    @{file=$FilePath; args=@($ArgumentList)} | ConvertTo-Json -Compress | Add-Content -LiteralPath $global:calls -Encoding UTF8
    if ($PassThru) { return [pscustomobject]@{ExitCode = $(if ($global:scenario -eq 'setup_failure') { 7 } else { 0 })} }
}
function Add-Type { throw 'No UI in tests' }
& $Script -Request $Request
''', encoding='utf-8-sig')
    script = Path(__file__).resolve().parents[1] / 'app' / 'update_install.ps1'
    calls = work / 'calls.jsonl'
    ps = Path(os.environ['WINDIR']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    subprocess.run([str(ps), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                    '-File', str(driver), str(script), str(request), scenario, str(calls)],
                   timeout=20, capture_output=True, check=False)
    result = json.loads((work / 'result.json').read_text(encoding='utf-8-sig'))
    assert result['status'] == expected
    records = [json.loads(line) for line in calls.read_text(encoding='utf-8-sig').splitlines()] if calls.exists() else []
    assert len(records) == launches
    if scenario == 'success':
        assert records[0]['file'] == str(setup)
        assert '/DIR="' + str(install) + '"' in records[0]['args']
        assert records[1]['file'] == str(install / 'SpectralSweep.exe')
        assert '"测试 value"' in records[1]['args']
        assert '"quote\\"x"' in records[1]['args']
        assert '"tail\\\\"' in records[1]['args']

import time


def test_partial_result_is_retried_then_reported(tmp_path, monkeypatch):
    app, handoff, installer, install, calls = setup_handoff(tmp_path, monkeypatch)
    failures = []
    handoff.failed.connect(failures.append)
    assert handoff.prepare(installer, hashlib.sha256(b'setup').hexdigest(), install)
    result = handoff._directory / 'result.json'
    result.write_text('{')
    handoff._poll()
    assert not failures
    result.write_text(json.dumps({'status': 'failed', 'detail': 'Bad digest'}))
    handoff._poll()
    assert failures == ['Bad digest']


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell required')
def test_real_powershell_verifies_and_cancels_without_modules(tmp_path):
    work = tmp_path / '测试 cache'
    work.mkdir()
    setup = work / 'setup.exe'
    setup.write_bytes(b'installer bytes')
    request = work / 'request.json'
    request.write_text(json.dumps({'parent_pid': os.getpid(), 'setup_file': str(setup),
        'install_dir': str(work), 'sha256': hashlib.sha256(b'installer bytes').hexdigest(),
        'restart_args': [], 'token': 'test-token'}, ensure_ascii=False), encoding='utf-8')
    ps = Path(os.environ['WINDIR']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    script = Path(__file__).resolve().parents[1] / 'app' / 'update_install.ps1'
    env = dict(os.environ, PSModulePath=str(work / 'no-modules'))
    process = subprocess.Popen([str(ps), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                '-File', str(script), '-Request', str(request)], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while not (work / 'ready').exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        assert (work / 'ready').exists(), process.communicate(timeout=2)
        (work / 'cancel').write_text('test-token')
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, (stdout, stderr)
        assert json.loads((work / 'result.json').read_text(encoding='utf-8-sig'))['status'] == 'cancelled'
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)

@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell required')
def test_real_powershell_arm_runs_simulated_setup_and_restart(tmp_path):
    """Exercise real process cmdlets and Windows argument parsing without touching the app."""
    work = tmp_path / '更新 space'
    work.mkdir()
    install = work / 'install space'
    install.mkdir()
    source = work / 'logger.cs'
    source.write_text('''using System; using System.IO;
class Program { static void Main(string[] args) {
  File.WriteAllLines(Environment.GetEnvironmentVariable("SS_TEST_LOG"), args);
} }''', encoding='utf-8')
    csc = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    if not csc.is_file():
        pytest.skip('Windows .NET Framework compiler unavailable')
    setup = work / 'setup space.exe'
    target = install / 'SpectralSweep.exe'
    subprocess.run([str(csc), '/nologo', '/target:exe', f'/out:{setup}', str(source)],
                   check=True, capture_output=True, timeout=30)
    import shutil
    shutil.copyfile(setup, target)
    setup_log = work / 'setup args.txt'
    restart_log = work / 'restart args.txt'
    # Separate executables need distinct output destinations without external processes.
    restart_source = work / 'restart.cs'
    restart_source.write_text('''using System; using System.IO;
class Program { static void Main(string[] args) {
  File.WriteAllLines(Environment.GetEnvironmentVariable("SS_RESTART_LOG"), args);
} }''', encoding='utf-8')
    subprocess.run([str(csc), '/nologo', '/target:exe', f'/out:{target}', str(restart_source)],
                   check=True, capture_output=True, timeout=30)
    production = Path(__file__).resolve().parents[1] / 'app' / 'update_install.ps1'
    script = work / 'helper.ps1'
    contents = production.read_text(encoding='utf-8-sig')
    assert "'SpectralSweep','SpectralSweep-worker'" in contents
    script.write_text(contents.replace("'SpectralSweep','SpectralSweep-worker'", "'SS-Test-No-Match-A','SS-Test-No-Match-B'"), encoding='utf-8-sig')
    token = 'test-token'
    request = work / 'request.json'
    request.write_text(json.dumps({'parent_pid': 2147483647, 'setup_file': str(setup),
        'install_dir': str(install), 'sha256': hashlib.sha256(setup.read_bytes()).hexdigest(),
        'restart_args': ['--name', '中文 value', 'quote"x', 'tail\\'], 'token': token},
        ensure_ascii=False), encoding='utf-8')
    ps = Path(os.environ['WINDIR']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    env = dict(os.environ, SS_TEST_LOG=str(setup_log), SS_RESTART_LOG=str(restart_log))
    process = subprocess.Popen([str(ps), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                '-File', str(script), '-Request', str(request)], cwd=work, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        deadline = time.monotonic() + 15
        while not (work / 'ready').exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        if not (work / 'ready').exists():
            assert False, process.communicate(timeout=2)
        (work / 'arm').write_text(token)
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, (stdout, stderr, (work / 'result.json').read_text(encoding='utf-8-sig'))
        deadline = time.monotonic() + 5
        while not restart_log.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert json.loads((work / 'result.json').read_text(encoding='utf-8-sig'))['status'] == 'success'
        assert setup_log.read_text(encoding='utf-8-sig').splitlines() == [
            '/SILENT', '/SP-', '/NORESTART', '/NOCLOSEAPPLICATIONS', '/NORESTARTAPPLICATIONS',
            '/DIR=' + str(install), '/LOG=' + str(work / 'install.log')]
        assert restart_log.read_text(encoding='utf-8-sig').splitlines() == ['--name', '中文 value', 'quote"x', 'tail\\']
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
