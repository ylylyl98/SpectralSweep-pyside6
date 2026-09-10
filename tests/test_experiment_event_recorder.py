from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

import numpy as np
import pandas as pd

from app.experiment_metadata import ExperimentMetadataService, ExperimentRun, _from_jsonable, _jsonable


class ExperimentEventRecorderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = ExperimentMetadataService(self.root, self.root / "history.sqlite")

    def test_large_typed_array_round_trips_without_truncation(self):
        source = np.arange(1207, dtype=np.float64).reshape(17, 71)
        encoded = _jsonable(source)
        self.assertEqual(encoded["__type__"], "ndarray")
        self.assertEqual(encoded["shape"], [17, 71])
        self.assertEqual(len(encoded["data"]), 17)
        restored = _from_jsonable(json.loads(json.dumps(encoded)))
        np.testing.assert_array_equal(restored, source)

    def test_snapshot_dedup_and_capture_event_associations(self):
        run = self.service.begin("motion_sweep", "sample", settings={"requested": {"x": 1}})
        one = run.register_settings_snapshot({"exposure_ms": 10, "temperature_c": -70})
        again = run.register_settings_snapshot({"exposure_ms": 10, "temperature_c": -70})
        self.assertEqual(one, again)
        first = run.record_capture(acquisition_id="a-1", settings_id=one,
                                   purpose="warmup", output={"file": "data.csv"})
        second = run.record_capture(acquisition_id="a-2", settings_id=one,
                                    purpose="measurement", output={"file": "data.csv"})
        self.assertNotEqual(first["event_id"], second["event_id"])
        run.complete()
        events = run.read_events()
        self.assertEqual([event["purpose"] for event in events], ["warmup", "measurement"])
        self.assertEqual({event["acquisition_id"] for event in events}, {"a-1", "a-2"})
        self.assertEqual(run.metadata["acquisitions"]["count"], 2)
        self.assertEqual(len(run.metadata["settings"]["snapshots"]), 1)

    def test_partial_log_is_readable_and_terminal_run_rejects_late_event(self):
        run = self.service.begin("spectrum_preview", "sample")
        run.record_event("condition_started", condition_id="c-1")
        with run.event_path.open("a", encoding="utf-8") as stream:
            stream.write('{"event":"truncated"')
        run.cancel("user stop")
        self.assertEqual(len(run.read_events()), 1)
        self.assertIsNone(run.record_event("late_export"))
        self.assertEqual(len(run.read_events()), 1)

    def test_legacy_sidecar_migration_is_read_only_and_loadable(self):
        path = self.root / "legacy.json"
        path.write_text(json.dumps({"schema_version": 1, "experiment_id": "old",
                                    "settings": {"exposure_ms": 3}}), encoding="utf-8")
        loaded = ExperimentMetadataService.load_metadata(path)
        self.assertEqual(loaded["run_id"], "old")
        self.assertEqual(loaded["settings"]["applied"], {})
        self.assertFalse((self.root / "legacy.events.jsonl").exists())

    def test_external_inputs_keep_distinct_portable_references(self):
        outside_temp = tempfile.TemporaryDirectory(dir=self.root.parent)
        self.addCleanup(outside_temp.cleanup)
        outside = Path(outside_temp.name)
        first = outside / "same-name.csv"
        second = outside / "same-name.csv"
        # Separate directories exercise the portable reference collision path.
        second_dir = outside / "other"
        second_dir.mkdir()
        second = second_dir / "same-name.csv"
        first.write_text("a\n", encoding="utf-8")
        second.write_text("b\n", encoding="utf-8")
        run = self.service.begin("bfp_binned_rc", "sample")
        run.register_file(first, role="metadata", external=True)
        run.register_file(second, role="metadata", external=True)
        refs = [item for item in run.metadata["files"] if item.get("external")]
        self.assertEqual(len(refs), 2)
        self.assertNotEqual(refs[0]["path"], refs[1]["path"])

    def test_frozen_external_identity_survives_source_replacement(self):
        source = self.root.parent / f"frozen-{id(self)}.csv"
        self.addCleanup(lambda: source.unlink(missing_ok=True))
        source.write_text("bytes used by compute\n", encoding="utf-8")
        import hashlib
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        identity = {"sha256": digest, "size_bytes": source.stat().st_size,
                    "captured_utc": "2026-01-01T00:00:00Z"}
        run = self.service.begin("bfp_binned_rc", "sample")
        source.write_text("replacement bytes\n", encoding="utf-8")
        ref = run.register_file(source, role="metadata", external=True,
                                frozen_identity=identity)
        item = next(entry for entry in run.metadata["files"] if entry["path"] == ref)
        self.assertEqual(item["sha256"], digest)
        self.assertEqual(item["size_bytes"], identity["size_bytes"])

    def test_compatibility_state_reconstructs_each_legacy_file_after_reopen(self):
        run = self.service.begin("mcd", "sample")
        run.record_compatibility_state("condition-1.meta.json", {
            "status": "running", "spectra": [1], "temperature_k": 4.0})
        run.record_compatibility_state("condition-1.meta.json", {
            "status": "complete", "spectra": [1, 2], "temperature_k": 4.0})
        run.record_compatibility_state("condition-2.meta.json", {
            "status": "failed", "spectra": [], "temperature_k": 5.0})
        path = run.path
        del run
        import gc
        gc.collect()
        reopened = self.service.open_run(path)
        self.assertEqual(reopened.reconstruct_compatibility_state("condition-1.meta.json"), {
            "status": "complete", "spectra": [1, 2], "temperature_k": 4.0})
        self.assertEqual(reopened.reconstruct_compatibility_state("condition-2.meta.json")["temperature_k"], 5.0)

    def test_dataframe_round_trip_preserves_table_schema(self):
        source = pd.DataFrame({"voltage": np.array([1.0, 2.0]), "enabled": [True, False]})
        restored = _from_jsonable(json.loads(json.dumps(_jsonable(source))))
        self.assertIsInstance(restored, pd.DataFrame)
        self.assertEqual(list(restored.columns), ["voltage", "enabled"])
        self.assertEqual(restored.to_dict("records"), source.to_dict("records"))

    def test_reopen_rebuilds_uncheckpointed_capture_summary(self):
        run = self.service.begin("motion_sweep", "sample")
        for index in range(3):
            run.record_capture(acquisition_id=f"a-{index}")
        path = run.path
        del run
        import gc
        gc.collect()
        reopened = self.service.open_run(path)
        self.assertEqual(reopened.metadata["acquisitions"]["count"], 3)
        self.assertEqual(reopened.metadata["acquisitions"]["recent_ids"], ["a-0", "a-1", "a-2"])

    def test_append_failure_marks_run_degraded_and_fails_terminalization(self):
        run = self.service.begin("motion_sweep", "sample")
        def fail_open(*args, **kwargs):
            raise OSError("disk full")
        with mock.patch("pathlib.Path.open", side_effect=fail_open):
            self.assertIsNone(run.record_event("capture", acquisition_id="lost"))
        result = run.complete()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(run.metadata["metadata_status"], "degraded")

    def test_reopen_adds_delimiter_to_valid_json_tail(self):
        run = self.service.begin("motion_sweep", "sample")
        run.record_event("first")
        path, event_path = run.path, run.event_path
        del run
        import gc
        gc.collect()
        event_path.write_bytes(event_path.read_bytes().rstrip(b"\n"))
        reopened = self.service.open_run(path)
        reopened.record_event("second")
        self.assertEqual([item["event"] for item in reopened.read_events()], ["first", "second"])
        self.assertEqual([item["event_index"] for item in reopened.read_events()], [1, 2])

    def test_checkpoint_failure_keeps_next_event_index(self):
        run = self.service.begin("motion_sweep", "sample")
        run._checkpoint_interval = 1
        run.record_event("first")
        original = run._write
        run._write = mock.Mock(side_effect=OSError("checkpoint unavailable"))
        self.assertIsNone(run.record_event("second"))
        run._write = original
        self.assertIsNotNone(run.record_event("third"))
        indexes = [item["event_index"] for item in run.read_events()]
        self.assertEqual(indexes, [1, 2, 3])

    def test_concurrent_open_run_returns_one_writer(self):
        run = self.service.begin("motion_sweep", "sample")
        path = run.path
        del run
        import gc
        gc.collect()
        barrier = threading.Barrier(2)

        results = []
        original_init = ExperimentRun.__init__
        def delayed_init(instance, *args, **kwargs):
            time.sleep(0.02)
            original_init(instance, *args, **kwargs)
        def collect():
            barrier.wait()
            results.append(self.service.open_run(path))
        with mock.patch.object(ExperimentRun, "__init__", delayed_init):
            threads = [threading.Thread(target=collect) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
        self.assertEqual(len(results), 2)
        self.assertIs(results[0], results[1])


if __name__ == "__main__":
    unittest.main()
