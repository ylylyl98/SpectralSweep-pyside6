"""Dataset ownership and late SDK replies, using only fake LightField objects."""
from __future__ import annotations

import ctypes
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

import lf6_automation as lf6
from app.devices.lf6_adapter import SpectrometerLF6
from app.devices.lightfield_optics import apply_optics
from app.lightfield_capture_guard import (
    LightFieldAcquisitionStalled,
    LightFieldCaptureOwnershipError,
    capture_status,
    get_capture_guard,
)
from app.lightfield_metadata import capture_with_metadata, read_snapshot


class Dataset:
    def __init__(self):
        self.data = np.array([10., 20., 30.])
        self.frame = SimpleNamespace(Width=3, Height=1, Format=None, GetData=lambda: self.data)
        self.disposals = 0
        self.get_frames = []
        self.dispose_error = None

    def GetFrame(self, region, index):
        self.get_frames.append((region, index))
        return self.frame

    def Dispose(self):
        self.disposals += 1
        self.data[:] = -999.
        if self.dispose_error is not None:
            raise self.dispose_error


@pytest.fixture
def setup(tmp_path):
    dataset = Dataset()
    value = object.__new__(lf6.LF6Setup)
    value.experiment = SimpleNamespace(
        Capture=Mock(return_value=dataset), Exists=lambda _: True, GetValue=lambda _: 2,
        Load=Mock(return_value=True), SetValue=Mock(),
    )
    value.verify_acquisition_settings = Mock()
    value.convert_buffer = lambda data, fmt: np.array(data, copy=True)
    value._capture_wait_timeout_s = 2.
    value._capture_diagnostics_path = tmp_path / "capture.jsonl"
    value.test_dataset = dataset
    return value


@pytest.mark.parametrize("method,kwargs,shape", [
    ("acquire", {}, (3,)),
    ("acquire_2d", {}, (1, 3)),
    ("multi_frame_acquire", {"x_pixels_num": 3}, (2, 3)),
    ("multi_frame_acquire", {"image_mode": True, "x_pixels_num": 3, "y_pixels_num": 1}, (2, 3)),
])
def test_all_capture_paths_copy_before_exactly_once_dispose(setup, method, kwargs, shape):
    result = getattr(setup, method)(**kwargs)
    assert result.shape == shape
    np.testing.assert_array_equal(result, np.broadcast_to([10., 20., 30.], shape))
    assert setup.test_dataset.disposals == 1
    assert not capture_status(setup)["active"]


@pytest.mark.parametrize("phase", ["post_validation", "get_frame", "get_data", "conversion"])
def test_failure_after_capture_disposes_once_and_preserves_original(setup, phase):
    error = RuntimeError(phase)
    if phase == "post_validation":
        setup.verify_acquisition_settings.side_effect = [None, error]
    elif phase == "get_frame":
        setup.test_dataset.GetFrame = Mock(side_effect=error)
    elif phase == "get_data":
        setup.test_dataset.frame.GetData = Mock(side_effect=error)
    else:
        setup.convert_buffer = Mock(side_effect=error)
    with pytest.raises(RuntimeError) as caught:
        setup.acquire()
    assert caught.value is error
    assert setup.test_dataset.disposals == 1
    assert not capture_status(setup)["active"]


def test_capture_error_has_no_dataset_to_dispose(setup):
    error = OSError("SDK Capture failed")
    setup.experiment.Capture.side_effect = error
    with pytest.raises(OSError) as caught:
        setup.acquire()
    assert caught.value is error
    assert setup.test_dataset.disposals == 0


def test_release_error_quarantines_and_does_not_mask_conversion_error(setup):
    original = ValueError("conversion failed")
    setup.convert_buffer = Mock(side_effect=original)
    setup.test_dataset.dispose_error = OSError("release failed")
    with pytest.raises(LightFieldAcquisitionStalled) as caught:
        setup.acquire()
    assert caught.value.__cause__ is original
    assert setup.test_dataset.disposals == 1
    assert capture_status(setup)["recovery_required"]


def test_release_error_without_primary_failure_is_visible_and_sticky(setup):
    setup.test_dataset.dispose_error = OSError("release failed")
    with pytest.raises((OSError, LightFieldAcquisitionStalled), match="release|recovery|budget"):
        setup.acquire()
    assert setup.test_dataset.disposals == 1
    assert capture_status(setup)["recovery_required"]
    with pytest.raises(LightFieldAcquisitionStalled):
        setup.acquire()
    assert setup.experiment.Capture.call_count == 1


def wait_until(predicate, timeout=2.):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Expected fake capture state was not observed")
        time.sleep(.005)


class Recorder:
    active = True

    def __init__(self):
        self.events = []

    def begin(self, snapshot, frames, purpose):
        record = {"acquisition_id": "fake-capture"}
        self.events.append(("capture_started", None))
        return record

    def finish(self, record, *, error=None, **kwargs):
        self.events.append(("capture_failed" if error else "capture_completed", error))


def test_indefinitely_blocked_capture_quarantines_then_releases_late_dataset(setup):
    entered, release = threading.Event(), threading.Event()
    setup._capture_wait_timeout_s = .08
    recorder = Recorder()
    setup._metadata_recorder = recorder
    setup.read_metadata_snapshot = lambda: {}
    result, errors = [], []

    def blocked_capture(frames):
        entered.set()
        release.wait()  # Only the test owner releases this fake SDK operation.
        return setup.test_dataset

    def acquire():
        try:
            result.append(setup.acquire())
        except BaseException as error:
            errors.append(error)

    setup.experiment.Capture.side_effect = blocked_capture
    owner = threading.Thread(target=acquire)
    owner.start()
    try:
        assert entered.wait(1)
        wait_until(lambda: capture_status(setup).get("stalled"))
        assert owner.is_alive()
        assert capture_status(setup)["active"]
        assert capture_status(setup)["context"]["acquisition_id"] == "fake-capture"
        assert setup.test_dataset.disposals == 0
        with pytest.raises((LightFieldAcquisitionStalled, LightFieldCaptureOwnershipError)):
            setup.acquire()
        with pytest.raises(LightFieldCaptureOwnershipError):
            setup.close()
        with pytest.raises((LightFieldAcquisitionStalled, LightFieldCaptureOwnershipError)):
            setup.load_experiment("must not load")
        setup.experiment.Load.assert_not_called()
        release.set()
        owner.join(2)
        assert not owner.is_alive()
        assert result == []
        assert len(errors) == 1 and isinstance(errors[0], LightFieldAcquisitionStalled)
        assert setup.test_dataset.disposals == 1
        assert [event for event, _ in recorder.events] == ["capture_started", "capture_failed"]
        assert setup.verify_acquisition_settings.call_count == 1
        assert not capture_status(setup)["active"]
        assert capture_status(setup)["recovery_required"]
        with pytest.raises(LightFieldAcquisitionStalled):
            setup.acquire()
        assert setup.experiment.Capture.call_count == 1
        # Drained quarantine allows disposing the owned Automation object.
        setup.auto = SimpleNamespace(Dispose=Mock())
        automation = setup.auto
        setup.close()
        automation.Dispose.assert_called_once()
    finally:
        release.set()
        owner.join(2)


@pytest.mark.parametrize("phase", ["get_data", "dispose", "wavelength"])
def test_late_readout_cleanup_or_wavelength_cannot_return_result(setup, phase):
    setup._capture_wait_timeout_s = .03
    original_data = setup.test_dataset.frame.GetData
    original_dispose = setup.test_dataset.Dispose

    def late_data():
        time.sleep(.07)
        return original_data()

    def late_dispose():
        time.sleep(.07)
        original_dispose()

    def late_wavelength():
        time.sleep(.07)
        return np.array([700., 701., 702.])

    method = setup.acquire
    if phase == "get_data":
        setup.test_dataset.frame.GetData = late_data
        setup.convert_buffer = Mock(wraps=setup.convert_buffer)
    elif phase == "dispose":
        setup.test_dataset.Dispose = late_dispose
    else:
        setup.get_wavelength_calibration = late_wavelength
        method = SpectrometerLF6(setup).acquire
    with pytest.raises(LightFieldAcquisitionStalled):
        method()
    assert setup.test_dataset.disposals == 1
    assert capture_status(setup)["recovery_required"]
    if phase == "get_data":
        setup.convert_buffer.assert_not_called()


def test_capture_helper_transfers_dataset_without_disposing_success(setup):
    assert capture_with_metadata(setup, 1) is setup.test_dataset
    assert setup.test_dataset.disposals == 0


def test_capture_helper_releases_rejected_final_handoff_while_still_owned(setup, monkeypatch):
    guard = get_capture_guard(setup)
    original_leave = guard._leave
    active_at_release = []
    original_dispose = setup.test_dataset.Dispose

    def dispose():
        active_at_release.append(capture_status(setup)["active"])
        original_dispose()

    def expire_at_exit(operation, on_stalled_exit=None):
        operation.deadline = time.monotonic() - 1
        return original_leave(operation, on_stalled_exit)

    setup.test_dataset.Dispose = dispose
    monkeypatch.setattr(guard, "_leave", expire_at_exit)
    with pytest.raises(LightFieldAcquisitionStalled):
        capture_with_metadata(setup, 1)
    assert setup.test_dataset.disposals == 1
    assert active_at_release == [True]
    assert not capture_status(setup)["active"]


def test_multiframe_budget_uses_frame_count_without_resetting_operation(setup):
    del setup._capture_wait_timeout_s
    setup._acquisition_recipe = {"exposure_ms": 2000., "frames": 40}
    seen = []
    setup.experiment.Capture.side_effect = lambda frames: seen.append(capture_status(setup)) or setup.test_dataset
    setup.multi_frame_acquire(x_pixels_num=3)
    assert seen[0]["timeout_s"] == 495.
    assert seen[0]["context"]["capture_frames_requested"] == 2
    assert setup.test_dataset.disposals == 1


def test_late_optional_metadata_getter_stops_before_other_sdk_calls(setup):
    setup._capture_wait_timeout_s = .03
    setup._metadata_recorder = Recorder()
    setup.read_metadata_snapshot = lambda: read_snapshot(
        setup, camera=lf6.CameraSettings, spectrometer=lf6.SpectrometerSettings,
        experiment=lf6.ExperimentSettings)
    setup.experiment.Exists = Mock(side_effect=lambda _: time.sleep(.07) or True)
    setup.experiment.GetValue = Mock(return_value=1)
    with pytest.raises(LightFieldAcquisitionStalled):
        setup.acquire()
    assert setup.experiment.Exists.call_count == 1
    setup.experiment.GetValue.assert_not_called()
    setup.experiment.Capture.assert_not_called()


def test_late_verification_readback_stops_before_next_setting_or_capture(setup):
    setup._capture_wait_timeout_s = .03
    setup._acquisition_recipe = {"center_nm": 730., "exposure_ms": 100., "frames": 1}
    setup.verify_acquisition_settings = lambda: lf6.LF6Setup._verify_acquisition_recipe(setup)
    setup.experiment.GetValue = Mock(side_effect=lambda _: time.sleep(.07) or 730.)
    with pytest.raises(LightFieldAcquisitionStalled):
        setup.acquire()
    assert setup.experiment.GetValue.call_count == 1
    setup.experiment.Capture.assert_not_called()


@pytest.mark.parametrize("phase", ["exists", "get_value"])
def test_late_output_route_getter_stops_before_next_setting_or_capture(setup, phase):
    setup._capture_wait_timeout_s = .03
    setup.detector_output_route = "front"
    setup.spectrometer_settings = lf6.SpectrometerSettings
    setup.verify_acquisition_settings = lambda: lf6.LF6Setup.verify_acquisition_settings(setup)
    setup.experiment.Exists = Mock(return_value=True)
    setup.experiment.GetValue = Mock(return_value="grating")
    if phase == "exists":
        setup.experiment.Exists.side_effect = lambda _: time.sleep(.07) or True
    else:
        setup.experiment.GetValue.side_effect = lambda _: time.sleep(.07) or "grating"
    with pytest.raises(LightFieldAcquisitionStalled):
        setup.acquire()
    assert setup.experiment.Exists.call_count == 1
    assert setup.experiment.GetValue.call_count == (0 if phase == "exists" else 1)
    setup.experiment.Capture.assert_not_called()
    # The same setup is used by borrowed-optics integrations too.
    with pytest.raises(LightFieldAcquisitionStalled):
        apply_optics(setup, {"output_port": "SideExit"})
    setup.experiment.SetValue.assert_not_called()


def test_late_missing_wavelength_getter_does_not_start_fallback(setup):
    setup._capture_wait_timeout_s = .03

    def missing_after_delay():
        time.sleep(.07)
        raise AttributeError("primary getter unavailable")

    setup.get_wavelength_calibration = missing_after_delay
    setup.calibrate_wavelength = Mock(return_value=[700., 701., 702.])
    with pytest.raises(LightFieldAcquisitionStalled):
        SpectrometerLF6(setup).acquire()
    setup.calibrate_wavelength.assert_not_called()
    assert setup.test_dataset.disposals == 1


@pytest.mark.parametrize("kind", ["image", "wavelength"])
def test_managed_buffer_copy_precedes_unpin(monkeypatch, kind):
    ctype = ctypes.c_ushort if kind == "image" else ctypes.c_double
    storage = (ctype * 3)(11, 22, 33)
    frees = []

    class Pinned:
        IsAllocated = True

        def AddrOfPinnedObject(self):
            return SimpleNamespace(ToInt64=lambda: ctypes.addressof(storage))

        def Free(self):
            # Simulate storage becoming invalid immediately after unpinning.
            frees.append(True)
            for index in range(3):
                storage[index] = 0
            self.IsAllocated = False

    monkeypatch.setattr(lf6, "GCHandle", SimpleNamespace(Alloc=lambda *args: Pinned()))
    value = object.__new__(lf6.LF6Setup)
    if kind == "image":
        result = value.convert_buffer([11, 22, 33], lf6.ImageDataFormat.MonochromeUnsigned16)
    else:
        value.experiment = SimpleNamespace(SystemColumnCalibration=[11., 22., 33.])
        result = value.get_wavelength_calibration()
    np.testing.assert_array_equal(result, [11., 22., 33.])
    assert frees == [True]


def test_pin_is_freed_when_copy_raises(monkeypatch):
    storage = (ctypes.c_ushort * 2)(1, 2)
    free = Mock()
    handle = SimpleNamespace(IsAllocated=True, Free=free,
        AddrOfPinnedObject=lambda: SimpleNamespace(ToInt64=lambda: ctypes.addressof(storage)))
    monkeypatch.setattr(lf6, "GCHandle", SimpleNamespace(Alloc=lambda *args: handle))
    error = MemoryError("copy failed")
    monkeypatch.setattr(lf6.np, "copy", Mock(side_effect=error))
    with pytest.raises(MemoryError) as caught:
        object.__new__(lf6.LF6Setup).convert_buffer([1, 2], lf6.ImageDataFormat.MonochromeUnsigned16)
    assert caught.value is error
    free.assert_called_once()
