"""Live references use completed frames without following the hardware lock."""
import numpy as np
import pytest
from PySide6.QtCore import QEvent, QObject, Signal
from PySide6.QtWidgets import QApplication

from tests.test_spectrum_panel_controls import _FakeSpectrumController
from ui.spectrum_panel import SpectrumPanel
from utils.config import AppConfig, cfg


class _LockingController(_FakeSpectrumController):
    switching_lock_changed = Signal(bool)
    spectrum_actions_blocked = False

    def set_capture_locked(self, locked):
        self.spectrum_actions_blocked = locked
        self.switching_lock_changed.emit(locked)


class _EnabledChanges(QObject):
    def __init__(self, button):
        super().__init__(button)
        self.states = []
        button.installEventFilter(self)

    def eventFilter(self, button, event):
        if event.type() == QEvent.Type.EnabledChange:
            self.states.append(button.isEnabled())
        return False


@pytest.fixture
def live_panel(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(cfg, "lf6", AppConfig().lf6)
    monkeypatch.setattr(cfg.filename, "base_out", str(tmp_path))
    monkeypatch.setattr("ui.detector_wavelength_advice.confirm_detector_wavelength", lambda *args: True)
    controller = _LockingController()
    panel = SpectrumPanel(controller)
    controller.connected.emit([])
    yield panel, controller
    panel._stop_continuous()
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_add_stays_enabled_between_live_frames_and_freezes_latest_result(live_panel):
    panel, controller = live_panel
    button = panel._add_spectrum_btn
    # A previous run's displayed data must not enable Add before the first frame.
    panel.push_spectrum(np.array([700., 701.]), np.array([1., 2.]))
    panel._run_1d_btn.click()
    controller.settings_applied.emit()
    controller.set_capture_locked(True)
    assert not button.isEnabled()
    button.click()
    assert not panel.references

    controller.spectrum_ready.emit(np.array([700., 701.]), np.array([3., 4.]))
    assert button.isEnabled()
    changes = _EnabledChanges(button)
    for counts in ([5., 6.], [7., 8.]):
        controller.set_capture_locked(False)
        QApplication.processEvents()  # Dispatch the queued next acquisition.
        controller.set_capture_locked(True)
        controller.set_capture_locked(True)  # Repeated capture-status poll.
        controller.spectrum_ready.emit(np.array([700., 701.]), np.array(counts))

    controller.set_capture_locked(True)
    assert changes.states == [], "A completed live frame must stay available during capture"
    assert button.isEnabled()
    assert not panel._acquire_btn.isEnabled()
    assert not panel._apply_btn.isEnabled()
    calls = controller.acquire_1d_calls
    button.click()
    assert controller.acquire_1d_calls == calls
    assert len(controller.apply_calls) == 1
    assert len(panel.references) == 1
    reference = panel.references[0]
    assert reference["saved"]
    np.testing.assert_array_equal(reference["counts"], [7., 8.])
    controller.spectrum_ready.emit(np.array([700., 701.]), np.array([9., 10.]))
    np.testing.assert_array_equal(reference["counts"], [7., 8.])
    assert changes.states == []


@pytest.mark.parametrize("finish", ["stop", "abort", "error", "disconnect"])
def test_live_reference_exception_ends_with_run(live_panel, finish):
    panel, controller = live_panel
    panel._run_1d_btn.click()
    controller.settings_applied.emit()
    controller.spectrum_ready.emit(np.array([700., 701.]), np.array([3., 4.]))
    controller.set_capture_locked(True)
    if finish == "stop":
        panel._stop_btn.click()
    elif finish == "abort":
        panel._abort_btn.click()
    elif finish == "error":
        controller.error.emit("Capture failed")
    else:
        controller.disconnected.emit()
    assert panel._continuous_mode is None
    assert not panel._add_spectrum_btn.isEnabled()
    controller.set_capture_locked(False)
    assert panel._add_spectrum_btn.isEnabled() == (finish != "disconnect")


def test_2d_run_does_not_offer_a_1d_reference(live_panel):
    panel, controller = live_panel
    panel._run_2d_btn.click()
    controller.settings_applied.emit()
    controller.frame_ready.emit(np.zeros((2, 4)))
    for locked in (True, False, True):
        controller.set_capture_locked(locked)
        assert not panel._add_spectrum_btn.isEnabled()
    assert not panel.references
