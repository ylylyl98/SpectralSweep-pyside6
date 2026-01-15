# app/steps/_import_all.py

# Import each step module independently so one failure doesn't block others.
# Also print a clear error so you can see which one failed.

def _safe_import(modname: str):
    try:
        __import__(modname, fromlist=['*'])
        return True, None
    except Exception as e:
        import sys, traceback
        print(f"[steps] failed to import {modname}: {e}", file=sys.stderr)
        traceback.print_exc()
        return False, e

# Import your steps. Put set_lightfield FIRST so it registers even if others fail.
modules = [
    "app.steps.set_lightfield",  # unified LF settings
    "app.steps.set_gate",
    "app.steps.set_bias",
    "app.steps.sync_wavelength_axis",
    # include whichever one is correct for your file name:
    "app.steps.dual_gate_sweep",   # if your file is dual_gate_swep.py
]

for m in modules:
    _safe_import(m)
