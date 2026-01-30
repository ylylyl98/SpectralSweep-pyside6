# app/ui_streamlit/views/bfp.py
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple, Optional

import numpy as np
import pandas as pd
import streamlit as st

import matplotlib
matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt


BASE_OUT = Path(r"D:\instrument_control_v3_1")
BASE_OUT.mkdir(parents=True, exist_ok=True)


# -----------------------------------------------------------------------------
# Small UI helpers
# -----------------------------------------------------------------------------
def _st_pyplot(fig):
    """Streamlit pyplot compat across versions."""
    try:
        st.pyplot(fig, use_container_width=True)
    except TypeError:
        st.pyplot(fig)


def _status_line(kind: str, text: str):
    """
    Compact status line to avoid huge st.success/st.info boxes.
    kind: 'info' | 'ok' | 'warn' | 'err'
    """
    cls = {
        "info": "bfp-info",
        "ok": "bfp-ok",
        "warn": "bfp-warn",
        "err": "bfp-err",
    }.get(kind, "bfp-info")
    st.markdown(
        f"<div class='bfp-status {cls}'>{text}</div>",
        unsafe_allow_html=True,
    )


# -----------------------------------------------------------------------------
# Cache loader (background CSVs)
# -----------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def _cached_load_bfp_csv(path_str: str, mtime: float) -> tuple[np.ndarray, np.ndarray]:
    """
    Load CSV saved by this page.

    Supports:
    - 1D: wavelength_nm,intensity  OR  pixel,intensity
    - 2D (WIDE, preferred):
        wavelength_nm | 0 | 1 | 2 | ... (y headers)
        returns: image (H,W) and wavelengths (W,)
    - 2D (LONG): wavelength_nm,y_px,intensity
    - Legacy 2D: index=y_px, columns=wavelength headers
    """
    p = Path(path_str)
    df0 = pd.read_csv(p)

    cols_lower = {c.strip().lower(): c for c in df0.columns}

    # -------------------------
    # 2D LONG: wavelength_nm,y_px,intensity
    # -------------------------
    if ("wavelength_nm" in cols_lower) and ("y_px" in cols_lower) and ("intensity" in cols_lower):
        wl_col = cols_lower["wavelength_nm"]
        y_col = cols_lower["y_px"]
        i_col = cols_lower["intensity"]

        piv = df0.pivot(index=y_col, columns=wl_col, values=i_col)
        piv = piv.sort_index(axis=0)  # y asc
        piv = piv.sort_index(axis=1)  # wl asc

        wls = piv.columns.to_numpy(dtype=float)           # (W,)
        arr = piv.to_numpy(dtype=float)                   # (H,W)
        return arr, wls

    # -------------------------
    # 1D wavelength_nm,intensity
    # -------------------------
    if ("wavelength_nm" in cols_lower) and ("intensity" in cols_lower) and ("y_px" not in cols_lower):
        wl_col = cols_lower["wavelength_nm"]
        i_col = cols_lower["intensity"]
        wls = df0[wl_col].to_numpy(dtype=float)
        y = df0[i_col].to_numpy(dtype=float)
        return y, wls

    # -------------------------
    # 1D pixel,intensity
    # -------------------------
    if ("pixel" in cols_lower) and ("intensity" in cols_lower) and ("wavelength_nm" not in cols_lower):
        i_col = cols_lower["intensity"]
        y = df0[i_col].to_numpy(dtype=float)
        return y, np.array([], dtype=float)

    # -------------------------
    # 2D WIDE (preferred):
    # wavelength_nm | 0 | 1 | 2 | ...
    # -------------------------
    wl_key = None
    if "wavelength_nm" in cols_lower:
        wl_key = cols_lower["wavelength_nm"]
    elif "x_px" in cols_lower:
        wl_key = cols_lower["x_px"]

    if wl_key is not None and ("y_px" not in cols_lower) and ("intensity" not in cols_lower):
        wls = df0[wl_key].to_numpy(dtype=float)  # (W,)

        # all other columns are y headers
        y_cols = [c for c in df0.columns if c != wl_key]

        # numeric sort if possible (0,1,2,...)
        def _try_int(s: str):
            try:
                return int(str(s).strip())
            except Exception:
                return None

        parsed = [(_try_int(c), c) for c in y_cols]
        if all(v is not None for v, _ in parsed):
            y_cols = [c for _, c in sorted(parsed, key=lambda t: t[0])]

        mat = df0[y_cols].to_numpy(dtype=float)  # (W,H)
        arr = mat.T                              # -> (H,W) standard image
        return arr, wls

    # -------------------------
    # Legacy 2D matrix style: index=y_px, columns=wavelength headers
    # -------------------------
    df = pd.read_csv(p, index_col=0)

    wls = []
    ok = True
    for c in df.columns:
        try:
            wls.append(float(c))
        except Exception:
            ok = False
            break
    wls = np.asarray(wls, dtype=float) if ok else np.array([], dtype=float)

    arr = df.to_numpy(dtype=float)
    if arr.shape[0] == 1:
        return arr[0].astype(float, copy=False), wls
    return arr.astype(float, copy=False), wls


# -----------------------------------------------------------------------------
# Filename helpers
# -----------------------------------------------------------------------------
def _sanitize_filename(s: str) -> str:
    s = str(s or "").strip()
    try:
        from app.utils import sanitize_filename  # type: ignore
        return sanitize_filename(s)
    except Exception:
        pass

    import re
    s = re.sub(r"[^\w\-.]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "unnamed"


def _now_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _build_stem(sample: str, note: str, suffix: str, ts: str, run_idx: int) -> str:
    parts = []
    if sample:
        parts.append(_sanitize_filename(sample))
    if note:
        parts.append(_sanitize_filename(note))
    if suffix:
        parts.append(_sanitize_filename(suffix))
    parts.append(ts)
    parts.append(f"{int(run_idx):03d}")
    return "_".join([p for p in parts if p])


def _sample_out_dir(sample: str) -> Path:
    s = _sanitize_filename(sample)
    return (BASE_OUT / s) if s else BASE_OUT


def _list_csvs(folder: Path) -> list[Path]:
    folder = Path(folder)
    if not folder.exists():
        return []
    csvs = [p for p in folder.glob("*.csv") if p.is_file()]
    csvs.sort(key=lambda p: p.stat().st_mtime, reverse=True)  # newest first
    return csvs


# -----------------------------------------------------------------------------
# LightField settings apply (ROI + center + exposure + EPF/accums)
# -----------------------------------------------------------------------------
def _apply_ccd_roi(spec: Any, mode: str) -> Tuple[bool, str]:
    m = (mode or "").strip().lower()
    want_full = m.startswith("full")
    want_line = ("bin" in m) or ("line" in m)

    setup = getattr(spec, "setup", None)
    if setup is None:
        return False, "Spectrometer adapter has no `.setup` (LF6Setup)."

    try:
        if want_full:
            if hasattr(setup, "change_roi_FullSensor"):
                setup.change_roi_FullSensor()
                return True, "ROI set to FullSensor"
            return False, "LF6Setup missing change_roi_FullSensor()"

        if want_line:
            if hasattr(setup, "change_roi_LineSensor"):
                setup.change_roi_LineSensor()
                return True, "ROI set to LineSensor (mapped from 'Bin all')"
            return False, "LF6Setup missing change_roi_LineSensor()"

        return False, f"Unknown ROI mode: {mode!r}"
    except Exception as e:
        return False, f"ROI apply failed: {e}"


def _apply_center(spec: Any, center_nm: float) -> Tuple[bool, str]:
    try:
        if hasattr(spec, "change_spectra_center") and callable(getattr(spec, "change_spectra_center")):
            spec.change_spectra_center(center_nm)
            return True, f"Center {float(center_nm):.1f} nm"
        setup = getattr(spec, "setup", None)
        if setup is not None and hasattr(setup, "change_spectra_center"):
            setup.change_spectra_center(center_nm)
            return True, f"Center {float(center_nm):.1f} nm"
        return False, "No change_spectra_center() found."
    except Exception as e:
        return False, f"Center apply failed: {e}"


def _apply_exposure(spec: Any, exposure_ms: float) -> Tuple[bool, str]:
    try:
        setup = getattr(spec, "setup", None)
        target = spec if hasattr(spec, "change_expose_time") else setup
        if target is not None and hasattr(target, "change_expose_time"):
            target.change_expose_time(exposure_ms)
            return True, f"Exposure {float(exposure_ms):g} ms"
        return False, "No change_expose_time() found."
    except Exception as e:
        return False, f"Exposure apply failed: {e}"


def _apply_epf(spec: Any, epf: int) -> Tuple[bool, str]:
    try:
        if hasattr(spec, "set_accumulations") and callable(getattr(spec, "set_accumulations")):
            spec.set_accumulations(int(epf))
            return True, f"EPF/Accums {int(epf)}"
        setup = getattr(spec, "setup", None)
        if setup is not None and hasattr(setup, "change_frame_to_combine"):
            setup.change_frame_to_combine(int(epf))
            return True, f"EPF/Accums {int(epf)}"
        return False, "No set_accumulations() / change_frame_to_combine() found."
    except Exception as e:
        return False, f"EPF apply failed: {e}"


def _apply_all_settings(spec: Any, roi_mode: str, center_nm: float, exposure_ms: float, epf: int) -> Tuple[bool, str]:
    msgs = []
    ok_all = True

    ok, msg = _apply_ccd_roi(spec, roi_mode); msgs.append(msg); ok_all &= ok
    ok, msg = _apply_exposure(spec, exposure_ms); msgs.append(msg); ok_all &= ok
    ok, msg = _apply_center(spec, center_nm); msgs.append(msg); ok_all &= ok
    ok, msg = _apply_epf(spec, epf); msgs.append(msg); ok_all &= ok

    return ok_all, " | ".join(msgs)


def _acquire_camera_output(spec: Any, roi_mode: str) -> np.ndarray:
    setup = getattr(spec, "setup", None)
    if setup is None:
        raise RuntimeError("Spectrometer adapter has no setup (LF6Setup).")

    if (roi_mode or "").strip().lower().startswith("full"):
        if hasattr(setup, "acquire_2d"):
            return np.asarray(setup.acquire_2d())
        return np.asarray(setup.acquire())

    return np.asarray(setup.acquire())


def _get_wavelengths_nm(spec: Any) -> np.ndarray:
    try:
        if hasattr(spec, "calibration_wavelengths"):
            return np.asarray(spec.calibration_wavelengths(force=True), dtype=float).ravel()
    except Exception:
        pass

    setup = getattr(spec, "setup", None)
    try:
        if setup is not None and hasattr(setup, "get_wavelength_calibration"):
            return np.asarray(setup.get_wavelength_calibration(), dtype=float).ravel()
    except Exception:
        pass

    return np.array([], dtype=float)


def _readback_roi_string(spec: Any) -> Optional[str]:
    setup = getattr(spec, "setup", None)
    exp = getattr(setup, "experiment", None) if setup is not None else None
    if exp is None:
        return None
    try:
        import lf6_automation
        key = lf6_automation.CameraSettings.ReadoutControlRegionsOfInterestSelection
        if hasattr(exp, "Exists") and exp.Exists(key):
            return str(exp.GetValue(key))
    except Exception:
        return None
    return None


# -----------------------------------------------------------------------------
# Background math + wavelength checks
# -----------------------------------------------------------------------------
def _wavelengths_match(w1: np.ndarray, w2: np.ndarray, tol_nm: float) -> tuple[bool, str]:
    w1 = np.asarray(w1, dtype=float).ravel()
    w2 = np.asarray(w2, dtype=float).ravel()
    if w1.size == 0 or w2.size == 0:
        return False, "Wavelength vector missing."
    if w1.size != w2.size:
        return False, f"Length mismatch: meas={w1.size}, bg={w2.size}"
    d = np.nanmax(np.abs(w1 - w2))
    if not np.isfinite(d):
        return False, "Wavelength compare failed (NaN)."
    if d > tol_nm:
        return False, f"Max |Δλ|={d:.4g} nm > tol {tol_nm:g} nm"
    return True, f"Max |Δλ|={d:.4g} nm"


def _orient_2d_to_wavelength_columns(arr2d: np.ndarray, wls: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    Ensure 2D data has wavelength along columns if possible.
    If wavelength length matches rows instead, transpose.
    Returns (oriented_image, wls_for_columns_or_empty).
    """
    img = np.asarray(arr2d)
    wls = np.asarray(wls, dtype=float).ravel() if wls is not None else np.array([], dtype=float)

    if img.ndim != 2 or wls.size == 0:
        return img, np.array([], dtype=float)

    if wls.size == img.shape[1]:
        return img, wls
    if wls.size == img.shape[0] and wls.size != img.shape[1]:
        return img.T, wls

    return img, np.array([], dtype=float)


def _apply_background_rr0(
    meas: np.ndarray,
    meas_wls: np.ndarray,
    bg: np.ndarray,
    bg_wls: np.ndarray,
    tol_nm: float,
) -> tuple[bool, np.ndarray, str]:
    meas = np.asarray(meas)
    bg = np.asarray(bg)

    if meas.ndim != bg.ndim:
        return False, meas, f"NOT applied: ndim mismatch meas={meas.ndim}D bg={bg.ndim}D"

    ok_w, wmsg = _wavelengths_match(meas_wls, bg_wls, tol_nm=tol_nm)
    if not ok_w:
        return False, meas, f"NOT applied: wavelength mismatch ({wmsg})"

    if meas.shape != bg.shape:
        return False, meas, f"NOT applied: shape mismatch meas={meas.shape} bg={bg.shape}"

    r0 = bg.astype(np.float64, copy=False)
    r = meas.astype(np.float64, copy=False)

    eps = 1e-12
    out = np.where(np.abs(r0) > eps, (r - r0) / r0, np.nan)
    return True, out, "Applied: (R-R0)/R0"


# -----------------------------------------------------------------------------
# Auto->Manual color-limit helper
# -----------------------------------------------------------------------------
def _compute_auto_limits_raw(data: np.ndarray, scale: str) -> tuple[float, float]:
    arr = np.asarray(data, dtype=np.float64)

    if scale.lower().startswith("log"):
        show = np.log1p(np.clip(arr, 0, None))
        lo_s, hi_s = np.nanpercentile(show, [1, 99])
        lo = float(np.expm1(max(lo_s, 0.0)))
        hi = float(np.expm1(max(hi_s, 0.0)))
    else:
        lo, hi = np.nanpercentile(arr, [1, 99])
        lo = float(lo)
        hi = float(hi)

    if not np.isfinite(lo):
        lo = 0.0
    if not np.isfinite(hi) or hi <= lo:
        hi = lo + 1.0
    return lo, hi


def _on_autoscale_change():
    """When Auto turns OFF, prefill vmin/vmax with last auto limits (raw units)."""
    if bool(st.session_state.get("bfp_autoscale", True)):
        return
    lo = float(st.session_state.get("bfp_last_auto_vmin_raw", 0.0))
    hi = float(st.session_state.get("bfp_last_auto_vmax_raw", lo + 1.0))
    if hi <= lo:
        hi = lo + 1.0
    st.session_state["bfp_vmin"] = lo
    st.session_state["bfp_vmax"] = hi


# -----------------------------------------------------------------------------
# CSV save (UPDATED: 2D = WIDE transpose with wavelength as first column)
# -----------------------------------------------------------------------------
def _save_csv_with_wavelengths(path: Path, data: np.ndarray, wls: np.ndarray) -> None:
    arr = np.asarray(data)
    wls = np.asarray(wls, dtype=float).ravel() if wls is not None else np.array([], dtype=float)

    # 1D: wavelength_nm,intensity (or pixel,intensity)
    if arr.ndim == 1:
        y = arr.astype(float, copy=False).ravel()
        if wls.size == y.size:
            df = pd.DataFrame({"wavelength_nm": wls, "intensity": y})
        else:
            df = pd.DataFrame({"pixel": np.arange(y.size, dtype=int), "intensity": y})
        df.to_csv(path, index=False)
        return

    # 2D: WIDE transpose
    # columns: wavelength_nm, 0,1,2,... (y headers)
    # rows: wavelength points
    if arr.ndim == 2:
        img = arr.astype(float, copy=False)  # (H,W) ideally (y, wavelength)

        # orient so columns match wavelength if possible
        img, wls_x = _orient_2d_to_wavelength_columns(img, wls)

        H, W = img.shape

        if wls_x.size == W:
            x_col = wls_x
            x_name = "wavelength_nm"
        else:
            x_col = np.arange(W, dtype=int).astype(float)
            x_name = "x_px"

        # Transpose to (W,H) so each row is a wavelength, columns are y=0..H-1
        mat = img.T  # (W,H)

        df = pd.DataFrame(mat, columns=[str(i) for i in range(H)])
        df.insert(0, x_name, x_col)
        df.to_csv(path, index=False)
        return

    raise ValueError(f"Unsupported data shape: {arr.shape}")


# -----------------------------------------------------------------------------
# Save PNG snapshot matching current display settings
# -----------------------------------------------------------------------------
def _save_png_from_current(path_png: Path, data: np.ndarray, spec: Any) -> None:
    arr = np.asarray(data)
    wls = _get_wavelengths_nm(spec)

    autoscale = bool(st.session_state.get("bfp_autoscale", True))
    scale = str(st.session_state.get("bfp_disp_scale", "Linear"))
    cmap = str(st.session_state.get("bfp_cmap", "gray"))
    show_cb = bool(st.session_state.get("bfp_show_colorbar", True))

    auto_vmin_raw = float(st.session_state.get("bfp_last_auto_vmin_raw", 0.0))
    auto_vmax_raw = float(st.session_state.get("bfp_last_auto_vmax_raw", 1.0))

    if arr.ndim == 2:
        img = np.asarray(arr, dtype=np.float64)

        # orient for wavelength axis
        img, wls_x = _orient_2d_to_wavelength_columns(img, wls)

        if scale.lower().startswith("log"):
            img_show = np.log1p(np.clip(img, 0, None))
            title = "BFP (2D, Log view)"
        else:
            img_show = img
            title = "BFP (2D, Linear view)"

        if autoscale:
            v_lo, v_hi = np.nanpercentile(img_show, [1, 99])
        else:
            raw_lo = float(st.session_state.get("bfp_vmin", auto_vmin_raw))
            raw_hi = float(st.session_state.get("bfp_vmax", auto_vmax_raw))
            if scale.lower().startswith("log"):
                raw_lo = max(raw_lo, 0.0)
                raw_hi = max(raw_hi, raw_lo + 1e-9)
                v_lo, v_hi = np.log1p(raw_lo), np.log1p(raw_hi)
            else:
                v_lo, v_hi = raw_lo, raw_hi

        if not np.isfinite(v_lo): v_lo = float(np.nanmin(img_show))
        if not np.isfinite(v_hi): v_hi = float(np.nanmax(img_show))
        if (not np.isfinite(v_lo)) or (not np.isfinite(v_hi)) or (v_hi <= v_lo):
            v_lo, v_hi = 0.0, 1.0

        fig, ax = plt.subplots(figsize=(7.0, 3.8), dpi=160)

        extent = None
        if wls_x.size > 0 and wls_x.size == img_show.shape[1]:
            extent = (float(wls_x[0]), float(wls_x[-1]), 0, img_show.shape[0] - 1)

        im = ax.imshow(
            img_show,
            origin="lower",
            aspect="auto",
            extent=extent,
            cmap=cmap,
            vmin=v_lo,
            vmax=v_hi,
            interpolation="nearest",
        )

        ax.set_xlabel("Wavelength (nm)" if extent is not None else "X (px)")
        ax.set_ylabel("Y (px)")
        ax.set_title(title, fontsize=10)

        if show_cb:
            fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)

        fig.tight_layout()
        fig.savefig(path_png, dpi=200, bbox_inches="tight")
        plt.close(fig)
        return

    if arr.ndim == 1:
        y = np.asarray(arr, dtype=np.float64).ravel()
        fig, ax = plt.subplots(figsize=(7.5, 3.0), dpi=160)

        if wls is not None and np.asarray(wls).size == y.size:
            ax.plot(np.asarray(wls, dtype=float).ravel(), y, linewidth=1.0)
            ax.set_xlabel("Wavelength (nm)")
        else:
            ax.plot(y, linewidth=1.0)
            ax.set_xlabel("Pixel index")

        ax.set_ylabel("Intensity (a.u.)")
        ax.grid(True, alpha=0.2)

        if autoscale and y.size > 0:
            lo, hi = np.nanpercentile(y, [1, 99])
            pad = 0.05 * (hi - lo) if hi > lo else 1.0
            ax.set_ylim(lo - pad, hi + pad)

        fig.tight_layout()
        fig.savefig(path_png, dpi=200, bbox_inches="tight")
        plt.close(fig)
        return

    raise ValueError(f"Unsupported data shape for PNG save: {arr.shape}")


# -----------------------------------------------------------------------------
# Page render
# -----------------------------------------------------------------------------
def render(devices: Dict[str, Any]) -> None:
    st.header("Back Focal Plane (BFP) Measurement")

    st.markdown(
        """
        <style>
        /* tighter page */
        .block-container {padding-top: 0.8rem; padding-bottom: 0.8rem;}
        div[data-testid="stVerticalBlock"] > div {gap: 0.35rem;}
        h1, h2, h3 {margin-bottom: 0.25rem;}

        /* compact status lines */
        .bfp-status {padding: 0.30rem 0.55rem; border-radius: 0.5rem; margin: 0.20rem 0; font-size: 0.92rem;}
        .bfp-info {background: #E6F4FF; border: 1px solid #91CAFF;}
        .bfp-ok   {background: #E9F7EF; border: 1px solid #B7EBC6;}
        .bfp-warn {background: #FFF7E6; border: 1px solid #FFE58F;}
        .bfp-err  {background: #FFF1F0; border: 1px solid #FFA39E;}

        /* reduce button height a bit */
        div.stButton > button {padding-top: 0.35rem; padding-bottom: 0.35rem;}
        </style>
        """,
        unsafe_allow_html=True,
    )

    spec = devices.get("spectrometer")
    if spec is None or not st.session_state.get("lf6_ready", False):
        st.warning("Connect LF6 / Spectrometer in the sidebar first.")
        return

    # ---- Defaults (your preferences) ----
    st.session_state.setdefault("bfp_roi_mode", "Full sensor")
    st.session_state.setdefault("bfp_center_nm", 885.0)
    st.session_state.setdefault("bfp_exposure_ms", 1000.0)
    st.session_state.setdefault("bfp_epf", 1)

    st.session_state.setdefault("bfp_warmup_after_roi", True)        # default ON
    st.session_state.setdefault("bfp_auto_apply_on_acquire", True)   # default ON

    st.session_state.setdefault("bfp_disp_scale", "Linear")
    st.session_state.setdefault("bfp_cmap", "gray")
    st.session_state.setdefault("bfp_autoscale", True)               # default ON
    st.session_state.setdefault("bfp_show_colorbar", True)           # default ON

    st.session_state.setdefault("bfp_vmin", 0.0)
    st.session_state.setdefault("bfp_vmax", 1000.0)
    st.session_state.setdefault("bfp_last_auto_vmin_raw", 0.0)
    st.session_state.setdefault("bfp_last_auto_vmax_raw", 1000.0)

    st.session_state.setdefault("bfp_sample", "")
    st.session_state.setdefault("bfp_note", "")
    st.session_state.setdefault("bfp_suffix", "BFP")
    st.session_state.setdefault("bfp_run_idx", 1)
    st.session_state.setdefault("bfp_auto_inc", True)
    st.session_state.setdefault("bfp_run_idx_widget", int(st.session_state["bfp_run_idx"]))
    st.session_state.setdefault("bfp_run_idx_bump", False)

    st.session_state.setdefault("bfp_save_notice", None)

    st.session_state.setdefault("bfp_use_bg", False)
    st.session_state.setdefault("bfp_bg_path", None)        # Path | None
    st.session_state.setdefault("bfp_bg_tol_nm", 1e-3)

    # ---- Save Name (compact row) ----
    st.subheader("Save Name")
    n1, n2, n3, n4, n5 = st.columns([1.25, 1.25, 0.85, 0.8, 0.9], gap="small")
    with n1:
        sample = st.text_input("Sample", key="bfp_sample")
    with n2:
        note = st.text_input("Note", key="bfp_note")
    with n3:
        suffix = st.text_input("Suffix", key="bfp_suffix")
    with n4:
        if st.session_state.get("bfp_run_idx_bump", False):
            st.session_state["bfp_run_idx_bump"] = False
            st.session_state["bfp_run_idx"] = int(st.session_state.get("bfp_run_idx", 1)) + 1
            st.session_state["bfp_run_idx_widget"] = int(st.session_state["bfp_run_idx"])
        run_idx = st.number_input("Run", 1, 99999, step=1, key="bfp_run_idx_widget")
        st.session_state["bfp_run_idx"] = int(st.session_state["bfp_run_idx_widget"])
    with n5:
        auto_inc = st.checkbox("Auto +1", key="bfp_auto_inc")

    save_dir = _sample_out_dir(sample)
    save_dir.mkdir(parents=True, exist_ok=True)

    ts_preview = st.session_state.get("bfp_last_ts") or _now_ts()
    stem_preview = _build_stem(sample, note, suffix, ts_preview, int(run_idx))
    st.caption(f"Save folder: `{save_dir}`  •  Preview: **{stem_preview}.csv**")

    st.markdown("---")

    # ---- Controls row: Acquire | Display | Background ----
    st.subheader("Controls")
    c_acq, c_disp, c_bg = st.columns([2.3, 1.1, 1.35], gap="large")

    with c_acq:
        r1 = st.columns([1.2, 1.0, 1.0, 0.8], gap="small")
        with r1[0]:
            st.selectbox("ROI", ["Full sensor", "Bin all"], key="bfp_roi_mode")
        with r1[1]:
            st.number_input("Center (nm)", 0.0, 2000.0, step=0.1, key="bfp_center_nm")
        with r1[2]:
            st.number_input("Exposure (ms)", 1.0, 600000.0, step=10.0, key="bfp_exposure_ms")
        with r1[3]:
            st.number_input("EPF", 1, 100000, step=1, key="bfp_epf")

        r2 = st.columns([1.0, 1.1, 1.2, 1.0], gap="small")
        with r2[0]:
            st.checkbox("Warm-up", key="bfp_warmup_after_roi")
        with r2[1]:
            st.checkbox("Auto-apply", key="bfp_auto_apply_on_acquire")
        with r2[2]:
            do_acq = st.button("Acquire", type="primary", key="btn_bfp_acquire", use_container_width=True)
        with r2[3]:
            do_clear = st.button("Clear", key="btn_bfp_clear", use_container_width=True)

        est_s = (float(st.session_state["bfp_exposure_ms"]) / 1000.0) * int(st.session_state["bfp_epf"])
        st.caption(f"≈ {est_s:.1f}s  •  Full sensor→2D, Bin all→1D")

    with c_disp:
        st.selectbox("Scale", ["Linear", "Log"], key="bfp_disp_scale")
        st.selectbox("Cmap", ["gray", "viridis", "plasma", "magma", "inferno", "cividis", "turbo"], key="bfp_cmap")

        st.checkbox("Auto (1–99%)", key="bfp_autoscale", on_change=_on_autoscale_change)
        st.checkbox("Colorbar", key="bfp_show_colorbar")

        if not bool(st.session_state["bfp_autoscale"]):
            st.number_input("vmin", key="bfp_vmin", step=1.0)
            st.number_input("vmax", key="bfp_vmax", step=1.0)
        else:
            st.caption("Auto uses 1–99% of displayed data.")

    with c_bg:
        st.markdown("**Background (ΔR/R0)**")

        bg_folder = _sample_out_dir(st.session_state.get("bfp_sample", ""))
        bg_folder.mkdir(parents=True, exist_ok=True)
        csvs = _list_csvs(bg_folder)
        bg_options = [None] + csvs

        use_bg = st.checkbox("Use background: (R-R0)/R0", key="bfp_use_bg")

        st.selectbox(
            "Background CSV",
            options=bg_options,
            key="bfp_bg_path",
            format_func=lambda p: "(none)" if p is None else p.name,
            disabled=not use_bg,
        )

        st.number_input(
            "λ match tol (nm)",
            min_value=0.0,
            value=float(st.session_state.get("bfp_bg_tol_nm", 1e-3)),
            step=1e-4,
            format="%.4g",
            key="bfp_bg_tol_nm",
            disabled=not use_bg,
        )

        if len(csvs) == 0:
            st.caption(f"Folder: `{bg_folder}`")
            st.info("No CSVs yet. Save a CSV first (same Sample folder).")
        else:
            st.caption(f"Folder: `{bg_folder}`")

    # ---- Status panel (compact) ----
    status_area = st.container()

    # ---- Clear ----
    if do_clear:
        st.session_state.pop("bfp_last_data", None)
        st.session_state.pop("bfp_last_ts", None)

    # ---- Acquire ----
    if do_acq:
        import time
        t0 = time.time()

        with status_area:
            _status_line("info", "Capturing… (LightField call blocks UI)")

        ok = True
        msg = ""

        if st.session_state.get("bfp_auto_apply_on_acquire", True):
            ok, msg = _apply_all_settings(
                spec,
                st.session_state["bfp_roi_mode"],
                float(st.session_state["bfp_center_nm"]),
                float(st.session_state["bfp_exposure_ms"]),
                int(st.session_state["bfp_epf"]),
            )

        if ok and st.session_state.get("bfp_warmup_after_roi", True):
            try:
                _ = _acquire_camera_output(spec, st.session_state["bfp_roi_mode"])
            except Exception:
                pass

        if not ok:
            with status_area:
                _status_line("err", f"Apply failed: {msg}")
        else:
            raw = _acquire_camera_output(spec, st.session_state["bfp_roi_mode"])
            st.session_state["bfp_last_data"] = np.asarray(raw)
            st.session_state["bfp_last_ts"] = _now_ts()

            dt = time.time() - t0
            rb = _readback_roi_string(spec)
            rb_txt = f" | ROI readback: {rb}" if rb else ""

            with status_area:
                if msg:
                    _status_line("info", f"Applied: {msg}")
                _status_line("ok", f"Acquired: shape={tuple(np.asarray(raw).shape)} ({np.asarray(raw).ndim}D) in {dt:.2f}s{rb_txt}")

    raw = st.session_state.get("bfp_last_data", None)
    if raw is None:
        st.info("No data acquired yet.")
        return

    raw = np.asarray(raw)
    ts_cap = st.session_state.get("bfp_last_ts", "")

    # ---- Background apply ----
    data = raw
    bg_msg_kind = None
    bg_msg_text = None

    use_bg = bool(st.session_state.get("bfp_use_bg", False))
    bg_path = st.session_state.get("bfp_bg_path", None)
    tol_nm = float(st.session_state.get("bfp_bg_tol_nm", 1e-3))

    if use_bg:
        if bg_path is None:
            bg_msg_kind = "warn"
            bg_msg_text = "Background ON, but no CSV selected → not applied."
        else:
            try:
                bg_path = Path(bg_path)
                mtime = bg_path.stat().st_mtime
                bg_arr, bg_wls = _cached_load_bfp_csv(str(bg_path), mtime)

                meas_wls = _get_wavelengths_nm(spec)

                if raw.ndim == 2:
                    raw_use, meas_wls_use = _orient_2d_to_wavelength_columns(raw, meas_wls)
                    bg_use, bg_wls_use = _orient_2d_to_wavelength_columns(bg_arr, bg_wls)
                else:
                    raw_use, meas_wls_use = raw, meas_wls
                    bg_use, bg_wls_use = bg_arr, bg_wls

                ok_bg, out, msg = _apply_background_rr0(
                    meas=raw_use,
                    meas_wls=meas_wls_use,
                    bg=bg_use,
                    bg_wls=bg_wls_use,
                    tol_nm=tol_nm,
                )

                if ok_bg:
                    data = out
                    bg_msg_kind = "ok"
                    bg_msg_text = f"Background {msg} | bg={bg_path.name}"
                else:
                    data = raw
                    bg_msg_kind = "warn"
                    bg_msg_text = f"Background {msg} | bg={bg_path.name}"

            except Exception as e:
                data = raw
                bg_msg_kind = "warn"
                bg_msg_text = f"Background NOT applied: load failed ({e})"

    if bg_msg_text:
        with status_area:
            _status_line(bg_msg_kind or "info", bg_msg_text)

    # ---- Update auto limits cache (so manual defaults match auto) ----
    try:
        lo, hi = _compute_auto_limits_raw(data, st.session_state.get("bfp_disp_scale", "Linear"))
        st.session_state["bfp_last_auto_vmin_raw"] = lo
        st.session_state["bfp_last_auto_vmax_raw"] = hi
    except Exception:
        pass

    # ---- Output ----
    st.subheader("Latest Output")

    scale = str(st.session_state.get("bfp_disp_scale", "Linear"))
    autoscale = bool(st.session_state.get("bfp_autoscale", True))

    if data.ndim == 2:
        img = np.asarray(data, dtype=np.float64)
        wls = _get_wavelengths_nm(spec)
        img, wls_x = _orient_2d_to_wavelength_columns(img, wls)

        if scale.lower().startswith("log"):
            img_show = np.log1p(np.clip(img, 0, None))
            title = f"Captured: {ts_cap} (2D, Log view)"
        else:
            img_show = img
            title = f"Captured: {ts_cap} (2D, Linear view)"

        if autoscale:
            v_lo, v_hi = np.nanpercentile(img_show, [1, 99])
        else:
            raw_lo = float(st.session_state.get("bfp_vmin", 0.0))
            raw_hi = float(st.session_state.get("bfp_vmax", raw_lo + 1.0))
            if scale.lower().startswith("log"):
                raw_lo = max(raw_lo, 0.0)
                raw_hi = max(raw_hi, raw_lo + 1e-9)
                v_lo, v_hi = np.log1p(raw_lo), np.log1p(raw_hi)
            else:
                v_lo, v_hi = raw_lo, raw_hi

        if not np.isfinite(v_lo): v_lo = float(np.nanmin(img_show))
        if not np.isfinite(v_hi): v_hi = float(np.nanmax(img_show))
        if (not np.isfinite(v_lo)) or (not np.isfinite(v_hi)) or (v_hi <= v_lo):
            v_lo, v_hi = 0.0, 1.0

        fig, ax = plt.subplots(figsize=(7.6, 3.9), dpi=120)

        extent = None
        if wls_x.size > 0 and wls_x.size == img_show.shape[1]:
            extent = (float(wls_x[0]), float(wls_x[-1]), 0, img_show.shape[0] - 1)

        im = ax.imshow(
            img_show,
            origin="lower",
            aspect="auto",
            extent=extent,
            cmap=st.session_state.get("bfp_cmap", "gray"),
            vmin=v_lo,
            vmax=v_hi,
            interpolation="nearest",
        )

        ax.set_title(title, fontsize=10)
        ax.set_xlabel("Wavelength (nm)" if extent is not None else "X (px)")
        ax.set_ylabel("Y (px)")

        if st.session_state.get("bfp_show_colorbar", True):
            fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)

        fig.tight_layout()
        _st_pyplot(fig)
        plt.close(fig)

    elif data.ndim == 1:
        y = np.asarray(data, dtype=np.float64).ravel()
        wls = _get_wavelengths_nm(spec)

        fig, ax = plt.subplots(figsize=(7.8, 3.0), dpi=120)

        if wls.size == y.size:
            ax.plot(wls, y, linewidth=1.0)
            ax.set_xlabel("Wavelength (nm)")
        else:
            ax.plot(y, linewidth=1.0)
            ax.set_xlabel("Pixel index")

        ax.set_title(f"Captured: {ts_cap} (1D)", fontsize=10)
        ax.set_ylabel("Intensity (a.u.)")

        if autoscale and y.size > 0:
            lo, hi = np.nanpercentile(y, [1, 99])
            pad = 0.05 * (hi - lo) if hi > lo else 1.0
            ax.set_ylim(lo - pad, hi + pad)

        ax.grid(True, alpha=0.2)
        fig.tight_layout()
        _st_pyplot(fig)
        plt.close(fig)

    else:
        st.warning(f"Unexpected shape: {data.shape}")
        st.write(data)

    # ---- Save (CSV + PNG) ----
    st.subheader("Save (CSV + PNG)")

    notice = st.session_state.get("bfp_save_notice")
    if notice:
        (st.success if notice.get("ok") else st.error)(notice.get("msg", ""))

    if notice and st.button("Clear save message", key="btn_bfp_clear_save_notice"):
        st.session_state["bfp_save_notice"] = None
        st.rerun()

    ts_save = st.session_state.get("bfp_last_ts", _now_ts())
    stem = _build_stem(sample, note, suffix, ts_save, int(run_idx))
    out_dir = _sample_out_dir(sample)
    out_dir.mkdir(parents=True, exist_ok=True)

    out_csv = out_dir / f"{stem}.csv"
    out_png = out_dir / f"{stem}.png"
    st.caption(f"Will save to: `{out_csv}` and `{out_png}`")

    if st.button("Save CSV + PNG", key="btn_bfp_save_csv", use_container_width=True):
        try:
            wls = _get_wavelengths_nm(spec)
            _save_csv_with_wavelengths(out_csv, data, wls)
            _save_png_from_current(out_png, data, spec)

            st.session_state["bfp_save_notice"] = {
                "ok": True,
                "msg": f"✅ Saved CSV: {out_csv}\n✅ Saved PNG: {out_png}",
                "ts": _now_ts(),
            }

            if auto_inc:
                st.session_state["bfp_run_idx_bump"] = True

            st.rerun()

        except Exception as e:
            st.session_state["bfp_save_notice"] = {
                "ok": False,
                "msg": f"❌ Save failed: {e}",
                "ts": _now_ts(),
            }
            st.rerun()
