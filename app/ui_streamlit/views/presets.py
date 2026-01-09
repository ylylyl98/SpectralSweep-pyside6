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

def unique_stem(out_dir: Path, stem: str) -> str:
    """
    Always return a numbered stem like 'name_001', 'name_002', ...
    - If no prior files: returns stem_001
    - If 'stem.csv' exists, the next becomes stem_002
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

def parse_values(s: str):
    if not s or not str(s).strip(): return None
    try: return [float(x.strip()) for x in str(s).split(",") if x.strip()]
    except ValueError: return None

# ---------- wavelength helpers ----------
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

# ---------- schema helpers ----------
LOOP_SCHEMA = ["Enable", "Parameter", "Values", "Level"]
BATCH_SCHEMA = ["Run", "repeat", "MeasurePower", "condition_label",
                "Vbg_start", "Vbg_stop", "Vtg_start", "Vtg_stop", "frames", "Vbias"]

def normalize_df(df: pd.DataFrame, schema):
    df = (df if isinstance(df, pd.DataFrame) else pd.DataFrame()).copy()
    for c in schema:
        if c not in df.columns:
            df[c] = np.nan
    return df[schema].reset_index(drop=True)

def commit_preview_to_table():
    if "preview_row" in st.session_state:
        new_row_df = pd.DataFrame([st.session_state.preview_row])
        st.session_state.batch_df = normalize_df(
            pd.concat([st.session_state.batch_df, new_row_df], ignore_index=True),
            BATCH_SCHEMA
        )
        del st.session_state.preview_row

# ---------- UI ----------
def render(devices, wavelength_headers, extra_scalar_fields_order):

    PARAM_TYPES = [
        "Center Wavelength (nm)",
        "Exposure Time (ms)",
        "Stage Position",
        "Accumulations (EPF)",
        "Rotation1 Angle (deg)",
        "Rotation2 Angle (deg)",

    ]

    st.header("Dual Gate Sweep – Advanced Looper")

    c_settings = st.container()
    c_looper   = st.container()
    c_table    = st.container()
    c_log      = st.container()

    # --- 1) GLOBAL SETTINGS ---
    with c_settings:
        with st.expander("Base Settings & Defaults", expanded=False):
            c1, c2, c3 = st.columns(3)
            sample_name = c1.text_input("Sample name", "Sample")
            subfolder   = c2.text_input("Subfolder", "Initial data")
            tag         = c3.text_input("Tag", "p1")

            m1, m2, m3, m4 = st.columns(4)
            def_laser  = m1.text_input("Laser λ (nm)", "730")
            def_power  = m2.text_input("Power (µW)", "1")
            def_exp    = m3.text_input("Default Exp (ms)", "1000")
            def_center = m4.text_input("Default Center (nm)", "885")

            def_epf = st.number_input("Default Accumulations (EPF)", 1, 1000, 2)
            center_tol_nm = st.number_input("Center match tolerance (nm)", value=1.0, min_value=0.1, step=0.1)

            pattern = st.text_input(
                "Filename pattern",
                "${sample}$~${tag}$~$6KPL{laser_nm}nm{power_uw}uw{exp_s}sx{epf}$~${center_nm}nmc$_${cond_block}$"
            )

    out_dir = (BASE_OUT / sample_name / subfolder)

    # --- 2) LOOP BUILDER ---
    with c_looper:
        st.info("👇 **Loop Builder**")

        sweep_mode = st.radio(
            "Sweep Logic",
            ["Grid Scan (Nested)", "Synchronized (Zipped)", "Custom (Advanced)"],
            horizontal=True
        )

        if "loop_df" not in st.session_state or st.session_state.loop_df.empty:
            st.session_state.loop_df = pd.DataFrame([
                {"Enable": False,  "Parameter": "Center Wavelength (nm)", "Values": "800, 850", "Level": 1},
                {"Enable": False,  "Parameter": "Exposure Time (ms)",     "Values": "100, 500", "Level": 1},
                {"Enable": False, "Parameter": "Stage Position","Values": "0, 10",    "Level": 2},
                {"Enable": False, "Parameter": "Rotation1 Angle (deg)",  "Values": "0, 45",    "Level": 2},
                {"Enable": False, "Parameter": "Rotation2 Angle (deg)",  "Values": "0, 90",    "Level": 2},
            ])
        st.session_state.loop_df = normalize_df(st.session_state.loop_df, LOOP_SCHEMA)

        col_config = {
            "Enable":   st.column_config.CheckboxColumn("On", width="small"),
            "Parameter":st.column_config.SelectboxColumn("Parameter", width="medium", options=PARAM_TYPES),
            "Values":   st.column_config.TextColumn("Values (comma sep)", width="large"),
            "Level":    st.column_config.NumberColumn("Level (1=Outer)", min_value=1, max_value=5, step=1),
        }
        if sweep_mode != "Custom (Advanced)":
            col_config["Level"] = None

        edited_df = st.data_editor(
            st.session_state.loop_df,
            column_config=col_config,
            width="stretch",
            num_rows="dynamic",
            key="loop_editor",
            hide_index=True
        )

        final_df = edited_df.copy()
        if sweep_mode == "Grid Scan (Nested)":
            levels = []
            curr_level = 1
            for enabled in final_df["Enable"]:
                levels.append(curr_level)
                if enabled:
                    curr_level += 1
            final_df["Level"] = levels
            st.caption("ℹ️ Logic: Top row is Outer Loop.")
        elif sweep_mode == "Synchronized (Zipped)":
            final_df["Level"] = 1
            st.caption("ℹ️ Logic: All parameters change simultaneously.")

        st.session_state.loop_df = normalize_df(final_df, LOOP_SCHEMA)

        with st.expander("Show Sequence Preview"):
            active_rows = st.session_state.loop_df[st.session_state.loop_df["Enable"] == True].copy()
            if active_rows.empty:
                st.warning("No parameters enabled.")
            else:
                try:
                    active_rows = active_rows.sort_values("Level")
                    levels = {}
                    for _, row in active_rows.iterrows():
                        lvl = int(row["Level"])
                        vals = parse_values(row["Values"])
                        if vals:
                            levels.setdefault(lvl, []).append({"param": row["Parameter"], "vals": vals})

                    dot = ['digraph G {']
                    dot.append('  rankdir=LR;')
                    dot.append('  node [shape=box, style="filled,rounded", fillcolor="#f0f2f6", fontname="Sans", fontsize=10];')
                    dot.append('  edge [color="#888888"];')
                    dot.append('  START [shape=ellipse, fillcolor="#d4edda", label="Start"];')

                    MAX_BRANCHES = 3

                    def build_tree(parent_id, level_idx):
                        sorted_levels = sorted(levels.keys())
                        if level_idx >= len(sorted_levels):
                            return
                        curr_lvl = sorted_levels[level_idx]
                        specs = levels[curr_lvl]
                        lengths = [len(s["vals"]) for s in specs]
                        if len(set(lengths)) > 1:
                            err_id = f"ERR_{curr_lvl}"
                            dot.append(f'  "{err_id}" [label="Error: Level {curr_lvl}\\nLengths mismatch!" fillcolor="#ffcccc"];')
                            dot.append(f'  "{parent_id}" -> "{err_id}";')
                            return
                        all_vals = list(zip(*[s["vals"] for s in specs]))
                        display_vals = all_vals[:MAX_BRANCHES]
                        has_more = len(all_vals) > MAX_BRANCHES
                        for i, tup in enumerate(display_vals):
                            label_lines = []
                            for s_idx, s in enumerate(specs):
                                p_name = s["param"].split("(")[0].strip()
                                p_val = tup[s_idx]
                                label_lines.append(f"{p_name}: {p_val}")
                            node_label = "\\n".join(label_lines)
                            node_id = f"{parent_id}_L{curr_lvl}_N{i}"
                            dot.append(f'  "{node_id}" [label="{node_label}"];')
                            dot.append(f'  "{parent_id}" -> "{node_id}";')
                            build_tree(node_id, level_idx + 1)
                        if has_more:
                            dots_id = f"{parent_id}_more"
                            dot.append(f'  "{dots_id}" [label="... ({len(all_vals)-MAX_BRANCHES} more)" shape=plaintext style=filled fillcolor=none];')
                            dot.append(f'  "{parent_id}" -> "{dots_id}" [style=dotted];')

                    build_tree("START", 0)
                    dot.append('}')
                    st.graphviz_chart("\n".join(dot), width="content")

                    total_steps = 1
                    for lvl in levels:
                        total_steps *= len(levels[lvl][0]['vals'])
                    st.caption(f"Total Sequences: {total_steps}")
                except Exception as e:
                    st.error(f"Preview Error: {e}")

    # --- 3) BATCH TABLE & GENERATOR ---
    with c_table:
        if "batch_df" not in st.session_state or st.session_state.batch_df.empty:
            st.session_state.batch_df = pd.DataFrame([
                {"Run": True, "repeat": 1, "MeasurePower": False, "condition_label":"BGonly",
                 "Vbg_start":0.0,"Vbg_stop":1.0,"Vtg_start":0.0,"Vtg_stop":0.0,"frames":11,"Vbias":""},
                {"Run": True, "repeat": 1, "MeasurePower": False, "condition_label":"TGonly",
                 "Vbg_start":0.0,"Vbg_stop":0.0,"Vtg_start":0.0,"Vtg_stop":1.0,"frames":11,"Vbias":""},
                {"Run": True, "repeat": 1, "MeasurePower": False, "condition_label":"TG-BG=0",
                 "Vbg_start":0.0,"Vbg_stop":1.0,"Vtg_start":0.0,"Vtg_stop":1.0,"frames":11,"Vbias":""},
                {"Run": True, "repeat": 1, "MeasurePower": False, "condition_label":"TG+BG=0",
                 "Vbg_start":-1.0,"Vbg_stop":1.0,"Vtg_start":1.0,"Vtg_stop":-1.0,"frames":11,"Vbias":""},
            ])
        st.session_state.batch_df = normalize_df(st.session_state.batch_df, BATCH_SCHEMA)

        st.write("---")
        st.write("**Inner Electrical Sweep**")

        with st.expander("⚡ Quick Generator: Coupled Sweep (Ratio * TG ± BG = K)"):
            with st.form("eqn_preview_form"):
                st.write("**1. Define Equation:** `Ratio * TG [±] BG = Constant`")
                c_eq1, c_eq2, c_eq3, c_eq4 = st.columns([1.5, 1, 1, 1.5])
                with c_eq1: ratio = st.number_input("Ratio", value=0.80, step=0.05, format="%.2f")
                with c_eq2: op_mode = st.selectbox("Operator", ["-", "+"], help="'-' for TG-BG, '+' for TG+BG")
                with c_eq3: st.markdown("<div style='text-align: center; padding-top: 35px;'><b>BG = </b></div>", unsafe_allow_html=True)
                with c_eq4: constant = st.number_input("Constant (K)", value=5.0, step=0.5)

                st.write("**2. Define Hardware Limits (Safe Box)**")
                c_lim1, c_lim2, c_lim3 = st.columns([1.5, 1.5, 1])
                with c_lim1:
                    lim_vtg_min = st.number_input("Vtg Min", value=-5.0)
                    lim_vtg_max = st.number_input("Vtg Max", value=5.0)
                with c_lim2:
                    lim_vbg_min = st.number_input("Vbg Min", value=-5.0)
                    lim_vbg_max = st.number_input("Vbg Max", value=5.0)
                with c_lim3:
                    step_size = st.number_input("Step (V)", min_value=0.001, max_value=1.0, value=0.050, step=0.001, format="%.3f")
                    reverse = st.checkbox("Reverse?", value=True)

                op_char = "-" if op_mode == "-" else "+"
                default_label = f"{ratio}TG{op_char}BG={constant}"
                final_label = st.text_input("Row Label", default_label)

                calc_btn = st.form_submit_button("🔍 Calculate / Preview")

            if calc_btn:
                if op_mode == "-":
                    slope = ratio
                    offset = -constant
                else:
                    slope = -ratio
                    offset = constant

                points = []
                y_at_min = slope * lim_vtg_min + offset
                if lim_vbg_min <= y_at_min <= lim_vbg_max:
                    points.append((lim_vtg_min, y_at_min))
                y_at_max = slope * lim_vtg_max + offset
                if lim_vbg_min <= y_at_max <= lim_vbg_max:
                    points.append((lim_vtg_max, y_at_max))
                if abs(slope) > 1e-9:
                    x_at_min = (lim_vbg_min - offset) / slope
                    if lim_vtg_min <= x_at_min <= lim_vtg_max:
                        if not any(abs(x_at_min - p[0]) < 1e-5 for p in points):
                            points.append((x_at_min, lim_vbg_min))
                    x_at_max = (lim_vbg_max - offset) / slope
                    if lim_vtg_min <= x_at_max <= lim_vtg_max:
                        if not any(abs(x_at_max - p[0]) < 1e-5 for p in points):
                            points.append((x_at_max, lim_vbg_max))

                if len(points) < 2:
                    st.error(f"❌ Line '{final_label}' does not pass through the safety limits!")
                    st.session_state.pop("preview_row", None)
                else:
                    points.sort(key=lambda p: p[0])
                    p_start, p_stop = points[0], points[-1]
                    if reverse:
                        p_start, p_stop = p_stop, p_start
                    max_dist = max(abs(p_stop[0] - p_start[0]), abs(p_stop[1] - p_start[1]))
                    frames = int(round(max_dist / step_size)) + 1
                    st.session_state.preview_row = {
                        "Run": True, "repeat": 1, "MeasurePower": False, "condition_label": final_label,
                        "Vtg_start": float(f"{p_start[0]:.4f}"),
                        "Vtg_stop":  float(f"{p_stop[0]:.4f}"),
                        "Vbg_start": float(f"{p_start[1]:.4f}"),
                        "Vbg_stop":  float(f"{p_stop[1]:.4f}"),
                        "frames": frames, "Vbias": ""
                    }

            if "preview_row" in st.session_state:
                p = st.session_state.preview_row
                st.markdown("---")
                st.markdown("##### 📊 Result Preview")
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Vbg Sweep", f"{p['Vbg_start']} → {p['Vbg_stop']} V")
                m2.metric("Vtg Sweep", f"{p['Vtg_start']} → {p['Vtg_stop']} V")
                m3.metric("Frames", p['frames'])
                m4.metric("Label", p['condition_label'])
                col_add, col_cancel = st.columns([1, 4])
                with col_add:
                    st.button("➕ Add Row to Table", type="primary", on_click=commit_preview_to_table)
                with col_cancel:
                    if st.button("Cancel"):
                        del st.session_state.preview_row
                        st.rerun()

        with st.form("batch_form"):
            df_init = normalize_df(st.session_state.batch_df, BATCH_SCHEMA)
            df_batch = st.data_editor(
                df_init,
                num_rows="dynamic",
                hide_index=True,
                column_config={
                    "Run": st.column_config.CheckboxColumn("Run", width="small"),
                    "repeat": st.column_config.NumberColumn("Rep", min_value=1, max_value=100, step=1, width="small"),
                    "MeasurePower": st.column_config.CheckboxColumn("Meas Pwr?"),
                    "condition_label": st.column_config.TextColumn("Label"),
                    "Vbg_start": st.column_config.NumberColumn("Vbg Start"),
                    "Vbg_stop": st.column_config.NumberColumn("Vbg Stop"),
                    "Vtg_start": st.column_config.NumberColumn("Vtg Start"),
                    "Vtg_stop": st.column_config.NumberColumn("Vtg Stop"),
                    "frames": st.column_config.NumberColumn("Frames"),
                    "Vbias": st.column_config.TextColumn("Vbias"),
                },
                column_order=BATCH_SCHEMA
            )
            st.session_state.batch_df = normalize_df(df_batch, BATCH_SCHEMA)
            run_btn = st.form_submit_button("Run Sequence")

    # --- 4) LOG ---
    with c_log:
        st.markdown("---")
        log_exp = st.expander("Run log", expanded=True)
        log_box = log_exp.empty()
        if "run_log" not in st.session_state:
            st.session_state.run_log = []

        def ui_log(msg: str):
            st.session_state.run_log.append(str(msg))
            log_box.markdown(
                f"<div style='font-family:monospace;white-space:pre-wrap;'>{chr(10).join(st.session_state.run_log[-200:])}</div>",
                unsafe_allow_html=True
            )

        if log_exp.button("Clear Log"):
            st.session_state.run_log = []
            log_box.empty()

    # --- 5) EXECUTION ---
    if run_btn:
        loop_df = st.session_state.loop_df
        active_rows = loop_df[loop_df["Enable"] == True].copy()

        if active_rows.empty:
            levels = {}
        else:
            active_rows = active_rows.sort_values("Level")
            levels = {}
            for _, row in active_rows.iterrows():
                lvl = int(row["Level"])
                vals = parse_values(row["Values"])
                if vals:
                    levels.setdefault(lvl, []).append({"param": row["Parameter"], "vals": vals})

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
            for tup in zipped_vals:
                level_dicts.append({k: v for k, v in zip(param_names, tup)})
            level_combinations.append(level_dicts)

        from itertools import chain

        if not level_combinations:
            final_sequence = [{}]
        else:
            final_sequence = [
                dict(chain.from_iterable(d.items() for d in combo))
                for combo in itertools.product(*level_combinations)
            ]


        ui_log(f"Generated {len(final_sequence)} conditions.")

        total = len(final_sequence)
        for seq_i, ctx_vars in enumerate(final_sequence):
            ui_log(f"--- Sequence {seq_i+1}/{total} ---")

            # defaults from current UI
            val_center = ctx_vars.get("Center Wavelength (nm)", float(def_center))
            val_exp    = ctx_vars.get("Exposure Time (ms)",     float(def_exp))
            val_stage  = ctx_vars.get("Stage Position",    None)
            val_epf    = int(ctx_vars.get("Accumulations (EPF)", def_epf))
            val_rot1   = ctx_vars.get("Rotation1 Angle (deg)", None)
            val_rot2   = ctx_vars.get("Rotation2 Angle (deg)", None)
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

            # Move Rotation Mount
            def _move_rotation_if_present(devices, key_name, angle, ui_log):
                """Move a connected rotation mount (if available)."""
                if angle is None:
                    return
                rot = devices.get(key_name)
                if rot is None:
                    # Some setups register a single 'rotation'; try that for rotation1 as a fallback.
                    if key_name == "rotation1":
                        rot = devices.get("rotation")
                if rot is None:
                    ui_log(f"{key_name} requested at {angle}°, but not connected.")
                    return
                try:
                    # Most adapters expose move_to(angle). If yours needs axis, the adapter should
                    # already be configured; these two mounts are separate devices.
                    rot.move_to(float(angle))
                    ui_log(f"Moved {key_name} to {float(angle):.2f}°")
                except Exception as e:
                    ui_log(f"{key_name} move failed: {e}")

            # --- call it for each mount if user included those params in the loop ---
            _move_rotation_if_present(devices, "rotation1", val_rot1, ui_log)
            _move_rotation_if_present(devices, "rotation2", val_rot2, ui_log)


            df_to_run = st.session_state.batch_df[st.session_state.batch_df.get("Run", True) == True]

            for idx, (_, row) in enumerate(df_to_run.iterrows(), start=1):
                n_repeats = int(row.get("repeat", 1))
                for r_i in range(n_repeats):
                    rep_suffix = f" (Rep {r_i+1}/{n_repeats})" if n_repeats > 1 else ""
                    cond_label = sanitize_filename(str(row.get("condition_label","")).strip())

                    current_power_str = str(val_power_nominal)
                    if row.get("MeasurePower", False):
                        pm = devices.get("pm")
                        if pm:
                            try:
                                p_watts = pm.get_power()
                                p_uw = p_watts * 1e6
                                current_power_str = f"{p_uw:.1f}"
                                ui_log(f"  > Meas Power: {p_uw:.2f} uW")
                            except Exception as e:
                                ui_log(f"  > Power Meas Failed: {e}")
                        else:
                            ui_log("  > Warning: 'Meas Pwr' checked but PM100D not connected.")

                    seconds_val = val_exp / 1000.0
                    seconds_str = f"{int(seconds_val)}" if float(seconds_val).is_integer() else f"{seconds_val:.2g}"

                    rctx = SafeDict(
                        sample=sample_name, tag=tag,
                        laser_nm=def_laser,
                        power_uw=current_power_str,
                        exp_s=seconds_str,
                        epf=f"{val_epf}",
                        exp_ms=f"{val_exp:.0f}",
                        center_nm=f"{val_center:.0f}",
                        cond_block=cond_label
                    )

                    suffix_parts = []
                    if val_stage is not None:
                        suffix_parts.append(f"Pos{val_stage}mm")
                    if val_rot1 is not None:
                        suffix_parts.append(f"R1{float(val_rot1):.1f}deg")
                    if val_rot2 is not None:
                        suffix_parts.append(f"R2{float(val_rot2):.1f}deg")
                    suffix = f"_{'_'.join(suffix_parts)}" if suffix_parts else ""
                    stem_base  = sanitize_filename(pattern.format_map(rctx)) + suffix
                    stem_final = unique_stem(out_dir, stem_base)

                    recipe = [{
                        "step": "set_lightfield", "ms": float(val_exp),
                        "center_nm": float(val_center), "epf": int(val_epf),
                    }]

                    rbias_str = str(row.get("Vbias", "")).strip()
                    if rbias_str:
                        try:
                            recipe.append({"step": "set_bias", "Vbias": float(rbias_str)})
                        except:
                            pass

                    recipe += build_recipe_from_preset(
                        "dual_gate_sweep",
                        dict(
                            Vbg_start=float(row["Vbg_start"]), Vbg_stop=float(row["Vbg_stop"]),
                            Vtg_start=float(row["Vtg_start"]), Vtg_stop=float(row["Vtg_stop"]),
                            frames=int(row["frames"]), file_base=stem_final,
                        ),
                    )

                    # FRESH WAVELENGTHS (force center & wait)
                    fresh_wls = None
                    lf6 = st.session_state.get("lf6")
                    if lf6 is not None:
                        try:
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

                    wl_headers_for_run = (fresh_wls.tolist()
                                          if isinstance(fresh_wls, np.ndarray) and fresh_wls.size
                                          else wavelength_headers)

                    ui_log(f"  > Saving: {stem_final}.csv{rep_suffix}")
                    out_dir.mkdir(parents=True, exist_ok=True)
                    runner_singleton.run_recipe(
                        recipe, devices, wl_headers_for_run, extra_scalar_fields_order,
                        str(out_dir), stem_final, ui_log
                    )
