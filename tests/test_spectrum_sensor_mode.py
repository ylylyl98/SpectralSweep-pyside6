"""Spectrum's 1D/2D policy with fake hardware and the real worker/UI paths."""
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from controllers.lf6_controller import _LF6Worker, LightFieldLifecycleState
from tests.quick_helpers import quick_application, drain_qt_deletions
from tests.test_spectrum_panel_controls import _FakeSpectrumController
from ui.spectrum_panel import SpectrumPanel


@pytest.fixture
def worker():
    quick_application()
    owner = _LF6Worker()
    owner._state = LightFieldLifecycleState.READY
    state = SimpleNamespace(mode="LineSensor", calls=[], failure=None, drift=False, cancel=None)

    def ensure(intent, *, stop_event=None, apply=True):
        target = "FullSensor" if intent['mode'] == "full_sensor" else "LineSensor"
        state.calls.append(("prepare" if apply else "verify", target))
        if state.failure:
            raise RuntimeError(state.failure)
        changed = state.mode != target
        if changed and not apply:
            raise RuntimeError(f"{target} sensor mode readback mismatch")
        state.mode = target
        if apply and state.cancel is not None:
            state.cancel.set()
        return dict(requested=target, actual=target, changed=changed, fingerprint=target)

    def capture(mode):
        state.calls.append(("capture", mode, state.mode))
        if state.drift:
            state.mode = "CustomRegions"
        return np.zeros((2, 3)) if mode == "2d" else (np.arange(3), np.ones(3))

    owner._setup = SimpleNamespace(prepare_sensor_readout=ensure, acquire_2d=lambda: capture("2d"))
    owner._adapter = SimpleNamespace(acquire=lambda: capture("1d"),
                                     invalidate_wavelengths=lambda: state.calls.append(("invalidate",)))
    errors, results, readbacks = [], [], []
    owner.error.connect(errors.append)
    owner.spectrum_ready.connect(lambda *args: results.append("1d"))
    owner.frame_ready.connect(lambda frame: results.append("2d"))
    owner.acquisition_settings_readback.connect(readbacks.append)
    yield owner, state, errors, results, readbacks
    owner.deleteLater()
    drain_qt_deletions()


def test_lightfield_2d_then_1d_switches_before_capture_and_checks_after(worker):
    owner, state, errors, results, readbacks = worker
    owner.acquire_2d()
    owner.acquire_single()
    assert not errors and results == ["2d", "1d"]
    assert state.calls == [
        ("prepare", "FullSensor"), ("invalidate",), ("capture", "2d", "FullSensor"), ("verify", "FullSensor"),
        ("prepare", "LineSensor"), ("invalidate",), ("capture", "1d", "LineSensor"), ("verify", "LineSensor"),
    ]
    assert [value["lightfield_sensor_mode"]["actual"] for value in readbacks] == ["FullSensor", "LineSensor"]


@pytest.mark.parametrize("mode", ["1d", "2d"])
def test_sensor_error_blocks_capture_and_reports_required_mode(worker, mode):
    owner, state, errors, results, _ = worker
    state.failure = "FullSensor / LineSensor unsupported; check LightField ROI"
    (owner.acquire_single if mode == "1d" else owner.acquire_2d)()
    assert len(errors) == 1 and "check LightField ROI" in errors[0]
    assert not results and not any(call[0] == "capture" for call in state.calls)


@pytest.mark.parametrize("mode", ["1d", "2d"])
def test_sensor_drift_does_not_publish_mislabeled_data(worker, mode):
    owner, state, errors, results, readbacks = worker
    state.drift = True
    (owner.acquire_single if mode == "1d" else owner.acquire_2d)()
    assert len(errors) == 1 and "mismatch" in errors[0]
    assert not results and not readbacks
    assert state.mode == "CustomRegions"  # post-capture verification never repairs


@pytest.mark.parametrize("blocked", ["cancelled", "external_owner"])
def test_queued_request_cannot_change_sensor_when_blocked(worker, blocked):
    owner, state, errors, results, _ = worker
    stop = threading.Event()
    if blocked == "cancelled":
        stop.set()
    else:
        owner.external_measurement_active.set()
    owner.acquire_2d(stop if blocked == "cancelled" else None)
    assert not state.calls and not results
    assert bool(errors) == (blocked == "external_owner")


def test_stop_after_preparation_prevents_capture(worker):
    owner, state, _, results, _ = worker
    state.cancel = stop = threading.Event()
    owner.acquire_2d(stop)
    assert not results and not any(call[0] == "capture" for call in state.calls)


def test_andor_2d_preserves_its_existing_roi_helper(worker):
    owner, state, errors, results, _ = worker
    owner._backend = "andor_si"
    owner._setup.change_roi_FullSensor = lambda: state.calls.append(("andor_roi",))
    owner.acquire_2d()
    assert not errors and results == ["2d"]
    assert state.calls == [("andor_roi",), ("capture", "2d", "LineSensor")]


@pytest.fixture
def panel(monkeypatch):
    quick_application()
    monkeypatch.setattr('ui.detector_wavelength_advice.confirm_detector_wavelength', lambda *args: True)
    controller = _FakeSpectrumController()
    controller.identity = {"backend": "lightfield"}
    widget = SpectrumPanel(controller)
    controller.connected.emit([])
    yield widget, controller
    widget.close()
    widget.deleteLater()
    drain_qt_deletions()


@pytest.mark.parametrize("action,expected", [("1d", "LineSensor"), ("2d", "FullSensor"),
                                           ("run_1d", "LineSensor"), ("run_2d", "FullSensor")])
def test_acquire_and_run_explain_the_sensor_policy_and_keep_verified_provenance(panel, action, expected):
    widget, controller = panel
    assert not widget._sensor_mode_hint.isHidden()
    assert "1D → Bin All" in widget._sensor_mode_hint.text()
    assert "2D → Full sensor" in widget._sensor_mode_hint.text()
    widget._apply_settings_then(action)
    assert ("Full sensor" if expected == "FullSensor" else "Bin All") in widget._sensor_mode_hint.text()
    controller.settings_applied.emit()
    widget._optics_readback.setText("Existing optics readback")
    sensor = dict(requested=expected, actual=expected, changed=True)
    widget._on_acquisition_readback({"lightfield_sensor_mode": sensor})
    assert widget._optics_readback.text() == "Existing optics readback"
    assert f"switched to {expected}" in widget._sensor_mode_hint.text()
    assert widget._pending_acquisition_snapshot["lightfield_sensor_mode"] == sensor
    assert widget._acquisition_settings_snapshot["lightfield_sensor_mode"] == sensor
    if action.startswith("run"):
        widget._acquire_next_continuous()
        assert len(controller.apply_calls) == 1  # no repeated UI preparation/dialog per frame
    widget._continuous_mode = None
    controller.disconnected.emit()
    assert widget._sensor_mode_hint.isHidden() and not widget._sensor_mode_hint.text()


def test_sensor_failure_exposes_actionable_error_and_stops_run(panel):
    widget, controller = panel
    widget._start_continuous("2d")
    controller.error.emit("FullSensor cannot be selected; check LightField ROI")
    assert widget._continuous_mode is None and widget._pending_acquisition is None
    assert not widget._error_panel.isHidden()
    assert "check LightField ROI" in widget._error_details.toPlainText()
    assert "not verified" in widget._sensor_mode_hint.text()


def test_other_detectors_do_not_claim_lightfield_sensor_policy(panel):
    widget, controller = panel
    controller.identity = {"backend": "andor_sdk2", "camera_role": "si"}
    controller.connected.emit([])
    assert widget._sensor_mode_hint.isHidden()
