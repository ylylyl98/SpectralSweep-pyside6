from __future__ import annotations

import os
import unittest

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QAbstractSpinBox

from ui.megasweep_panel import CoordSystem, MegaSweepPanel, _PreviewPlot


class MegaSweepLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _show_panel(self, width: int, height: int) -> MegaSweepPanel:
        panel = MegaSweepPanel()
        panel.resize(width, height)
        panel.show()
        self.app.processEvents()
        panel._refresh_settings_scroll_range()
        self.app.processEvents()
        self.addCleanup(panel.close)
        return panel

    def test_progress_geometry_is_cached_and_sliced_without_rescanning_points(self):
        preview = _PreviewPlot()
        self.addCleanup(preview.close)
        preview._coord = CoordSystem.PHYSICAL
        preview._ratio = 1.0
        preview._axis_a_vals = np.asarray([0.0, 1.0])
        preview._valid_points = [
            {"axis_a": outer, "axis_b": inner, "raw": (outer + 10, inner + 20, 0)}
            for outer in (0.0, 1.0)
            for inner in (0.0, 1.0, 2.0)
        ]
        preview._cache_progress_geometry()

        preview.update_progress(4, 3)

        completed_x, completed_y = preview._completed_axis_pts.getData()
        self.assertEqual(completed_x.tolist(), [0.0, 0.0, 0.0, 1.0])
        self.assertEqual(completed_y.tolist(), [0.0, 1.0, 2.0, 0.0])
        done_x, _ = preview._done_axis.getData()
        np.testing.assert_allclose(done_x[:2], [0.0, 0.0])
        self.assertTrue(np.isnan(done_x[2]))
        current_x, current_y = preview._current_axis_stripe.getData()
        self.assertEqual(current_x.tolist(), [1.0, 1.0])
        self.assertEqual(current_y.tolist(), [0.0, 2.0])

    @staticmethod
    def _wheel_event(
        *,
        angle_y: int = 0,
        pixel_y: int = 0,
        modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
    ) -> QWheelEvent:
        return QWheelEvent(
            QPointF(5, 5),
            QPointF(5, 5),
            QPoint(0, pixel_y),
            QPoint(0, angle_y),
            Qt.MouseButton.NoButton,
            modifiers,
            Qt.ScrollPhase.ScrollUpdate,
            False,
        )

    def test_wide_layout_has_visible_splitter_and_no_horizontal_settings_overflow(self):
        panel = self._show_panel(1000, 700)

        self.assertEqual(
            panel._splitter.orientation(),
            Qt.Orientation.Horizontal,
        )
        self.assertGreaterEqual(panel._splitter.handle(1).width(), 8)
        self.assertFalse(
            panel._settings_scroll.horizontalScrollBar().isVisible()
        )
        self.assertLessEqual(
            panel._settings_content.width(),
            panel._settings_scroll.viewport().width(),
        )
        self.assertLessEqual(
            panel._settings_content.minimumSizeHint().width(),
            panel._settings_scroll.viewport().width(),
        )

        for widget in (
            panel._axis_selector._outer,
            panel._axis_selector._inner,
            panel._axis_a._start,
            panel._axis_a._stop,
            panel._axis_a._step,
            panel._axis_a._mode,
        ):
            self.assertGreater(widget.width(), 0)
            self.assertLessEqual(
                widget.geometry().right(),
                widget.parentWidget().rect().right(),
            )

    def test_settings_cards_reflow_between_wide_and_narrow_panes(self):
        panel = self._show_panel(1500, 760)

        self.assertTrue(panel._settings_two_column)
        self.assertEqual(panel._axis_a.y(), panel._axis_b.y())
        self.assertGreater(panel._axis_b.x(), panel._axis_a.x())
        self.assertEqual(
            panel._fixed_widget.y(),
            panel._safety_widget.y(),
        )
        self.assertLessEqual(
            panel._settings_content.minimumSizeHint().width(),
            panel._settings_scroll.viewport().width(),
        )

        panel.resize(1000, 760)
        self.app.processEvents()
        panel._refresh_settings_scroll_range()
        self.app.processEvents()

        self.assertFalse(panel._settings_two_column)
        self.assertEqual(panel._axis_a.x(), panel._axis_b.x())
        self.assertGreater(
            panel._axis_b.y(),
            panel._axis_a.geometry().bottom(),
        )
        self.assertLessEqual(
            panel._settings_content.minimumSizeHint().width(),
            panel._settings_scroll.viewport().width(),
        )

        panel.resize(1500, 760)
        self.app.processEvents()
        panel._refresh_settings_scroll_range()
        self.app.processEvents()
        self.assertTrue(panel._settings_two_column)
        self.assertEqual(panel._axis_a.y(), panel._axis_b.y())

    def test_numeric_editors_stay_compact(self):
        panel = self._show_panel(1500, 760)
        spins = panel._settings_content.findChildren(QAbstractSpinBox)

        self.assertGreaterEqual(len(spins), 21)
        for spin in spins:
            self.assertLessEqual(spin.width(), 132)
            self.assertLess(
                spin.width(),
                max(1, spin.parentWidget().width()),
            )

    def test_long_path_does_not_force_the_preview_or_splitter_wider(self):
        panel = self._show_panel(1200, 720)
        panel._sample_edit.setText("sample-" + ("very-long-name-" * 30))
        panel._refresh_preview()
        self.app.processEvents()

        splitter_sizes = panel._splitter.sizes()
        self.assertLessEqual(sum(splitter_sizes), panel._splitter.width())
        self.assertLess(panel._right_panel.minimumSizeHint().width(), 600)
        self.assertLess(panel._summary.minimumSizeHint().width(), 200)
        self.assertIn("Full path:", panel._summary.toPlainText())
        self.assertEqual(
            panel._summary.toolTip(),
            panel._summary.toPlainText(),
        )

    def test_narrow_layout_stacks_settings_above_preview(self):
        panel = self._show_panel(900, 700)

        self.assertEqual(
            panel._splitter.orientation(),
            Qt.Orientation.Vertical,
        )
        self.assertGreaterEqual(panel._settings_scroll.height(), 220)
        self.assertGreaterEqual(panel._right_panel.height(), 300)
        self.assertGreaterEqual(panel._splitter.handle(1).height(), 8)

    def test_compact_height_is_not_forced_below_the_usable_screen(self):
        panel = self._show_panel(900, 450)

        self.assertEqual(panel.height(), 450)
        self.assertEqual(
            panel._splitter.orientation(),
            Qt.Orientation.Vertical,
        )
        self.assertGreater(
            panel._settings_scroll.verticalScrollBar().maximum(),
            0,
        )

    def test_bottom_metadata_group_is_reachable_after_dynamic_height_changes(self):
        panel = self._show_panel(1000, 600)
        panel._timing_section.set_expanded(True)
        panel._optical_group.set_expanded(True)
        panel._metadata_group.set_expanded(True)
        panel._timing_widget._toggle.setChecked(True)
        self.app.processEvents()
        panel._refresh_settings_scroll_range()
        self.app.processEvents()

        scrollbar = panel._settings_scroll.verticalScrollBar()
        self.assertGreater(scrollbar.maximum(), 0)
        scrollbar.setValue(scrollbar.maximum())
        self.app.processEvents()

        viewport = panel._settings_scroll.viewport()
        top = panel._metadata_group.mapTo(
            viewport, panel._metadata_group.rect().topLeft()
        ).y()
        bottom = panel._metadata_group.mapTo(
            viewport, panel._metadata_group.rect().bottomLeft()
        ).y()
        self.assertGreaterEqual(top, 0)
        self.assertLess(bottom, viewport.height())

    def test_optional_sections_collapse_without_losing_values(self):
        panel = self._show_panel(1000, 500)
        panel._timing_section.set_expanded(True)
        panel._optical_group.set_expanded(True)
        panel._metadata_group.set_expanded(True)
        panel._timing_widget._settle.setValue(0.875)
        panel._exp_spin.setValue(145.5)
        panel._sample_edit.setText("collapse-preserves-this")
        panel._refresh_settings_scroll_range()
        self.app.processEvents()
        expanded_height = panel._settings_content.minimumHeight()

        panel._timing_section.set_expanded(False)
        panel._optical_group.set_expanded(False)
        panel._metadata_group.set_expanded(False)
        panel._refresh_settings_scroll_range()
        self.app.processEvents()
        collapsed_height = panel._settings_content.minimumHeight()

        self.assertLess(collapsed_height, expanded_height)
        self.assertTrue(panel._safety_widget.isVisible())
        self.assertAlmostEqual(panel._timing_widget._settle.value(), 0.875)
        self.assertAlmostEqual(panel._exp_spin.value(), 145.5)
        self.assertEqual(
            panel._sample_edit.text(),
            "collapse-preserves-this",
        )

    def test_mouse_wheel_over_spin_box_scrolls_settings_instead_of_changing_value(self):
        panel = self._show_panel(1000, 500)
        scrollbar = panel._settings_scroll.verticalScrollBar()
        spin = panel._safety_widget._spins["vtg_min"]
        original_value = spin.value()
        scrollbar.setValue(0)

        event = self._wheel_event(angle_y=-120)
        QApplication.sendEvent(spin, event)
        self.app.processEvents()

        self.assertGreater(scrollbar.value(), 0)
        self.assertEqual(spin.value(), original_value)

    def test_all_numeric_inputs_ignore_wheel_when_focused(self):
        panel = self._show_panel(1000, 600)
        panel._timing_section.set_expanded(True)
        panel._timing_widget._toggle.setChecked(True)
        self.app.processEvents()
        panel._refresh_settings_scroll_range()

        spins = panel._settings_content.findChildren(QAbstractSpinBox)
        expected = [
            panel._coord_widget._ratio_spin,
            panel._axis_a._start,
            panel._axis_a._stop,
            panel._axis_a._step,
            panel._axis_b._start,
            panel._axis_b._stop,
            panel._axis_b._step,
            *panel._fixed_widget._spins.values(),
            *panel._safety_widget._spins.values(),
            panel._timing_widget._settle,
            panel._timing_widget._extra_overhead,
            panel._timing_widget._ramp_step,
            panel._timing_widget._step_delay_ms,
            panel._exp_spin,
            panel._center_spin,
            panel._frames_spin,
        ]
        self.assertEqual(
            {id(spin) for spin in spins},
            {id(spin) for spin in expected},
        )
        self.assertGreaterEqual(len(spins), 21)

        for spin in spins:
            if not spin.isEnabled():
                continue
            original = spin.value()
            spin.setFocus()
            QApplication.sendEvent(
                spin,
                self._wheel_event(angle_y=120),
            )
            QApplication.sendEvent(
                spin,
                self._wheel_event(angle_y=-120),
            )
            self.assertEqual(
                spin.value(),
                original,
                f"wheel changed {type(spin).__name__}",
            )

    def test_wheel_is_safe_at_both_scroll_boundaries(self):
        panel = self._show_panel(1000, 450)
        scrollbar = panel._settings_scroll.verticalScrollBar()
        spin = panel._coord_widget._ratio_spin
        spin.setValue(1.25)
        spin.setFocus()

        scrollbar.setValue(0)
        QApplication.sendEvent(
            spin,
            self._wheel_event(angle_y=120),
        )
        self.assertEqual(scrollbar.value(), 0)
        self.assertAlmostEqual(spin.value(), 1.25)

        scrollbar.setValue(scrollbar.maximum())
        QApplication.sendEvent(
            spin,
            self._wheel_event(angle_y=-120),
        )
        self.assertEqual(scrollbar.value(), scrollbar.maximum())
        self.assertAlmostEqual(spin.value(), 1.25)

    def test_touchpad_wheel_cannot_change_numeric_values(self):
        panel = self._show_panel(1000, 500)
        scrollbar = panel._settings_scroll.verticalScrollBar()
        spin = panel._frames_spin
        spin.setValue(37)
        spin.setFocus()
        scrollbar.setValue(0)

        QApplication.sendEvent(
            spin,
            self._wheel_event(pixel_y=-18),
        )
        self.app.processEvents()

        self.assertEqual(spin.value(), 37)
        self.assertGreater(scrollbar.value(), 0)

    def test_dynamic_fixed_input_is_wheel_guarded_immediately(self):
        panel = self._show_panel(1000, 600)
        panel._vbias_available = lambda: True
        panel._sync_vbias_availability()

        first = panel._fixed_widget._spins["Vbias"]
        self.assertTrue(first.property("settings_wheel_redirect"))
        panel._axis_selector._inner.setCurrentText("Vbias")
        second = panel._fixed_widget._spins["Vbg"]

        self.assertIsNot(first, second)
        self.assertTrue(second.property("settings_wheel_redirect"))
        second.setValue(0.75)
        QApplication.sendEvent(
            second,
            self._wheel_event(angle_y=120),
        )
        self.assertAlmostEqual(second.value(), 0.75)

    def test_restored_splitter_sizes_are_converted_to_a_safe_ratio(self):
        panel = self._show_panel(1200, 720)
        panel.restore_session_state({"splitter_sizes": [1, 5000]})
        self.app.processEvents()
        panel._apply_responsive_layout(force=True)
        self.app.processEvents()

        left, right = panel._splitter.sizes()
        self.assertGreaterEqual(left, 420)
        self.assertGreaterEqual(right, 420)
        state = panel.capture_session_state()
        self.assertGreaterEqual(state["splitter_ratio"], 0.25)
        self.assertLessEqual(state["splitter_ratio"], 0.55)


if __name__ == "__main__":
    unittest.main()
