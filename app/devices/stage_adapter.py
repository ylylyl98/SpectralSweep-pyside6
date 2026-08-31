from __future__ import annotations

from app.devices.stage_elliptec_adapter import ElliptecLinearStage
from app.devices.stage_newport_adapter import NewportESP300LinearStage
from app.devices.stage_profiles import LinearStageProfile, get_linear_stage_profile


def create_linear_stage(
    backend_key: str,
    address: str,
    *,
    axis: int | None = None,
):
    profile = get_linear_stage_profile(backend_key)
    if profile.backend_key == "elliptec":
        return ElliptecLinearStage(address)
    if profile.backend_key == "esp300":
        resolved_axis = int(axis if axis is not None else profile.default_axis or 1)
        return NewportESP300LinearStage(address, axis=resolved_axis)
    raise ValueError(f"Unsupported linear stage backend: {backend_key!r}")


class LinearStage(ElliptecLinearStage):
    """
    Backward-compatible alias for existing Elliptec-only code paths.

    New code should use ``create_linear_stage()`` and ``get_linear_stage_profile()``.
    """
