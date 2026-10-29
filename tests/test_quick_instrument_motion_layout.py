"""Motion regrouping preserves source controls and hardware safety gates."""
import copy
import os
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from tests.test_esp32_stage_v2 import _Control
from ui.instrument_panel import (
    _ESP32ImagingStageSection, _RotationBlock, _StageSection,
)
from ui.quick_instrument_motion import configure_motion_section
from ui.quick_parameter_forms import _rows, install_parameter_forms
from utils.config import cfg


def _find_quick_item(item, name):
    if item.objectName() == name:
        return item
    for child in item.childItems():
        found = _find_quick_item(child, name)
        if found is not None:
            return found
    return None


class _RotationController(QObject):
    connected = Signal(str, str)
    disconnected = Signal(str)
    error = Signal(str)
    axes_scanned = Signal(list)

    def is_connected(self, _slot):
        return False


class _StageController(QObject):
    connected = Signal(str)
    disconnected = Signal()
    error = Signal(str)
    axes_scanned = Signal(list)
    is_connected = False

    def disconnect_instrument(self):
        pass


class QuickInstrumentMotionLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls._previous_font = cls.app.font()
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
        if font.is_file():
            QFontDatabase.addApplicationFont(str(font))
        cls.app.setFont(QFont("Segoe UI", 9))

    @classmethod
    def tearDownClass(cls):
        cls.app.setFont(cls._previous_font)

    def setUp(self):
        self.widgets = []
        self.saved_rotation = copy.deepcopy(cfg.rotation)
        self.saved_stage = copy.deepcopy(cfg.stage)

    def tearDown(self):
        cfg.rotation = self.saved_rotation
        cfg.stage = self.saved_stage
        for widget in self.widgets:
            manifest = getattr(widget, "_quick_parameter_forms", None)
            if manifest:
                for form in manifest["forms"]:
                    form.shutdown()
            widget.close()
            widget.deleteLater()
        self.app.processEvents()

    def keep(self, widget):
        self.widgets.append(widget)
        return widget

    def test_rotation_daily_rows_and_profile_order_preserve_sources(self):
        controller = _RotationController()
        section = self.keep(_RotationBlock("rot1", controller))
        original_rows = [
            section.layout().itemAt(i).layout() for i in range(8, 11)]
        controls = list(section._ctrl_widgets)
        profile = section._profile_group
        groups = configure_motion_section(section)
        self.assertIs(groups, configure_motion_section(section))
        self.assertEqual(list(groups), ["connection", "motion", "profile"])
        self.assertEqual(section._ctrl_widgets, controls)
        self.assertIs(groups["profile"], profile)
        form = groups["motion"].layout().itemAt(0).layout()
        self.assertEqual([form.itemAt(i).layout() for i in range(form.count())], original_rows)
        rows = _rows(form, {}, "motion")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row.bounded for row in rows))
        section._type_combo.setCurrentText("Newport ESP300 (shared)")
        self.assertFalse(profile.isHidden())
        self.assertFalse(section._axis_row_widget.isHidden())
        section._type_combo.setCurrentText("Thorlabs Elliptec")
        self.assertTrue(profile.isHidden())
        self.assertTrue(section._axis_row_widget.isHidden())

    def test_stage_keeps_axis_layout_and_backend_visibility(self):
        section = self.keep(_StageSection(_StageController()))
        axis_layout = section._axis_row
        target = section._target_spn
        groups = configure_motion_section(section)
        self.assertIs(section._axis_row, axis_layout)
        self.assertIs(section._target_spn, target)
        self.assertIs(section._range_hint.parentWidget(), groups["motion"])
        section._type_combo.setCurrentIndex(section._type_combo.findData("esp300"))
        self.assertFalse(section._scan_axes_btn.isHidden())
        self.assertFalse(section._axis_combo.isHidden())
        section._type_combo.setCurrentIndex(section._type_combo.findData("elliptec"))
        self.assertTrue(section._scan_axes_btn.isHidden())
        self.assertTrue(section._axis_combo.isHidden())

    def test_stage_numeric_editors_fit_full_values_with_long_units_at_sidebar_widths(self):
        controller = _StageController()
        # No enumeration, connections, motion commands, or QSettings are used.
        with patch("ui.instrument_panel._list_com_ports", return_value=[]), \
                patch.object(_StageSection, "_on_refresh", lambda *args: None):
            section = self.keep(_StageSection(controller))
        section._type_combo.setCurrentIndex(section._type_combo.findData("elliptec"))
        section._apply_stage_profile()
        originals = (section._target_spn, section._jog_spn)
        configure_motion_section(section)
        section._set_ctrl_enabled(True)
        manifest = install_parameter_forms(section, "instruments")
        entries = {source: (form, field) for form in manifest["forms"]
                   for field, source in form.adapter._fields.items()}

        for width in (350, 440):
            section.resize(width, 1000)
            section.show()
            for source in originals:
                form, field = entries[source]
                rendered = _find_quick_item(form.rootObject(), "parameter_" + field.fieldId)
                self.assertIsNotNone(rendered)
                self.assertEqual(source.suffix(), " stage units")
                for value in (300.0, source.maximum()):
                    with self.subTest(width=width, field=field.fieldId, value=value):
                        source.setValue(value)
                        form.adapter.refresh()
                        for _ in range(50):
                            QTest.qWait(10)
                            if (rendered.width() >= 80
                                    and rendered.property("text") == source.cleanText()
                                    and not form._height_timer.isActive()):
                                break
                        self.assertEqual(rendered.property("text"), source.cleanText())
                        self.assertTrue(source.cleanText().endswith(".000"))
                        self.assertGreaterEqual(rendered.width(), 80)
                        available = (rendered.width() - rendered.property("leftPadding")
                                     - rendered.property("rightPadding"))
                        self.assertLessEqual(rendered.property("contentWidth"), available + .5)
                        composite = rendered.parentItem()
                        bounds = composite.mapRectToScene(composite.boundingRect())
                        self.assertGreaterEqual(bounds.left(), -.5)
                        self.assertLessEqual(bounds.right(), form.width() + .5)
                        self.assertLessEqual(form.width(), section.width())

                # The visible editor still edits this exact original source.
                self.assertTrue(field.edit("1234.567"))
                self.assertTrue(field.commit("1234.567"))
                self.assertAlmostEqual(source.value(), 1234.567)
                self.assertIs(entries[source][0].adapter._fields[field], source)

            # Wider number+unit controls may wrap actions onto the next line;
            # the buttons must stay inside the form rather than scroll sideways.
            for source in (section._move_btn, section._home_btn,
                           section._jog_neg_btn, section._jog_pos_btn):
                form, field = entries[source]
                rendered = _find_quick_item(form.rootObject(), "parameter_" + field.fieldId)
                bounds = rendered.mapRectToScene(rendered.boundingRect())
                self.assertGreaterEqual(bounds.left(), -.5)
                self.assertLessEqual(bounds.right(), form.width() + .5)

        self.assertEqual((section._target_spn, section._jog_spn), originals)

    def test_migrated_position_controls_still_follow_source_interlocks(self):
        sections = [self.keep(_RotationBlock("rot1", _RotationController())),
                    self.keep(_StageSection(_StageController()))]
        for section in sections:
            with self.subTest(section=type(section).__name__):
                configure_motion_section(section)
                manifest = install_parameter_forms(section, "instruments")
                field_sources = {id(widget): field for form in manifest["forms"]
                                 for field, widget in form.adapter._fields.items()}
                self.assertTrue(all(id(widget) in field_sources
                                    for widget in section._ctrl_widgets))
                section._set_ctrl_enabled(True)
                section._on_position(12.125)
                for form in manifest["forms"]:
                    form.adapter.refresh()
                self.assertTrue(field_sources[id(section._move_btn)].enabled)
                self.assertEqual(section._target_spn.value(), 12.125)
                section._set_ctrl_enabled(False)
                for form in manifest["forms"]:
                    form.adapter.refresh()
                self.assertFalse(field_sources[id(section._move_btn)].enabled)
                self.assertFalse(field_sources[id(section._home_btn)].enabled)

    def imaging_section(self):
        controller = _Control()
        with patch("ui.instrument_panel._list_com_ports", return_value=[]):
            section = self.keep(_ESP32ImagingStageSection(controller))
        configure_motion_section(section)
        return section, controller

    def test_imaging_layout_keeps_stop_and_absolute_interlocks(self):
        section, controller = self.imaging_section()
        self.assertGreater(section.maximumWidth(), 460)
        self.assertTrue(section._advanced.isHidden())
        self.assertEqual(controller.calls, [])
        controller.connected.emit("COM-test")
        state = dict(valid=True, protocol="stage-v2", homed=True,
                     moving=False, pending=False, cooldown=False, steps=0,
                     limit=9601, stepsPerMm=3200, pulsesPerRev=3200)
        controller.state_changed.emit(state)
        section._goto_zero.click()
        section._goto_maximum.click()
        self.assertEqual(controller.calls, [("goto", (0.0,)), ("goto", (9601 / 3200,))])
        controller.calls.clear()
        for changes in (dict(homed=False), dict(limit=0), dict(valid=False),
                        dict(moving=True), dict(pending=True), dict(cooldown=True)):
            controller.state_changed.emit(dict(state, **changes))
            self.assertFalse(section._goto_zero.isEnabled())
            self.assertFalse(section._goto_maximum.isEnabled())
            self.assertFalse(section._move_target.isEnabled())
            self.assertTrue(section._stop.isEnabled())
        section._stop.click()
        self.assertEqual(controller.calls, [("stop", ())])
        section._advanced_btn.setChecked(True)
        self.assertFalse(section._advanced.isHidden())

    def test_imaging_migration_retains_full_safety_labels_and_disclosure(self):
        section, controller = self.imaging_section()
        manifest = install_parameter_forms(section, "instruments")
        fields = {id(widget): field for form in manifest["forms"]
                  for field, widget in form.adapter._fields.items()}
        for widget in (section._stop, section._goto_zero, section._goto_maximum,
                       section._jog_minus, section._jog_plus):
            self.assertIn(id(widget), fields)
            self.assertEqual(fields[id(widget)].text, widget.text())
            self.assertEqual(widget.property("quickPreferredWidth"), 10000)
        self.assertEqual(fields[id(section._advanced_btn)].kind, "disclosure")
        self.assertTrue(section._advanced.isHidden())
        self.assertEqual(controller.calls, [])

    def test_unknown_section_is_untouched(self):
        widget = self.keep(QWidget())
        self.assertIsNone(configure_motion_section(widget))


if __name__ == "__main__":
    unittest.main()
