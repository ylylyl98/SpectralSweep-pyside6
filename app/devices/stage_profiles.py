"""Static linear-stage UI metadata with no device-library imports."""

from __future__ import annotations

from dataclasses import dataclass


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
        minimum_position=0.0,
        maximum_position=3600.0,
        position_unit="stage units",
        default_address_kind="com",
        default_axis=None,
    ),
    "esp300": LinearStageProfile(
        backend_key="esp300",
        display_name="Newport ESP300",
        minimum_position=0.0,
        maximum_position=50.0,
        position_unit="mm",
        default_address_kind="visa",
        default_axis=3,
    ),
}


def get_linear_stage_profile(backend_key: str) -> LinearStageProfile:
    key = (backend_key or "elliptec").strip().lower()
    try:
        return _PROFILES[key]
    except KeyError as exc:
        raise ValueError(f"Unsupported linear stage backend: {backend_key!r}") from exc
