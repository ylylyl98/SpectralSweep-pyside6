import pytest
from utils.bias_control import normalize_bias2, resolve_bias2, apply_bias_pair


class Device:
    def __init__(self, roles=("Vbias", "Vbias2")):
        self.roles = roles
        self.role_map = {role: role for role in roles}
        self.values = {"Vbias": 0.0, "Vbias2": 0.2}
        self.writes = []
    def role_is_available(self, role):
        return role in self.roles
    def read_role_voltage(self, role, *, strict=False):
        assert strict
        return self.values[role]
    def set_role_voltage_fast(self, role, value, delay):
        self.values[role] = value
        self.writes.append((role, value))
        return True
    def ramp_to(self, role, value, step, delay, **kwargs):
        return self.set_role_voltage_fast(role, value, delay)


def test_follow_interleaves_steps_and_preserves_offset_at_completed_pairs():
    device = Device()
    apply_bias_pair(device, vbias=0.3, settings={"bias2_mode": "follow", "bias2_offset_v": 0.2}, ramp_step=0.1, delay_s=0)
    assert len(device.writes) == 6
    for master, follower in zip(device.writes[::2], device.writes[1::2]):
        assert (master[0], follower[0]) == ("Vbias", "Vbias2")
        assert follower[1] - master[1] == pytest.approx(0.2)
    assert device.values == pytest.approx({"Vbias": 0.3, "Vbias2": 0.5})


def test_missing_follower_aborts_before_any_writes():
    device = Device(("Vbias",))
    with pytest.raises(RuntimeError, match="Vbias2"):
        apply_bias_pair(device, vbias=1, settings={"bias2_mode": "follow"}, ramp_step=0.1, delay_s=0)
    assert not device.writes


def test_disabled_legacy_and_explicit_zero_are_distinct():
    assert normalize_bias2({})["mode"] == "off"
    assert resolve_bias2({}, 1) is None
    device = Device()
    apply_bias_pair(device, vbias=None, settings={"bias2_mode": "independent", "vbias2_v": 0}, ramp_step=0.1, delay_s=0)
    assert device.writes == [("Vbias2", 0.0)]
    with pytest.raises(ValueError, match="requires"):
        resolve_bias2({"bias2_mode": "follow"}, None)


def test_gate_mode_does_not_enable_second_bias():
    assert normalize_bias2({"mode": "direct"})["mode"] == "off"


def test_manual_worker_follow_step_and_zero():
    from controllers.smu_controller import _SMUWorker
    device = Device()
    worker = _SMUWorker()
    worker._device = device
    worker._applied_voltage_ranges = {role: 20.0 for role in device.roles}
    errors = []
    worker.manual_error.connect(errors.append)
    # Refresh is not needed to verify the control operation.
    worker._emit_role_reading = lambda *args, **kwargs: None
    settings = {"bias2_mode": "follow", "bias2_offset_v": 0.2}
    worker.manual_control("step", "Vbias", 0.2, 0.1, 0, settings)
    assert not errors
    assert device.values == pytest.approx({"Vbias": 0.2, "Vbias2": 0.4})
    worker.manual_control("zero", "Vbias", 0, 0.1, 0, settings)
    assert not errors
    assert device.values == pytest.approx({"Vbias": 0, "Vbias2": 0.2})


def test_manual_worker_follow_preflight_stops_first_source():
    from controllers.smu_controller import _SMUWorker
    device = Device(("Vbias",))
    worker = _SMUWorker()
    worker._device = device
    worker._applied_voltage_ranges = {role: 20.0 for role in device.roles}
    errors = []
    worker.manual_error.connect(errors.append)
    worker.manual_control("set_fast", "Vbias", 1, 0.1, 0, {"bias2_mode": "follow"})
    assert errors and "Vbias2" in errors[0]
    assert not device.writes


def test_manual_follow_rejects_derived_target_before_writes():
    from controllers.smu_controller import _SMUWorker
    device = Device()
    worker = _SMUWorker()
    worker._device = device
    worker._applied_voltage_ranges = {"Vbias": 10.0, "Vbias2": 1.0}
    errors = []
    worker.manual_error.connect(errors.append)
    worker.manual_control("step", "Vbias", 1.0, 0.1, 0,
                          {"bias2_mode": "follow", "bias2_offset_v": 0.2})
    assert errors and "Vbias2" in errors[0]
    assert not device.writes


def test_cleanup_bias2_is_opt_in_and_includes_failures():
    from app.devices.iv_adapter import IVDevice
    device = object.__new__(IVDevice)
    device.has_role = lambda role: role in ("Vbias", "Vbias2")
    calls = []
    def ramp(role, *_args):
        calls.append(role)
        if role == "Vbias2":
            raise RuntimeError("second failed")
        return True
    device.ramp_to = ramp
    assert device.ramp_all_to_zero(delay_s=0) == []
    assert calls == ["Vbias", "Vbias"]
    calls.clear()
    errors = device.ramp_all_to_zero(delay_s=0, include_bias2=True)
    assert errors == ["Vbias2: second failed"]
    assert calls == ["Vbias2", "Vbias", "Vbias"]
    device._io_frozen = True
    assert "Vbias2" not in device.ramp_all_to_zero_report()
    assert device.ramp_all_to_zero_report(include_bias2=True)["Vbias2"]["status"] == "skipped_frozen_after_error"


def test_public_voltage_preflight_requires_verified_limits_and_checks_both():
    from controllers.smu_controller import SMUController
    from PySide6.QtCore import QObject
    controller = SMUController.__new__(SMUController)
    QObject.__init__(controller)
    controller._role_map = {"Vbias": "A", "Vbias2": "B"}
    controller._verified_voltage_ranges = {"A": 10.0, "B": 1.0}
    controller.role_is_available = lambda role: role in controller._role_map
    controller.validate_voltage_targets({"Vbias": 1.0, "Vbias2": -1.0})
    with pytest.raises(ValueError, match="Vbias2"):
        controller.validate_voltage_targets({"Vbias": 1.0, "Vbias2": 1.1})
    controller._verified_voltage_ranges.pop("B")
    with pytest.raises(RuntimeError, match="verified"):
        controller.validate_voltage_targets({"Vbias2": 0.0})


def test_manual_linked_step_uses_stricter_source_limit(monkeypatch):
    from controllers.smu_controller import _SMUWorker, cfg
    monkeypatch.setattr(cfg.ramp, "vbias_step_V", 0.2)
    monkeypatch.setattr(cfg.ramp, "vbias2_step_V", 0.025)
    assert _SMUWorker._linked_ramp_step(0.1) == 0.025
    with pytest.raises(ValueError):
        _SMUWorker._linked_ramp_step(0)
