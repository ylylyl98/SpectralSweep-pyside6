import streamlit as st
import pandas as pd
from pathlib import Path
import re
import time
import itertools
import numpy as np
from app.steps.registry import build_recipe_from_preset
from app.engine.runner import runner_singleton
import app.steps._import_all  # noqa: F401

BASE_OUT = Path(r"D:\instrument_control_v3_1")

# ---------- helpers ----------
INVALID_CHARS = '<>:"/\\|?*'
def sanitize_filename(s: str) -> str:
    for ch in INVALID_CHARS:
        s = s.replace(ch, "")
    return s.strip()

class SafeDict(dict):
    def __missing__(self, k): return ""

def commit_preview_to_table():
    """Add the SINGLE preview row and clear it."""
    if "preview_row" in st.session_state:
        # 1. Get the single row dictionary
        row_data = st.session_state.preview_row
        
        # 2. Create a DataFrame from that ONE row
        new_row_df = pd.DataFrame([row_data])
        
        # 3. Add to the main table
        st.session_state.batch_df = pd.concat(
            [st.session_state.batch_df, new_row_df], 
            ignore_index=True
        )
        
        # 4. Clear the preview so it can't be added again
        del st.session_state.preview_row

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


def parse_values(s: str):
    if not s or not str(s).strip(): return None
    try: return [float(x.strip()) for x in str(s).split(",") if x.strip()]
    except ValueError: return None

# ---------- wavelength helpers (NEW) ----------
def _midpoint_nm(wls: np.ndarray) -> float:
    return 0.5 * (float(wls[0]) + float(wls[-1])) if wls.size > 1 else float("nan")

def wait_lambda_from_lf6(lf6, target_center_nm: float,
                         tol_nm: float = 1.0,
                         timeout_s: float = 25.0,
                         poll_s: float = 0.3,
                         require_consecutive: int = 2) -> np.ndarray:
    """
    Poll lf6.get_wavelength_calibration() until mid-λ ~= target center.
    Returns a numpy array; raises TimeoutError on failure.
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



# ---------- UI ----------
def render(devices, wavelength_headers, extra_scalar_fields_order):
    
    # 1. DEFINE PARAMETERS (Crucial fix: must be defined before use)
    PARAM_TYPES = [
        "Center Wavelength (nm)",
        "Exposure Time (ms)",
        "Stage Position (0-3600)",
        "Accumulations (EPF)"
    ]

    st.header("Dual Gate Sweep – Advanced Looper")

    c_settings = st.container()
    c_looper   = st.container()
    c_table    = st.container()
    c_log      = st.container()

    # --- 1. GLOBAL SETTINGS ---
    with c_settings:
        with st.expander("Base Settings & Defaults", expanded=False):
            c1, c2, c3 = st.columns(3)
            with c1: 
                if "shared_sample_name" not in st.session_state:
                    st.session_state.shared_sample_name = "YZD219"
                sample_name = st.text_input("Sample name", key="shared_sample_name")
            with c2: subfolder   = st.text_input("Subfolder", "Initial data")
            with c3: tag         = st.text_input("Tag", "p1")

            m1, m2, m3, m4 = st.columns(4)
            with m1: def_laser  = st.text_input("Laser λ (nm)", "730")
            with m2: def_power  = st.text_input("Power (µW)", "1")
            with m3: def_exp    = st.text_input("Default Exp (ms)", "1000")
            with m4: def_center = st.text_input("Default Center (nm)", "885")
            
            def_epf = st.number_input("Default Accumulations (EPF)", 1, 1000, 2)
            center_tol_nm = st.number_input("Center match tolerance (nm)", value=1.0, min_value=0.1, step=0.1)

            pattern = st.text_input("Filename pattern", 
                "${sample}$~${tag}$~$6KPL{laser_nm}nm{power_uw}uw{exp_s}sx{epf}$~${center_nm}nmc$_${cond_block}$")

    out_dir = (BASE_OUT / sample_name / subfolder)

    # --- 2. ADVANCED LOOPER ---
    with c_looper:
        st.info("👇 **Loop Builder**")

        # -----------------------------------------------------------
        # A. SWEEP MODE SELECTOR
        # -----------------------------------------------------------
        sweep_mode = st.radio(
            "Sweep Logic", 
            ["Grid Scan (Nested)", "Synchronized (Zipped)", "Custom (Advanced)"],
            horizontal=True,
            help="Grid: Test every combination.\nSynced: Change all parameters together.\nCustom: Use Levels manually."
        )

        # -----------------------------------------------------------
        # B. DATA PREPARATION
        # -----------------------------------------------------------
        if "loop_df" not in st.session_state:
            st.session_state.loop_df = pd.DataFrame([
                {"Enable": True,  "Parameter": "Center Wavelength (nm)", "Values": "800, 850", "Level": 1},
                {"Enable": True,  "Parameter": "Exposure Time (ms)",     "Values": "100, 500", "Level": 1},
                {"Enable": False, "Parameter": "Stage Position (0-3600)","Values": "0, 10",    "Level": 2},
            ])

        # 01. Ensure Level is always Integer (Prevents 2.0 vs 2 type conflicts)
        st.session_state.loop_df["Level"] = st.session_state.loop_df["Level"].astype(int)

        # 02. Reset Index (fixes "ghost" rows from sorting/filtering)
        st.session_state.loop_df = st.session_state.loop_df.reset_index(drop=True)

        # -----------------------------------------------------------
        # C. RENDER EDITOR (FIXED)
        # -----------------------------------------------------------
        
        # 01. Define Config
        # We define the "Level" config here, but we might set it to None (Hidden) later.
        col_config = {
            "Enable": st.column_config.CheckboxColumn("On", width="small"),
            "Parameter": st.column_config.SelectboxColumn("Parameter", width="medium", options=PARAM_TYPES),
            "Values": st.column_config.TextColumn("Values (comma sep)", width="large"),
            "Level": st.column_config.NumberColumn("Level (1=Outer)", min_value=1, max_value=5, step=1),
        }

        # 02. HIDE Level column if not in Custom mode (Instead of dropping it)
        # Setting a column config to None hides it from the UI but keeps the data!
        if sweep_mode != "Custom (Advanced)":
            col_config["Level"] = None

        # 03. Render Editor
        # CRITICAL FIX: Pass st.session_state.loop_df DIRECTLY. 
        # Do not use .copy() and do not use an intermediate variable like 'display_df'.
        edited_df = st.data_editor(
            st.session_state.loop_df,
            column_config=col_config,
            width="stretch",
            num_rows="dynamic",
            key="loop_editor"
        )

        # 04. Update Logic
        # Now we can safely copy the output to process the logic
        final_df = edited_df.copy()
        
        if sweep_mode == "Grid Scan (Nested)":
            levels = []
            curr_level = 1
            for enabled in final_df["Enable"]:
                levels.append(curr_level)
                if enabled: curr_level += 1
            final_df["Level"] = levels
            st.caption("ℹ️ Logic: Top row is Outer Loop.")

        elif sweep_mode == "Synchronized (Zipped)":
            final_df["Level"] = 1
            st.caption("ℹ️ Logic: All parameters change simultaneously.")

        # Save back to session state
        st.session_state.loop_df = final_df

        # -----------------------------------------------------------
        # E. VISUAL PREVIEW (Tree / Flowchart)
        # -----------------------------------------------------------
        with st.expander("Show Sequence Preview"):
            active_rows = final_df[final_df["Enable"] == True].copy()
            
            if active_rows.empty:
                st.warning("No parameters enabled.")
            else:
                try:
                    # 1. PARSE DATA
                    active_rows = active_rows.sort_values("Level")
                    levels = {}
                    for _, row in active_rows.iterrows():
                        lvl = int(row["Level"])
                        parsed_vals = parse_values(row["Values"])
                        if parsed_vals:
                            if lvl not in levels: levels[lvl] = []
                            levels[lvl].append({"param": row["Parameter"], "vals": parsed_vals})

                    # 2. GENERATE GRAPHVIZ DOT CODE
                    dot = ['digraph G {']
                    dot.append('  rankdir=LR;')  # Left-to-Right layout
                    dot.append('  node [shape=box, style="filled,rounded", fillcolor="#f0f2f6", fontname="Sans", fontsize=10];')
                    dot.append('  edge [color="#888888"];')
                    dot.append('  START [shape=ellipse, fillcolor="#d4edda", label="Start"];')

                    # Helper to limit massive trees
                    MAX_BRANCHES = 3 

                    # RECURSIVE BUILDER (Handles Grid, Custom, and Sync)
                    def build_tree(parent_id, level_idx):
                        sorted_levels = sorted(levels.keys())
                        
                        # Base case: No more levels
                        if level_idx >= len(sorted_levels):
                            return

                        curr_lvl = sorted_levels[level_idx]
                        specs = levels[curr_lvl]
                        
                        # Check lengths if multiple params are on this level (Zipped)
                        lengths = [len(s["vals"]) for s in specs]
                        if len(set(lengths)) > 1:
                            # Error visualization
                            err_id = f"ERR_{curr_lvl}"
                            dot.append(f'  "{err_id}" [label="Error: Level {curr_lvl}\nLengths mismatch!" fillcolor="#ffcccc"];')
                            dot.append(f'  "{parent_id}" -> "{err_id}";')
                            return

                        # Get values for this level (Zipped together)
                        all_vals = list(zip(*[s["vals"] for s in specs]))
                        
                        # Limit branches to avoid UI freeze
                        display_vals = all_vals[:MAX_BRANCHES]
                        has_more = len(all_vals) > MAX_BRANCHES

                        for i, val_tuple in enumerate(display_vals):
                            # Build Label (e.g. "Wave: 800\nExp: 100")
                            label_lines = []
                            for s_idx, s in enumerate(specs):
                                p_name = s["param"].split("(")[0].strip() # Shorten name
                                p_val = val_tuple[s_idx]
                                label_lines.append(f"{p_name}: {p_val}")
                            
                            node_label = "\\n".join(label_lines)
                            node_id = f"{parent_id}_L{curr_lvl}_N{i}"
                            
                            dot.append(f'  "{node_id}" [label="{node_label}"];')
                            dot.append(f'  "{parent_id}" -> "{node_id}";')
                            
                            # Recurse to next level
                            build_tree(node_id, level_idx + 1)

                        if has_more:
                            dots_id = f"{parent_id}_more"
                            dot.append(f'  "{dots_id}" [label="... ({len(all_vals)-MAX_BRANCHES} more)" shape=plaintext style=filled fillcolor=none];')
                            dot.append(f'  "{parent_id}" -> "{dots_id}" [style=dotted];')

                    # 3. BUILD & RENDER
                    build_tree("START", 0)
                    
                    dot.append('}')
                    st.graphviz_chart("\n".join(dot), width="content")
                    
                    # Stats
                    total_steps = 1
                    for lvl in levels:
                        total_steps *= len(levels[lvl][0]['vals'])
                    st.caption(f"Total Sequences: {total_steps}")

                except ImportError:
                    st.error("Graphviz not installed.")
                except Exception as e:
                    st.error(f"Preview Error: {e}")
        

    # --- 3. BATCH TABLE (With 'Measure Power' Column) ---
    with c_table:
        if "batch_df" not in st.session_state:
            st.session_state.batch_df = pd.DataFrame([
                {"Run": True, "repeat": 1, "MeasurePower": False, "condition_label":"BGonly", 
                 "Vbg_start":0.0,"Vbg_stop":0.5,"Vtg_start":0.0,"Vtg_stop":0.0,"frames":11,"Vbias":""},
            ])
        
        st.write("---")
        st.write("**Inner Electrical Sweep**")

        # ==========================================
        # ⚡ NEW: PREVIEW & ADD GENERATOR
        # ==========================================
        with st.expander("⚡ Quick Generator: Coupled Sweep (Ratio * TG ± BG = K)"):
            
            # --- 1. INPUT FORM ---
            with st.form("eqn_preview_form"):
                st.write("**1. Define Equation:** `Ratio * TG [±] BG = Constant`")
                
                c_eq1, c_eq2, c_eq3, c_eq4 = st.columns([1.5, 1, 1, 1.5])
                with c_eq1: 
                    ratio = st.number_input("Ratio", value=0.80, step=0.05, format="%.2f")
                with c_eq2: 
                    op_mode = st.selectbox("Operator", ["-", "+"], help="'-' for TG-BG, '+' for TG+BG")
                with c_eq3: 
                    st.markdown("<div style='text-align: center; padding-top: 35px;'><b>BG = </b></div>", unsafe_allow_html=True)
                with c_eq4: 
                    constant = st.number_input("Constant (K)", value=5.0, step=0.5)

                st.write("**2. Define Hardware Limits (Safe Box)**")
                c_lim1, c_lim2, c_lim3 = st.columns([1.5, 1.5, 1])
                with c_lim1:
                    lim_vtg_min = st.number_input("Vtg Min", value=-5.0)
                    lim_vtg_max = st.number_input("Vtg Max", value=5.0)
                with c_lim2:
                    lim_vbg_min = st.number_input("Vbg Min", value=-5.0)
                    lim_vbg_max = st.number_input("Vbg Max", value=5.0)
                with c_lim3:
                    # FIX: step matches min_value decimals to avoid validation errors
                    step_size = st.number_input("Step (V)", min_value=0.001, max_value=1.0, value=0.050, step=0.001, format="%.3f")
                    reverse = st.checkbox("Reverse?", value=True)

                # Auto-labeling
                op_char = "-" if op_mode == "-" else "+"
                default_label = f"{ratio}TG{op_char}BG={constant}"
                final_label = st.text_input("Row Label", default_label)

                # Submit Button just for Preview
                calc_btn = st.form_submit_button("🔍 Calculate / Preview")

            # --- 2. CALCULATION LOGIC ---
            if calc_btn:
                # 1. Convert Equation variables
                if op_mode == "-":
                    slope = ratio
                    offset = -constant
                else:
                    slope = -ratio
                    offset = constant

                # 2. Find Intersections with Box Limits
                points = []
                
                # Check Vtg Walls (Vertical)
                y_at_min = slope * lim_vtg_min + offset
                if lim_vbg_min <= y_at_min <= lim_vbg_max: 
                    points.append((lim_vtg_min, y_at_min))
                
                y_at_max = slope * lim_vtg_max + offset
                if lim_vbg_min <= y_at_max <= lim_vbg_max: 
                    points.append((lim_vtg_max, y_at_max))

                # Check Vbg Walls (Horizontal)
                if abs(slope) > 1e-9:
                    x_at_min = (lim_vbg_min - offset) / slope
                    # Check bounds and avoid duplicate corner points
                    if lim_vtg_min <= x_at_min <= lim_vtg_max:
                        if not any(abs(x_at_min - p[0]) < 1e-5 for p in points): 
                            points.append((x_at_min, lim_vbg_min))
                    
                    x_at_max = (lim_vbg_max - offset) / slope
                    if lim_vtg_min <= x_at_max <= lim_vtg_max:
                        if not any(abs(x_at_max - p[0]) < 1e-5 for p in points): 
                            points.append((x_at_max, lim_vbg_max))
                
                # 3. Validation & Storage
                if len(points) < 2:
                    st.error(f"❌ Line '{final_label}' does not pass through the safety limits!")
                    # CRITICAL: Remove any stale preview if calculation fails
                    st.session_state.pop("preview_row", None) 
                else:
                    # Sort points to ensure correct direction
                    points.sort(key=lambda p: p[0])
                    p_start, p_stop = points[0], points[-1]
                    if reverse: 
                        p_start, p_stop = p_stop, p_start

                    # Calculate Frames
                    max_dist = max(abs(p_stop[0] - p_start[0]), abs(p_stop[1] - p_start[1]))
                    frames = int(round(max_dist / step_size)) + 1
                    
                    # CRITICAL: Overwrite session state with a SINGLE dictionary
                    # Do not use .append() here!
                    st.session_state.preview_row = {
                        "Run": True, "repeat": 1, "MeasurePower": False, "condition_label": final_label,
                        "Vtg_start": float(f"{p_start[0]:.4f}"), 
                        "Vtg_stop":  float(f"{p_stop[0]:.4f}"),
                        "Vbg_start": float(f"{p_start[1]:.4f}"), 
                        "Vbg_stop":  float(f"{p_stop[1]:.4f}"),
                        "frames": frames, "Vbias": ""
                    }

            # --- 3. PREVIEW & ADD UI ---
            if "preview_row" in st.session_state:
                p_data = st.session_state.preview_row
                
                st.markdown("---")
                st.markdown("##### 📊 Result Preview")
                
                # Display Metrics
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Vbg Sweep", f"{p_data['Vbg_start']} → {p_data['Vbg_stop']} V")
                m2.metric("Vtg Sweep", f"{p_data['Vtg_start']} → {p_data['Vtg_stop']} V")
                m3.metric("Frames", p_data['frames'])
                m4.metric("Label", p_data['condition_label'])

                # Add Button with CALLBACK
                col_add, col_cancel = st.columns([1, 4])
                with col_add:
                    # USE 'on_click' to trigger the function defined above
                    st.button("➕ Add Row to Table", type="primary", on_click=commit_preview_to_table)
                
                with col_cancel:
                    if st.button("Cancel"):
                        del st.session_state.preview_row
                        st.rerun()


        with st.form("batch_form"):
            df_batch = st.data_editor(
                st.session_state.batch_df,
                num_rows="dynamic",
                hide_index=True,
                column_config={
                    "Run": st.column_config.CheckboxColumn("Run", width="small"),
                    "repeat": st.column_config.NumberColumn("Rep", min_value=1, max_value=100, step=1, width="small", help="Repeat this row N times"),
                    "MeasurePower": st.column_config.CheckboxColumn("Meas Pwr?", help="If checked, measures PM100D and saves value to filename."),
                    "condition_label": st.column_config.TextColumn("Label"),
                    "Vbg_start": st.column_config.NumberColumn("Vbg Start"),
                    "Vbg_stop": st.column_config.NumberColumn("Vbg Stop"),
                    "Vtg_start": st.column_config.NumberColumn("Vtg Start"),
                    "Vtg_stop": st.column_config.NumberColumn("Vtg Stop"),
                    "frames": st.column_config.NumberColumn("Frames"),
                    "Vbias": st.column_config.TextColumn("Vbias"),
                },
                column_order=["Run", "repeat", "MeasurePower", "condition_label", "Vbg_start", "Vbg_stop", "Vtg_start", "Vtg_stop", "frames", "Vbias"]
            )
            run_btn = st.form_submit_button("Run Sequence")

    # --- 4. LOG ---
    with c_log:
        st.markdown("---")
        log_exp = st.expander("Run log", expanded=True)
        log_box = log_exp.empty()
        if "run_log" not in st.session_state: st.session_state.run_log = []
        
        def ui_log(msg: str):
            st.session_state.run_log.append(str(msg))
            log_box.markdown(
                f"<div style='font-family:monospace;white-space:pre-wrap;'>{chr(10).join(st.session_state.run_log[-200:])}</div>", 
                unsafe_allow_html=True
            )

        if log_exp.button("Clear Log"):
            st.session_state.run_log = []
            log_box.empty()

    # --- EXECUTION LOGIC ---
    if run_btn:
        # [LOOP PARSING LOGIC]
        # Use final_df (calculated above) instead of raw session state if possible, 
        # but here we use session_state.loop_df because it was updated at end of UI block.
        loop_df = st.session_state.loop_df
        active_rows = loop_df[loop_df["Enable"] == True].copy()
        
        if active_rows.empty: 
            levels = {} 
        else:
            active_rows = active_rows.sort_values("Level")
            levels = {}
            for _, row in active_rows.iterrows():
                lvl = int(row["Level"])
                parsed_vals = parse_values(row["Values"])
                if parsed_vals:
                    if lvl not in levels: levels[lvl] = []
                    levels[lvl].append({"param": row["Parameter"], "vals": parsed_vals})

        level_combinations = []
        for lvl in sorted(levels.keys()):
            specs = levels[lvl]
            lengths = [len(s["vals"]) for s in specs]
            if len(set(lengths)) > 1:
                st.error(f"Error Level {lvl}: Zipped params must have same length! {lengths}")
                return
            zipped_vals = list(zip(*[s["vals"] for s in specs]))
            param_names = [s["param"] for s in specs]
            level_dicts = []
            for tuple_val in zipped_vals:
                level_dicts.append({k: v for k, v in zip(param_names, tuple_val)})
            level_combinations.append(level_dicts)

        if not level_combinations: 
            final_sequence = [{}] 
        else:
            final_sequence = []
            for combo in itertools.product(*level_combinations):
                merged = {}
                for d in combo: merged.update(d)
                final_sequence.append(merged)

        ui_log(f"Generated {len(final_sequence)} conditions.")

        # [RUN LOOP]
        total = len(final_sequence)
        for seq_i, ctx_vars in enumerate(final_sequence):
            ui_log(f"--- Sequence {seq_i+1}/{total} ---")
            
            val_center = ctx_vars.get("Center Wavelength (nm)", float(def_center))
            val_exp    = ctx_vars.get("Exposure Time (ms)",     float(def_exp))
            val_stage  = ctx_vars.get("Stage Position (0-3600)",    None)
            val_epf    = int(ctx_vars.get("Accumulations (EPF)", def_epf))
            
            # Default 'nominal' power from UI textbox
            val_power_nominal = def_power

            # Move Stage
            if val_stage is not None:
                stage = devices.get("stage")
                if stage:
                    ui_log(f"Moving Stage to {val_stage} mm...")
                    stage.move_to(val_stage)
                else:
                    st.error(f"Stage move requested but not connected!")
                    return

            # INNER BATCH LOOP
            df_to_run = df_batch[df_batch.get("Run", True) == True]
            
            for idx, (_, row) in enumerate(df_to_run.iterrows(), start=1):
                # 1. Get Repeat Count (Default to 1 if missing)
                n_repeats = int(row.get("repeat", 1))

                # 2. Loop N times
                for r_i in range(n_repeats):   
                    # Optional: Add suffix to log if repeating
                    rep_suffix = f" (Rep {r_i+1}/{n_repeats})" if n_repeats > 1 else ""
                    cond_label = sanitize_filename(str(row.get("condition_label","")).strip())

                    # --- MEASURE POWER ---
                    current_power_str = str(val_power_nominal) 
                    
                    if row.get("MeasurePower", False):
                        pm = devices.get("pm")
                        if pm:
                            try:
                                # Measure W, convert to uW, round to 1 decimal
                                p_watts = pm.get_power()
                                p_uw = p_watts * 1e6
                                current_power_str = f"{p_uw:.1f}" 
                                ui_log(f"  > Meas Power: {p_uw:.2f} uW")
                            except Exception as e:
                                ui_log(f"  > Power Meas Failed: {e}")
                        else:
                            ui_log("  > Warning: 'Meas Pwr' checked but PM100D not connected.")

                    # [Helper] Convert ms to seconds (remove .0 if whole number)
                    seconds_val = val_exp / 1000.0
                    if seconds_val.is_integer():
                        seconds_str = f"{int(seconds_val)}"
                    else:
                        seconds_str = f"{seconds_val:.2g}" # e.g., 0.1, 0.5, 1.5

                    # Update Filename Context with ACTUAL (or Nominal) Power
                    rctx = SafeDict(
                        sample=sample_name, tag=tag,
                        laser_nm=def_laser, 
                        power_uw=current_power_str, 
                        exp_s=seconds_str,       # e.g., "1" or "0.5"
                        epf=f"{val_epf}",        # e.g., "2"
                        exp_ms=f"{val_exp:.0f}",
                        center_nm=f"{val_center:.0f}", 
                        cond_block=cond_label
                    )
                    
                    suffix = f"_Pos{val_stage}mm" if val_stage is not None else ""
                    stem_base  = sanitize_filename(pattern.format_map(rctx)) + suffix
                    stem_final = unique_stem(out_dir, stem_base)

                    recipe = []
                    recipe.append({
                        "step": "set_lightfield", "ms": float(val_exp),
                        "center_nm": float(val_center), "epf": int(val_epf),
                    })

                    rbias_str = str(row.get("Vbias", "")).strip()
                    if rbias_str:
                        try: recipe.append({"step": "set_bias", "Vbias": float(rbias_str)})
                        except: pass

                    recipe += build_recipe_from_preset(
                        "dual_gate_sweep",
                        dict(
                            Vbg_start=float(row["Vbg_start"]), Vbg_stop=float(row["Vbg_stop"]),
                            Vtg_start=float(row["Vtg_start"]), Vtg_stop=float(row["Vtg_stop"]),
                            frames=int(row["frames"]), file_base=stem_final,
                        ),
                    )

                    # --------- FRESH WAVELENGTHS (force center & wait) ---------
                    fresh_wls = None
                    lf6 = st.session_state.get("lf6")
                    if lf6 is not None:
                        try:
                            # Apply settings now so header matches data
                            try:
                                lf6.change_spectra_center(f"{float(val_center):.0f}")
                            except Exception:
                                lf6.change_spectra_center(float(val_center))
                            if hasattr(lf6, "change_expose_time"):
                                lf6.change_expose_time(float(val_exp))
                            if hasattr(lf6, "set_frames"):
                                lf6.set_frames(int(val_epf))
                            elif hasattr(lf6, "set_accumulations"):
                                lf6.set_accumulations(int(val_epf))

                            fresh_wls = wait_lambda_from_lf6(
                                lf6,
                                target_center_nm=float(val_center),
                                tol_nm=float(center_tol_nm),
                                timeout_s=25.0,
                                poll_s=0.3,
                                require_consecutive=2
                            )
                            ui_log(f"  > λ mid ≈ {_midpoint_nm(fresh_wls):.3f} nm")
                        except Exception as e:
                            ui_log(f"  > Wavelength wait warning: {e}")

                    # Fall back to the initially-provided headers if needed
                    wl_headers_for_run = (fresh_wls.tolist() if isinstance(fresh_wls, np.ndarray) and fresh_wls.size
                                          else wavelength_headers)

                    ui_log(f"  > Saving: {stem_final}.csv{rep_suffix}")
                    out_dir.mkdir(parents=True, exist_ok=True)
                    runner_singleton.run_recipe(
                        recipe, devices, wl_headers_for_run, extra_scalar_fields_order,
                        str(out_dir), stem_final, ui_log
                    )

        st.success("Sequence Complete!")