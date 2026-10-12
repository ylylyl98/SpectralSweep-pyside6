import json
import os
import tempfile
import threading
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from app.experiment_metadata import ExperimentHistory, ExperimentMetadataService


class HistoryPerformanceTests(unittest.TestCase):
    def test_summary_pagination_preserves_search_in_settings_without_decoding_them(self):
        with tempfile.TemporaryDirectory() as root:
            history = ExperimentHistory(Path(root) / "history.sqlite")
            with history._connect() as db:
                for i in range(45):
                    db.execute("INSERT INTO experiments VALUES (?,?,?,?,?,?,?,?,?)", (
                        str(i), "sample", "sweep", f"2026-10-{i:02d}", None,
                        "completed", "file.json", json.dumps({"tag": "unique needle" if i == 2 else "other"}), "{}"))
            db.close()
            page = history.query_page("sample", "sweep", page=1)
            self.assertEqual(len(page["rows"]), 20)
            self.assertEqual(page["total"], 45)
            self.assertNotIn("settings", page["rows"][0])
            found = history.query_page("sample", "sweep", filter_text="UNIQUE NEEDLE", page=99)
            self.assertEqual(found["page"], 0)
            self.assertEqual([r["experiment_id"] for r in found["rows"]], ["2"])
            self.assertEqual(history.query("sample", "sweep", limit=50)[-3]["settings"]["tag"], "unique needle")

    def test_preview_bounds_work_without_modifying_full_metadata(self):
        class CountedList(list):
            visits = 0
            def __iter__(self):
                for item in super().__iter__():
                    self.visits += 1
                    yield item
        outputs = CountedList([{"frames": list(range(2000)), "filename": "x" * 2000}] * 10000)
        metadata = {"plan": {"schedule": outputs}, "settings": {"sample": "D1"}}
        text = ExperimentMetadataService.format_history_preview(metadata)
        self.assertLess(len(text), 100000)
        self.assertLess(outputs.visits, 30)
        self.assertIn("omitted", text)
        self.assertEqual(len(metadata["plan"]["schedule"]), 10000)

    def test_background_tasks_return_immediately_and_ignore_stale_results(self):
        from ui.history_loader import HistoryTaskRunner
        app = QApplication.instance() or QApplication([])
        runner = HistoryTaskRunner()
        release = threading.Event()
        entered = threading.Event()
        delivered = []
        def slow(cancel):
            entered.set()
            release.wait(2)
            return "old"
        start = time.perf_counter()
        runner.submit("list", slow, lambda result, error: delivered.append(result))
        self.assertLess(time.perf_counter() - start, .1)
        self.assertTrue(entered.wait(1))
        runner.submit("list", lambda cancel: "new", lambda result, error: delivered.append(result))
        deadline = time.perf_counter() + 2
        while not delivered and time.perf_counter() < deadline:
            QTest.qWait(10)
        release.set()
        QTest.qWait(50)
        self.assertEqual(delivered, ["new"])
        runner.cancel_all()

    def test_event_pages_do_not_skip_the_second_half_of_twenty_entries(self):
        from ui.history_loader import load_preview
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "run.json"
            journal = Path(root) / "run.events.jsonl"
            path.write_text(json.dumps({"event_log": {"path": journal.name}}))
            for count in (20, 21, 40, 41):
                journal.write_text("\n".join(json.dumps({"event": f"ENTRY_{i:03d}"}) for i in range(count)))
                for offset in range(0, count, 20):
                    result = load_preview(str(path), root, "", offset, threading.Event())
                    for i in range(offset, min(count, offset + 20)):
                        self.assertIn(f"ENTRY_{i:03d}", result["text"])
                    self.assertEqual(result["has_next"], count > offset + 20)

    def test_large_metadata_is_parsed_outside_the_gui_python_process(self):
        from ui.history_loader import load_preview, load_settings
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "large.json"
            path.write_text(json.dumps({"settings": {"requested": {"sample": "big-test"},
                                                    "loadable": {"center_nm": 860}, "padding": "x" * 2_000_000}}))
            with patch.object(ExperimentMetadataService, "load_metadata", side_effect=AssertionError("Large JSON parsed in GUI process")):
                result = load_preview(str(path), root, "", 0, threading.Event())
                settings = load_settings(str(path), root, threading.Event())
            self.assertIn("big-test", result["text"])
            self.assertLess(len(result["text"]), 10000)
            self.assertEqual(settings, {"center_nm": 860})

    def test_large_unsupported_loadable_plan_does_not_return_to_gui(self):
        from ui.history_loader import load_settings
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "plan.json"
            path.write_text(json.dumps({"settings": {"loadable": {
                "center_nm": 860, "executed_plan": [{"record": "x" * 1000}] * 2000}}}))
            settings = load_settings(str(path), root, threading.Event(), supported_keys=["center_nm"])
            self.assertEqual(settings, {"center_nm": 860, "executed_plan": None})
            self.assertLess(len(json.dumps(settings)), 1000)

    def test_adapter_key_query_is_readonly_and_bfp_dispatch_preserves_it(self):
        from tests import test_sweep_line_calculator
        from PySide6.QtWidgets import QWidget
        from ui.presets_panel import PresetsPanel
        from ui.megasweep_panel import MegaSweepPanel
        from ui.power_sweep_panel import PowerSweepPanel
        from ui.mcd_panel import MCDPanel
        from ui.mcd2100_panel import MCD2100Panel
        from ui.bfp_panel_integrated import BFPPanel, _BRCWidget, _FRCWidget
        app = QApplication.instance() or QApplication([])
        def bare(cls):
            widget = cls.__new__(cls)
            QWidget.__init__(widget)
            self.addCleanup(widget.deleteLater)
            return widget
        for cls in (PresetsPanel, MegaSweepPanel, PowerSweepPanel, MCDPanel, MCD2100Panel, _BRCWidget, _FRCWidget):
            widget = bare(cls)
            with self.subTest(panel=cls.__name__):
                before = dict(widget.__dict__)
                keys = widget.apply_saved_experiment_settings({}, keys_only=True)["supported_keys"]
                self.assertTrue(keys)
                self.assertEqual(widget.__dict__, before)
        bfp = bare(BFPPanel)
        bfp._brc, bfp._frc = bare(_BRCWidget), bare(_FRCWidget)
        for index, expected in ((0, "roi_mode"), (1, "scale"), (2, "calculation")):
            bfp._tabs = SimpleNamespace(currentIndex=lambda: index)
            self.assertIn(expected, bfp.apply_saved_experiment_settings({}, keys_only=True)["supported_keys"])


if __name__ == "__main__":
    unittest.main()
