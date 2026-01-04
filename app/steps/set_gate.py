
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Any
from .registry import register

@register
class SetGate:
    id = "set_gate"
    label = "Set Gate"
    schema = {"type":"object","properties":{"Vbg":{"type":"number"},"Vtg":{"type":"number"}},"required":["Vbg","Vtg"]}
    def __init__(self, cfg: Dict[str, Any]): self.cfg = cfg
    def validate(self, ctx): pass
    def run(self, ctx):
        Vbg = float(self.cfg.get("Vbg", 0.0))
        Vtg = float(self.cfg.get("Vtg", 0.0))
        iv = ctx.devices.get("iv")
        if iv is not None:
            try:
                iv.set_gates(Vbg, Vtg)
            except Exception as e:
                ctx.log(f"IV set_gates error: {e}")
        ctx.axes["Vbg"] = Vbg
        ctx.axes["Vtg"] = Vtg
        ctx.log(f"Set Vbg={Vbg} V, Vtg={Vtg} V")
