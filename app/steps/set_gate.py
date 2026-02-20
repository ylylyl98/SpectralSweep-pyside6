# app/steps/set_gate.py
from __future__ import annotations

from typing import Dict, Any, Tuple
import math

from .registry import register


def _safe_read_current(iv) -> Tuple[float, float]:
    nan = float("nan")
    if iv is None:
        return nan, nan
    try:
        if hasattr(iv, "read_current_gates"):
            bg, tg = iv.read_current_gates()
            try:
                bg = float(bg)
            except Exception:
                bg = nan
            try:
                tg = float(tg)
            except Exception:
                tg = nan
            return bg, tg
    except Exception:
        pass
    return nan, nan


@register
class SetGate:
    id = "set_gate"
    label = "Set Gate"
    schema = {
        "type": "object",
        "properties": {
            "Vbg": {"type": "number"},
            "Vtg": {"type": "number"},
            "ramp_step": {"type": "number"},
            "step_delay_s": {"type": "number"},
        },
        "required": ["Vbg", "Vtg"],
    }

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg

    def validate(self, ctx):
        pass

    def run(self, ctx):
        nan = float("nan")

        Vbg = float(self.cfg.get("Vbg", 0.0))
        Vtg = float(self.cfg.get("Vtg", 0.0))

        ramp_step = float(self.cfg.get("ramp_step", 0.1))
        step_delay_s = float(self.cfg.get("step_delay_s", 0.2))

        iv = ctx.devices.get("iv")

        has_vbg = bool(getattr(iv, "has_role", lambda *_: False)("Vbg")) if iv else False
        has_vtg = bool(getattr(iv, "has_role", lambda *_: False)("Vtg")) if iv else False

        # Read BEFORE (best effort)
        bg0, tg0 = _safe_read_current(iv)

        ctx.log(
            f"Setting gates (ramp): "
            f"Vbg={bg0:g}→{Vbg:g} V, "
            f"Vtg={tg0:g}→{Vtg:g} V "
            f"(step={ramp_step:g} V, dt={step_delay_s:g} s)"
        )

        if iv is not None:
            try:
                iv.set_gates(
                    Vbg=Vbg if has_vbg else None,
                    Vtg=Vtg if has_vtg else None,
                    ramp_step=ramp_step,
                    delay_s=step_delay_s,
                )
            except Exception as e:
                ctx.log(f"IV set_gates error: {e}")

        # Read AFTER and log
        bg1, tg1 = _safe_read_current(iv)
        ctx.log(f"Gates now: Vbg={bg1:g} V, Vtg={tg1:g} V")

        # Always keep ctx.axes in sync with "current" gates:
        # prefer measured values if finite; otherwise fall back to commanded setpoints.
        if not getattr(ctx, "axes", None):
            ctx.axes = {}

        if has_vbg:
            ctx.axes["Vbg"] = float(bg1) if math.isfinite(bg1) else float(Vbg)
        else:
            ctx.axes["Vbg"] = nan

        if has_vtg:
            ctx.axes["Vtg"] = float(tg1) if math.isfinite(tg1) else float(Vtg)
        else:
            ctx.axes["Vtg"] = nan
