from __future__ import annotations

import inspect
import copy
import concurrent.futures
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QObject, QTimer, Qt, Signal
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QGroupBox, QHeaderView, QScrollArea, QSizePolicy, QStatusBar, QTableWidget, QTabWidget, QWidget

from ui.main_window import MainWindow
from ui.mcd2100_panel import MCD2100Panel, _LightFieldRotationService, _Runner
from controllers.attodry2100_controller import AttoDRY2100Controller
from tests.test_attodry2100_controller import EventGatedAdapter
from utils.mcd_common import MODE_DIRECT, MODE_DOPING_EFIELD, build_condition_batch
from utils.config import AttoDRY2100Config, FilenameConfig, MCD2100Config, cfg


class FakeHandle:
    def __init__(self, value=None, error=None, state="SUCCEEDED"):
        self.value, self.error = value, error
        self.state = SimpleNamespace(name=state)
        self.accepted = True

    def result(self, timeout=None):
        if self.error:
            raise self.error
        return self.value

    def wait_drained(self, timeout=None):
        if self.error:
            raise self.error
        return self.value


class StagedRampHandle:
    """Separate client timeout from a later owner-drain report."""
    def __init__(self, report):
        self.report = report
        self.state = SimpleNamespace(name="TIMED_OUT_DRAINING")
        self.drained = False

    def result(self, timeout=None):
        raise RuntimeError("read_ramp_tables request timed out")

    def wait_drained(self, timeout=None):
        if not self.drained:
            raise concurrent.futures.TimeoutError()
        return self.report


class ManualTelemetryHandle:
    def __init__(self, value=None):
        self.value = value
        self.state = SimpleNamespace(name="RUNNING")
        self.completed_at = None
        self.accepted = True

    def complete(self, value=None, completed_at=None):
        if value is not None:
            self.value = value
        self.completed_at = completed_at
        self.state.name = "SUCCEEDED"

    def result(self, timeout=None):
        if self.state.name != "SUCCEEDED":
            raise concurrent.futures.TimeoutError()
        return self.value

    def wait_drained(self, timeout=None):
        if self.state.name != "SUCCEEDED":
            raise concurrent.futures.TimeoutError()
        return self.value


class FakeController(QObject):
    connected = Signal(object)
    disconnected = Signal()
    state_changed = Signal(object)
    snapshot_updated = Signal(object)
    error = Signal(str)
    display_cycle_finished = Signal(int)

    def __init__(self, connected=True):
        super().__init__()
        self.state = SimpleNamespace(name="IDLE" if connected else "DISCONNECTED")
        self.connect_handle = FakeHandle(SimpleNamespace(host="fake-2100"))
        self.connect_calls = 0
        self.stop_calls = 0
        self.temperature_configure_calls = []
        self.generation = 1
        self.ramp_calls = 0

    def connect_async(self):
        self.connect_calls += 1
        return self.connect_handle

    def disconnect_async(self):
        self.state.name = "DISCONNECTED"
        return FakeHandle(True)

    def read_snapshot_async(self):
        status = SimpleNamespace(quench=False, backend_details={"field_control": True})
        return FakeHandle(SimpleNamespace(field_t=1.25, setpoint_t=1.5, temperature_k=1.8, status=status))

    def read_display_snapshot_async(self, *, max_age_s=0.5, source="manual"):
        return self.read_snapshot_async()

    def read_temperature_snapshot_async(self):
        return FakeHandle(SimpleNamespace(
            sample_temperature_k=12.0, vti_temperature_k=2.0,
            sample_setpoint_k=12.0, sample_control_active=True,
            sample_ramp_active=False,
        ))

    def read_display_temperature_async(self, *, max_age_s=0.5, source="manual"):
        return self.read_temperature_snapshot_async()

    def invalidate_display_cache(self, group=None):
        return None

    def configure_sample_temperature_async(self, target, ramp_rate):
        self.temperature_configure_calls.append((float(target), float(ramp_rate)))
        return FakeHandle(SimpleNamespace(
            sample_temperature_k=12.0, vti_temperature_k=2.0,
            sample_setpoint_k=float(target), sample_control_active=True,
            sample_ramp_active=False, sample_ramp_rate_k_per_min=100.0,
        ))

    def request_stop(self):
        self.stop_calls += 1
        return FakeHandle(True)

    def read_ramp_tables_async(self):
        self.ramp_calls += 1
        return FakeHandle(None)

    def __getattr__(self, name):
        if any(token in name.lower() for token in ("adapter", "vendor", "sdk")):
            raise AssertionError(f"forbidden raw access: {name}")
        raise AttributeError(name)


class FakeWorker:
    def __init__(self, result=None, gate=None):
        self.result = result or {"status": "COMPLETED", "spectra_written": 2}
        self.gate = gate
        self.cancelled = threading.Event()

    def run(self):
        if self.gate is not None:
            self.gate.wait(2)
        if self.cancelled.is_set():
            return {"status": "CANCELLED", "spectra_written": 0}
        return dict(self.result)

    def request_cancel(self):
        self.cancelled.set()
        if self.gate is not None:
            self.gate.set()


class WorkerFactory:
    def __init__(self, workers):
        self.workers = list(workers)
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.workers.pop(0)


class WorkflowStub(QWidget):
    def __init__(self):
        super().__init__()
        self.externally_busy = False

    def set_externally_busy(self, busy):
        self.externally_busy = bool(busy)

    def can_start(self):
        return not self.externally_busy


class MCD2100PanelTests(unittest.TestCase):
    def test_preparation_progress_ages_locally_and_ignores_old_runner(self):
        panel, controller, _ = self.make_panel()
        worker = SimpleNamespace()
        runner, old_runner = _Runner(worker), _Runner(worker)
        panel.worker, panel.runner = worker, runner
        self.addCleanup(lambda: setattr(panel, "worker", None))
        runner.preparation_progress.connect(panel._on_preparation_progress)
        old_runner.preparation_progress.connect(panel._on_preparation_progress)
        event = dict(stage="positioning", field_t=-1.5, target_t=-2.,
                     sampled_at=97., started_at=90., deadline=200., stable_count=0)
        with patch("ui.mcd2100_panel.time.monotonic", return_value=100.):
            runner.preparation_progress.emit(event)
            self.assertIn("age=3.0 s", panel.status.text())
        phase_started = panel._phase_started_at
        with patch("ui.mcd2100_panel.time.monotonic", return_value=115.):
            panel._refresh_activity()
            self.assertIn("age=18.0 s", panel.status.text())
            self.assertIn("remaining=85 s", panel.status.text())
            old_runner.preparation_progress.emit(dict(event, field_t=5.))
            self.assertIn("field=-1.5", panel.status.text())
        self.assertEqual(panel._phase_started_at, phase_started)
        panel._on_terminal(dict(operation="magnet_preparation", status="FAILED"))
        runner.preparation_progress.emit(event)
        runner.phase.connect(panel._on_phase)
        runner.phase.emit("Moving to Start: late")
        panel._refresh_activity()
        self.assertEqual(panel.status.text(), "FAILED")

    def test_mode_progress_reuses_snapshot_time_and_keeps_15_second_log_cadence(self):
        panel, controller, _ = self.make_panel()
        runner = _Runner(SimpleNamespace())
        panel.worker, panel.runner = runner.worker, runner
        self.addCleanup(lambda: setattr(panel, "worker", None))
        runner.preparation_progress.connect(panel._on_preparation_progress)
        event = dict(stage="mode readiness", field_t=0., target_t=-2.,
                     sampled_at=97., started_at=90., deadline=390., stable_count=0)
        with patch("ui.mcd2100_panel.time.monotonic", return_value=100.):
            runner.preparation_progress.emit(event)
        snapshot = controller.read_snapshot_async().value
        snapshot.monotonic_s = 110.
        snapshot.status.driven_mode = False
        snapshot.status.persistent_mode = True
        with patch.object(controller, "read_snapshot_async", side_effect=AssertionError("extra getter")):
            with patch("ui.mcd2100_panel.time.monotonic", return_value=115.):
                panel._on_snapshot(snapshot)
                self.assertIn("age=5.0 s", panel.status.text())
                self.assertIn("Driven=no", panel.status.text())
                self.assertIn("Persistent=yes", panel.status.text())
                self.assertNotIn("stable=", panel.status.text())
                self.assertEqual(panel._log.toPlainText().count("mode readiness"), 1)
            with patch("ui.mcd2100_panel.time.monotonic", return_value=120.):
                panel._on_snapshot(snapshot)
                panel._refresh_activity()
                self.assertIn("age=10.0 s", panel.status.text())
                self.assertEqual(panel._log.toPlainText().count("mode readiness"), 1)
            with patch("ui.mcd2100_panel.time.monotonic", return_value=130.):
                panel._refresh_activity()
                self.assertIn("age=20.0 s", panel.status.text())
                self.assertEqual(panel._log.toPlainText().count("mode readiness"), 2)

    def test_long_preparation_status_wraps_inside_the_status_group(self):
        panel, _, _ = self.make_panel()
        from app.engine.magnet_preparation import format_preparation_progress
        event = dict(stage="mode readiness", field_t=-1.23456, target_t=-2.,
                     sampled_at=0., started_at=0., deadline=300., stable_count=0)
        panel.status.setText(format_preparation_progress(event, 123.))
        panel.resize(1100, 700)
        panel.show()
        self.app.processEvents()
        self.assertTrue(panel.status.wordWrap())
        self.assertLess(panel.status.minimumSizeHint().width(), 400)
        self.assertLessEqual(panel.status.width(), panel.status.parentWidget().contentsRect().width())
        self.assertGreater(panel.status.height(), panel.status.fontMetrics().height())
        panel.hide()

    def test_runner_keeps_phase_and_spectrum_event_with_older_callback_signature(self):
        received = []
        class OlderWorker:
            def set_callbacks(self, *, progress=None, spectrum=None, spectrum_event=None,
                              log=None, phase=None):
                self.phase, self.spectrum_event = phase, spectrum_event
        worker = OlderWorker()
        runner = _Runner(worker)
        runner.phase.connect(lambda value: received.append(value))
        runner.spectrum_event.connect(lambda value: received.append(value))
        worker.phase("Preparing")
        worker.spectrum_event({"spectrum": 1})
        self.assertEqual(received, ["Preparing", {"spectrum": 1}])

    def test_prepare_entry_uses_configured_position_budget(self):
        panel, _, _ = self.make_panel()
        cfg.attodry2100.position_timeout_s = 1234.
        with patch.object(panel, "_handoff_worker") as handoff:
            panel.prepare_magnet()
        self.assertEqual(handoff.call_args.args[0].preparation.position_timeout_s, 1234.)

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # Keep the real application's persistent config untouched while
        # making panel defaults deterministic for each isolated test.
        self._cfg_snapshot = copy.deepcopy(cfg.__dict__)
        self._cfg_save_patch = patch.object(cfg, "save")
        self._cfg_save_patch.start()
        cfg.mcd2100 = MCD2100Config()
        cfg.filename = FilenameConfig()

    def tearDown(self):
        self._cfg_save_patch.stop()
        cfg.__dict__.clear()
        cfg.__dict__.update(self._cfg_snapshot)

    def make_panel(self, *, controller=None, workers=None):
        self.output = tempfile.TemporaryDirectory()
        self.addCleanup(self.output.cleanup)
        controller = controller or FakeController(True)
        factory = WorkerFactory(workers or [FakeWorker()])
        panel = MCD2100Panel(
            controller,
            worker_factory=factory,
            optical_factory=lambda: object(),
        )
        panel.output.setText(self.output.name)
        panel.angles.setText("0, 90")
        self.addCleanup(lambda: panel.shutdown(2000))
        return panel, controller, factory

    def wait_terminal(self, panel, timeout=2000):
        if panel.worker is not None and panel.thread is not None and panel.thread.isRunning():
            loop = QEventLoop()
            panel.thread.finished.connect(loop.quit)
            QTimer.singleShot(timeout, loop.quit)
            loop.exec()
        self.app.processEvents()
        self.assertIsNone(panel.worker)

    def test_session_state_preserves_raw_gate_entry_draft(self):
        panel, _controller, _factory = self.make_panel()
        panel._condition_table.item(0, 8).setText("partial")
        state = panel.capture_session_state()
        restored, _controller2, _factory2 = self.make_panel()
        restored.restore_session_state(state)
        self.assertEqual(restored._condition_table.item(0, 8).text(), "partial")

    def test_session_state_captures_invalid_partial_ratio(self):
        panel, _controller, _factory = self.make_panel()
        panel.gate_vtg_factor.setValue(0.0)
        panel._condition_table.item(0, 5).setText("unfinished")
        state = panel.capture_session_state()
        self.assertIsNone(state["gate_ratio"])
        self.assertEqual(state["condition_drafts"][0]["vbias_v"], "unfinished")

    def make_arbiter(self):
        old, new = WorkflowStub(), WorkflowStub()
        tabs = QTabWidget()
        other = QWidget()
        tabs.addTab(other, "Other")
        tabs.addTab(old, "MCD 1000")
        tabs.addTab(new, "MCD 2100")
        host = SimpleNamespace(
            _active_mcd_panel=None, _mcd=old, _mcd2100=new,
            _tabs=tabs, _inst_panel=QWidget(), _status=QStatusBar(),
        )
        return host, old, new, other

    def test_panel_and_separate_main_window_tabs_are_constructible(self):
        panel, _, _ = self.make_panel()
        self.assertIsInstance(panel, MCD2100Panel)
        source = inspect.getsource(MainWindow.__init__)
        self.assertIn('addTab(self._mcd, "MCD 1000")', source)
        self.assertIn('addTab(self._mcd2100, "MCD 2100")', source)

    def test_continuous_controls_follow_grouped_layout_without_discrete_selector(self):
        panel, _, _ = self.make_panel()
        titles = {box.title() for box in panel.findChildren(QGroupBox)}
        self.assertTrue({
            "Sample / Device", "Field Sweep", "Rotation", "Temperature",
            "LightField", "Gate / SMU", "Filename", "Run control",
            "Status / Progress / Log",
        }.issubset(titles))
        source = inspect.getsource(MCD2100Panel).lower()
        self.assertNotIn("discrete selector", source)
        self.assertFalse(panel.bidirectional.isVisible())
        self.assertFalse(panel.vtg.isVisible())
        self.assertFalse(panel.vbg.isVisible())
        self.assertFalse(panel.vbias.isVisible())
        self.assertFalse(panel.sample_target.isHidden())
        self.assertTrue(panel.sample_ramp_rate.isHidden())
        self.assertTrue(panel.temperature_tolerance.isHidden())
        self.assertTrue(panel.temperature_stable.isHidden())
        self.assertTrue(panel.temperature_timeout.isHidden())
        self.assertGreaterEqual(panel._workflow_layout.indexOf(panel._gate_group), 0)
        self.assertEqual(panel._workflow_layout.indexOf(panel._temperature_group), -1)
        self.assertEqual(panel._workflow_layout.indexOf(panel._lightfield_group), -1)

    def test_continuous_panel_scrolls_at_compact_window_size(self):
        panel, _, _ = self.make_panel()
        panel.resize(520, 520)
        panel.show()
        self.app.processEvents()
        scroll = panel.findChild(QScrollArea)
        self.assertIsNotNone(scroll)
        self.assertTrue(scroll.widgetResizable())
        self.assertGreaterEqual(scroll.verticalScrollBar().maximum(), 0)
        panel.hide()

    def test_short_inputs_are_bounded_and_filename_preview_remains_expanding(self):
        panel, _, _ = self.make_panel()
        for widget, maximum in (
            (panel.start_field, 140), (panel.stop_field, 140),
            (panel.angles, 240), (panel.rotator, 180),
            (panel.lf_center, 140), (panel.lf_exposure, 140),
            (panel.lf_frames, 100), (panel.vtg, 140),
            (panel.vbg, 140), (panel.vbias, 140), (panel.gate_ratio, 140),
            (panel._sample_id, 300), (panel._point, 160), (panel.stem, 450),
        ):
            self.assertLessEqual(widget.maximumWidth(), maximum)
            self.assertEqual(widget.sizePolicy().verticalPolicy(), QSizePolicy.Policy.Fixed)
        self.assertEqual(panel.output.sizePolicy().horizontalPolicy(), QSizePolicy.Policy.Expanding)
        self.assertGreater(panel.output.maximumWidth(), 10000)
        self.assertTrue(panel.output.isHidden())
        self.assertTrue(panel.output_browse.isHidden())
        self.assertTrue(panel.stem.isHidden())
        self.assertTrue(panel.filename_preview.isReadOnly())
        self.assertEqual(
            panel.filename_preview.sizePolicy().horizontalPolicy(),
            QSizePolicy.Policy.Expanding,
        )
        self.assertGreater(panel.start_btn.receivers("2clicked(bool)"), 0)
        self.assertGreater(panel.stop_btn.receivers("2clicked(bool)"), 0)

    def test_filename_preview_tracks_point_fields_and_first_enabled_gate(self):
        panel, _, _ = self.make_panel()
        panel.gate_vtg_factor.setValue(1.0)
        panel.gate_vbg_factor.setValue(1.0)
        panel.start_field.setText("-1")
        panel.stop_field.setText("1")
        panel._sample_id.setText("YZ365")
        derived_output = panel.output.text()
        panel._point.setText("p5n2")
        panel._seed_condition_table([
            {"enabled": False, "vtg_v": 9.0, "vbg_v": 9.0, "vbias_v": 0.0},
            {"enabled": True, "vtg_v": 0.5, "vbg_v": -0.25, "vbias_v": 0.1},
        ])
        panel._update_condition_editable()

        preview = panel.filename_preview.text()
        self.assertTrue(preview.startswith("YZ365_p5n2_MCD_"), preview)
        self.assertIn("K_G01_", preview)
        self.assertIn("_G01_", preview)
        self.assertIn("_B-1to+1T_", preview)
        self.assertIn("_Vtg+0p5_", preview)
        self.assertIn("_Vbg-0p25_", preview)
        self.assertTrue(preview.endswith("_roundtrip.csv"), preview)
        self.assertEqual(panel.output.text(), derived_output)
        self.assertNotIn("p5n2", panel.output.text())

    def test_gate_selection_move_buttons_scroll_and_execution_preview(self):
        panel, _, _ = self.make_panel()
        rows = [
            {"enabled": False, "mode": MODE_DIRECT, "input_a": 1,
             "input_b": 10, "vbias_v": 0},
            {"enabled": True, "mode": MODE_DIRECT, "input_a": 2,
             "input_b": 20, "vbias_v": 0.1},
            {"enabled": True, "mode": MODE_DOPING_EFIELD, "input_a": 3,
             "input_b": 30, "vbias_v": 0.2},
        ]
        panel._seed_condition_table(rows)
        panel._update_condition_editable()
        self.assertTrue(
            panel._condition_table.item(0, 0).flags() & Qt.ItemFlag.ItemIsSelectable
        )
        panel._condition_table.cellClicked.emit(1, 0)
        self.assertEqual(panel._condition_table.currentRow(), 1)
        self.assertTrue(panel._move_condition_up_btn.isEnabled())
        panel._move_condition_up_btn.click()
        self.assertEqual(panel._condition_table.currentRow(), 0)
        self.assertEqual(panel._condition_rows()[0]["input_a"], 2.0)
        self.assertFalse(panel._move_condition_up_btn.isEnabled())
        self.assertIn("3 total · 2 enabled", panel._condition_summary.text())
        preview = panel._condition_plan_preview.toPlainText()
        self.assertIn("G01 · Table row 1", preview)
        self.assertIn("G02 · Table row 3", preview)
        self.assertNotIn("G03", preview)
        self.assertIn("Selected table row 1 (G01)", panel._selected_condition_summary.text())

        many = [
            {"enabled": True, "mode": MODE_DIRECT, "input_a": index,
             "input_b": -index, "vbias_v": 0}
            for index in range(12)
        ]
        panel._seed_condition_table(many)
        panel._update_condition_editable()
        scrollbar = panel._condition_table.verticalScrollBar()
        self.app.processEvents()
        scrollbar.setValue(max(1, scrollbar.maximum() // 2))
        before = scrollbar.value()
        panel._condition_table.selectRow(5)
        panel._move_condition_up_btn.click()
        self.assertEqual(panel._condition_table.currentRow(), 4)
        self.assertLessEqual(abs(scrollbar.value() - before), 26)

    def test_live_or_controlled_sample_temperature_updates_filename(self):
        panel, _, _ = self.make_panel()
        panel._sample_id.setText("YZ365")
        panel._on_temperature_snapshot(SimpleNamespace(
            sample_temperature_k=4.0, vti_temperature_k=3.8,
            sample_control_active=False,
        ))
        self.assertIn("_MCD_4K_G01_", panel.filename_preview.text())
        panel.temperature_control_enabled.setChecked(True)
        panel.sample_target.setValue(20.25)
        self.assertIn("_MCD_20p25K_G01_", panel.filename_preview.text())

    def test_gate_table_modes_derived_values_reorder_and_shared_rotator(self):
        panel, _, _ = self.make_panel()
        panel._seed_condition_table([
            {"enabled": True, "mode": MODE_DIRECT, "input_a": 0,
             "input_b": 0, "vbias_v": 0}
        ])
        self.assertIsInstance(panel.rotator, QComboBox)
        self.assertFalse(panel.rotator.isEditable())
        self.assertGreaterEqual(panel.rotator.count(), 2)
        self.assertEqual(panel._condition_table.rowCount(), 1)
        panel.vtg.setValue(1.0); panel.vbg.setValue(2.0); panel.gate_ratio.setValue(2.0)
        panel._set_row_value(0, 1, 1.0); panel._set_row_value(0, 2, 2.0)
        panel._update_condition_editable()
        self.assertAlmostEqual(panel._row_value(0, 4), 5.0)
        self.assertAlmostEqual(panel._row_value(0, 5), -3.0)
        panel._add_condition_row()
        self.assertEqual(panel._condition_table.rowCount(), 2)
        panel._condition_table.selectRow(1)
        panel._move_condition(-1)
        self.assertEqual(panel._condition_table.currentRow(), 0)

    def test_two_sided_ratio_and_atomic_batch_rows_support_both_input_modes(self):
        panel, _, _ = self.make_panel()
        panel.gate_vtg_factor.setValue(2.0)
        panel.gate_vbg_factor.setValue(1.0)
        self.assertAlmostEqual(panel._gate_ratio(), .5)
        rows = build_condition_batch(
            MODE_DOPING_EFIELD, "1,2", "0", "paired", panel._gate_ratio(),
            voltage_limit=1000,
        )
        provenance = {"mode": MODE_DOPING_EFIELD, "input_a_spec": "1,2",
                      "input_b_spec": "0", "expansion": "paired", "row_count": 2}
        panel._append_condition_rows(rows, provenance)
        self.assertEqual(panel._condition_table.rowCount(), 2)
        self.assertEqual(
            [panel._condition_table.cellWidget(row, 2).currentData() for row in range(2)],
            [MODE_DOPING_EFIELD, MODE_DOPING_EFIELD],
        )
        self.assertEqual(panel.capture_session_state()["gate_batches"], [provenance])
        before = panel._condition_table.rowCount()
        with self.assertRaises(ValueError):
            panel._append_condition_rows([
                {"enabled": True, "mode": MODE_DIRECT, "input_a": 5000,
                 "input_b": 0, "vbias_v": 0}
            ])
        self.assertEqual(panel._condition_table.rowCount(), before)

    def test_visible_gate_entry_creates_single_lists_grid_and_edits_selected(self):
        panel, _, _ = self.make_panel()
        panel._seed_condition_table([{
            "enabled": True, "mode": MODE_DIRECT, "input_a": 0,
            "input_b": 0, "vbias_v": 0,
        }])
        headers = [
            panel._condition_table.horizontalHeaderItem(column).text()
            for column in range(8)
        ]
        self.assertEqual(
            headers,
            ["Use", "#", "Input type", "Vtg", "Vbg", "Vbias", "Doping", "E-field"],
        )
        self.assertTrue(panel._condition_table.isColumnHidden(8))
        self.assertTrue(panel._condition_table.isColumnHidden(9))
        self.assertEqual(
            panel._condition_table.horizontalHeader().sectionResizeMode(2),
            QHeaderView.ResizeMode.Stretch,
        )
        self.assertLessEqual(panel._condition_table.columnWidth(7), 90)

        panel._gate_entry_a.setText("1,2")
        panel._gate_entry_b.setText("3")
        self.assertEqual(panel._gate_entry_add.text(), "Add 2 rows")
        panel._commit_gate_entry()
        self.assertEqual(panel._condition_table.rowCount(), 2)
        self.assertEqual([panel._row_value(row, 3) for row in range(2)], [1, 2])
        self.assertEqual([panel._row_value(row, 4) for row in range(2)], [3, 3])

        panel._condition_table.selectRow(0)
        panel._edit_selected_condition()
        panel._gate_entry_a.setText("5")
        panel._gate_entry_b.setText("6")
        self.assertEqual(panel._gate_entry_add.text(), "Replace selected with 1 row")
        panel._commit_gate_entry()
        self.assertEqual(panel._condition_table.rowCount(), 2)
        self.assertEqual(panel._row_value(0, 3), 5)
        self.assertEqual(panel._row_value(0, 4), 6)

    def test_session_round_trip_restores_gate_rows_modes_and_optics(self):
        panel, _, _ = self.make_panel()
        panel._seed_condition_table([
            {"enabled": True, "mode": "direct", "input_a": 1, "input_b": 2, "vbias_v": 3},
            {"enabled": False, "mode": "vtg_from_vbg_ratio", "input_a": 4, "input_b": 5, "vbias_v": 6},
            {"enabled": True, "mode": "fixed_efield", "input_a": -2, "input_b": 1, "vbias_v": 0},
        ])
        panel.angles.setText("10, 20, 30")
        panel._point.setText("p5n2")
        panel.lf_center.setValue(900); panel.lf_exposure.setValue(12); panel.lf_frames.setValue(7)
        panel.temperature_control_enabled.setChecked(True)
        panel.sample_target.setValue(20.0)
        panel.sample_ramp_rate.setValue(2.5)
        panel.initial_voltage_settle.setValue(600.0)
        panel.voltage_settle.setValue(120.0)
        snapshot = panel.capture_session_state()
        restored, _, _ = self.make_panel()
        restored.restore_session_state(snapshot)
        self.assertEqual(restored._condition_table.rowCount(), 3)
        self.assertEqual(restored.capture_session_state()["angles"], "10, 20, 30")
        self.assertEqual(restored.capture_session_state()["point"], "p5n2")
        modes = [restored._condition_table.cellWidget(row, 2).currentData() for row in range(3)]
        self.assertEqual(modes, ["direct", "vtg_from_vbg_ratio", "fixed_efield"])
        self.assertEqual(restored.lf_frames.value(), 7)
        self.assertTrue(restored.temperature_control_enabled.isChecked())
        self.assertEqual(restored.sample_target.value(), 20.0)
        self.assertEqual(restored.sample_ramp_rate.value(), 2.5)
        self.assertEqual(restored.initial_voltage_settle.value(), 600.0)
        self.assertEqual(restored.voltage_settle.value(), 120.0)

    def test_compact_layout_activity_feedback_and_collapsed_error_area(self):
        panel, _, _ = self.make_panel()
        panel.resize(1100, 700)
        panel.show()
        self.app.processEvents()
        self.assertTrue(panel.error_display.isHidden())
        self.assertLessEqual(panel._sample_group.height(), 70)
        self.assertLess(abs(panel._sweep_group.y() - panel._rotation_group.y()), 20)

        panel.worker = FakeWorker()
        panel._on_phase("Gate settling after first gate ramp: 5 s")
        self.assertIn("Gate settling", panel.status.text())
        self.assertIn("Gate settling", panel._log.toPlainText())
        self.assertIn("Settling", panel.run_activity.text())
        self.assertIn("remaining", panel.run_activity.text())
        panel._on_spectrum_event({
            "label": "A", "wavelengths": [700.0, 701.0], "counts": [1.0, 2.0],
            "B1_T": -1.0, "direction": "forward", "gate_index": 2,
            "gate_count": 3, "total_spectra": 7,
        })
        self.assertIn("New spectrum", panel.run_activity.text())
        self.assertIn("Spectrum 7", panel.spectrum_activity.text())
        self.assertIn("Gate 2/3", panel._plot_overlay.toPlainText())
        panel._active_phase = "Acquiring spectra while field moves -1 T to +1 T"
        panel._phase_started_at = time.monotonic() - 100.0
        panel._last_spectrum_at = time.monotonic() - 100.0
        panel._refresh_activity()
        self.assertIn("no recent spectrum", panel.spectrum_activity.text())
        panel.worker = None
        panel.hide()

    def test_fake_connection_and_telemetry_are_reflected_nonblocking(self):
        controller = FakeController(False)
        panel, _, _ = self.make_panel(controller=controller)
        panel.connect_instrument()
        self.assertIn("Connecting", panel.connection_status.text())
        self.app.processEvents()
        self.assertIn("Connected", panel.connection_status.text())
        controller.snapshot_updated.emit(controller.read_snapshot_async().result())
        self.assertEqual(panel.field_value.text(), "1.25 T")
        self.assertEqual(panel.temperature_value.text(), "1.8 K")
        self.assertEqual(panel.control_value.text(), "Active")
        self.assertEqual(panel.current_target.text(), "1.5 T")
        panel._on_temperature_snapshot(controller.read_temperature_snapshot_async().result())
        self.assertEqual(panel.sample_temperature_value.text(), "12 K")
        self.assertEqual(panel.vti_temperature_value.text(), "2 K")
        self.assertEqual(panel.sample_temperature_control_value.text(), "Active")
        self.assertEqual(panel.sample_temperature_setpoint_value.text(), "12 K")

    def test_apply_temperature_sends_target_immediately_and_reports_ramping(self):
        panel, controller, _ = self.make_panel()
        panel.temperature_control_enabled.setChecked(True)
        panel.sample_target.setValue(20.0)
        panel.sample_ramp_rate.setValue(2.5)

        panel.apply_temperature_btn.click()
        self.app.processEvents()

        self.assertEqual(controller.temperature_configure_calls, [(20.0, 2.5)])
        self.assertEqual(panel.sample_temperature_setpoint_value.text(), "20 K")
        self.assertIn("Ramping to 20", panel.temperature_apply_status.text())
        self.assertTrue(panel.apply_temperature_btn.isEnabled())

    def test_temperature_accepts_typed_1_67_k_and_applies_without_clamping(self):
        panel, controller, _ = self.make_panel()
        panel.temperature_control_enabled.setChecked(True)
        panel.sample_target.selectAll()
        QTest.keyClicks(panel.sample_target, "1.67")
        QTest.keyClick(panel.sample_target, Qt.Key_Return)
        self.assertAlmostEqual(panel.sample_target.value(), 1.67)
        panel.apply_temperature_btn.click()
        self.app.processEvents()
        self.assertAlmostEqual(controller.temperature_configure_calls[0][0], 1.67)
        self.assertEqual(panel.sample_temperature_setpoint_value.text(), "1.67 K")

    def test_apply_temperature_requires_connection_and_is_disabled_during_run(self):
        disconnected = FakeController(False)
        panel, _, _ = self.make_panel(controller=disconnected)
        panel.temperature_control_enabled.setChecked(True)
        self.assertFalse(panel.apply_temperature_btn.isEnabled())

        connected, _, _ = self.make_panel()
        connected.temperature_control_enabled.setChecked(True)
        connected.worker = FakeWorker()
        connected._refresh_controls()
        self.assertFalse(connected.apply_temperature_btn.isEnabled())

    def test_completed_detach_is_distinguished_and_reconnects_telemetry_only(self):
        controller = FakeController(True)
        panel, _, _ = self.make_panel(controller=controller)
        panel._on_snapshot(controller.read_snapshot_async().result())

        controller.state_changed.emit(SimpleNamespace(name="DETACHED"))
        controller.disconnected.emit()

        self.assertIn("Detached", panel.connection_status.text())
        self.assertIn("final field", panel.connection_status.text())
        self.assertEqual(panel.connect_btn.text(), "Reconnect telemetry")
        self.assertIn("last-known", panel.telemetry_note.text())
        self.assertIn("not live", panel.telemetry_note.text())
        self.assertFalse(panel.refresh_btn.isEnabled())

        panel.connect_instrument()
        self.app.processEvents()

        self.assertEqual(controller.connect_calls, 1)
        self.assertEqual(controller.stop_calls, 0)
        self.assertIn("Connected", panel.connection_status.text())
        self.assertEqual(panel.connect_btn.text(), "Connect")
        self.assertIn("Live telemetry", panel.telemetry_note.text())

    def test_unexpected_disconnect_is_not_presented_as_completed_detach(self):
        controller = FakeController(True)
        panel, _, _ = self.make_panel(controller=controller)

        controller.state_changed.emit(SimpleNamespace(name="DISCONNECTED"))
        controller.disconnected.emit()

        self.assertEqual(panel.connection_status.text(), "Disconnected")
        self.assertEqual(panel.connect_btn.text(), "Connect")
        self.assertEqual(panel.telemetry_note.text(), "Telemetry unavailable")

    def test_disconnected_start_is_rejected_before_worker_creation(self):
        panel, _, factory = self.make_panel(controller=FakeController(False))
        panel.start()
        self.assertIsNone(panel.worker)
        self.assertIn("Connect", panel.error_display.toPlainText())
        self.assertEqual(factory.calls, [])

    def test_mcd2100_start_defers_lightfield_readiness_until_worker(self):
        output = tempfile.TemporaryDirectory()
        self.addCleanup(output.cleanup)
        factory = WorkerFactory([FakeWorker()])
        shared_lf = SimpleNamespace(
            is_connected=True, is_ready=False,
            adapter=SimpleNamespace(), setup=SimpleNamespace(),
        )
        panel = MCD2100Panel(
            FakeController(True), lf6_ctrl=shared_lf,
            worker_factory=factory, optical_factory=lambda: object(),
        )
        self.addCleanup(lambda: panel.shutdown(2000))
        panel.output.setText(output.name)
        panel.start()
        self.assertIsNotNone(panel.worker)
        self.assertEqual(panel.error_display.toPlainText(), "")
        self.assertEqual(len(factory.calls), 1)

    def test_mcd2100_start_rechecks_shared_lightfield_readiness(self):
        output = tempfile.TemporaryDirectory()
        self.addCleanup(output.cleanup)
        factory = WorkerFactory([FakeWorker()])

        class SharedLightField:
            is_connected = True
            is_ready = False
            adapter = SimpleNamespace()
            setup = SimpleNamespace()

            def __init__(self):
                self.ensure_calls = []

            def ensure_ready(self, **kwargs):
                self.ensure_calls.append(kwargs)
                self.is_ready = True

        shared_lf = SharedLightField()
        panel = MCD2100Panel(
            FakeController(True), lf6_ctrl=shared_lf,
            worker_factory=factory, optical_factory=lambda: object(),
        )
        self.addCleanup(lambda: panel.shutdown(2000))
        panel.output.setText(output.name)
        panel.start()
        self.assertEqual(len(shared_lf.ensure_calls), 0)
        self.assertIsNotNone(panel.worker)
        self.assertEqual(len(factory.calls), 1)

    def test_apply_voltages_requires_connected_smu_before_worker_start(self):
        panel, _, factory = self.make_panel(controller=FakeController(True))
        panel.apply_voltages.setChecked(True)
        panel.start()
        self.assertIsNone(panel.worker)
        self.assertIn("SMU", panel.error_display.toPlainText())
        self.assertEqual(factory.calls, [])

    def test_mcd2100_reuses_shared_lightfield_controller_and_service_path(self):
        events = []

        class SharedAdapter:
            def calibration_wavelengths(self, force=False):
                events.append(("calibration", force))
                return [700.0, 701.0]

            def change_spectra_center(self, value):
                events.append(("center", float(value)))

            def set_center_wavelength_when_ready(self, value):
                self.change_spectra_center(value)

            def set_frames(self, value):
                events.append(("frames", int(value)))

            def configure_for_acquisition(self, *, center_nm, exposure_ms, frames):
                self.change_spectra_center(center_nm)
                events.append(("exposure", float(exposure_ms)))
                self.set_frames(frames)
                return {"result": "succeeded"}

            def acquire(self):
                events.append("acquire")
                return [700.0, 701.0], [1.0, 2.0]

        class SharedSetup:
            def change_expose_time(self, value):
                events.append(("exposure", float(value)))

        class SharedLightField:
            is_connected = True

            def __init__(self):
                self.adapter = SharedAdapter()
                self.setup = SharedSetup()
                self.connect_calls = 0

        class Rotator:
            def move_to(self, angle):
                events.append(("move", float(angle)))

            def get_position(self):
                events.append("position")
                return 33.0

        class SharedRotation:
            def __init__(self):
                self.rotator = Rotator()
                self.adapter_calls = 0

            def is_connected(self, name):
                self.adapter_calls += 1
                return name == "rot1"

            def adapter(self, name):
                self.adapter_calls += 1
                return self.rotator if name == "rot1" else None

        shared_lf = SharedLightField()
        rotation = SharedRotation()
        service = _LightFieldRotationService(shared_lf, rotation, "rot1")
        self.assertEqual(service.prepare(threading.Event()), [700.0, 701.0])
        service.configure(center_nm=730.0, exposure_ms=12.0, frames=4)
        service.move_to(33.0)
        self.assertEqual(service.get_position(), 33.0)
        wavelengths, counts, measured = service.acquire(33.0, "33", threading.Event())

        self.assertIs(service._lf6, shared_lf)
        self.assertEqual(shared_lf.connect_calls, 0)
        self.assertEqual((wavelengths, counts, measured), ([700.0, 701.0], [1.0, 2.0], 33.0))
        self.assertEqual(
            [item for item in events if isinstance(item, str)],
            ["position", "acquire"],
        )
        self.assertIn(("center", 730.0), events)
        self.assertIn(("exposure", 12.0), events)
        self.assertIn(("frames", 4), events)

    def test_panel_worker_receives_optical_service_bound_to_shared_lightfield(self):
        class SharedLightField:
            is_connected = True
            adapter = SimpleNamespace()
            setup = SimpleNamespace()
            connect_calls = 0

        class SharedRotation:
            def is_connected(self, name):
                return name == "rot1"

            def adapter(self, name):
                return object() if name == "rot1" else None

        shared_lf = SharedLightField()
        rotation = SharedRotation()
        output = tempfile.TemporaryDirectory()
        self.addCleanup(output.cleanup)
        factory = WorkerFactory([FakeWorker()])
        panel = MCD2100Panel(
            FakeController(True), lf6_ctrl=shared_lf, rotation_ctrl=rotation,
            worker_factory=factory,
        )
        self.addCleanup(lambda: panel.shutdown(2000))
        panel.output.setText(output.name)
        panel.start()
        self.wait_terminal(panel)
        optical = factory.calls[0][0][1]
        self.assertIsInstance(optical, _LightFieldRotationService)
        self.assertIs(optical._lf6, shared_lf)
        self.assertEqual(shared_lf.connect_calls, 0)

    def test_valid_settings_start_accepted_worker_and_double_start_is_ignored(self):
        gate = threading.Event()
        panel, _, factory = self.make_panel(workers=[FakeWorker(gate=gate)])
        panel._point.setText("p5n2")
        panel.temperature_control_enabled.setChecked(True)
        panel.sample_target.setValue(20.0)
        panel.start()
        panel.start()
        self.assertEqual(len(factory.calls), 1)
        args, kwargs = factory.calls[0]
        self.assertEqual(args[2], -2.0)
        self.assertEqual(args[3], 2.0)
        self.assertEqual(args[4], [0.0, 90.0])
        self.assertNotIn("settling", kwargs)
        self.assertTrue(kwargs["temperature_control_enabled"])
        self.assertEqual(kwargs["sample_target_k"], 20.0)
        self.assertEqual(kwargs["metadata"]["point"], "p5n2")
        panel.stop()
        self.wait_terminal(panel)

    def test_invalid_empty_or_nonfinite_fields_angles_and_output_do_not_start(self):
        panel, _, factory = self.make_panel()
        for field, value in (
            (panel.start_field, ""), (panel.start_field, "nan"),
            (panel.angles, ""), (panel.angles, "inf"), (panel.output, ""),
        ):
            original = field.text()
            field.setText(value)
            panel.start()
            self.assertIsNone(panel.worker)
            self.assertTrue(panel.error_display.toPlainText())
            field.setText(original)
        self.assertEqual(factory.calls, [])

    def test_start_locks_shared_controls_and_completion_restores_them(self):
        panel, _, _ = self.make_panel()
        host, old, _, other = self.make_arbiter()
        host._mcd2100 = panel
        panel.run_state_changed.connect(
            lambda running: MainWindow._on_mcd_workflow_state_changed(host, panel, running)
        )
        panel.start()
        self.assertFalse(host._inst_panel.isEnabled())
        self.assertFalse(host._tabs.isTabEnabled(host._tabs.indexOf(old)))
        self.assertFalse(host._tabs.isTabEnabled(host._tabs.indexOf(other)))
        self.wait_terminal(panel)
        self.assertTrue(host._inst_panel.isEnabled())
        self.assertTrue(all(host._tabs.isTabEnabled(i) for i in range(host._tabs.count())))
        self.assertIn("Completed", panel.status.text())

    def test_cancel_is_nonblocking_reaches_worker_and_restores_controls(self):
        gate = threading.Event()
        worker = FakeWorker(gate=gate)
        panel, _, _ = self.make_panel(workers=[worker])
        host, old, _, _ = self.make_arbiter()
        host._mcd2100 = panel
        panel.run_state_changed.connect(
            lambda running: MainWindow._on_mcd_workflow_state_changed(host, panel, running)
        )
        panel.start()
        panel.stop()
        self.assertTrue(worker.cancelled.is_set())
        self.assertIsNotNone(panel.worker)
        self.wait_terminal(panel)
        self.assertEqual(panel.status.text(), "CANCELLED")
        self.assertTrue(host._inst_panel.isEnabled())

    def test_failure_shows_error_restores_locks_and_allows_retry(self):
        failed = FakeWorker({"status": "FAILED", "error": "camera failed", "spectra_written": 0})
        completed = FakeWorker()
        panel, _, factory = self.make_panel(workers=[failed, completed])
        host, _, _, _ = self.make_arbiter()
        host._mcd2100 = panel
        panel.run_state_changed.connect(
            lambda running: MainWindow._on_mcd_workflow_state_changed(host, panel, running)
        )
        panel.start()
        self.wait_terminal(panel)
        self.assertEqual(panel.status.text(), "FAILED")
        self.assertIn("camera failed", panel.error_display.toPlainText())
        self.assertTrue(panel.start_btn.isEnabled())
        panel.start()
        self.wait_terminal(panel)
        self.assertIn("Completed", panel.status.text())
        self.assertEqual(len(factory.calls), 2)

    def test_mcd1000_and_mcd2100_are_mutually_exclusive_both_directions(self):
        host, old, new, _ = self.make_arbiter()
        MainWindow._on_mcd_workflow_state_changed(host, new, True)
        self.assertFalse(old.can_start())
        self.assertTrue(new.can_start())
        MainWindow._on_mcd_workflow_state_changed(host, new, False)
        MainWindow._on_mcd_workflow_state_changed(host, old, True)
        self.assertTrue(old.can_start())
        self.assertFalse(new.can_start())
        self.assertFalse(host._tabs.isTabEnabled(host._tabs.indexOf(new)))
        MainWindow._on_mcd_workflow_state_changed(host, old, False)
        self.assertTrue(new.can_start())

    def test_panel_external_busy_rejects_start_and_stop_waits_for_terminal_cleanup(self):
        panel, _, factory = self.make_panel()
        panel.set_externally_busy(True)
        panel.start()
        self.assertEqual(factory.calls, [])
        self.assertIn("Another MCD", panel.error_display.toPlainText())
        self.assertFalse(panel.start_btn.isEnabled())

    def test_prepare_magnet_is_independent_of_optics_and_sample_controls(self):
        class PreparationController(FakeController):
            mode_recovery_required = False
            has_pending_work = False
            def preflight_magnet_async(self, targets=()):
                target = float(tuple(targets)[0]) if targets else 0.0
                status = SimpleNamespace(
                    quench=False, driven_mode=True, persistent_mode=False,
                    backend_details={"field_control": True},
                )
                return FakeHandle(SimpleNamespace(
                    field_t=target, setpoint_t=target, temperature_k=4.0,
                    status=status,
                ))
            def prepare_driven_mode_async(self):
                return FakeHandle(SimpleNamespace(mode_requested=False))
            def cancel_magnet_preparation(self):
                self.cancel_prepare_calls = getattr(self, "cancel_prepare_calls", 0) + 1

        controller = PreparationController(True)
        panel, _, _ = self.make_panel(controller=controller)
        panel._lf6 = None
        panel._smu = None
        panel.prepare_magnet()
        self.wait_terminal(panel, timeout=5000)
        self.assertIn("Magnet ready", panel.status.text(), panel.error_display.toPlainText())
        self.assertEqual(controller.cancel_prepare_calls if hasattr(controller, "cancel_prepare_calls") else 0, 0)

    def test_recovery_keeps_shared_lock_and_blocks_temperature_mutation(self):
        panel, controller, _ = self.make_panel(controller=FakeController(True))
        controller.mode_recovery_required = True
        panel.temperature_control_enabled.setChecked(True)
        panel._refresh_controls()
        self.assertFalse(panel.start_btn.isEnabled())
        self.assertFalse(panel.disconnect_btn.isEnabled())
        self.assertFalse(panel.apply_temperature_btn.isEnabled())
        panel.apply_temperature()
        self.assertEqual(controller.temperature_configure_calls, [])

        signals = []
        panel.run_state_changed.connect(signals.append)
        panel._launch_worker(FakeWorker({
            "status": "FAILED", "error": "mode uncertain",
            "recovery_required": True, "spectra_written": 0,
        }), "magnet_preparation")
        self.wait_terminal(panel)
        self.assertEqual(signals, [True])
        self.assertTrue(panel._interlock_held)
        self.assertFalse(panel.shutdown(100))

    def test_pending_owner_drain_releases_shared_lock_on_later_state_callback(self):
        panel, controller, _ = self.make_panel(controller=FakeController(True))
        controller.has_pending_work = True
        signals = []
        panel.run_state_changed.connect(signals.append)
        panel._launch_worker(FakeWorker({
            "status": "FAILED", "error": "owner drain",
            "spectra_written": 0,
        }), "magnet_preparation")
        self.wait_terminal(panel)
        self.assertEqual(signals, [True])
        self.assertTrue(panel._interlock_held)
        self.assertFalse(panel.start_btn.isEnabled())
        self.assertFalse(panel.apply_temperature_btn.isEnabled())
        controller.has_pending_work = False
        controller.state_changed.emit(SimpleNamespace(name="IDLE"))
        self.assertEqual(signals, [True, False])
        self.assertFalse(panel._interlock_held)
        self.assertTrue(panel.start_btn.isEnabled())

    def test_terminal_display_preserves_primary_and_cleanup_failures(self):
        panel, _controller, _ = self.make_panel()
        panel._on_terminal({
            "operation": "magnet_preparation", "status": "FAILED",
            "error": "preparation cancelled", "cleanup_error": "magnet Stop failed",
            "spectra_written": 0,
        })
        text = panel.error_display.toPlainText()
        self.assertIn("preparation cancelled", text)
        self.assertIn("magnet Stop failed", text)

    def test_ramp_tables_are_explicit_only_and_render_two_read_only_tabs(self):
        panel, controller, _ = self.make_panel()
        calls = []
        report = SimpleNamespace(
            channel=2,
            current=SimpleNamespace(
                kind="current", reported_count=1,
                rows=(SimpleNamespace(index=0, raw_range="r", raw_rate="0.000000123456789", error=None),),
                errors=(),
            ),
            default=SimpleNamespace(
                kind="default", reported_count=1,
                rows=(SimpleNamespace(index=0, raw_range="d", raw_rate="raw", error=None),),
                errors=(),
            ),
            interrupted=False,
        )
        def read():
            calls.append("read")
            return FakeHandle(report)
        controller.read_ramp_tables_async = read
        self.assertEqual(calls, [])
        panel.read_ramp_tables()
        self.app.processEvents()
        self.assertEqual(calls, ["read"])
        self.assertIs(panel._ramp_tables_report, report)
        panel.view_ramp_tables()
        self.assertIsNotNone(panel._ramp_tables_dialog)
        self.assertEqual(panel._ramp_tables_dialog.findChild(QTabWidget).count(), 2)
        tables = panel._ramp_tables_dialog.findChildren(QTableWidget)
        self.assertTrue(any("0.000000123456789" in table.item(0, 3).text() for table in tables))
        self.assertTrue(all(table.editTriggers() == QTableWidget.EditTrigger.NoEditTriggers for table in tables))
        panel._ramp_tables_dialog.close()

    def test_ramp_timeout_keeps_interlock_until_partial_owner_drain(self):
        panel, controller, _ = self.make_panel()
        report = SimpleNamespace(
            channel=2, interrupted=True,
            current=SimpleNamespace(
                kind="current", reported_count=2,
                rows=(SimpleNamespace(index=0, raw_range="partial-range", raw_rate="partial-rate", error=None),),
                errors=("getRampRate(channel=2, index=1): row failed",),
            ),
            default=SimpleNamespace(kind="default", reported_count=None, rows=(), errors=("default table not read: cancelled",)),
        )
        handle = StagedRampHandle(report)
        controller.read_ramp_tables_async = lambda: handle
        panel.read_ramp_tables()
        panel._poll_ramp_tables(handle)
        self.assertTrue(panel._interlock_held)
        self.assertFalse(panel.start_btn.isEnabled())
        self.assertIn("waiting for owner drain", panel.ramp_tables_status.text().lower())
        self.assertEqual(controller.stop_calls, 0)
        handle.drained = True
        panel._poll_ramp_tables(handle)
        self.assertIsNone(panel._ramp_tables_handle)
        self.assertIn("timed out", panel.ramp_tables_status.text().lower())
        panel.view_ramp_tables()
        self.assertTrue(any(
            table.item(0, 3) is not None and "partial-rate" in table.item(0, 3).text()
            for table in panel._ramp_tables_dialog.findChildren(QTableWidget)
        ))
        self.assertIn("error(s)", panel._log.toPlainText().lower())
        self.assertFalse(panel._interlock_held)
        panel._ramp_tables_dialog.close()

    def test_auto_ramp_runs_once_after_display_cycle_and_view_is_local(self):
        panel, controller, _ = self.make_panel()
        report = SimpleNamespace(
            channel=2, interrupted=False,
            current=SimpleNamespace(kind="current", reported_count=0, rows=(), errors=()),
            default=SimpleNamespace(kind="default", reported_count=0, rows=(), errors=()),
        )
        controller.read_ramp_tables_async = lambda: (setattr(controller, "ramp_calls", controller.ramp_calls + 1)
                                                      or FakeHandle(report))
        panel._on_connected(SimpleNamespace(host="fake"))
        controller.display_cycle_finished.emit(controller.generation)
        self.app.processEvents()
        self.assertEqual(controller.ramp_calls, 1)
        self.assertIsNone(panel._ramp_tables_dialog)
        self.assertIsNotNone(panel._ramp_tables_report)
        before = controller.ramp_calls
        panel.view_ramp_tables()
        self.assertEqual(controller.ramp_calls, before)
        self.assertIsNotNone(panel._ramp_tables_dialog)
        panel._ramp_tables_dialog.close()

    def test_duplicate_connected_and_cycle_notifications_do_not_repeat_auto_ramp(self):
        panel, controller, _ = self.make_panel()
        controller.read_ramp_tables_async = lambda: (setattr(controller, "ramp_calls", controller.ramp_calls + 1)
                                                      or FakeHandle(SimpleNamespace(
                                                          channel=2, interrupted=False,
                                                          current=SimpleNamespace(rows=(), errors=()),
                                                          default=SimpleNamespace(rows=(), errors=()))))
        panel._on_connected(SimpleNamespace(host="fake"))
        panel._on_connected(SimpleNamespace(host="fake"))
        controller.display_cycle_finished.emit(controller.generation)
        controller.display_cycle_finished.emit(controller.generation)
        self.app.processEvents()
        self.assertEqual(controller.ramp_calls, 1)

    def test_auto_ramp_defers_while_busy_then_starts_after_owner_status(self):
        panel, controller, _ = self.make_panel()
        controller.has_pending_work = False
        busy_worker = object()
        panel.worker = busy_worker
        panel._on_connected(SimpleNamespace(host="fake"))
        controller.display_cycle_finished.emit(controller.generation)
        self.assertEqual(controller.ramp_calls, 0)
        self.assertTrue(panel._ramp_auto_pending)
        panel.worker = None
        panel._on_work_status_changed()
        self.app.processEvents()
        self.assertEqual(controller.ramp_calls, 1)

    def test_manual_ramp_consumes_pending_auto_and_repeated_clicks_do_not_duplicate(self):
        panel, controller, _ = self.make_panel()
        handles = []
        controller.read_ramp_tables_async = lambda: (
            setattr(controller, "ramp_calls", controller.ramp_calls + 1)
            or handles.append(ManualTelemetryHandle()) or handles[-1]
        )
        panel._on_connected(SimpleNamespace(host="fake"))
        panel.read_ramp_tables()
        panel.read_ramp_tables()
        self.assertEqual(controller.ramp_calls, 1)
        self.assertFalse(panel._ramp_auto_pending)
        handles[0].complete(SimpleNamespace(
            channel=2, interrupted=False,
            current=SimpleNamespace(kind="current", reported_count=0, rows=(), errors=()),
            default=SimpleNamespace(kind="default", reported_count=0, rows=(), errors=()),
        ))
        panel._poll_ramp_tables(handles[0])
        panel.view_ramp_tables()
        self.assertIsNotNone(panel._ramp_tables_dialog)
        panel._ramp_tables_dialog.close()

    def test_disconnect_invalidates_auto_callback_and_reconnect_rearms_once(self):
        panel, controller, _ = self.make_panel()
        controller.read_ramp_tables_async = lambda: (
            setattr(controller, "ramp_calls", controller.ramp_calls + 1)
            or FakeHandle(SimpleNamespace(
                channel=2, interrupted=False,
                current=SimpleNamespace(rows=(), errors=()),
                default=SimpleNamespace(rows=(), errors=()),
            ))
        )
        panel._on_connected(SimpleNamespace(host="fake"))
        old_generation = controller.generation
        panel._on_disconnected()
        controller.generation = old_generation + 1
        controller.display_cycle_finished.emit(old_generation)
        self.assertEqual(controller.ramp_calls, 0)
        panel._on_connected(SimpleNamespace(host="fake"))
        controller.display_cycle_finished.emit(controller.generation)
        self.app.processEvents()
        self.assertEqual(controller.ramp_calls, 1)

    def test_shutdown_blocks_late_connection_and_cycle_callbacks(self):
        panel, controller, _ = self.make_panel()
        panel._on_connected(SimpleNamespace(host="fake"))
        self.assertTrue(panel.shutdown(1))
        self.assertTrue(panel._closing)
        controller.connected.emit(SimpleNamespace(host="late"))
        controller.display_cycle_finished.emit(controller.generation)
        self.app.processEvents()
        self.assertFalse(panel._ramp_auto_pending)
        self.assertEqual(controller.ramp_calls, 0)

    def test_same_generation_latest_failure_is_shown_with_historical_cache(self):
        panel, controller, _ = self.make_panel()
        old_report = SimpleNamespace(
            channel=2, interrupted=False,
            current=SimpleNamespace(kind="current", reported_count=0, rows=(), errors=()),
            default=SimpleNamespace(kind="default", reported_count=0, rows=(), errors=()),
        )
        controller.read_ramp_tables_async = lambda: FakeHandle(old_report)
        panel.read_ramp_tables()
        panel._poll_ramp_tables()
        old_time = panel._ramp_tables_read_at
        failed = FakeHandle(error=RuntimeError("same generation read failed"), state="FAILED")
        controller.read_ramp_tables_async = lambda: failed
        panel.read_ramp_tables()
        panel._poll_ramp_tables(failed)
        self.assertIs(panel._ramp_tables_report, old_report)
        self.assertEqual(panel._ramp_tables_read_at, old_time)
        self.assertFalse(panel._ramp_tables_cache_valid)
        panel.view_ramp_tables()
        text = " ".join(label.text() for label in panel._ramp_tables_dialog.findChildren(QLabel))
        self.assertIn("same generation read failed", text)
        self.assertIn("historical", text.lower())
        self.assertNotIn("previous connection", text.lower())
        panel._ramp_tables_dialog.close()

    def test_ramp_error_summary_deduplicates_table_and_row_messages(self):
        panel, controller, _ = self.make_panel()
        current_error = "current index 4 unavailable"
        default_errors = tuple(f"default index {index} unavailable" for index in range(1, 5))
        report = SimpleNamespace(
            channel=2, interrupted=True,
            current=SimpleNamespace(
                kind="current", reported_count=5,
                rows=tuple(
                    SimpleNamespace(index=index, raw_range=None, raw_rate=None,
                                    error=current_error if index == 4 else None)
                    for index in range(5)
                ),
                errors=(current_error,),
            ),
            default=SimpleNamespace(
                kind="default", reported_count=5,
                rows=tuple(
                    SimpleNamespace(index=index, raw_range=None, raw_rate=None,
                                    error=default_errors[index - 1] if index else None)
                    for index in range(5)
                ),
                errors=default_errors,
            ),
        )
        controller.read_ramp_tables_async = lambda: FakeHandle(report)
        panel.read_ramp_tables()
        panel._poll_ramp_tables()

        self.assertTrue(panel._ramp_tables_last_error.startswith("5 ramp-table error(s):"))
        aggregate = panel._ramp_tables_last_error
        for message in (current_error,) + default_errors:
            self.assertEqual(aggregate.count(message), 1)
        matching_logs = [
            line for line in panel._log.toPlainText().splitlines()
            if "Ramp-table read completed with" in line
        ]
        self.assertEqual(len(matching_logs), 1)
        self.assertIn("5 error(s)", matching_logs[0])
        for message in (current_error,) + default_errors:
            self.assertEqual(matching_logs[0].count(message), 1)
        self.assertEqual(panel._ramp_tables_report.current.errors, (current_error,))
        self.assertEqual(panel._ramp_tables_report.default.errors, default_errors)

    def test_ramp_error_summary_keeps_table_only_and_row_only_messages(self):
        panel, controller, _ = self.make_panel()
        table_only = "default table was cancelled"
        row_only = "current index 4 unavailable"
        report = SimpleNamespace(
            channel=2, interrupted=True,
            current=SimpleNamespace(
                kind="current", reported_count=5,
                rows=(SimpleNamespace(index=4, raw_range=None, raw_rate=None, error=row_only),),
                errors=(),
            ),
            default=SimpleNamespace(
                kind="default", reported_count=5, rows=(), errors=(table_only,),
            ),
        )
        controller.read_ramp_tables_async = lambda: FakeHandle(report)
        panel.read_ramp_tables()
        panel._poll_ramp_tables()
        self.assertEqual(panel._ramp_tables_last_error.count(table_only), 1)
        self.assertEqual(panel._ramp_tables_last_error.count(row_only), 1)
        self.assertEqual(panel._ramp_tables_report.current.rows[0].error, row_only)
        self.assertEqual(panel._ramp_tables_report.default.errors, (table_only,))
        shared = "same message in both tables"
        cross_table = SimpleNamespace(
            current=SimpleNamespace(errors=(shared,), rows=()),
            default=SimpleNamespace(errors=(shared,), rows=()),
        )
        self.assertEqual(MCD2100Panel._ramp_table_error_messages(cross_table), [shared, shared])

    def test_start_handoff_waits_for_display_owner_then_launches_once(self):
        controller = FakeController(True)
        controller.has_pending_work = True
        controller._display_slots = {"magnet": SimpleNamespace(request_id="display"),
                                     "temperature": None}
        worker = FakeWorker()
        panel, _controller, factory = self.make_panel(controller=controller, workers=[worker])
        panel.start()
        self.assertIsNotNone(panel._workflow_intent)
        self.assertIsNone(panel.worker)
        self.assertEqual(len(factory.calls), 1)
        self.assertTrue(controller.has_pending_work)

        controller.has_pending_work = False
        controller._display_slots["magnet"] = None
        panel._on_work_status_changed()
        self.wait_terminal(panel)
        self.assertEqual(len(factory.calls), 1)
        self.assertIsNone(panel._workflow_intent)

    def test_cancel_waiting_handoff_does_not_run_worker_or_stop_device(self):
        controller = FakeController(True)
        controller.has_pending_work = True
        controller._display_slots = {"magnet": SimpleNamespace(request_id="display"),
                                     "temperature": None}
        worker = FakeWorker()
        panel, _controller, factory = self.make_panel(controller=controller, workers=[worker])
        panel.start()
        panel.stop()
        self.assertIsNone(panel._workflow_intent)
        self.assertIsNone(panel.worker)
        self.assertEqual(len(factory.calls), 1)
        self.assertEqual(controller.stop_calls, 0)

    def test_real_prepare_handoff_waits_for_display_owner_drain(self):
        adapter = EventGatedAdapter()
        controller = AttoDRY2100Controller(
            config=AttoDRY2100Config(poll_interval_s=.02),
            adapter_factory=lambda _config: adapter,
            request_timeout_s=.5,
            shutdown_wait_s=.2,
        )
        controller.connect(timeout=1.0)
        panel = MCD2100Panel(
            controller, worker_factory=WorkerFactory([]), optical_factory=lambda: object()
        )
        self.addCleanup(lambda: panel.shutdown(100))
        self.addCleanup(lambda: controller.shutdown(1.0))
        adapter.arm_gate("read")
        display = controller.read_display_snapshot_async(max_age_s=0, source="manual")
        deadline = time.monotonic() + 1.0
        while not adapter.entered.is_set() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.002)
        self.assertTrue(adapter.entered.is_set())
        panel._refresh_controls()
        self.assertTrue(panel.prepare_magnet_btn.isEnabled())
        self.assertTrue(panel.start_btn.isEnabled())
        panel.prepare_magnet()
        self.assertIsNotNone(panel._workflow_intent)
        self.assertFalse(any(name == "preflight" for name, _, _ in adapter.calls))
        adapter.unblock()
        deadline = time.monotonic() + 1.5
        while panel.worker is None and panel._workflow_intent is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.002)
        self.assertIsNone(panel._workflow_intent)
        self.assertTrue(any(name == "preflight" for name, _, _ in adapter.calls))
        display.wait_drained(1.0)

    def test_real_cancelled_prepare_handoff_waits_for_drain_without_stop(self):
        adapter = EventGatedAdapter()
        controller = AttoDRY2100Controller(
            config=AttoDRY2100Config(poll_interval_s=.02),
            adapter_factory=lambda _config: adapter,
            request_timeout_s=.5,
            shutdown_wait_s=.2,
        )
        controller.connect(timeout=1.0)
        panel = MCD2100Panel(
            controller, worker_factory=WorkerFactory([]), optical_factory=lambda: object()
        )
        self.addCleanup(lambda: panel.shutdown(100))
        self.addCleanup(lambda: controller.shutdown(1.0))
        adapter.arm_gate("read")
        display = controller.read_display_snapshot_async(max_age_s=0, source="manual")
        deadline = time.monotonic() + 1.0
        while not adapter.entered.is_set() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.002)
        self.assertTrue(adapter.entered.is_set())
        panel.prepare_magnet()
        panel.stop()
        self.assertIsNone(panel.worker)
        self.assertIsNone(panel._workflow_intent)
        self.assertEqual([name for name, _, _ in adapter.calls].count("stop"), 0)
        self.assertTrue(panel._interlock_held)
        adapter.unblock()
        deadline = time.monotonic() + 1.0
        while panel._interlock_held and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.002)
        self.assertFalse(panel._interlock_held)
        display.wait_drained(1.0)

    def test_shutdown_blocks_idle_prepare_and_start_before_worker_construction(self):
        panel, controller, factory = self.make_panel()
        self.assertTrue(panel.shutdown(100))
        panel.start()
        panel.prepare_magnet()
        self.assertEqual(factory.calls, [])
        self.assertEqual(controller.stop_calls, 0)

    def test_refused_shutdown_allows_recovery_and_restores_display_after_drain(self):
        panel, controller, _ = self.make_panel()
        controller._display_polling_enabled = True
        controller.set_polling_enabled = lambda enabled: setattr(
            controller, "_display_polling_enabled", bool(enabled)
        )
        panel._temperature_monitor_timer.start(10_000)
        controller.mode_recovery_required = True
        self.assertFalse(panel.shutdown(1))
        self.assertIsNone(panel._workflow_admission_error("magnet_preparation"))
        self.assertTrue(panel._telemetry_age_timer.isActive())
        self.assertFalse(controller._display_polling_enabled)
        self.assertFalse(panel._temperature_monitor_timer.isActive())
        worker = FakeWorker({"operation": "magnet_preparation", "status": "COMPLETED"})
        self.assertTrue(panel._handoff_worker(worker, "magnet_preparation"))
        self.wait_terminal(panel)
        controller.mode_recovery_required = False
        panel._restore_workflow_display_state()
        self.assertTrue(controller._display_polling_enabled)
        self.assertTrue(panel._temperature_monitor_timer.isActive())

    def test_refused_shutdown_for_owner_drain_does_not_permanently_close_panel(self):
        panel, controller, _ = self.make_panel()
        controller.has_pending_work = True
        self.assertFalse(panel.shutdown(1))
        controller.has_pending_work = False
        self.assertIsNone(panel._workflow_admission_error("magnet_preparation"))

    def test_operation_polls_release_drained_timeouts_but_wait_for_owner(self):
        for attribute, poll_name in (
            ("_connect_handle", "_poll_connect"),
            ("_disconnect_handle", "_poll_disconnect"),
            ("_temperature_apply_handle", "_poll_apply_temperature"),
        ):
            with self.subTest(operation=attribute):
                panel, controller, _ = self.make_panel()
                handle = FakeHandle(error=TimeoutError("client deadline"), state="TIMED_OUT_DRAINING")
                handle.drained_done = False
                setattr(panel, attribute, handle)
                getattr(panel, poll_name)()
                self.assertIs(getattr(panel, attribute), handle)
                handle.drained_done = True
                getattr(panel, poll_name)()
                self.assertIsNone(getattr(panel, attribute))
                self.assertIn("client deadline", panel.error_display.toPlainText())

    def test_runner_construction_failure_rolls_back_worker_and_interlock(self):
        worker = FakeWorker()
        panel, controller, factory = self.make_panel(workers=[worker])
        with patch("ui.mcd2100_panel._Runner", side_effect=RuntimeError("runner failed")):
            panel.start()
        self.assertEqual(len(factory.calls), 1)
        self.assertIsNone(panel.worker)
        self.assertIsNone(panel.runner)
        self.assertIsNone(panel.thread)
        self.assertFalse(panel._interlock_held)
        self.assertIn("runner failed", panel.error_display.toPlainText())
        self.assertEqual(controller.stop_calls, 0)

    def test_invalidated_handoff_restores_saved_polling_and_monitor_state(self):
        controller = FakeController(True)
        controller.has_pending_work = True
        controller._display_slots = {"magnet": SimpleNamespace(request_id="display"),
                                     "temperature": None}
        panel, _controller, _factory = self.make_panel(controller=controller, workers=[FakeWorker()])
        panel._temperature_monitor_timer.start(10_000)
        panel.start()
        self.assertIsNotNone(panel._workflow_intent)
        panel.set_externally_busy(True)
        self.assertIsNone(panel._workflow_intent)
        controller.has_pending_work = False
        controller._display_slots["magnet"] = None
        panel.set_externally_busy(False)
        self.assertTrue(panel._temperature_monitor_timer.isActive())

    def test_cancel_waiting_does_not_enable_saved_false_polling_before_drain(self):
        controller = FakeController(True)
        controller._display_polling_enabled = False
        controller.set_polling_enabled = lambda enabled: setattr(
            controller, "_display_polling_enabled", bool(enabled)
        )
        controller.has_pending_work = True
        controller._display_slots = {
            "magnet": SimpleNamespace(request_id="display"),
            "temperature": None,
        }
        panel, _controller, _factory = self.make_panel(
            controller=controller, workers=[FakeWorker()]
        )
        panel.start()
        self.assertIsNotNone(panel._workflow_intent)
        panel.stop()
        self.assertFalse(controller._display_polling_enabled)
        controller.has_pending_work = False
        controller._display_slots["magnet"] = None
        panel._on_work_status_changed()
        self.assertFalse(controller._display_polling_enabled)

    def test_terminal_worker_does_not_fallback_enable_saved_false_polling(self):
        controller = FakeController(True)
        controller._display_polling_enabled = False
        controller.set_polling_enabled = lambda enabled: setattr(
            controller, "_display_polling_enabled", bool(enabled)
        )
        panel, _controller, _factory = self.make_panel(
            controller=controller, workers=[FakeWorker()]
        )
        panel.start()
        self.wait_terminal(panel)
        self.assertFalse(controller._display_polling_enabled)

    def test_terminal_restore_captures_state_before_reentrant_interlock_release(self):
        adapter = EventGatedAdapter()
        controller = AttoDRY2100Controller(
            config=AttoDRY2100Config(poll_interval_s=.02),
            adapter_factory=lambda _config: adapter,
            request_timeout_s=.5,
            shutdown_wait_s=.2,
        )
        controller.connect(timeout=1.0)
        # Finish the controller's queued connection setup before panel wiring,
        # matching the normal MainWindow construction order.
        self.app.processEvents()
        time.sleep(.05)
        self.app.processEvents()
        class ReentrantWorker:
            def __init__(self, owner, *args, **kwargs):
                self.owner = owner

            def run(self):
                handle = self.owner.preflight_magnet_async((0.0,))
                handle.result(timeout=1.0)
                handle.wait_drained(timeout=1.0)
                return {"status": "COMPLETED", "spectra_written": 0}

            def request_cancel(self):
                return None

        panel = MCD2100Panel(
            controller,
            worker_factory=lambda owner, *args, **kwargs: ReentrantWorker(owner),
            optical_factory=lambda: object(),
        )
        self.addCleanup(lambda: panel.shutdown(100))
        self.addCleanup(lambda: controller.shutdown(1.0))
        controller.set_polling_enabled(False)
        # MainWindow's shared-lock callback can synchronously re-enter the
        # panel and consume saved restoration state during release.
        panel.run_state_changed.connect(lambda _running: panel.set_externally_busy(False))
        panel.start()
        self.wait_terminal(panel)
        self.assertFalse(controller._display_polling_enabled)

    def test_saved_display_state_is_discarded_on_generation_change_disconnect_and_shutdown(self):
        controller = FakeController(True)
        controller._display_polling_enabled = False
        controller.set_polling_enabled = lambda enabled: setattr(
            controller, "_display_polling_enabled", bool(enabled)
        )
        panel, _controller, _factory = self.make_panel(controller=controller)
        panel._workflow_restore_state = SimpleNamespace(
            polling=True, monitor=False, generation=controller.generation
        )
        controller.generation += 1
        panel._restore_workflow_display_state()
        self.assertIsNone(panel._workflow_restore_state)
        self.assertFalse(controller._display_polling_enabled)

        panel._workflow_restore_state = SimpleNamespace(
            polling=True, monitor=False, generation=controller.generation
        )
        panel._on_disconnected()
        self.assertIsNone(panel._workflow_restore_state)
        panel._workflow_restore_state = SimpleNamespace(
            polling=True, monitor=False, generation=controller.generation
        )
        self.assertTrue(panel.shutdown(100))
        self.assertIsNone(panel._workflow_restore_state)

    def test_auto_ramp_timeout_keeps_interlock_and_opens_no_dialog(self):
        panel, controller, _ = self.make_panel()
        report = SimpleNamespace(
            channel=2, interrupted=True,
            current=SimpleNamespace(rows=(), errors=("current failed",)),
            default=SimpleNamespace(rows=(), errors=("default skipped",)),
        )
        handle = StagedRampHandle(report)
        controller.read_ramp_tables_async = lambda: handle
        panel._on_connected(SimpleNamespace(host="fake"))
        controller.display_cycle_finished.emit(controller.generation)
        panel._poll_ramp_tables(handle)
        self.assertTrue(panel._interlock_held)
        self.assertIsNone(panel._ramp_tables_dialog)
        handle.drained = True
        panel._poll_ramp_tables(handle)
        self.assertFalse(panel._interlock_held)
        self.assertIsNone(panel._ramp_tables_dialog)
        self.assertIn("timed out", panel.ramp_tables_status.text().lower())

    def test_real_controller_auto_ramp_waits_for_display_cycle_and_uses_owner(self):
        adapter = EventGatedAdapter()
        controller = AttoDRY2100Controller(
            config=AttoDRY2100Config(poll_interval_s=.02),
            adapter_factory=lambda _config: adapter,
            request_timeout_s=.5,
            shutdown_wait_s=.2,
        )
        panel = MCD2100Panel(controller, worker_factory=WorkerFactory([]),
                             optical_factory=lambda: object())
        self.addCleanup(lambda: panel.shutdown(100))
        self.addCleanup(lambda: controller.shutdown(1.0))
        controller.connect(timeout=1.0)
        deadline = time.monotonic() + 1.5
        while sum(1 for item in adapter.calls if item[0] == "ramp_read") < 1 and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertEqual(sum(1 for item in adapter.calls if item[0] == "ramp_read"), 1)
        self.assertIsNone(panel._ramp_tables_dialog)

    def test_real_controller_auto_ramp_timeout_recovers_after_owner_drain(self):
        adapter = EventGatedAdapter()
        adapter.arm_gate("ramp_read")
        controller = AttoDRY2100Controller(
            config=AttoDRY2100Config(poll_interval_s=.02),
            adapter_factory=lambda _config: adapter,
            request_timeout_s=.05,
            shutdown_wait_s=.2,
        )
        panel = MCD2100Panel(controller, worker_factory=WorkerFactory([]),
                             optical_factory=lambda: object())
        self.addCleanup(lambda: panel.shutdown(100))
        self.addCleanup(lambda: controller.shutdown(1.0))
        controller.connect(timeout=1.0)
        deadline = time.monotonic() + 1.5
        while not adapter.entered.is_set() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertTrue(adapter.entered.is_set())
        self.app.processEvents()
        self.assertTrue(panel._interlock_held)
        adapter.unblock()
        deadline = time.monotonic() + 1.5
        while panel._ramp_tables_handle is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertIsNone(panel._ramp_tables_handle)
        self.assertFalse(panel._interlock_held)
        self.assertIsNone(panel._ramp_tables_dialog)

    def test_failed_new_generation_keeps_old_report_historical_and_stale(self):
        panel, controller, _ = self.make_panel()
        old_report = SimpleNamespace(
            channel=2, interrupted=False,
            current=SimpleNamespace(kind="current", reported_count=0, rows=(), errors=()),
            default=SimpleNamespace(kind="default", reported_count=0, rows=(), errors=()),
        )
        controller.read_ramp_tables_async = lambda: FakeHandle(old_report)
        panel.read_ramp_tables()
        self.app.processEvents()
        self.assertIs(panel._ramp_tables_report, old_report)
        controller.generation = 2
        failed = FakeHandle(error=RuntimeError("new generation read failed"), state="FAILED")
        controller.read_ramp_tables_async = lambda: failed
        panel.read_ramp_tables()
        panel._poll_ramp_tables(failed)
        self.assertIs(panel._ramp_tables_report, old_report)
        self.assertFalse(panel._ramp_tables_cache_valid)
        panel.view_ramp_tables()
        self.assertIn("previous connection / stale", panel._ramp_tables_dialog.findChildren(QLabel)[0].text())
        panel._ramp_tables_dialog.close()

    def test_rejected_auto_admission_keeps_generation_arrangement_pending(self):
        panel, controller, _ = self.make_panel()
        rejected = FakeHandle(error=RuntimeError("controller admission rejected"), state="FAILED")
        rejected.accepted = False
        controller.read_ramp_tables_async = lambda: rejected
        panel._on_connected(SimpleNamespace(host="fake"))
        panel._ramp_auto_telemetry_ready = True
        panel._start_ramp_tables_read(auto=True)
        self.assertFalse(rejected.accepted)
        self.assertTrue(panel._ramp_auto_pending)
        self.assertFalse(panel._ramp_auto_attempted)

    def test_temperature_monitor_pauses_submission_during_ramp_read(self):
        panel, controller, _ = self.make_panel()
        calls = []
        controller.read_display_temperature_async = lambda **_kwargs: (
            calls.append("temperature") or FakeHandle(SimpleNamespace())
        )
        panel._temperature_monitor_timer.start(10_000)
        panel._ramp_tables_handle = SimpleNamespace()
        panel._monitor_applied_temperature()
        self.assertEqual(calls, [])
        self.assertTrue(panel._temperature_monitor_timer.isActive())
        panel._ramp_tables_handle = None
        panel._monitor_applied_temperature()
        self.assertEqual(calls, ["temperature"])
        panel._temperature_monitor_handle = None

    def test_refresh_coalesces_and_displays_magnet_before_temperature_drain(self):
        panel, controller, _ = self.make_panel()
        magnet = ManualTelemetryHandle()
        temperature = ManualTelemetryHandle()
        calls = []
        controller.read_display_snapshot_async = lambda **_kwargs: calls.append("magnet") or magnet
        controller.read_display_temperature_async = lambda **_kwargs: calls.append("temperature") or temperature
        panel.refresh_telemetry()
        panel.refresh_telemetry()
        self.assertEqual(calls, ["magnet", "temperature"])
        self.assertEqual(panel.refresh_btn.text(), "Refreshing…")
        magnet.complete(SimpleNamespace(field_t=1.25, setpoint_t=1.5, temperature_k=1.8,
                                        status=SimpleNamespace(backend_details={"field_control": True}, quench=False)),
                        completed_at=10.0)
        panel._poll_telemetry(panel._telemetry_cycle)
        self.assertIn("1.25", panel.current_field.text())
        self.assertIsNotNone(panel._telemetry_cycle)
        temperature.complete(SimpleNamespace(sample_temperature_k=4.0, sample_setpoint_k=4.0,
                                             sample_control_active=True, sample_ramp_active=False,
                                             vti_temperature_k=2.0), completed_at=11.0)
        panel._poll_telemetry(panel._telemetry_cycle)
        self.assertIsNone(panel._telemetry_cycle)
        self.assertEqual(panel.refresh_btn.text(), "Refresh telemetry")
        self.assertTrue(panel.refresh_btn.isEnabled())

    def test_live_owner_read_drain_releases_interlock_after_worker_finishes(self):
        adapter = EventGatedAdapter()
        controller = AttoDRY2100Controller(
            config=AttoDRY2100Config(maximum_field_t=6.0,
                                     minimum_temperature_k=1.0,
                                     maximum_temperature_k=7.0,
                                     poll_interval_s=.02),
            adapter_factory=lambda _config: adapter,
            request_timeout_s=.5,
            shutdown_wait_s=.2,
        )
        controller.connect(timeout=1.0)
        panel = MCD2100Panel(controller, worker_factory=WorkerFactory([]),
                             optical_factory=lambda: object())
        panel._connected = True
        self.addCleanup(lambda: panel.shutdown(100))
        self.addCleanup(lambda: controller.shutdown(1.0))
        states = []
        panel.run_state_changed.connect(states.append)
        worker_gate = threading.Event()
        panel._launch_worker(FakeWorker(gate=worker_gate), "magnet_preparation")
        adapter.arm_gate("read")
        reading = controller.read_snapshot_async()
        self.assertTrue(adapter.entered.wait(1.0))
        worker_gate.set()
        self.wait_terminal(panel)
        self.assertEqual(states, [True])
        self.assertTrue(panel._interlock_held)
        adapter.unblock()
        self.assertEqual(reading.result(1.0)["field"], 0.0)
        deadline = time.monotonic() + 1.0
        while states != [True, False] and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertEqual(states, [True, False])
        self.assertFalse(panel._interlock_held)

    def test_temperature_monitor_cache_hit_preserves_owner_completion_age(self):
        adapter = EventGatedAdapter()
        controller = AttoDRY2100Controller(
            config=AttoDRY2100Config(poll_interval_s=.02),
            adapter_factory=lambda _config: adapter,
            request_timeout_s=.5,
            shutdown_wait_s=.2,
        )
        controller.connect(timeout=1.0)
        panel = MCD2100Panel(controller, worker_factory=WorkerFactory([]),
                             optical_factory=lambda: object())
        panel._connected = True
        self.addCleanup(lambda: panel.shutdown(100))
        self.addCleanup(lambda: controller.shutdown(1.0))
        first = controller.read_display_temperature_async(max_age_s=0)
        first.wait_drained(1.0)
        completed_at = first.completed_at
        panel._monitor_applied_temperature()
        panel._poll_applied_temperature()
        self.assertEqual(panel._last_temperature_success_at, completed_at)

    def test_plain_snapshot_with_stale_generation_is_ignored(self):
        panel, controller, _ = self.make_panel()
        panel._connected = True
        panel.current_field.setText("current")
        panel._on_snapshot(
            SimpleNamespace(field_t=9.0, setpoint_t=9.0, temperature_k=1.8,
                             status=SimpleNamespace(
                                 backend_details={"field_control": True}, quench=False)),
            completed_at=3.0,
            generation=1,
        )
        before = panel.current_field.text()
        controller.generation = 2
        panel._on_snapshot(
            SimpleNamespace(field_t=10.0, setpoint_t=10.0, temperature_k=1.8,
                             status=SimpleNamespace(
                                 backend_details={"field_control": True}, quench=False)),
            completed_at=4.0,
            generation=1,
        )
        self.assertEqual(panel.current_field.text(), before)

    def test_ui_never_accesses_raw_2100_adapter_or_vendor_sdk(self):
        source = inspect.getsource(MCD2100Panel)
        self.assertNotIn("controller.adapter", source)
        self.assertNotIn("setHSetPoint", source)
        self.assertNotIn("startFieldControl", source)
        self.assertNotIn("stopFieldControl", source)
        panel, controller, _ = self.make_panel()
        panel.start()
        self.wait_terminal(panel)
        with self.assertRaises(AssertionError):
            getattr(controller, "adapter")


if __name__ == "__main__":
    unittest.main()
