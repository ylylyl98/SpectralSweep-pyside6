import streamlit as st
import numpy as np
import time
import re
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from pathlib import Path
from datetime import datetime
import math
BASE_OUT = Path(r"D:\instrument_control_v3_1")
EPS = 1e-9  # kept for future use if needed

# -------------------------
# Helpers
# -------------------------

def apply_lf6_settings(lf6, center_nm: float, exp_ms: float, frames: int):
    """Try hard to apply center/exposure/frames on many LF6 wrappers."""
    if lf6 is None:
        return

    # center
    if hasattr(lf6, "change_spectra_center"):
        try:
            lf6.change_spectra_center(f"{float(center_nm):.0f}")
        except Exception:
            lf6.change_spectra_center(center_nm)
        time.sleep(0.15)

    # exposure
    if hasattr(lf6, "change_expose_time"):
        try:
            lf6.change_expose_time(float(exp_ms))
        except Exception:
            lf6.change_expose_time(exp_ms)
        time.sleep(0.10)

    # frames / accumulations (some wrappers use one, some the other; some expose both)
    n = int(frames)
    set_ok = False

    # 1) common wrapper APIs
    for fn in ("set_accumulations", "set_frames"):
        if hasattr(lf6, fn):
            try:
                getattr(lf6, fn)(n)
                ui_log(f"LF6: {fn}({n})")
                set_ok = True
                break
            except Exception as e:
                ui_log(f"LF6: {fn} failed: {e}")

    # 2) YOUR LF6 automation API: OnlineProcessingFrameCombinationFramesCombined
    if not set_ok:
        for target in (lf6, getattr(lf6, "setup", None)):
            if target is None:
                continue
            if hasattr(target, "change_frame_to_combine"):
                try:
                    target.change_frame_to_combine(n)
                    ui_log(f"LF6: change_frame_to_combine({n})")
                    set_ok = True
                    break
                except Exception as e:
                    ui_log(f"LF6: change_frame_to_combine failed: {e}")

    if not set_ok:
        ui_log("LF6: no supported frame/accum method found (need set_frames/set_accumulations or change_frame_to_combine).")

    # (optional) readback confirmation
    # rb = None
    # if hasattr(lf6, "readback_online_process"):
    #     try:
    #         rb = lf6.readback_online_process()
    #     except Exception:
    #         rb = None
    # elif hasattr(getattr(lf6, "setup", None), "readback_online_process"):
    #     try:
    #         rb = lf6.setup.readback_online_process()
    #     except Exception:
    #         rb = None

    # if rb is not None:
    #     ui_log(f"LF6 online process readback: {rb}")

    if not set_ok:
        ui_log("LF6: no set_frames/set_accumulations method found.")

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
    """ALL planned points in traversal order (includes out-of-safety)."""
    pts = []
    for i, vtg in enumerate(outer_vals):
        reverse = bool(is_snake) and (i % 2 == 1)
        seq = inner_vals[::-1] if reverse else inner_vals
        for vbg in seq:
            pts.append((float(vtg), float(vbg)))
    return pts

def ui_log(msg: str, keep_last: int = 300):
    """Append a timestamped log line to session_state."""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    st.session_state.setdefault("megasweep_log", [])
    st.session_state["megasweep_log"].append(line)
    if len(st.session_state["megasweep_log"]) > keep_last:
        st.session_state["megasweep_log"] = st.session_state["megasweep_log"][-keep_last:]

def refresh_log_box(log_ph=None, tail: int = 200):
    """Update the on-screen log box immediately (if placeholder exists)."""
    if log_ph is None:
        return
    txt = "\n".join(st.session_state.get("megasweep_log", [])[-tail:]) or "(no messages)"
    log_ph.code(txt, language="")


def _megasweep_reset_tracking(reason: str = ""):
    st.session_state["megasweep_measured_n"] = 0
    st.session_state["megasweep_running"] = False
    if reason:
        ui_log(f"Reset measured state: {reason}")



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
    st.session_state.setdefault("megasweep_log", [])
    st.session_state.setdefault("megasweep_measured_n", 0)
    st.session_state.setdefault("megasweep_running", False)
    st.session_state.setdefault("megasweep_sig", None)

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
        with c_opt4:
            lf_epf = st.number_input(
                "Frames/Accums",
                value=20,
                min_value=1,
                step=1,
                format="%d",
            )
        lf_epf = int(lf_epf)

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
        # --- measured progress for preview overlay ---
        measured_n = int(min(st.session_state.get("megasweep_measured_n", 0), len(pts)))
        running = bool(st.session_state.get("megasweep_running", False))
        # "current point" index:
        # - while running: highlight the NEXT point to be measured (measured_n = already finished count)
        # - when not running: highlight the LAST measured point
        if len(pts) > 0:
            if running:
                cur_i = min(measured_n, len(pts) - 1)
            else:
                cur_i = min(max(measured_n - 1, 0), len(pts) - 1)
        else:
            cur_i = None

        # --- colors for "order" (used for hollow edges and filled measured points) ---
        cmap = plt.cm.viridis
        norm = plt.Normalize(vmin=0, vmax=max(1, len(o_in) - 1))
        colors_in = cmap(norm(o_in))  # RGBA per point (run order)

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

                # in-bounds path line (run path)
                if len(x_in) >= 2:
                    ax.plot(x_in, y_in, linewidth=1, alpha=0.6)

                # --- unmeasured = hollow circles (edge colored by order) ---
                if len(x_in) > 0:
                    ax.scatter(
                        x_in, y_in,
                        s=40,
                        facecolors="none",          # hollow
                        edgecolors=colors_in,       # order color
                        linewidths=1.2,
                        alpha=0.9,
                        zorder=3
                    )

                    # --- measured = filled circles (same order color) ---
                    if measured_n > 0:
                        ax.scatter(
                            x_in[:measured_n], y_in[:measured_n],
                            s=55,
                            facecolors=colors_in[:measured_n],  # filled
                            edgecolors="k",
                            linewidths=0.6,
                            alpha=1.0,
                            zorder=5
                        )

                    # colorbar for order (independent of scatter style)
                    sm = plt.cm.ScalarMappable(norm=norm, cmap=cmap)
                    sm.set_array([])  # needed for mpl<3.8 sometimes
                    cb = fig.colorbar(sm, ax=ax, fraction=0.046, pad=0.04)
                    cb.set_label("Order (run)")

                    # START/END are run start/end (in-bounds)
                    ax.scatter([x_in[0]], [y_in[0]], s=60, marker="v", edgecolors="k", linewidths=1.0, zorder=5)
                    ax.annotate("START", (x_in[0], y_in[0]), xytext=(6, 6), textcoords="offset points", fontsize=9)

                    ax.scatter([x_in[-1]], [y_in[-1]], s=60, marker="s", edgecolors="k", linewidths=1.0, zorder=5)
                    ax.annotate("END", (x_in[-1], y_in[-1]), xytext=(6, 6), textcoords="offset points", fontsize=9)
                    # --- CURRENT point highlight (red ring + dot) ---
                    if cur_i is not None and len(x_in) > 0:
                        cx, cy = float(x_in[cur_i]), float(y_in[cur_i])

                        # big red ring
                        ax.scatter([cx], [cy],
                                s=160, marker="o",
                                facecolors="none", edgecolors="red",
                                linewidths=2.8, zorder=20)

                        # small red filled dot
                        ax.scatter([cx], [cy],
                                s=28, marker="o",
                                color="red", zorder=21)

                        ax.annotate("NOW", (cx, cy),
                                    xytext=(8, -12), textcoords="offset points",
                                    color="red", fontsize=9, fontweight="bold", zorder=22)

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

            # use full column width (looks much nicer in Streamlit)
            plot_v_ph = st.empty()          # <--- ADD this placeholder (keep variable name)
            fig.tight_layout()
            plot_v_ph.pyplot(fig, use_container_width=True)
            plt.close(fig)


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

                # in-bounds path line (run path)
                if len(dop_in) >= 2:
                    ax2.plot(dop_in, fld_in, linewidth=1, alpha=0.6)

                if len(dop_in) > 0:
                    # unmeasured = hollow circles
                    ax2.scatter(
                        dop_in, fld_in,
                        s=40,
                        facecolors="none",
                        edgecolors=colors_in,
                        linewidths=1.2,
                        alpha=0.9,
                        zorder=3
                    )

                    # measured = filled circles
                    if measured_n > 0:
                        ax2.scatter(
                            dop_in[:measured_n], fld_in[:measured_n],
                            s=55,
                            facecolors=colors_in[:measured_n],
                            edgecolors="k",
                            linewidths=0.6,
                            alpha=1.0,
                            zorder=5
                        )

                    # colorbar for order
                    sm2 = plt.cm.ScalarMappable(norm=norm, cmap=cmap)
                    sm2.set_array([])
                    cb2 = fig2.colorbar(sm2, ax=ax2, fraction=0.046, pad=0.04)
                    cb2.set_label("Order (run)")



                    # START/END (run)
                    ax2.scatter([dop_in[0]], [fld_in[0]], s=70, marker="v", edgecolors="k", linewidths=1.2, zorder=5)
                    ax2.annotate("START", (dop_in[0], fld_in[0]), xytext=(6, 6), textcoords="offset points", fontsize=9)

                    ax2.scatter([dop_in[-1]], [fld_in[-1]], s=70, marker="s", edgecolors="k", linewidths=1.2, zorder=5)
                    ax2.annotate("END", (dop_in[-1], fld_in[-1]), xytext=(6, 6), textcoords="offset points", fontsize=9)
                    # --- CURRENT point highlight (red ring + dot) ---
                    if cur_i is not None and len(dop_in) > 0:
                        cd, cf = float(dop_in[cur_i]), float(fld_in[cur_i])

                        ax2.scatter([cd], [cf],
                                    s=160, marker="o",
                                    facecolors="none", edgecolors="red",
                                    linewidths=2.8, zorder=20)

                        ax2.scatter([cd], [cf],
                                    s=28, marker="o",
                                    color="red", zorder=21)

                        ax2.annotate("NOW", (cd, cf),
                                    xytext=(8, -12), textcoords="offset points",
                                    color="red", fontsize=9, fontweight="bold", zorder=22)

                # out-of-bounds: red X
                if len(dop_out) > 0:
                    ax2.scatter(dop_out, fld_out, s=45, marker="x", color="red", linewidths=1.5, alpha=0.9, zorder=6)

                ax2.set_xlabel(f"D = r*Vtg + Vbg (r={ratio_val:g})")
                ax2.set_ylabel("F = r*Vtg - Vbg")
                ax2.grid(True, linestyle="--", alpha=0.4)

                plot_p_ph = st.empty()          # <--- ADD this placeholder (keep variable name)
                fig2.tight_layout()
                plot_p_ph.pyplot(fig2,  width="content")
                plt.close(fig2)

            else:
                st.info("No points in preview (check grid).")

        with st.expander("📜 MegaSweep log", expanded=False):
            cL1, cL2 = st.columns([1, 1])
            with cL1:
                if st.button("Clear log",  width="stretch", key="megasweep_clear_log"):
                    st.session_state["megasweep_log"] = []
            with cL2:
                if st.button("Reset measured",  width="stretch", key="megasweep_reset_measured"):
                    _megasweep_reset_tracking("User reset")

            log_ph = st.empty()
            log_text = "\n".join(st.session_state.get("megasweep_log", [])[-200:]) or "(no messages)"
            log_ph.code(log_text, language="")


        
    if start_clicked:
        try:
            st.session_state["megasweep_running"] = True
            st.session_state["megasweep_measured_n"] = 0
            ui_log("START pressed. Beginning sweep.")

            # Spectrometer config (apply center/exposure/frames on the LF6 object)
            if lf6 is not None:
                apply_lf6_settings(lf6, center_nm=lf_center, exp_ms=lf_exp, frames=lf_epf)


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

            # st.caption(f"λ mid ≈ {_midpoint_nm(wls):.3f} nm")

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

            ui_log(f"Sweep points (in safety): {total_pts}. Planned total: {len(path_pts)}. Skipped(out of safety): {len(path_pts)-total_pts}.")

            # Initial safe ramp to the first corner
            # Optional: set Vbias once (constant throughout sweep)
            vbias_set_val = float(vbias_set) if enable_vbias else NAN

            # --- log current readbacks before ramp-to-start (if available) ---
            vbg0, vtg0 = _read_gates_readback(iv)
            ui_log(f"Pre-ramp readback: Vtg={vtg0:.3f}, Vbg={vbg0:.3f}")
            refresh_log_box(log_ph if "log_ph" in locals() else None)

            # --- Vbias ramp (optional) ---
            vbias_set_val = float(vbias_set) if enable_vbias else NAN
            if enable_vbias:
                vb0 = _read_bias_readback(iv)
                ui_log(f"Ramping Vbias: from {vb0:.3f} -> {vbias_set_val:.3f} (ramp_step={vbias_ramp_step:.3f}, delay={go_delay:.3f})")
                refresh_log_box(log_ph if "log_ph" in locals() else None)

                try:
                    if hasattr(iv, "set_bias"):
                        iv.set_bias(Vbias=vbias_set_val, delay_s=go_delay, ramp_step=float(vbias_ramp_step))
                    else:
                        st.error("IV device has no set_bias().")
                        st.stop()
                    time.sleep(settle_delay)
                    vb1 = _read_bias_readback(iv)
                    ui_log(f"Vbias reached (readback): {vb1:.3f}  | settle={settle_delay:.3f}s")
                    refresh_log_box(log_ph if "log_ph" in locals() else None)
                except Exception as e:
                    st.error(f"Failed to set Vbias: {e}")
                    st.stop()

            # --- ramp to first ACTUAL executed point ---
            first_vtg, first_vbg = pts[0]
            ui_log(f"Ramping to START point: Vtg={first_vtg:.3f}, Vbg={first_vbg:.3f} (ramp_step={go_step:.3f}, delay={go_delay:.3f})")
            refresh_log_box(log_ph if "log_ph" in locals() else None)

            # ramp Vtg then Vbg (keep your original order)
            iv.set_gates(Vtg=first_vtg, delay_s=go_delay, ramp_step=go_step)
            ui_log(f"Vtg ramp complete (target={first_vtg:.3f})")
            refresh_log_box(log_ph if "log_ph" in locals() else None)

            iv.set_gates(Vbg=first_vbg, delay_s=go_delay, ramp_step=go_step)
            ui_log(f"Vbg ramp complete (target={first_vbg:.3f})")
            refresh_log_box(log_ph if "log_ph" in locals() else None)

            time.sleep(settle_delay)
            vbg1, vtg1 = _read_gates_readback(iv)
            ui_log(f"START reached (readback): Vtg={vtg1:.3f}, Vbg={vbg1:.3f}  | settle={settle_delay:.3f}s")
            refresh_log_box(log_ph if "log_ph" in locals() else None)



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
                    st.session_state["megasweep_measured_n"] = done

                    # log each point (or throttle if you want)
                    ui_log(f"{done}/{total_pts}: set Vtg={vtg:.3f}, Vbg={vbg:.3f} | meas Vtg={vtg_meas:.3f}, Vbg={vbg_meas:.3f}")

                    # --- UI refresh: log every point, plots every point ---
                    LOG_UPDATE_EVERY  = 1
                    PLOT_UPDATE_EVERY = 1

                    if (done % LOG_UPDATE_EVERY == 0) or (done == total_pts):
                        log_text = "\n".join(st.session_state.get("megasweep_log", [])[-200:]) or "(no messages)"
                        log_ph.code(log_text, language="")

                    # Update plots (measured overlay)
                    if (done % PLOT_UPDATE_EVERY == 0) or (done == total_pts):
                        measured_n = int(min(done, len(pts)))   # done == measured count

                        # ---------------------------
                        # Voltage plot refresh
                        # ---------------------------
                        fig, ax = plt.subplots(figsize=(4.4, 3.4))

                        # safety box
                        if st.session_state.get("megasweep_show_safety", True):
                            rect = patches.Rectangle(
                                (lim_vtg_min, lim_vbg_min),
                                lim_vtg_max - lim_vtg_min,
                                lim_vbg_max - lim_vbg_min,
                                linewidth=1, fill=False, edgecolor="red", linestyle="--", alpha=0.8
                            )
                            ax.add_patch(rect)

                        # planned path (light)
                        if len(x_all) >= 2:
                            ax.plot(x_all, y_all, linewidth=1, alpha=0.25)

                        # order colors for in-bounds points
                        if len(x_in) > 0:
                            cmap = plt.cm.viridis
                            norm = plt.Normalize(vmin=0, vmax=max(1, len(o_in) - 1))
                            colors_in = cmap(norm(o_in))

                            # in-bounds run path
                            if len(x_in) >= 2:
                                ax.plot(x_in, y_in, linewidth=1, alpha=0.6)

                            # unmeasured = hollow
                            ax.scatter(
                                x_in, y_in,
                                s=40, facecolors="none",
                                edgecolors=colors_in,
                                linewidths=1.2, alpha=0.9, zorder=3
                            )

                            # measured = filled
                            if measured_n > 0:
                                ax.scatter(
                                    x_in[:measured_n], y_in[:measured_n],
                                    s=55,
                                    facecolors=colors_in[:measured_n],
                                    edgecolors="k", linewidths=0.6,
                                    zorder=5
                                )

                            # START / END
                            ax.scatter([x_in[0]], [y_in[0]], s=60, marker="o", edgecolors="k", linewidths=1.0, zorder=6)
                            ax.annotate("START", (x_in[0], y_in[0]), xytext=(6, 6), textcoords="offset points", fontsize=9)
                            ax.scatter([x_in[-1]], [y_in[-1]], s=60, marker="s", edgecolors="k", linewidths=1.0, zorder=6)
                            ax.annotate("END", (x_in[-1], y_in[-1]), xytext=(6, 6), textcoords="offset points", fontsize=9)

                            # out-of-bounds = red X
                            if len(x_out) > 0:
                                ax.scatter(x_out, y_out, s=45, marker="x", color="red", linewidths=1.5, alpha=0.9, zorder=7)

                            # colorbar
                            sm = plt.cm.ScalarMappable(norm=norm, cmap=cmap)
                            sm.set_array([])
                            cb = fig.colorbar(sm, ax=ax, fraction=0.046, pad=0.04)
                            cb.set_label("Order (run)")

                        # zoom
                        if st.session_state.get("megasweep_zoom", True) and len(x_all) > 0:
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

                        ax.set_xlabel("Vtg (V)")
                        ax.set_ylabel("Vbg (V)")
                        ax.grid(True, linestyle="--", alpha=0.4)
                        fig.tight_layout()
                        plot_v_ph.pyplot(fig, width="content")
                        plt.close(fig)

                        # ---------------------------
                        # Physics plot refresh
                        # ---------------------------
                        fig2, ax2 = plt.subplots(figsize=(4.4, 3.4))

                        dop_all = ratio_val * x_all + y_all
                        fld_all = ratio_val * x_all - y_all

                        dop_in  = ratio_val * x_in + y_in
                        fld_in  = ratio_val * x_in - y_in

                        dop_out = dop_all[~in_mask]
                        fld_out = fld_all[~in_mask]

                        if len(dop_all) >= 2:
                            ax2.plot(dop_all, fld_all, linewidth=1, alpha=0.25)

                        if len(dop_in) > 0:
                            cmap = plt.cm.viridis
                            norm = plt.Normalize(vmin=0, vmax=max(1, len(o_in) - 1))
                            colors_in = cmap(norm(o_in))

                            if len(dop_in) >= 2:
                                ax2.plot(dop_in, fld_in, linewidth=1, alpha=0.6)

                            # unmeasured hollow
                            ax2.scatter(
                                dop_in, fld_in,
                                s=40, facecolors="none",
                                edgecolors=colors_in,
                                linewidths=1.2, alpha=0.9, zorder=3
                            )

                            # measured filled
                            if measured_n > 0:
                                ax2.scatter(
                                    dop_in[:measured_n], fld_in[:measured_n],
                                    s=55,
                                    facecolors=colors_in[:measured_n],
                                    edgecolors="k", linewidths=0.6,
                                    zorder=5
                                )

                            # START / END
                            ax2.scatter([dop_in[0]], [fld_in[0]], s=70, marker="o", edgecolors="k", linewidths=1.2, zorder=6)
                            ax2.annotate("START", (dop_in[0], fld_in[0]), xytext=(6, 6), textcoords="offset points", fontsize=9)
                            ax2.scatter([dop_in[-1]], [fld_in[-1]], s=70, marker="s", edgecolors="k", linewidths=1.2, zorder=6)
                            ax2.annotate("END", (dop_in[-1], fld_in[-1]), xytext=(6, 6), textcoords="offset points", fontsize=9)

                            # out-of-bounds red X
                            if len(dop_out) > 0:
                                ax2.scatter(dop_out, fld_out, s=45, marker="x", color="red", linewidths=1.5, alpha=0.9, zorder=7)

                            sm2 = plt.cm.ScalarMappable(norm=norm, cmap=cmap)
                            sm2.set_array([])
                            cb2 = fig2.colorbar(sm2, ax=ax2, fraction=0.046, pad=0.04)
                            cb2.set_label("Order (run)")

                        ax2.set_xlabel(f"D = r*Vtg + Vbg (r={ratio_val:g})")
                        ax2.set_ylabel("F = r*Vtg - Vbg")
                        ax2.grid(True, linestyle="--", alpha=0.4)
                        fig2.tight_layout()
                        plot_p_ph.pyplot(fig2, width="content")
                        plt.close(fig2)



                    prog.progress(done / total_pts)

            prog.progress(1.0)
            st.success("Done.")
            ui_log("Sweep complete.")
            st.session_state["megasweep_running"] = False
            st.session_state["megasweep_measured_n"] = total_pts

            # Return to 0 safely
            try:
                ui_log("Ramping back to 0V on Vtg/Vbg...")
                refresh_log_box(log_ph if "log_ph" in locals() else None)

                iv.set_gates(Vbg=0.0, Vtg=0.0, delay_s=go_delay, ramp_step=go_step)
                time.sleep(settle_delay)

                vbgz, vtgz = _read_gates_readback(iv)
                ui_log(f"Returned to 0V (readback): Vtg={vtgz:.3f}, Vbg={vbgz:.3f}")
                refresh_log_box(log_ph if "log_ph" in locals() else None)
            except Exception as e:
                ui_log(f"Return-to-zero failed: {e}")
                refresh_log_box(log_ph if "log_ph" in locals() else None)


        except Exception as e:
            st.error(f"Error: {e}")
