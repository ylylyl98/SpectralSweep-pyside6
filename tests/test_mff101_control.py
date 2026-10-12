"""Manual flipper control: thread ownership, feedback, and passive restore."""
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_for(app, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert predicate()


class Device:
    def __init__(self):
        self.position = 1
        self.calls = []
        self.release = threading.Event()
        self.release.set()
        self.fail_move = False
        self.fail_read = False
        self.fail_close = False
        self.read_release = threading.Event()
        self.read_release.set()

    def record(self, action):
        self.calls.append((action, threading.get_ident()))

    def open(self, serial_number):
        self.record(("open", serial_number))
        self.serial_number = serial_number
        return self

    def get_position(self):
        self.record("read")
        if not self.read_release.wait(2):
            raise TimeoutError("test read blocked")
        if self.fail_read:
            raise OSError("USB lost")
        return self.position

    def move_to(self, position, *, stop_event=None):
        self.record(("move", position))
        if not self.release.wait(2):
            raise TimeoutError("test device blocked")
        if self.fail_move:
            raise TimeoutError("Flipper did not reach target")
        self.position = position
        return position

    def close(self):
        self.record("close")
        if self.fail_close:
            raise OSError("USB close failed")


@pytest.fixture
def control(app, monkeypatch):
    from controllers.mff101_controller import MFF101Controller
    from utils.config import cfg, MFF101Config

    monkeypatch.setattr(cfg, "mff101", MFF101Config())
    device = Device()
    ctrl = MFF101Controller(adapter_factory=device.open, scan_func=lambda: ["37000001"])
    yield ctrl, device
    device.release.set()
    device.read_release.set()
    device.fail_close = False
    assert ctrl.shutdown()
    app.processEvents()


def connect(app, ctrl):
    assert ctrl.connect_instrument("37000001")
    wait_for(app, lambda: ctrl.is_connected and not ctrl.is_busy)


def test_connection_move_and_close_all_own_one_background_thread(app, control):
    ctrl, device = control
    main_thread = threading.get_ident()
    assert device.calls == []
    connect(app, ctrl)
    assert ctrl.state["position"] == 1
    assert ctrl.move_to(2)
    wait_for(app, lambda: not ctrl.is_busy)
    assert ctrl.state["position"] == 2
    assert ctrl.disconnect_instrument()
    wait_for(app, lambda: not ctrl.is_busy)
    assert not ctrl.is_connected
    assert ctrl.state["position"] is None
    assert device.calls[-1][0] == "close"
    owners = {owner for _, owner in device.calls}
    assert len(owners) == 1 and main_thread not in owners


def test_pending_move_cannot_queue_another_flip(app, control):
    ctrl, device = control
    connect(app, ctrl)
    device.release.clear()
    assert ctrl.move_to(2)
    assert ctrl.is_busy and ctrl.state["position"] is None
    assert not ctrl.move_to(1)
    device.release.set()
    wait_for(app, lambda: not ctrl.is_busy)
    assert [action for action, _ in device.calls if isinstance(action, tuple) and action[0] == "move"] == [("move", 2)]


def test_move_timeout_never_reports_target_as_reached(app, control):
    ctrl, device = control
    connect(app, ctrl)
    device.fail_move = True
    assert ctrl.move_to(2)
    wait_for(app, lambda: not ctrl.is_busy)
    assert ctrl.state["position"] is None
    assert "target" in ctrl.state["error"]
    device.fail_move = False
    assert ctrl.move_to(2)
    wait_for(app, lambda: not ctrl.is_busy)
    assert ctrl.state["position"] == 2
    assert ctrl.state["error"] == ""


def test_lost_usb_invalidates_position_and_allows_reconnect(app, control):
    ctrl, device = control
    connect(app, ctrl)
    device.fail_read = True
    assert ctrl.read_position()
    wait_for(app, lambda: not ctrl.is_busy)
    assert not ctrl.is_connected
    assert ctrl.state["position"] is None
    assert "USB lost" in ctrl.state["error"]
    device.fail_read = False
    connect(app, ctrl)
    assert ctrl.state["position"] == 1


def test_poll_tracks_flips_from_device_button(app, control):
    ctrl, device = control
    connect(app, ctrl)
    device.position = None
    wait_for(app, lambda: ctrl.state["position"] is None)
    device.position = 2
    wait_for(app, lambda: ctrl.state["position"] == 2)


def test_shutdown_during_move_closes_on_worker_without_gui_event_loop(app, control):
    ctrl, device = control
    connect(app, ctrl)
    device.release.clear()
    assert ctrl.move_to(2)
    release_timer = threading.Timer(0.05, device.release.set)
    release_timer.start()
    try:
        assert ctrl.shutdown()
        assert not ctrl.is_connected
        assert device.calls[-1][0] == "close"
        assert device.position == 2
        assert not ctrl.move_to(1)
    finally:
        device.release.set()
        release_timer.join()


def test_initial_read_failure_releases_device_and_reports_connection_failure(app, control):
    ctrl, device = control
    device.fail_read = True
    assert ctrl.connect_instrument("37000001")
    wait_for(app, lambda: not ctrl.is_busy)
    assert not ctrl.is_connected
    assert device.calls[-1][0] == "close"
    assert "USB lost" in ctrl.state["error"]


def test_shutdown_close_failure_is_reported_and_can_be_retried(app, control):
    ctrl, device = control
    connect(app, ctrl)
    device.fail_close = True
    errors = []
    ctrl.error.connect(errors.append)
    assert not ctrl.shutdown(timeout_ms=500)
    assert "USB close failed" in ctrl.state["error"]
    assert errors and "USB close failed" in errors[-1]
    device.fail_close = False
    assert ctrl.shutdown()
    assert [action for action, _ in device.calls].count("close") == 2


def test_pending_read_preserves_actual_observation_timestamp(app, control):
    from ui.mff101_section import MFF101Section

    ctrl, device = control
    panel = MFF101Section(ctrl)
    try:
        connect(app, ctrl)
        observed = panel._spectrum_readbacks["position"]["timestamp_utc"]
        device.read_release.clear()
        ctrl.read_position()
        assert ctrl.is_busy
        assert panel._spectrum_readbacks["position"]["timestamp_utc"] == observed
        device.read_release.set()
        wait_for(app, lambda: not ctrl.is_busy)
        assert panel._spectrum_readbacks["position"]["timestamp_utc"] != observed
    finally:
        device.read_release.set()
        panel.close()


def test_scan_does_not_connect_and_buttons_follow_verified_state(app, control):
    from ui.mff101_section import MFF101Section

    ctrl, device = control
    panel = MFF101Section(ctrl)
    try:
        assert not panel._position1.isEnabled()
        panel._scan.click()
        wait_for(app, lambda: not ctrl.is_busy)
        assert panel._serial.currentText() == "37000001"
        assert device.calls == []
        panel._connect.click()
        wait_for(app, lambda: ctrl.is_connected and not ctrl.is_busy)
        assert "1" in panel._position.text()
        device.release.clear()
        panel._position2.click()
        assert not panel._position1.isEnabled()
        assert "Moving" in panel._status.text()
        device.release.set()
        wait_for(app, lambda: not ctrl.is_busy)
        assert "2" in panel._position.text()
        assert panel._position1.isEnabled()
        assert not panel._position2.isEnabled()
    finally:
        panel.close()


def test_sidebar_restore_is_passive_and_readback_becomes_unknown_on_disconnect(app, control):
    from ui.instrument_panel import InstrumentPanel

    ctrl, device = control
    panel = InstrumentPanel(mff101_ctrl=ctrl)
    try:
        panel.restore_session_state({"mff101": {"serial_number": "37000001", "position": 2}})
        app.processEvents()
        assert device.calls == []
        assert panel.capture_session_state()["mff101"] == {"serial_number": "37000001"}
        connect(app, ctrl)
        observed = panel.capture_spectrum_readbacks()["instruments"]["mff101"]
        assert observed["connected"]
        assert observed["readbacks"]["position"]["value"] == 1
        ctrl.disconnect_instrument()
        wait_for(app, lambda: not ctrl.is_busy)
        observed = panel.capture_spectrum_readbacks()["instruments"]["mff101"]
        assert not observed["connected"]
        assert observed["readbacks"]["position"]["available"] is False
    finally:
        panel.close()


def test_serial_config_round_trip_and_old_config_defaults(tmp_path):
    from utils.config import AppConfig

    config = AppConfig()
    config.mff101.serial_number = "37000001"
    path = tmp_path / "config.json"
    config.save(path)
    restored = AppConfig()
    restored.load(path)
    assert restored.mff101.serial_number == "37000001"
    path.write_text("{}", encoding="utf-8")
    restored = AppConfig()
    restored.load(path)
    assert restored.mff101.serial_number == ""


def test_sample_profile_does_not_replace_global_flipper_identity():
    from ui.main_window import MainWindow

    state = {"panels": {"instruments": {"mff101": {"serial_number": "37000001"}}}}
    filtered = MainWindow._sample_scoped_state(state)
    assert "serial_number" not in filtered["panels"]["instruments"]["mff101"]
    assert state["panels"]["instruments"]["mff101"]["serial_number"] == "37000001"
