"""Shared hardware-free MCD coordinates, conditions, safety and filenames."""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

from utils.filename_builder import (
    FilenameContext, build_base_filename, format_compact_number, sanitize_token,
)

MODE_DIRECT = "direct"
MODE_VTG_FROM_VBG_RATIO = "vtg_from_vbg_ratio"
MODE_VBG_FROM_VTG_RATIO = "vbg_from_vtg_ratio"
MODE_FIXED_EFIELD = "fixed_efield"
MODE_FIXED_DOPING = "fixed_doping"
MODE_DOPING_EFIELD = "doping_efield"
GATE_MODES = (MODE_DIRECT, MODE_VTG_FROM_VBG_RATIO, MODE_VBG_FROM_VTG_RATIO,
              MODE_FIXED_EFIELD, MODE_FIXED_DOPING, MODE_DOPING_EFIELD)


def _finite(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def mcd_coordinates(vtg_v: float, vbg_v: float, ratio: float) -> tuple[float, float]:
    """Canonical coordinates D=Vtg+rVbg and F=Vtg-rVbg."""
    vtg, vbg, r = (_finite(vtg_v, "Vtg"), _finite(vbg_v, "Vbg"), _finite(ratio, "Gate ratio"))
    return vtg + r * vbg, vtg - r * vbg


def vtg_vbg_from_doping_efield(doping_v: float, efield_v: float, ratio: float) -> tuple[float, float]:
    doping, efield, r = (_finite(doping_v, "Doping"), _finite(efield_v, "E-field"), _finite(ratio, "Gate ratio"))
    if abs(r) <= 1e-12:
        raise ValueError("Gate ratio r must be non-zero to compute Vtg/Vbg from Doping/E-field")
    return (doping + efield) / 2.0, (doping - efield) / (2.0 * r)


def gate_ratio_from_factors(vtg_factor: float, vbg_factor: float) -> float:
    """Canonical ``r`` for the displayed ``a×Vtg = b×Vbg`` relation."""
    a = _finite(vtg_factor, "Vtg ratio factor")
    b = _finite(vbg_factor, "Vbg ratio factor")
    if abs(a) <= 1e-12 or abs(b) <= 1e-12:
        raise ValueError("Both gate-ratio factors must be non-zero")
    return b / a


def parse_numeric_spec(text: str, label: str, *, maximum_values: int = 1000) -> list[float]:
    """Parse comma values and inclusive ``start:step:stop`` ranges."""
    values: list[float] = []
    for raw in str(text).split(","):
        token = raw.strip()
        if not token:
            continue
        if ":" not in token:
            values.append(_finite(token, label))
            continue
        parts = token.split(":")
        if len(parts) != 3:
            raise ValueError(f"{label} range must use start:step:stop")
        start, step, stop = (_finite(part, label) for part in parts)
        if abs(step) <= 1e-15:
            raise ValueError(f"{label} range step must be non-zero")
        if (stop - start) * step < 0:
            raise ValueError(f"{label} range step points away from its stop")
        count = int(math.floor((stop - start) / step + 1e-12)) + 1
        if count < 1 or len(values) + count > maximum_values:
            raise ValueError(f"{label} expands beyond {maximum_values} values")
        values.extend(start + index * step for index in range(count))
    if not values:
        raise ValueError(f"{label} must contain at least one value")
    if len(values) > maximum_values:
        raise ValueError(f"{label} expands beyond {maximum_values} values")
    return values


def expand_condition_inputs(values_a: Iterable[float], values_b: Iterable[float],
                            expansion: str, *, maximum_rows: int = 10000) -> list[tuple[float, float]]:
    a = [_finite(value, "Input A") for value in values_a]
    b = [_finite(value, "Input B") for value in values_b]
    if not a or not b:
        raise ValueError("Both gate inputs require at least one value")
    method = str(expansion).strip().lower()
    if method == "grid":
        pairs = [(left, right) for left in a for right in b]
    elif method == "paired":
        if len(a) == 1:
            pairs = [(a[0], right) for right in b]
        elif len(b) == 1:
            pairs = [(left, b[0]) for left in a]
        elif len(a) == len(b):
            pairs = list(zip(a, b))
        else:
            raise ValueError("Paired inputs must have equal lengths or one input must be a single value")
    else:
        raise ValueError("Expansion must be paired or grid")
    if len(pairs) > maximum_rows:
        raise ValueError(f"Gate inputs expand beyond {maximum_rows} rows")
    return pairs


def build_condition_batch(mode: str, input_a: str, input_b: str, expansion: str,
                          ratio: float, *, vbias_v: float = 0.0,
                          voltage_limit: float | None = None) -> list[dict[str, Any]]:
    """Build and validate a complete batch before callers mutate their table."""
    labels = (
        ("Vtg", "Vbg") if str(mode) == MODE_DIRECT
        else ("Doping", "E-field")
    )
    values_a = parse_numeric_spec(input_a, labels[0])
    values_b = parse_numeric_spec(input_b, labels[1])
    rows = [
        resolve_condition_line({
            "enabled": True, "mode": mode, "input_a": a,
            "input_b": b, "vbias_v": vbias_v,
        }, ratio)
        for a, b in expand_condition_inputs(values_a, values_b, expansion)
    ]
    if voltage_limit is not None:
        validate_gate_conditions(rows, voltage_limit)
    return rows


def _mode(condition: Mapping[str, Any]) -> str:
    mode = str(condition.get("mode", MODE_DIRECT)).strip().lower()
    mode = {"voltage": MODE_DIRECT, "coordinates": MODE_FIXED_DOPING,
            "vtg/vbg": MODE_DIRECT, "vtg_from_vbg": MODE_VTG_FROM_VBG_RATIO,
            "vbg_from_vtg": MODE_VBG_FROM_VTG_RATIO, "fixed efield": MODE_FIXED_EFIELD,
            "fixed doping": MODE_FIXED_DOPING,
            "doping / e-field": MODE_DOPING_EFIELD}.get(mode, mode)
    if mode not in GATE_MODES:
        raise ValueError(f"Unknown gate condition mode: {mode}")
    return mode


def resolve_condition_line(condition: Mapping[str, Any], ratio: float) -> dict[str, Any]:
    """Resolve one canonical condition while preserving input provenance.

    The stable row representation is ``mode/input_a/input_b``.  Legacy rows
    with Vtg/Vbg are accepted as direct rows.  Ratio modes use input_b as q;
    fixed coordinate modes use input_a/input_b as D/F.
    """
    item = dict(condition)
    mode = _mode(item)
    r = _finite(ratio, "Gate ratio")
    a = item.get("input_a", item.get("value_a", item.get("constant", item.get("vtg_v", 0.0))))
    b = item.get("input_b", item.get("value_b", item.get("q", item.get("vbg_v", 0.0))))
    if mode == MODE_DIRECT:
        vtg, vbg = _finite(a, "Input A/Vtg"), _finite(b, "Input B/Vbg")
        equation = "Vtg=input_a; Vbg=input_b"
    elif mode == MODE_VTG_FROM_VBG_RATIO:
        vbg, q = _finite(a, "Input A/Vbg"), _finite(b, "Input B/q")
        vtg, equation = q * vbg, "Vtg=q*Vbg; Vbg=input_a; q=input_b"
    elif mode == MODE_VBG_FROM_VTG_RATIO:
        vtg, q = _finite(a, "Input A/Vtg"), _finite(b, "Input B/q")
        vbg, equation = q * vtg, "Vbg=q*Vtg; Vtg=input_a; q=input_b"
    elif mode == MODE_DOPING_EFIELD:
        doping, efield = _finite(a, "Input A/Doping"), _finite(b, "Input B/E-field")
        vtg, vbg = vtg_vbg_from_doping_efield(doping, efield, r)
        equation = "D=input_a; F=input_b; Vtg=(D+F)/2; Vbg=(D-F)/(2*r)"
    elif mode == MODE_FIXED_EFIELD:
        efield, vbg = _finite(a, "Input A/E-field"), _finite(b, "Input B/Vbg")
        vtg, equation = efield + r * vbg, "F=input_a; Vbg=input_b; Vtg=F+r*Vbg"
    else:  # fixed doping
        doping, vbg = _finite(a, "Input A/Doping"), _finite(b, "Input B/Vbg")
        vtg, equation = doping - r * vbg, "D=input_a; Vbg=input_b; Vtg=D-r*Vbg"
    doping, efield = mcd_coordinates(vtg, vbg, r)
    return {
        **item, "mode": mode, "input_a": _finite(a, "Input A"), "input_b": _finite(b, "Input B"),
        "enabled": bool(item.get("enabled", True)), "vtg_v": vtg, "vbg_v": vbg,
        "vbias_v": _finite(item.get("vbias_v", 0.0), "Vbias"), "doping_v": doping, "efield_v": efield,
        "resolved": {"vtg_v": vtg, "vbg_v": vbg, "doping_v": doping, "efield_v": efield},
        "provenance": {"mode": mode, "input_a": a, "input_b": b, "gate_ratio": r, "equation": equation},
    }


def condition_line_representation(condition: Mapping[str, Any], ratio: float) -> dict[str, Any]:
    resolved = resolve_condition_line(condition, ratio)
    return {"mode": resolved["mode"], "input_a": resolved["input_a"], "input_b": resolved["input_b"],
            "gate_ratio": _finite(ratio, "Gate ratio"), "resolved": resolved["resolved"],
            "provenance": resolved["provenance"]}


def resolve_gate_conditions(conditions: Iterable[Mapping[str, Any]], ratio: float) -> list[dict[str, Any]]:
    result = [resolve_condition_line(condition, ratio) for condition in conditions]
    if not result or not any(item["enabled"] for item in result):
        raise ValueError("At least one enabled voltage condition is required")
    return result


def validate_gate_conditions(conditions: Iterable[Mapping[str, Any]], voltage_limit: float) -> None:
    limit = abs(_finite(voltage_limit, "Voltage limit"))
    for index, condition in enumerate(conditions, start=1):
        for key in ("vtg_v", "vbg_v", "vbias_v"):
            value = _finite(condition.get(key, 0.0), f"gate condition {index} {key}")
            if abs(value) > limit + 1e-9:
                raise ValueError(f"gate condition {index} {key} is outside the ±{limit:g} V compliance limit")


def smu_readiness_issues(smu_ctrl: Any, required_roles: Iterable[str]) -> list[str]:
    """Shared role/health/reconnect/compliance readiness check."""
    roles = tuple(str(role) for role in required_roles)
    if not roles:
        return []
    if smu_ctrl is None or not bool(getattr(smu_ctrl, "is_connected", False)):
        return [f"Required SMU channels are not connected: {', '.join(roles)}."]
    device = getattr(smu_ctrl, "device", None)
    if device is None:
        return [f"Required SMU channels are unavailable: {', '.join(roles)}."]
    missing = []
    checker = getattr(device, "role_is_available", None) or getattr(device, "has_role", None)
    if not callable(checker):
        return [f"Required SMU channel roles cannot be verified: {', '.join(roles)}."]
    for role in roles:
        try:
            if not bool(checker(role)):
                missing.append(role)
        except Exception:
            missing.append(role)
    if missing:
        return [f"Required SMU channels are missing: {', '.join(missing)}."]
    health = getattr(device, "health_states", {})
    if isinstance(health, Mapping):
        unhealthy = [f"{role}={health.get(role)}" for role in roles if health.get(role, "ready") != "ready"]
        if unhealthy:
            return ["Reconnect the SMU after a hardware fault: " + ", ".join(unhealthy)]
    if bool(getattr(device, "requires_reconnect", False)):
        return ["Reconnect the SMU after the hardware fault."]
    limits = getattr(smu_ctrl, "limits_are_applied_for_roles", None)
    if callable(limits):
        try:
            if not bool(limits(roles)):
                return ["Apply and verify SMU compliance settings before running."]
        except Exception:
            return ["SMU compliance settings could not be verified."]
    return []


def build_mcd2100_filename(device_id: str, gate_index: int, start_t: float,
                           stop_t: float, direction: str, *, doping_v=None,
                           efield_v=None, vtg_v=None, vbg_v=None, vbias_v=None,
                           ratio=None) -> str:
    """Build the MCD extension around the application's canonical filename base."""
    context = FilenameContext(
        device_id=sanitize_token(device_id) or "device", tag="MCD", temperature="",
        mode="MCD", laser_nm="", nominal_power_uw=None, center_nm=None,
        exposure_ms=None, accumulations=None,
    )
    parts = [build_base_filename(context, []) or "device", "MCD", f"G{int(gate_index):02d}",
             f"B{format_compact_number(start_t, keep_sign=True)}to"
             f"{format_compact_number(stop_t, keep_sign=True)}T"]
    for prefix, value, signed in (("D", doping_v, False), ("F", efield_v, False),
                                  ("Vtg", vtg_v, True), ("Vbg", vbg_v, True),
                                  ("Vb", vbias_v, True), ("r", ratio, False)):
        if value is not None:
            parts.append(prefix + format_compact_number(value, keep_sign=signed))
    parts.append(sanitize_token(direction).lower() or "forward")
    return "_".join(sanitize_token(part) for part in parts if sanitize_token(part)) + ".csv"


__all__ = ["MODE_DIRECT", "MODE_VTG_FROM_VBG_RATIO", "MODE_VBG_FROM_VTG_RATIO", "MODE_FIXED_EFIELD",
           "MODE_FIXED_DOPING", "GATE_MODES", "mcd_coordinates", "vtg_vbg_from_doping_efield",
           "MODE_DOPING_EFIELD", "gate_ratio_from_factors", "parse_numeric_spec", "expand_condition_inputs",
           "build_condition_batch",
           "resolve_condition_line", "condition_line_representation", "resolve_gate_conditions",
           "validate_gate_conditions", "smu_readiness_issues", "FilenameContext", "build_base_filename",
           "build_mcd2100_filename", "format_compact_number", "sanitize_token"]
