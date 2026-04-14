from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import numpy as np
import pandas as pd


@dataclass
class FullImageData:
    wl: np.ndarray
    y: np.ndarray
    image: np.ndarray
    orientation: str


def load_numeric_csv(path: str | Path) -> np.ndarray:
    data = np.genfromtxt(path, delimiter=",", dtype=float)
    if np.isnan(data).all():
        data = np.genfromtxt(path, delimiter=None, dtype=float)
    if data.ndim == 1:
        data = data.reshape(-1, 1)
    return data


def looks_monotonic(arr: np.ndarray) -> bool:
    arr = np.asarray(arr, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size < 4:
        return False
    diffs = np.diff(arr)
    frac_pos = np.mean(diffs > 0)
    frac_neg = np.mean(diffs < 0)
    return max(frac_pos, frac_neg) > 0.85


def looks_like_pixel_axis(arr: np.ndarray) -> bool:
    arr = np.asarray(arr, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size < 4 or not looks_monotonic(arr):
        return False
    if float(np.nanmin(arr)) < -1e-6 or float(np.nanmax(arr)) > 512.5:
        return False
    rounded = np.round(arr)
    return bool(np.nanmax(np.abs(arr - rounded)) < 1e-3)


def load_binned_csv(path: str | Path) -> tuple[np.ndarray, np.ndarray]:
    data = load_numeric_csv(path)
    if data.shape[1] < 2:
        raise ValueError("Binned CSV must have at least 2 columns: wavelength, intensity.")
    wl = data[:, 0].astype(float)
    intensity = data[:, 1].astype(float)
    mask = np.isfinite(wl) & np.isfinite(intensity)
    wl = wl[mask]
    intensity = intensity[mask]
    if wl.size < 2:
        raise ValueError("Binned CSV does not contain enough valid points.")
    idx = np.argsort(wl)
    return wl[idx], intensity[idx]


def _try_load_header_full_image_csv(path: str | Path) -> FullImageData | None:
    try:
        df = pd.read_csv(path)
    except Exception:
        return None
    if df.shape[1] < 3:
        return None
    first_col = str(df.columns[0]).strip().lower()
    if first_col not in {"wavelength_nm", "wavelength", "wl"}:
        return None
    wl = pd.to_numeric(df.iloc[:, 0], errors="coerce").to_numpy(dtype=float)
    y_values: list[float] = []
    value_cols: list[str] = []
    for col in df.columns[1:]:
        try:
            y_values.append(float(str(col).strip()))
            value_cols.append(col)
        except Exception:
            continue
    if len(value_cols) < 2:
        return None
    image = df[value_cols].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float).T
    y = np.asarray(y_values, dtype=float)
    mw = np.isfinite(wl)
    my = np.isfinite(y)
    wl = wl[mw]
    y = y[my]
    image = image[np.ix_(my, mw)]
    if wl.size < 2 or y.size < 2:
        return None
    idx = np.argsort(wl)
    jdx = np.argsort(y)
    wl = wl[idx]
    y = y[jdx]
    image = image[np.ix_(jdx, idx)]
    return FullImageData(wl=wl, y=y, image=image, orientation="header_wl_col_y")


def detect_full_image_format(data: np.ndarray) -> str | None:
    if data.ndim != 2 or data.shape[0] < 4 or data.shape[1] < 4:
        return None
    corner_ok = not np.isfinite(data[0, 0]) or data[0, 0] == 0
    if not corner_ok:
        return None
    row_header = data[0, 1:]
    col_header = data[1:, 0]
    img = data[1:, 1:]
    if np.isfinite(img).sum() < 10:
        return None
    row_is_pixel = looks_like_pixel_axis(row_header)
    col_is_pixel = looks_like_pixel_axis(col_header)
    row_is_wl = looks_monotonic(row_header)
    col_is_wl = looks_monotonic(col_header)
    row_all_finite = np.isfinite(row_header).all()
    col_all_finite = np.isfinite(col_header).all()
    if row_is_pixel and col_is_wl and col_all_finite:
        return "row_y_col_wl"
    if col_is_pixel and row_is_wl and row_all_finite:
        return "row_wl_col_y"
    if row_is_wl and col_all_finite:
        return "row_wl_col_y"
    if col_is_wl and row_all_finite:
        return "row_y_col_wl"
    return None


def load_full_image_csv(path: str | Path) -> FullImageData:
    header_format = _try_load_header_full_image_csv(path)
    if header_format is not None:
        return header_format
    data = load_numeric_csv(path)
    orientation = detect_full_image_format(data)
    if orientation is None:
        raise ValueError("Not recognized as full-sensor image CSV.")
    if orientation == "row_wl_col_y":
        wl = data[0, 1:].astype(float)
        y = data[1:, 0].astype(float)
        image = data[1:, 1:].astype(float)
    else:
        y = data[0, 1:].astype(float)
        wl = data[1:, 0].astype(float)
        image = data[1:, 1:].astype(float).T
    mw = np.isfinite(wl)
    my = np.isfinite(y)
    wl = wl[mw]
    y = y[my]
    image = image[np.ix_(my, mw)]
    if wl.size < 2 or y.size < 2:
        raise ValueError("Full-image CSV does not contain enough valid wavelength/y points.")
    idx = np.argsort(wl)
    jdx = np.argsort(y)
    wl = wl[idx]
    y = y[jdx]
    image = image[np.ix_(jdx, idx)]
    return FullImageData(wl=wl, y=y, image=image, orientation=orientation)


def detect_csv_mode(path: str | Path) -> Literal["binned", "full"]:
    if _try_load_header_full_image_csv(path) is not None:
        return "full"
    data = load_numeric_csv(path)
    if detect_full_image_format(data) is not None:
        return "full"
    return "binned"


def interpolate_1d_to_reference(
    wl_ref: np.ndarray, wl_other: np.ndarray, y_other: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    wl_min = max(np.min(wl_ref), np.min(wl_other))
    wl_max = min(np.max(wl_ref), np.max(wl_other))
    mask = (wl_ref >= wl_min) & (wl_ref <= wl_max)
    wl_common = np.asarray(wl_ref, dtype=float)[mask]
    if wl_common.size < 2:
        raise ValueError("Sample and background do not overlap in wavelength.")
    y_interp = np.interp(wl_common, wl_other, y_other)
    return wl_common, y_interp


def interp_image_to_grid(
    wl_src: np.ndarray,
    y_src: np.ndarray,
    img_src: np.ndarray,
    wl_tgt: np.ndarray,
    y_tgt: np.ndarray,
) -> np.ndarray:
    wl_src = np.asarray(wl_src, dtype=float)
    y_src = np.asarray(y_src, dtype=float)
    img_src = np.asarray(img_src, dtype=float)
    wl_tgt = np.asarray(wl_tgt, dtype=float)
    y_tgt = np.asarray(y_tgt, dtype=float)
    if not looks_monotonic(wl_src) or not looks_monotonic(y_src):
        raise ValueError("Full-image axes must be monotonic for interpolation.")
    img_w = np.empty((img_src.shape[0], wl_tgt.size), dtype=float)
    for iy in range(img_src.shape[0]):
        img_w[iy, :] = np.interp(wl_tgt, wl_src, img_src[iy, :])
    img_out = np.empty((y_tgt.size, wl_tgt.size), dtype=float)
    for ix in range(wl_tgt.size):
        img_out[:, ix] = np.interp(y_tgt, y_src, img_w[:, ix])
    return img_out


def save_binned_csv(path: str | Path, wl: np.ndarray, intensity: np.ndarray) -> None:
    data = np.column_stack([wl, intensity])
    np.savetxt(path, data, delimiter=",", fmt="%.8f")


def save_full_image_csv(path: str | Path, wl: np.ndarray, y: np.ndarray, image: np.ndarray) -> None:
    wl = np.asarray(wl, dtype=float).ravel()
    y = np.asarray(y, dtype=float).ravel()
    image = np.asarray(image, dtype=float)
    if image.ndim != 2:
        raise ValueError("Full-image save expects a 2D array.")
    if image.shape != (y.size, wl.size):
        raise ValueError(f"Image shape {image.shape} does not match y/wavelength sizes {(y.size, wl.size)}.")
    y_labels = [str(int(v)) if float(v).is_integer() else f"{float(v):g}" for v in y]
    df = pd.DataFrame(image.T, columns=y_labels)
    df.insert(0, "wavelength_nm", wl)
    df.to_csv(path, index=False, float_format="%.8f")


def save_csv_atomic(path: str | Path, save_func: Callable, *args) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    save_func(tmp_path, *args)
    if not tmp_path.exists() or tmp_path.stat().st_size == 0:
        raise IOError(f"Failed to create CSV file: {target}")
    try:
        tmp_path.replace(target)
    except PermissionError:
        # Some Windows environments briefly lock the destination during rename.
        save_func(target, *args)
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
