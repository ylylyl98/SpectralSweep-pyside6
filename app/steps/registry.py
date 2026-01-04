
from __future__ import annotations
from typing import Dict, Type, Any, List

_REGISTRY: Dict[str, type] = {}

def register(cls):
    _REGISTRY[cls.id] = cls
    return cls

def get_step_class(step_id: str):
    if step_id not in _REGISTRY:
        raise KeyError(f"Unknown step: {step_id}")
    return _REGISTRY[step_id]

def list_steps() -> List[str]:
    return list(_REGISTRY.keys())

def build_recipe_from_preset(name: str, params: dict) -> list:
    if name == "dual_gate_sweep":
        return [
            {"step": "set_gate", "Vbg": params["Vbg_start"], "Vtg": params["Vtg_start"]},
            {"step": "dual_gate_sweep",
             "Vbg_start": params["Vbg_start"], "Vbg_stop": params["Vbg_stop"],
             "Vtg_start": params["Vtg_start"], "Vtg_stop": params["Vtg_stop"],
             "frames": params["frames"], "file_base": params.get("file_base","run")},
        ]
    raise ValueError(f"Unknown preset {name}")

try:
    from . import _import_all  # noqa
except Exception as e:
    import sys, traceback
    print("[steps] failed to import _import_all:", e, file=sys.stderr)
    traceback.print_exc()

