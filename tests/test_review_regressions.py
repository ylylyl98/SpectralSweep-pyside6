"""One regression per defect fixed after the 2026-11 code review."""
import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

import tests.test_andor_adapter as andor_tests
from tests.test_dual_gate_bias2 import row
import tests.test_smu_resilience as smu_tests
import ui.presets_panel as panel
from utils.config import AppConfig, cfg


# ---- hardware stop paths -------------------------------------------------

def test_failed_move_sends_a_stop():
    from app.devices import motion_verification

    class Stage:
        stopped = 0
        def move_to(self, target): raise TimeoutError("adapter timeout")
        def get_position(self): return 0.0
        def stop_motion(self): self.stopped += 1

    stage = Stage()
    with pytest.raises(Exception):
        motion_verification.move_and_verify(stage, 10.0, tolerance=0.1, timeout_s=5.0)
    assert stage.stopped >= 1


def test_aps100_pause_is_sent_even_if_the_status_query_fails():
    from app.devices.aps100_attodry1000_adapter import APS100AttoDry1000Adapter

    class Link:
        bytes_in_buffer = 0
        def __init__(self): self.writes, self.reply = [], []
        def write(self, command):
            self.writes.append(command)
            self.reply = None if command == "*ESR?" else [command]
        def read(self):
            if self.reply is None: raise TimeoutError("timeout")
            return self.reply.pop(0)
        def flush(self, operation): pass

    adapter = APS100AttoDry1000Adapter.__new__(APS100AttoDry1000Adapter)
    adapter._lock, adapter._resource, adapter._identity = threading.RLock(), Link(), object()
    adapter.expect_echo, adapter.command_observer = True, None
    adapter.pause(confirm=False)
    assert "SWEEP PAUSE" in adapter._resource.writes


def test_magnet_pause_vetoes_an_operation_queued_before_it():
    from controllers.magnet_controller import _MagnetWorker

    QCoreApplication.instance() or QCoreApplication([])
    worker = _MagnetWorker()

    class Supply:
        paused = moved = 0
        def pause(self): self.paused += 1
        def enter_driven_mode(self, **kwargs): self.moved += 1
        def read_snapshot(self): raise RuntimeError("n/a")

    worker.adapter = Supply()
    worker.request_pause()
    worker.enter_driven_mode()
    assert worker.adapter.moved == 0 and worker._stop_event.is_set()
    worker.pause()
    worker.enter_driven_mode()
    assert (worker.adapter.paused, worker.adapter.moved) == (1, 1)


def test_rotation_requests_run_on_the_worker_thread():
    from controllers.rotation_controller import RotationController

    app = QCoreApplication.instance() or QCoreApplication([])
    controller, seen = RotationController(), {}

    class Mount:
        def get_position(self):
            seen["thread"] = threading.get_ident()
            return 12.5
        def close(self): pass

    try:
        controller._worker._adapters["rot1"] = Mount()
        loop = QEventLoop()
        controller.position_ready.connect(lambda slot, value: (seen.update(value=value), loop.quit()))
        QTimer.singleShot(5000, loop.quit)
        controller.get_position("rot1")
        assert "thread" not in seen  # queued, not run inline on the caller
        loop.exec()
        assert seen.get("value") == 12.5 and seen["thread"] != threading.get_ident()
    finally:
        controller.shutdown()
        app.processEvents()


def test_request_state_can_be_read_while_its_lock_is_held():
    from controllers.attodry2100_controller import Command, RequestState, _Request

    request, seen = _Request(Command.READ, (), 0), []
    request.set_state(RequestState.RUNNING)
    with request.lock:  # held by the timeout watchdog while it takes the controller lock
        reader = threading.Thread(target=lambda: seen.append(request.get_state()))
        reader.start()
        reader.join(2.0)
    assert seen == [RequestState.RUNNING]


# ---- SMU -----------------------------------------------------------------

def test_settled_readings_are_checked_against_setpoints(monkeypatch):
    from app.devices.iv_adapter import IVDevice, SetpointVerificationError

    for name, value in (("verify_setpoints", True), ("setpoint_tolerance_V", 0.005),
                        ("setpoint_tolerance_range_fraction", 2.5e-4),
                        ("compliance_trip_fraction", 0.95)):
        monkeypatch.setattr(cfg.smu, name, value)

    class Device(IVDevice):
        def __init__(self): pass
        def has_role(self, name): return name in ("Vbg", "Vtg")
        def _instrument_for_role(self, role):
            return SimpleNamespace(_volt_range_V=20.0, _curr_compliance_A=1e-6)
        def read_role_snapshot(self, role, *, strict=False):
            raise AssertionError("the check must not read the instrument")

    device, nan = Device(), float("nan")
    # On target; missing readings, unset targets and absent roles are skipped.
    device.check_setpoint_readings(
        {"Vbg": 2.0, "Vtg": nan, "Vbias": 3.0},
        {"Vbg": (2.003, 1e-9), "Vtg": (9.0, 9.0), "Vbias": (0.0, 9.0)})
    device.check_setpoint_readings({"Vbg": 2.0}, {"Vbg": (nan, None)})
    with pytest.raises(SetpointVerificationError, match="not at its setpoint"):
        device.check_setpoint_readings({"Vbg": 2.0}, {"Vbg": (0.0, 0.0)})
    with pytest.raises(SetpointVerificationError, match="current-limited"):
        device.check_setpoint_readings({"Vbg": 5.0}, {"Vbg": (5.0, -0.97e-6)})
    monkeypatch.setattr(cfg.smu, "verify_setpoints", False)
    device.check_setpoint_readings({"Vbg": 2.0}, {"Vbg": (0.0, 0.0)})


def test_manual_smu_target_beyond_the_verified_range_is_refused():
    from controllers.smu_controller import _SMUWorker

    worker = SimpleNamespace(
        _device=SimpleNamespace(role_map={"Vbg": "GPIB::24", "Vtg": "GPIB::25"}),
        _applied_voltage_ranges={"GPIB::24": 20.0})
    _SMUWorker._validate_manual_target(worker, "Vbg", -20.0)
    _SMUWorker._validate_manual_target(worker, "Vtg", 150.0)  # no verified range yet
    with pytest.raises(ValueError, match="exceeds"):
        _SMUWorker._validate_manual_target(worker, "Vbg", 150.0)


# ---- detectors -----------------------------------------------------------

def test_andor_close_still_closes_camera_when_spectrograph_close_fails():
    _module, setup = andor_tests.AndorAdapterTests("make_setup").make_setup()
    camera = setup._camera

    def broken_close():
        raise RuntimeError("shamrock gone")

    setup._spectrograph.close = broken_close
    with pytest.raises(RuntimeError, match="shamrock gone"):
        setup.close()
    assert camera.closed and setup._camera is None


def test_winspec_close_and_gui_stop_do_not_depend_on_the_bridge(monkeypatch):
    from app.devices import winspec_adapter

    release = threading.Event()

    class Bridge:
        def request(self, command, *args, **kwargs):
            if command == "STOP":
                release.wait(5.0)
                return {"stop_requested": True}, None
            raise OSError("XP bridge unreachable")

    setup = winspec_adapter.WinSpecSetup.__new__(winspec_adapter.WinSpecSetup)
    setup._closed, setup.start_acceleration, setup._start_session = False, True, "session"
    setup._abort, setup._busy, setup.client = threading.Event(), threading.Event(), Bridge()
    monkeypatch.setattr(winspec_adapter, "_GUI_STOP_WAIT_S", 0.05)
    try:
        assert setup.abort_acquisition() is False and setup._abort.is_set()
    finally:
        release.set()
    setup.close()
    assert setup._closed


# ---- files, metadata, configuration ---------------------------------------

def test_dual_gate_filenames_identify_vbias2_sweep_and_andor_settings():
    meta = smu_tests.SMUResilienceTests._run_meta()
    sweep = row(bias2_mode="independent", Vbias2_start=0, Vbias2_stop=2, frames=11)
    full, _, _ = panel._build_run_filename_base(meta, {}, sweep)
    nested, _, tokens = panel._build_run_filename_base(
        meta, {}, dict(sweep, frames=1, _nested_original_frames=11))
    assert ("Vbias2", "Vbias2_0V_to_2V") in tokens and nested == full

    plain, _, _ = panel._build_run_filename_base(meta, {}, row())
    andor, _, _ = panel._build_run_filename_base(
        meta, {"Andor Center Wavelength (nm)": 1210, "Andor Exposure Time (ms)": 500,
               "Andor Accumulations (EPF)": 3}, row())
    assert andor == plain + "_Andor_1210nmc_0.5sx3"


def test_partial_paired_save_is_reported_as_a_save_failure():
    from app.paired_spectrum_files import PairedSpectrumSaveError

    class Writer:
        def write_point(self, *args, **kwargs):
            raise PairedSpectrumSaveError(["andor_si: disk full"], {"lightfield": "a.csv"})

    worker = SimpleNamespace(
        _stop=threading.Event(),
        _lf6=SimpleNamespace(acquire_station_pair=lambda *args, **kwargs: {}))
    with pytest.raises(panel._RunFlowError) as raised:
        panel._RunWorker._write_paired_point(worker, Writer(), "andor_si", {}, {}, 4, 9)
    assert raised.value.stage == "save" and "disk full" in str(raised.value)


def test_numpy_scalars_are_stored_as_plain_numbers():
    np = pytest.importorskip("numpy")
    from app.experiment_metadata import _jsonable

    stored = _jsonable({"count": np.int64(3), "gain": np.float32(1.5), "on": np.bool_(True)})
    assert stored == {"count": 3, "gain": 1.5, "on": True}
    assert [type(value) for value in stored.values()] == [int, float, bool]


def test_unreadable_config_keeps_defaults_and_is_preserved(tmp_path):
    content = b'{"lf6": {"center_nm": 700}} \xb5'
    path = tmp_path / "config.json"
    path.write_bytes(content)
    config = AppConfig()
    default_center = config.lf6.center_nm
    config.load(path)
    assert config.lf6.center_nm == default_center
    assert (tmp_path / "config.json.bad").read_bytes() == content


def test_megasweep_rejects_a_step_that_explodes_the_point_count():
    from ui.megasweep_panel import _get_linear_array

    assert len(_get_linear_array(0.0, 1.0, 0.25, "Step Size")) == 5
    with pytest.raises(ValueError, match="limit"):
        _get_linear_array(-10.0, 10.0, 1e-6, "Step Size")
