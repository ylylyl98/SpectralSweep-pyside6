from __future__ import annotations

from dataclasses import dataclass

from app.devices.stage_elliptec_adapter import ElliptecLinearStage
from app.devices.stage_newport_adapter import NewportESP300LinearStage


@dataclass(frozen=True)
class LinearStageProfile:
    backend_key: str
    display_name: str
    minimum_position: float
    maximum_position: float
    position_unit: str
    default_address_kind: str
    default_axis: int | None = None


_PROFILES: dict[str, LinearStageProfile] = {
    "elliptec": LinearStageProfile(
        backend_key="elliptec",
        display_name="Thorlabs Elliptec",
        minimum_position=ElliptecLinearStage.minimum_position,
        maximum_position=ElliptecLinearStage.maximum_position,
        position_unit="stage units",
        default_address_kind=ElliptecLinearStage.default_address_kind,
        default_axis=None,
    ),
    "esp300": LinearStageProfile(
        backend_key="esp300",
        display_name="Newport ESP300",
        minimum_position=NewportESP300LinearStage.minimum_position,
        maximum_position=NewportESP300LinearStage.maximum_position,
        position_unit="mm",
        default_address_kind=NewportESP300LinearStage.default_address_kind,
        default_axis=NewportESP300LinearStage.default_axis,
    ),
}


def get_linear_stage_profile(backend_key: str) -> LinearStageProfile:
    key = (backend_key or "elliptec").strip().lower()
    try:
        return _PROFILES[key]
    except KeyError as exc:
        raise ValueError(f"Unsupported linear stage backend: {backend_key!r}") from exc


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
