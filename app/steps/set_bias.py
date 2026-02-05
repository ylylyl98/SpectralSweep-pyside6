from __future__ import annotations
from typing import Dict, Any, Optional, Tuple

from app.steps.registry import register

def _safe_read_current_bias(iv) -> Optional[float]:
    """
    Best-effort readback of current Vbias from the IV adapter.
    Tries:
      1) iv.read_current_bias() if you add it
      2) read via iv.setup/y_channel_collection if available (measured_Vbias)
    Returns float or None.
    """
    if iv is None:
        return None

    # 1) Preferred: explicit adapter method
    try:
        if hasattr(iv, "read_current_bias"):
            v = iv.read_current_bias()
            return float(v) if v is not None else None
    except Exception:
        pass

    # 2) Fallback: try common IVSetup measured channel name
    try:
        setup = getattr(iv, "setup", None)
        ycc = getattr(setup, "y_channel_collection", None)
        if setup is None or ycc is None:
            return None

        meas_key = "measured_Vbias"
        try:
            inst = ycc.get_instrument(meas_key)
        except KeyError:
            return None

        if inst:
            inst.read_y()
        ycc.receive_y(meas_key)

        v = setup.get_single_y_value(meas_key)
        return float(v)
    except Exception:
        return None


def _fmt(v: Optional[float]) -> str:
    return "?" if v is None else f"{v:g}"

@register
class SetBias:
    id = "set_bias"
    label = "Set Bias"
    schema = {
        "type": "object",
        "properties": {"Vbias": {"type": "number"}},
        "required": ["Vbias"],
    }
    def __init__(self, cfg): self.cfg = cfg

    def run(self, ctx):
        Vbias = float(self.cfg.get("Vbias", 0.0))
        iv = ctx.devices.get("iv")

        if not (iv and getattr(iv, "has_role", lambda *_: False)("Vbias")):
            ctx.log("Vbias not configured → skipping SetBias")
            return

        # Read BEFORE (for the log line at the moment setting begins)
        vb0 = _safe_read_current_bias(iv)
        ctx.log(f"Setting bias (ramp): Vbias={_fmt(vb0)}→{Vbias:g} V")

        try:
            # Keep ramping behavior for safety (IVDevice.set_bias defaults ramp_step>0)
            iv.set_bias(Vbias=Vbias, ramp_step=0.1)
        except Exception as e:
            ctx.log(f"SetBias error: {e}")
            return

        # Update axes (prefer readback if available)
        vb1 = _safe_read_current_bias(iv)
        ctx.axes["Vbias"] = float(vb1) if vb1 is not None else Vbias
