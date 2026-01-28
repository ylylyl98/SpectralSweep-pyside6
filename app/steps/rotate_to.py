# app/steps/rotate_to.py
from __future__ import annotations

import time
from app.steps.registry import register


@register
class RotateTo:
    id = "rotate_to"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def run(self, ctx):
        angle = float(self.cfg.get("angle_deg"))
        which = str(self.cfg.get("which", "rotation1")).lower()

        # optional behavior knobs
        tol = float(self.cfg.get("tol_deg", 0.2))          # readback tolerance
        settle_s = float(self.cfg.get("settle_s", 0.0))    # extra settle time after move
        verify = bool(self.cfg.get("verify", True))        # verify by readback if possible

        def log(msg: str):
            cb = getattr(ctx, "progress_cb", None)
            if cb:
                cb(msg)

        def _read_angle(dev):
            # try common APIs (your adapters support get_position)
            for attr in ("get_angle", "get_position", "position", "angle"):
                if hasattr(dev, attr):
                    v = getattr(dev, attr)
                    try:
                        return float(v() if callable(v) else v)
                    except Exception:
                        return None
            return None

        def _move(name: str) -> bool:
            dev = ctx.devices.get(name)
            if dev is None:
                log(f"[rotate_to] {name}: not connected (skip)")
                return False

            before = _read_angle(dev)
            log(f"[rotate_to] {name}: target={angle:g}°" + (f" (before={before:g}°)" if before is not None else ""))

            # support common APIs
            if hasattr(dev, "move_to"):
                dev.move_to(angle)              # EPS300 adapter already blocks by default; Elliptec usually blocks too
            elif hasattr(dev, "move_abs"):
                dev.move_abs(angle)
            else:
                raise RuntimeError(f"{name} device has no move_to/move_abs method")

            if settle_s > 0:
                time.sleep(settle_s)

            after = _read_angle(dev)
            if verify and after is not None:
                err = abs(after - angle)
                if err > tol:
                    raise RuntimeError(f"[rotate_to] {name}: verify failed. target={angle:g}°, after={after:g}°, |Δ|={err:g}° > tol={tol:g}°")
                log(f"[rotate_to] {name}: reached {after:g}° (|Δ|={err:g}°)")
            else:
                log(f"[rotate_to] {name}: move issued" + (f", after={after:g}°" if after is not None else ""))

            return True

        if which == "both":
            ok1 = _move("rotation1")
            ok2 = _move("rotation2")
            if not (ok1 or ok2):
                raise RuntimeError("No rotation mounts connected for 'both'.")
        else:
            if which not in ("rotation1", "rotation2"):
                raise ValueError("which must be 'rotation1', 'rotation2', or 'both'")
            if not _move(which):
                raise RuntimeError(f"Rotation mount '{which}' not connected.")
