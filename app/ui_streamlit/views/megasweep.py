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

    # Track the highest used index
    max_n = 0

    # Treat old bare file (stem.csv) as occupying slot 1
    if (out_dir / f"{stem}.csv").exists():
        max_n = max(max_n, 1)

    # Scan numbered files stem_###.csv
    pat = re.compile(re.escape(stem) + r"_(\d{3})\.csv$", re.IGNORECASE)
    for p in out_dir.glob(f"{stem}_*.csv"):
        m = pat.search(p.name)
        if m:
            max_n = max(max_n, int(m.group(1)))

    # First numbered file if none exist → 001; otherwise increment
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

# ---------- Always return intensity for saving ----------
def read_intensity(spec, expected_len: int):
    """Always return intensity (not λ)."""
    sp = spec.acquire()
    if isinstance(sp, tuple) and len(sp) >= 2:
        return np.asarray(sp[1]).squeeze()
    if isinstance(sp, dict):
        for k in ("intensity", "y", "counts", "data"):
            if k in sp:
                return np.asarray(sp[k]).squeeze()
    arr = np.asarray(sp).squeeze()
    if arr.ndim == 2 and 2 in arr.shape:
        return arr[1, :] if arr.shape[0] == 2 else arr[-1, :]
    return arr

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

# -------------------------
# Main render
# -------------------------
def render(devices):
    st.header("⚡ MegaSweep (Vtg stripes, Vbg ratio-scaled step)")

    iv = devices.get("iv")
    spec = devices.get("spectrometer")
    lf6 = st.session_state.get("lf6")

    role_map = getattr(iv, "role_map", {}) if iv else {}
    axes_available = [k for k, v in (role_map or {}).items() if v is not None]
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

        c_r1, c_r2 = st.columns(2)
        with c_r1:
            go_step  = st.number_input("Ramp step (V) for ramping", value=0.1, min_value=0.001, format="%.3f")
        with c_r2:
            go_delay = st.number_input("Ramp delay (s)", value=0.02, min_value=0.0,   format="%.3f")

        # NEW: user-configurable center tolerance
        center_tol_nm = st.number_input("Center match tolerance (nm)", value=1.0, min_value=0.1, step=0.1)

        with st.expander("🛡️ Absolute Voltage Limits", expanded=True):
            c_s1, c_s2, c_s3, c_s4 = st.columns(4)
            with c_s1: lim_vtg_min = st.number_input("Min Vtg", value=-10.0)
            with c_s2: lim_vtg_max = st.number_input("Max Vtg", value= 10.0)
            with c_s3: lim_vbg_min = st.number_input("Min Vbg", value=-10.0)
            with c_s4: lim_vbg_max = st.number_input("Max Vbg", value= 10.0)

        st.subheader("2) Grids (Vtg stripes / Vbg linear)")

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
        is_snake = st.checkbox("Zig-Zag (snake inner order)", value=True)

        sim_vtg, sim_vbg = [], []
        flag = True
        for vtg in outer_vals:
            seq = inner_vals[::-1] if (is_snake and not flag) else inner_vals
            for vbg in seq:
                if lim_vtg_min <= vtg <= lim_vtg_max and lim_vbg_min <= vbg <= lim_vbg_max:
                    sim_vtg.append(vtg); sim_vbg.append(vbg)
            flag = not flag

        tab_v, tab_p = st.tabs(["⚡ Voltage Grid", "📐 Physics (analysis)"])
        with tab_v:
            fig, ax = plt.subplots(figsize=(3.5, 2.8))
            if sim_vtg:
                ax.scatter(sim_vtg, sim_vbg, s=8)
            rect = patches.Rectangle(
                (lim_vtg_min, lim_vbg_min),
                lim_vtg_max - lim_vtg_min,
                lim_vbg_max - lim_vbg_min,
                linewidth=1, fill=False, linestyle='--'
            )
            ax.add_patch(rect)
            ax.set_xlabel("Vtg (V)"); ax.set_ylabel("Vbg (V)")
            ax.grid(True, linestyle='--', alpha=0.5)
            fig.tight_layout()
            st.pyplot(fig)

        with tab_p:
            if sim_vtg:
                vtg_arr = np.array(sim_vtg)
                vbg_arr = np.array(sim_vbg)
                dop = ratio_val * vtg_arr + vbg_arr
                fld = ratio_val * vtg_arr - vbg_arr
                fig2, ax2 = plt.subplots(figsize=(3.5, 2.8))
                ax2.scatter(dop, fld, s=8)
                ax2.set_xlabel(f"D = r*Vtg + Vbg (r={ratio_val})")
                ax2.set_ylabel("F = r*Vtg - Vbg")
                ax2.grid(True, linestyle='--', alpha=0.5)
                fig2.tight_layout()
                st.pyplot(fig2)
            else:
                st.info("No points in preview (check limits).")

    # ==========================================
    # Run
    # ==========================================
    st.divider()
    c_run1, c_run2 = st.columns([1, 2])
    with c_run1:
        settle_delay = st.number_input("Settle after each point (s)", value=0.05, step=0.01)
    with c_run2:
        st.write(""); st.write("")
        can_run = iv is not None and spec is not None and {"Vtg","Vbg"}.issubset(set(axes_available))
        if st.button("🚀 START", type="primary", use_container_width=True, disabled=not can_run):
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

                # Header (wavelengths) — get λ DIRECTLY from LF6 and block until updated
                try:
                    wls = wait_lambda_from_lf6(
                        lf6,
                        target_center_nm=float(lf_center),
                        tol_nm=float(center_tol_nm),  # <- use UI tolerance
                        timeout_s=25.0, poll_s=0.3, require_consecutive=2
                    )
                except TimeoutError as e:
                    st.warning(str(e))
                    try:
                        wls = np.asarray(lf6.get_wavelength_calibration(), dtype=float).ravel()
                    except Exception:
                        wls = np.array([], dtype=float)

                # show actual mid (quantized)
                if wls.size > 1:
                    st.caption(f"λ mid ≈ {_midpoint_nm(wls):.3f} nm")

                h_row = np.concatenate((np.array(["Vbg","Vtg"], dtype='U'), wls.astype('U'))).reshape([1, -1])
                with open(file_path, 'a') as f:
                    np.savetxt(f, h_row, fmt='%s', delimiter=',')

                # Initial safe ramp to the first corner
                first_vtg = float(outer_vals[0]); first_vbg = float(inner_vals[0])
                iv.set_gates(Vtg=first_vtg, delay_s=go_delay, ramp_step=go_step)
                iv.set_gates(Vbg=first_vbg, delay_s=go_delay, ramp_step=go_step)
                time.sleep(settle_delay)

                # Sweep (outer = Vtg fixed, inner = Vbg with ratio-scaled step)
                total_pts = max(1, int(len(outer_vals) * len(inner_vals)))
                done, flag = 0, True
                prog = st.progress(0.0)

                with open(file_path, 'a') as f:
                    for vtg in outer_vals:
                        iv.set_gates(Vtg=float(vtg), delay_s=go_delay, ramp_step=go_step)

                        seq = inner_vals[::-1] if (is_snake and not flag) else inner_vals
                        for vbg in seq:
                            if not (lim_vtg_min <= vtg <= lim_vtg_max and lim_vbg_min <= vbg <= lim_vbg_max):
                                continue
                            iv.set_gates(Vbg=float(vbg), delay_s=go_delay, ramp_step=go_step)
                            time.sleep(settle_delay)

                            # Save INTENSITY aligned to wavelength header
                            y = np.asarray(read_intensity(spec, expected_len=len(wls))).reshape(-1)
                            if wls.size and y.size != wls.size:  # keep CSV rectangular
                                y = y[:min(wls.size, y.size)]
                            row = np.concatenate((np.array([vbg, vtg], ndmin=1, dtype=np.float64), y)).reshape([1, -1])
                            np.savetxt(f, row, fmt='%.5e', delimiter=',')

                            done += 1
                            if done % 5 == 0:
                                prog.progress(min(1.0, done / total_pts))
                        flag = not flag

                prog.progress(1.0)
                st.success("Done.")

                # Return to 0 safely
                try:
                    iv.set_gates(Vbg=0.0, delay_s=go_delay, ramp_step=go_step)
                    iv.set_gates(Vtg=0.0, delay_s=go_delay, ramp_step=go_step)
                except Exception:
                    pass

            except Exception as e:
                st.error(f"Error: {e}")
