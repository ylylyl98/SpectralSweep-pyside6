"""Resolve and apply an optional second bias source.

The sources are written sequentially; no software loop guarantees simultaneous
hardware changes. Both starting voltages must be readable before a paired ramp.
"""
import math
import time
from collections.abc import Mapping


def _finite(value, name):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def normalize_bias2(settings: Mapping | None) -> dict:
    settings = settings or {}
    mode = str(settings.get("bias2_mode", "off")).strip().lower()
    if mode not in ("off", "independent", "follow"):
        raise ValueError(f"Unknown Vbias2 mode: {mode}")
    voltage = _finite(settings.get("vbias2_v", settings.get("voltage", 0.0)), "Vbias2")
    offset = _finite(settings.get("bias2_offset_v", settings.get("offset", 0.0)), "Vbias2 offset")
    return {"mode": mode, "voltage": voltage, "offset": offset}


def resolve_bias2(settings: Mapping | None, vbias: float | None) -> float | None:
    options = normalize_bias2(settings)
    if options["mode"] == "off":
        return None
    if options["mode"] == "independent":
        return options["voltage"]
    if vbias is None:
        raise ValueError("Vbias2 follow mode requires a Vbias target")
    return _finite(_finite(vbias, "Vbias") + options["offset"], "Vbias2 target")


def apply_bias_pair(device, *, vbias: float | None, settings, ramp_step: float,
                    delay_s: float, stop_cb=None, stop_exc=None):
    """Apply requested outputs after preflight, interleaving paired ramp steps."""
    master = None if vbias is None else _finite(vbias, "Vbias")
    follower = resolve_bias2(settings, master)
    targets = [(role, value) for role, value in (("Vbias", master), ("Vbias2", follower)) if value is not None]
    step = _finite(ramp_step, "ramp step")
    delay = _finite(delay_s, "ramp delay")
    if step < 0 or delay < 0:
        raise ValueError("Ramp step and delay must be non-negative")
    available = getattr(device, "role_is_available", None) or getattr(device, "has_role", None)
    for role, _ in targets:
        if not callable(available) or not available(role):
            raise RuntimeError(f"Required Keithley channel is unavailable: {role}")
    if not targets:
        return

    def check_stop():
        if callable(stop_cb) and stop_cb():
            raise (stop_exc or RuntimeError)()

    if len(targets) == 1:
        check_stop()
        role, value = targets[0]
        if not device.ramp_to(role, value, step, delay, stop_cb=stop_cb, stop_exc=stop_exc):
            raise RuntimeError(f"Failed to set {role}")
        return
    starts = []
    for role, _ in targets:
        value = device.read_role_voltage(role, strict=True)
        if value is None:
            raise RuntimeError(f"Cannot read initial voltage for {role}")
        starts.append(_finite(value, f"{role} initial voltage"))
    distances = [abs(target - start) for (_, target), start in zip(targets, starts)]
    count = max(1, math.ceil(max(distances) / step)) if step > 0 else 1
    for index in range(1, count + 1):
        fraction = index / count
        for (role, target), start in zip(targets, starts):
            check_stop()
            if not device.set_role_voltage_fast(role, start + (target - start) * fraction, 0.0):
                raise RuntimeError(f"Failed to set {role}")
        if delay:
            time.sleep(delay)
