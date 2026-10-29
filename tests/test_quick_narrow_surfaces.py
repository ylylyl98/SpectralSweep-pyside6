"""Real history controls and reference rows fit without horizontal scrolling."""
import json

import numpy as np
import pytest
from PySide6.QtCore import QObject, QPointF, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest

from app.experiment_metadata import ExperimentHistory
from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.quick_history import QuickHistoryWidget
from ui.quick_parameter_forms import install_parameter_forms
from ui.quick_reference_list import install_reference_list
from ui.spectrum_panel import SpectrumPanel


def application():
    app = quick_application()
    # Match actual Windows glyph metrics when the batch platform is offscreen.
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeuib.ttf")
    return app


def visual(item, name):
    if item.objectName() == name:
        return item
    for child in item.childItems():
        match = visual(child, name)
        if match is not None:
            return match


@pytest.mark.parametrize("width,height", [(360, 450), (360, 720), (500, 720), (760, 720), (1060, 720)])
def test_history_filters_actions_and_long_metadata_stay_inside_viewport(width, height, tmp_path):
    application()
    history = ExperimentHistory(tmp_path / "history.sqlite")
    path = tmp_path / "run.json"
    record = {"experiment_id": "narrow-history", "device_id": "long sample " * 8,
              "experiment_type": "dual_gate_sweep", "status": "completed",
              "metadata_path": str(path), "started_utc": "2026-10-26T10:00:00Z",
              "description": "unbroken-metadata-path-" * 100}
    path.write_text(json.dumps(record), encoding="utf-8")
    history.upsert(record)
    widget = QuickHistoryWidget(history, tmp_path, record["device_id"])
    try:
        widget.resize(width, height)
        widget.show()
        for _ in range(100):
            if not widget.view_model.loading:
                break
            QTest.qWait(10)
        widget.view_model.selectRow(0)
        QTest.qWait(50)
        root = widget.rootObject()
        for name in ("RefreshHistory", "HistoryWorkflow", "HistorySearch", "OpenHistoryMetadata",
                     "LoadHistorySettings", "PreviousHistoryEvents", "NextHistoryEvents",
                     "PreviousHistoryRuns", "NextHistoryRuns", "HistoryMetadataPreview"):
            item = root.findChild(QObject, name)
            assert item is not None
            left = item.mapToScene(QPointF(0, 0)).x()
            assert item.width() > 20, (name, item.width())
            assert left >= 0 and left + item.width() <= root.width() + 1, (name, left, item.width(), root.width())
        scroll = root.findChild(QObject, "HistoryMetadataScroll")
        assert scroll.property("contentWidth") <= scroll.property("availableWidth") + 1
        assert not root.findChild(QObject, "HistoryMetadataHorizontalBar").property("visible")
        viewport = root.findChild(QObject, "HistoryViewport")
        assert viewport.property("contentWidth") <= viewport.property("availableWidth") + 1
        if height < 500:
            # A short dialog scrolls vertically; its final action remains
            # reachable, rather than squeezing metadata into a zero-height box.
            flickable = viewport.property("contentItem")
            flickable.setProperty("contentY", viewport.property("contentHeight") - viewport.property("availableHeight"))
            QTest.qWait(20)
            button = root.findChild(QObject, "LoadHistorySettings")
            point = button.mapToScene(QPointF(button.width() / 2, button.height() / 2))
            assert 0 <= point.y() < root.height()
        assert not widget.qml_warnings
    finally:
        widget.shutdown()
        widget.close()
        widget.deleteLater()
        drain_qt_deletions()


def test_real_spectrum_reference_pane_at_70px_keeps_name_color_and_wrapped_status_reachable(monkeypatch, tmp_path):
    app = application()
    previous = app.property("spectralQuick")
    app.setProperty("spectralQuick", True)
    from utils.config import cfg
    monkeypatch.setattr(cfg, "save", lambda *a, **k: None)
    monkeypatch.setattr(cfg.filename, "base_out", str(tmp_path))
    panel = SpectrumPanel()
    reference = panel.add_reference(np.array([650., 700.]), np.array([1., 2.]), auto_save=False)
    editor = install_reference_list(panel)
    forms = install_parameter_forms(panel, "spectrum")
    try:
        panel._comparison_mode.setCurrentText("Compare")
        panel._references_toggle.setChecked(True)
        panel.resize(900, 850)
        panel.show()
        panel._content_splitter.setSizes([830, 70])
        reference["_row_source"].setText("Long reference setup and detector identity " * 10)
        reference["_row_status"].setText("Reference status with detailed processing information " * 5)
        editor.model.refresh()
        QTest.qWait(60)
        assert editor.width() <= 80
        root = editor.rootObject()
        name = visual(root, "referenceName_" + reference["id"])
        status = visual(root, "referenceStatus_" + reference["id"])
        assert name is not None and status is not None
        for item in (name, status):
            left = item.mapToScene(QPointF(0, 0)).x()
            assert left >= 0 and left + item.width() <= root.width() + 1
        assert name.width() >= 40
        assert status.height() > 50  # Full source/status text wraps into rows.
        # TextField.boundingRect can include overflowing text decoration at
        # narrow widths. Click the actual control box, not that expanded box.
        point = name.mapToScene(QPointF(name.width() / 2, name.height() / 2)).toPoint()
        assert 0 <= point.x() < editor.width() and 0 <= point.y() < editor.height()
        QTest.mouseClick(editor, Qt.MouseButton.LeftButton,
                         pos=point)
        assert name.property("activeFocus")
        QTest.keyClick(editor, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClicks(editor, "narrow-reference")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        assert reference["name"] == "narrow-reference"
        assert reference["_row_name"].text() == "narrow-reference"
    finally:
        for form in forms["forms"]:
            form.shutdown()
        editor.shutdown()
        panel.close()
        panel.deleteLater()
        drain_qt_deletions()
        app.setProperty("spectralQuick", previous)
