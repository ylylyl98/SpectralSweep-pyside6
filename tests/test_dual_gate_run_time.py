"""Real Dual Gate progress callbacks with a fake clock and no device I/O."""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import pandas as pd

from app.run_time_estimate import RunTimeEstimate
from tests.quick_helpers import quick_application, drain_qt_deletions
from tests.test_run_time_estimate import FakeClock
from ui.presets_panel import PresetsPanel
from utils.config import cfg


@pytest.fixture
def timed_panel(monkeypatch):
    quick_application()
    monkeypatch.setattr(cfg, "save", lambda: pytest.fail("No production config writes"))
    panel = PresetsPanel(bind_controller=False, initialize_plan=False)
    clock = FakeClock()
    panel._run_time_estimate = RunTimeEstimate(clock)
    panel._run_time_estimate.start(0, 12)
    panel._run_time_timer.start()
    panel._run_outcome = "running"
    panel._total_points = 12
    panel._refresh_run_time()
    yield panel, clock
    panel.dispose_reloaded_page()
    panel.close()
    panel.deleteLater()
    drain_qt_deletions()


def test_real_progress_updates_elapsed_and_adapts_remaining_time(timed_panel):
    panel, clock = timed_panel
    assert panel._run_time_lbl.text() == "Elapsed 00:00 · Remaining Estimating…"
    clock.advance(10)
    panel._on_frame_progress(2, 12)
    assert panel._progress.value() == 2
    assert panel._run_time_lbl.text() == "Elapsed 00:10 · Remaining ≈ 00:50"
    # A slower point changes the estimate instead of preserving a fixed total.
    clock.advance(10)
    panel._on_frame_progress(3, 12)
    assert panel._run_time_lbl.text() == "Elapsed 00:20 · Remaining ≈ 00:59"
    clock.advance(70)
    panel._refresh_run_time()
    assert panel._run_time_lbl.text() == "Elapsed 01:30 · Remaining ≈ 02:02"
    assert panel._progress.value() == 3


def test_complete_points_keep_final_saving_separate_from_run_completion(timed_panel):
    panel, clock = timed_panel
    clock.advance(30)
    panel._on_frame_progress(12, 12)
    assert "Finalizing…" in panel._run_time_lbl.text()
    assert panel._run_outcome == "running"
    panel._on_finalizing()
    clock.advance(5)
    panel._refresh_run_time()
    assert panel._run_time_lbl.text() == "Elapsed 00:35 · Remaining Saving…"
    panel._on_finished(True, "Completed")
    assert panel._run_outcome == "completed"
    assert not panel._run_time_timer.isActive()
    assert panel._run_time_lbl.text() == "Elapsed 00:35 · Remaining —"
    clock.advance(100)
    panel._refresh_run_time()
    assert panel._run_time_lbl.text() == "Elapsed 00:35 · Remaining —"


@pytest.mark.parametrize("stopped", [True, False])
def test_stopping_or_failure_preserves_partial_progress_and_clears_eta(timed_panel, stopped):
    panel, clock = timed_panel
    clock.advance(8)
    panel._on_frame_progress(2, 12)
    if stopped:
        panel._run_worker = SimpleNamespace(preparing_run=True, metadata_failed=False)
        panel._on_stop()
        assert "Stopping…" in panel._run_time_lbl.text()
    clock.advance(2)
    panel._on_finished(False, "Interrupted")
    assert panel._run_outcome == ("stopped" if stopped else "failed")
    assert panel._progress.value() == 2
    assert panel._run_time_lbl.text() == "Elapsed 00:10 · Remaining —"
    assert not panel._run_time_timer.isActive()


def test_applying_new_plan_clears_previous_run_time(timed_panel):
    panel, clock = timed_panel
    clock.advance(25)
    panel._on_frame_progress(2, 12)
    panel._on_finished(False, "Interrupted")
    panel._update_plan(resolved_plan=([], panel._df_batch.iloc[:0], 0, []))
    assert panel._run_outcome == "idle"
    assert panel._status_lbl.text() == "Idle"
    assert panel._run_time_lbl.text() == "Elapsed 00:00 · Remaining —"
    assert not panel._run_time_timer.isActive()


def test_progress_parameter_order_reads_columns_without_filtering_frames(timed_panel):
    panel, _ = timed_panel

    class ColumnsOnlyFrame(pd.DataFrame):
        def __getitem__(self, key):
            assert isinstance(key, str), "Progress must not allocate a filtered DataFrame"
            return super().__getitem__(key)

    panel._loop_src = ColumnsOnlyFrame({
        "Enable": [True, False, True, True, True],
        "Parameter": ["Stage Position", "Disabled", "Exposure Time (ms)", "Stage Position", "  "],
    })
    assert panel._tree_param_order() == ["Stage Position", "Exposure Time (ms)"]
    for index in range(10):
        panel._on_frame_progress(index, 12)
    assert panel._progress.value() == 9
    # Applying/restoring different loop order must not leave a stale cached order.
    panel._loop_src = ColumnsOnlyFrame({"Enable": [True], "Parameter": ["Rotation1 Angle (deg)"]})
    assert panel._tree_param_order() == ["Rotation1 Angle (deg)"]
    panel._loop_src.loc[0, "Enable"] = False
    assert panel._tree_param_order()[0] == "Center Wavelength (nm)"
