"""Embedded calibration keeps dynamic progress and original action routes."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel

from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.quick_calibration_layout import configure_quick_calibration
from ui.quick_parameter_forms import install_parameter_forms
from ui.wavelength_calibration_dialog import WavelengthCalibrationDialog


def test_embedded_calibration_retains_rows_sources_advanced_and_dynamic_progress():
    app = quick_application()
    previous = app.property("spectralQuick")
    app.setProperty("spectralQuick", True)
    panel = WavelengthCalibrationDialog()
    panel.setWindowFlags(Qt.WindowType.Widget)
    panel.embedded = True
    sources = (panel.source_confirm, panel.capture_button, panel.stop_button,
               panel.load_model_button, panel.result, panel.advanced_toggle)
    progress_host = panel.progress_host
    try:
        assembly = configure_quick_calibration(panel)
        assert configure_quick_calibration(panel) is assembly
        coverage = install_parameter_forms(panel, "calibration")
        panel.resize(1600, 850)
        panel.show()
        QTest.qWait(100)
        migrated = [source for form in coverage["forms"] for source in form.adapter._fields.values()]
        for source in sources:
            if source is not panel.result:  # Label-only result remains native.
                assert migrated.count(source) == 1
        assert panel.progress_host is progress_host
        assert progress_host.parentWidget().isVisible()
        dynamic_progress = QLabel("Test progress widget")
        progress_host.addWidget(dynamic_progress)
        QTest.qWait(20)
        assert dynamic_progress.isVisibleTo(panel)
        assert assembly["progress"].isAncestorOf(dynamic_progress)
        assert panel.simple_panel.width() == 1100
        source_forms = {id(source): form for form in coverage["forms"]
                        for source in form.adapter._fields.values()}
        assert source_forms[id(panel.source_confirm)] is not source_forms[id(panel.capture_button)]
        assert not panel.steps.isVisible()
        form, toggle = next((form, field) for form in coverage["forms"]
                            for field, source in form.adapter._fields.items()
                            if source is panel.advanced_toggle)
        assert toggle.trigger()
        assert panel.steps.isVisible()
        assert toggle.trigger()
        assert not panel.steps.isVisible()
        panel.automatic_started.emit()
        assert panel.stop_button.isEnabled()
        assert panel.result.text() == "Calibration in progress. Results are saved automatically."
        assert sources == (panel.source_confirm, panel.capture_button, panel.stop_button,
                           panel.load_model_button, panel.result, panel.advanced_toggle)
    finally:
        for form in getattr(panel, "_quick_parameter_forms", {}).get("forms", []):
            form.shutdown()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()
        app.setProperty("spectralQuick", previous)


def test_calibration_dialog_layout_is_unchanged():
    app = quick_application()
    previous = app.property("spectralQuick")
    app.setProperty("spectralQuick", True)
    panel = WavelengthCalibrationDialog()
    try:
        layout = panel.simple_panel.layout()
        assert configure_quick_calibration(panel) == {}
        assert panel.simple_panel.layout() is layout
        assert panel.progress_host.parentWidget() is panel.simple_panel
    finally:
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()
        app.setProperty("spectralQuick", previous)
