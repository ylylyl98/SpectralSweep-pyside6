"""Dual Gate startup stays responsive and persists its plan before device I/O."""
import json
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication

from app.experiment_metadata import ExperimentMetadataService
from tests import test_smu_resilience as smu_fixtures
from ui import presets_panel as panel


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication([])


def pump_until(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    app.processEvents()
    assert predicate(), "Qt startup/completion did not arrive"


def events(run):
    if not run.event_path.exists():
        return []
    return [json.loads(line) for line in run.event_path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def startup(app, tmp_path, monkeypatch):
    state = SimpleNamespace(commands=[], runs=[], release=threading.Event(), entered=threading.Event(),
                            begin_threads=[], dialogs=[], tmp_path=tmp_path)

    def command(name):
        state.commands.append(name)
        assert state.runs, "Device command preceded experiment metadata creation"
        run = state.runs[0]
        saved = run.service.load_metadata(run.path)
        assert saved["settings"]["requested"]["executed_plan"]["sequence"]
        assert any(event["event"] == "plan_requested" for event in events(run))

    class Device(smu_fixtures._HealthyRunDevice):
        def read_current_gates(self, **kwargs):
            command("read_gates")
            return super().read_current_gates(**kwargs)

        def read_currents(self, **kwargs):
            command("read_currents")
            return super().read_currents(**kwargs)

        def set_gates(self, **kwargs):
            command("set_gates")
            return super().set_gates(**kwargs)

        def ramp_all_to_zero_report(self, **kwargs):
            command("zero")
            return super().ramp_all_to_zero_report(**kwargs)

    class Detector(smu_fixtures._FakeLFController):
        def configure_for_acquisition(self, **kwargs):
            command("configure")
            return super().configure_for_acquisition(**kwargs)

    detector = Detector()
    original_acquire = detector.adapter.acquire

    def acquire():
        command("acquire")
        return original_acquire()

    detector.adapter.acquire = acquire
    state.detector = detector
    state.device = Device()
    state.widget = widget = panel.PresetsPanel(
        lf6_ctrl=detector, smu_ctrl=SimpleNamespace(is_connected=True, device=state.device),
        bind_controller=False, initialize_plan=False)
    widget._current_output_dir = lambda _: tmp_path
    draft = widget.capture_session_state()
    loops = [dict(Enable=True, Parameter="Center Wavelength (nm)", Values="700", Group=1)]
    rows = [smu_fixtures.SMUResilienceTests._batch_row(frames=2, Vbg_start=0., Vbg_stop=.01)]
    draft.update(draft_loop=loops, applied_loop=loops, draft_batch=rows, applied_batch=rows,
                 execution_order=None, applied_execution_order=None, nested_schedule_enabled=False)
    widget.restore_session_state(draft)
    widget._sample_edit.setText("startup-test")
    widget._temp_edit.setText("4")
    widget._initial_voltage_settle_spin.setValue(0)
    widget._voltage_settle_spin.setValue(0)
    widget._preview_timer.stop()
    monkeypatch.setattr(panel.cfg.ramp, "delay_s", 0.)
    monkeypatch.setattr(panel.cfg.ramp, "settle_s", 0.)
    monkeypatch.setattr("ui.detector_wavelength_advice.confirm_sequence_wavelength", lambda *_a, **_k: True)
    monkeypatch.setattr(panel.QMessageBox, "warning", lambda *_args: state.dialogs.append(_args))
    monkeypatch.setattr(panel.QMessageBox, "critical", lambda *_args: state.dialogs.append(_args))
    yield state
    state.release.set()
    if widget._run_thread is not None:
        pump_until(app, lambda: not widget._run_thread.isRunning())
    widget._preview_timer.stop()
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def block_metadata(startup, monkeypatch, *, failure=None):
    class Service:
        def __init__(self, output_root):
            self.service = ExperimentMetadataService(output_root, startup.tmp_path / "history.sqlite")

        def begin(self, *args, **kwargs):
            startup.begin_threads.append(threading.get_ident())
            startup.entered.set()
            assert startup.release.wait(5), "Test did not release startup metadata"
            if failure == "begin":
                raise OSError("simulated startup persistence failure")
            run = self.service.begin(*args, **kwargs)
            startup.runs.append(run)
            if failure == "plan_requested":
                original = run.record_event

                def record_event(event, **values):
                    if event == "plan_requested":
                        return None
                    return original(event, **values)

                run.record_event = record_event
            return run

    monkeypatch.setattr(panel, "ExperimentMetadataService", Service)


def start_blocked(startup, app):
    started = time.monotonic()
    startup.widget._on_run()
    assert time.monotonic() - started < 1, "Run click waited for metadata on the GUI thread"
    assert not startup.dialogs, startup.dialogs
    pump_until(app, startup.entered.is_set)
    assert startup.begin_threads == [startup.begin_threads[0]]
    assert startup.begin_threads[0] != threading.get_ident()
    assert not startup.commands
    assert startup.widget._run_thread.isRunning()


def test_slow_metadata_keeps_gui_live_and_precedes_all_device_io(startup, app, monkeypatch):
    block_metadata(startup, monkeypatch)
    start_blocked(startup, app)
    ticks = []
    QTimer.singleShot(0, lambda: (ticks.append(True), startup.widget._log("startup heartbeat")))
    app.processEvents()
    assert ticks == [True]
    assert "startup heartbeat" in startup.widget._log_text.toPlainText()
    assert startup.widget._stop_btn.isEnabled()
    startup.release.set()
    pump_until(app, lambda: not startup.widget._run_thread.isRunning())
    assert "acquire" in startup.commands
    assert startup.runs[0].metadata["status"] == "completed"
    assert startup.widget._run_outcome == "completed"


def test_slow_plan_validation_runs_on_worker_and_stop_prevents_metadata_and_io(startup, app, monkeypatch):
    validation_threads = []
    original = panel._validate_safe_jumps

    def validate(*args, **kwargs):
        validation_threads.append(threading.get_ident())
        startup.entered.set()
        assert startup.release.wait(5), "Test did not release plan validation"
        return original(*args, **kwargs)

    monkeypatch.setattr(panel, "_validate_safe_jumps", validate)
    service = Mock(side_effect=AssertionError("Metadata started after Stop during validation"))
    monkeypatch.setattr(panel, "ExperimentMetadataService", service)
    started = time.monotonic()
    startup.widget._on_run()
    assert time.monotonic() - started < 1
    pump_until(app, startup.entered.is_set)
    assert validation_threads == [validation_threads[0]]
    assert validation_threads[0] != threading.get_ident()
    ticks = []
    QTimer.singleShot(0, lambda: ticks.append(True))
    app.processEvents()
    assert ticks == [True]
    startup.widget._on_stop()
    startup.release.set()
    pump_until(app, lambda: not startup.widget._run_thread.isRunning())
    service.assert_not_called()
    assert not startup.commands
    assert startup.widget._run_outcome == "stopped"


def test_stop_during_startup_finishes_durable_metadata_without_device_commands(startup, app, monkeypatch):
    block_metadata(startup, monkeypatch)
    start_blocked(startup, app)
    startup.widget._on_stop()
    assert startup.widget._stop_event.is_set()
    assert not startup.commands
    startup.release.set()
    pump_until(app, lambda: not startup.widget._run_thread.isRunning())
    assert not startup.commands
    assert startup.detector.adapter.acquire_calls == 0
    assert startup.runs[0].service.load_metadata(startup.runs[0].path)["status"] == "cancelled"
    assert startup.widget._run_outcome == "stopped"


@pytest.mark.parametrize("failure", ["begin", "plan_requested"])
def test_startup_storage_error_cannot_issue_device_commands(startup, app, monkeypatch, failure):
    block_metadata(startup, monkeypatch, failure=failure)
    start_blocked(startup, app)
    startup.release.set()
    pump_until(app, lambda: not startup.widget._run_thread.isRunning())
    assert not startup.commands
    assert startup.widget._run_outcome == "failed"
    assert startup.widget._run_worker.metadata_failed
    if startup.runs:
        assert startup.runs[0].service.load_metadata(startup.runs[0].path)["status"] == "failed"


@pytest.mark.parametrize("capture_status", [{"active": True}, {"active": False, "recovery_required": True}])
def test_active_or_quarantined_capture_blocks_plain_plan_without_rebinding(startup, app, monkeypatch, capture_status):
    startup.detector.capture_status = capture_status
    assert all("Measurement setup" not in task["ctx"] for task in startup.widget._acquisition_schedule)
    bind = Mock(side_effect=AssertionError("Existing capture recorder was rebound"))
    monkeypatch.setattr(panel, "bind_lightfield_metadata", bind)
    block_metadata(startup, monkeypatch)
    start_blocked(startup, app)
    startup.release.set()
    pump_until(app, lambda: not startup.widget._run_thread.isRunning())
    bind.assert_not_called()
    assert not startup.commands
    assert startup.widget._run_outcome == "failed"
    assert startup.runs[0].service.load_metadata(startup.runs[0].path)["status"] == "failed"


def test_startup_keeps_applied_plan_and_filename_when_next_draft_is_edited(startup, app, monkeypatch):
    block_metadata(startup, monkeypatch)
    start_blocked(startup, app)
    widget = startup.widget
    sequence = widget._final_seq
    schedule = widget._acquisition_schedule
    worker = widget._run_worker
    batch = worker._batch.copy(deep=True)
    parts = list(worker._parts)
    assert not widget._apply_btn.isEnabled()
    widget._loop_table.item(0, 2).setText("710")
    widget._batch_table.item(0, panel.BATCH_SCHEMA.index("frames")).setText("3")
    widget._sample_edit.setText("next-sample")
    widget._temp_edit.setText("9")
    widget._on_apply()
    widget._on_discard()
    assert widget._final_seq is sequence and widget._acquisition_schedule is schedule
    assert worker._seq is sequence and worker._batch.equals(batch)
    assert worker._meta["device_id"] == "startup-test"
    assert worker._meta["temperature"] == "4"
    assert worker._parts == parts
    widget._on_stop()
    startup.release.set()
    pump_until(app, lambda: not widget._run_thread.isRunning())
    assert not startup.commands
    saved = startup.runs[0].service.load_metadata(startup.runs[0].path)
    assert saved["settings"]["requested"]["executed_plan"]["sequence"] == sequence
    assert saved["settings"]["requested"]["device_id"] == "startup-test"
    assert saved["settings"]["requested"]["temperature"] == "4"


def test_deferred_gui_validation_does_not_scan_resolved_schedule(startup, monkeypatch):
    widget = startup.widget
    for name in ("_required_smu_roles", "_required_optical_axes", "_setup_readiness_issues", "_validate_safe_jumps"):
        monkeypatch.setattr(panel, name, Mock(side_effect=AssertionError(f"GUI scanned {name}")))
    monkeypatch.setattr(widget, "_refresh_readiness", Mock(side_effect=AssertionError("GUI refreshed readiness")))
    monkeypatch.setattr(widget, "_refresh_sequence_preview", Mock(side_effect=AssertionError("GUI rebuilt preview")))
    metadata, error = widget._validate_before_run(defer_plan_checks=True)
    assert error is None
    assert metadata["device_id"] == "startup-test"
    assert metadata["executed_plan"]["sequence"] == widget._final_seq


@pytest.mark.parametrize("invalid", ["sample", "temperature", "dirty", "disconnected"])
def test_deferred_gui_validation_still_rejects_bad_inputs(startup, invalid):
    widget = startup.widget
    if invalid == "sample":
        widget._sample_edit.setText("")
    elif invalid == "temperature":
        widget._temp_edit.setText("")
    elif invalid == "dirty":
        widget._loop_table.item(0, 2).setText("710")
    else:
        startup.detector.is_connected = False
    metadata, error = widget._validate_before_run(defer_plan_checks=True)
    assert metadata is None and error
