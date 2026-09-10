"""Shared stage-position to optical-power calibration utilities."""
from __future__ import annotations

import math
import copy
from typing import Iterable
import numpy as np
from utils.config import NDCalibrationConfig, cfg


def _clean(position: Iterable[float], power: Iterable[float]):
    x = np.asarray(list(position), dtype=float).ravel()
    y = np.asarray(list(power), dtype=float).ravel()
    if x.size != y.size or x.size < 2:
        raise ValueError("Calibration needs at least two position/power points.")
    if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)) or np.any(y <= 0):
        raise ValueError("Calibration points must be finite and power must be positive.")
    order = np.argsort(x)
    x, y = x[order], y[order]
    if np.any(np.diff(x) <= 0):
        raise ValueError("Calibration positions must be unique.")
    dy = np.diff(y)
    # Every interval must have a direction.  Allowing a zero delta creates a
    # flat segment which cannot be inverted uniquely.
    if not (np.all(dy > 0) or np.all(dy < 0)):
        raise ValueError("Calibration power must be monotonic over the usable range.")
    return x, np.log(y)


def predict_power(position, positions, powers, *, reference_position=None, reference_power=None):
    """Predict corrected sample power (or normalized transmission)."""
    x, log_y = _clean(positions, powers)
    p = np.asarray(position, dtype=float)
    if np.any(~np.isfinite(p)):
        raise ValueError("Position must be finite.")
    if np.any(p < x[0]) or np.any(p > x[-1]):
        raise ValueError(f"Position is outside calibrated range [{x[0]:g}, {x[-1]:g}].")
    relative = np.exp(np.interp(p, x, log_y))
    if reference_position is None or reference_power is None:
        return relative / math.exp(log_y[0])
    ref = float(reference_power)
    if not math.isfinite(ref) or ref <= 0:
        raise ValueError("Reference power must be finite and positive.")
    rp = float(reference_position)
    if not math.isfinite(rp):
        raise ValueError("Reference position must be finite.")
    if rp < x[0] or rp > x[-1]:
        raise ValueError("Reference position is outside calibrated range.")
    ref_curve = math.exp(float(np.interp(rp, x, log_y)))
    return ref * relative / ref_curve


def positions_for_power(power, positions, powers, *, reference_position=None, reference_power=None):
    """Invert a monotonic calibration, returning stage positions."""
    x, log_y = _clean(positions, powers)
    curve = predict_power(x, x, np.exp(log_y), reference_position=reference_position,
                          reference_power=reference_power)
    target = np.asarray(power, dtype=float)
    if np.any(~np.isfinite(target)) or np.any(target <= 0):
        raise ValueError("Target powers must be finite and positive.")
    if curve[0] > curve[-1]:
        curve, x = curve[::-1], x[::-1]
    # Endpoint values can differ by a few ulps after reference scaling.
    # Accept only that numerical noise and clamp before log interpolation;
    # substantive extrapolation remains rejected.
    eps = np.finfo(float).eps * 64.0
    lower_tol = max(np.finfo(float).tiny, abs(float(curve[0]))) * eps
    upper_tol = max(np.finfo(float).tiny, abs(float(curve[-1]))) * eps
    if np.any(target < curve[0] - lower_tol) or np.any(target > curve[-1] + upper_tol):
        raise ValueError(f"Target power is outside calibrated range [{curve[0]:g}, {curve[-1]:g}].")
    target = np.clip(target, curve[0], curve[-1])
    # Match the forward log interpolation used for the calibration itself.
    return np.interp(np.log(target), np.log(curve), x)


def make_power_points(start, stop, count, spacing="linear"):
    start, stop, count = float(start), float(stop), int(count)
    if count < 1 or not all(math.isfinite(v) for v in (start, stop)) or start <= 0 or stop <= 0:
        raise ValueError("Power range is invalid; values must be positive and finite.")
    if count == 1:
        return np.array([start])
    return (np.geomspace(start, stop, count) if str(spacing).lower() == "log"
            else np.linspace(start, stop, count))


def save_calibration(positions, powers, *, reference_position=None,
                     reference_power_uw=None, raw_powers=None, profile_name=None,
                     wavelength_nm=None, correction_factor=None, persist=True):
    """Validate and install the app-wide calibration from corrected readings."""
    try:
        positions = list(positions)
        powers = list(powers)
    except TypeError as exc:
        raise ValueError("Calibration positions and powers must be iterable.") from exc
    x, log_y = _clean(positions, powers)
    source_positions = np.asarray(positions, dtype=float).ravel()
    if raw_powers is not None:
        try:
            raw = np.asarray(list(raw_powers), dtype=float).ravel()
        except (TypeError, ValueError) as exc:
            raise ValueError("Raw powers must be finite and positive.") from exc
        if raw.size != x.size:
            raise ValueError("Raw powers must match calibration points.")
        if not np.all(np.isfinite(raw)) or np.any(raw <= 0):
            raise ValueError("Raw powers must be finite and positive.")
        raw = raw[np.argsort(source_positions)]
    else:
        raw = None
    if reference_position is not None:
        ref_position = float(reference_position)
        if not math.isfinite(ref_position):
            raise ValueError("Reference position must be finite.")
        if ref_position < x[0] or ref_position > x[-1]:
            raise ValueError("Reference position is outside calibrated range.")
    if reference_power_uw is not None and (not math.isfinite(float(reference_power_uw)) or float(reference_power_uw) <= 0):
        raise ValueError("Reference power must be finite and positive.")
    if correction_factor is not None:
        factor = float(correction_factor)
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError("Power correction factor must be finite and positive.")
    else:
        factor = float(cfg.nd_calibration.correction_factor)
        if not math.isfinite(factor) or factor <= 0:
            factor = 1.0
    if wavelength_nm is not None:
        wavelength = float(wavelength_nm)
        if not math.isfinite(wavelength) or wavelength <= 0:
            raise ValueError("Wavelength must be finite and positive.")
    else:
        wavelength = cfg.nd_calibration.wavelength_nm
    profile = None if profile_name is None else str(profile_name).strip()
    if profile_name is not None and not profile:
        raise ValueError("Calibration profile name must not be empty.")

    # Build a complete candidate before touching the singleton.  If the final
    # atomic save fails, put the original object back exactly as it was.
    previous = cfg.nd_calibration
    candidate = copy.deepcopy(previous)
    candidate.positions = x.tolist()
    candidate.powers = np.exp(log_y).tolist()
    candidate.raw_powers = [] if raw is None else raw.tolist()
    if profile_name is not None:
        candidate.profile_name = profile
    if wavelength_nm is not None:
        candidate.wavelength_nm = wavelength
    candidate.correction_factor = factor
    candidate.reference_position = (None if reference_position is None else float(reference_position))
    candidate.reference_power_uw = (None if reference_power_uw is None else float(reference_power_uw))
    if not isinstance(candidate, NDCalibrationConfig):
        raise TypeError("Invalid calibration candidate")
    cfg.nd_calibration = candidate
    try:
        if persist:
            cfg.save()
    except Exception:
        cfg.nd_calibration = previous
        raise
    return candidate
