import os
from pathlib import Path
import shutil
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'tools' / 'update_source.ps1'
pytestmark = pytest.mark.skipif(os.name != 'nt' or not shutil.which('git') or not shutil.which('powershell'), reason='Windows Git and PowerShell required')


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


def update(path):
    return subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(SCRIPT), '-ProjectRoot', str(path)], capture_output=True, text=True, timeout=45)


@pytest.fixture
def repos(tmp_path):
    remote, local, peer = (tmp_path / name for name in ('remote.git', 'local', 'peer'))
    subprocess.run(['git', 'init', '--bare', '--initial-branch=main', str(remote)], check=True, capture_output=True)
    subprocess.run(['git', 'clone', str(remote), str(local)], check=True, capture_output=True)
    for repo in (local,):
        git(repo, 'config', 'user.name', 'Test')
        git(repo, 'config', 'user.email', 'test@example.invalid')
    (local / 'data').write_text('initial')
    git(local, 'add', 'data')
    git(local, 'commit', '-m', 'initial')
    git(local, 'remote', 'rename', 'origin', 'custom')
    git(local, 'push', '-u', 'custom', 'main')
    subprocess.run(['git', 'clone', str(remote), str(peer)], check=True, capture_output=True)
    git(peer, 'config', 'user.name', 'Test')
    git(peer, 'config', 'user.email', 'test@example.invalid')
    return local, peer


def commit(repo, content):
    (repo / 'data').write_text(content)
    git(repo, 'commit', '-am', content)


def test_custom_remote_fast_forward_and_current(repos):
    local, peer = repos
    commit(peer, 'remote')
    git(peer, 'push')
    assert update(local).returncode == 0
    assert (local / 'data').read_text() == 'remote'
    assert update(local).returncode == 0


@pytest.mark.parametrize('kind', ['unstaged', 'staged', 'untracked', 'branch', 'upstream', 'ahead', 'diverged'])
def test_refuses_unsafe_states(repos, kind):
    local, peer = repos
    if kind in ('unstaged', 'staged'):
        (local / 'data').write_text('local change')
        if kind == 'staged':
            git(local, 'add', 'data')
    elif kind == 'untracked':
        (local / 'new').write_text('local change')
    elif kind == 'branch':
        git(local, 'checkout', '-b', 'other')
    elif kind == 'upstream':
        git(local, 'config', 'branch.main.merge', 'refs/heads/other')
    else:
        commit(local, 'local')
        if kind == 'diverged':
            commit(peer, 'remote')
            git(peer, 'push')
    before = git(local, 'rev-parse', 'HEAD')
    status = git(local, 'status', '--porcelain')
    assert update(local).returncode != 0
    assert git(local, 'rev-parse', 'HEAD') == before
    assert git(local, 'status', '--porcelain') == status


def test_non_repository_rejected(tmp_path):
    assert update(tmp_path).returncode != 0


def test_fetch_failure_preserves_local_state(repos, tmp_path):
    local, _ = repos
    git(local, 'remote', 'set-url', 'custom', str(tmp_path / 'missing-remote.git'))
    before = git(local, 'rev-parse', 'HEAD')
    result = update(local)
    assert result.returncode != 0
    assert 'Fetch failed' in result.stdout
    assert git(local, 'rev-parse', 'HEAD') == before
    assert git(local, 'status', '--porcelain') == ''
