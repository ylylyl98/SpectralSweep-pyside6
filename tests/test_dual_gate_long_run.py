"""Long runs release completed CSVs while preserving interleaved streams."""
import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.engine.csv_writer import CSVWriter
from tests import test_dual_gate_interleaving_regression as fixtures


class NestedStreamLifetimeTests(unittest.TestCase):
    def run_plan(self, folder, order, *, cancel_after=None):
        class TrackedWriter(CSVWriter):
            open_writers = set()
            peak_open = 0

            def _ensure_open(self):
                super()._ensure_open()
                self.open_writers.add(id(self))
                type(self).peak_open = max(type(self).peak_open, len(self.open_writers))

            def close(self):
                super().close()
                self.open_writers.discard(id(self))

        runner = fixtures.DualGateInterleavingRegressionTests()
        with patch("app.engine.csv_writer.CSVWriter", TrackedWriter):
            result = runner.run_worker(folder, order=order, cancel_after=cancel_after)
        self.assertFalse(TrackedWriter.open_writers)
        return result, TrackedWriter.peak_open

    def assert_complete_csvs(self, folder):
        paths = list((Path(folder) / "REF").glob("*.csv"))
        self.assertEqual(len(paths), 8)
        for path in paths:
            with path.open(newline="", encoding="utf-8-sig") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([float(row["Vbg_set"]) for row in rows], [0., 1., 2.], path.name)

    def test_completed_sequential_streams_release_file_handles(self):
        with tempfile.TemporaryDirectory() as folder:
            result, peak = self.run_plan(folder, ["group:1", "group:2", "conditions", "points"])
            _, finished, progress, frames, _ = result
            self.assertTrue(finished[-1][0], finished)
            self.assertEqual(progress[-1], (8, 8))
            self.assertEqual(frames[-1], (24, 24))
            self.assertEqual(peak, 1)
            self.assert_complete_csvs(folder)

    def test_only_unfinished_interleaved_streams_keep_file_handles(self):
        with tempfile.TemporaryDirectory() as folder:
            result, peak = self.run_plan(folder, ["group:1", "conditions", "points", "group:2"])
            self.assertTrue(result[1][-1][0], result[1])
            self.assertEqual(peak, 2)
            self.assert_complete_csvs(folder)

    def test_cancel_closes_unfinished_interleaved_streams(self):
        with tempfile.TemporaryDirectory() as folder:
            result, peak = self.run_plan(
                folder, ["group:1", "conditions", "points", "group:2"], cancel_after=3,
            )
            self.assertIn("stopped", result[1][-1][1].lower())
            self.assertEqual(peak, 2)
            paths = list((Path(folder) / "REF").glob("*.csv"))
            self.assertEqual(len(paths), 2)
            for path in paths:
                with path.open(newline="", encoding="utf-8-sig") as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
