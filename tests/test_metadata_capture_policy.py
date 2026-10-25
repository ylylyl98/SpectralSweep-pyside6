"""File metadata policy skips capture journals without weakening acquisition."""
import copy
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest

from app.devices.andor_adapter import AndorSDK2Setup
from app.devices.winspec_scan_adapter import WinSpecScanAdapter
from app.experiment_metadata import ExperimentMetadataService
from app.lightfield_capture_guard import LightFieldAcquisitionStalled, capture_status
from app.lightfield_metadata import (
    LightFieldRecorder, bind_lightfield_metadata, capture_with_metadata,
    set_lightfield_context,
)


@pytest.fixture
def run(tmp_path):
    return ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite").begin("dual_gate_sweep", "sample")


def snapshot(backend="lightfield"):
    return {"captured_utc": "2026-10-25T12:00:00Z", "identity": {"backend": backend},
            "camera": {"temperature_c": -70., "temperature_status": "locked"},
            "observed": {"last_frame": {"elapsed_s": 1.25}, "exposure_ms": 50.},
            "calibration": {"wavelength_axis_nm": [700.1234567890123, 701.2, 702.3]}}


def backend_capture(backend, recorder):
    """Return fake-only capture surfaces plus observable validation calls."""
    if backend == "lightfield":
        dataset = SimpleNamespace(GetFrame=Mock(return_value=object()), Dispose=Mock())
        setup = SimpleNamespace(_metadata_recorder=recorder,
                                read_metadata_snapshot=Mock(return_value=snapshot()),
                                verify_acquisition_settings=Mock(),
                                experiment=SimpleNamespace(Capture=Mock(return_value=dataset)),
                                _frame_dims=lambda _: (3, 1))
        return SimpleNamespace(call=lambda: capture_with_metadata(setup, 1), setup=setup,
                               reads=[setup.read_metadata_snapshot], sdk=setup.experiment.Capture,
                               verify=setup.verify_acquisition_settings, dataset=dataset)
    if backend == "andor":
        setup = object.__new__(AndorSDK2Setup)
        setup._metadata_recorder = recorder
        setup._abort, setup._busy = threading.Event(), threading.Event()
        setup.options = SimpleNamespace(operation_timeout_s=1., timeout_margin_s=.5)
        setup._exposure_ms, setup._accumulations = 50., 2
        setup.read_metadata_snapshot = Mock(return_value=snapshot("andor_sdk2"))
        setup._capture_frames_on_owner = Mock()
        setup._call = Mock(return_value=np.array([[10., 20., 30.]]))
        return SimpleNamespace(call=setup.acquire_2d, setup=setup,
                               reads=[setup.read_metadata_snapshot], sdk=setup._call)
    context = {"center_nm": 700.0}
    setup = SimpleNamespace(_metadata_recorder=recorder,
                            acquire=Mock(return_value=(None, np.array([10., 20., 30.]))),
                            read_metadata_snapshot=Mock(return_value=snapshot("winspec_ingaas")),
                            last_calibration_context=dict(context))
    adapter = WinSpecScanAdapter(setup)
    adapter._check_prepared = Mock(return_value=(context, {}, np.array([700., 702.]), np.array([True, False, True])))
    adapter.read_metadata_snapshot = Mock(return_value=snapshot("winspec_ingaas"))
    return SimpleNamespace(call=adapter.acquire, setup=setup, adapter=adapter,
                           reads=[adapter.read_metadata_snapshot, setup.read_metadata_snapshot],
                           sdk=setup.acquire, verify=adapter._check_prepared)


@pytest.mark.parametrize("backend", ["lightfield", "andor", "winspec"])
def test_file_policy_has_no_capture_reads_events_or_callbacks(run, backend):
    recorder = LightFieldRecorder(run, capture_policy="file")
    fake = backend_capture(backend, recorder)
    assert recorder.active and not recorder.records_captures
    with patch.object(recorder, "begin", side_effect=AssertionError("capture metadata begin")), \
            patch.object(recorder, "finish", side_effect=AssertionError("capture metadata finish")):
        for _ in range(3):
            fake.call()
    assert fake.sdk.call_count == 3
    assert all(read.call_count == 0 for read in fake.reads)
    assert run.read_events() == []
    assert run.metadata["acquisitions"]["count"] == 0
    if backend == "lightfield":
        assert fake.verify.call_count == 6  # Before and after every Capture.
        assert not capture_status(fake.setup)["active"]
    elif backend == "winspec":
        assert fake.verify.call_count == 3
    else:
        assert not fake.setup._busy.is_set()
        assert fake.sdk.call_args.kwargs["timeout_s"] == pytest.approx(2.1)


@pytest.mark.parametrize("backend", ["lightfield", "andor", "winspec"])
def test_default_policy_and_legacy_recorder_doubles_still_record_captures(run, backend):
    recorder = LightFieldRecorder(run)
    fake = backend_capture(backend, recorder)
    fake.call()
    assert [event["event"] for event in run.read_events()] == ["capture_started", "capture_completed"]
    assert all(read.call_count == 1 for read in fake.reads)
    legacy = SimpleNamespace(active=True, begin=Mock(return_value={"settings_id": 1}), finish=Mock())
    legacy_fake = backend_capture(backend, legacy)
    legacy_fake.call()
    legacy.begin.assert_called_once()
    legacy.finish.assert_called_once()


@pytest.mark.parametrize("backend", ["lightfield", "andor", "winspec"])
def test_file_policy_preserves_sdk_errors_and_skips_failure_capture_records(run, backend):
    recorder = LightFieldRecorder(run, capture_policy="file")
    fake = backend_capture(backend, recorder)
    error = RuntimeError("fake detector failed")
    fake.sdk.side_effect = error
    with patch.object(recorder, "finish", side_effect=AssertionError("unexpected capture event")):
        with pytest.raises(RuntimeError) as caught:
            fake.call()
    assert caught.value is error
    assert all(read.call_count == 0 for read in fake.reads)
    assert run.read_events() == []


def test_lightfield_timeout_and_late_dataset_cleanup_remain_active(run):
    recorder = LightFieldRecorder(run, capture_policy="file")
    fake = backend_capture("lightfield", recorder)
    fake.setup._capture_wait_timeout_s = .01

    def late_capture(_frames):
        time.sleep(.04)
        return fake.dataset

    fake.sdk.side_effect = late_capture
    with pytest.raises(LightFieldAcquisitionStalled):
        fake.call()
    assert capture_status(fake.setup)["recovery_required"]
    fake.dataset.Dispose.assert_called_once()
    assert run.read_events() == []


def test_winspec_post_capture_calibration_validation_still_rejects_changed_context(run):
    fake = backend_capture("winspec", LightFieldRecorder(run, capture_policy="file"))
    fake.setup.last_calibration_context = {"center_nm": 750.}
    with pytest.raises(RuntimeError, match="context changed during acquisition"):
        fake.call()
    assert all(read.call_count == 0 for read in fake.reads)


def test_binding_updates_same_run_policy_and_switches_backends_without_hardware_reads(run, tmp_path):
    lightfield = SimpleNamespace()
    lightfield.bind_metadata_run = Mock(side_effect=lambda value: setattr(lightfield, "_metadata_recorder", LightFieldRecorder(value)))
    andor = object.__new__(AndorSDK2Setup)
    winspec = SimpleNamespace()
    adapter = WinSpecScanAdapter(winspec)
    controller = SimpleNamespace()
    for name, setup, current_adapter in [("lightfield", lightfield, None), ("andor_si", andor, None),
                                          ("winspec_ingaas", winspec, adapter), ("lightfield", lightfield, None)]:
        controller.backend, controller.setup, controller.adapter = name, setup, current_adapter
        bind_lightfield_metadata(controller, run, capture_policy="file")
        recorder = setup._metadata_recorder
        set_lightfield_context(controller, point_index=9, output_file=tmp_path / "point.csv")
        assert recorder.context == {"point_index": 9, "output_file": "point.csv"}
        assert recorder.active and not recorder.records_captures
        bind_lightfield_metadata(controller, run)  # Same run can restore ordinary capture recording.
        assert setup._metadata_recorder is recorder and recorder.records_captures
        bind_lightfield_metadata(controller, run, capture_policy="file")
        assert setup._metadata_recorder is recorder and not recorder.records_captures
    next_run = run.service.begin("spectrum_preview", "next")
    bind_lightfield_metadata(controller, next_run)
    assert controller.setup._metadata_recorder._run() is next_run
    assert controller.setup._metadata_recorder.records_captures


@pytest.mark.parametrize("backend", ["lightfield", "andor", "winspec"])
def test_terminal_run_is_inactive_and_does_not_read_metadata(run, backend):
    recorder = LightFieldRecorder(run)
    fake = backend_capture(backend, recorder)
    run.complete()
    before = run.path.read_bytes()
    assert not recorder.active and not recorder.records_captures
    assert recorder.record_settings_snapshot(snapshot()) is None
    fake.call()
    assert all(read.call_count == 0 for read in fake.reads)
    assert run.path.read_bytes() == before


@pytest.mark.parametrize("backend", ["lightfield", "andor_sdk2", "winspec_ingaas"])
def test_explicit_file_snapshot_is_durable_lossless_and_deduplicates_telemetry(run, backend):
    recorder = LightFieldRecorder(run, capture_policy="file")
    first = snapshot(backend)
    if backend == "andor_sdk2":
        first["observed"].update(temperature_c=-70., temperature_status="locked")
    second = copy.deepcopy(first)
    second["captured_utc"] = "2026-10-25T12:01:00Z"
    second["camera"].update(temperature_c=-69.9, temperature_status="stabilizing")
    if backend == "andor_sdk2":
        second["observed"].update(temperature_c=-69.9, temperature_status="stabilizing")
    if backend == "winspec_ingaas":
        second["observed"]["last_frame"] = {"elapsed_s": 1.3, "raw_counts": [10, 20, 30]}
    before = copy.deepcopy(first)
    one = recorder.record_settings_snapshot(first)
    two = recorder.record_settings_snapshot(second)
    assert one == two
    assert first == before
    saved = run.service.load_metadata(run.path)
    settings = saved["settings"]["snapshots"]
    assert len(settings) == 1
    assert settings[0]["settings_id"] == one
    assert settings[0]["values"]["calibration"] == first["calibration"]
    assert "temperature_c" not in settings[0]["values"]["camera"]
    assert saved["acquisitions"]["count"] == 0
    assert run.read_events() == []
    assert recorder.begin(first, 1, "measurement") is None


def test_explicit_snapshot_failure_propagates_for_file_boundary_to_abort(run):
    recorder = LightFieldRecorder(run, capture_policy="file")
    error = OSError("fake storage failure")
    with patch.object(run, "register_settings_snapshot", side_effect=error):
        with pytest.raises(OSError) as caught:
            recorder.record_settings_snapshot(snapshot())
    assert caught.value is error
    assert run.metadata["metadata_status"] == "degraded"


def test_invalid_policy_is_rejected_before_rebinding(run):
    setup = SimpleNamespace(bind_metadata_run=Mock())
    with pytest.raises(ValueError, match="capture policy"):
        bind_lightfield_metadata(SimpleNamespace(setup=setup), run, capture_policy="unknown")
    setup.bind_metadata_run.assert_not_called()


def test_cached_portable_context_does_not_resolve_filesystem_paths(run):
    recorder = LightFieldRecorder(run, capture_policy="file")
    controller = SimpleNamespace(setup=SimpleNamespace(_metadata_recorder=recorder))
    with patch.object(Path, "resolve", side_effect=AssertionError("unexpected per-point path resolution")):
        set_lightfield_context(controller, output_file_is_portable=True,
                               output_file="REF/scan.csv", point_index=3,
                               Vtg_set=1.23456789012345, purpose="measurement")
    assert recorder.context == {"output_file": "REF/scan.csv", "point_index": 3,
                                "Vtg_set": 1.23456789012345, "purpose": "measurement"}
    assert "output_file_is_portable" not in recorder.context
