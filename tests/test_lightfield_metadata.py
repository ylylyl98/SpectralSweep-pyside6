from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.experiment_metadata import ExperimentMetadataService
from app.lightfield_metadata import (
    FIELDS, LightFieldRecorder, bind_lightfield_metadata, capture_with_metadata,
    read_snapshot, set_lightfield_context,
)


class LightFieldMetadataTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.run = ExperimentMetadataService(root, root / "history.sqlite").begin(
            "motion_sweep", "sample", settings={"exposure_ms": 999})
        self.namespaces = {name: SimpleNamespace() for name in ("camera", "spectrometer", "experiment")}
        self.values = {}
        for _, _, namespace, member, convert in FIELDS:
            setattr(self.namespaces[namespace], member, member)
            self.values[member] = "Average" if convert is str else 2
        self.values["ShutterTimingExposureTime"] = 25.5
        self.values["SensorTemperatureReading"] = -70.0
        self.experiment = SimpleNamespace(
            Exists=lambda key: key in self.values,
            GetValue=lambda key: self.values[key],
            Name="PL experiment",
            ExperimentDevices=[SimpleNamespace(Type="Camera", Model="PIXIS", SerialNumber="123")],
            SelectedRegions=[SimpleNamespace(X=0, Y=10, Width=100, Height=20, XBinning=2, YBinning=20)],
            SystemColumnCalibration=[700.0, 701.0],
            Capture=Mock(return_value=SimpleNamespace(GetFrame=lambda *_: object())),
        )
        self.setup = SimpleNamespace(experiment=self.experiment, _frame_dims=lambda _: (50, 1))
        self.setup.read_metadata_snapshot = lambda: read_snapshot(self.setup, **self.namespaces)
        self.setup.bind_metadata_run = lambda run: setattr(self.setup, "_metadata_recorder", LightFieldRecorder(run))
        self.controller = SimpleNamespace(setup=self.setup)
        bind_lightfield_metadata(self.controller, self.run)

    def records(self):
        return [json.loads(line) for line in self.setup._metadata_recorder.path.read_text().splitlines()]

    def test_actual_readbacks_roi_and_unavailable_fields_are_json_safe(self):
        del self.values["AdcEMGain"]
        delattr(self.namespaces["camera"], "AdcSpeed")
        result = self.setup.read_metadata_snapshot()
        self.assertEqual(result["acquisition"]["exposure_ms"], 25.5)
        self.assertEqual(result["acquisition"]["combination_method"], "Average")
        self.assertEqual(result["detector"]["regions"][0]["y_binning"], 20)
        self.assertEqual(result["provenance"]["devices"][0]["serial_number"], "123")
        self.assertIsNone(result["camera"]["em_gain"])
        self.assertIn("camera.em_gain", result["unavailable"])
        self.assertIn("camera.adc_speed_mhz", result["unavailable"])
        json.dumps(result, allow_nan=False)

    def test_one_failed_getter_does_not_lose_other_readbacks(self):
        def get(key):
            if key == "AdcEMGain":
                raise RuntimeError("device busy")
            return self.values[key]
        self.experiment.GetValue = get
        result = self.setup.read_metadata_snapshot()
        self.assertIn("device busy", result["unavailable"]["camera.em_gain"])
        self.assertEqual(result["spectrometer"]["center_wavelength_nm"], 2.0)

    def test_settings_deduplicated_temperature_and_point_context_retained(self):
        for point in (1, 2):
            set_lightfield_context(self.controller, output_file=self.run.path.parent / "data.csv", point_index=point)
            capture_with_metadata(self.setup, 1)
            self.values["SensorTemperatureReading"] = -69.5
        self.values["ShutterTimingExposureTime"] = 50
        capture_with_metadata(self.setup, 3)
        self.run.complete()
        metadata = json.loads(self.run.path.read_text())
        snapshots = metadata["observed"]["lightfield"]["settings_snapshots"]
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(metadata["settings"]["requested"]["exposure_ms"], 999)
        records = self.records()
        starts = records[::2]
        self.assertEqual([r["settings_id"] for r in starts], [1, 1, 2])
        self.assertEqual([r["temperature_c"] for r in starts], [-70, -69.5, -69.5])
        self.assertEqual(starts[1]["context"], {"output_file": "data.csv", "point_index": 2})
        self.assertEqual(starts[2]["capture_frames_requested"], 3)
        self.assertEqual(records[1]["output_dimensions"], {"width": 50, "height": 1})
        entry = next(f for f in metadata["files"] if f.get("kind") == "lightfield_acquisitions")
        self.assertFalse(Path(entry["path"]).is_absolute())

    def test_capture_error_preserved_and_logged(self):
        error = RuntimeError("capture failed")
        self.experiment.Capture.side_effect = error
        with self.assertRaises(RuntimeError) as caught:
            capture_with_metadata(self.setup, 1)
        self.assertIs(caught.exception, error)
        self.assertEqual(self.records()[-1]["event"], "capture_failed")

    def test_context_uuid_is_the_durable_capture_id(self):
        set_lightfield_context(self.controller, acquisition_id="measurement-uuid-1", purpose="measurement")
        capture_with_metadata(self.setup, 1)
        started = next(item for item in self.records() if item["event"] == "capture_started")
        finished = next(item for item in self.records() if item["event"] == "capture_completed")
        self.assertEqual(started["acquisition_id"], "measurement-uuid-1")
        self.assertEqual(finished["acquisition_id"], "measurement-uuid-1")

    def test_optional_snapshot_failure_does_not_stop_capture(self):
        self.setup.read_metadata_snapshot = Mock(side_effect=RuntimeError("readback failed"))
        with self.assertLogs("app.lightfield_metadata", level="WARNING"):
            dataset = capture_with_metadata(self.setup, 1)
        self.assertIs(dataset, self.experiment.Capture.return_value)

    def test_optional_storage_failure_does_not_stop_capture(self):
        self.setup._metadata_recorder._append = Mock(side_effect=OSError("disk full"))
        with self.assertLogs("app.lightfield_metadata", level="WARNING"):
            dataset = capture_with_metadata(self.setup, 1)
        self.assertIs(dataset, self.experiment.Capture.return_value)

    def test_terminal_run_not_read_or_written_and_new_run_isolated(self):
        capture_with_metadata(self.setup, 1, purpose="warmup")
        self.run.cancel()
        before = self.run.path.read_bytes()
        self.setup.read_metadata_snapshot = Mock(side_effect=AssertionError("late read"))
        capture_with_metadata(self.setup, 1)
        self.setup.read_metadata_snapshot.assert_not_called()
        self.assertEqual(self.run.path.read_bytes(), before)
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(self.records()[0]["purpose"], "warmup")
        next_run = self.run.service.begin("motion_sweep", "next")
        bind_lightfield_metadata(self.controller, next_run)
        self.assertEqual(self.setup._metadata_recorder._index, 0)
        self.assertNotEqual(self.setup._metadata_recorder.path, self.run.path)

    def test_other_backends_are_untouched(self):
        controller = SimpleNamespace(setup=SimpleNamespace())
        bind_lightfield_metadata(controller, self.run)
        set_lightfield_context(controller, point_index=1)
        self.assertEqual(self.run.metadata["observed"], {})


if __name__ == "__main__":
    unittest.main()
