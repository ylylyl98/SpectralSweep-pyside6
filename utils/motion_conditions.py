"""Hardware independent planning for Motion Sweep condition batches."""
from __future__ import annotations

import itertools
import math
from typing import Any, Iterable, Mapping

from utils.mcd_common import MODE_DOPING_EFIELD, build_condition_batch, parse_numeric_spec

ROTATION_KEEP = "keep"
ROTATION_FIXED = "fixed"
ROTATION_LIST = "list"

# Versioned motion-axis plan vocabulary.  The legacy ``rotation_*`` helpers
# below remain deliberately unchanged for old saved sessions.
MOTION_PLAN_VERSION = 2
MOTION_NOT_USED = "not_used"
MOTION_HOLD = "hold"
MOTION_FIXED = "fixed"
MOTION_SWEEP = "sweep"
MOTION_MODES = (MOTION_NOT_USED, MOTION_HOLD, MOTION_FIXED, MOTION_SWEEP)
MOTION_AXES = ("stage", "rot1", "rot2")
MOTION_MAX_POINTS = 100000
# Descriptive aliases used by callers that prefer the axis terminology.
AXIS_MODE_NOT_USED = MOTION_NOT_USED
AXIS_MODE_HOLD = MOTION_HOLD
AXIS_MODE_FIXED = MOTION_FIXED
AXIS_MODE_SWEEP = MOTION_SWEEP


def motion_range_points(start: Any, stop: Any, step: Any, *, maximum_values: int = MOTION_MAX_POINTS) -> list[float]:
    """Return an explicit Start/Stop/Step sequence (stop is never rounded up)."""
    start, stop, step = (_number(x, name) for x, name in ((start, "Start"), (stop, "Stop"), (step, "Step")))
    if step == 0.0:
        raise ValueError("Step must be non-zero")
    delta = stop - start
    if delta and delta * step < 0:
        raise ValueError("Step direction does not reach Stop")
    if not delta:
        return [start]
    count = int(math.floor(abs(delta) / abs(step) + 1e-12)) + 1
    if count < 1 or count > maximum_values:
        raise ValueError(f"Range expands beyond {maximum_values} values")
    return [start + i * step for i in range(count)]


def _number(value: Any, label: str) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be finite") from exc
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return value


def parse_motion_values(value: Any, *, mode: str = MOTION_SWEEP,
                        maximum_values: int = MOTION_MAX_POINTS) -> list[float]:
    """Parse motion values without the legacy tuple=count ambiguity.

    Sweep ranges are represented as ``{start, stop, step}``; a list/tuple of
    explicit values is also accepted.  A scalar is valid for Fixed and Sweep.
    """
    mode = str(mode).strip().lower()
    if mode not in MOTION_MODES:
        raise ValueError(f"Unknown motion mode: {mode}")
    if mode in (MOTION_NOT_USED, MOTION_HOLD):
        return []
    if isinstance(value, Mapping) and {"start", "stop", "step"}.issubset(value):
        values = motion_range_points(value["start"], value["stop"], value["step"], maximum_values=maximum_values)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("Motion values are empty")
        # The explicit ``start,stop,step`` tuple is the new motion syntax.
        try:
            import ast
            node = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            node = None
        if isinstance(node, tuple) and len(node) == 3:
            values = motion_range_points(*node, maximum_values=maximum_values)
        elif isinstance(node, (list, tuple)):
            values = [_number(v, "Motion value") for v in node]
        else:
            values = [_number(part.strip(), "Motion value") for part in text.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        # Structured [start, stop, step] is a range only when explicitly
        # wrapped in a mapping; lists stay exact point lists.
        values = [_number(v, "Motion value") for v in value]
    else:
        values = [_number(value, "Motion value")]
    if not values or len(values) > maximum_values:
        raise ValueError(f"Motion values must contain 1..{maximum_values} points")
    return values


def normalize_motion_axis(axis: str, spec: Any, *, maximum_values: int = MOTION_MAX_POINTS) -> dict[str, Any]:
    """Normalize one axis and preserve whether values were requested or held."""
    axis = str(axis).strip().lower()
    if axis not in MOTION_AXES:
        raise ValueError(f"Unknown motion axis: {axis}")
    if isinstance(spec, str):
        mode, raw = spec, None
    elif isinstance(spec, Mapping):
        mode, raw = spec.get("mode", spec.get("kind", MOTION_NOT_USED)), spec.get("values", spec.get("value"))
        if raw is None and {"start", "stop", "step"}.issubset(spec):
            raw = {key: spec[key] for key in ("start", "stop", "step")}
    else:
        mode, raw = MOTION_SWEEP, spec
    aliases = {"not used": MOTION_NOT_USED, "unused": MOTION_NOT_USED, "hold position": MOTION_HOLD,
               "keep": MOTION_HOLD, "list": MOTION_SWEEP}
    mode = aliases.get(str(mode).strip().lower(), str(mode).strip().lower())
    if mode not in MOTION_MODES:
        raise ValueError(f"Unknown motion mode for {axis}: {mode}")
    if mode == MOTION_NOT_USED:
        return {"axis": axis, "mode": mode, "values": [], "count": 1}
    if mode == MOTION_HOLD:
        return {"axis": axis, "mode": mode, "values": [], "count": 1}
    values = parse_motion_values(raw, mode=mode, maximum_values=maximum_values)
    if mode == MOTION_FIXED and len(values) != 1:
        raise ValueError(f"Fixed {axis} requires one value")
    return {"axis": axis, "mode": mode, "values": values, "count": len(values)}


def build_motion_plan(axes: Mapping[str, Any], conditions: Iterable[Mapping[str, Any]] | None = None,
                      *, repeat: int = 1, maximum_entries: int = MOTION_MAX_POINTS,
                      axis_order: Iterable[str] | None = None) -> dict[str, Any]:
    """Build a gate-outer motion plan with explicit per-point targets.

    Axis order is the supplied mapping order (slow to fast).  Hold and
    Not-used axes produce no target and are never connected by the worker.
    """
    if isinstance(repeat, bool) or int(repeat) != repeat or int(repeat) < 1:
        raise ValueError("Repeat must be a positive integer")
    if isinstance(maximum_entries, bool) or int(maximum_entries) != maximum_entries or int(maximum_entries) < 1:
        raise ValueError("maximum_entries must be a positive integer")
    repeat = int(repeat)
    # Bound each individual expansion by the total plan cap.  This rejects a
    # huge range before materializing it, while the product check below still
    # catches otherwise-valid dimensions whose Cartesian product is too large.
    per_axis_limit = min(MOTION_MAX_POINTS, int(maximum_entries))
    normalized = {axis: normalize_motion_axis(axis, spec, maximum_values=per_axis_limit) for axis, spec in axes.items()}
    if axis_order is None:
        requested_order = [axis for axis in MOTION_AXES if axis in normalized]
    else:
        requested_order = [str(axis).lower() for axis in axis_order]
    if set(requested_order) != set(normalized):
        missing = set(normalized).difference(requested_order)
        extra = set(requested_order).difference(normalized)
        if missing or extra:
            raise ValueError("axis_order must contain each configured axis exactly once")
    if len(requested_order) != len(set(requested_order)):
        raise ValueError("axis_order cannot contain duplicate axes")
    ordered = requested_order
    rows = [dict(row) for row in (conditions or ({},))]
    enabled = [row for row in rows if bool(row.get("enabled", True))]
    if not enabled:
        raise ValueError("At least one enabled gate condition is required")
    active = [axis for axis in ordered if normalized[axis]["mode"] != MOTION_NOT_USED]
    sweep_axes = [axis for axis in active if normalized[axis]["mode"] == MOTION_SWEEP]
    dimensions = [len(normalized[axis]["values"]) for axis in sweep_axes]
    count = len(enabled)
    for dimension in dimensions:
        count *= dimension
    count *= repeat
    if count > maximum_entries:
        raise ValueError(f"Motion plan exceeds {maximum_entries} entries")
    # Repeat the fastest sweep segment for each gate/slower-axis combination.
    fast = sweep_axes[-1] if sweep_axes else None
    slower = [axis for axis in sweep_axes if axis != fast]
    entries: list[dict[str, Any]] = []
    sequence = 0
    for ci, condition in enumerate(enabled):
        slow_products = itertools.product(*[
            normalized[axis]["values"] for axis in slower
        ]) or [()]
        for slow_values in slow_products:
            fast_values = normalized[fast]["values"] if fast else [None]
            for rep in range(1, repeat + 1):
                for fast_value in fast_values:
                    targets = {}
                    for axis in active:
                        mode = normalized[axis]["mode"]
                        if mode == MOTION_HOLD:
                            continue
                        if axis == fast:
                            targets[axis] = fast_value
                        elif mode == MOTION_FIXED:
                            targets[axis] = normalized[axis]["values"][0]
                        else:
                            targets[axis] = slow_values[slower.index(axis)]
                    sequence += 1
                    entries.append({"sequence": sequence, "condition_index": ci,
                                    "condition": dict(condition), "targets": targets, "repeat": rep})
    return {"version": MOTION_PLAN_VERSION, "axes": normalized, "axis_order": ordered,
            "conditions": enabled, "repeat": repeat, "entries": entries,
            "count": len(entries), "fast_axis": fast}


def parse_condition_input(text: str, label: str) -> list[float]:
    return parse_numeric_spec(text, label, maximum_values=10000)


def build_conditions(mode: str, input_a: str, input_b: str, expansion: str,
                     ratio: float, vbias: float = 0.0, voltage_limit: float = 200.0) -> list[dict[str, Any]]:
    if mode not in (MODE_DOPING_EFIELD, "direct"):
        raise ValueError("Condition mode must be Doping / E-field or Vtg / Vbg")
    return build_condition_batch(mode, input_a, input_b, expansion, ratio,
                                 vbias_v=vbias, voltage_limit=voltage_limit)


def resolve_rotation_plan(spec: str, current: Mapping[str, float], *, axis: str = "rot1") -> dict[str, Any]:
    """Resolve Keep current, Fixed, or List/range rotation settings."""
    value = str(spec).strip()
    if value.lower() in ("keep", "keep current", "current"):
        return {"mode": ROTATION_KEEP, "axis": axis, "values": [], "requested": current.get(axis)}
    if value.lower().startswith("fixed"):
        value = value.split(":", 1)[1].strip() if ":" in value else value[5:].strip()
        vals = parse_numeric_spec(value, "Fixed rotation", maximum_values=1)
        return {"mode": ROTATION_FIXED, "axis": axis, "values": vals, "requested": vals[0]}
    vals = parse_numeric_spec(value, "Rotation list", maximum_values=10000)
    return {"mode": ROTATION_LIST, "axis": axis, "values": vals, "requested": vals}


def expand_sequence(conditions: Iterable[Mapping[str, Any]], rotations: Iterable[float] | Mapping[str, Iterable[float]],
                    *, rotation2: Iterable[float] | None = None,
                    order: str = "gate-first", repeats: int = 1,
                    selected: Iterable[tuple[int, ...]] | None = None,
                    maximum_entries: int = 100000) -> list[dict[str, int]]:
    """Expand a condition/rotation plan without allocating an oversized plan.

    ``rotations`` may be the historical one-axis list, or a mapping containing
    ``rot1`` and ``rot2`` lists.  The latter produces the independent Cartesian
    rotation combinations.  A selected iterable may contain ``(condition,
    rot1)`` for the old API or ``(condition, rot1, rot2)`` for two axes.
    """
    conds = list(conditions)
    if isinstance(maximum_entries, bool) or not isinstance(maximum_entries, int) or maximum_entries < 1:
        raise ValueError("maximum_entries must be a positive integer")
    if isinstance(rotations, Mapping):
        r1 = list(rotations.get("rot1", rotations.get("rotation", ())))
        r2 = list(rotations.get("rot2", ()))
    else:
        r1 = list(rotations)
        r2 = list(rotation2) if rotation2 is not None else []
    if not conds or repeats < 1 or int(repeats) != repeats:
        raise ValueError("Sequence requires conditions, rotations, and at least one repeat")
    repeats = int(repeats)
    if not r1:
        # A rot2-only or gate-only plan uses a singleton placeholder for the
        # absent first axis; the worker never treats it as a commanded angle.
        r1 = [0.0]
    if order not in ("gate-first", "rotation-first"):
        raise ValueError("Sequence order must be gate-first or rotation-first")
    if any(not isinstance(value, (int, float)) or not math.isfinite(float(value))
           for value in r1 + r2):
        raise ValueError("Rotation values must be finite")
    # A missing second axis is the one-axis compatibility case.  Keep an
    # explicit rot2 list independent, including its own one-value plan.
    n2 = len(r2) if r2 else 1
    if selected is None:
        pair_count = len(conds) * len(r1) * n2
        if pair_count * repeats > maximum_entries:
            raise ValueError(f"Expanded sequence exceeds {maximum_entries} entries")
        if order == "gate-first":
            pairs = [(ci, ri, rj) for ci in range(len(conds))
                     for ri in range(len(r1)) for rj in range(n2)]
        else:
            pairs = [(ci, ri, rj) for ri in range(len(r1)) for rj in range(n2)
                     for ci in range(len(conds))]
    else:
        pairs = []
        for raw in selected:
            if not isinstance(raw, (tuple, list)) or len(raw) not in (2, 3):
                raise ValueError("Sequence selection entries must contain condition and rotation indices")
            if any(isinstance(index, bool) or not isinstance(index, int) for index in raw):
                raise ValueError("Sequence selection indices must be integers")
            ci, ri = raw[:2]
            rj = raw[2] if len(raw) == 3 else 0
            if not (0 <= ci < len(conds) and 0 <= ri < len(r1) and 0 <= rj < n2):
                raise ValueError("Sequence selection is out of range")
            pairs.append((ci, ri, rj))
            if len(pairs) * repeats > maximum_entries:
                raise ValueError(f"Expanded sequence exceeds {maximum_entries} entries")
        if not pairs:
            raise ValueError("Sequence selection cannot be empty")
    result: list[dict[str, int]] = []
    for ci, ri, rj in pairs:
        for rep in range(1, repeats + 1):
            result.append({"condition_index": ci, "rotation_index": ri,
                           "rotation_index2": rj, "rot1_index": ri,
                           "rot2_index": rj, "repeat": rep})
    return result


def sequence_preview(sequence: Iterable[Mapping[str, Any]], conditions: list[Mapping[str, Any]],
                     rotations: list[float] | Mapping[str, list[float]]) -> list[dict[str, Any]]:
    if isinstance(rotations, Mapping):
        rot1 = list(rotations.get("rot1", ()))
        rot2 = list(rotations.get("rot2", ()))
    else:
        rot1, rot2 = list(rotations), []
    out = []
    for n, item in enumerate(sequence, 1):
        ci, ri = int(item["condition_index"]), int(item.get("rot1_index", item.get("rotation_index", 0)))
        rj = int(item.get("rot2_index", item.get("rotation_index2", 0)))
        condition = conditions[ci]
        out.append({"sequence": n, "repeat": int(item.get("repeat", 1)),
                    "condition_index": ci + 1, "rotation_index": ri + 1,
                    "rotation": float(rot1[ri]) if rot1 else None,
                    "rot1": float(rot1[ri]) if rot1 else None,
                    "rot2": float(rot2[rj]) if rot2 else None,
                    "D": condition.get("doping_v"), "F": condition.get("efield_v"),
                    "Vtg": condition.get("vtg_v"), "Vbg": condition.get("vbg_v"),
                    "Vbias": condition.get("vbias_v", 0.0)})
    return out


__all__ = ["ROTATION_KEEP", "ROTATION_FIXED", "ROTATION_LIST", "MOTION_PLAN_VERSION",
           "MOTION_NOT_USED", "MOTION_HOLD", "MOTION_FIXED", "MOTION_SWEEP", "MOTION_MODES",
           "AXIS_MODE_NOT_USED", "AXIS_MODE_HOLD", "AXIS_MODE_FIXED", "AXIS_MODE_SWEEP",
           "MOTION_AXES", "motion_range_points", "parse_motion_values", "normalize_motion_axis",
           "build_motion_plan", "parse_condition_input",
           "build_conditions", "resolve_rotation_plan", "expand_sequence", "sequence_preview"]
