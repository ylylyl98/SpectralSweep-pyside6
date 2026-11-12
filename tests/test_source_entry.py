import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows batch entry point')


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'source 空格 & spaces'
    tools = root / 'tools'
    tools.mkdir(parents=True)
    shutil.copy2(ROOT / 'launch.bat', root / 'launch.bat')
    shutil.copy2(ROOT / 'tools' / 'start_source.ps1', tools / 'start_source.ps1')
    for name, event in [('create_source_shortcut.ps1', 'create'), ('update_source.ps1', 'update')]:
        (tools / name).write_text(f"Add-Content -LiteralPath (Join-Path $PSScriptRoot 'events.txt') -Value '{event}'\nexit 0\n", encoding='ascii')
    (tools / 'launch_app.py').write_text(
        "import json, pathlib, sys\n"
        "p = pathlib.Path(__file__).parent\n"
        "with (p / 'events.txt').open('a') as f: f.write('launch\\n')\n"
        "(p / 'args.json').write_text(json.dumps(sys.argv[1:]))\n", encoding='utf-8')
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(root / '.venv-pyside6-313')], check=True, capture_output=True)
    return root


def run_entry(root, args):
    env = os.environ.copy()
    env.pop('SPECTRAL_SOURCE_STARTING', None)
    # cmd /s /c needs an outer quote pair as well as the batch path quotes.
    # A normal argv list uses C quoting and loses protection around '&' here.
    command = f'cmd.exe /d /s /c ""{root / "launch.bat"}" {subprocess.list2cmdline(args)}"'
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, errors='replace', timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    events = (root / 'tools' / 'events.txt').read_text().splitlines()
    forwarded = json.loads((root / 'tools' / 'args.json').read_text())
    return events, forwarded


def test_normal_launch_orders_callbacks_and_forwards_arguments(source):
    events, args = run_entry(source, ['--mock', '--watch-styles', '--ui', 'widgets'])
    assert events == ['create', 'update', 'launch']
    assert sorted(args) == sorted(['--mock', '--watch-styles', '--ui', 'widgets'])


@pytest.mark.parametrize('argument', ['--install', '--preview-workspaces'])
def test_special_commands_skip_create_and_update(source, argument):
    events, args = run_entry(source, [argument])
    assert events == ['launch']
    assert args == [argument]


def test_update_failure_still_launches(source):
    (source / 'tools' / 'update_source.ps1').write_text(
        "Add-Content -LiteralPath (Join-Path $PSScriptRoot 'events.txt') -Value 'update'\nexit 1\n", encoding='ascii')
    events, args = run_entry(source, [])
    assert events == ['create', 'update', 'launch']
    assert args == []
