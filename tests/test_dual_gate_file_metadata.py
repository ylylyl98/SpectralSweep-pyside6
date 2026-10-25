"""Dual Gate file-boundary metadata, exercised through the real worker with fake devices."""
import copy
import csv
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest
from PySide6.QtWidgets import QApplication

from app.engine.csv_writer import CSVWriter
from app.experiment_metadata import ExperimentMetadataService
from app.lightfield_metadata import LightFieldRecorder, managed_capture_dataset
from tests import test_dual_gate_interleaving_regression as interleaving
from tests import test_smu_resilience as smu_fixtures
from ui import presets_panel as panel
from utils.config import cfg


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def journal(run):
    if not run.event_path.exists():
        return []
    return [json.loads(line) for line in run.event_path.read_text(encoding="utf-8").splitlines()]


def scalar_values(value):
    if isinstance(value, dict):
        return [item for child in value.values() for item in scalar_values(child)]
    if isinstance(value, list):
        return [item for child in value for item in scalar_values(child)]
    return [value]


class Detector(smu_fixtures._FakeLFController):
    """Runs the real recorder/capture boundary without importing the .NET SDK."""
    backend = "lightfield"
    connected_backends = ("lightfield", "winspec_ingaas")

    def __init__(self, run, stop, *, image_rows=1, stop_on=None, fail_on=None):
        super().__init__()
        self.run, self.stop = run, stop
        self.image_rows, self.stop_on, self.fail_on = image_rows, stop_on, fail_on
        self.capture_count = 0
        self.before_capture = None
        self.snapshots = []
        self.settings = {"center_nm": 700.1234567890123,
                         "exposure_ms": 10.1234567890123, "frames": 3}
        frame = SimpleNamespace(Width=2, Height=image_rows)
        self.dataset = SimpleNamespace(GetFrame=lambda *_: frame, Dispose=Mock())
        self.setup = SimpleNamespace(
            _capture_wait_timeout_s=5,
            _frame_dims=lambda _: (2, image_rows),
            experiment=SimpleNamespace(Capture=self._capture),
            verify_acquisition_settings=Mock(),
        )
        self.setup.bind_metadata_run = self.bind_metadata_run
        self.setup.read_metadata_snapshot = self.read_metadata_snapshot
        self.bind_metadata_run(run)
        self.adapter = SimpleNamespace(acquire=self.acquire, bind_metadata_run=self.bind_metadata_run)

    def bind_metadata_run(self, run):
        self.setup._metadata_recorder = LightFieldRecorder(run)

    def configure_for_acquisition(self, *, center_nm, exposure_ms, frames):
        self.settings = dict(center_nm=center_nm, exposure_ms=exposure_ms, frames=frames)
        self.setup._acquisition_recipe = dict(self.settings)

    def prepare_scan_condition(self, backend, center_nm, exposure_ms, frames, reduction, stop):
        self.backend = backend
        self.configure_for_acquisition(center_nm=center_nm, exposure_ms=exposure_ms, frames=frames)
        return self.adapter

    def read_metadata_snapshot(self):
        self.snapshots.append(copy.deepcopy(self.settings))
        return {"captured_utc": "2026-10-25T00:00:00Z", "backend": self.backend,
                "acquisition": {"exposure_ms": self.settings["exposure_ms"],
                                "exposures_per_frame": self.settings["frames"]},
                "spectrometer": {"center_wavelength_nm": self.settings["center_nm"]},
                "calibration": {"detector_wavelength_nm": [700.1234567890123, 701.2345678901234]}}

    def _capture(self, _frames):
        self.capture_count += 1
        if self.before_capture is not None:
            self.before_capture(self.capture_count)
        if self.fail_on == self.capture_count:
            raise RuntimeError("simulated capture failure")
        return self.dataset

    def acquire(self):
        with managed_capture_dataset(self.setup, 1):
            values = np.array([10., 11.])
            if self.image_rows > 1:
                values = np.tile(values, (self.image_rows, 1))
        if self.stop_on == self.capture_count:
            self.stop.set()
        return np.array([700.1234567890123, 701.2345678901234]), values


def build_worker(tmp_path, monkeypatch, *, points=5, image_rows=1, stop_on=None, fail_on=None,
                 nested=False, order=None, recipes=None, writer_class=None):
    stop = threading.Event()
    device_events = []
    device = interleaving._Device(device_events)
    stage = interleaving._Axis("stage", device_events)
    rotation = interleaving._Axis("rotation", device_events)
    if nested:
        sequence, rows, schedule = interleaving.DualGateInterleavingRegressionTests().plan(order=order)
    else:
        sequence = recipes or [{"Center Wavelength (nm)": 700.1234567890123,
                               "Exposure Time (ms)": 10.1234567890123,
                               "Accumulations (EPF)": 3}]
        rows = pd.DataFrame([smu_fixtures.SMUResilienceTests._batch_row(frames=points, Vbg_stop=points - 1)])
        schedule = None
    run = ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite").begin(
        "dual_gate_sweep", "sample", settings={"sequence": sequence, "batch": rows.to_dict("records")})
    detector = Detector(run, stop, image_rows=image_rows, stop_on=stop_on, fail_on=fail_on)
    metadata = smu_fixtures.SMUResilienceTests._run_meta()
    metadata.update(initial_voltage_settle_s=0., voltage_settle_s=0., measurement_mode="Ref",
                    spectrometer_defaults={"Center Wavelength (nm)": 700.1234567890123,
                                           "Exposure Time (ms)": 10.1234567890123,
                                           "Accumulations (EPF)": 3})
    worker = panel._RunWorker(
        sequence, rows, lf6_ctrl=detector, smu_ctrl=SimpleNamespace(is_connected=True, device=device),
        stage_ctrl=SimpleNamespace(is_connected=True, adapter=stage),
        rotation_ctrl=SimpleNamespace(is_connected=lambda _: True, adapter=lambda _: rotation),
        out_dir=tmp_path, run_meta=metadata,
        filename_parts=["device_id", "center", "exposure", "stage_position", "rotation1"],
        stop_event=stop, acquisition_schedule=schedule, metadata_run=run)
    finished = []
    worker.finished.connect(lambda *args: finished.append(args))
    monkeypatch.setattr(cfg.ramp, "delay_s", 0.)
    monkeypatch.setattr(cfg.ramp, "settle_s", 0.)
    if writer_class is not None:
        monkeypatch.setattr("app.engine.csv_writer.CSVWriter", writer_class)
    return SimpleNamespace(worker=worker, run=run, detector=detector, stop=stop, finished=finished,
                           directory=tmp_path, device_events=device_events)


def file_events(run):
    return [event for event in journal(run) if event["event"] in {
        "file_started", "file_completed", "file_stopped", "file_failed"}]


def assert_no_capture_events(run):
    assert not [event for event in journal(run) if event["event"] in {
        "capture_started", "capture_completed", "capture_failed"}]


def test_one_file_persists_full_precision_before_first_capture_and_summary_after_close(tmp_path, monkeypatch):
    closed_paths = set()

    class ObservedWriter(CSVWriter):
        def close(self):
            super().close()
            closed_paths.add(self.path.resolve())

    test = build_worker(tmp_path, monkeypatch, points=7, writer_class=ObservedWriter)
    durable_before_acquire = []

    def before_capture(_index):
        events = file_events(test.run)
        assert [event["event"] for event in events] == ["file_started"]
        saved = test.run.service.load_metadata(test.run.path)
        snapshots = saved["settings"]["snapshots"]
        assert len(snapshots) == 1
        values = scalar_values(snapshots)
        assert 700.1234567890123 in values
        assert 10.1234567890123 in values
        assert 701.2345678901234 in values
        durable_before_acquire.append(events[0]["file_id"])

    test.detector.before_capture = before_capture
    original_event = test.run.record_event

    def record_event(event, **values):
        if event == "file_completed":
            assert (tmp_path / values["output"]["file"]).resolve() in closed_paths
        return original_event(event, **values)

    monkeypatch.setattr(test.run, "record_event", record_event)
    test.worker.run()
    assert test.finished[-1][0], test.finished
    started, completed = file_events(test.run)
    assert [started["event"], completed["event"]] == ["file_started", "file_completed"]
    assert completed["file_id"] == started["file_id"]
    assert completed["completed_points"] == completed["csv_rows"] == completed["planned_points"] == 7
    assert completed["last_point_index"] == 7
    assert len(durable_before_acquire) == test.detector.capture_count == 7
    assert len(set(durable_before_acquire)) == 1
    assert len(test.detector.snapshots) == 1
    assert_no_capture_events(test.run)


def test_full_sensor_counts_points_separately_from_csv_rows(tmp_path, monkeypatch):
    test = build_worker(tmp_path, monkeypatch, points=3, image_rows=4)
    test.worker.run()
    assert test.finished[-1][0], test.finished
    completed = next(event for event in file_events(test.run) if event["event"] == "file_completed")
    assert completed["completed_points"] == completed["planned_points"] == 3
    assert completed["csv_rows"] == 12
    with (tmp_path / completed["output"]["file"]).open(newline="", encoding="utf-8-sig") as stream:
        assert len(list(csv.DictReader(stream))) == 12
    assert_no_capture_events(test.run)


@pytest.mark.parametrize("outcome", ["stopped", "failed"])
def test_interruption_records_partial_file_before_run_finalization(tmp_path, monkeypatch, outcome):
    test = build_worker(tmp_path, monkeypatch, points=5,
                        stop_on=3 if outcome == "stopped" else None,
                        fail_on=3 if outcome == "failed" else None)
    finalizing_events = []
    test.worker.finalizing.connect(lambda: finalizing_events.extend(journal(test.run)))
    test.worker.run()
    assert not test.finished[-1][0], test.finished
    events = file_events(test.run)
    assert [event["event"] for event in events] == ["file_started", f"file_{outcome}"]
    terminal = events[-1]
    assert terminal["completed_points"] == terminal["csv_rows"] == 2
    assert terminal["planned_points"] == 5
    assert terminal["last_point_index"] == 2
    assert any(event["event"] == "run_interrupted" for event in finalizing_events)
    assert any(event["event"] == f"file_{outcome}" for event in finalizing_events)
    assert len([event for event in journal(test.run) if event["event"] == "run_interrupted"]) == 1
    assert test.run.metadata["status"] == ("cancelled" if outcome == "stopped" else "failed")
    assert_no_capture_events(test.run)


@pytest.mark.parametrize("acquire_error", [False, True])
def test_close_failure_never_announces_complete_and_preserves_primary_error(tmp_path, monkeypatch, acquire_error):
    class FailedCloseWriter(CSVWriter):
        def close(self):
            super().close()
            raise OSError("simulated CSV close failure")

    test = build_worker(tmp_path, monkeypatch, points=4, fail_on=3 if acquire_error else None,
                        writer_class=FailedCloseWriter)
    test.worker.run()
    assert not test.finished[-1][0], test.finished
    events = file_events(test.run)
    assert [event["event"] for event in events] == ["file_started", "file_failed"]
    terminal = events[-1]
    assert terminal["completed_points"] == (2 if acquire_error else 4)
    assert terminal["csv_closed"] is False
    assert "simulated CSV close failure" in terminal["close_error"]
    if acquire_error:
        assert "simulated capture failure" in terminal["error"]
    assert test.run.metadata["status"] == "failed"


@pytest.mark.parametrize("order", [
    ["group:1", "group:2", "conditions", "points"],
    ["group:1", "conditions", "points", "group:2"],
])
def test_nested_and_interleaved_streams_have_independent_file_counts(tmp_path, monkeypatch, order):
    test = build_worker(tmp_path, monkeypatch, nested=True, order=order)
    test.worker.run()
    assert test.finished[-1][0], test.finished
    events = file_events(test.run)
    started = {event["file_id"]: event for event in events if event["event"] == "file_started"}
    completed = {event["file_id"]: event for event in events if event["event"] == "file_completed"}
    assert len(started) == len(completed) == 8
    assert set(started) == set(completed)
    assert len({event["output"]["file"] for event in started.values()}) == 8
    for file_id, terminal in completed.items():
        assert terminal["output"]["file"] == started[file_id]["output"]["file"]
        assert terminal["completed_points"] == terminal["csv_rows"] == terminal["planned_points"] == 3
        assert terminal["last_point_index"] == 3
    assert sum(event["completed_points"] for event in completed.values()) == test.detector.capture_count == 24
    assert len(test.detector.snapshots) == 8
    assert_no_capture_events(test.run)


@pytest.mark.parametrize("outcome", ["stopped", "failed"])
def test_interleaved_interruption_closes_every_partial_stream(tmp_path, monkeypatch, outcome):
    test = build_worker(tmp_path, monkeypatch, nested=True,
                        order=["group:1", "conditions", "points", "group:2"],
                        stop_on=3 if outcome == "stopped" else None,
                        fail_on=3 if outcome == "failed" else None)
    test.worker.run()
    assert not test.finished[-1][0], test.finished
    events = file_events(test.run)
    started = {event["file_id"]: event for event in events if event["event"] == "file_started"}
    partial = {event["file_id"]: event for event in events if event["event"] == f"file_{outcome}"}
    assert len(started) >= 2
    assert set(started) == set(partial)
    assert not any(event["event"] == "file_completed" for event in events)
    assert sum(event["completed_points"] for event in partial.values()) == 2
    assert all(event["planned_points"] == 3 for event in partial.values())
    assert all(event["csv_rows"] == event["completed_points"] for event in partial.values())
    assert all(event["csv_closed"] for event in partial.values())
    interruption = next(event for event in journal(test.run) if event["event"] == "run_interrupted")
    assert {event["file_id"] for event in interruption["active_files"]} == set(started)
    assert test.run.metadata["summary"]["completed_points"] == 2
    assert_no_capture_events(test.run)


@pytest.mark.parametrize("change_setup", [False, True])
def test_recipe_or_setup_change_preserves_distinct_durable_settings(tmp_path, monkeypatch, change_setup):
    recipes = [
        {"Center Wavelength (nm)": 700.1234567890123, "Exposure Time (ms)": 10.1234567890123,
         "Accumulations (EPF)": 3},
        {"Center Wavelength (nm)": 810.9876543210987, "Exposure Time (ms)": 25.9876543210987,
         "Accumulations (EPF)": 5},
    ]
    if change_setup:
        recipes[0]["Measurement setup"] = "lightfield"
        recipes[1]["Measurement setup"] = "winspec_ingaas"
    test = build_worker(tmp_path, monkeypatch, points=2, recipes=recipes)
    test.worker.run()
    assert test.finished[-1][0], test.finished
    starts = [event for event in file_events(test.run) if event["event"] == "file_started"]
    assert len(starts) == 2
    assert starts[0]["settings_id"] != starts[1]["settings_id"]
    snapshots = test.run.service.load_metadata(test.run.path)["settings"]["snapshots"]
    assert len(snapshots) == 2
    values = scalar_values(snapshots)
    assert all(recipe["Center Wavelength (nm)"] in values for recipe in recipes)
    assert all(recipe["Exposure Time (ms)"] in values for recipe in recipes)
    if change_setup:
        assert "winspec_ingaas" in values and "lightfield" in values
    assert_no_capture_events(test.run)


@pytest.mark.parametrize("revision", ["configuration", "setup_identity", "adapter_identity"])
def test_open_file_refreshes_changed_settings_before_next_capture(tmp_path, monkeypatch, revision):
    test = build_worker(tmp_path, monkeypatch, points=3)
    next_exposure = 23.9876543210987
    seen_before_capture = []
    original_acquire = test.detector.adapter.acquire

    def acquire():
        data = original_acquire()
        if test.detector.capture_count == 1:
            # Simulate a verified settings update between two points without
            # closing the stream. Identity changes must also invalidate the
            # settings even when the explicit generation is unchanged.
            test.detector.configure_for_acquisition(
                center_nm=test.detector.settings["center_nm"], exposure_ms=next_exposure,
                frames=test.detector.settings["frames"])
            if revision == "configuration":
                test.worker._file_metadata.configuration_changed()
            elif revision == "setup_identity":
                test.detector.setup = SimpleNamespace(**vars(test.detector.setup))
            else:
                test.detector.adapter = SimpleNamespace(**vars(test.detector.adapter))
        return data

    def before_capture(index):
        events = [event for event in journal(test.run)
                  if event["event"] in {"file_started", "file_settings_changed"}]
        expected = ["file_started"] if index == 1 else ["file_started", "file_settings_changed"]
        assert [event["event"] for event in events] == expected
        assert len({event["file_id"] for event in events}) == 1
        saved = test.run.service.load_metadata(test.run.path)
        snapshots = saved["settings"]["snapshots"]
        assert len(snapshots) == (1 if index == 1 else 2)
        active = next(item["values"] for item in snapshots
                      if item["settings_id"] == events[-1]["settings_id"])
        assert active["acquisition"]["exposure_ms"] == (
            10.1234567890123 if index == 1 else next_exposure)
        seen_before_capture.append(events[-1]["settings_id"])

    test.detector.adapter.acquire = acquire
    test.detector.before_capture = before_capture
    test.worker.run()
    assert test.finished[-1][0], test.finished
    events = [event for event in journal(test.run)
              if event["event"] in {"file_started", "file_settings_changed", "file_completed"}]
    assert [event["event"] for event in events] == [
        "file_started", "file_settings_changed", "file_completed"]
    assert len({event["file_id"] for event in events}) == 1
    assert len({event["output"]["file"] for event in events}) == 1
    assert seen_before_capture[0] != seen_before_capture[1] == seen_before_capture[2]
    assert len(test.detector.snapshots) == 2
    assert events[-1]["completed_points"] == events[-1]["csv_rows"] == events[-1]["planned_points"] == 3
    assert events[-1]["settings_id"] == seen_before_capture[-1]
    assert_no_capture_events(test.run)


def test_failed_file_started_record_blocks_first_capture(tmp_path, monkeypatch):
    test = build_worker(tmp_path, monkeypatch)
    original_event = test.run.record_event
    monkeypatch.setattr(test.run, "record_event", lambda event, **values:
                        None if event == "file_started" else original_event(event, **values))
    test.worker.run()
    assert not test.finished[-1][0], test.finished
    assert test.detector.capture_count == 0
    assert not [event for event in journal(test.run) if event["event"] == "file_completed"]


@pytest.mark.parametrize("cancel_during", ["snapshot", "file_started"])
def test_cancellation_during_file_metadata_stops_before_acquire_and_cleanup(tmp_path, monkeypatch, cancel_during):
    cleanup_events = []

    class ObservedWriter(CSVWriter):
        def close(self):
            # The interruption must already be durable before potentially slow
            # file close and hardware cleanup begin.
            events = journal(test.run)
            assert [event["event"] for event in events].count("run_interrupted") == 1
            interruption = next(event for event in events if event["event"] == "run_interrupted")
            assert interruption["status"] == "stopped"
            cleanup_events.append("close")
            super().close()

    test = build_worker(tmp_path, monkeypatch, writer_class=ObservedWriter)
    original_snapshot = test.detector.setup.read_metadata_snapshot
    original_event = test.run.record_event

    def read_snapshot():
        result = original_snapshot()
        if cancel_during == "snapshot":
            test.stop.set()
        return result

    def record_event(event, **values):
        result = original_event(event, **values)
        if event == "file_started" and cancel_during == "file_started":
            test.stop.set()
        return result

    monkeypatch.setattr(test.detector.setup, "read_metadata_snapshot", read_snapshot)
    monkeypatch.setattr(test.run, "record_event", record_event)
    device = test.worker._smu.device
    original_zero = device.ramp_all_to_zero_report

    def zero(**kwargs):
        assert any(event["event"] == "file_stopped" for event in journal(test.run))
        cleanup_events.append("zero")
        return original_zero(**kwargs)

    monkeypatch.setattr(device, "ramp_all_to_zero_report", zero)
    test.worker.run()
    assert not test.finished[-1][0], test.finished
    assert test.detector.capture_count == 0
    events = file_events(test.run)
    assert [event["event"] for event in events] == ["file_started", "file_stopped"]
    assert events[-1]["completed_points"] == events[-1]["csv_rows"] == 0
    assert events[-1].get("last_point_index") is None
    assert cleanup_events == ["close", "zero"]
    assert test.run.metadata["status"] == "cancelled"


def test_snapshot_read_error_stops_before_first_acquire(tmp_path, monkeypatch):
    test = build_worker(tmp_path, monkeypatch)
    monkeypatch.setattr(test.detector.setup, "read_metadata_snapshot",
                        Mock(side_effect=OSError("simulated snapshot read failure")))
    test.worker.run()
    assert not test.finished[-1][0], test.finished
    assert test.detector.capture_count == 0
    assert test.run.metadata["status"] == "failed"
    interruption = next(event for event in journal(test.run) if event["event"] == "run_interrupted")
    assert interruption["status"] == "failed"
    assert "simulated snapshot read failure" in interruption["reason"]
    assert not any(event["event"] == "file_completed" for event in journal(test.run))
