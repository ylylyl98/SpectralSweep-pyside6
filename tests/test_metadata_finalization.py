"""Durable sweep finalization must not stall the GUI or duplicate the plan."""
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
import pytest
from PySide6.QtCore import QThread, QTimer, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QStatusBar

from app import experiment_metadata as metadata_module
from app.experiment_metadata import ExperimentMetadataService
from app.experiment_lifecycle import subscribe, unsubscribe
from tests import test_smu_resilience as fixtures
from tests.test_smu_resilience import _FakeLFController, _HealthyRunDevice
from ui.presets_panel import PresetsPanel, _RunWorker
from ui.main_window import MainWindow


@pytest.fixture(scope="module", autouse=True)
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def run(tmp_path):
    service = ExperimentMetadataService(tmp_path, tmp_path / "private-history.sqlite")
    return service.begin("dual_gate_sweep", "sample", settings={"voltage_settle_s": 0})


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_all_output_files_and_terminal_state_share_one_durable_write(run, tmp_path, status):
    files = []
    for i in range(46):
        path = tmp_path / f"output-{i}.csv"
        path.write_text("x,y\n1,2\n", encoding="utf-8")
        files.append({"path": path, "role": "raw", "details": {"condition_id": str(i)}})
    events = []
    def terminal(event):
        saved = json.loads(run.path.read_text(encoding="utf-8"))
        events.append((event.status, saved["status"], len(saved["files"])))
    subscribe(terminal)
    try:
        with patch.object(metadata_module, "_atomic_json", wraps=metadata_module._atomic_json) as write, \
                patch.object(run.service.history, "upsert", wraps=run.service.history.upsert) as index:
            if status == "completed":
                run.complete({"message": "Run complete."}, files=files)
            elif status == "failed":
                run.fail("acquisition failed", files=files)
            else:
                run.cancel("operator stopped", files=files)
        assert write.call_count == 1
        assert index.call_count == 1
        assert events == [(status, status, 47)]
        assert run.service.history.query("sample", "dual_gate_sweep")[0]["status"] == status
        saved = json.loads(run.path.read_text(encoding="utf-8"))
        assert [entry["condition_id"] for entry in saved["files"][1:]] == [str(i) for i in range(46)]
    finally:
        unsubscribe(terminal)


def test_repeated_file_registration_does_not_rewrite_unchanged_metadata(run, tmp_path):
    path = tmp_path / "output.csv"
    run.register_file(path, "raw", details={"condition_id": "1"})
    with patch.object(metadata_module, "_atomic_json", wraps=metadata_module._atomic_json) as write:
        run.register_file(path, "raw", details={"condition_id": "1"})
        assert write.call_count == 0
        run.register_file(path, "raw", details={"partial": True})
        assert write.call_count == 1
    saved = json.loads(run.path.read_text(encoding="utf-8"))
    assert saved["files"][-1]["condition_id"] == "1"
    assert saved["files"][-1]["partial"] is True


def test_failed_terminal_write_does_not_publish_completion(run, tmp_path):
    events = []
    subscribe(events.append)
    try:
        with patch.object(metadata_module, "_atomic_json", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                run.complete(files=[{"path": tmp_path / "output.csv", "role": "raw"}])
        assert events == []
        assert not run._terminal
        assert json.loads(run.path.read_text(encoding="utf-8"))["status"] == "running"
    finally:
        unsubscribe(events.append)


def test_identical_registration_retries_after_failed_persistence(run, tmp_path):
    path = tmp_path / "retry.csv"
    with patch.object(metadata_module, "_atomic_json", side_effect=OSError("temporary write failure")):
        with pytest.raises(OSError, match="temporary write failure"):
            run.register_file(path, "raw")
    with patch.object(metadata_module, "_atomic_json", wraps=metadata_module._atomic_json) as write:
        run.register_file(path, "raw")
        assert write.call_count == 1
    saved = json.loads(run.path.read_text(encoding="utf-8"))
    assert saved["files"][-1]["path"] == "retry.csv"


def test_executed_plan_is_preserved_once_and_reported_as_non_loadable(tmp_path):
    schedule = [{"point": i, "configuration": {"center_nm": 1100}} for i in range(10022)]
    settings = {"executed_plan": {"acquisition_schedule": schedule}, "voltage_settle_s": .05}
    service = ExperimentMetadataService(tmp_path, tmp_path / "private-history.sqlite")
    run = service.begin("dual_gate_sweep", "sample", settings=settings)
    saved = json.loads(run.path.read_text(encoding="utf-8"))
    assert saved["settings"]["requested"]["executed_plan"]["acquisition_schedule"] == schedule
    assert "executed_plan" not in saved["settings"]["loadable"]
    preview = service.preview_settings(saved)
    assert preview["loadable"]["voltage_settle_s"] == .05
    assert "executed_plan" in preview["skipped"]
    assert settings["executed_plan"]["acquisition_schedule"] is schedule


def make_worker(run, tmp_path, *, stop=None):
    meta = fixtures.SMUResilienceTests._run_meta()
    meta.update(initial_voltage_settle_s=0., voltage_settle_s=0.)
    return _RunWorker(
        [{"Center Wavelength (nm)": 700., "Exposure Time (ms)": 1., "Accumulations (EPF)": 1}],
        pd.DataFrame([fixtures.SMUResilienceTests._batch_row()]),
        lf6_ctrl=_FakeLFController(),
        smu_ctrl=SimpleNamespace(is_connected=True, device=_HealthyRunDevice()),
        out_dir=tmp_path, run_meta=meta, filename_parts=["device_id"],
        stop_event=stop or threading.Event(), metadata_run=run,
        files_before={tmp_path / "old.csv", tmp_path / "private-history.sqlite"},
    )


def test_worker_saves_outputs_before_finished_while_gui_timers_continue(run, tmp_path, monkeypatch):
    from utils.config import cfg
    monkeypatch.setattr(cfg.ramp, "delay_s", 0.)
    (tmp_path / "old.csv").write_text("old", encoding="utf-8")
    worker = make_worker(run, tmp_path)
    entered, release = threading.Event(), threading.Event()
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
    finished = []
    worker.finished.connect(lambda success, message: finished.append((success, message)), Qt.ConnectionType.DirectConnection)
    events = []
    subscribe(events.append)
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
    timer.start(5)
    original_write = metadata_module._atomic_json
    def delayed_write(path, value):
        if value.get("status") == "completed":
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test finalization was not released")
        original_write(path, value)
    try:
        with patch.object(metadata_module, "_atomic_json", side_effect=delayed_write):
            thread.start()
            deadline = time.perf_counter() + 2
            while not entered.is_set() and time.perf_counter() < deadline:
                QTest.qWait(10)
            assert entered.is_set(), "Worker did not finalize the experiment"
            assert worker.metadata_finalizing
            assert events == []
            assert finished == []
            baseline = len(ticks)
            QTest.qWait(100)
            assert len(ticks) > baseline + 2
            release.set()
            assert thread.wait(2000)
        assert finished and finished[-1][0], finished
        assert [e.status for e in events] == ["completed"]
        saved = json.loads(run.path.read_text(encoding="utf-8"))
        raw = [entry["path"] for entry in saved["files"] if entry["role"] == "raw"]
        assert len(raw) == 1 and raw[0].startswith("PL/")
        assert "old.csv" not in raw
        assert not worker.metadata_finalizing
    finally:
        release.set()
        thread.quit()
        assert thread.wait(4000)
        timer.stop()
        unsubscribe(events.append)


def test_worker_reports_save_failure_without_completed_event(run, tmp_path, monkeypatch):
    from utils.config import cfg
    monkeypatch.setattr(cfg.ramp, "delay_s", 0.)
    worker = make_worker(run, tmp_path)
    finished, events = [], []
    worker.finished.connect(lambda *args: finished.append(args))
    subscribe(events.append)
    try:
        with patch.object(metadata_module, "_atomic_json", side_effect=OSError("disk full")):
            worker.run()
        assert finished and finished[-1][0] is False
        assert "disk full" in finished[-1][1]
        assert not any(event.status == "completed" for event in events)
    finally:
        unsubscribe(events.append)


def test_cancelled_run_with_degraded_metadata_is_reported_as_failure(run, tmp_path, monkeypatch):
    from utils.config import cfg
    monkeypatch.setattr(cfg.ramp, "delay_s", 0.)
    run.metadata["metadata_status"] = "degraded"
    stop = threading.Event()
    stop.set()
    worker = make_worker(run, tmp_path, stop=stop)
    finished = []
    worker.finished.connect(lambda *args: finished.append(args))
    worker.run()
    assert run.metadata["status"] == "failed"
    assert finished[-1][0] is False
    assert "Metadata finalization failed" in finished[-1][1]


def test_metadata_failure_is_visible_even_after_stop():
    widget = PresetsPanel()
    widget._stop_event.set()
    widget._run_worker = SimpleNamespace(metadata_failed=True)
    try:
        widget._on_finished(False, "Metadata finalization failed: disk full")
        assert widget._run_outcome == "failed"
        assert "metadata" in widget._status_lbl.text().lower()
    finally:
        widget.close()


def test_panel_finished_callback_does_not_write_metadata(run):
    widget = PresetsPanel()
    widget._experiment_run = run
    try:
        with patch.object(metadata_module, "_atomic_json", wraps=metadata_module._atomic_json) as write:
            widget._on_finished(True, "Run complete.")
        assert write.call_count == 0
    finally:
        widget.close()


@pytest.mark.parametrize("finalizing", [True, False])
def test_close_keeps_application_open_through_cleanup_and_finalization(finalizing):
    window = MainWindow.__new__(MainWindow)
    QMainWindow.__init__(window)
    window._presets = SimpleNamespace(
        metadata_finalizing=finalizing,
        _run_thread=SimpleNamespace(isRunning=lambda: True),
    )
    window._power_sweep = SimpleNamespace(shutdown=Mock(return_value=True))
    window._status = QStatusBar(window)
    event = QCloseEvent()
    try:
        with patch("ui.main_window.QMessageBox.critical"):
            window.closeEvent(event)
        assert not event.isAccepted()
        window._power_sweep.shutdown.assert_not_called()
        assert ("saving" if finalizing else "running") in window._status.currentMessage().lower()
    finally:
        window.deleteLater()
