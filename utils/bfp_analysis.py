from __future__ import annotations

from typing import Optional

import numpy as np


def moving_average_1d(y: np.ndarray, window: int) -> np.ndarray:
    y = np.asarray(y, dtype=float)
    if y.size < 3 or window <= 1:
        return y.copy()
    window = int(window)
    if window < 1:
        window = 1
    if window % 2 == 0:
        window += 1
    if window > y.size:
        window = y.size if y.size % 2 == 1 else y.size - 1
    if window <= 1:
        return y.copy()
    pad = window // 2
    y_pad = np.pad(y, (pad, pad), mode="edge")
    kernel = np.ones(window, dtype=float) / float(window)
    return np.convolve(y_pad, kernel, mode="valid")


def repeated_gradient(x: np.ndarray, y: np.ndarray, order: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = np.asarray(y, dtype=float).copy()
    order = max(0, int(order))
    for _ in range(order):
        out = np.gradient(out, x, edge_order=1)
    return out


def validate_odd_window(value: object, npts: int, field_name: str = "Smooth window") -> int:
    if value in (None, ""):
        return 1
    try:
        window = int(round(float(value)))
    except Exception as exc:
        raise ValueError(f"{field_name} must be a number or blank.") from exc
    if window < 1:
        raise ValueError(f"{field_name} must be >= 1.")
    if window % 2 == 0:
        window += 1
    if npts >= 3 and window > npts:
        window = npts if npts % 2 == 1 else npts - 1
    return max(1, window)


def limits_from_pair(arr: np.ndarray, lo: Optional[float], hi: Optional[float]) -> tuple[float, float]:
    arr = np.asarray(arr, dtype=float)
    lo_use = float(np.nanmin(arr)) if lo is None else float(lo)
    hi_use = float(np.nanmax(arr)) if hi is None else float(hi)
    if hi_use < lo_use:
        lo_use, hi_use = hi_use, lo_use
    return lo_use, hi_use


def rc_suffix_brc(mode: str, order: int) -> str:
    if mode == "contrast":
        return "RC"
    if mode == "subtract":
        return "Sub"
    if mode == "division":
        return "Div"
    if mode == "derivative":
        return f"RC_D{int(order)}"
    return "Result"


def rc_suffix_frc(mode: str, display_key: str) -> str:
    if display_key == "background":
        return "BG"
    if display_key == "sample":
        return "Sample"
    if mode == "subtract":
        return "SubBG"
    if mode == "division":
        return "Div"
    return "RC"


def compute_rc_contrast(sample: np.ndarray, bg: np.ndarray, scale: float = 1.0) -> np.ndarray:
    sample = np.asarray(sample, dtype=float)
    bg_scaled = float(scale) * np.asarray(bg, dtype=float)
    return (bg_scaled - sample) / (bg_scaled + 1e-12)


def compute_rc_subtract(sample: np.ndarray, bg: np.ndarray, scale: float = 1.0) -> np.ndarray:
    sample = np.asarray(sample, dtype=float)
    bg_scaled = float(scale) * np.asarray(bg, dtype=float)
    return sample - bg_scaled


def compute_rc_division(sample: np.ndarray, bg: np.ndarray, scale: float = 1.0) -> np.ndarray:
    sample = np.asarray(sample, dtype=float)
    bg_scaled = float(scale) * np.asarray(bg, dtype=float)
    return sample / (bg_scaled + 1e-12)
