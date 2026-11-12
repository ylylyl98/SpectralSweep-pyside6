"""Integration coverage for second-bias condition expansion and gate services."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from utils.mcd_common import build_condition_batch, expand_bias2_conditions, resolve_gate_conditions
from utils.motion_conditions import expand_sequence
from ui.mcd2100_panel import _LightFieldRotationService
from ui.mcd_panel import _ContinuousMCDWorker


class Device:
    def __init__(self, roles=("Vbg", "Vtg", "Vbias", "Vbias2")):
        self.roles = roles
        self.values = {"Vbias": 0.3, "Vbias2": 0.5}
        self.calls = []
    def role_is_available(self, role): return role in self.roles
    def set_gates(self, **kwargs): self.calls.append(("gates", kwargs))
    def read_current_gates(self): return (0.0, 0.0)
    def read_current_bias(self): return self.values["Vbias"]
    def read_current_bias2(self, *, strict=False): return self.values["Vbias2"]
    def read_bias2_current(self, *, strict=False): return 1e-9
    def read_currents(self): return (0.0, 0.0, 0.0)
    def read_role_voltage(self, role, *, strict=False): return self.values[role]
    def set_role_voltage_fast(self, role, value, delay):
        self.calls.append((role, value)); self.values[role] = value; return True
    def ramp_to(self, role, value, step, delay, **kwargs):
        return self.set_role_voltage_fast(role, value, delay)
    def set_bias(self, Vbias, **kwargs): self.calls.append(("Vbias", Vbias)); self.values["Vbias"] = Vbias


class Controller:
    is_connected = True
    def __init__(self, roles=("Vbg", "Vtg", "Vbias", "Vbias2")):
        self.device = Device(roles)
        self.targets = []
    def limits_are_applied_for_roles(self, roles): return True
    def validate_voltage_targets(self, targets): self.targets.append(dict(targets))


def condition(mode="follow", offset=0.2):
    return {"mode": "direct", "input_a": 0, "input_b": 0, "vbias_v": 0.0,
            "bias2_mode": mode, "bias2_offset_v": offset, "vbias2_v": 0.0}


def test_expansion_and_motion_sequence_preserve_second_bias_fields():
    base = build_condition_batch("direct", "0,1", "0", "grid", 1, vbias_v=0, voltage_limit=20)
    expanded = expand_bias2_conditions(base, {"bias2_mode": "independent", "vbias2_v": 0, "bias2_offset_v": 0}, "-0.1,0.2", 1)
    restored = resolve_gate_conditions(expanded, 1)
    assert [row["vbias2_target_v"] for row in restored] == pytest.approx([-0.1, 0.2, -0.1, 0.2])
    sequence = expand_sequence(restored, [0.0])
    assert [restored[item["condition_index"]]["vbias2_target_v"] for item in sequence] == pytest.approx([-0.1, 0.2, -0.1, 0.2])


def test_mcd2100_follow_zero_executes_and_records_target(monkeypatch):
    from utils.config import cfg
    monkeypatch.setattr(cfg.ramp, "delay_s", 0)
    row = resolve_gate_conditions([condition()], 1)[0]
    smu = Controller()
    service = _LightFieldRotationService(None, None, smu_controller=smu)
    result = service.apply_gates(vtg_v=0, vbg_v=0, vbias_v=0, ratio=1, bias2_settings=row)
    assert smu.targets == [{"Vbias2": pytest.approx(0.2), "Vbias": 0.0}]
    assert smu.device.values == pytest.approx({"Vbias": 0.0, "Vbias2": 0.2})
    assert result["Vbias2_target_V"] == pytest.approx(0.2)
    assert result["Vbias2_V"] == pytest.approx(0.2)
    assert result["Ibias2_A"] == pytest.approx(1e-9)


def test_mcd2100_missing_follower_fails_before_any_write():
    smu = Controller(("Vbg", "Vtg", "Vbias"))
    service = _LightFieldRotationService(None, None, smu_controller=smu)
    row = resolve_gate_conditions([condition()], 1)[0]
    with pytest.raises(RuntimeError, match="Vbias2"):
        service.apply_gates(vtg_v=0, vbg_v=0, vbias_v=0, ratio=1, bias2_settings=row)
    assert smu.device.calls == []


def test_mcd1000_follow_zero_applies_and_keeps_metadata(monkeypatch):
    from utils.config import cfg
    monkeypatch.setattr(cfg.ramp, "vbias2_step_V", 0.1)
    smu = Controller()
    worker = _ContinuousMCDWorker({}, None, None, None, smu)
    row = resolve_gate_conditions([condition()], 1)[0]
    row.update(apply_voltages=True, voltage_ramp_step_v=0.1, voltage_vbias_step_v=0.1,
               voltage_step_delay_s=0, voltage_settle_s=0)
    info = worker._apply_voltages(row)
    assert smu.device.values == pytest.approx({"Vbias": 0, "Vbias2": 0.2})
    assert info["bias2_target_v"] == pytest.approx(0.2)
    assert info["readback"]["Vbias2_V"] == pytest.approx(0.2)


def test_motion_follow_execution_and_active_cleanup(monkeypatch):
    from ui.power_sweep_panel import _PowerSweepWorker
    from utils.config import cfg
    monkeypatch.setattr(cfg.ramp, "vbias2_step_V", 0.1)
    smu = Controller()
    smu.device.ramp_all_to_zero = lambda **kwargs: smu.device.calls.append(("cleanup", kwargs)) or []
    worker = _PowerSweepWorker({}, None, None, None, None, smu)
    worker._sequence_cleanup_errors = []
    row = resolve_gate_conditions([condition()], 1)[0]
    identity = worker._sequence_apply_gate({"ramp_step_V": 0.1, "step_delay_s": 0, "settle_s": 0}, (0, 0, 0), None, row)
    assert smu.device.values == pytest.approx({"Vbias": 0, "Vbias2": 0.2})
    before = len(smu.device.calls)
    assert worker._sequence_apply_gate({}, (0, 0, 0), identity, row) == identity
    assert len(smu.device.calls) == before
    worker._sequence_return_gates_zero({"bias2_active": True, "step_delay_s": 0})
    assert smu.device.calls[-1][1]["include_bias2"] is True


def test_motion_missing_follow_device_rejected_before_writes():
    from types import SimpleNamespace
    from ui.power_sweep_panel import _PowerSweepWorker
    smu = Controller(("Vbg", "Vtg", "Vbias"))
    lf = SimpleNamespace(is_connected=True, adapter=object())
    worker = _PowerSweepWorker({}, None, None, None, lf, smu)
    p = {"positions": [0], "apply_gates": True}
    with pytest.raises(RuntimeError, match="Vbias2"):
        worker._sequence_preflight(p, [{"condition_index": 0}], [resolve_gate_conditions([condition()], 1)[0]])
    assert smu.device.calls == []
