"""Exercise production ROI preparation without importing CLR or the LF SDK."""
import ast
import builtins
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
import threading

import numpy as np
import pytest


ROI = "roi"
FULL, LINE = object(), object()


class Clock:
    def __init__(self):
        self.now = 0.
        self.after_sleep = lambda: None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.after_sleep()


class Experiment:
    IsRunning = False
    IsUpdating = False

    def __init__(self):
        self.actual = LINE
        self.available = True
        self.writable = True
        self.supported = [FULL, LINE]
        self.writes = []
        self.ignore_write = False
        self.after_write = lambda: None
        self.read_error = None

    def Exists(self, _setting):
        return True

    def IsAvailable(self, _setting):
        return self.available

    def IsWritable(self, _setting):
        return self.writable

    def GetCurrentCapabilities(self, _setting):
        return self.supported

    def GetValue(self, _setting):
        if self.read_error:
            raise self.read_error
        return self.actual

    def SetValue(self, setting, value):
        self.writes.append((setting, value))
        if not self.ignore_write:
            self.actual = value
        self.after_write()


@pytest.fixture
def sensor():
    # Compile the exact production methods, including the real capture guard
    # decorator. Selecting AST nodes avoids all SDK imports and Automation code.
    source = Path(__file__).resolve().parents[1] / "lf6_automation.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    setup_class = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                       and node.name == "LF6Setup")
    methods = {
        "_flag", "readiness_evidence", "readiness_snapshot", "is_ready", "is_busy",
        "setting_is_available", "setting_is_writable", "ensure_spectrum_sensor_mode",
    }
    selected = ast.ClassDef(name="SensorSetup", bases=[], keywords=[],
                            body=[node for node in setup_class.body
                                  if isinstance(node, ast.FunctionDef) and node.name in methods],
                            decorator_list=[])
    guard = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == "_guard_setup_call")
    clock = Clock()
    capture = SimpleNamespace(check_available=lambda: None)
    namespace = {
        "np": np, "builtins": builtins, "wraps": wraps, "time": clock,
        "get_capture_guard": lambda _setup: capture,
        "CameraSettings": SimpleNamespace(ReadoutControlRegionsOfInterestSelection=ROI,
                                           ShutterTimingExposureTime="exposure"),
        "SpectrometerSettings": SimpleNamespace(GratingCenterWavelength="center"),
        "ExperimentSettings": SimpleNamespace(OnlineProcessingFrameCombinationFramesCombined="frames"),
        "RegionsOfInterestSelection": SimpleNamespace(FullSensor=FULL, LineSensor=LINE),
        "LIGHTFIELD_SETTING_TIMEOUT_S": .5, "LIGHTFIELD_POLL_INTERVAL_S": .01,
        "LightFieldSettingTimeoutError": type("LightFieldSettingTimeoutError", (TimeoutError,), {}),
    }
    module = ast.fix_missing_locations(ast.Module(body=[guard, selected], type_ignores=[]))
    exec(compile(module, str(source), "exec"), namespace)
    setup = namespace["SensorSetup"]()
    setup.application = SimpleNamespace(IsReady=True)
    setup.experiment = Experiment()
    return setup, clock, capture


def test_2d_switches_and_following_1d_restores_line_sensor(sensor):
    setup, clock, _ = sensor
    assert setup.ensure_spectrum_sensor_mode("2d") == {
        "requested": "FullSensor", "actual": "FullSensor", "changed": True}
    assert setup.ensure_spectrum_sensor_mode("1d") == {
        "requested": "LineSensor", "actual": "LineSensor", "changed": True}
    assert setup.experiment.writes == [(ROI, FULL), (ROI, LINE)]
    assert clock.now == 0  # Ready readback needs no per-frame settling delay.


@pytest.mark.parametrize("mode,actual", [("1d", LINE), ("2d", FULL)])
def test_already_correct_readonly_mode_has_no_write_or_wait(sensor, mode, actual):
    setup, clock, _ = sensor
    setup.experiment.actual, setup.experiment.writable = actual, False
    assert setup.ensure_spectrum_sensor_mode(mode)["changed"] is False
    assert setup.experiment.writes == []
    assert clock.now == 0


@pytest.mark.parametrize("failure", ["ignored", "unavailable", "readonly", "busy", "not_ready", "read_error"])
def test_cannot_verify_mode_blocks_with_bounded_actionable_error(sensor, failure):
    setup, clock, _ = sensor
    experiment = setup.experiment
    if failure == "ignored": experiment.ignore_write = True
    if failure == "unavailable": experiment.available = False
    if failure == "readonly": experiment.writable = False
    if failure == "busy": experiment.IsUpdating = True
    if failure == "not_ready": setup.application.IsReady = False
    if failure == "read_error": experiment.read_error = RuntimeError("updating readback")
    with pytest.raises(TimeoutError, match="FullSensor.*Check LightField.*blocked"):
        setup.ensure_spectrum_sensor_mode("2d", timeout_s=.1)
    assert clock.now == pytest.approx(.1)
    assert len(experiment.writes) == (1 if failure == "ignored" else 0)


def test_unknown_capabilities_allow_verified_mode_but_unsupported_mode_blocks(sensor):
    setup, _, _ = sensor
    setup.experiment.supported = None
    assert setup.ensure_spectrum_sensor_mode("2d")["actual"] == "FullSensor"
    setup.experiment.supported = [FULL]
    with pytest.raises(RuntimeError, match="LineSensor.*unsupported.*Check LightField"):
        setup.ensure_spectrum_sensor_mode("1d")
    assert setup.experiment.writes == [(ROI, FULL)]


def test_write_waits_until_ready_and_verifies_delayed_sdk_readback(sensor):
    setup, clock, _ = sensor
    experiment = setup.experiment
    experiment.IsUpdating = True
    experiment.ignore_write = True

    def advance():
        experiment.IsUpdating = False
        if clock.now >= .03:
            experiment.actual = FULL

    clock.after_sleep = advance
    assert setup.ensure_spectrum_sensor_mode("2d")["changed"] is True
    assert experiment.writes == [(ROI, FULL)]
    assert clock.now >= .03


def test_busy_empty_capabilities_are_not_mistaken_for_unsupported_mode(sensor):
    setup, clock, _ = sensor
    setup.experiment.IsUpdating = True
    setup.experiment.supported = []

    def finish_update():
        setup.experiment.IsUpdating = False
        setup.experiment.supported = [FULL, LINE]

    clock.after_sleep = finish_update
    assert setup.ensure_spectrum_sensor_mode("2d")["changed"] is True
    assert setup.experiment.writes == [(ROI, FULL)]


@pytest.mark.parametrize("apply", [True, False])
def test_correct_readonly_mode_does_not_need_current_capabilities(sensor, apply):
    setup, clock, _ = sensor
    setup.experiment.actual = FULL
    setup.experiment.writable = False
    setup.experiment.supported = []
    assert setup.ensure_spectrum_sensor_mode("2d", apply=apply)["changed"] is False
    assert setup.experiment.writes == []
    assert clock.now == 0


def test_changed_mode_can_verify_while_capabilities_become_empty(sensor):
    setup, _, _ = sensor
    setup.experiment.after_write = lambda: setattr(setup.experiment, "supported", [])
    assert setup.ensure_spectrum_sensor_mode("2d")["changed"] is True
    assert setup.experiment.writes == [(ROI, FULL)]


@pytest.mark.parametrize("probe", ["writable", "capabilities"])
def test_slow_sdk_probe_cannot_start_a_write_after_deadline(sensor, probe):
    setup, clock, _ = sensor

    def slow_probe(_setting):
        clock.now = .1
        return True if probe == "writable" else [FULL, LINE]

    if probe == "writable": setup.experiment.IsWritable = slow_probe
    else: setup.experiment.GetCurrentCapabilities = slow_probe
    with pytest.raises(TimeoutError, match="FullSensor.*Check LightField.*blocked"):
        setup.ensure_spectrum_sensor_mode("2d", timeout_s=.1)
    assert setup.experiment.writes == []
    assert setup.experiment.actual is LINE


@pytest.mark.parametrize("phase", ["before", "waiting", "after_write"])
def test_cancel_prevents_further_roi_work(sensor, phase):
    setup, clock, _ = sensor
    stop = threading.Event()
    if phase == "before": stop.set()
    if phase == "waiting":
        setup.experiment.IsUpdating = True
        clock.after_sleep = stop.set
    if phase == "after_write": setup.experiment.after_write = stop.set
    with pytest.raises(RuntimeError, match="FullSensor.*cancelled.*blocked"):
        setup.ensure_spectrum_sensor_mode("2d", stop_event=stop)
    assert len(setup.experiment.writes) == (1 if phase == "after_write" else 0)


def test_readonly_validation_detects_post_capture_drift_without_repair(sensor):
    setup, clock, _ = sensor
    setup.ensure_spectrum_sensor_mode("2d")
    setup.experiment.actual = LINE
    with pytest.raises(RuntimeError, match="FullSensor.*mismatch.*Check LightField"):
        setup.ensure_spectrum_sensor_mode("2d", apply=False)
    assert setup.experiment.writes == [(ROI, FULL)]
    assert clock.now == 0


def test_capture_guard_reservation_prevents_mode_access(sensor):
    setup, _, capture = sensor

    def reserved():
        raise RuntimeError("capture reserved")

    capture.check_available = reserved
    with pytest.raises(RuntimeError, match="capture reserved"):
        setup.ensure_spectrum_sensor_mode("2d")
    assert setup.experiment.writes == []


@pytest.mark.parametrize("failure", ["capabilities", "write"])
def test_sdk_error_keeps_desired_mode_and_guidance_in_error(sensor, failure):
    setup, _, _ = sensor

    def fail(*_args):
        raise RuntimeError("SDK failed")

    if failure == "capabilities": setup.experiment.GetCurrentCapabilities = fail
    else: setup.experiment.SetValue = fail
    with pytest.raises(RuntimeError, match="FullSensor.*SDK failed.*Check LightField.*blocked"):
        setup.ensure_spectrum_sensor_mode("2d")


def test_successful_readonly_post_capture_verification_is_not_a_change(sensor):
    setup, clock, _ = sensor
    setup.experiment.actual = FULL
    assert setup.ensure_spectrum_sensor_mode("2d", apply=False) == {
        "requested": "FullSensor", "actual": "FullSensor", "changed": False}
    assert setup.experiment.writes == []
    assert clock.now == 0


@pytest.mark.parametrize("kwargs", [{"mode": "3d"}, {"mode": "2d", "timeout_s": 0},
                                   {"mode": "2d", "poll_interval_s": 0},
                                   {"mode": "2d", "timeout_s": float("nan")},
                                   {"mode": "2d", "timeout_s": float("inf")}])
def test_bad_request_has_no_sdk_writes(sensor, kwargs):
    setup, _, _ = sensor
    with pytest.raises(ValueError):
        setup.ensure_spectrum_sensor_mode(**kwargs)
    assert setup.experiment.writes == []
