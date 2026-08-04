from __future__ import annotations

import os
import importlib.util
import sys
import types
import unittest
from unittest.mock import Mock

import pandas as pd

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

from ui.presets_panel import (
    BATCH_SCHEMA,
    PresetsPanel,
    _build_acquisition_schedule,
    _parse_sweep_constants,
    _solve_condition_line,
)
from utils.when_condition import evaluate_when_expression, validate_when_expression


class SweepLineSolverTests(unittest.TestCase):
    def test_batch_first_uses_same_forward_loop_order_for_every_row(self):
        sequence = [
            {"Rotation1 Angle (deg)": 10.0},
            {"Rotation1 Angle (deg)": 20.0},
        ]
        batch = pd.DataFrame(
            [
                {"Run": True, "condition_label": "row-1", "repeat": 1},
                {"Run": True, "condition_label": "row-2", "repeat": 1},
                {"Run": True, "condition_label": "row-3", "repeat": 1},
            ]
        )

        schedule = _build_acquisition_schedule(
            sequence,
            batch,
            acquisition_grouping="batch_first",
        )

        self.assertEqual(
            [
                (task["row"]["condition_label"], task["ctx"]["Rotation1 Angle (deg)"])
                for task in schedule
            ],
            [
                ("row-1", 10.0),
                ("row-1", 20.0),
                ("row-2", 10.0),
                ("row-2", 20.0),
                ("row-3", 10.0),
                ("row-3", 20.0),
            ],
        )

    def test_batch_first_schedule_preserves_any_resolved_loop_context(self):
        sequence = [
            {"Rotation1 Angle (deg)": 0.0, "Center Wavelength (nm)": 750.0},
            {"Rotation1 Angle (deg)": 45.0, "Center Wavelength (nm)": 810.0},
        ]
        batch = pd.DataFrame(
            [
                {
                    "Run": True,
                    "condition_label": "conditional",
                    "repeat": 1,
                    "When": "Center_Wavelength == 810",
                },
                {"Run": True, "condition_label": "all", "repeat": 1},
            ]
        )

        schedule = _build_acquisition_schedule(
            sequence,
            batch,
            acquisition_grouping="batch_first",
        )

        self.assertEqual(
            [
                (task["row"]["condition_label"], task["seq_i"])
                for task in schedule
            ],
            [("conditional", 1), ("all", 0), ("all", 1)],
        )
        self.assertEqual(schedule[0]["ctx"], sequence[1])

    def test_loop_first_remains_the_default_schedule(self):
        sequence = [{"Rotation1 Angle (deg)": 10.0}, {"Rotation1 Angle (deg)": 20.0}]
        batch = pd.DataFrame(
            [
                {"Run": True, "condition_label": "row-1", "repeat": 1},
                {"Run": True, "condition_label": "row-2", "repeat": 1},
            ]
        )

        schedule = _build_acquisition_schedule(sequence, batch)

        self.assertEqual(
            [(task["seq_i"], task["row_i"]) for task in schedule],
            [(0, 0), (0, 1), (1, 0), (1, 1)],
        )

    def test_constant_parser_accepts_arrays_and_inclusive_ranges(self):
        self.assertEqual(_parse_sweep_constants("4"), [4.0])
        self.assertEqual(
            _parse_sweep_constants("[-4, -2, 0, 2, 4]"),
            [-4.0, -2.0, 0.0, 2.0, 4.0],
        )
        self.assertEqual(
            _parse_sweep_constants("-4:2:4, 7"),
            [-4.0, -2.0, 0.0, 2.0, 4.0, 7.0],
        )
        self.assertEqual(
            _parse_sweep_constants("4:-2:-4"),
            [4.0, 2.0, 0.0, -2.0, -4.0],
        )

    def test_constant_parser_rejects_invalid_ranges(self):
        with self.assertRaisesRegex(ValueError, "zero step"):
            _parse_sweep_constants("-4:0:4")
        with self.assertRaisesRegex(ValueError, "reverse the step sign"):
            _parse_sweep_constants("-4:-1:4")
        with self.assertRaisesRegex(ValueError, "At most 500"):
            _parse_sweep_constants("-200:0.001:200")

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

    def test_measurement_order_options_use_action_based_names(self):
        panel = PresetsPanel()
        self.assertEqual(
            [
                panel._acquisition_group_combo.itemText(index)
                for index in range(panel._acquisition_group_combo.count())
            ],
            [
                "At each loop setting, run all batch rows",
                "For each batch row, run all loop settings",
            ],
        )

    def test_loop_and_batch_sections_have_matching_semantic_colors(self):
        panel = PresetsPanel()
        self.assertEqual(panel._loop_group.title(), "LOOP VARIABLES")
        self.assertEqual(panel._batch_group.title(), "BATCH SWEEP ROWS")
        self.assertIn("#7C3AED", panel._loop_group.styleSheet())
        self.assertIn("#2563EB", panel._batch_group.styleSheet())
        self.assertIn("#EDE9FE", panel._loop_table.styleSheet())
        self.assertIn("#DBEAFE", panel._batch_table.styleSheet())

        panel._set_acquisition_grouping("loop_first")
        panel._on_acquisition_order_changed()
        loop_first = panel._measurement_order_indicator.text()
        self.assertLess(loop_first.index("LOOP SETTING"), loop_first.index("BATCH ROW"))

        panel._set_acquisition_grouping("batch_first")
        panel._on_acquisition_order_changed()
        batch_first = panel._measurement_order_indicator.text()
        self.assertLess(batch_first.index("BATCH ROW"), batch_first.index("LOOP SETTING"))

    def test_dual_gate_safety_control_is_a_compact_single_row(self):
        panel = PresetsPanel()
        self.assertEqual(panel._safety_bar.maximumHeight(), 36)
        self.assertEqual(panel._safe_jump_spin.width(), 96)
        self.assertLessEqual(panel._safety_bar.sizeHint().height(), 36)
        self.assertEqual(panel._tree.minimumHeight(), 220)

    def test_calculator_physical_limits_round_trip_in_session(self):
        panel = PresetsPanel()
        panel._sweep_calc._doping_min_spin.setValue(-7.5)
        panel._sweep_calc._doping_max_spin.setValue(8.5)
        panel._sweep_calc._efield_min_spin.setValue(-2.25)
        panel._sweep_calc._efield_max_spin.setValue(3.25)
        panel._sweep_calc._repeat_spin.setValue(7)

        restored = PresetsPanel()
        restored.restore_session_state(panel.capture_session_state())

        self.assertAlmostEqual(restored._sweep_calc._doping_min_spin.value(), -7.5)
        self.assertAlmostEqual(restored._sweep_calc._doping_max_spin.value(), 8.5)
        self.assertAlmostEqual(restored._sweep_calc._efield_min_spin.value(), -2.25)
        self.assertAlmostEqual(restored._sweep_calc._efield_max_spin.value(), 3.25)
        self.assertEqual(restored._sweep_calc._repeat_spin.value(), 7)

    def test_constant_expression_round_trips_and_documents_range_syntax(self):
        panel = PresetsPanel()
        panel._sweep_calc._constant_edit.setText("[-4, 0, 4:2:8]")

        restored = PresetsPanel()
        restored.restore_session_state(panel.capture_session_state())

        self.assertEqual(
            restored._sweep_calc._constant_edit.text(),
            "[-4, 0, 4:2:8]",
        )
        tooltip = restored._sweep_calc._constant_edit.toolTip()
        self.assertIn("start:step:stop", tooltip)
        self.assertIn("-4:2:4", tooltip)

    def test_calculator_array_builds_and_bulk_inserts_all_rows(self):
        panel = PresetsPanel()
        calc = panel._sweep_calc
        calc._op_combo.setCurrentText("−")
        calc._ratio_spin.setValue(0.5)
        calc._repeat_spin.setValue(4)
        calc._constant_edit.setText("-1:1:1")
        calc._recalculate()

        self.assertEqual(len(calc._calculated_rows), 3)
        self.assertFalse(calc._multi_preview.isHidden())
        self.assertEqual(calc._multi_preview.rowCount(), 3)
        self.assertEqual(calc._multi_preview.columnCount(), 4)
        self.assertEqual(
            calc._multi_preview.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        self.assertIn("Add All 3 Rows", calc._add_btn.text())
        self.assertEqual(
            [row["condition_label"] for row in calc._calculated_rows],
            ["TG−0.5BG=-1", "TG−0.5BG=0", "TG−0.5BG=1"],
        )

        self.assertEqual(
            [row["repeat"] for row in calc._calculated_rows],
            [4, 4, 4],
        )

        panel._batch_table.selectRow(0)
        history_before = panel._batch_history_index
        calc._on_add_clicked()

        self.assertEqual(panel._batch_table.rowCount(), 4)
        self.assertEqual(panel._batch_history_index, history_before + 1)
        self.assertEqual(
            [
                panel._batch_table.item(row, BATCH_SCHEMA.index("condition_label")).text()
                for row in range(1, 4)
            ],
            ["TG−0.5BG=-1", "TG−0.5BG=0", "TG−0.5BG=1"],
        )

        self.assertEqual(
            [
                panel._batch_table.item(row, BATCH_SCHEMA.index("repeat")).text()
                for row in range(1, 4)
            ],
            ["4", "4", "4"],
        )

    def test_calculator_outputs_obey_physical_limits(self):
        panel = PresetsPanel()
        calc = panel._sweep_calc
        calc._op_combo.setCurrentText("−")
        calc._ratio_spin.setValue(0.5)
        calc._constant_edit.setText("0")
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
        calc._constant_edit.setText("40")
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
        calc._constant_edit.setText("39")
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

    def test_filename_preview_applies_coefficient_to_manual_power(self):
        panel = PresetsPanel()
        panel._sample_edit.setText("Sample1")
        panel._laser_edit.setText("532")
        panel._power_edit.setText("1100")
        panel._mode_combo_name.blockSignals(True)
        panel._mode_combo_name.setCurrentText("PL")
        panel._mode_combo_name.blockSignals(False)
        panel._power_coeff_edit.blockSignals(True)
        panel._power_coeff_edit.setText("2")
        panel._power_coeff_edit.blockSignals(False)
        panel._manual_filename_parts.add("laser_power")

        panel._refresh_filename_preview()

        self.assertIn("532nm2200.000uW", panel._filename_preview_lbl.text())
        self.assertEqual(
            panel._filename_parts_table.item(1, 2).text(),
            "532nm2200.000uW",
        )

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

    def test_duplicate_batch_row_copies_every_field_below_selection(self):
        panel = PresetsPanel()
        table = panel._batch_table
        values = {
            "When": "Center_Wavelength == 860",
            "condition_label": "duplicate-me",
            "repeat": "3",
            "frames": "41",
            "Vbg_start": "-4.5",
            "Vbg_stop": "2.5",
            "Vtg_start": "1.25",
            "Vtg_stop": "8.75",
            "Vbias_start": "-0.1",
            "Vbias_stop": "0.2",
        }
        for name, value in values.items():
            table.item(0, BATCH_SCHEMA.index(name)).setText(value)
        table.item(0, BATCH_SCHEMA.index("Run")).setCheckState(
            Qt.CheckState.Unchecked
        )
        table.item(0, BATCH_SCHEMA.index("MeasurePower")).setCheckState(
            Qt.CheckState.Checked
        )
        table.selectRow(0)

        panel._duplicate_batch_row()

        self.assertEqual(table.rowCount(), 2)
        self.assertEqual(panel._selected_batch_row_index(), 1)
        for column, name in enumerate(BATCH_SCHEMA):
            source = table.item(0, column)
            duplicate = table.item(1, column)
            if name in {"Run", "MeasurePower"}:
                self.assertEqual(duplicate.checkState(), source.checkState())
            else:
                self.assertEqual(duplicate.text(), source.text())
        self.assertTrue(panel._tables_dirty)

    def test_create_rev_row_swaps_all_voltage_pairs_and_uses_rev_label(self):
        panel = PresetsPanel()
        table = panel._batch_table
        source_values = {
            "condition_label": "TG-1.05BG=20",
            "Vbg_start": "-12.5",
            "Vbg_stop": "-2.381",
            "Vtg_start": "6.875",
            "Vtg_stop": "17.5",
            "Vbias_start": "-0.2",
            "Vbias_stop": "0.4",
        }
        for name, value in source_values.items():
            table.item(0, BATCH_SCHEMA.index(name)).setText(value)
        table.selectRow(0)

        panel._create_rev_batch_row()

        self.assertEqual(table.rowCount(), 2)
        self.assertEqual(
            table.item(1, BATCH_SCHEMA.index("condition_label")).text(),
            "TG-1.05BG=20_Rev",
        )
        for start_name, stop_name in (
            ("Vbg_start", "Vbg_stop"),
            ("Vtg_start", "Vtg_stop"),
            ("Vbias_start", "Vbias_stop"),
        ):
            self.assertEqual(
                table.item(1, BATCH_SCHEMA.index(start_name)).text(),
                source_values[stop_name],
            )
            self.assertEqual(
                table.item(1, BATCH_SCHEMA.index(stop_name)).text(),
                source_values[start_name],
            )
        self.assertEqual(panel._selected_batch_row_index(), 1)

    def test_run_selection_actions_toggle_rows_as_a_group(self):
        panel = PresetsPanel()
        panel._batch_table.selectRow(0)
        panel._add_batch_row()
        panel._add_batch_row()
        table = panel._batch_table
        run_col = BATCH_SCHEMA.index("Run")
        table.selectRow(1)

        panel._run_only_selected_rows()

        self.assertEqual(
            [
                table.item(row, run_col).checkState()
                for row in range(table.rowCount())
            ],
            [
                Qt.CheckState.Unchecked,
                Qt.CheckState.Checked,
                Qt.CheckState.Unchecked,
            ],
        )
        panel._set_all_rows_run_state(True)
        self.assertTrue(
            all(
                table.item(row, run_col).checkState()
                == Qt.CheckState.Checked
                for row in range(table.rowCount())
            )
        )
        panel._set_selected_rows_run_state(False)
        self.assertEqual(
            table.item(1, run_col).checkState(),
            Qt.CheckState.Unchecked,
        )

    def test_quick_repeat_updates_selected_or_all_rows_as_one_undoable_edit(self):
        panel = PresetsPanel()
        panel._batch_table.selectRow(0)
        panel._add_batch_row()
        panel._add_batch_row()
        table = panel._batch_table
        repeat_col = BATCH_SCHEMA.index("repeat")

        table.selectRow(1)
        panel._batch_repeat_spin.setValue(4)
        history_before = panel._batch_history_index
        panel._set_selected_rows_repeat()

        self.assertEqual(
            [table.item(row, repeat_col).text() for row in range(3)],
            ["1", "4", "1"],
        )
        self.assertEqual(panel._batch_history_index, history_before + 1)
        panel._undo_batch_edit()
        self.assertEqual(
            [table.item(row, repeat_col).text() for row in range(3)],
            ["1", "1", "1"],
        )

        panel._batch_repeat_spin.setValue(6)
        panel._set_all_rows_repeat()
        self.assertEqual(
            [table.item(row, repeat_col).text() for row in range(3)],
            ["6", "6", "6"],
        )

    def test_row_clipboard_paste_and_undo_redo(self):
        panel = PresetsPanel()
        table = panel._batch_table
        label_col = BATCH_SCHEMA.index("condition_label")
        table.item(0, label_col).setText("clipboard-row")
        table.selectRow(0)
        panel._copy_batch_rows()

        panel._paste_batch_rows()

        self.assertEqual(table.rowCount(), 2)
        self.assertEqual(table.item(1, label_col).text(), "clipboard-row")
        panel._undo_batch_edit()
        self.assertEqual(table.rowCount(), 1)
        panel._redo_batch_edit()
        self.assertEqual(table.rowCount(), 2)
        self.assertEqual(table.item(1, label_col).text(), "clipboard-row")

    def test_auto_frames_uses_largest_selected_voltage_range(self):
        panel = PresetsPanel()
        table = panel._batch_table
        panel._safe_jump_spin.setValue(0.5)
        for name, value in {
            "Vbg_start": "0",
            "Vbg_stop": "1",
            "Vtg_start": "-1",
            "Vtg_stop": "1",
            "Vbias_start": "",
            "Vbias_stop": "",
        }.items():
            table.item(0, BATCH_SCHEMA.index(name)).setText(value)
        table.selectRow(0)

        panel._auto_frames_for_selected_rows()

        self.assertEqual(
            table.item(0, BATCH_SCHEMA.index("frames")).text(),
            "5",
        )
        self.assertEqual(panel._batch_validation_issues(), [])

    def test_validation_reports_invalid_condition_and_numeric_cell(self):
        panel = PresetsPanel()
        table = panel._batch_table
        table.item(0, BATCH_SCHEMA.index("When")).setText(
            "Center_Wavelength = 860"
        )
        table.item(0, BATCH_SCHEMA.index("Vtg_start")).setText("not-a-number")

        issues = panel._batch_validation_issues()

        self.assertTrue(any("Use ==" in issue for issue in issues))
        self.assertTrue(any("Vtg_start must be numeric" in issue for issue in issues))

    def test_move_to_edge_and_delete_disabled_rows(self):
        panel = PresetsPanel()
        table = panel._batch_table
        label_col = BATCH_SCHEMA.index("condition_label")
        run_col = BATCH_SCHEMA.index("Run")
        table.item(0, label_col).setText("first")
        table.selectRow(0)
        panel._add_batch_row()
        table.item(1, label_col).setText("middle")
        panel._add_batch_row()
        table.item(2, label_col).setText("last")
        table.selectRow(2)

        panel._move_batch_row_to_edge(top=True)

        self.assertEqual(table.item(0, label_col).text(), "last")
        table.item(1, run_col).setCheckState(Qt.CheckState.Unchecked)
        panel._delete_disabled_batch_rows()
        self.assertEqual(table.rowCount(), 2)
        self.assertNotIn(
            "first",
            [
                table.item(row, label_col).text()
                for row in range(table.rowCount())
            ],
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
