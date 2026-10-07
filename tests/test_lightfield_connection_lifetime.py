import ast
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from controllers.lf6_controller import _LF6Worker


def test_empty_experiment_connects_once_without_acquisition_readiness(monkeypatch):
    setup = SimpleNamespace(is_ready=False, is_busy=False,
                            get_saved_experiments=lambda: ['saved'], close=Mock())
    factory = Mock(return_value=setup)
    monkeypatch.setitem(sys.modules, 'lf6_automation', SimpleNamespace(LF6Setup=factory))
    monkeypatch.setitem(sys.modules, 'app.devices.lf6_adapter', SimpleNamespace(SpectrometerLF6=Mock()))
    worker = _LF6Worker()
    worker.wait_until_ready = Mock(side_effect=TimeoutError('no loaded experiment'))
    worker._route_selected_detector = Mock(return_value=None)
    connected, errors = [], []
    worker.connected.connect(connected.append)
    worker.error.connect(errors.append)
    worker.connect_instrument(False, 'lightfield')
    worker.connect_instrument(False, 'lightfield')
    assert factory.call_count == 1
    assert not errors
    assert connected == [['saved'], ['saved']]
    assert worker._setup is setup
    assert not worker.is_ready
    worker.wait_until_ready.assert_not_called()


def load_setup_class(automation):
    tree = ast.parse(Path('lf6_automation.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'LF6Setup')
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in ('__init__', 'close')]
    ns = dict(Automation=automation, List={str: list}, String=str,
              ExperimentSettings=object(), SpectrometerSettings=object())
    exec(compile(ast.Module(body=[cls], type_ignores=[]), '<LF6 lifetime>', 'exec'), ns)
    return ns['LF6Setup']


def test_owned_automation_disposed_once_on_disconnect():
    auto = SimpleNamespace(LightFieldApplication=SimpleNamespace(Experiment=object()), Dispose=Mock())
    setup = load_setup_class(lambda *a: auto)()
    setup.close()
    setup.close()
    auto.Dispose.assert_called_once()


def test_constructor_failure_disposes_created_automation():
    class Broken:
        Dispose = Mock()
        @property
        def LightFieldApplication(self):
            raise RuntimeError('SDK initialization failed')
    auto = Broken()
    import pytest
    with pytest.raises(RuntimeError, match='initialization'):
        load_setup_class(lambda *a: auto)()
    auto.Dispose.assert_called_once()
