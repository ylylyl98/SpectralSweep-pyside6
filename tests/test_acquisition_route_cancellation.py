"""Cancel real controller/UI requests at simulated USB and detector boundaries."""
import os
import threading
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from controllers.lf6_controller import LF6Controller, LightFieldLifecycleState
from controllers.mff101_controller import MFF101Controller
from tests.test_mff101_control import Device, wait_for
from utils.config import MFF101Config, cfg


@pytest.fixture
def acquisition(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(cfg, "mff101", MFF101Config(auto_route=True))
    usb = Device()
    usb.position = 2
    route_started = threading.Event()
    original_read = usb.get_position

    def read_position():
        if not usb.read_release.is_set():
            route_started.set()
        return original_read()

    usb.get_position = read_position
    flipper = MFF101Controller(adapter_factory=usb.open, scan_func=lambda: ["37862922"])
    controller = LF6Controller()
    controller.set_flipper_controller(flipper)
    captures, spectra, frames, completions, errors = [], [], [], [], []
    axis = np.array([729., 730., 731.])

    def acquire_1d():
        captures.append("1d")
        return axis, np.ones(3)

    def acquire_2d():
        captures.append("2d")
        return np.ones((2, 3))

    setup = SimpleNamespace(close=lambda: None, is_ready=True, is_busy=False,
        configure_for_acquisition=lambda **settings: {}, acquire_2d=acquire_2d)
    adapter = SimpleNamespace(acquire=acquire_1d, abort_acquisition=lambda: False,
        calibration_wavelengths=lambda **kw: axis, invalidate_wavelengths=lambda: None)
    worker = controller._worker
    worker._setup, worker._adapter = setup, adapter
    worker._identity = {"backend": "lightfield"}
    worker._state = LightFieldLifecycleState.READY
    controller.spectrum_ready.connect(lambda *data: spectra.append(data))
    controller.frame_ready.connect(frames.append)
    controller.error.connect(errors.append)
    worker.acquisition_finished.connect(lambda *args: completions.append(True))
    harness = SimpleNamespace(app=app, usb=usb, flipper=flipper, ctrl=controller,
        setup=setup, captures=captures, spectra=spectra, frames=frames,
        completions=completions, errors=errors, route_started=route_started)
    try:
        yield harness
    finally:
        usb.read_release.set()
        usb.release.set()
        controller.shutdown()
        assert flipper.shutdown()
        app.processEvents()


def wait_until_finished(acquisition):
    wait_for(acquisition.app, lambda: acquisition.ctrl._acquisition_requests == 0
             and not acquisition.ctrl.switching_locked)


@pytest.mark.parametrize("mode", ["1d", "2d"])
def test_spectrum_stop_during_second_route_readback_prevents_exposure(acquisition, monkeypatch, mode):
    from ui.spectrum_panel import SpectrumPanel

    h = acquisition
    configured = []

    def configure(**settings):
        configured.append(settings)
        # First route succeeded. Only the acquisition preflight will now wait.
        h.usb.read_release.clear()
        return {"exposure_ms": settings["exposure_ms"], "frames": settings["frames"]}

    h.setup.configure_for_acquisition = configure
    # The unrelated optics drawer has no vendor SDK surface in this fake setup.
    monkeypatch.setattr(h.ctrl, "lightfield_optics", lambda requested=None: None)
    monkeypatch.setattr("ui.detector_wavelength_advice.confirm_detector_wavelength", lambda *args: True)
    panel = SpectrumPanel(h.ctrl)
    panel._on_lf6_connected([])
    try:
        getattr(panel, f"_run_{mode}_btn").click()
        wait_for(h.app, h.route_started.is_set)
        assert configured
        assert panel._pending_acquisition is None
        assert panel._continuous_mode == mode
        assert panel._stop_btn.isEnabled()
        assert h.captures == []
        panel._stop_btn.click()
        assert panel._continuous_mode is None
        h.usb.read_release.set()
        wait_until_finished(h)
        assert h.captures == [], "Stop must prevent a new exposure after USB readback"
        assert not h.spectra and not h.frames
        assert len(h.completions) == 1
        assert h.errors == [], "Expected cancellation must not become an acquisition error"
        assert not h.flipper.state["manual_locked"]
    finally:
        h.usb.read_release.set()
        panel.close()


@pytest.mark.parametrize("first_mode", ["1d", "2d"])
def test_abort_cancels_active_and_queued_requests_but_allows_fresh_request(acquisition, first_mode):
    h = acquisition
    h.usb.read_release.clear()
    first = h.ctrl.acquire_single if first_mode == "1d" else h.ctrl.acquire_2d
    second = h.ctrl.acquire_2d if first_mode == "1d" else h.ctrl.acquire_single
    first()
    wait_for(h.app, h.route_started.is_set)
    second()
    assert h.ctrl._acquisition_requests == 2
    assert h.ctrl.switching_locked
    assert h.ctrl.abort_acquisition(), "A pending preflight cancellation is accepted"
    # Queue a new independent request before the old ones have completed.
    h.ctrl.acquire_single()
    assert h.ctrl._acquisition_requests == 3
    assert h.ctrl.switching_locked
    h.usb.read_release.set()
    wait_until_finished(h)
    assert h.captures == ["1d"], "Only the request submitted after Stop may expose"
    assert len(h.spectra) == 1 and not h.frames
    assert len(h.completions) == 3
    assert h.errors == []
    assert not h.flipper.state["manual_locked"]


def test_abort_cancels_requests_queued_before_worker_starts_them(acquisition):
    h = acquisition
    configuring, release = threading.Event(), threading.Event()

    def configure(**settings):
        configuring.set()
        if not release.wait(2):
            raise TimeoutError("Test setup configuration was not released")
        return {}

    h.setup.configure_for_acquisition = configure
    try:
        h.ctrl.apply_settings(100., 730., 1)
        wait_for(h.app, configuring.is_set)
        h.ctrl.acquire_single()
        h.ctrl.acquire_2d()
        h.ctrl.abort_acquisition()
        release.set()
        wait_until_finished(h)
        assert h.captures == []
        assert not h.spectra and not h.frames
        assert len(h.completions) == 2
        assert h.errors == []
    finally:
        release.set()


def test_stop_during_andor_2d_mode_change_prevents_exposure(acquisition):
    h = acquisition
    mode_started, release = threading.Event(), threading.Event()

    def change_mode():
        mode_started.set()
        if not release.wait(2):
            raise TimeoutError("Test read mode change was not released")

    worker = h.ctrl._worker
    worker._backend = "andor_si"
    worker._identity = {"backend": "andor_sdk2", "camera_role": "si"}
    h.usb.position = 1
    h.setup.change_roi_FullSensor = change_mode
    try:
        h.ctrl.acquire_2d()
        wait_for(h.app, mode_started.is_set)
        h.ctrl.abort_acquisition()
        release.set()
        wait_until_finished(h)
        assert h.captures == [], "Check Stop again after the read mode SDK operation"
        assert not h.frames
        assert len(h.completions) == 1
        assert h.errors == []
    finally:
        release.set()


def test_stop_during_physical_route_prevents_retry_command(acquisition, monkeypatch):
    from app.devices import mff101_adapter as module
    from tests.test_mff101_adapter import _Flipper

    h = acquisition
    driver = _Flipper()
    cfg.mff101.serial_number = "37000123"
    commands, started = [], threading.Event()
    driver.get_state = lambda: 0

    def move(value):
        commands.append(value)
        started.set()

    driver.move_to_state = move
    monkeypatch.setattr(module, "_load_thorlabs", lambda: SimpleNamespace(MFF=lambda serial: driver))

    class FastAdapter(module.MFF101Adapter):
        def move_to(self, position, *, stop_event=None):
            return super().move_to(position, timeout_s=.15, stop_event=stop_event)

    h.flipper._worker._factory = FastAdapter
    h.ctrl.acquire_single()
    wait_for(h.app, started.is_set)
    h.ctrl.abort_acquisition()
    wait_until_finished(h)
    assert commands == [1], "Stop must prevent the second physical flip command"
    assert h.captures == []
    assert len(h.completions) == 1
    assert h.errors == []


@pytest.mark.parametrize("mode", ["1d", "2d"])
def test_one_shot_abort_during_route_restores_spectrum_controls(acquisition, monkeypatch, mode):
    from ui.spectrum_panel import SpectrumPanel

    h = acquisition

    def configure(**settings):
        h.usb.read_release.clear()
        return {}

    h.setup.configure_for_acquisition = configure
    monkeypatch.setattr(h.ctrl, "lightfield_optics", lambda requested=None: None)
    monkeypatch.setattr("ui.detector_wavelength_advice.confirm_detector_wavelength", lambda *args: True)
    panel = SpectrumPanel(h.ctrl)
    panel._on_lf6_connected([])
    try:
        button = panel._acquire_btn if mode == "1d" else panel._acquire_2d_btn
        button.click()
        wait_for(h.app, h.route_started.is_set)
        assert panel._abort_btn.isEnabled()
        panel._abort_btn.click()
        h.usb.read_release.set()
        wait_for(h.app, lambda: h.ctrl._acquisition_requests == 0)
        assert h.captures == []
        assert panel._acquire_btn.isEnabled(), "A cancelled one-shot must restore its controls"
        assert not h.ctrl.switching_locked
        assert "spectrum" not in h.ctrl._temperature_pause_sources
        assert h.errors == []
    finally:
        h.usb.read_release.set()
        panel.close()


def test_one_shot_abort_allows_new_request_without_publishing_cancelled_frame(acquisition, monkeypatch):
    from ui.spectrum_panel import SpectrumPanel

    h = acquisition
    capture_started, release = threading.Event(), threading.Event()
    capture_number = 0

    def acquire():
        nonlocal capture_number
        capture_number += 1
        value = 101. if capture_number == 1 else 202.
        if capture_number == 1:
            capture_started.set()
            if not release.wait(2):
                raise TimeoutError("Test capture was not released")
        return np.array([729., 730., 731.]), np.full(3, value)

    h.ctrl._worker._adapter.acquire = acquire
    monkeypatch.setattr(h.ctrl, "lightfield_optics", lambda requested=None: None)
    monkeypatch.setattr("ui.detector_wavelength_advice.confirm_detector_wavelength", lambda *args: True)
    panel = SpectrumPanel(h.ctrl)
    panel._on_lf6_connected([])
    try:
        panel._acquire_btn.click()
        wait_for(h.app, capture_started.is_set)
        panel._abort_btn.click()
        assert panel._acquire_btn.isEnabled()
        # Submit a fresh one-shot before the cancelled detector call returns.
        panel._acquire_btn.click()
        release.set()
        wait_until_finished(h)
        assert capture_number == 2
        assert len(h.spectra) == 1, "The cancelled request's late frame must be discarded"
        np.testing.assert_array_equal(h.spectra[0][1], [202., 202., 202.])
        np.testing.assert_array_equal(panel._last_data, [202., 202., 202.])
        assert h.errors == []
        assert panel._acquire_btn.isEnabled()
    finally:
        release.set()
        panel.close()


@pytest.mark.parametrize("mode", ["1d", "2d", "winspec_readback"])
def test_abort_filters_results_already_queued_for_gui_delivery(acquisition, mode):
    h = acquisition
    worker = h.ctrl._worker
    completed = threading.Event()
    worker.acquisition_finished.connect(lambda *args: completed.set(), Qt.ConnectionType.DirectConnection)
    readbacks = []
    h.ctrl.acquisition_settings_readback.connect(readbacks.append)
    capture_number = 0

    def acquire():
        nonlocal capture_number
        capture_number += 1
        value = 101. if capture_number == 1 else 202.
        if mode == "2d":
            return np.full((2, 3), value)
        return np.arange(3), np.full(3, value)

    worker._adapter.acquire = acquire
    h.setup.acquire_2d = acquire
    if mode == "winspec_readback":
        worker._backend = "winspec_ingaas"
        worker._identity = {"backend": "winspec_ingaas", "camera_role": "ingaas"}
        h.setup.last_calibration_context = None
        h.setup.read_metadata_snapshot = lambda: {"observed": {"last_frame": {
            "raw_accumulated_counts": [101. if capture_number == 1 else 202.] * 3}}}
    request = h.ctrl.acquire_2d if mode == "2d" else h.ctrl.acquire_single
    request()
    # No GUI events are processed between dispatch and Abort. The worker has
    # emitted both its payload and completion; neither has reached the GUI.
    assert completed.wait(2), "Fake acquisition did not finish"
    assert h.ctrl._acquisition_requests == 1
    assert h.spectra == [] and h.frames == [] and readbacks == []
    assert h.ctrl.abort_acquisition()
    request()
    wait_until_finished(h)
    assert capture_number == 2
    result = h.frames if mode == "2d" else [data[1] for data in h.spectra]
    assert len(result) == 1, "Abort must discard a cancelled payload at GUI delivery"
    np.testing.assert_array_equal(result[0], np.full((2, 3) if mode == "2d" else 3, 202.))
    if mode == "winspec_readback":
        assert len(readbacks) == 1, "Cancelled readback must not overwrite new request metadata"
        assert readbacks[0]["winspec_raw_accumulated_counts"] == [202.] * 3
    assert h.errors == []
    assert len(h.completions) == 2


@pytest.mark.parametrize("mode", ["1d", "2d"])
def test_abort_filters_capture_error_already_queued_for_gui_delivery(acquisition, mode):
    h = acquisition
    completed = threading.Event()
    h.ctrl._worker.acquisition_finished.connect(lambda *args: completed.set(), Qt.ConnectionType.DirectConnection)
    capture_number = 0

    def acquire():
        nonlocal capture_number
        capture_number += 1
        if capture_number == 1:
            raise OSError("Cancelled request's delayed detector error")
        return np.ones((2, 3)) if mode == "2d" else (np.arange(3), np.ones(3))

    h.ctrl._worker._adapter.acquire = acquire
    h.setup.acquire_2d = acquire
    request = h.ctrl.acquire_2d if mode == "2d" else h.ctrl.acquire_single
    request()
    assert completed.wait(2), "Fake acquisition did not finish"
    assert h.errors == []
    assert h.ctrl.abort_acquisition()
    request()
    wait_until_finished(h)
    assert h.errors == [], "A cancelled queued error must not clean up the new UI request"
    assert len(h.frames if mode == "2d" else h.spectra) == 1
    assert len(h.completions) == 2


@pytest.mark.parametrize("mode", ["1d", "2d"])
def test_uncancelled_capture_error_is_delivered(acquisition, mode):
    h = acquisition

    def acquire():
        raise OSError("Uncancelled detector error")

    h.ctrl._worker._adapter.acquire = acquire
    h.setup.acquire_2d = acquire
    (h.ctrl.acquire_2d if mode == "2d" else h.ctrl.acquire_single)()
    wait_until_finished(h)
    assert len(h.errors) == 1 and "Uncancelled detector error" in h.errors[0]
    assert len(h.completions) == 1


@pytest.mark.parametrize("action", ["acquire", "run"])
@pytest.mark.parametrize("mode", ["1d", "2d"])
def test_pending_route_stop_with_backend_abort_error_restores_ui(acquisition, monkeypatch, action, mode):
    from ui.spectrum_panel import SpectrumPanel

    h = acquisition

    def configure(**settings):
        h.usb.read_release.clear()
        return {}

    def abort():
        raise OSError("simulated STOP IPC failure")

    h.setup.configure_for_acquisition = configure
    h.ctrl._worker._adapter.abort_acquisition = abort
    monkeypatch.setattr(h.ctrl, "lightfield_optics", lambda requested=None: None)
    monkeypatch.setattr("ui.detector_wavelength_advice.confirm_detector_wavelength", lambda *args: True)
    panel = SpectrumPanel(h.ctrl)
    panel._on_lf6_connected([])
    try:
        button = (getattr(panel, f"_run_{mode}_btn") if action == "run"
                  else panel._acquire_btn if mode == "1d" else panel._acquire_2d_btn)
        button.click()
        wait_for(h.app, h.route_started.is_set)
        (panel._stop_btn if action == "run" else panel._abort_btn).click()
        h.usb.read_release.set()
        wait_for(h.app, lambda: h.ctrl._acquisition_requests == 0)
        assert panel._acquire_btn.isEnabled(), "Accepted request cancellation must release UI ownership"
        assert "spectrum" not in h.ctrl._temperature_pause_sources
        assert not h.ctrl.switching_locked
        assert panel._continuous_mode is None and panel._pending_acquisition is None
        assert h.captures == []
        assert h.errors == []
        assert "STOP IPC failure" in panel._status_lbl.text(), "Retain the unconfirmed hardware Stop diagnostic"
        # A stale Abort diagnostic must not affect a subsequent normal request.
        h.setup.configure_for_acquisition = lambda **settings: {}
        h.ctrl._worker._adapter.abort_acquisition = lambda: False
        panel._acquire_btn.click()
        wait_for(h.app, lambda: len(h.spectra) == 1 and h.ctrl._acquisition_requests == 0)
        assert panel._acquire_btn.isEnabled()
        assert h.errors == []
    finally:
        h.usb.read_release.set()
        panel.close()


def test_spectrum_abort_finishes_automatic_calibration_and_restores_capture_settings(acquisition, monkeypatch):
    from ui.spectrum_panel import SpectrumPanel

    h = acquisition
    h.ctrl._worker._backend = "winspec_ingaas"
    h.ctrl._worker._identity = {"backend": "winspec_ingaas", "camera_role": "ingaas", "axis_unit": "pixel"}

    def configure(**settings):
        h.usb.read_release.clear()
        return {}

    h.setup.configure_for_acquisition = configure
    monkeypatch.setattr(h.ctrl, "lightfield_optics", lambda requested=None: None)
    monkeypatch.setattr("ui.detector_wavelength_advice.confirm_detector_wavelength", lambda *args: True)
    panel = SpectrumPanel(h.ctrl)
    panel._on_lf6_connected([])
    page = panel.create_calibration_page()
    page.open_broad()
    broad = page.broad_dialog
    panel._exposure.setValue(123.)
    panel._accumulations.setValue(3)
    original_nm = panel._use_winspec_nm.isChecked()
    try:
        page.automatic_started.emit()
        broad.confirm.setChecked(True)
        broad.start_automatic_collection(456.)
        wait_for(h.app, h.route_started.is_set)
        assert broad.automatic and broad.sweep_active
        assert panel._calibration_capture_pending
        assert panel._exposure.value() == 456. and panel._accumulations.value() == 1
        panel._abort_btn.click()
        h.usb.read_release.set()
        wait_until_finished(h)
        assert not broad.automatic and not broad.sweep_active and not broad.batch_active
        assert panel._calibration_previous_capture is None
        assert panel._calibration_previous_nm is None
        assert panel._exposure.value() == 123. and panel._accumulations.value() == 3
        assert panel._use_winspec_nm.isChecked() == original_nm
        assert panel._acquire_btn.isEnabled()
        assert h.captures == [] and h.errors == []
    finally:
        h.usb.read_release.set()
        broad.close()
        page.close()
        panel.close()
