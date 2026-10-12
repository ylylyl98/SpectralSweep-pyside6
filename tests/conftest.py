"""Keep collection-time services and every test away from real user history."""
import os
from pathlib import Path
import tempfile

import pytest


def pytest_configure(config):
    # Collection and unittest setUpClass can create services before fixtures run.
    config._history_previous_path = os.environ.get('SPECTRALSWEEP_HISTORY_PATH')
    config._history_collection_dir = tempfile.TemporaryDirectory(
        prefix='spectralsweep-pytest-history-', ignore_cleanup_errors=True)
    os.environ['SPECTRALSWEEP_HISTORY_PATH'] = str(
        Path(config._history_collection_dir.name) / 'collection.sqlite')
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    # Individual tests may override this with their own explicit database.
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH', str(tmp_path / 'history.sqlite'))


def pytest_unconfigure(config):
    directory = getattr(config, '_history_collection_dir', None)
    if directory is None:
        return
    previous = config._history_previous_path
    if previous is None:
        os.environ.pop('SPECTRALSWEEP_HISTORY_PATH', None)
    else:
        os.environ['SPECTRALSWEEP_HISTORY_PATH'] = previous
    directory.cleanup()
