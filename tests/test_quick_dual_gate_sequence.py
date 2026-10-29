"""Real Quick sequence review with compiled fixtures, no hardware services."""
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import Q_ARG, QMetaObject, QObject, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QDialog

from tests.quick_helpers import quick_application, drain_qt_deletions
from tests.test_dual_gate_sequence_model import flat_plan, nested_plan
from ui.quick_dual_gate_sequence import QuickDualGateSequencePreview


@pytest.fixture(scope="module")
def app():
    app = quick_application()
    path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
    if not QFontDatabase.families() and path.exists():
        QFontDatabase.addApplicationFont(str(path))
        app.setFont(QFont("Segoe UI", 9))
    return app


def show(widget):
    widget.resize(1060, 700)
    widget.show()
    QTest.qWait(30)
    assert widget.quick_widget.status() == QQuickWidget.Status.Ready, widget.qml_warnings
    assert widget.quick_widget.rootObject().objectName() == "DualGateSequence"
    return widget.quick_widget.rootObject()


def dispose(widget):
    widget.close()
    widget.deleteLater()
    drain_qt_deletions()


def invoke(root, name, *args):
    values = [Q_ARG(str if isinstance(value, str) else int, value) for value in args]
    assert QMetaObject.invokeMethod(root, name, Qt.ConnectionType.DirectConnection, *values)


def test_actual_qml_shows_exact_flat_order_and_when_candidates(app):
    contexts, rows, tasks = flat_plan()
    widget = QuickDualGateSequencePreview()
    try:
        widget.update_plan(contexts, rows, acquisition_schedule=tasks, total_acq=5)
        root = show(widget)
        assert widget.sequence_model.rowCount() == 3
        assert widget.sequence_model.summary.endswith("1 skipped combinations")
        widget.sequence_model.setShowSkipped(True)
        assert widget.sequence_model.rowCount() == 4
        skipped = next(i for i, row in enumerate(widget.sequence_model._rows) if row["sourceIndex"] < 0)
        invoke(root, "selectRow", skipped)
        assert widget.sequence_model.selectedDetail["title"] == "Skipped combination"
        assert "750" in widget.sequence_model.selectedDetail["loop"]
        assert "Center_Wavelength > 800" in widget.sequence_model.selectedDetail["when"]
        invoke(root, "goToStep", "3")
        assert widget.sequence_model.selectedDetail["title"] == "Step 3"
        assert not widget.sequence_model.followCurrent
        assert not widget.qml_warnings
    finally:
        dispose(widget)


def test_hidden_preview_keeps_latest_plan_without_formatting_or_progress_work(app):
    contexts, rows, tasks = [], [], nested_plan(40)
    widget = QuickDualGateSequencePreview()
    try:
        with patch.object(widget.sequence_model, "update_plan", wraps=widget.sequence_model.update_plan) as update:
            widget.update_plan(contexts, rows, acquisition_schedule=tasks, current_seq_i=0, run_outcome="running")
            widget.update_plan(contexts, rows, acquisition_schedule=tasks, current_seq_i=5, run_outcome="running")
            assert update.call_count == 0
            show(widget)
            assert update.call_count == 1
            assert widget.sequence_model.currentStep == 6
            widget.hide()
            widget.update_plan(contexts, rows, acquisition_schedule=tasks, current_seq_i=21, run_outcome="running")
            assert update.call_count == 1
            widget.show()
            QTest.qWait(10)
            assert update.call_count == 2
            assert widget.sequence_model.currentStep == 22
        assert not widget.qml_warnings
    finally:
        dispose(widget)


def test_paging_jump_keyboard_and_view_state_survive_reload(app):
    contexts, rows, tasks = [], [], nested_plan(80)
    original = QuickDualGateSequencePreview()
    restored = QuickDualGateSequencePreview()
    try:
        original.update_plan(contexts, rows, acquisition_schedule=tasks)
        root = show(original)
        invoke(root, "nextPage")
        assert original.sequence_model.page == 2
        invoke(root, "goToStep", "21")
        assert original.sequence_model.page == 3
        steps = root.findChild(QObject, "DualGateFocusCard")
        steps.forceActiveFocus()
        QTest.keyClick(original.quick_widget, Qt.Key.Key_Down)
        assert original.sequence_model.selectedDetail["title"] == "Step 22"
        saved = original.capture_view_state()
        assert saved["page"] == 3 and saved["page_row"] == 5
        assert not saved["follow_current"]
        restored.update_plan(contexts, rows, acquisition_schedule=tasks)
        restored.restore_view_state(saved)
        show(restored)
        assert restored.sequence_model.page == 3
        assert restored.sequence_model.selectedDetail["title"] == "Step 22"
        restored.update_plan(contexts, rows, acquisition_schedule=tasks,
                             current_seq_i=60, completed_points=60, run_outcome="running")
        assert restored.sequence_model.page == 3
        restored.sequence_model.setFollowCurrent(True)
        assert restored.sequence_model.selectedDetail["title"] == "Step 61"
        assert not original.qml_warnings and not restored.qml_warnings
    finally:
        dispose(original)
        dispose(restored)


def test_full_view_is_independent_bounded_and_released_on_close(app):
    contexts, rows, tasks = [], [], nested_plan(1000)
    widget = QuickDualGateSequencePreview()
    try:
        widget.update_plan(contexts, rows, acquisition_schedule=tasks)
        root = show(widget)
        invoke(root, "openFullSequence")
        QTest.qWait(30)
        full = widget._full_sequence_preview
        dialog = widget._full_sequence_dialog
        assert full is not None and dialog.isVisible()
        assert full.sequence_model.rowCount() == 200
        assert widget.sequence_model.rowCount() == 8
        full.sequence_model.goToStep("601")
        assert full.sequence_model.page == 4
        widget.update_plan(contexts, rows, acquisition_schedule=tasks,
                           current_seq_i=300, completed_points=300, run_outcome="running")
        assert full.sequence_model.page == 4
        full_root = full.quick_widget.rootObject()
        assert 0 < full_root.property("liveDelegateCount") < 60
        assert full.minimumHeight() < full.maximumHeight()
        with patch.object(full.sequence_model, "update_plan", wraps=full.sequence_model.update_plan) as update:
            for expanded in (False, True):
                full_root.setProperty("detailsExpanded", expanded)
                dialog.resize(780, 560 if expanded else 720)
                QTest.qWait(30)
                assert full.sequence_model.selectedDetail["title"] == "Step 601"
                assert full.sequence_model.rowCount() == 200
                assert update.call_count == 0
        dialog.close()
        drain_qt_deletions()
        assert widget._full_sequence_dialog is None
        assert widget.findChildren(QDialog) == []
        widget.show_full_sequence()
        QTest.qWait(10)
        assert len(widget.findChildren(QDialog)) == 1
        assert not widget.qml_warnings
    finally:
        dispose(widget)


def test_compact_details_opens_full_view_at_selected_skipped_candidate(app):
    contexts, rows, tasks = flat_plan()
    widget = QuickDualGateSequencePreview()
    try:
        widget.update_plan(contexts, rows, acquisition_schedule=tasks)
        root = show(widget)
        widget.sequence_model.setShowSkipped(True)
        skipped = next(i for i, row in enumerate(widget.sequence_model._rows) if row["sourceIndex"] < 0)
        widget.sequence_model.selectRow(skipped)
        invoke(root, "openFullSequence")
        QTest.qWait(10)
        assert widget._full_sequence_preview.sequence_model.selectedDetail["title"] == "Skipped combination"
        assert widget._full_sequence_preview.sequence_model.showSkipped
    finally:
        dispose(widget)


def test_100000_schedule_is_borrowed_and_only_pages_are_read(app):
    template = nested_plan(1)[0]

    class Schedule:
        reads = 0

        def __len__(self):
            return 100000

        def __getitem__(self, index):
            if not 0 <= index < len(self):
                raise IndexError(index)
            self.reads += 1
            return dict(template, seq_i=index)

        def __iter__(self):
            raise AssertionError("Quick preview iterated the complete schedule")

        def __deepcopy__(self, memo):
            raise AssertionError("Quick preview copied the complete schedule")

    tasks, contexts, rows = Schedule(), [], []
    widget = QuickDualGateSequencePreview(page_size=200, compact=False)
    try:
        widget.update_plan(contexts, rows, acquisition_schedule=tasks)
        root = show(widget)
        assert tasks.reads < 210
        tasks.reads = 0
        invoke(root, "goToStep", "100000")
        QTest.qWait(10)
        assert widget.sequence_model.selectedDetail["title"] == "Step 100,000"
        assert tasks.reads < 210
        assert 0 < root.property("liveDelegateCount") < 60
        assert widget.sequence_model.rowCount() == 200
        assert widget._last_plan["acquisition_schedule"] is tasks
        assert not widget.qml_warnings
    finally:
        dispose(widget)


def test_clear_and_shutdown_stop_future_plan_updates(app):
    contexts, rows, tasks = flat_plan()
    widget = QuickDualGateSequencePreview()
    try:
        widget.update_plan(contexts, rows, acquisition_schedule=tasks)
        show(widget)
        widget.clear_plan()
        assert widget.sequence_model.rowCount() == 0
        widget.shutdown()
        assert widget.quick_widget.rootObject() is None
        widget.update_plan(contexts, rows, acquisition_schedule=tasks)
        assert widget._last_plan is None and widget.sequence_model.rowCount() == 0
    finally:
        dispose(widget)


def test_progress_bursts_publish_latest_position_once_and_terminal_is_immediate(app):
    contexts, rows, tasks = [], [], nested_plan(1000)
    widget = QuickDualGateSequencePreview()
    try:
        widget.update_plan(contexts, rows, acquisition_schedule=tasks,
                           current_seq_i=0, run_outcome="running")
        show(widget)
        with patch.object(widget.sequence_model, "update_plan", wraps=widget.sequence_model.update_plan) as update:
            for current in range(1, 101):
                widget.update_plan(contexts, rows, acquisition_schedule=tasks,
                                   current_seq_i=current, run_outcome="running")
            assert update.call_count == 0
            assert widget._update_timer.isActive()
            QTest.qWait(100)
            assert update.call_count == 1
            assert widget.sequence_model.focusStep["stepTitle"] == "Spectrum 101 / 1,000"
            widget.update_plan(contexts, rows, acquisition_schedule=tasks,
                               current_seq_i=101, run_outcome="running")
            widget.update_plan(contexts, rows, acquisition_schedule=tasks,
                               current_seq_i=101, run_outcome="failed")
            assert update.call_count == 2
            assert widget.sequence_model.focusStep["caption"] == "Failed step"
            assert not widget._update_timer.isActive()
            QTest.qWait(70)
            assert update.call_count == 2
        assert not widget.qml_warnings
    finally:
        dispose(widget)


def test_running_order_path_keeps_delegates_while_live_values_change(app):
    contexts, rows, tasks = flat_plan()
    widget = QuickDualGateSequencePreview()
    kwargs = dict(acquisition_schedule=tasks, current_frame_total=3, run_outcome="running")
    try:
        widget.update_plan(contexts, rows, current_seq_i=0, current_frame_i=1, **kwargs)
        root = show(widget)
        path = root.findChild(QObject, "DualGateExecutionPath")

        def subtree(item):
            return [item, *(descendant for child in item.childItems() for descendant in subtree(child))]

        initial = subtree(path)
        destroyed = []
        for item in initial:
            item.destroyed.connect(lambda: destroyed.append(True))
        widget.update_plan(contexts, rows, current_seq_i=0, current_frame_i=2, **kwargs)
        QTest.qWait(90)
        assert subtree(path) == initial
        assert not destroyed
        assert "Frame 2 / 3" in [item.property("text") for item in subtree(path)]

        # The next task has the same structure but different requested values.
        widget.update_plan(contexts, rows, current_seq_i=1, current_frame_i=1, **kwargs)
        QTest.qWait(90)
        assert subtree(path) == initial
        assert not destroyed
        assert "810 nm" in [item.property("text") for item in subtree(path)]
        widget.update_plan(contexts, rows, current_seq_i=1, current_frame_i=1,
                           **{**kwargs, "run_outcome": "failed"})
        assert widget.sequence_model.focusStep["statusKind"] == "failed"
        assert subtree(path) == initial
        assert not widget.qml_warnings
    finally:
        dispose(widget)


def test_hiding_cancels_pending_progress_and_show_publishes_latest(app):
    contexts, rows, tasks = [], [], nested_plan(1000)
    widget = QuickDualGateSequencePreview()
    try:
        widget.update_plan(contexts, rows, acquisition_schedule=tasks,
                           current_seq_i=0, run_outcome="running")
        show(widget)
        with patch.object(widget.sequence_model, "update_plan", wraps=widget.sequence_model.update_plan) as update:
            widget.update_plan(contexts, rows, acquisition_schedule=tasks,
                               current_seq_i=1, run_outcome="running")
            widget.hide()
            widget.update_plan(contexts, rows, acquisition_schedule=tasks,
                               current_seq_i=999, run_outcome="running")
            QTest.qWait(70)
            assert update.call_count == 0
            assert not widget._update_timer.isActive()
            widget.show()
            assert update.call_count == 1
            assert widget.sequence_model.focusStep["stepTitle"] == "Spectrum 1,000 / 1,000"
    finally:
        dispose(widget)


def test_compact_height_tracks_empty_single_and_bounded_long_pages_without_rebuilds(app):
    widget = QuickDualGateSequencePreview()
    try:
        root = show(widget)
        focus = root.findChild(QObject, "DualGateFocusCard")
        following = root.findChild(QObject, "DualGateFollowingCard")
        navigation = root.findChild(QObject, "DualGateCompactNavigation")
        changes = QSignalSpy(widget.preferredHeightChanged)

        def settle_height():
            # QML polish and the coalesced Widgets height update are separate
            # event-loop turns. Wait for their agreement instead of assuming a
            # loaded CI machine completes both within a fixed 35 ms.
            previous, stable = None, 0
            for _ in range(50):
                QTest.qWait(20)
                state = (widget.height(), root.property("preferredContentHeight"), changes.count())
                stable = stable + 1 if state == previous else 0
                previous = state
                if stable >= 3 and abs(state[0] - state[1]) <= 1 and not widget._height_timer.isActive():
                    return
            pytest.fail(f"Sequence height did not settle: {previous}")

        for width in (420, 361, 1060, 350, 420):
            widget.resize(width, widget.height())
            for count in (0, 1, 2, 1000):
                widget.update_plan([], [], acquisition_schedule=nested_plan(count))
                settle_height()
                assert focus.isVisible() == (count > 0)
                assert following.isVisible() == (count > 1)
                assert not root.findChild(QObject, "DualGateSteps").isVisible()
                if count:
                    last = following if count > 1 else focus
                    bottom = last.mapToScene(last.boundingRect().bottomRight()).y()
                    assert 0 <= navigation.mapToScene(navigation.boundingRect().topLeft()).y() - bottom <= 7
                assert widget.minimumHeight() == widget.maximumHeight() == widget.height()
                assert widget.height() == pytest.approx(root.property("preferredContentHeight"), abs=1)
                with patch.object(widget.sequence_model, "update_plan", wraps=widget.sequence_model.update_plan) as update:
                    widget.sequence_model.selectRow(0)
                    settle_height()
                    settled = changes.count()
                    QTest.qWait(60)
                    assert changes.count() == settled
                    assert update.call_count == 0
        contexts, rows, tasks = flat_plan()
        widget.update_plan(contexts, rows, acquisition_schedule=tasks)
        for expanded in (True, False):
            root.setProperty("filtersExpanded", expanded)
            settle_height()
            settled = changes.count()
            QTest.qWait(60)
            assert changes.count() == settled
        assert not widget.qml_warnings
    finally:
        dispose(widget)


@pytest.mark.skipif(os.name != "nt", reason="Windows native rendering/scaling check")
@pytest.mark.parametrize("scale", ["1", "1.5", "2"])
def test_native_compact_and_full_layout_in_light_dark_at_each_scale(tmp_path, scale):
    script = r'''
import sys
from pathlib import Path
from PySide6.QtCore import QObject,Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from tests.test_dual_gate_sequence_model import nested_plan
from ui.quick_dual_gate_sequence import QuickDualGateSequencePreview
app=QApplication([])
assert app.platformName()=='windows' and 'Segoe UI' in QFontDatabase.families()
widget=QuickDualGateSequencePreview()
widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen,True)
widget.update_plan([],[],acquisition_schedule=nested_plan(240),current_seq_i=4,completed_points=4,run_outcome='running',done=1,total_acq=80)
for width,height,tag in ((361,360,'small'),(420,820,'compact'),(1120,820,'wide')):
 widget.resize(width,height);widget.show();QTest.qWait(60)
 for theme in ('light','dark'):
  widget.set_theme(theme);QTest.qWait(50)
  image=widget.quick_widget.grabFramebuffer()
  assert not image.isNull()
  assert image.save(str(Path(sys.argv[1])/(tag+'-'+theme+'.png')))
 root=widget.quick_widget.rootObject()
 for name in ('FullDualGateSequence','PreviousDualGateStep','NextDualGateStep','FollowCompactDualGateStep','DualGateFocusCard','DualGateFollowingCard','DualGateRulesToggle'):
  item=root.findChild(QObject,name)
  edge=item.mapToScene(item.boundingRect().bottomRight())
  assert edge.x()<=root.width()+1 and edge.y()<=root.height()+1,(name,edge,root.size())
assert not widget.qml_warnings,widget.qml_warnings
widget.close();widget.deleteLater();QTest.qWait(10)
'''
    environment = dict(os.environ, QT_QPA_PLATFORM="windows", QT_QUICK_BACKEND="software",
                       QT_SCALE_FACTOR=scale, SPECTRALSWEEP_HISTORY_PATH=str(tmp_path / "history.sqlite"))
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                            cwd=Path(__file__).resolve().parents[1], env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "compact-light.png").is_file()
    assert (tmp_path / "wide-dark.png").is_file()
