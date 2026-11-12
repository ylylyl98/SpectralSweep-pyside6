import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from ui.instrument_panel import InstrumentPanel, _record_spectrum_readback
from utils.config import cfg
from tests.test_keithley_controls import _FakeSMUController


def test_partial_readbacks_retain_each_bias_with_its_own_timestamp():
    owner = SimpleNamespace()
    _record_spectrum_readback(owner, "smu", {"Vbias2_meas": .12, "Ibias2": 3e-9})
    timestamp = owner._spectrum_readbacks["smu"]["field_timestamps_utc"]["Vbias2_meas"]
    _record_spectrum_readback(owner, "smu", {"Vbg_meas": 2.})
    cached = owner._spectrum_readbacks["smu"]
    assert cached["value"] == {"Vbias2_meas": .12, "Ibias2": 3e-9, "Vbg_meas": 2.}
    assert cached["field_timestamps_utc"]["Vbias2_meas"] == timestamp


def test_legacy_manual_session_keeps_follow_semantics_without_hardware_io():
    app = QApplication.instance() or QApplication([])
    ctrl = _FakeSMUController()
    with patch.object(cfg.smu, "vbias2_resource", ""):
        panel = InstrumentPanel(smu_ctrl=ctrl)
        manual = panel._sections["manual_smu"]
        assert manual._bias2_link.isHidden()
        panel.restore_session_state({"manual_smu": {"step_v": .1}})
        manual._bias2_link.enable.setChecked(True)
        manual._bias2_link.offset.setValue(.02)
        manual._target_spn["Vbias"].setValue(.1)
        assert manual._bias2_link.settings()["bias2_mode"] == "follow"
        assert abs(manual._target_spn["Vbias2"].value() - .12) < 1e-9
        assert not manual._target_spn["Vbias2"].isEnabled()
        manual._on_controller_readings_ready({"Vbias2_meas": .12})
        snapshot = panel.capture_spectrum_readbacks()
        assert snapshot["instruments"]["smu"]["readbacks"]["smu"]["value"]["Vbias2_meas"] == .12
        manual._on_smu_disconnected()
        assert manual._spectrum_readbacks == {}
        panel.close()
