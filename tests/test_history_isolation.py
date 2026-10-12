"""Regression tests must never append simulated runs to the user's history."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

from app.experiment_metadata import ExperimentMetadataService


def test_pytest_isolates_collection_and_each_test_from_existing_history(tmp_path):
    existing = tmp_path / 'existing-history.sqlite'
    service = ExperimentMetadataService(tmp_path / 'existing-output', existing)
    saved = service.begin('test', 'user-sample')
    saved.complete()
    sandbox = tmp_path / 'suite'
    sandbox.mkdir()
    conftest = Path(__file__).with_name('conftest.py')
    if conftest.exists():
        shutil.copyfile(conftest, sandbox / 'conftest.py')
    (sandbox / 'test_inner.py').write_text('''
from pathlib import Path
from app.experiment_metadata import ExperimentMetadataService, ExperimentHistory

root = Path(__file__).parent
collection = ExperimentMetadataService(root / 'collection').begin('test', 'collection')
collection.complete()

def test_first():
    run = ExperimentMetadataService(root / 'first').begin('test', 'test-sample')
    run.complete()
    assert len(ExperimentHistory().query('test-sample', 'test')) == 1

def test_second():
    assert ExperimentHistory().query('test-sample', 'test') == []
''', encoding='utf-8')
    environment = dict(os.environ)
    environment['SPECTRALSWEEP_HISTORY_PATH'] = str(existing)
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
    environment['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', str(sandbox)],
        cwd=sandbox, env=environment, capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert service.history.query('collection', 'test') == []
    assert service.history.query('test-sample', 'test') == []
    assert [row['experiment_id'] for row in service.history.query('user-sample', 'test')] == [saved.experiment_id]
