from app.steps.registry import register_step

@register_step("rotate_to")
def rotate_to(ctx, devices, **kwargs):
    """
    Rotate one or both mounts.
    kwargs:
      - angle_deg: float
      - which: 'rotation1' | 'rotation2' | 'both' (default 'rotation1')
    """
    angle = float(kwargs.get("angle_deg"))
    which = str(kwargs.get("which", "rotation1")).lower()

    def _do(name):
        dev = devices.get(name)
        if dev:
            dev.move_to(angle)
            return True
        return False

    if which == "both":
        ok1 = _do("rotation1")
        ok2 = _do("rotation2")
        if not (ok1 or ok2):
            raise RuntimeError("No rotation mounts connected for 'both'.")
        return {"rotation1_deg": angle if ok1 else None, "rotation2_deg": angle if ok2 else None}

    if not _do(which):
        raise RuntimeError(f"Rotation mount '{which}' not connected.")
    return {f"{which}_deg": angle}
