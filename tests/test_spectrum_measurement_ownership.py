"""Real controller/panel boundaries with a simulated XP camera; no hardware."""
import time

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from tests.test_winspec_scans import scan
from ui.spectrum_panel import SpectrumPanel


def drain_until(predicate):
    deadline = time.monotonic() + 2.
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(.005)
    assert predicate(), 'Controller did not finish the request'


@pytest.mark.parametrize('owner', ['presets', 'megasweep'])
def test_spectrum_controls_follow_external_owner_without_claiming_ownership(scan, owner):
    controller, setup, camera, live = scan
    panel = SpectrumPanel(controller)
    try:
        panel._on_lf6_connected([])
        controller.set_temperature_monitor_paused(owner, True)
        for button in (panel._apply_btn, panel._acquire_btn, panel._run_1d_btn):
            assert not button.isEnabled()
        assert 'spectrum' not in controller._temperature_pause_sources
        # Completion/error callbacks must not accidentally unlock another scan.
        panel._finish_one_shot()
        assert not panel._apply_btn.isEnabled()
        controller.set_temperature_monitor_paused(owner, False)
        assert panel._apply_btn.isEnabled()
        assert panel._acquire_btn.isEnabled()
        assert panel._run_1d_btn.isEnabled()
    finally:
        panel.close()


@pytest.mark.parametrize('entry', ['public', 'queued_worker'])
def test_external_owner_blocks_spectrum_requests_and_preserves_scan_recipe(scan, entry):
    controller, setup, camera, live = scan
    controller.configure_for_acquisition(center_nm=1050, exposure_ms=500, frames=2)
    controller.set_temperature_monitor_paused('presets', True)
    errors, spectra = [], []
    controller.error.connect(errors.append)
    controller.spectrum_ready.connect(lambda *data: spectra.append(data))
    camera.calls.clear()
    target = controller if entry == 'public' else controller._worker
    # Worker entry models a request queued before measurement ownership changed.
    target.apply_settings(800, 1050, 4)
    target.acquire_single()
    target.acquire_2d()
    drain_until(lambda: len(errors) >= 3)
    assert not camera.calls
    assert not spectra
    assert camera.settings['exposure_ms'] == 500
    assert camera.settings['accumulations'] == 2
    assert controller._acquisition_requests == 0
    # Scan-owned preparation and acquisition continue to work under the lock.
    controller.configure_for_acquisition(center_nm=1050, exposure_ms=250, frames=3)
    _, counts = controller.adapter.acquire()
    np.testing.assert_allclose(counts, np.arange(512)[19:490] / 3)


def test_spectrum_can_apply_and_acquire_under_its_own_owner(scan):
    controller, setup, camera, live = scan
    controller.set_temperature_monitor_paused('spectrum', True)
    applied, spectra = [], []
    controller.settings_applied.connect(lambda: applied.append(True))
    controller.spectrum_ready.connect(lambda *data: spectra.append(data))
    controller.apply_settings(250, 1050, 3)
    drain_until(lambda: bool(applied))
    controller.acquire_single()
    drain_until(lambda: bool(spectra) and controller._acquisition_requests == 0)
    np.testing.assert_allclose(spectra[0][1], np.arange(512) / 3)
