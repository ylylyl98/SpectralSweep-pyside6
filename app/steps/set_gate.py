# app/steps/set_gate.py
from __future__ import annotations
from typing import Dict, Any, Tuple, Optional
from .registry import register

def _safe_read_current(iv) -> Tuple[float, float]:
    nan = float("nan")
    if iv is None:
        return nan, nan
    try:
        if hasattr(iv, "read_current_gates"):
            bg, tg = iv.read_current_gates()
            # force float, fallback to nan
            try: bg = float(bg)
            except Exception: bg = nan
            try: tg = float(tg)
            except Exception: tg = nan
            return bg, tg
    except Exception:
        pass
    return nan, nan

@register
class SetGate:
    id = "set_gate"
    label = "Set Gate"
    schema = {"type":"object","properties":{"Vbg":{"type":"number"},"Vtg":{"type":"number"}},"required":["Vbg","Vtg"]}

    def __init__(self, cfg: Dict[str, Any]): self.cfg = cfg
    def validate(self, ctx): pass

    def run(self, ctx):
        nan = float("nan")

        Vbg = float(self.cfg.get("Vbg", 0.0))
        Vtg = float(self.cfg.get("Vtg", 0.0))
        iv = ctx.devices.get("iv")

        has_vbg = bool(getattr(iv, "has_role", lambda *_: False)("Vbg")) if iv else False
        has_vtg = bool(getattr(iv, "has_role", lambda *_: False)("Vtg")) if iv else False

        # Read BEFORE (will be nan if missing)
        bg0, tg0 = _safe_read_current(iv)

        # Log AT the setting step
        ctx.log(
            f"Setting gates (ramp): "
            f"Vbg={bg0:g}→{Vbg:g} V, "
            f"Vtg={tg0:g}→{Vtg:g} V"
        )

        if iv is not None:
            try:
                iv.set_gates(
                    Vbg=Vbg if has_vbg else None,
                    Vtg=Vtg if has_vtg else None,
                )
            except Exception as e:
                ctx.log(f"IV set_gates error: {e}")

        # Axes: write nan when not mapped so nothing looks “real”
        ctx.axes["Vbg"] = Vbg if has_vbg else nan
        ctx.axes["Vtg"] = Vtg if has_vtg else nan

        # (optional) read AFTER and log
        bg1, tg1 = _safe_read_current(iv)
        ctx.log(f"Gates now: Vbg={bg1:g} V, Vtg={tg1:g} V")
