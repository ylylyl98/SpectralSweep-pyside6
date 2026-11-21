from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
import gc
import threading
import time

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from controllers.lf6_controller import LF6Controller, LightFieldLifecycleState
from tests.quick_helpers import drain_qt_deletions, quick_application
from utils.config import cfg


RECIPES = {
    "lightfield": {"center_nm": 650, "exposure_ms": 20, "frames": 2},
    "andor_si": {"center_nm": 720, "exposure_ms": 50, "frames": 3},
}
CENTERS = {"lightfield": 650., "andor_si": 720.}


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)
    assert predicate()


@pytest.fixture
def controller(monkeypatch):
    quick_application()
    # Collect wrappers from the preceding Quick page tests on this thread.
    # Letting a detector worker's allocations trigger their cleanup can
    # destroy Qt-owned objects from the wrong thread.
    gc.collect()
    drain_qt_deletions()
    monkeypatch.setattr(cfg.lf6, "station_id", "attodry2100")
    monkeypatch.setattr(cfg.lf6, "backend", "lightfield")
    ctrl = LF6Controller()
    yield ctrl
    wait_for(lambda: not ctrl.acquisition_in_progress)
    ctrl.shutdown()
    ctrl.deleteLater()
    drain_qt_deletions()


def test_static_paths_connect_and_switch_without_automatic_routing(controller, monkeypatch):
    route = Mock()
    controller._worker.flipper = route
    for backend in ("lightfield", "andor_si", "lightfield"):
        controller.connect_instrument(use_mock=True, backend=backend)
        wait_for(lambda: controller.is_connected and controller.backend == backend)
    controller.ensure_light_path()
    assert controller.select_setup_path("andor_si")
    assert not route.mock_calls
    assert set(controller.connected_backends) == {"lightfield", "andor_si"}
    # Pairing from logical WinSpec would leave its selected output stale,
    # because PIXIS shares that spectrograph's output mirror.
    with monkeypatch.context() as override:
        override.setattr(controller._worker, "_backend", "winspec_ingaas")
        with pytest.raises(RuntimeError, match="activate PIXIS or Andor"):
            controller.validate_station_pair("andor_si", RECIPES)
    assert not route.mock_calls
    saved = Mock()
    monkeypatch.setattr(cfg, "save", saved)
    changes = []
    controller.station_changed.connect(changes.append)
    assert controller.set_station("default")
    assert changes == ["default"]
    saved.assert_called_once_with()
    controller.ensure_light_path()
    route.ensure_setup_position.assert_called_once()


@pytest.mark.parametrize("second_fails", [False, True])
def test_apply_centers_only_uses_mock_centers_and_blocks_other_requests(controller, monkeypatch, second_fails):
    flipper = Mock()
    controller._worker.flipper = flipper
    for backend in CENTERS:
        controller.connect_instrument(use_mock=True, backend=backend)
        wait_for(lambda: controller.is_connected and controller.backend == backend)
    lightfield, _, _ = controller._worker._session("lightfield")
    andor, _, _ = controller._worker._session("andor_si")
    initial = [(setup._exposure_ms, setup._accumulations, setup._roi) for setup in (lightfield, andor)]
    original = lightfield.apply_center_wavelength
    entered, release = threading.Event(), threading.Event()

    def apply_lf(center, *, stop_event):
        entered.set()
        assert release.wait(5)
        return original(center, stop_event=stop_event)

    monkeypatch.setattr(lightfield, "apply_center_wavelength", apply_lf)
    if second_fails:
        monkeypatch.setattr(andor, "apply_center_wavelength", Mock(side_effect=RuntimeError("Andor motion failed")))
    never = Mock(side_effect=AssertionError("center only must never configure or expose"))
    for setup in (lightfield, andor):
        monkeypatch.setattr(setup, "configure_for_acquisition", never)
        monkeypatch.setattr(setup, "acquire", never)
    with ThreadPoolExecutor(2) as pool:
        controller.set_temperature_monitor_paused("spectrum", True)
        active = pool.submit(controller.apply_station_pair_centers, "andor_si", CENTERS, threading.Event())
        wait_for(active.done)
        with pytest.raises(RuntimeError, match="current spectrometer operation"):
            active.result()
        controller.set_temperature_monitor_paused("spectrum", False)
        change = pool.submit(controller.apply_station_pair_centers, "andor_si", CENTERS, threading.Event())
        try:
            wait_for(entered.is_set)
            assert controller.switching_locked and controller.acquisition_in_progress
            capture = pool.submit(controller.acquire_station_pair, "andor_si", RECIPES, threading.Event())
            wait_for(capture.done)
            with pytest.raises(RuntimeError, match="current spectrometer operation"):
                capture.result()
        finally:
            release.set()
        wait_for(change.done)
        result = change.result()
    assert result["lightfield"] == {"center_nm": 650.}
    assert result["andor_si"] == ({"error": "Andor motion failed"} if second_fails else {"center_nm": 720.})
    assert [(setup._exposure_ms, setup._accumulations, setup._roi) for setup in (lightfield, andor)] == initial
    never.assert_not_called()
    flipper.assert_not_called()
    assert not flipper.mock_calls


@pytest.mark.parametrize("scenario", ["complete", "partial", "stop", "unready"])
def test_pair_uses_independent_owners_and_drains_before_return(controller, scenario):
    capture = {name: threading.Event() for name in RECIPES}
    release = threading.Event()
    threads = {}
    main_thread = threading.get_ident()
    master = controller._worker
    flipper = Mock()
    master.flipper = flipper

    def verify_settings(name, **settings):
        threads[name] = threading.get_ident()
        assert settings == RECIPES[name]
        if scenario == "partial" and name == "andor_si":
            raise RuntimeError("Andor preparation failed")
        return settings

    def acquire(name):
        assert threading.get_ident() == threads[name]
        if name == "andor_si":
            assert capture["lightfield"].wait(3)
        capture[name].set()
        if name == "lightfield":
            assert release.wait(4)
        axis = np.arange(3, dtype=float) + RECIPES[name]["center_nm"]
        return axis, axis * 2

    def session(name, **setup_options):
        setup = SimpleNamespace(is_ready=scenario != "unready" or name != "andor_si", **setup_options)
        adapter = SimpleNamespace(
            configure_for_acquisition=lambda **values: verify_settings(name, **values),
            acquire=lambda: acquire(name),
        )
        return setup, adapter, {"backend": name}, []

    with ThreadPoolExecutor(1) as requester, ThreadPoolExecutor(1) as andor:
        andor_owner = andor.submit(threading.get_ident).result()
        sensor = Mock(return_value={"fingerprint": "verified"})
        master._setup, master._adapter, master._identity, master._experiments = session(
            "lightfield", prepare_sensor_readout=sensor)
        master._state = LightFieldLifecycleState.READY
        master._parked["andor_si"] = session("andor_si", run_paired_operation=lambda fn: andor.submit(fn).result())
        stop = threading.Event()
        controller.set_temperature_monitor_paused("presets", True)
        future = requester.submit(controller.acquire_station_pair, "andor_si", RECIPES, stop, owner="presets")
        if scenario == "unready":
            wait_for(future.done)
            results = future.result()
            assert "requires andor_si to be ready" in results["lightfield"]["error"]
            assert "not ready" in results["andor_si"]["error"]
            assert not threads and not any(event.is_set() for event in capture.values())
            sensor.assert_not_called()
            wait_for(lambda: not controller.acquisition_in_progress)
            controller.set_temperature_monitor_paused("presets", False)
            return
        try:
            wait_for(capture["lightfield"].is_set)
            assert controller.switching_locked and controller.spectrum_actions_blocked
            assert controller.acquisition_in_progress
            with pytest.raises(RuntimeError, match="paired spectrum"):
                controller.configure_for_acquisition(center_nm=600, exposure_ms=10, frames=1)
            if scenario != "partial":
                wait_for(capture["andor_si"].is_set)
            if scenario == "stop":
                assert controller.abort_acquisition()
                assert stop.is_set() and not future.done() and controller.acquisition_in_progress
        finally:
            release.set()
        wait_for(future.done)
        results = future.result()
        assert results["lightfield"]["settings"] == RECIPES["lightfield"]
        assert threads["lightfield"] != main_thread
        assert threads["andor_si"] == andor_owner
        assert andor_owner not in {main_thread, threads["lightfield"]}
        assert flipper.mock_calls == []
        sensor.assert_called_once()
        for result in results.values():
            assert datetime.fromisoformat(result["finished_at"]).utcoffset().total_seconds() == 0
        if scenario == "partial":
            assert results["andor_si"]["started_at"] is None
            assert results["andor_si"]["error"] == "Andor preparation failed"
        # A SDK call that finishes with a complete frame during Stop is still
        # persisted. Cancellation prevents subsequent acquisitions.
        np.testing.assert_allclose(results["lightfield"]["wavelengths"], [650, 651, 652])
        wait_for(lambda: not controller.acquisition_in_progress)
        assert controller._temperature_pause_sources == {"presets"}
        controller.set_temperature_monitor_paused("presets", False)
