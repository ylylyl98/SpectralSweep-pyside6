from __future__ import annotations
from app.steps.registry import register

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
        Vbias = float(self.cfg["Vbias"])
        iv = ctx.devices.get("iv")
        if iv and getattr(iv, "has_role", lambda *_: False)("Vbias"):
            try:
                iv.set_bias(Vbias)
                ctx.axes["Vbias"] = Vbias  # so downstream rows can include it
                ctx.log(f"Set Vbias={Vbias} V")
            except Exception as e:
                ctx.log(f"SetBias error: {e}")
        else:
            ctx.log("Vbias not configured → skipping SetBias")
