"""Disclosure controls retain their meaning in native and Quick forms."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QToolButton
from tests.quick_helpers import quick_application
from ui.quick_parameter_forms import _kind


def check_toggle(toggle, content):
    assert _kind(toggle) == "disclosure"
    for opened in (True, False):
        toggle.setChecked(opened)
        assert content.isHidden() is not opened
        assert toggle.arrowType() == (Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow)


def test_megasweep_disclosure_and_timing():
    quick_application()
    from ui.megasweep_panel import _CollapsibleSection, _TimingWidget
    content = QWidget()
    section = _CollapsibleSection("Test", content, expanded=False)
    check_toggle(section._header, content)
    timing = _TimingWidget()
    check_toggle(timing._toggle, timing._advanced)
    section.close()
    timing.close()


def test_wavelength_advanced_sections_have_arrows():
    quick_application()
    from ui.wavelength_calibration_dialog import WavelengthCalibrationDialog
    dialog = WavelengthCalibrationDialog()
    check_toggle(dialog.advanced_toggle, dialog.steps)
    toggle = next(button for button in dialog.findChildren(QToolButton)
                  if button.text() == "Advanced fit settings and history")
    assert _kind(toggle) == "disclosure"
    for opened in (True, False):
        toggle.setChecked(opened)
        assert toggle.arrowType() == (Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow)
    assert _kind(dialog.source_confirm) == "check"
    dialog.close()


def test_dual_gate_disclosures_do_not_convert_menu_actions(monkeypatch):
    quick_application()
    from ui.presets_panel import PresetsPanel
    from utils.config import cfg
    monkeypatch.setattr(cfg, "save", lambda *args: None)
    panel = PresetsPanel()
    assert all(_kind(button) == "disclosure" for button in
               (panel._filename_details_toggle, panel._sequence_toggle, panel._log_toggle))
    check_toggle(panel._filename_details_toggle, panel._filename_details)
    assert _kind(panel._batch_actions_btn) != "disclosure"
    assert panel._batch_actions_btn.menu() is not None
    panel.dispose_reloaded_page()
    panel.close()


def test_spectrum_window_visibility_updates_disclosure(monkeypatch):
    quick_application()
    from PySide6.QtTest import QSignalSpy
    from ui.presets_panel import PresetsPanel
    from utils.config import cfg
    monkeypatch.setattr(cfg, "save", lambda *args: None)
    panel = PresetsPanel()
    button = panel._spectrum_btn
    spy = QSignalSpy(button.toggled)
    panel._on_spectrum_viewer_visibility_changed(True)
    assert _kind(button) == "disclosure" and button.isChecked()
    assert button.arrowType() == Qt.ArrowType.DownArrow
    panel._on_spectrum_viewer_visibility_changed(False)
    assert not button.isChecked() and button.arrowType() == Qt.ArrowType.RightArrow
    assert spy.count() == 2
    panel.dispose_reloaded_page()
    panel.close()


def test_quick_spectrum_disclosure_tracks_real_window_close(monkeypatch):
    from PySide6.QtCore import QPointF
    from PySide6.QtTest import QTest
    from tests.test_quick_parameter_forms import find_item
    from tests.quick_helpers import drain_qt_deletions
    from ui.presets_panel import PresetsPanel
    from ui.quick_parameter_forms import QuickParameterForm, WidgetsParameterAdapter
    from utils.config import cfg

    quick_application()
    monkeypatch.setattr(cfg, "save", lambda *args: None)
    panel = PresetsPanel()
    button = panel._spectrum_btn
    adapter = WidgetsParameterAdapter([[('spectrum', button, 'disclosure')]], visibility_root=panel)
    form = QuickParameterForm(adapter)
    form.resize(260, 80)
    form.show()
    QTest.qWait(100)
    try:
        item = find_item(form.rootObject(), 'parameter_spectrum')
        assert item is not None
        def click():
            point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
            QTest.mouseClick(form, Qt.MouseButton.LeftButton, pos=point)
            QTest.qWait(80)
        def check(opened):
            assert button.isChecked() is opened
            assert item.property('expanded') is opened
            assert item.property('text') == ('Hide Spectrum' if opened else 'Show Spectrum')
            arrow = find_item(item, 'disclosureChevron_spectrum')
            assert arrow.property('rotation') == (90 if opened else 0)
        click()
        check(True)
        click()
        check(False)
        click()
        check(True)
        panel._spectrum_viewer.close()
        QTest.qWait(80)
        check(False)
    finally:
        form.shutdown()
        form.close()
        form.deleteLater()
        panel.dispose_reloaded_page()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()
