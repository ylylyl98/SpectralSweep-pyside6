import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import app.experiment_metadata as experiment_metadata
from app.experiment_metadata import (
    CANCELLED,
    COMPLETED,
    FAILED,
    ExperimentMetadataService,
)
from app.experiment_lifecycle import subscribe, unsubscribe


class ExperimentMetadataContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = ExperimentMetadataService(self.root, self.root / "history.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def test_terminal_event_once_after_durable_write_and_listener_isolation(self):
        events = []
        def bad(event): raise RuntimeError("listener")
        subscribe(bad); subscribe(events.append)
        try:
            run = self.service.begin("motion_sweep", "D1", settings={})
            run.complete()
            self.assertEqual([(e.experiment_id, e.experiment_type, e.status) for e in events],
                             [(run.experiment_id, "motion_sweep", COMPLETED)])
            self.assertEqual(json.loads(run.path.read_text())["status"], COMPLETED)
        finally:
            unsubscribe(bad); unsubscribe(events.append)

    def test_completed_sidecar_is_portable_and_indexed(self):
        run = self.service.begin(
            "motion_sweep", "D237",
            settings={"exposure_ms": 20, "maximum_field_t": 9, "output_dir": str(self.root)},
        )
        data = self.root / "raw" / "sweep.csv"
        data.parent.mkdir()
        data.write_text("x,y\n1,2\n", encoding="utf-8")
        run.register_file(data, "raw")
        run.complete({"rows": 1})
        metadata = json.loads(run.path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["schema_version"], 1)
        self.assertEqual(metadata["status"], COMPLETED)
        self.assertEqual(metadata["device_id"], "D237")
        self.assertEqual(metadata["experiment_type"], "motion_sweep")
        self.assertTrue(all(not Path(item["path"]).is_absolute() for item in metadata["files"]))
        self.assertEqual(len(self.service.query_history("D237", "motion_sweep")), 1)
        history_row = self.service.query_history("D237", "motion_sweep")[0]
        self.assertEqual(Path(history_row["metadata_path"]), run.path.resolve())
        self.assertEqual(metadata["software"]["version"], None)
        self.assertIn("summary", metadata["result"])
        self.assertIn("cancellation", metadata["result"])
        self.assertIn("error", metadata["result"])
        self.assertEqual(metadata["settings"]["requested"]["maximum_field_t"], 9)
        self.assertEqual(metadata["settings"]["requested"]["output_dir"], ".")
        self.assertNotIn("maximum_field_t", metadata["settings"]["loadable"])

    def test_begin_persists_initial_metadata_once_with_sidecar_registered(self):
        with patch(
            "app.experiment_metadata._atomic_json",
            wraps=experiment_metadata._atomic_json,
        ) as atomic_json:
            run = self.service.begin("motion_sweep", "D-once")

        self.assertEqual(atomic_json.call_count, 1)
        metadata = json.loads(run.path.read_text(encoding="utf-8"))
        self.assertEqual(
            metadata["files"],
            [{
                "path": run.path.resolve().relative_to(self.root.resolve()).as_posix(),
                "role": "metadata",
                "kind": "experiment_metadata",
            }],
        )

    def test_history_connection_closes_if_schema_setup_fails(self):
        connection = MagicMock()
        connection.execute.side_effect = sqlite3.OperationalError("schema failure")
        with patch("app.experiment_metadata.sqlite3.connect", return_value=connection):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.history._connect()
        connection.close.assert_called_once_with()

    def test_history_schema_setup_is_reused_after_first_connection(self):
        first = MagicMock()
        second = MagicMock()
        with patch(
            "app.experiment_metadata.sqlite3.connect",
            side_effect=[first, second],
        ):
            self.service.history._connect()
            self.service.history._connect()

        self.assertEqual(first.execute.call_count, 3)
        second.execute.assert_not_called()

    def test_cancelled_and_failed_records_are_retained(self):
        cancelled = self.service.begin("x", "D1")
        cancelled.cancel("user stop")
        failed = self.service.begin("x", "D1")
        failed.fail(RuntimeError("hardware unavailable"))
        records = self.service.query_history("D1", "x")
        self.assertEqual({row["status"] for row in records}, {CANCELLED, FAILED})
        self.assertEqual(json.loads(cancelled.path.read_text())["cancellation_reason"], "user stop")
        self.assertEqual(json.loads(failed.path.read_text())["error"]["type"], "RuntimeError")

    def test_history_failure_does_not_remove_sidecar(self):
        run = self.service.begin("x", "D1")
        # Replace the index path with a directory after the sidecar exists.
        db_path = self.root / "broken"
        db_path.mkdir()
        run.service.history.path = db_path
        run.complete()
        self.assertTrue(run.path.exists())
        self.assertEqual(json.loads(run.path.read_text())["status"], COMPLETED)

    def test_terminal_transition_is_locked(self):
        run = self.service.begin("x", "D1")
        run.complete()
        with self.assertRaises(RuntimeError):
            run.cancel("late stop")
        with self.assertRaises(RuntimeError):
            run.register_file(self.root / "late.csv")

    def test_file_details_merge_idempotently_and_history_keeps_one_id(self):
        run = self.service.begin("mcd_attodry2100", "D-detail")
        data = self.root / "g1.csv"
        data.write_text("header\n", encoding="utf-8")
        run.register_file(data, "raw", kind="continuous_mcd_spectrum",
                          details={"condition_index": 1, "direction": "forward"})
        run.register_file(data, "raw", kind="continuous_mcd_spectrum",
                          details={"condition_index": 1, "direction": "forward", "complete": True})
        run.complete()
        metadata = json.loads(run.path.read_text(encoding="utf-8"))
        entries = [item for item in metadata["files"] if item.get("path") == "g1.csv"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["condition_index"], 1)
        self.assertTrue(entries[0]["complete"])
        history = self.service.query_history("D-detail", "mcd_attodry2100")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["experiment_id"], run.experiment_id)

    def test_preview_and_safe_apply_skip_historical_safety_fields(self):
        metadata = {
            "settings": {"requested": {
                "center_nm": 860, "maximum_field_t": 9,
                "interlock_enabled": False, "exposure_ms": 10,
            }}
        }
        preview = self.service.preview_settings(metadata)
        self.assertEqual(preview["loadable"], {"center_nm": 860, "exposure_ms": 10})
        applied = {}
        class Adapter:
            def apply_saved_experiment_settings(self, values):
                applied.update(values)
                return {"applied": list(values), "skipped": []}
        result = self.service.apply_saved_settings(
            metadata, Adapter()
        )
        self.assertEqual(applied, {"center_nm": 860, "exposure_ms": 10})
        self.assertNotIn("maximum_field_t", applied)
        self.assertNotIn("interlock_enabled", applied)

    def test_all_current_workflow_types_share_one_run_id(self):
        types = (
            "dual_gate_sweep", "gate_map_2d", "motion_sweep", "mcd_aps100",
            "mcd_attodry2100", "bfp_acquisition", "bfp_binned_rc", "bfp_full_sensor_rc",
        )
        for index, experiment_type in enumerate(types):
            folder = self.root / experiment_type
            run = ExperimentMetadataService(folder, self.root / "history.sqlite").begin(
                experiment_type, f"D{index}", output_dir=folder, settings={"exposure_ms": 1}
            )
            files = []
            for suffix in (".csv", ".log"):
                path = folder / f"run{suffix}"
                path.write_text("data", encoding="utf-8")
                run.register_file(path, "raw" if suffix == ".csv" else "intermediate")
                files.append(path)
            run.cancel("no data" if index % 2 else None) if index % 2 else run.fail("simulated failure")
            metadata = json.loads(run.path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["experiment_type"], experiment_type)
            self.assertEqual({entry["path"] for entry in metadata["files"] if entry["role"] != "metadata"}, {p.name for p in files})
            self.assertEqual(metadata["metadata_path"], run.path.name)


if __name__ == "__main__":
    unittest.main()
