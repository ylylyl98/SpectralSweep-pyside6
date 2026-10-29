"""Test sensor readiness using production methods without importing the SDK."""
import ast
import builtins
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

import pytest


ROI_SELECTION = "sensor_roi_selection"


@pytest.fixture
def sensor():
    source = Path(__file__).resolve().parents[1] / "lf6_automation.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    setup_class = next(node for node in tree.body
                       if isinstance(node, ast.ClassDef) and node.name == "LF6Setup")
    names = {"_flag", "readiness_evidence", "is_busy", "sensor_readout_ready"}
    selected = ast.ClassDef(
        name="SensorSetup", bases=[], keywords=[], decorator_list=[],
        body=[node for node in setup_class.body
              if isinstance(node, ast.FunctionDef) and node.name in names],
    )
    guard = next(node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == "_guard_setup_call")
    capture = SimpleNamespace(check_available=lambda: None)
    settings = SimpleNamespace(ReadoutControlRegionsOfInterestSelection=ROI_SELECTION)
    namespace = {
        "builtins": builtins, "wraps": wraps, "np": SimpleNamespace(bool_=bool),
        "CameraSettings": settings, "get_capture_guard": lambda _setup: capture,
    }
    module = ast.fix_missing_locations(ast.Module(body=[guard, selected], type_ignores=[]))
    exec(compile(module, str(source), "exec"), namespace)
    setup = namespace["SensorSetup"]()
    calls = []

    def exists(setting):
        calls.append(setting)
        assert setting == ROI_SELECTION, "Sensor readiness probed an unrelated setting"
        return True

    def saved_experiments():
        raise AssertionError("Sensor readiness queried saved experiments")

    setup.application = SimpleNamespace()
    setup.experiment = SimpleNamespace(Exists=exists, GetSavedExperiments=saved_experiments)
    return setup, calls, capture, settings


def test_sensor_ready_needs_only_roi_capability_when_explicit_flags_are_absent(sensor):
    setup, calls, _, _ = sensor
    assert setup.sensor_readout_ready is True
    assert calls == [ROI_SELECTION]


@pytest.mark.parametrize("owner, flag", [
    ("application", "IsReady"), ("application", "IsInitialized"),
    ("experiment", "IsReady"), ("experiment", "IsLoaded"),
])
def test_explicit_not_ready_blocks_sensor_access(sensor, owner, flag):
    setup, calls, _, _ = sensor
    setattr(getattr(setup, owner), flag, False)
    assert setup.sensor_readout_ready is False
    assert calls == []


@pytest.mark.parametrize("owner", ["application", "experiment"])
@pytest.mark.parametrize("flag", [
    "IsRunning", "IsUpdating", "IsBusy", "Busy", "IsAcquiring", "Acquiring",
    "IsLoading", "Loading",
])
def test_busy_acquiring_or_loading_still_blocks_sensor_access(sensor, owner, flag):
    setup, calls, _, _ = sensor
    setattr(getattr(setup, owner), flag, True)
    assert setup.sensor_readout_ready is False
    assert calls == []


@pytest.mark.parametrize("owner", ["application", "experiment"])
def test_missing_live_owner_blocks_sensor_access(sensor, owner):
    setup, calls, _, _ = sensor
    setattr(setup, owner, None)
    assert setup.sensor_readout_ready is False
    assert calls == []


def test_missing_roi_capability_blocks_sensor_access(sensor):
    setup, _, _, _ = sensor
    setup.experiment.Exists = lambda _setting: False
    assert setup.sensor_readout_ready is False


def test_missing_sdk_roi_key_blocks_sensor_access(sensor):
    setup, calls, _, settings = sensor
    del settings.ReadoutControlRegionsOfInterestSelection
    assert setup.sensor_readout_ready is False
    assert calls == []


def test_failed_roi_probe_blocks_sensor_access(sensor):
    setup, _, _, _ = sensor

    def disconnected(_setting):
        raise RuntimeError("Experiment disconnected")

    setup.experiment.Exists = disconnected
    assert setup.sensor_readout_ready is False


def test_quarantined_capture_blocks_sensor_readiness_probe(sensor):
    setup, calls, capture, _ = sensor

    def blocked():
        raise RuntimeError("Capture quarantined")

    capture.check_available = blocked
    with pytest.raises(RuntimeError, match="Capture quarantined"):
        _ = setup.sensor_readout_ready
    assert calls == []
