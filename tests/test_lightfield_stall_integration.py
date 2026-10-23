"""Offline boundary checks: stalled SDK ownership, UI state and sweep output."""
import csv
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pandas as pd
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QStatusBar

from app.experiment_metadata import ExperimentMetadataService
from app.lightfield_capture_guard import capture_operation, capture_status, LightFieldAcquisitionStalled
from app.ntfy_notifications import NtfyNotifier
from controllers.lf6_controller import LF6Controller, LightFieldLifecycleState
from tests import test_smu_resilience as smu_fixtures
from tests.test_smu_resilience import _FakeLFController, _HealthyRunDevice
from ui.main_window import MainWindow
from ui.presets_panel import PresetsPanel, _RunWorker, _setup_readiness_issues
from utils.config import cfg


@pytest.fixture(scope="module", autouse=True)
def app():
    yield QApplication.instance() or QApplication([])


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert predicate()


class NoSDKProbe:
    def __init__(self):
        self._capture_wait_timeout_s = .08
        self.close = Mock()
        self.configure_for_acquisition = Mock(side_effect=AssertionError("Unexpected configure"))

    @property
    def is_busy(self):
        raise AssertionError("Guard state must not query hardware busy")

    @property
    def is_ready(self):
        raise AssertionError("Guard state must not query hardware ready")


@pytest.mark.parametrize("parked", [False, True])
def test_controller_reserves_stalled_owner_and_allows_only_drained_disconnect(parked):
    ctrl, setup = LF6Controller(), NoSDKProbe()
    worker = ctrl._worker
    worker._setup = setup
    worker._adapter = SimpleNamespace(abort_acquisition=Mock())
    worker._identity = {"backend": "lightfield"}
    worker._state = LightFieldLifecycleState.READY
    if parked:
        worker._parked["lightfield"] = (setup, worker._adapter, worker._identity, [])
        worker._setup = SimpleNamespace(is_ready=True, is_busy=False, close=Mock())
        worker._backend = "andor_si"
    entered, release = threading.Event(), threading.Event()
    faults, incidents, states, errors = [], [], [], []
    ctrl.capture_stalled.connect(incidents.append)
    ctrl.capture_status_changed.connect(states.append)
    ctrl.error.connect(errors.append)

    def blocked_sdk():
        try:
            with capture_operation(setup) as op:
                op.stage("capture")
                entered.set()
                assert release.wait(3)
        except Exception as exc:
            faults.append(exc)

    owner = threading.Thread(target=blocked_sdk)
    try:
        owner.start()
        assert entered.wait(1)
        wait_for(lambda: ctrl.capture_status.get("recovery_required"))
        for _ in range(3):
            ctrl._poll_capture_status()
        assert len(incidents) == 1
        assert states[-1]["active"] and states[-1]["phase"] == "capture"
        assert ctrl.switching_locked and ctrl.spectrum_actions_blocked
        assert ctrl.is_busy and not ctrl.is_ready
        ctrl.set_temperature_monitor_paused("presets", True)
        ctrl.set_temperature_monitor_paused("presets", False)
        assert ctrl.spectrum_actions_blocked
        ctrl.abort_acquisition()
        worker._adapter.abort_acquisition.assert_not_called()
        ctrl.disconnect_instrument()
        assert worker.disconnect_instrument() is False
        with pytest.raises(RuntimeError, match="still active"):
            ctrl.shutdown()
        with pytest.raises(RuntimeError, match="stalled"):
            ctrl.configure_for_acquisition(center_nm=730, exposure_ms=300, frames=10)
        worker.apply_settings(300, 730, 10)
        worker.acquire_single()
        worker.lightfield_optics(None)
        worker.read_temperature()
        worker.read_temperature_snapshot(0)
        assert errors
        setup.close.assert_not_called()
        setup.configure_for_acquisition.assert_not_called()
        release.set()
        owner.join(2)
        assert not owner.is_alive()
        assert isinstance(faults[0], LightFieldAcquisitionStalled)
        assert not ctrl.capture_status["active"]
        assert ctrl.spectrum_actions_blocked and not ctrl.is_ready
        assert not ctrl.is_busy
        ctrl.connect_instrument(use_mock=True)
        assert not ctrl._connection_pending
        worker.disconnect_all()
        setup.close.assert_called_once()
        assert ctrl.capture_status == {}
    finally:
        release.set()
        owner.join(2)
        ctrl.shutdown()


def test_stall_ui_overrides_stop_and_keeps_saved_progress():
    status = dict(active=True, recovery_required=True, phase="capture", stalled_phase="capture",
                  elapsed_s=35, incident_id="fake", diagnostic_path="local.jsonl")
    panel = PresetsPanel()
    panel._lf6 = SimpleNamespace(capture_status=status)
    panel._run_thread = SimpleNamespace(isRunning=lambda: True)
    panel._done_frames, panel._total_points = 586, 906
    panel._run_worker = SimpleNamespace(metadata_failed=False)
    try:
        panel._on_stop()
        assert "stalled" in panel._status_lbl.text().lower()
        assert "586/906" in panel._status_lbl.text()
        assert "zero-ramp will begin immediately" not in panel._log_text.toPlainText()
        assert _setup_readiness_issues(panel._lf6, [])
        status["active"] = False
        panel._on_finished(False, "stalled")
        assert panel._run_outcome == "failed"
        assert "reconnect" in panel._status_lbl.text()
    finally:
        panel._run_thread = None
        panel._lf6 = None
        panel.close()


def test_parked_drained_fault_cannot_hide_another_active_owner():
    ctrl = LF6Controller()
    failed = SimpleNamespace(_capture_wait_timeout_s=.01)
    active = SimpleNamespace(_capture_wait_timeout_s=10.)
    try:
        with pytest.raises(LightFieldAcquisitionStalled):
            with capture_operation(failed) as op:
                op.quarantine("fake failed release")
        ctrl._worker._parked["lightfield"] = (failed, None, {}, [])
        ctrl._worker._setup = active
        with capture_operation(active):
            assert ctrl.capture_status["recovery_required"]
            assert ctrl.capture_status["active"]
            with pytest.raises(RuntimeError, match="still active"):
                ctrl.shutdown()
            assert ctrl._worker.disconnect_instrument() is False
    finally:
        ctrl.shutdown()


def test_capture_observer_does_not_probe_sdk_on_healthy_transition_to_idle():
    ctrl, setup = LF6Controller(), NoSDKProbe()
    setup._capture_wait_timeout_s = 10.
    ctrl._worker._setup = setup
    locks = []
    ctrl.switching_lock_changed.connect(locks.append)
    try:
        with capture_operation(setup):
            ctrl._poll_capture_status()
        ctrl._poll_capture_status()
        assert locks == [True, False]
    finally:
        ctrl.shutdown()


@pytest.mark.parametrize("active", [False, True])
def test_main_window_rejects_close_without_touching_devices_and_queues_warning(active):
    window = MainWindow.__new__(MainWindow)
    QMainWindow.__init__(window)
    status = dict(active=active, recovery_required=True, phase="capture", elapsed_s=31, incident_id="fake")
    window._lf6 = SimpleNamespace(capture_status=status, acquisition_in_progress=True)
    window._status = QStatusBar(window)
    window._power_sweep = SimpleNamespace(shutdown=Mock())
    window._ntfy_notifier = SimpleNamespace(notify_warning=Mock())
    try:
        event = QCloseEvent()
        window.closeEvent(event)
        assert not event.isAccepted()
        window._power_sweep.shutdown.assert_not_called()
        window._on_capture_stalled(status)
        window._ntfy_notifier.notify_warning.assert_called_once()
        assert "retained" in window._status.currentMessage()
    finally:
        window.deleteLater()


def test_shutdown_waits_for_queued_capture_before_its_guard_starts():
    ctrl = LF6Controller()
    try:
        ctrl._acquisition_requests = 1
        assert ctrl.capture_status == {}
        assert ctrl.acquisition_in_progress
        with pytest.raises(RuntimeError, match="still active"):
            ctrl.shutdown()
        assert ctrl._thread.isRunning()
    finally:
        ctrl._acquisition_requests = 0
        ctrl.shutdown()


def test_stalled_capture_sends_ntfy_before_sdk_returns_and_deduplicates(monkeypatch):
    # Exercise the actual monitor -> Qt signal -> notifier queue -> HTTP path,
    # with network traffic intercepted and no real subscription used.
    monkeypatch.setattr("app.ntfy_notifications.runtime_url", lambda: "https://ntfy.sh/test-local-only")
    delivered = []

    def fake_urlopen(request, timeout):
        delivered.append((request.full_url, request.headers["Title"], request.data.decode("utf-8")))
        return SimpleNamespace(read=lambda: b"")

    monkeypatch.setattr("app.ntfy_notifications.urllib.request.urlopen", fake_urlopen)
    notifier = NtfyNotifier()
    ctrl, setup = LF6Controller(), NoSDKProbe()
    ctrl._worker._setup = setup
    window = SimpleNamespace(_status=SimpleNamespace(showMessage=Mock()), _ntfy_notifier=notifier)
    ctrl.capture_stalled.connect(lambda status: MainWindow._on_capture_stalled(window, status))
    entered, release = threading.Event(), threading.Event()
    failures = []

    def blocked_sdk():
        try:
            with capture_operation(setup) as operation:
                operation.stage("capture")
                entered.set()
                assert release.wait(5)
        except Exception as exc:
            failures.append(exc)

    owner = threading.Thread(target=blocked_sdk)
    try:
        owner.start()
        assert entered.wait(1)
        # Let the real controller timer detect the stalled call; no manual poll.
        wait_for(lambda: bool(delivered))
        assert owner.is_alive() and not release.is_set()
        assert ctrl.capture_status["active"]
        assert delivered[0][0] == "https://ntfy.sh/test-local-only"
        assert delivered[0][1] == "LightField acquisition stalled"
        assert "capture" in delivered[0][2] and "ownership retained" in delivered[0][2]
        for _ in range(3):
            ctrl._poll_capture_status()
            # Replay delivery too: notifier deduplication protects this layer.
            MainWindow._on_capture_stalled(window, ctrl.capture_status)
        release.set()
        owner.join(2)
        assert not owner.is_alive()
        assert isinstance(failures[0], LightFieldAcquisitionStalled)
        ctrl._poll_capture_status()
        QTest.qWait(150)
        assert len(delivered) == 1
    finally:
        release.set()
        owner.join(2)
        ctrl.shutdown()
        notifier.shutdown()
        notifier._thread.join(1)


@pytest.mark.parametrize("late_error", [False, True])
def test_stop_plus_late_capture_fails_without_saving_or_zeroing(tmp_path, monkeypatch, late_error):
    monkeypatch.setattr(cfg.ramp, "delay_s", 0.)
    service = ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite")
    run = service.begin("dual_gate_sweep", "sample")
    meta = smu_fixtures.SMUResilienceTests._run_meta()
    meta.update(initial_voltage_settle_s=0., voltage_settle_s=0.)
    setup = SimpleNamespace(_capture_wait_timeout_s=.08)
    lf, device = _FakeLFController(), _HealthyRunDevice()
    device.ramp_all_to_zero_report = Mock(wraps=device.ramp_all_to_zero_report)
    device.set_gates = Mock(wraps=device.set_gates)
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    acquisitions = []

    def acquire():
        acquisitions.append(True)
        if len(acquisitions) == 2:
            with capture_operation(setup) as op:
                op.stage("capture")
                entered.set()
                assert release.wait(3)
                if late_error:
                    raise RuntimeError("late SDK failure")
        return np.array([700., 701.]), np.array([10., 11.])

    lf.adapter.acquire = acquire
    worker = _RunWorker(
        [{"Center Wavelength (nm)": 700.}],
        pd.DataFrame([smu_fixtures.SMUResilienceTests._batch_row(frames=3, Vbg_start=.1, Vbg_stop=.3)]),
        lf6_ctrl=lf, smu_ctrl=SimpleNamespace(is_connected=True, device=device),
        out_dir=tmp_path, run_meta=meta, filename_parts=["device_id"],
        stop_event=stop, metadata_run=run)
    finished, progress = [], []
    worker.finished.connect(lambda *args: finished.append(args), Qt.ConnectionType.DirectConnection)
    worker.frame_progress.connect(lambda *args: progress.append(args), Qt.ConnectionType.DirectConnection)
    owner = threading.Thread(target=worker.run)
    try:
        owner.start()
        assert entered.wait(2)
        stop.set()
        wait_for(lambda: capture_status(setup).get("recovery_required"))
        set_calls = device.set_gates.call_count
        reads = device.current_reads
        assert not finished
        release.set()
        owner.join(3)
        assert not owner.is_alive()
        assert finished[-1][0] is False
        assert run.metadata["status"] == "failed"
        assert len(acquisitions) == 2
        assert device.set_gates.call_count == set_calls
        assert device.current_reads == reads
        device.ramp_all_to_zero_report.assert_not_called()
        assert progress[-1] == (1, 3)
        files = list((tmp_path / "PL").glob("*.csv"))
        assert len(files) == 1
        with files[0].open(encoding="utf-8-sig", newline="") as stream:
            assert len(list(csv.DictReader(stream))) == 1
    finally:
        release.set()
        owner.join(4)
