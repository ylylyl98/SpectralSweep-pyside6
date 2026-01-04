import streamlit as st
import numpy as np
import time
import re
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from pathlib import Path

BASE_OUT = Path(r"D:\instrument_control_v3_1")

# --- HELPER FUNCTIONS ---
class SafeDict(dict):
    def __missing__(self, k): return f"{{{k}}}"

def sanitize_filename(s: str) -> str:
    for ch in '<>:"/\\|?*': s = s.replace(ch, "")
    return s.strip()

def unique_stem(out_dir: Path, stem: str) -> str:
    stem = sanitize_filename(stem)
    if not (out_dir / f"{stem}.csv").exists(): return stem
    max_n = 0
    pat = re.compile(re.escape(stem) + r"_(\d{3})\.csv$", re.IGNORECASE)
    for p in out_dir.glob(f"{stem}_*.csv"):
        m = pat.search(p.name)
        if m: max_n = max(max_n, int(m.group(1)))
    return f"{stem}_{max_n+1:03d}"

def format_exposure_for_filename(ms_val):
    seconds = ms_val / 1000.0
    return f"{int(seconds)}" if seconds.is_integer() else f"{seconds:.2g}"

def get_sweep_array(start, stop, param, mode):
    if mode == "Total Points":
        return np.linspace(start, stop, int(param))
    else: # Step Size
        step = float(param)
        if step <= 0: return np.array([start])
        if stop < start: step = -abs(step)
        else: step = abs(step)
        count = int(np.floor((stop - start) / step) + 1.00001)
        return start + (np.arange(count) * step)

# --- MAIN RENDER ---
def render(devices):
    st.markdown("""
        <style>
        .block-container { padding-top: 1rem; padding-bottom: 2rem; }
        div[data-testid="stExpander"] div[role="button"] p { font-size: 1.0rem; font-weight: bold; }
        </style>
    """, unsafe_allow_html=True)

    st.header("⚡ MegaSweep (Zig-Zag Map)")
    
    # HARDWARE CHECK
    iv = devices.get("iv")
    spec = devices.get("spectrometer")
    lf6 = st.session_state.get("lf6")

    missing = []
    if not iv: missing.append("IV")
    if not spec: missing.append("Spec")
    if not lf6: missing.append("LF")
    
    valid_hardware_axes = []
    if iv and hasattr(iv, 'role_map'):
        valid_hardware_axes = [k for k, v in iv.role_map.items() if v is not None]
    
    fixed_options = ["None"] + ["Vtg", "Vbg", "Vbias"]
    fixed_options = list(dict.fromkeys(fixed_options))
    default_fix_idx = fixed_options.index("Vbias") if "Vbias" in valid_hardware_axes else 0

    # ==========================================
    # 1. TOP BAR: FILE & OPTICAL
    # ==========================================
    with st.expander("📂 File, Optical & Pattern Settings", expanded=False):
        c_id1, c_id2, c_id3 = st.columns([1, 1, 2])
        with c_id1:
            if "shared_sample_name" not in st.session_state:
                st.session_state.shared_sample_name = "YZD219"
            sample = st.text_input("Sample Name", key="shared_sample_name")
        with c_id2:
            tag = st.text_input("Tag / Desc", "Megasweep1")
        with c_id3:
            subfolder = "megasweep"
            out_path = BASE_OUT / sample / subfolder
            st.text_input("Save Path", str(out_path), disabled=True)

        c_opt1, c_opt2, c_opt3, c_opt4, c_opt5 = st.columns(5)
        with c_opt1: laser_nm = st.text_input("Laser (nm)", "730")
        with c_opt2: power_uw = st.text_input("Power (µW)", "1")
        with c_opt3: lf_exp = st.number_input("Exp (ms)", value=1000.0, step=100.0)
        with c_opt4: lf_epf = st.number_input("Accums", value=2, min_value=1)
        with c_opt5: lf_center = st.number_input("Center (nm)", value=885.0, step=1.0)
        
        default_pattern = "${sample}$~${tag}$~$6KPL{laser_nm}nm{power_uw}uw{exp_s}sx{epf}$~${center_nm}nmc$"
        pattern = st.text_input("Filename Pattern", value=default_pattern)

    # ==========================================
    # MAIN SPLIT LAYOUT
    # ==========================================
    col_ctrl, col_view = st.columns([1.2, 1], gap="medium")

    # --- INIT VARIABLES ---
    outer_vals, inner_vals = [], []
    ratio_val = 1.0

    with col_ctrl:
        st.subheader("1. Sweep Setup")
        
        # A. SWEEP MODE & FIXED
        c_mode, c_fix_ax, c_fix_val = st.columns([1.5, 1, 1])
        with c_mode:
            sweep_mode = st.selectbox("Sweep Mode", ["Compensated (Ratio Locked)", "Independent (Rectangle)"])
        with c_fix_ax:
            fixed_ax = st.selectbox("Fixed Axis", fixed_options, index=default_fix_idx)
        with c_fix_val:
            if fixed_ax != "None":
                fixed_val = st.number_input(f"{fixed_ax} (V)", value=0.0, step=0.01)
            else:
                st.write(""); fixed_val = 0.0

        st.markdown("---")

        # B. GRID DEFINITION
        step_mode = st.radio("Grid Type:", ["Step Size (Grid)", "Total Points"], horizontal=True)

        # C. SWEEP LOOPS CONFIG
        if sweep_mode.startswith("Compensated"):
            # RATIO MODE
            c_rat, c_dum = st.columns([1, 1])
            with c_rat: 
                ratio_val = st.number_input(
                    "Ratio (Doping Efficiency)", 
                    value=1.0, step=0.1, 
                    help="Doping ~ Ratio*Tg + Bg.\nIf Ratio=1, Sweep is standard Rectangle."
                )

            st.caption("**Outer Loop: Vtg**")
            c_o1, c_o2, c_o3 = st.columns([1, 1, 1])
            with c_o1: o_start = st.number_input("Vtg Start", value=-5.0, key="vtg_s")
            with c_o2: o_stop  = st.number_input("Vtg Stop", value=5.0, key="vtg_e")
            with c_o3: 
                if step_mode == "Total Points": o_param = st.number_input("Vtg Pts", value=51, min_value=2, key="vtg_p")
                else: o_param = st.number_input("Vtg Step", value=0.2, min_value=0.001, format="%.3f", key="vtg_step")

            st.caption("**Inner Loop: Vbg** (Base Range)")
            c_i1, c_i2, c_i3 = st.columns([1, 1, 1])
            with c_i1: i_start = st.number_input("Vbg Start", value=-5.0, key="vbg_s")
            with c_i2: i_stop  = st.number_input("Vbg Stop", value=5.0, key="vbg_e")
            with c_i3: 
                if step_mode == "Total Points": i_param = st.number_input("Vbg Pts", value=51, min_value=2, key="vbg_p")
                else: i_param = st.number_input("Vbg Step", value=0.2, min_value=0.001, format="%.3f", key="vbg_step")

            outer_vals = get_sweep_array(o_start, o_stop, o_param, step_mode)
            inner_vals = get_sweep_array(i_start, i_stop, i_param, step_mode)
            outer_ax, inner_ax = "Vtg", "Vbg_Scan"

        else: # Independent
            remaining_axes = [x for x in ["Vtg", "Vbg", "Vbias"] if x != fixed_ax]
            c_o1, c_o2, c_o3, c_o4 = st.columns([1.2, 1, 1, 1])
            with c_o1: outer_ax = st.selectbox("Outer (Slow)", remaining_axes, index=0)
            with c_o2: o_start = st.number_input("Start", value=-7.0, key="os_std")
            with c_o3: o_stop  = st.number_input("Stop", value=7.0, key="oe_std")
            with c_o4: 
                if step_mode == "Total Points": o_param = st.number_input("Pts", value=71, min_value=2, key="op_std")
                else: o_param = st.number_input("Step", value=0.2, min_value=0.001, key="op_st")
            
            final_inner = [x for x in remaining_axes if x != outer_ax]
            c_i1, c_i2, c_i3, c_i4 = st.columns([1.2, 1, 1, 1])
            with c_i1: inner_ax = st.selectbox("Inner (Fast)", final_inner, index=0)
            with c_i2: i_start = st.number_input("Start", value=-7.0, key="is_std")
            with c_i3: i_stop  = st.number_input("Stop", value=7.0, key="ie_std")
            with c_i4: 
                if step_mode == "Total Points": i_param = st.number_input("Pts", value=71, min_value=2, key="ip_std")
                else: i_param = st.number_input("Step", value=0.2, min_value=0.001, key="ip_st")
            
            outer_vals = get_sweep_array(o_start, o_stop, o_param, step_mode)
            inner_vals = get_sweep_array(i_start, i_stop, i_param, step_mode)
            st.session_state.safety_vtg_min = -100 

        # C. SAFETY LIMITS
        with st.expander("🛡️ Absolute Voltage Limits (Safety)", expanded=True):
            c_s1, c_s2, c_s3, c_s4 = st.columns(4)
            with c_s1: lim_vtg_min = st.number_input("Min Vtg", value=-20.0)
            with c_s2: lim_vtg_max = st.number_input("Max Vtg", value=20.0)
            with c_s3: lim_vbg_min = st.number_input("Min Vbg", value=-20.0)
            with c_s4: lim_vbg_max = st.number_input("Max Vbg", value=20.0)

    # --- RIGHT COLUMN: PREVIEW & ACTION ---
    with col_view:
        is_snake = st.checkbox("Zig-Zag (Snake)", value=True)
        
        sim_vtg, sim_vbg = [], []
        skip_vtg, skip_vbg = [], [] # TRACKING SKIPPED POINTS
        flag = True
        
        for o_v in outer_vals:
            curr_inner = np.flip(inner_vals) if (is_snake and not flag) else inner_vals
            for i_v in curr_inner:
                if sweep_mode == "Independent (Rectangle)":
                    val_vtg = fixed_val if (fixed_ax == "Vtg") else 0.0
                    val_vbg = fixed_val if (fixed_ax == "Vbg") else 0.0
                    if outer_ax == "Vtg": val_vtg = o_v
                    elif outer_ax == "Vbg": val_vbg = o_v
                    if inner_ax == "Vtg": val_vtg = i_v
                    elif inner_ax == "Vbg": val_vbg = i_v
                else:
                    val_vtg = o_v
                    val_vbg = i_v + (1.0 - ratio_val) * val_vtg

                # CHECK LIMITS
                if not (lim_vtg_min <= val_vtg <= lim_vtg_max and lim_vbg_min <= val_vbg <= lim_vbg_max):
                    skip_vtg.append(val_vtg)
                    skip_vbg.append(val_vbg)
                    continue
                
                sim_vtg.append(val_vtg)
                sim_vbg.append(val_vbg)  
            flag = not flag

        skipped_count = len(skip_vtg)

        # PLOTS
        tab_v, tab_p = st.tabs(["⚡ Voltage", "⚛️ Physics"])
        
        with tab_v:
            fig, ax = plt.subplots(figsize=(3.5, 2.8))
            
            # 1. Plot Valid
            if len(sim_vtg) > 0:
                ax.scatter(sim_vtg, sim_vbg, c=range(len(sim_vtg)), cmap='viridis', s=8, label="Valid")
            
            # 2. Plot Skipped (Red Ghost)
            if skipped_count > 0:
                ax.scatter(skip_vtg, skip_vbg, c='red', alpha=0.15, s=8, label="Skipped", marker='x')

            # 3. Draw Safety Box
            rect = patches.Rectangle(
                (lim_vtg_min, lim_vbg_min), 
                lim_vtg_max - lim_vtg_min, 
                lim_vbg_max - lim_vbg_min, 
                linewidth=1, edgecolor='r', facecolor='none', linestyle='--', label="Safety Limit"
            )
            ax.add_patch(rect)

            ax.set_xlabel("Vtg (V)"); ax.set_ylabel("Vbg (V)")
            ax.grid(True, linestyle='--', alpha=0.5)
            # ax.legend(fontsize=6, loc='upper right')
            fig.tight_layout()
            st.pyplot(fig)

        with tab_p:
            fig2, ax2 = plt.subplots(figsize=(3.5, 2.8))
            calc_ratio = ratio_val if sweep_mode.startswith("Compensated") else 1.0
            
            # Convert both Valid and Skipped to Physics Space
            if len(sim_vtg) > 0:
                dop = (calc_ratio * np.array(sim_vtg)) + np.array(sim_vbg)
                fld = (calc_ratio * np.array(sim_vtg)) - np.array(sim_vbg)
                ax2.scatter(dop, fld, c=range(len(dop)), cmap='coolwarm', s=8)
            
            if skipped_count > 0:
                dop_s = (calc_ratio * np.array(skip_vtg)) + np.array(skip_vbg)
                fld_s = (calc_ratio * np.array(skip_vtg)) - np.array(skip_vbg)
                ax2.scatter(dop_s, fld_s, c='red', alpha=0.15, s=8, marker='x')

            ax2.set_xlabel(f"Doping ({calc_ratio}*Tg + Bg)")
            ax2.set_ylabel(f"Field ({calc_ratio}*Tg - Bg)")
            ax2.grid(True, linestyle='--', alpha=0.5)
            fig2.tight_layout()
            st.pyplot(fig2)
            
            if skipped_count > 0: 
                st.warning(f"⚠️ {skipped_count} points will be skipped (Red 'x').")

        # ACTION
        st.divider()
        c_run1, c_run2 = st.columns([1, 1.5])
        with c_run1:
            delay_s = st.number_input("Delay (s)", value=0.05, step=0.01)
        with c_run2:
            st.write("") 
            st.write("") 
            errors = []
            if len(missing) > 0: errors.append(f"Missing: {missing}")
            if fixed_ax != "None" and valid_hardware_axes and fixed_ax not in valid_hardware_axes: st.warning(f"Check {fixed_ax}")
            can_run = (len(errors) == 0)
            
            if st.button("🚀 START", type="primary", use_container_width=True, disabled=not can_run):
                def set_hard_param(axis, val, dly):
                    if valid_hardware_axes and axis not in valid_hardware_axes: return
                    if axis == "Vbias": iv.set_bias(val); time.sleep(dly)
                    else: iv.set_gates(**{axis: val}, delay_s=dly)

                try:
                    out_path.mkdir(parents=True, exist_ok=True)
                    final_stem = unique_stem(out_path, base_name)
                    file_path = out_path / f"{final_stem}.csv"
                    
                    if fixed_ax != "None": set_hard_param(fixed_ax, fixed_val, 0.1)
                    if hasattr(lf6, 'change_spectra_center'): lf6.change_spectra_center(lf_center)
                    if hasattr(lf6, 'change_expose_time'): lf6.change_expose_time(lf_exp)
                    if hasattr(lf6, 'set_frames'): lf6.set_frames(lf_epf)
                    elif hasattr(lf6, 'set_accumulations'): lf6.set_accumulations(lf_epf)
                    wls = spec.calibration_wavelengths()
                    
                    # Header
                    if sweep_mode == "Independent (Rectangle)":
                        h_names = [inner_ax, outer_ax]
                    else:
                        h_names = ["Vbg_Scan_Val", "Vtg_Outer", "Calculated_Vbg"]

                    h_row = np.concatenate((np.array(h_names, dtype='U'), wls.astype('U'))).reshape([1, -1])
                    with open(file_path, 'a') as f: np.savetxt(f, h_row, fmt='%s', delimiter=',')

                    prog = st.progress(0.0)
                    ctr = 0
                    total_pts = len(outer_vals) * len(inner_vals)

                    flg = True
                    for o_v in outer_vals:
                        if sweep_mode.startswith("Independent"): 
                            set_hard_param(outer_ax, o_v, delay_s*2)
                        else:
                            set_hard_param("Vtg", o_v, delay_s*2)

                        curr_in = np.flip(inner_vals) if (is_snake and not flg) else inner_vals
                        for i_v in curr_in:
                            if sweep_mode.startswith("Independent"):
                                set_hard_param(inner_ax, i_v, delay_s)
                                saved = np.array([i_v, o_v])
                            else:
                                vtg_t = o_v
                                vbg_t = i_v + (1.0 - ratio_val) * vtg_t
                                if not (lim_vtg_min <= vtg_t <= lim_vtg_max and lim_vbg_min <= vbg_t <= lim_vbg_max): continue
                                set_hard_param("Vbg", vbg_t, delay_s)
                                saved = np.array([i_v, o_v, vbg_t])

                            sp = np.array(spec.acquire()).reshape(-1)
                            d_row = np.concatenate((saved, sp)).reshape([1, -1])
                            with open(file_path, 'a') as f: np.savetxt(f, d_row, fmt='%.5e', delimiter=',')
                            ctr += 1
                            if ctr % 5 == 0: prog.progress(min(1.0, ctr / total_pts))
                        flg = not flg
                    
                    prog.progress(1.0)
                    st.success("Done!")
                    if "Vtg" in valid_hardware_axes: iv.set_gates(Vbg=0, Vtg=0, delay_s=0.1)
                except Exception as e: st.error(f"Error: {e}")