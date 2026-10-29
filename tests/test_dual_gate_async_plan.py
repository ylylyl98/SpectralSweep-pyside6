"""Background Apply stays responsive, validates safety, and rejects old drafts."""
import os
import threading
import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
import pandas as pd
from PySide6.QtCore import QCoreApplication, QEvent, QThread, QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QCloseEvent

from ui import presets_panel as panel
from ui.dual_gate_reload_state import dual_gate_reload_block_reason


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def spin(app, predicate, timeout=5):
    until = time.monotonic() + timeout
    while not predicate() and time.monotonic() < until:
        app.processEvents()
        time.sleep(0.002)
    assert predicate(), "background plan did not reach the expected state"


@pytest.fixture
def widget(app, monkeypatch):
    monkeypatch.setattr(panel, "_ASYNC_PLAN_THRESHOLD", 1)
    result = panel.PresetsPanel()
    yield result
    result.cancel_plan_builds()
    spin(app, lambda: not result._plan_build_threads)
    result._preview_timer.stop()
    result.close()
    result.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def edit_frames(widget, frames=3):
    widget._batch_table.item(0, panel.BATCH_SCHEMA.index("frames")).setText(str(frames))


def test_apply_does_not_block_heartbeat_and_installs_atomically(app, widget):
    edit_frames(widget)
    old = widget._acquisition_schedule
    entered, release = threading.Event(), threading.Event()
    compiler = panel._compile_apply_snapshot
    worker_threads = []

    def blocked(snapshot, cancelled):
        worker_threads.append(QThread.currentThread())
        entered.set()
        assert release.wait(5)
        return compiler(snapshot, cancelled)

    beats = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: beats.append(1))
    timer.start()
    try:
        with patch.object(panel, "_compile_apply_snapshot", side_effect=blocked):
            started = time.monotonic()
            widget._on_apply()
            assert time.monotonic() - started < 0.5
            spin(app, entered.is_set)
            spin(app, lambda: len(beats) >= 3)
            assert widget._plan_apply_pending
            assert not widget._run_btn.isEnabled()
            assert widget._acquisition_schedule is old
            assert "sequence build" in dual_gate_reload_block_reason(widget)
            release.set()
            spin(app, lambda: not widget._plan_build_threads)
        assert worker_threads[0] != app.thread()
        assert widget._acquisition_schedule is not old
        assert widget._total_points == 3
        assert not widget._tables_dirty
        assert widget._readiness_jump_cache[0] is widget._acquisition_schedule
        assert widget._plan_readiness_cache[0] is widget._acquisition_schedule
        index = widget._plan_sequence_index_cache[1]
        assert index.schedule is widget._acquisition_schedule
        assert widget._tree._last_plan["prepared_sequence_index"] is index
        widget._on_progress(0, widget._total_acq)
        assert widget._tree._last_plan["prepared_sequence_index"] is index
    finally:
        release.set()
        timer.stop()


@pytest.mark.parametrize("change", ["sample", "draft", "discard", "silent_draft"])
def test_changed_inputs_reject_stale_results(app, widget, change):
    edit_frames(widget)
    old = widget._acquisition_schedule
    entered, release = threading.Event(), threading.Event()
    compiler = panel._compile_apply_snapshot

    def blocked(snapshot, cancelled):
        # Compile before editing, to prove delivery also rejects already-ready
        # results when the input changed without an ordinary edit signal.
        result = compiler(snapshot, cancelled)
        entered.set()
        assert release.wait(5)
        return result

    try:
        with patch.object(panel, "_compile_apply_snapshot", side_effect=blocked):
            widget._on_apply()
            spin(app, entered.is_set)
            if change == "sample":
                widget._sample_edit.setText("changed during compilation")
            elif change == "draft":
                edit_frames(widget, 4)
            elif change == "discard":
                widget._discard_btn.click()
            else:
                widget._batch_table.blockSignals(True)
                edit_frames(widget, 4)
                widget._batch_table.blockSignals(False)
            assert not widget._run_btn.isEnabled()
            release.set()
            spin(app, lambda: not widget._plan_build_threads)
        assert widget._acquisition_schedule is old
        assert not widget._plan_apply_pending
    finally:
        release.set()


def test_explicit_cancel_releases_thread_and_emits_idle(app, widget):
    edit_frames(widget)
    old = widget._acquisition_schedule
    entered = threading.Event()
    idle = []
    widget.plan_build_idle.connect(lambda: idle.append(True))

    def cancellable(snapshot, cancelled):
        entered.set()
        assert cancelled.wait(5)
        return None

    with patch.object(panel, "_compile_apply_snapshot", side_effect=cancellable):
        widget._on_apply()
        spin(app, entered.is_set)
        widget.cancel_plan_builds()
        spin(app, lambda: not widget._plan_build_threads)
    assert idle == [True]
    assert widget._acquisition_schedule is old
    assert not widget.plan_build_running()


def test_background_safety_scan_is_installed_without_gui_rescan(app, widget):
    edit_frames(widget, 3)
    widget._batch_table.item(0, panel.BATCH_SCHEMA.index("Vbg_stop")).setText("10")
    seen_threads = []
    scan = panel._validate_safe_jumps

    def tracked(*args, **kwargs):
        seen_threads.append(QThread.currentThread())
        return scan(*args, **kwargs)

    with patch.object(panel, "_validate_safe_jumps", side_effect=tracked), \
         patch.object(panel.QMessageBox, "warning") as warning:
        widget._on_apply()
        spin(app, lambda: not widget._plan_build_threads)
    assert len(seen_threads) == 1
    assert seen_threads[0] != app.thread()
    assert widget._readiness_jump_cache[2]
    assert "Unsafe" in widget._readiness_jump_cache[2][0]
    assert warning.called
    assert not widget._run_btn.isEnabled()


def test_nested_compiler_preserves_scope_transitions_and_stage_snapshot(app):
    widget = panel.PresetsPanel()
    try:
        loop, batch = widget._draft_frames()
        batch.loc[0, "frames"] = 3
        batch.loc[0, "Vbg_stop"] = 10
        snapshot = {"loop": loop, "batch": batch, "mode": "Synchronize",
                    "grouping": "loop_first", "nested": True,
                    "execution_order": None, "legacy_order": [],
                    "stage_profile": panel._active_stage_profile(), "safe_jump": 0.5}
        prepared = panel._compile_apply_snapshot(snapshot, threading.Event())
        assert prepared["points"] == 3
        assert prepared["settle_counts"] == (1, 2)
        assert prepared["jumps"]
        assert all(task["nested"] for task in prepared["resolved"][3])
        assert not hasattr(panel._PLAN_BUILD_CONTEXT, "stage_profile")
    finally:
        widget.close()


def test_main_window_close_cancels_and_retries_after_worker_exit(app, widget):
    from ui.main_window import MainWindow
    edit_frames(widget)
    entered = threading.Event()
    worker_threads = []

    def cancellable(snapshot, cancelled):
        worker_threads.append(QThread.currentThread())
        entered.set()
        assert cancelled.wait(5)
        return None

    class Shell:
        closeEvent = MainWindow.closeEvent
        _resume_close_after_plan_build = MainWindow._resume_close_after_plan_build

        def __init__(self):
            self._presets = widget
            self._status = SimpleNamespace(showMessage=Mock())
            self.close = Mock()

    shell = Shell()
    with patch.object(panel, "_compile_apply_snapshot", side_effect=cancellable):
        widget._on_apply()
        spin(app, entered.is_set)
        with pytest.raises(RuntimeError, match="sequence build"):
            widget.dispose_reloaded_page()
        event = QCloseEvent()
        shell.closeEvent(event)
        assert not event.isAccepted()
        assert shell._plan_close_retry_pending
        assert not shell.close.called
        spin(app, lambda: not widget._plan_build_threads)
    shell.close.assert_called_once()
    assert not shell._plan_close_retry_pending


def test_standalone_panel_close_waits_for_its_worker(app, widget):
    edit_frames(widget)
    entered = threading.Event()

    def cancellable(snapshot, cancelled):
        entered.set()
        assert cancelled.wait(5)
        return None

    with patch.object(panel, "_compile_apply_snapshot", side_effect=cancellable):
        widget._on_apply()
        spin(app, entered.is_set)
        event = QCloseEvent()
        widget.closeEvent(event)
        assert not event.isAccepted()
        spin(app, lambda: not widget._plan_build_threads)
    assert not widget._plan_close_retry_pending


def test_filename_context_cache_preserves_original_sequence_and_disabled_rows(app):
    loop = pd.DataFrame([dict(Enable=True, Parameter="Exposure Time (ms)", Values="1,2,3", Group=1)])
    batch = pd.concat([panel._DEFAULT_BATCH] * 2, ignore_index=True)
    batch.loc[:, "Run"] = [False, True]
    batch.loc[:, "When"] = ["Exposure_Time == 3", "Exposure_Time == 2"]
    snapshot = {"loop": loop, "batch": batch, "mode": "Synchronize", "grouping": "batch_first",
                "nested": False, "execution_order": None, "legacy_order": [],
                "stage_profile": panel._active_stage_profile(), "safe_jump": 0.5}
    prepared = panel._compile_apply_snapshot(snapshot, threading.Event())
    seq = prepared["resolved"][0]
    assert len(prepared["resolved"][3]) == 1
    with patch.object(panel, "_when_ok", side_effect=AssertionError("GUI rescanned the plan")):
        for row_i, expected in ((0, 3), (1, 2)):
            ctx = panel._first_applicable_seq_ctx(
                seq, batch.iloc[row_i].to_dict(), first_contexts=prepared["filename_contexts"])
            assert ctx["Exposure Time (ms)"] == expected


def test_worker_uses_captured_stage_profile_without_mutating_live_configuration(app):
    from app.devices.stage_profiles import get_linear_stage_profile
    from utils.config import cfg
    loop = pd.DataFrame([dict(Enable=True, Parameter="Stage Position", Values="100", Group=1)])
    snapshot = {"loop": loop, "batch": panel._DEFAULT_BATCH.copy(), "mode": "Synchronize",
                "grouping": "loop_first", "nested": False, "execution_order": None, "legacy_order": [],
                "stage_profile": get_linear_stage_profile("elliptec"), "safe_jump": 0.5}
    with patch.object(cfg.stage, "backend", "esp300"):
        prepared = panel._compile_apply_snapshot(snapshot, threading.Event())
        assert prepared["resolved"][0] == [{"Stage Position": 100.0}]
        assert cfg.stage.backend == "esp300"
    assert not hasattr(panel._PLAN_BUILD_CONTEXT, "stage_profile")


def test_nested_shared_metadata_is_plan_local_and_read_only_during_execution(tmp_path):
    from tests.test_dual_gate_interleaving_regression import DualGateInterleavingRegressionTests
    runner = DualGateInterleavingRegressionTests()
    plan = runner.plan()
    other = runner.plan()
    schedule = plan[2]
    for field in ("execution_order", "group_order_by_param"):
        assert schedule[0][field] is schedule[-1][field]
        assert schedule[0][field] is not other[2][0][field]
    saved = deepcopy([(task["execution_order"], task["group_order_by_param"]) for task in schedule])
    with patch.object(runner, "plan", return_value=plan):
        _, finished, *_ = runner.run_worker(tmp_path)
    assert finished[-1][0]
    assert [(task["execution_order"], task["group_order_by_param"]) for task in schedule] == saved
