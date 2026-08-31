from __future__ import annotations

import inspect
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QObject, QTimer, Qt, Signal
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QGroupBox, QHeaderView, QScrollArea, QSizePolicy, QStatusBar, QTabWidget, QWidget

from ui.main_window import MainWindow
from ui.mcd2100_panel import MCD2100Panel, _LightFieldRotationService
from utils.mcd_common import MODE_DIRECT, MODE_DOPING_EFIELD, build_condition_batch


class FakeHandle:
    def __init__(self, value=None, error=None, state="SUCCEEDED"):
        self.value, self.error = value, error
        self.state = SimpleNamespace(name=state)

    def result(self, timeout=None):
        if self.error:
            raise self.error
        return self.value

    def wait_drained(self, timeout=None):
        if self.error:
            raise self.error
        return self.value


class FakeController(QObject):
    connected = Signal(object)
    disconnected = Signal()
    state_changed = Signal(object)
    snapshot_updated = Signal(object)
    error = Signal(str)

    def __init__(self, connected=True):
        super().__init__()
        self.state = SimpleNamespace(name="IDLE" if connected else "DISCONNECTED")
        self.connect_handle = FakeHandle(SimpleNamespace(host="fake-2100"))
        self.connect_calls = 0
        self.stop_calls = 0
        self.temperature_configure_calls = []

    def connect_async(self):
        self.connect_calls += 1
        return self.connect_handle

    def disconnect_async(self):
        self.state.name = "DISCONNECTED"
        return FakeHandle(True)

    def read_snapshot_async(self):
        status = SimpleNamespace(quench=False, backend_details={"field_control": True})
        return FakeHandle(SimpleNamespace(field_t=1.25, setpoint_t=1.5, temperature_k=1.8, status=status))

    def read_temperature_snapshot_async(self):
        return FakeHandle(SimpleNamespace(
            sample_temperature_k=12.0, vti_temperature_k=2.0,
            sample_setpoint_k=12.0, sample_control_active=True,
            sample_ramp_active=False,
        ))

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
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

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

    def test_mcd2100_start_is_gated_before_magnet_workflow_when_lightfield_not_ready(self):
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
        self.assertIsNone(panel.worker)
        self.assertIn("LightField is not ready", panel.error_display.toPlainText())
        self.assertEqual(factory.calls, [])

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
        self.assertEqual(len(shared_lf.ensure_calls), 1)
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
