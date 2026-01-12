import streamlit as st
import numpy as np
import time
import re
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from pathlib import Path

BASE_OUT = Path(r"D:\instrument_control_v3_1")
EPS = 1e-9  # kept for future use if needed

# -------------------------
# Helpers
# -------------------------

import math

def build_sweep_points(outer_vals, inner_vals,
                      lim_vtg_min, lim_vtg_max, lim_vbg_min, lim_vbg_max,
                      is_snake: bool = True):
    """
    Returns ordered list of (vtg, vbg) points EXACTLY as executed.
    Stripe index i: reverse inner sweep if snake and i is odd.
    """
    pts = []
    for i, vtg in enumerate(outer_vals):
        reverse = bool(is_snake) and (i % 2 == 1)
        seq = inner_vals[::-1] if reverse else inner_vals
        for vbg in seq:
            if (lim_vtg_min <= vtg <= lim_vtg_max) and (lim_vbg_min <= vbg <= lim_vbg_max):
                pts.append((float(vtg), float(vbg)))
    return pts

def build_scan_path(outer_vals, inner_vals, is_snake: bool = True):
    """
    Returns ALL planned (vtg, vbg) points in the exact scan traversal order,
    including points outside safety limits.
    """
    pts = []
    for i, vtg in enumerate(outer_vals):
        reverse = bool(is_snake) and (i % 2 == 1)
        seq = inner_vals[::-1] if reverse else inner_vals
        for vbg in seq:
            pts.append((float(vtg), float(vbg)))
    return pts


NAN = float("nan")

def _read_gates_readback(iv) -> tuple[float, float]:
    """Return (Vbg_meas, Vtg_meas) as floats; NAN if unavailable."""
    if iv is None or not hasattr(iv, "read_current_gates"):
        return NAN, NAN
    try:
        bg, tg = iv.read_current_gates()
        bg = float(bg) if bg is not None else NAN
        tg = float(tg) if tg is not None else NAN
        return bg, tg
    except Exception:
        return NAN, NAN

def _read_bias_readback(iv) -> float:
    """Return Vbias_meas as float; NAN if unavailable."""
    if iv is None:
        return NAN

    # support either name
    if hasattr(iv, "read_current_bias"):
        try:
            vb = iv.read_current_bias()
            return float(vb) if vb is not None else NAN
        except Exception:
            return NAN

    return NAN



def sanitize_filename(s: str) -> str:
    for ch in '<>:"/\\|?*':
        s = s.replace(ch, "")
    return s.strip()

def unique_stem(out_dir: Path, stem: str) -> str:
    """
    Always return a numbered stem like 'name_001', 'name_002', ...
    - If no prior files: returns stem_001
    - If 'stem.csv' (old style) exists, the next becomes stem_002
    - If stem_00N exists, next is stem_00(N+1)
    """
    stem = sanitize_filename(stem)
    out_dir.mkdir(parents=True, exist_ok=True)

    max_n = 0
    if (out_dir / f"{stem}.csv").exists():
        max_n = max(max_n, 1)

    pat = re.compile(re.escape(stem) + r"_(\d{3})\.csv$", re.IGNORECASE)
    for p in out_dir.glob(f"{stem}_*.csv"):
        m = pat.search(p.name)
        if m:
            max_n = max(max_n, int(m.group(1)))

    next_n = 1 if max_n == 0 else max_n + 1
    return f"{stem}_{next_n:03d}"


def format_exposure_for_filename(ms_val):
    seconds = ms_val / 1000.0
    return f"{int(seconds)}" if seconds.is_integer() else f"{seconds:.2g}"

def get_linear_array(start, stop, param, mode):
    """Return array either by total points or by fixed step size (inclusive of stop when close)."""
    if mode == "Total Points":
        return np.linspace(start, stop, int(param))
    step = float(param)
    if step <= 0:
        return np.array([start])
    step = abs(step) if stop >= start else -abs(step)
    n = int(np.floor((stop - start) / step) + 1.00001)
    vals = start + np.arange(n) * step
    if (step > 0 and vals[-1] < stop - 1e-12) or (step < 0 and vals[-1] > stop + 1e-12):
        vals = np.append(vals, stop)
    return vals

def infer_step_from_array(arr: np.ndarray, fallback: float = 0.2) -> float:
    """Infer a uniform step from a 1D array (outer_vals)."""
    if arr is None or len(arr) < 2:
        return fallback
    return float(arr[1] - arr[0])

def make_signed_step(start: float, stop: float, step_mag: float) -> float:
    """Return a signed step that follows the direction start->stop."""
    if step_mag <= 0:
        return 0.0
    return step_mag if stop >= start else -step_mag

def arange_inclusive(start: float, stop: float, step: float) -> np.ndarray:
    """np.arange that includes stop (within tolerance)."""
    if step == 0:
        return np.array([start])
    n = int(np.floor((stop - start) / step + 1e-9)) + 1
    vals = start + np.arange(max(1, n)) * step
    if len(vals):
        if (step > 0 and vals[-1] < stop - 1e-12) or (step < 0 and vals[-1] > stop + 1e-12):
            vals = np.append(vals, stop)
    return vals

def _pad_or_trim(y: np.ndarray, n: int, pad_val: float = 0.0) -> np.ndarray:
    """Force vector length == n (keeps CSV rectangular)."""
    y = np.asarray(y, dtype=float).reshape(-1)
    if n <= 0:
        return y
    if y.size > n:
        return y[:n]
    if y.size < n:
        return np.pad(y, (0, n - y.size), constant_values=float(pad_val))
    return y

# ---------- Always return intensity for saving ----------
def read_intensity(spec, expected_len: int):
    """Always return intensity (not λ), padded/trimmed to expected_len."""
    sp = spec.acquire()
    if isinstance(sp, tuple) and len(sp) >= 2:
        y = np.asarray(sp[1]).squeeze()
        return _pad_or_trim(y, expected_len, pad_val=0.0)

    if isinstance(sp, dict):
        for k in ("intensity", "y", "counts", "data"):
            if k in sp:
                y = np.asarray(sp[k]).squeeze()
                return _pad_or_trim(y, expected_len, pad_val=0.0)

    arr = np.asarray(sp).squeeze()
    if arr.ndim == 2 and 2 in arr.shape:
        y = arr[1, :] if arr.shape[0] == 2 else arr[-1, :]
        return _pad_or_trim(y, expected_len, pad_val=0.0)

    return _pad_or_trim(arr, expected_len, pad_val=0.0)

# ---------- Get wavelengths by querying LF6 directly (bypasses wrapper cache) ----------
def _midpoint_nm(wls: np.ndarray) -> float:
    return 0.5 * (float(wls[0]) + float(wls[-1])) if wls.size > 1 else float("nan")

def wait_lambda_from_lf6(lf6, target_center_nm: float,
                         tol_nm: float = 0.5,
                         timeout_s: float = 25.0,
                         poll_s: float = 0.3,
                         require_consecutive: int = 2) -> np.ndarray:
    """
    Poll lf6.get_wavelength_calibration() until mid-λ ~= target center.
    This bypasses SpectrometerLF6 caching and reflects the actual instrument.
    """
    deadline = time.time() + timeout_s
    ok = 0
    last_mid = None

    while time.time() < deadline:
        try:
            w = np.asarray(lf6.get_wavelength_calibration(), dtype=float).ravel()
        except Exception:
            w = np.array([], dtype=float)

        if w.size > 2:
            mid = _midpoint_nm(w)
            last_mid = mid
            if abs(mid - float(target_center_nm)) <= tol_nm:
                ok += 1
                if ok >= require_consecutive:
                    return w
            else:
                ok = 0
        time.sleep(poll_s)

    raise TimeoutError(f"λ not updated in {timeout_s}s via LF6 (target={target_center_nm}, last_mid={last_mid})")

def get_wavelengths_or_fail(spec, lf6, target_center_nm: float, tol_nm: float) -> np.ndarray:
    """
    Robust wavelength header acquisition:
      1) try LF6 wait/poll
      2) fallback LF6 get_wavelength_calibration
      3) fallback spec.calibration_wavelengths()
      4) fallback one spec.acquire() tuple (wl, y)
    """
    # 1) wait via LF6
    if lf6 is not None:
        try:
            w = wait_lambda_from_lf6(lf6, target_center_nm=float(target_center_nm), tol_nm=float(tol_nm))
            w = np.asarray(w, dtype=float).ravel()
            if w.size > 2:
                return w
        except Exception:
            pass

        # 2) direct LF6 read
        try:
            w = np.asarray(lf6.get_wavelength_calibration(), dtype=float).ravel()
            if w.size > 2:
                return w
        except Exception:
            pass

    # 3) spec calibration
    if spec is not None and hasattr(spec, "calibration_wavelengths"):
        try:
            w = np.asarray(list(spec.calibration_wavelengths()), dtype=float).ravel()
            if w.size > 2:
                return w
        except Exception:
            pass

    # 4) last resort: first acquisition tuple
    if spec is not None:
        try:
            sp = spec.acquire()
            if isinstance(sp, tuple) and len(sp) >= 2:
                w = np.asarray(sp[0], dtype=float).ravel()
                if w.size > 2:
                    return w
        except Exception:
            pass

    return np.array([], dtype=float)

# -------------------------
# Main render
# -------------------------
def render(devices):
    st.header("⚡ MegaSweep (Vtg stripes, Vbg ratio-scaled step)")

    iv = devices.get("iv")
    spec = devices.get("spectrometer")
    lf6 = st.session_state.get("lf6")

    role_map = getattr(iv, "role_map", {}) if iv else {}
    # FIX: match adapter semantics (empty string is NOT a mapping)
    axes_available = [k for k, v in (role_map or {}).items() if bool(v)]
    if role_map:
        st.info(f"🔌 IV Role Map: {role_map}")

    # ==========================================
    # File & optical settings
    # ==========================================
    with st.expander("📂 File, Optical & Pattern Settings", expanded=False):
        c_id1, c_id2, c_id3 = st.columns([1, 1, 2])
        with c_id1:
            if "shared_sample_name" not in st.session_state:
                st.session_state.shared_sample_name = "Sample"
            sample = st.text_input("Sample Name", key="shared_sample_name")
        with c_id2:
            tag = st.text_input("Tag / Desc", "Megasweep")
        with c_id3:
            subfolder = "megasweep"
            out_path = BASE_OUT / sample / subfolder
            st.text_input("Save Path", str(out_path), disabled=True)

        c_opt1, c_opt2, c_opt3, c_opt4, c_opt5 = st.columns(5)
        with c_opt1: laser_nm = st.text_input("Laser (nm)", "730")
        with c_opt2: power_uw = st.text_input("Power (µW)", "1")
        with c_opt3: lf_exp = st.number_input("Exp (ms)", value=100.0, step=10.0)
        with c_opt4: lf_epf = st.number_input("Frames/Accums", value=20, min_value=1)
        with c_opt5: lf_center = st.number_input("Center (nm)", value=730.0, step=1.0)

        default_pattern = "${sample}$~${tag}$~$REF{center_nm}nm{exp_s}msx{epf}$~"
        pattern = st.text_input("Filename Pattern", value=default_pattern)

    # ==========================================
    # Sweep setup
    # ==========================================
    col_ctrl, col_view = st.columns([1.25, 1], gap="medium")

    with col_ctrl:
        st.subheader("1) Motion & Safety")

        if not iv or "Vtg" not in axes_available or "Vbg" not in axes_available:
            st.error("Map both Vtg and Vbg in the sidebar to run this sweep.")
            st.stop()
        can_run = (iv is not None) and (spec is not None) and {"Vtg", "Vbg"}.issubset(set(axes_available))

        c_r1, c_r2, c_r3 = st.columns(3)
        with c_r1:
            go_step  = st.number_input("Ramp step (V) for ramping", value=0.1, min_value=0.001, format="%.3f")
        with c_r2:
            go_delay = st.number_input("Ramp delay (s)", value=0.02, min_value=0.0, format="%.3f")
        with c_r3:
            settle_delay = st.number_input("Settle after each point (s)", value=0.05, step=0.01, format="%.3f")

        # --- Vbias option (constant during whole sweep) ---
        st.subheader("2) Vbias (optional)")

        c_b1, c_b2, c_b3 = st.columns([1.2, 1, 1])
        with c_b1:
            enable_vbias = st.checkbox("Enable Vbias", value=False)
        with c_b2:
            vbias_set = st.number_input("Vbias set (V)", value=0.0, step=0.05, disabled=not enable_vbias)
        with c_b3:
            vbias_ramp_step = st.number_input("Vbias ramp step (V)", value=0.1, min_value=0.001, format="%.3f", disabled=not enable_vbias)

        # NEW: user-configurable center tolerance
        center_tol_nm = 1.0

        with st.expander("🛡️ Absolute Voltage Limits", expanded=True):
            c_s1, c_s2, c_s3, c_s4 = st.columns(4)
            with c_s1: lim_vtg_min = st.number_input("Min Vtg", value=-10.0)
            with c_s2: lim_vtg_max = st.number_input("Max Vtg", value= 10.0)
            with c_s3: lim_vbg_min = st.number_input("Min Vbg", value=-10.0)
            with c_s4: lim_vbg_max = st.number_input("Max Vbg", value= 10.0)

        st.subheader("3) Grids (Vtg stripes / Vbg linear)")
        is_snake = st.checkbox("Snake pattern (reverse Vbg every stripe)", value=True)

        # Outer Vtg range
        st.caption("**Outer (fixed per stripe): Vtg**")
        c_o1, c_o2, c_o3 = st.columns([1, 1, 1])
        with c_o1: vtg_start = st.number_input("Vtg Start", value=-5.0, key="vtg_s")
        with c_o2: vtg_stop  = st.number_input("Vtg Stop",  value= 5.0, key="vtg_e")
        with c_o3:
            vtg_mode = st.radio("Vtg Grid", ["Step Size (Grid)", "Total Points"], horizontal=True, key="vtg_mode")
        vtg_param = (st.number_input("Vtg Pts",  value=11, min_value=2, key="vtg_pts")
                     if vtg_mode == "Total Points"
                     else st.number_input("Vtg Step", value=1.0, min_value=0.001, format="%.3f", key="vtg_step"))
        outer_vals = get_linear_array(vtg_start, vtg_stop, vtg_param, vtg_mode)

        # Ratio defines inner (Vbg) step magnitude relative to effective Vtg step
        st.caption("**Inner (swept for each stripe): Vbg**")
        c_i1, c_i2, c_i3 = st.columns([1, 1, 1])
        with c_i1: vbg_start = st.number_input("Vbg Start", value=-5.0, key="vbg_s")
        with c_i2: vbg_stop  = st.number_input("Vbg Stop",  value= 5.0, key="vbg_e")
        with c_i3: ratio_val = st.number_input("Ratio r (Vbg step = r × Vtg step)", value=1.0, step=0.05)

        vtg_step_eff = abs(infer_step_from_array(outer_vals, fallback=1.0))
        vbg_step_mag = max(1e-9, abs(ratio_val) * vtg_step_eff)
        vbg_step = make_signed_step(vbg_start, vbg_stop, vbg_step_mag)
        inner_vals = arange_inclusive(vbg_start, vbg_stop, vbg_step)

        st.caption(f"Derived steps → Vtg step ≈ {vtg_step_eff:.6g} V, Vbg step ≈ {vbg_step_mag:.6g} V (signed {vbg_step:.6g})")

    # ==========================================
    # Preview  (no seam de-dupe: show ALL points)
    # ==========================================
    with col_view:
        # ---- compact "run bar" at the top of the preview column ----
        h1, h3 = st.columns([1.6, 1.0], vertical_alignment="bottom")
        with h1:
            st.markdown("### Preview")
        with h3:
            start_clicked = st.button(
                "🚀 START",
                type="primary",
                width="stretch",
                disabled=not can_run,
            )

        # 1) full planned traversal (includes out-of-safety)
        path_pts = build_scan_path(outer_vals, inner_vals, is_snake=is_snake)
        x_all = np.array([p[0] for p in path_pts], dtype=float)
        y_all = np.array([p[1] for p in path_pts], dtype=float)

        in_mask = (
            (x_all >= lim_vtg_min) & (x_all <= lim_vtg_max) &
            (y_all >= lim_vbg_min) & (y_all <= lim_vbg_max)
        )

        x_out = x_all[~in_mask]
        y_out = y_all[~in_mask]

        # 2) run-executed points (in-bounds only) -> keeps preview/run order 1:1
        pts = build_sweep_points(
            outer_vals, inner_vals,
            lim_vtg_min, lim_vtg_max, lim_vbg_min, lim_vbg_max,
            is_snake=is_snake
        )
        x_in = np.array([p[0] for p in pts], dtype=float)
        y_in = np.array([p[1] for p in pts], dtype=float)
        o_in = np.arange(len(pts), dtype=float)

        st.caption(
            f"Planned: **{len(path_pts)}** | In safety (run): **{len(pts)}** | Out of safety: **{len(path_pts)-len(pts)}**"
        )
        st.caption("Red ✕ = outside safety limits (will be skipped by run)")

        tab_v, tab_p = st.tabs(["⚡ Voltage Grid", "📐 Physics (analysis)"])

        with tab_v:
            # --- UI controls for preview zoom ---
            focus_preview = st.checkbox("Auto-zoom to sweep region", value=True, key="megasweep_zoom")
            show_safety_box = st.checkbox("Show safety limits box", value=True, key="megasweep_show_safety")

            fig, ax = plt.subplots(figsize=(4.4, 3.4))

            # safety box (draw it, but DO NOT let it decide the zoom; we'll set limits later)
            if show_safety_box:
                rect = patches.Rectangle(
                    (lim_vtg_min, lim_vbg_min),
                    lim_vtg_max - lim_vtg_min,
                    lim_vbg_max - lim_vbg_min,
                    linewidth=1,
                    fill=False,
                    edgecolor="red",
                    linestyle="--",
                    alpha=0.8,
                )
                ax.add_patch(rect)

            if len(path_pts) > 0:
                # draw full planned path lightly (includes out-of-safety)
                if len(x_all) >= 2:
                    ax.plot(x_all, y_all, linewidth=1, alpha=0.25)

                # in-bounds points: colored by RUN order
                if len(x_in) > 0:
                    sc = ax.scatter(x_in, y_in, s=18, c=o_in, cmap="viridis")
                    cb = fig.colorbar(sc, ax=ax, fraction=0.046, pad=0.04)
                    cb.set_label("Order (run)")

                    if len(x_in) >= 2:
                        ax.plot(x_in, y_in, linewidth=1, alpha=0.6)

                    # START/END are run start/end (in-bounds)
                    ax.scatter([x_in[0]], [y_in[0]], s=60, marker="o", edgecolors="k", linewidths=1.0, zorder=5)
                    ax.annotate("START", (x_in[0], y_in[0]), xytext=(6, 6), textcoords="offset points", fontsize=9)

                    ax.scatter([x_in[-1]], [y_in[-1]], s=60, marker="s", edgecolors="k", linewidths=1.0, zorder=5)
                    ax.annotate("END", (x_in[-1], y_in[-1]), xytext=(6, 6), textcoords="offset points", fontsize=9)

                # out-of-bounds points: red X
                if len(x_out) > 0:
                    ax.scatter(x_out, y_out, s=45, marker="x", color="red", linewidths=1.5, alpha=0.9, zorder=6)

                # --- zoom uses ALL planned points so out-of-bounds are visible ---
                if focus_preview:
                    xmin, xmax = float(np.min(x_all)), float(np.max(x_all))
                    ymin, ymax = float(np.min(y_all)), float(np.max(y_all))
                    span_x = max(1e-9, xmax - xmin)
                    span_y = max(1e-9, ymax - ymin)
                    pad_x = max(abs(vtg_step_eff), 0.05 * span_x)
                    pad_y = max(abs(vbg_step),     0.05 * span_y)
                    ax.set_xlim(xmin - pad_x, xmax + pad_x)
                    ax.set_ylim(ymin - pad_y, ymax + pad_y)
                else:
                    ax.set_xlim(lim_vtg_min, lim_vtg_max)
                    ax.set_ylim(lim_vbg_min, lim_vbg_max)
            else:
                ax.set_xlim(lim_vtg_min, lim_vtg_max)
                ax.set_ylim(lim_vbg_min, lim_vbg_max)


            ax.set_xlabel("Vtg (V)")
            ax.set_ylabel("Vbg (V)")
            ax.grid(True, linestyle="--", alpha=0.4)
            fig.tight_layout()

            # use full column width (looks much nicer in Streamlit)
            st.pyplot(fig, width="content")

            # still communicate safety limits even when zoomed
            st.caption(f"Safety limits: Vtg[{lim_vtg_min:g}, {lim_vtg_max:g}]  |  Vbg[{lim_vbg_min:g}, {lim_vbg_max:g}]")


        with tab_p:
            if len(path_pts) > 0:
                dop_all = ratio_val * x_all + y_all
                fld_all = ratio_val * x_all - y_all

                dop_in  = ratio_val * x_in + y_in
                fld_in  = ratio_val * x_in - y_in

                dop_out = dop_all[~in_mask]
                fld_out = fld_all[~in_mask]

                fig2, ax2 = plt.subplots(figsize=(4.4, 3.4))

                # full planned path lightly
                if len(dop_all) >= 2:
                    ax2.plot(dop_all, fld_all, linewidth=1, alpha=0.25)

                # in-bounds points colored by run order
                if len(dop_in) > 0:
                    sc2 = ax2.scatter(dop_in, fld_in, s=18, c=o_in, cmap="viridis")
                    cb2 = fig2.colorbar(sc2, ax=ax2, fraction=0.046, pad=0.04)
                    cb2.set_label("Order (run)")

                    if len(dop_in) >= 2:
                        ax2.plot(dop_in, fld_in, linewidth=1, alpha=0.6)

                    # START/END (run)
                    ax2.scatter([dop_in[0]], [fld_in[0]], s=70, marker="o", edgecolors="k", linewidths=1.2, zorder=5)
                    ax2.annotate("START", (dop_in[0], fld_in[0]), xytext=(6, 6), textcoords="offset points", fontsize=9)

                    ax2.scatter([dop_in[-1]], [fld_in[-1]], s=70, marker="s", edgecolors="k", linewidths=1.2, zorder=5)
                    ax2.annotate("END", (dop_in[-1], fld_in[-1]), xytext=(6, 6), textcoords="offset points", fontsize=9)

                # out-of-bounds: red X
                if len(dop_out) > 0:
                    ax2.scatter(dop_out, fld_out, s=45, marker="x", color="red", linewidths=1.5, alpha=0.9, zorder=6)

                ax2.set_xlabel(f"D = r*Vtg + Vbg (r={ratio_val:g})")
                ax2.set_ylabel("F = r*Vtg - Vbg")
                ax2.grid(True, linestyle="--", alpha=0.4)
                fig2.tight_layout()
                st.pyplot(fig2, width="content")
            else:
                st.info("No points in preview (check grid).")


        
    if start_clicked:
        try:
            # Spectrometer config (apply center/exposure/frames on the LF6 object)
            if lf6 is not None:
                if hasattr(lf6, 'change_spectra_center'):
                    # send as string first (LightField UI often expects text)
                    try:
                        lf6.change_spectra_center(f"{float(lf_center):.0f}")
                    except Exception:
                        lf6.change_spectra_center(lf_center)
                if hasattr(lf6, 'change_expose_time'):
                    lf6.change_expose_time(lf_exp)
                if hasattr(lf6, 'set_frames'):
                    lf6.set_frames(lf_epf)
                elif hasattr(lf6, 'set_accumulations'):
                    lf6.set_accumulations(lf_epf)
            # --- Vbias mapping check if requested ---
            if enable_vbias:
                has_vbias = bool(getattr(iv, "has_role", lambda *_: False)("Vbias"))
                if not has_vbias:
                    st.error("Vbias is enabled but IV role 'Vbias' is NOT mapped. Map Vbias or disable Vbias.")
                    st.stop()

            # Filename
            exp_s_fmt = format_exposure_for_filename(lf_exp)
            name_vars = {
                "sample": sample, "tag": tag,
                "laser_nm": laser_nm, "power_uw": power_uw,
                "exp_s": exp_s_fmt, "epf": lf_epf, "center_nm": lf_center
            }
            clean_pattern = pattern.replace("${", "{").replace("}$", "}")
            base_name = clean_pattern.format(**name_vars)
            out_path.mkdir(parents=True, exist_ok=True)
            file_path = out_path / f"{unique_stem(out_path, base_name)}.csv"

            # Header wavelengths (robust)
            wls = get_wavelengths_or_fail(
                spec=spec,
                lf6=lf6,
                target_center_nm=float(lf_center),
                tol_nm=float(center_tol_nm),
            )
            if wls.size <= 2:
                st.error("Could not obtain wavelength calibration (wls empty). Aborting to avoid corrupt CSV.")
                st.stop()

            st.caption(f"λ mid ≈ {_midpoint_nm(wls):.3f} nm")

            include_setpoints = False  # (you commented out the checkbox; keep False so code won't crash)


            # Write header (NEW file)
            # Columns:
            # - if include_setpoints: Vbg_set, Vtg_set (and Vbias_set if enabled)
            # - always: Vbg_meas, Vtg_meas, Vbias_meas (Vbias columns filled with NAN if disabled)
            cols = []
            # if include_setpoints:
            #     cols += ["Vbg_set", "Vtg_set"]
            #     cols += ["Vbias_set"]  # keep for clarity even if disabled

            cols += ["Vbg_meas", "Vtg_meas", "Vbias_meas"]

            wl_str = np.array([f"{float(x):g}" for x in wls], dtype="U")
            h_row = np.concatenate((np.array(cols, dtype="U"), wl_str)).reshape([1, -1])

            with open(file_path, "w", newline="") as f:
                np.savetxt(f, h_row, fmt="%s", delimiter=",")


            # Compute total valid points (for accurate progress)
            pts = build_sweep_points(
                outer_vals, inner_vals,
                lim_vtg_min, lim_vtg_max, lim_vbg_min, lim_vbg_max,
                is_snake=is_snake
            )

            if not pts:
                st.error("No points in sweep (all out of bounds). Check limits/grid.")
                st.stop()

            total_pts = len(pts)


            # Initial safe ramp to the first corner
            # Optional: set Vbias once (constant throughout sweep)
            vbias_set_val = float(vbias_set) if enable_vbias else NAN

            if enable_vbias:
                try:
                    # ramp Vbias for safety
                    if hasattr(iv, "set_bias"):
                        iv.set_bias(Vbias=vbias_set_val, delay_s=go_delay, ramp_step=float(vbias_ramp_step))
                    else:
                        st.error("IV device has no set_bias().")
                        st.stop()

                    time.sleep(settle_delay)
                except Exception as e:
                    st.error(f"Failed to set Vbias: {e}")
                    st.stop()

            first_vtg, first_vbg = pts[0]  # first ACTUAL executed point (in-bounds + snake)
            iv.set_gates(Vtg=first_vtg, delay_s=go_delay, ramp_step=go_step)
            iv.set_gates(Vbg=first_vbg, delay_s=go_delay, ramp_step=go_step)
            time.sleep(settle_delay)


            # Sweep
            done = 0
            prog = st.progress(0.0)

            current_vtg = None

            with open(file_path, "a", newline="") as f:
                for vtg, vbg in pts:
                    # only change Vtg when needed
                    if current_vtg is None or (vtg != current_vtg):
                        iv.set_gates(Vtg=vtg, delay_s=go_delay, ramp_step=go_step)
                        current_vtg = vtg

                    iv.set_gates(Vbg=vbg, delay_s=go_delay, ramp_step=go_step)
                    time.sleep(settle_delay)

                    # Read back measured gates/bias
                    vbg_meas, vtg_meas = _read_gates_readback(iv)
                    vbias_meas = _read_bias_readback(iv) if enable_vbias else NAN

                    # Save intensity aligned to wavelength header
                    y = read_intensity(spec, expected_len=int(wls.size))

                    prefix = []
                    if include_setpoints:
                        prefix += [vbg, vtg]
                        prefix += [vbias_set_val]

                    prefix += [vbg_meas, vtg_meas, vbias_meas]

                    row = np.concatenate((np.array(prefix, dtype=np.float64), y)).reshape([1, -1])
                    np.savetxt(f, row, fmt="%.6e", delimiter=",")

                    done += 1
                    prog.progress(done / total_pts)

            prog.progress(1.0)
            st.success("Done.")

            # Return to 0 safely
            try:
                iv.set_gates(Vbg=0.0, Vtg=0.0, delay_s=go_delay, ramp_step=go_step)
            except Exception:
                pass

        except Exception as e:
            st.error(f"Error: {e}")
