from __future__ import annotations

import os
import importlib.util
import sys
import types
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import (
    QApplication,
    QAbstractSpinBox,
    QDoubleSpinBox,
    QMainWindow,
    QSpinBox,
)

if importlib.util.find_spec("pylablib") is None:
    pylablib_stub = types.ModuleType("pylablib")
    devices_stub = types.ModuleType("pylablib.devices")
    devices_stub.Thorlabs = types.SimpleNamespace(ElliptecMotor=object)
    pylablib_stub.devices = devices_stub
    sys.modules.setdefault("pylablib", pylablib_stub)
    sys.modules.setdefault("pylablib.devices", devices_stub)

from ui.presets_panel import BATCH_SCHEMA, PresetsPanel, _solve_condition_line
from utils.when_condition import evaluate_when_expression, validate_when_expression


class SweepLineSolverTests(unittest.TestCase):
    def test_constant_efield_line_is_clipped_by_doping_limits(self):
        result = _solve_condition_line(
            "−", 0.5, 0.0,
            -10.0, 10.0, -10.0, 10.0,
            -4.0, 4.0, -1.0, 1.0,
        )
        self.assertIsNotNone(result)
        vbg_start, vbg_stop, vtg_start, vtg_stop = result
        self.assertAlmostEqual(vbg_start, -4.0)
        self.assertAlmostEqual(vbg_stop, 4.0)
        self.assertAlmostEqual(vtg_start, -2.0)
        self.assertAlmostEqual(vtg_stop, 2.0)

    def test_constant_doping_line_is_clipped_by_efield_limits(self):
        result = _solve_condition_line(
            "+", 0.5, 0.0,
            -10.0, 10.0, -10.0, 10.0,
            -1.0, 1.0, -3.0, 3.0,
        )
        self.assertIsNotNone(result)
        vbg_start, vbg_stop, vtg_start, vtg_stop = result
        self.assertAlmostEqual(vbg_start, -3.0)
        self.assertAlmostEqual(vbg_stop, 3.0)
        self.assertAlmostEqual(vtg_start, 1.5)
        self.assertAlmostEqual(vtg_stop, -1.5)

    def test_rejects_line_whose_fixed_coordinate_is_outside_limits(self):
        result = _solve_condition_line(
            "−", 0.5, 5.0,
            -10.0, 10.0, -10.0, 10.0,
            -20.0, 20.0, -1.0, 1.0,
        )
        self.assertIsNone(result)

    def test_when_conditions_fail_closed_and_explain_comparison_syntax(self):
        context = {"Center_Wavelength": 770.0}
        self.assertTrue(
            evaluate_when_expression("Center_Wavelength == 770", context)
        )
        self.assertFalse(
            evaluate_when_expression("Center_Wavelength = 770", context)
        )
        error = validate_when_expression(
            "Center_Wavelength = 770", {"Center_Wavelength"}
        )
        self.assertIsNotNone(error)
        self.assertIn("Use ==", error)
        self.assertFalse(evaluate_when_expression("Unknown == 1", context))


class SweepLinePanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_calculator_physical_limits_round_trip_in_session(self):
        panel = PresetsPanel()
        panel._sweep_calc._doping_min_spin.setValue(-7.5)
        panel._sweep_calc._doping_max_spin.setValue(8.5)
        panel._sweep_calc._efield_min_spin.setValue(-2.25)
        panel._sweep_calc._efield_max_spin.setValue(3.25)

        restored = PresetsPanel()
        restored.restore_session_state(panel.capture_session_state())

        self.assertAlmostEqual(restored._sweep_calc._doping_min_spin.value(), -7.5)
        self.assertAlmostEqual(restored._sweep_calc._doping_max_spin.value(), 8.5)
        self.assertAlmostEqual(restored._sweep_calc._efield_min_spin.value(), -2.25)
        self.assertAlmostEqual(restored._sweep_calc._efield_max_spin.value(), 3.25)

    def test_calculator_outputs_obey_physical_limits(self):
        panel = PresetsPanel()
        calc = panel._sweep_calc
        calc._op_combo.setCurrentText("−")
        calc._ratio_spin.setValue(0.5)
        calc._constant_spin.setValue(0.0)
        calc._vtg_min_spin.setValue(-10.0)
        calc._vtg_max_spin.setValue(10.0)
        calc._vbg_min_spin.setValue(-10.0)
        calc._vbg_max_spin.setValue(10.0)
        calc._doping_min_spin.setValue(-4.0)
        calc._doping_max_spin.setValue(4.0)
        calc._efield_min_spin.setValue(-1.0)
        calc._efield_max_spin.setValue(1.0)
        calc._recalculate()

        self.assertAlmostEqual(calc._vbg_start_spin.value(), -4.0)
        self.assertAlmostEqual(calc._vbg_stop_spin.value(), 4.0)
        self.assertAlmostEqual(calc._vtg_start_spin.value(), -2.0)
        self.assertAlmostEqual(calc._vtg_stop_spin.value(), 2.0)
        self.assertIsNone(calc._calculated_row_error())
        self.assertTrue(calc._add_btn.isEnabled())

        calc._vtg_stop_spin.setValue(6.0)
        self.assertIsNotNone(calc._calculated_row_error())
        self.assertFalse(calc._add_btn.isEnabled())

    def test_rounded_boundary_endpoint_is_not_rejected(self):
        panel = PresetsPanel()
        calc = panel._sweep_calc
        calc._op_combo.setCurrentText("−")
        calc._ratio_spin.setValue(1.05)
        calc._constant_spin.setValue(40.0)
        calc._vtg_min_spin.setValue(-25.0)
        calc._vtg_max_spin.setValue(30.0)
        calc._vbg_min_spin.setValue(-25.0)
        calc._vbg_max_spin.setValue(30.0)
        calc._doping_min_spin.setValue(-6.0)
        calc._doping_max_spin.setValue(12.0)
        calc._efield_min_spin.setValue(-20.0)
        calc._efield_max_spin.setValue(50.0)
        calc._recalculate()

        displayed_doping = (
            calc._vtg_start_spin.value()
            + calc._ratio_spin.value() * calc._vbg_start_spin.value()
        )
        self.assertAlmostEqual(displayed_doping, -6.00004, places=7)
        self.assertIsNone(calc._calculated_row_error())
        self.assertTrue(calc._add_btn.isEnabled())

    def test_recalculation_replaces_focused_stale_endpoint(self):
        panel = PresetsPanel()
        panel.show()
        calc = panel._sweep_calc
        calc._op_combo.setCurrentText("−")
        calc._ratio_spin.setValue(1.05)
        calc._constant_spin.setValue(39.0)
        calc._vtg_min_spin.setValue(-25.0)
        calc._vtg_max_spin.setValue(30.0)
        calc._vbg_min_spin.setValue(-25.0)
        calc._vbg_max_spin.setValue(30.0)
        calc._doping_min_spin.setValue(-5.0)
        calc._doping_max_spin.setValue(12.0)
        calc._efield_min_spin.setValue(-20.0)
        calc._efield_max_spin.setValue(50.0)
        calc._vtg_start_spin.setValue(-1.0)
        calc._vtg_start_spin.setFocus()

        calc._recalculate()

        self.assertTrue(calc._vtg_start_spin.isReadOnly())
        self.assertAlmostEqual(calc._vtg_start_spin.value(), 17.0, places=4)
        self.assertAlmostEqual(calc._vbg_start_spin.value(), -20.9524, places=4)
        self.assertAlmostEqual(calc._vtg_stop_spin.value(), 25.5, places=4)
        self.assertAlmostEqual(calc._vbg_stop_spin.value(), -12.8571, places=4)
        self.assertIsNone(calc._calculated_row_error())
        self.assertTrue(calc._add_btn.isEnabled())
        panel.close()

    def test_invalid_when_marks_draft_and_blocks_apply_and_run(self):
        panel = PresetsPanel()
        when_item = panel._batch_table.item(0, BATCH_SCHEMA.index("When"))
        when_item.setText("Center_Wavelength = 770")
        self.app.processEvents()

        self.assertTrue(panel._tables_dirty)
        self.assertEqual(panel._draft_badge.text(), "Invalid draft")
        self.assertFalse(panel._apply_btn.isEnabled())
        self.assertFalse(panel._run_btn.isEnabled())
        self.assertIn("Use ==", when_item.toolTip())

    def test_table_edit_requires_apply_before_it_can_be_run(self):
        panel = PresetsPanel()
        label_item = panel._batch_table.item(
            0, BATCH_SCHEMA.index("condition_label")
        )
        label_item.setText("edited")
        self.app.processEvents()

        self.assertTrue(panel._tables_dirty)
        self.assertEqual(panel._draft_badge.text(), "Unapplied changes")
        self.assertTrue(panel._apply_btn.isEnabled())
        self.assertFalse(panel._run_btn.isEnabled())

        panel._on_apply()

        self.assertFalse(panel._tables_dirty)
        self.assertEqual(panel._draft_badge.text(), "Plan applied")

    def test_filename_preview_never_reads_hardware_synchronously(self):
        panel = PresetsPanel()
        measure_item = panel._batch_table.item(
            0, BATCH_SCHEMA.index("MeasurePower")
        )
        measure_item.setCheckState(Qt.CheckState.Checked)
        adapter = Mock()
        panel._pm = types.SimpleNamespace(is_connected=True, adapter=adapter)

        panel._refresh_filename_preview()

        adapter.get_power.assert_not_called()

    def test_successful_finish_fills_progress(self):
        panel = PresetsPanel()
        panel._total_acq = 2
        panel._total_points = 17
        panel._progress.setMaximum(17)
        panel._progress.setValue(0)

        panel._on_finished(True, "complete")

        self.assertEqual(panel._progress.maximum(), 17)
        self.assertEqual(panel._progress.value(), 17)
        self.assertEqual(panel._status_lbl.text(), "Completed")

    def test_calculator_spin_boxes_have_no_arrows_or_wheel_changes(self):
        panel = PresetsPanel()
        calc = panel._sweep_calc
        spins = calc.findChildren(QDoubleSpinBox) + calc.findChildren(QSpinBox)
        self.assertGreater(len(spins), 0)
        for spin in spins:
            self.assertEqual(
                spin.buttonSymbols(),
                QAbstractSpinBox.ButtonSymbols.NoButtons,
            )

        class _WheelEvent:
            ignored = False

            def ignore(self):
                self.ignored = True

        event = _WheelEvent()
        calc._ratio_spin.wheelEvent(event)
        self.assertTrue(event.ignored)

    def test_expanding_calculator_does_not_grow_panel_height(self):
        panel = PresetsPanel()
        panel.resize(1400, 600)
        panel.show()
        self.app.processEvents()
        height_before = panel.height()

        panel._sweep_calc._toggle.setChecked(True)
        self.app.processEvents()
        self.app.processEvents()

        self.assertEqual(panel.height(), height_before)
        self.assertGreater(
            panel._workflow_scroll.verticalScrollBar().maximum(),
            0,
        )
        self.assertGreater(
            panel._results_scroll.verticalScrollBar().maximum(),
            0,
        )
        panel.close()

    def test_expanding_calculator_preserves_maximized_window(self):
        host = QMainWindow()
        panel = PresetsPanel()
        host.setCentralWidget(panel)
        host.showMaximized()
        self.app.processEvents()
        geometry_before = host.geometry()

        panel._sweep_calc._toggle.setChecked(True)
        self.app.processEvents()
        self.app.processEvents()

        self.assertTrue(host.isMaximized())
        self.assertEqual(host.geometry(), geometry_before)
        host.close()

    def test_batch_add_inserts_below_selection_and_rows_move(self):
        panel = PresetsPanel()
        label_col = BATCH_SCHEMA.index("condition_label")
        panel._batch_table.item(0, label_col).setText("first")

        panel._batch_table.selectRow(0)
        panel._add_batch_row()
        panel._batch_table.item(1, label_col).setText("second")
        panel._batch_table.item(1, BATCH_SCHEMA.index("Run")).setCheckState(
            Qt.CheckState.Unchecked
        )

        panel._batch_table.selectRow(0)
        panel._add_batch_row()
        labels = [
            panel._batch_table.item(row, label_col).text()
            for row in range(panel._batch_table.rowCount())
        ]
        self.assertEqual(labels, ["first", "baseline", "second"])

        panel._batch_table.selectRow(2)
        panel._move_batch_row_up()
        labels = [
            panel._batch_table.item(row, label_col).text()
            for row in range(panel._batch_table.rowCount())
        ]
        self.assertEqual(labels, ["first", "second", "baseline"])
        self.assertEqual(panel._selected_batch_row_index(), 1)
        self.assertEqual(
            panel._batch_table.item(1, BATCH_SCHEMA.index("Run")).checkState(),
            Qt.CheckState.Unchecked,
        )

    def test_batch_move_blocks_cell_events_and_refreshes_once(self):
        panel = PresetsPanel()
        panel._batch_table.selectRow(0)
        panel._add_batch_row()
        panel._batch_table.selectRow(1)
        panel._add_batch_row()
        panel._batch_table.selectRow(1)
        changed_spy = QSignalSpy(panel._batch_table.itemChanged)
        preview = Mock()
        panel._update_filename_preview = preview

        panel._move_batch_row_down()

        self.assertEqual(changed_spy.count(), 0)
        preview.assert_called_once_with()
        self.assertEqual(panel._selected_batch_row_index(), 2)

    def test_batch_move_stops_at_table_boundaries(self):
        panel = PresetsPanel()
        panel._batch_table.selectRow(0)
        panel._add_batch_row()
        labels_before = [
            panel._batch_table.item(row, BATCH_SCHEMA.index("condition_label")).text()
            for row in range(panel._batch_table.rowCount())
        ]
        preview = Mock()
        panel._update_filename_preview = preview

        panel._batch_table.selectRow(0)
        preview.reset_mock()
        panel._move_batch_row_up()
        preview.assert_not_called()
        panel._batch_table.selectRow(panel._batch_table.rowCount() - 1)
        preview.reset_mock()
        panel._move_batch_row_down()

        labels_after = [
            panel._batch_table.item(row, BATCH_SCHEMA.index("condition_label")).text()
            for row in range(panel._batch_table.rowCount())
        ]
        self.assertEqual(labels_after, labels_before)
        preview.assert_not_called()


if __name__ == "__main__":
    unittest.main()
