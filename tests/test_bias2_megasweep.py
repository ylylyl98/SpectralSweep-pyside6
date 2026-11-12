import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication
from ui.megasweep_panel import CoordSystem, MegaSweepPanel, build_sweep_points, _MegaSweepWorker
from utils.config import cfg
from tests.test_megasweep_sequence import _params, _FakeIV, _FakeLF6Controller


SAFETY = {"vtg_min": -10, "vtg_max": 10, "vbg_min": -10, "vbg_max": 10,
          "vbias_min": -1, "vbias_max": 1, "vbias2_min": -1, "vbias2_max": 1}
FOLLOW = {"bias2_mode": "follow", "bias2_offset_v": .02}


def test_doping_bias_map_follows_offset_in_both_snake_directions():
    all_points, points = build_sweep_points(CoordSystem.PHYSICAL, "Doping", np.array([-2., 2.]),
        "Vbias", np.array([-.1, 0, .1]), {"E-field": 0.}, 2., SAFETY, True, bias2=FOLLOW)
    assert len(points) == len(all_points) == 6
    np.testing.assert_allclose([p["raw"] for p in points], [
        (-1, -.5, -.1), (-1, -.5, 0), (-1, -.5, .1),
        (1, .5, .1), (1, .5, 0), (1, .5, -.1)])
    np.testing.assert_allclose([p["Vbias2_set"] for p in points], [-.08, .02, .12, .12, .02, -.08])


def test_independent_second_axis_and_derived_range_checks():
    _, points = build_sweep_points(CoordSystem.RAW, "Vtg", np.array([0., 1.]),
        "Vbias2", np.array([-.1, .1]), {"Vbg": 0, "Vbias": .3}, 1, SAFETY, False,
        bias2={"bias2_mode": "independent"})
    assert [p["Vbias2_set"] for p in points] == [-.1, .1, -.1, .1]
    assert all(p["raw"][2] == .3 for p in points)
    planned, valid = build_sweep_points(CoordSystem.PHYSICAL, "Doping", np.array([0]),
        "Vbias", np.array([0., .99]), {"E-field": 0}, 1, SAFETY, False, bias2=FOLLOW)
    assert len(planned) == 2 and len(valid) == 1
    assert planned[-1]["bias2_in_bounds"] is False


def test_absent_source_hidden_and_follow_recipe_survives_disconnect():
    app = QApplication.instance() or QApplication([])
    with patch.object(cfg.smu, "vbias2_resource", ""):
        panel = MegaSweepPanel()
        assert panel._bias2_control.isHidden()
        assert "Vbias2" not in panel._current_axis_pool()
        panel._bias2_control.set_settings(FOLLOW)
        panel._coord_widget._physical.setChecked(True)
        panel._axis_selector._outer.setCurrentText("Doping")
        panel._axis_selector._inner.setCurrentText("Vbias")
        state = panel.capture_session_state()
        panel.restore_session_state(state)
        panel._sync_vbias_availability()
        assert not panel._bias2_control.isHidden()
        assert panel._axis_selector.inner() == "Vbias"
        assert panel._bias2_control.settings()["bias2_offset_v"] == .02
        panel._refresh_preview()
        with patch("ui.megasweep_panel.QMessageBox.critical") as notice:
            assert not panel._validate(panel._collect_params())
            assert "Vbias2" in notice.call_args.args[2]
        panel.close()


class _PairedIV(_FakeIV):
    def __init__(self):
        super().__init__()
        self.values = {"Vbias": 0., "Vbias2": 0.}
        self.writes = []

    def role_is_available(self, role):
        return role in ("Vbg", "Vtg", "Vbias", "Vbias2")

    def read_role_voltage(self, role, strict=False):
        return self.values[role]

    def set_role_voltage_fast(self, role, value, delay):
        self.values[role] = value
        self.writes.append((role, value))
        return True

    def read_current_bias(self):
        return self.values["Vbias"]

    def read_current_bias2(self, strict=False):
        return self.values["Vbias2"]

    def read_bias2_current(self, strict=False):
        return 4e-9

    def ramp_all_to_zero(self, **kwargs):
        assert kwargs.get("include_bias2")
        self.values = {"Vbias": 0., "Vbias2": 0.}
        super().ramp_all_to_zero(**kwargs)


def test_worker_writes_actual_follower_values_and_scoped_cleanup(tmp_path):
    QApplication.instance() or QApplication([])
    params = _params(tmp_path)
    params.update(bias2=FOLLOW, safety=SAFETY)
    params["all_points"], params["valid_points"] = build_sweep_points(
        CoordSystem.PHYSICAL, "Doping", np.array([0]), "Vbias", np.array([-.1, .1]),
        {"E-field": 0}, 1, SAFETY, False, bias2=FOLLOW)
    params["axis_a"] = "Doping"
    params["axis_b"] = "Vbias"
    params["optical_conditions"] = params["optical_conditions"][:1]
    device = _PairedIV()
    controller = SimpleNamespace(is_connected=True, device=device)
    worker = _MegaSweepWorker(params, controller, _FakeLF6Controller())
    with patch("ui.megasweep_panel.time.sleep"), patch("utils.bias_control.time.sleep"):
        worker._run_sweep(params)
    csv = next(tmp_path.glob("*.csv"))
    data = np.genfromtxt(csv, delimiter=",", names=True)
    np.testing.assert_allclose(data["Vbias2_set"], [-.08, .12])
    np.testing.assert_allclose(data["Vbias2_meas"], [-.08, .12])
    np.testing.assert_allclose(data["Ibias2_A"], [4e-9, 4e-9])
    assert device.zero_ramps == 1
