"""Power's modal preview releases real Qt views after reading accepted rows."""
import copy
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QWidget

from app.motion_sequence_model import MotionSequenceModel
from ui.power_sweep_panel import PowerSweepPanel
from ui.quick_motion_sequence import QuickMotionSequencePreviewDialog


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class PreviewHost(QWidget):
    """Only the state consumed by the production handler; no controllers."""
    _show_condition_preview = PowerSweepPanel._show_condition_preview

    def __init__(self):
        super().__init__()
        self._custom_condition_sequence = None
        self._positions = np.array([0.0, 1.0])
        self._condition_preview_lbl = QLabel(self)
        self.refresh_count = 0

    def _planned_condition_sequence(self):
        sequence = [{"condition_index": i, "rotation_index": 0, "repeat": i + 1}
                    for i in range(3)]
        rows = [{"sequence": i + 1, "condition_index": i + 1,
                 "repeat": item["repeat"], "_source_index": i}
                for i, item in enumerate(sequence)]
        return [], {}, sequence, rows

    def _update_condition_preview(self):
        self.refresh_count += 1


def flush_deletes(app):
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.mark.parametrize("quick", [False, True], ids=["widgets", "quick"])
def test_reopening_apply_and_cancel_releases_views_but_preserves_source_order(app, quick):
    previous = app.property("spectralQuick")
    app.setProperty("spectralQuick", quick)
    host = PreviewHost()
    baseline_objects = len(host.findChildren(QObject))
    errors = []
    opened = []

    def open_and_finish(apply):
        def finish():
            dialogs = host.findChildren(QDialog)
            try:
                assert len(dialogs) == 1
                dialog = dialogs[0]
                assert isinstance(dialog, QuickMotionSequencePreviewDialog) == quick
                model = dialog.sequence_model
                opened.append(model.selected_indices())
                if apply:
                    # Move source 2 ahead of source 0 and omit source 1.
                    model.set_checked(1, False)
                    assert model.move_entry(2, 0)
                    dialog.apply_selection()
                else:
                    model.set_all_checked(False)
                    assert model.move_entry(0, 1)
                    dialog.reject()
            except BaseException as error:
                errors.append(error)
                for dialog in dialogs:
                    dialog.reject()

        QTimer.singleShot(0, finish)
        host._show_condition_preview()
        flush_deletes(app)
        assert not errors, errors
        assert host.findChildren(QDialog) == []
        assert host.findChildren(QQuickWidget) == []
        assert host.findChildren(MotionSequenceModel) == []
        assert len(host.findChildren(QObject)) == baseline_objects

    try:
        open_and_finish(False)
        assert host._custom_condition_sequence is None
        open_and_finish(True)
        applied = copy.deepcopy(host._custom_condition_sequence)
        assert [item["_source_index"] for item in applied] == [2, 0]
        assert [item["condition_index"] for item in applied] == [2, 0]
        assert [item["repeat"] for item in applied] == [3, 1]
        for _ in range(3):
            open_and_finish(False)
            assert host._custom_condition_sequence == applied
        assert host.refresh_count == 1
        assert opened == [[0, 1, 2], [0, 1, 2], [2, 0], [2, 0], [2, 0]]
    finally:
        app.setProperty("spectralQuick", previous)
        host.deleteLater()
        flush_deletes(app)


def test_exec_error_also_releases_parent_owned_dialog(app, monkeypatch):
    class FailedDialog(QDialog):
        def __init__(self, rows, **kwargs):
            super().__init__(kwargs["parent"])

        def exec(self):
            raise RuntimeError("preview could not execute")

    monkeypatch.setattr("ui.power_sweep_panel.MotionSequencePreviewDialog", FailedDialog)
    host = PreviewHost()
    try:
        with pytest.raises(RuntimeError, match="preview could not execute"):
            host._show_condition_preview()
        flush_deletes(app)
        assert host.findChildren(QDialog) == []
        assert host._custom_condition_sequence is None
    finally:
        host.deleteLater()
        flush_deletes(app)
