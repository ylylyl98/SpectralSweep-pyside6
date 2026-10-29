"""Shared physical ROI policy with fake SDK values; no CLR/Automation imports."""
import ast
import builtins
import copy
from functools import wraps
import json
import math
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from app.devices.lightfield_sensor import (
    prepare_sensor_readout, read_sensor_snapshot, sensor_fingerprint, verify_sensor_readout,
)


SELECTION = "ReadoutControlRegionsOfInterestSelection"
RESULT = "ReadoutControlRegionsOfInterestResult"
FULL_RESULT = "ReadoutControlRegionsOfInterestFullSensor"
ROW_COUNT = "ReadoutControlRegionsOfInterestLineSensorRowBinning"
PROVIDER = "ReadoutControlRegionsOfInterestBinningProvider"
WIDTH, HEIGHT = "SensorInformationActiveAreaWidth", "SensorInformationActiveAreaHeight"
SETTINGS = SimpleNamespace(**{key: key for key in (SELECTION, RESULT, FULL_RESULT, ROW_COUNT, PROVIDER, WIDTH, HEIGHT)})
SELECTIONS = SimpleNamespace(LineSensor="LineSensor", FullSensor="FullSensor")


class Clock:
    def __init__(self):
        self.now = 0.
        self.after_sleep = lambda: None

    def monotonic(self): return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.after_sleep()


def region(**changes):
    return dict(dict(x=0, y=0, width=1340, height=100, x_binning=1, y_binning=1), **changes)


class Experiment:
    Name = "Test LF experiment"

    def __init__(self):
        self.selection = "FullSensor"
        self.row_count = 10
        self.provider = "Hardware"
        self.full_regions = [region()]
        self.custom = [region(y=45, height=10, y_binning=10)]
        self.writes = []
        self.missing, self.readonly = set(), set()
        self.supported = ["LineSensor", "FullSensor", "CustomRegions"]
        self.ignore = set()
        self.after_write = lambda: None

    def Exists(self, key): return key not in self.missing

    @property
    def SelectedRegions(self):
        if self.selection == "CustomRegions": return copy.deepcopy(self.custom)
        if self.selection == "LineSensor":
            return [region(y=(100 - self.row_count) // 2, height=self.row_count, y_binning=self.row_count)]
        return copy.deepcopy(self.full_regions)

    def GetValue(self, key):
        return {SELECTION: self.selection, RESULT: self.SelectedRegions,
                FULL_RESULT: self.full_regions, ROW_COUNT: self.row_count,
                PROVIDER: self.provider, WIDTH: 1340, HEIGHT: 100}[key]

    def GetCurrentCapabilities(self, key): return self.supported

    def IsValid(self, key, value):
        return key != ROW_COUNT or 1 <= value <= 100

    def SetValue(self, key, value):
        self.writes.append((key, value))
        if key not in self.ignore:
            if key == SELECTION: self.selection = value
            elif key == ROW_COUNT: self.row_count = value
            else: raise AssertionError(f"Unexpected sensor mutation: {key}")
        self.after_write()


@pytest.fixture
def sensor():
    experiment, clock = Experiment(), Clock()
    setup = SimpleNamespace(experiment=experiment, is_ready=True, is_busy=False,
                            setting_is_available=lambda key: key not in experiment.missing,
                            setting_is_writable=lambda key: key not in experiment.readonly)
    return setup, clock


def snap(setup): return read_sensor_snapshot(setup, SETTINGS)


def prepare(sensor, intent, **kwargs):
    setup, clock = sensor
    return prepare_sensor_readout(setup, intent, SETTINGS, SELECTIONS, clock=clock,
                                  timeout_s=.1, poll_interval_s=.01, **kwargs)


def test_snapshot_is_json_safe_readonly_and_changes_fingerprint_for_geometry(sensor):
    setup, _ = sensor
    actual = snap(setup)
    assert set(actual) == {"selection", "regions", "full_regions", "sensor_width", "sensor_height",
                           "provider", "experiment", "fingerprint", "row_binning",
                           "acquisition_orientation", "calibration_orientation"}
    assert json.loads(json.dumps(actual)) == actual
    assert setup.experiment.writes == []
    setup.experiment.provider = "Software"
    assert snap(setup)["fingerprint"] != actual["fingerprint"]


def test_snapshot_uses_selected_regions_on_versions_without_result_setting(sensor):
    setup, _ = sensor
    setup.experiment.missing.add(RESULT)
    assert snap(setup)["regions"] == [region()]


def test_optional_orientation_readback_is_fingerprinted_without_mutation(sensor):
    setup, _ = sensor
    settings = SimpleNamespace(AcquisitionOrientationResult='acquisition_orientation',
                               WavelengthCalibrationOrientation='calibration_orientation')
    original = setup.experiment.GetValue
    values = dict(acquisition_orientation='Normal', calibration_orientation='Normal')
    setup.experiment.GetValue = lambda key: values[key] if key in values else original(key)
    first = read_sensor_snapshot(setup, SETTINGS, experiment_settings=settings)
    assert first['acquisition_orientation'] == first['calibration_orientation'] == 'Normal'
    values['calibration_orientation'] = 'FlippedHorizontally'
    second = read_sensor_snapshot(setup, SETTINGS, experiment_settings=settings)
    assert second['fingerprint'] != first['fingerprint']
    assert not setup.experiment.writes


def test_bin_all_sets_line_selection_and_full_height_without_touching_provider(sensor):
    setup, _ = sensor
    setup.experiment.provider = "Software"
    actual = prepare(sensor, {"mode": "bin_all"})
    assert actual["selection"] == "LineSensor"
    assert actual["row_binning"] == actual["sensor_height"] == 100
    assert actual["regions"] == [region(y_binning=100)]
    assert actual["provider"] == "Software" and actual["changed"] is True
    assert setup.experiment.writes == [(SELECTION, "LineSensor"), (ROW_COUNT, 100)]


def test_center_n_line_sensor_is_not_mistaken_for_bin_all(sensor):
    setup, _ = sensor
    setup.experiment.selection = "LineSensor"
    with pytest.raises(RuntimeError, match="entire physical active area"):
        prepare(sensor, {"mode": "bin_all"}, apply=False)
    result = prepare(sensor, {"mode": "bin_all"})
    assert result["changed"] and setup.experiment.writes == [(ROW_COUNT, 100)]


def test_verified_bin_all_succeeds_readonly_without_writes_or_wait(sensor):
    setup, clock = sensor
    setup.experiment.selection, setup.experiment.row_count = "LineSensor", 100
    setup.experiment.readonly.update({SELECTION, ROW_COUNT})
    assert prepare(sensor, {"mode": "bin_all"})["changed"] is False
    assert not setup.experiment.writes and clock.now == 0


def test_verified_bin_all_does_not_wait_for_editability(sensor):
    setup, clock = sensor
    setup.experiment.selection, setup.experiment.row_count = "LineSensor", 100
    setup.setting_is_available = lambda _key: False
    setup.setting_is_writable = lambda _key: False
    actual = prepare(sensor, {"mode": "bin_all"})
    assert actual["regions"] == [region(y_binning=100)]
    assert actual["changed"] is False
    assert clock.now == 0 and not setup.experiment.writes


def test_fixed_matching_selection_allows_writable_row_count_change(sensor):
    setup, clock = sensor
    setup.experiment.selection = "LineSensor"
    setup.setting_is_available = lambda key: key != SELECTION
    setup.setting_is_writable = lambda key: key != SELECTION
    actual = prepare(sensor, {"mode": "bin_all"})
    assert actual["row_binning"] == 100
    assert setup.experiment.writes == [(ROW_COUNT, 100)]
    assert clock.now == 0


def test_sensor_readiness_does_not_require_unrelated_recipe_capabilities(sensor):
    setup, clock = sensor
    setup.is_ready = False
    setup.sensor_readout_ready = True
    assert prepare(sensor, {"mode": "bin_all"})["row_binning"] == 100
    assert clock.now == 0


@pytest.mark.parametrize("busy", [False, True])
def test_sensor_not_ready_or_busy_still_blocks_preparation(sensor, busy):
    setup, clock = sensor
    setup.sensor_readout_ready = busy
    setup.is_busy = busy
    with pytest.raises(TimeoutError, match="waiting for the sensor.*Preview/Acquire"):
        prepare(sensor, {"mode": "bin_all"})
    assert not setup.experiment.writes


def test_geometry_timeout_reports_the_failed_readback(sensor):
    setup, clock = sensor
    original = setup.experiment.GetValue
    setup.experiment.GetValue = lambda key: ([region(height=50, y_binning=50)]
                                            if key == RESULT else original(key))
    with pytest.raises(TimeoutError, match="verifying the sensor regions.*entire physical active area.*regions="):
        prepare(sensor, {"mode": "bin_all"})
    assert clock.now == pytest.approx(.1)


def test_readback_returning_after_deadline_cannot_start_a_write(sensor):
    setup, clock = sensor
    original = setup.experiment.GetValue
    def delayed(key):
        clock.now = .2
        return original(key)
    setup.experiment.GetValue = delayed
    with pytest.raises(TimeoutError, match="reading the current sensor regions"):
        prepare(sensor, {"mode": "bin_all"})
    assert not setup.experiment.writes


@pytest.mark.parametrize("authorization", [None, "stale", "matching", "allow_custom"])
def test_custom_overwrite_needs_exact_reviewed_fingerprint(sensor, authorization):
    setup, _ = sensor
    setup.experiment.selection = "CustomRegions"
    intent = {"mode": "bin_all"}
    if authorization:
        key = "allow_custom" if authorization == "allow_custom" else "expected_fingerprint"
        intent[key] = snap(setup)["fingerprint"] if authorization != "stale" else "old"
    if authorization in {None, "stale"}:
        with pytest.raises(RuntimeError, match="Custom.*review.*fingerprint"):
            prepare(sensor, intent)
        assert not setup.experiment.writes
    else:
        assert prepare(sensor, intent)["selection"] == "LineSensor"


def test_custom_mode_preserves_exact_one_row_roi_and_never_writes(sensor):
    setup, _ = sensor
    setup.experiment.selection = "CustomRegions"
    expected = snap(setup)
    actual = prepare(sensor, {"mode": "custom", "snapshot": expected})
    assert actual["regions"] == expected["regions"] and actual["changed"] is False
    assert not setup.experiment.writes


@pytest.mark.parametrize("invalid", ["multiple", "two_rows", "drift"])
def test_custom_multi_region_or_multi_row_or_drift_cannot_be_flattened(sensor, invalid):
    setup, _ = sensor
    setup.experiment.selection = "CustomRegions"
    if invalid == "multiple": setup.experiment.custom *= 2
    if invalid == "two_rows": setup.experiment.custom = [region(y_binning=50)]
    expected = snap(setup)
    if invalid == "drift": setup.experiment.custom[0]["y"] += 1
    with pytest.raises(RuntimeError, match="one spectrum|exactly one row|fingerprint"):
        prepare(sensor, {"mode": "custom", "snapshot": expected})
    assert not setup.experiment.writes


def test_full_sensor_restores_two_dimensions_from_custom(sensor):
    setup, _ = sensor
    setup.experiment.selection = "CustomRegions"
    actual = prepare(sensor, {"mode": "full_sensor"})
    assert actual["regions"] == [region()] and actual["selection"] == "FullSensor"
    assert setup.experiment.writes == [(SELECTION, "FullSensor")]


@pytest.mark.parametrize("failure", ["unknown_height", "cropped_layout", "horizontal_bin"])
def test_unknown_or_overridden_full_area_is_rejected_before_writes(sensor, failure):
    setup, _ = sensor
    if failure == "unknown_height": setup.experiment.missing.add(HEIGHT)
    if failure == "cropped_layout": setup.experiment.full_regions = [region(height=50)]
    if failure == "horizontal_bin": setup.experiment.full_regions = [region(x_binning=2)]
    with pytest.raises(RuntimeError, match="Physical full-sensor area"):
        prepare(sensor, {"mode": "bin_all"})
    assert not setup.experiment.writes


def test_capability_failure_cannot_select_unsupported_line_mode(sensor):
    setup, _ = sensor
    setup.experiment.supported = ["FullSensor"]
    with pytest.raises(RuntimeError, match="unsupported.*blocked"):
        prepare(sensor, {"mode": "bin_all"})
    assert not setup.experiment.writes


def test_provider_drift_is_not_silently_accepted_or_repaired(sensor):
    setup, _ = sensor
    setup.experiment.after_write = lambda: setattr(setup.experiment, "provider", "Software")
    with pytest.raises(RuntimeError, match="provider changed"):
        prepare(sensor, {"mode": "bin_all"})
    assert all(key != PROVIDER for key, _ in setup.experiment.writes)


@pytest.mark.parametrize("failure", ["ignored", "readonly", "unavailable"])
def test_unapplied_full_height_cannot_report_bin_all(sensor, failure):
    setup, clock = sensor
    setup.experiment.selection = "LineSensor"
    if failure == "ignored": setup.experiment.ignore.add(ROW_COUNT)
    if failure == "readonly": setup.experiment.readonly.add(ROW_COUNT)
    if failure == "unavailable": setup.experiment.missing.add(ROW_COUNT)
    expected = RuntimeError if failure == "unavailable" else TimeoutError
    with pytest.raises(expected, match=ROW_COUNT) as error:
        prepare(sensor, {"mode": "bin_all"})
    assert "acquisition blocked" in str(error.value)
    if failure == "ignored":
        assert "Requested 100; read back 10" in str(error.value)
        assert "write was sent once" in str(error.value)
    if failure == "readonly":
        assert "read-only" in str(error.value)
    assert clock.now == pytest.approx(0 if failure == "unavailable" else .1)
    assert setup.experiment.writes == ([(ROW_COUNT, 100)] if failure == "ignored" else [])


def test_slow_capabilities_probe_cannot_write_after_deadline(sensor):
    setup, clock = sensor

    def capabilities(_key):
        clock.now = .1
        return ["LineSensor", "FullSensor"]

    setup.experiment.GetCurrentCapabilities = capabilities
    with pytest.raises(TimeoutError): prepare(sensor, {"mode": "bin_all"})
    assert not setup.experiment.writes


@pytest.mark.parametrize("when", ["before", "waiting", "after_selection"])
def test_stop_blocks_next_roi_mutation(sensor, when):
    setup, clock = sensor
    stop = threading.Event()
    if when == "before": stop.set()
    if when == "waiting":
        setup.is_busy = True
        clock.after_sleep = stop.set
    if when == "after_selection": setup.experiment.after_write = stop.set
    with pytest.raises(RuntimeError, match="cancelled"):
        prepare(sensor, {"mode": "bin_all"}, stop_event=stop)
    assert setup.experiment.writes == ([(SELECTION, "LineSensor")] if when == "after_selection" else [])


def test_new_custom_region_during_sdk_probe_is_preserved(sensor):
    setup, _ = sensor

    def capabilities(_key):
        setup.experiment.selection = "CustomRegions"
        return ["LineSensor", "FullSensor"]

    setup.experiment.GetCurrentCapabilities = capabilities
    with pytest.raises(RuntimeError, match="Custom.*changed before Bin All"):
        prepare(sensor, {"mode": "bin_all"})
    assert not setup.experiment.writes


@pytest.fixture
def real_methods(sensor):
    """Compile only the production boundary methods without executing SDK imports."""
    source = Path(__file__).resolve().parents[1] / "lf6_automation.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LF6Setup")
    names = {"read_sensor_snapshot", "prepare_sensor_readout", "verify_acquisition_settings",
             "configure_for_acquisition", "change_roi_FullSensor", "change_roi_LineSensor"}
    selected = ast.ClassDef(name="Setup", bases=[], keywords=[], decorator_list=[],
                            body=[node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names])
    guard = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_guard_setup_call")
    setup, clock = sensor
    namespace = {"wraps": wraps, "builtins": builtins, "time": clock, "CameraSettings": SETTINGS,
                 "Int32": int,
                 "ExperimentSettings": SimpleNamespace(),
                 "np": SimpleNamespace(isfinite=math.isfinite),
                 "RegionsOfInterestSelection": SELECTIONS, "get_capture_guard": lambda _s: SimpleNamespace(check_available=lambda: None),
                 "LIGHTFIELD_SETTING_TIMEOUT_S": .1, "LIGHTFIELD_POLL_INTERVAL_S": .01,
                 "SpectrometerSettings": SimpleNamespace(), "LightFieldSettingTimeoutError": TimeoutError}
    module = ast.fix_missing_locations(ast.Module(body=[guard, selected], type_ignores=[]))
    exec(compile(module, str(source), "exec"), namespace)
    owner = namespace["Setup"]()
    owner.__dict__.update(setup.__dict__)
    owner._ensure_detector_output = lambda **kw: None
    owner._acquisition_prepared = True
    return owner


def test_bound_roi_drift_blocks_per_capture_verification_without_repair(real_methods):
    owner = real_methods
    result = owner.prepare_sensor_readout({"mode": "bin_all"})
    assert result["row_binning"] == 100
    owner.verify_acquisition_settings()
    before = list(owner.experiment.writes)
    owner.experiment.row_count = 10
    with pytest.raises(RuntimeError, match="physical active area|changed during acquisition"):
        owner.verify_acquisition_settings()
    assert owner.experiment.writes == before and owner.experiment.row_count == 10


def test_failed_sensor_preparation_blocks_capture_until_prepared_again(real_methods):
    owner = real_methods
    owner.experiment.selection = "CustomRegions"
    with pytest.raises(RuntimeError): owner.prepare_sensor_readout({"mode": "bin_all"})
    with pytest.raises(RuntimeError, match="sensor preparation failed"):
        owner.verify_acquisition_settings()
    assert not owner.experiment.writes


def test_legacy_explicit_roi_choice_releases_previous_bound_policy(real_methods):
    owner = real_methods
    owner.prepare_sensor_readout({"mode": "bin_all"})
    owner.change_roi_FullSensor()
    assert owner._sensor_intent is None
    owner.verify_acquisition_settings()


def test_new_acquisition_recipe_does_not_inherit_another_pages_roi_policy(real_methods):
    owner = real_methods
    owner.prepare_sensor_readout({"mode": "bin_all"})
    owner.set_center_wavelength_when_ready = lambda *args, **kwargs: None
    owner.change_expose_time = owner.change_frame_to_combine = lambda value: None
    owner._verify_acquisition_recipe = lambda: dict(exposure_ms=10., frames=1.)
    owner.center_wavelength_write_stats = {}
    owner.configure_for_acquisition(center_nm=730., exposure_ms=10., frames=1)
    assert owner._sensor_intent is None and owner._sensor_prepared
    owner.experiment.selection = 'FullSensor'
    owner.verify_acquisition_settings()  # The next page applies its own explicit policy.


def test_bound_provider_change_is_rejected_before_the_next_capture(real_methods):
    owner = real_methods
    owner.prepare_sensor_readout({"mode": "bin_all"})
    before = list(owner.experiment.writes)
    owner.experiment.provider = 'Software'
    with pytest.raises(RuntimeError, match='changed during acquisition'):
        owner.verify_acquisition_settings()
    assert owner.experiment.writes == before


def test_custom_preparation_is_also_bound_for_pre_and_post_capture(real_methods):
    owner = real_methods
    owner.experiment.selection = 'CustomRegions'
    snapshot = owner.read_sensor_snapshot()
    owner.prepare_sensor_readout({'mode': 'custom', 'snapshot': snapshot})
    owner.verify_acquisition_settings()
    owner.experiment.custom[0]['x'] += 1
    with pytest.raises(RuntimeError, match='fingerprint changed'):
        owner.verify_acquisition_settings()
    assert not owner.experiment.writes


def test_mock_uses_the_same_shared_policy_and_capture_guard():
    from utils.mock_lf6 import MockLF6Setup
    setup = MockLF6Setup(simulate_delay=False)
    snapshot = setup.prepare_sensor_readout({"mode": "bin_all"})
    assert snapshot["row_binning"] == snapshot["sensor_height"] == 100
    setup.acquire()
    setup._sensor_row_binning = 10
    with pytest.raises(RuntimeError): setup.acquire()


def test_mock_custom_capture_matches_cropped_and_binned_output_shape():
    from utils.mock_lf6 import MockLF6Setup
    setup = MockLF6Setup(simulate_delay=False)
    setup._roi = "CustomRegions"
    setup._custom_sensor_regions = [region(x=10, y=45, width=12, height=10, x_binning=2, y_binning=10)]
    snapshot = setup.read_sensor_snapshot()
    setup.prepare_sensor_readout({'mode': 'custom', 'snapshot': snapshot})
    assert setup.acquire().shape == setup.get_wavelength_calibration().shape == (6,)
    assert setup.acquire_2d().shape == (1, 6)
