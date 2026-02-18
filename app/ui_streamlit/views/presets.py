# app/ui_streamlit/views/presets.py
from __future__ import annotations

import time
import re
from pathlib import Path
from typing import List, Tuple, Optional, Dict
import html
import math

import itertools
import threading
import queue

import numpy as np
import pandas as pd
import streamlit as st


from app.steps.registry import build_recipe_from_preset
from app.engine.runner import runner_singleton
import app.steps._import_all  # noqa: F401

BASE_OUT = Path(r"D:\instrument_control_v3_1")

# -------------------- log helpers --------------------
INVALID_CHARS = '<>:"/\\|?*'

def build_run_status_tree_html(
    final_sequence: List[dict],
    df_batch: pd.DataFrame,
    *,
    done: int,
    total_acq: int,
    current_seq_i: int,
    current_label: str,
    current_rep_i: int,
    max_seq_show: int = 10,
    param_order: Optional[List[str]] = None
) -> str:
    """
    Live status tree (HTML <pre>) with 3 colors:
      - done (finished)
      - now (current running)
      - todo (not started)
    Shows a compact window of sequences around the current one.
    """

    # ---- normalize batch rows (Run=True) ----
    dfb = _normalize_batch_types(df_batch).copy()
    if "Run" in dfb.columns:
        dfb["Run"] = dfb["Run"].map(_to_bool)
        dfb = dfb[dfb["Run"] == True].reset_index(drop=True)

    # ---- per-sequence acquisition counts (depends on When + outer ctx) ----
    acq_counts: List[int] = []
    seq_plans: List[List[Tuple[str, int]]] = []  # per seq: [(label, reps), ...]

    for ctx in final_sequence:
        outer = _outer_ctx_alias(ctx)
        plan = []
        n = 0
        for _, r in dfb.iterrows():
            if _when_ok(r.get("When", ""), outer):
                lab = sanitize_filename(r.get("condition_label", ""))
                reps = max(int(r.get("repeat", 1)), 1)
                plan.append((lab, reps))
                n += reps
        seq_plans.append(plan)
        acq_counts.append(n)

    # prefix sums for mapping done -> seq bounds
    prefix = [0]
    for n in acq_counts:
        prefix.append(prefix[-1] + n)

    def seq_bounds(seq_i: int):
        s0 = prefix[seq_i]
        s1 = prefix[seq_i + 1]
        return s0, s1

    # map current (seq_i,label,rep_i) -> absolute acquisition index
    current_acq_index = prefix[current_seq_i] if (0 <= current_seq_i < len(prefix) - 1) else 0
    if 0 <= current_seq_i < len(seq_plans):
        off = 0
        for lab, reps in seq_plans[current_seq_i]:
            if lab == current_label:
                current_acq_index += min(max(current_rep_i, 0), max(reps - 1, 0)) + off
                break
            off += reps

    # ---- small ctx formatter ----
    def fmt_ctx(ctx: dict) -> str:
        default_keys = ["Center Wavelength (nm)", "Exposure Time (ms)", "Stage Position"]
        keys = param_order or default_keys

        parts = []
        for k in keys:
            v = ctx.get(k, None)
            if v is None or str(v).strip() == "":
                continue
            k0 = k.split("(")[0].strip()
            try:
                parts.append(f"{k0}={float(v):g}")
            except Exception:
                parts.append(f"{k0}={v}")
        return ", ".join(parts) if parts else "(no loop vars)"


    def esc(s: str) -> str:
        return html.escape(str(s), quote=False)

    # ---- styling (3 colors) ----
    # You can tweak these colors anytime.
    STYLE_DONE = "color:#6E7681;"   # readable in dark + light
    STYLE_TODO = "color:#8B949E;"  # still visible but lighter
    STYLE_NOW  = (
        "color: var(--text-color, #111827);"
        "background: rgba(245,158,11,0.22);"        # amber highlight works in dark mode
        "border-left: 4px solid #f59e0b;"
        "padding: 1px 6px;"
        "border-radius: 4px;"
        "font-weight: 800;"
    )

    def paint(text: str, state: str) -> str:
        if state == "done":
            return f"<span style='{STYLE_DONE}'>✓ {esc(text)}</span>"
        if state == "now":
            return f"<span style='{STYLE_NOW}'>▶ {esc(text)}</span>"
        return f"<span style='{STYLE_TODO}'>• {esc(text)}</span>"

    # ---- choose which sequences to show (window around current) ----
    n_seq = len(final_sequence)
    if n_seq <= max_seq_show:
        start = 0
    else:
        start = max(0, min(current_seq_i - max_seq_show // 2, n_seq - max_seq_show))
    end = min(n_seq, start + max_seq_show)


    # ---- build lines (real tree) ----
    lines = []
    header = f"Run (progress {done}/{total_acq})"
    lines.append(f"<span style='font-weight:700;color:#8B949E'>{esc(header)}</span>")

    for seq_i in range(start, end):
        ctx = final_sequence[seq_i]
        seq_text = f"Seq {seq_i+1}: {fmt_ctx(ctx)}"

        s0, s1 = seq_bounds(seq_i)
        if done >= s1:
            seq_state = "done"
        elif done <= s0:
            seq_state = "todo"
        else:
            seq_state = "now"

        # tree glyphs
        seq_is_last = (seq_i == end - 1)
        seq_prefix = "└─ " if seq_is_last else "├─ "
        child_prefix = "   " if seq_is_last else "│  "

        lines.append(seq_prefix + paint(seq_text, seq_state))

        # children: batch+rep flattened into acquisition lines
        # determine per-acquisition state based on done/current
        # acquisition absolute index for this seq starts at s0
        acq_abs = s0
        plan = seq_plans[seq_i] if (0 <= seq_i < len(seq_plans)) else []
        for b_i, (lab, reps) in enumerate(plan):
            for r_i in range(reps):
                acq_state = "todo"
                if acq_abs < done:
                    acq_state = "done"
                elif acq_abs == current_acq_index and done < total_acq:
                    acq_state = "now"

                leaf = f"{lab}  (rep {r_i+1}/{reps})"
                # last leaf glyph control
                is_last_leaf = (b_i == len(plan) - 1) and (r_i == reps - 1)
                leaf_prefix = "└─ " if is_last_leaf else "├─ "
                lines.append(child_prefix + leaf_prefix + paint(leaf, acq_state))


                acq_abs += 1

    if end < n_seq:
        lines.append(f"<span style='{STYLE_TODO}'>… (+{n_seq-end} more sequences)</span>")

    # render each line as a block so Streamlit can't collapse it into one row
    body = "".join(f"<div>{ln}</div>" for ln in lines)

    return (
        "<div style='"
        "margin:0; padding:8px 10px;"
        "background: var(--secondary-background-color, rgba(148,163,184,0.12));"
        "border: 1px solid rgba(148,163,184,0.35);"
        "border-radius:8px;"
        "color: var(--text-color, #8B949E);"
        "font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;"
        "font-size:18px; line-height:1.35;"
        "white-space:pre;"
        "overflow-x:auto;"
        "max-width:100%;"
        "'>"
        + body
        + "</div>"
    )


def get_enabled_param_order(loop_src: pd.DataFrame) -> List[str]:
    df = normalize_df(loop_src, LOOP_SCHEMA).copy()
    df["Enable"] = df["Enable"].map(_to_bool)
    df["Level"] = pd.to_numeric(df.get("Level", 1), errors="coerce").fillna(1).astype(int)

    df = df[df["Enable"] == True].reset_index().rename(columns={"index": "_row"})
    # stable sort: Level first, then original row order
    df = df.sort_values(["Level", "_row"], kind="mergesort")

    order: List[str] = []
    for p in df["Parameter"].astype(str).tolist():
        if p and p not in order:
            order.append(p)
    return order



def ui_log(msg: str):
    """Log to session state and update the UI box if it exists."""
    if "run_log" not in st.session_state:
        st.session_state.run_log = []
    # dedupe
    if msg and st.session_state.get("_last_log_msg") == msg:
        return
    st.session_state["_last_log_msg"] = msg
    if msg:
        ts = time.strftime("%H:%M:%S")
        st.session_state.run_log.append(f"[{ts}] {str(msg)}")

    box = st.session_state.get("active_log_box")
    if box:
        # IMPORTANT: escape because we render inside unsafe HTML
        safe_lines = [html.escape(x, quote=False) for x in st.session_state.run_log[-200:]]

        box.markdown(
            "<div style='"
            "font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;"
            "white-space: pre-wrap;"
            "max-height: 300px;"
            "overflow-y: auto;"
            "padding: 10px;"
            "border-radius: 8px;"
            "color: var(--text-color, #7C8A9A);"
            "background: var(--secondary-background-color, rgba(148,163,184,0.12));"
            "border: 1px solid rgba(148,163,184,0.35);"
            "'>"
            + "\n".join(safe_lines)
            + "</div>",
            unsafe_allow_html=True,
        )


def build_run_preview(loop_src: pd.DataFrame, batch_src: pd.DataFrame, max_examples: int = 6) -> dict:
    """
    Compact preview based ONLY on SAVED tables (loop_src + batch_src).
    Call only on 'Apply All' (or Discard All) to avoid hiccups.
    """
    # --- Loop conditions count + a few example condition strings ---
    loop_df = normalize_df(loop_src, LOOP_SCHEMA).copy()
    if "Enable" in loop_df.columns:
        loop_df["Enable"] = loop_df["Enable"].map(_to_bool)

    active = loop_df[loop_df["Enable"] == True].copy()
    if not active.empty:
        active = active.sort_values("Level")

    levels: Dict[int, List[dict]] = {}
    for _, row in active.iterrows():
        vals = parse_values(row.get("Values", ""), row.get("Parameter", ""))

        if vals:
            levels.setdefault(int(row.get("Level", 1)), []).append({"p": row["Parameter"], "v": vals})

    # Count conditions (multiply per Level; no full expansion)
    cond_count = 1
    for lvl in sorted(levels.keys()):
        specs = levels[lvl]
        lens = [len(s["v"]) for s in specs]
        if len(set(lens)) > 1:
            return {"ok": False, "error": f"Loop level {lvl} length mismatch: {lens}"}
        cond_count *= lens[0]


    # --- Loop param summary (readable) ---
    param_summary = []
    for lvl in sorted(levels.keys()):
        for spec in levels[lvl]:
            p = str(spec["p"])
            # show the first few values only, plus count
            vals = spec["v"]
            shown = ", ".join(f"{v:g}" for v in vals[:4])
            suffix = "" if len(vals) <= 4 else f", … (+{len(vals)-4})"
            param_summary.append({
                "parameter": p.split("(")[0].strip(),
                "count": len(vals),
                "values": shown + suffix,
                "level": int(lvl),
            })

    param_summary_df = pd.DataFrame(param_summary).sort_values(["level", "parameter"]) if param_summary else pd.DataFrame()


    # --- Batch enabled rows + totals (RESPECTS When) ---
    # Use the same plan builder the runner uses
    try:
        final_sequence, dfb, total_acq = build_run_plan_from_saved(loop_src, batch_src)
    except Exception as e:
        return {"ok": False, "error": f"Preview build failed: {e}"}

    batch_rows = int(len(dfb))

    # Totals depend on When + outer-loop ctx
    total_frames = 0
    for ctx in final_sequence:
        outer = _outer_ctx_alias(ctx)
        for _, r in dfb.iterrows():
            if _when_ok(r.get("When", ""), outer):
                reps = max(int(r.get("repeat", 1)), 1)
                frs  = max(int(r.get("frames", 1)), 1)
                total_frames += reps * frs

    # Keep these keys for the UI
    cond_count = int(len(final_sequence))     # exact number of sequences
    total_acquisitions = int(total_acq)       # exact acquisitions
    rep_sum = int(dfb["repeat"].sum()) if (batch_rows and "repeat" in dfb.columns) else 0
    frames_sum = int((dfb["repeat"] * dfb["frames"]).sum()) if (batch_rows and "frames" in dfb.columns) else 0


    labels = (
        dfb["condition_label"].astype(str).tolist()[:10]
        if (batch_rows and "condition_label" in dfb.columns)
        else []
    )

    # Build a compact preview table for enabled batch rows
    preview_table = pd.DataFrame()
    if batch_rows:
        cols = ["condition_label", "When", "repeat", "frames", "Vbg_start", "Vbg_stop", "Vtg_start", "Vtg_stop", "Vbias_start", "Vbias_stop"]
        cols = [c for c in cols if c in dfb.columns]
        preview_table = dfb[cols].copy()
        preview_table = preview_table.rename(columns={"condition_label": "label"}).reset_index(drop=True)

    # --- Tiny graph data (only enabled batch rows; lightweight) ---
    graph_rows = []
    if batch_rows:
        # keep only first ~12 batch labels for plotting to stay readable
        plot_dfb = dfb.copy().reset_index(drop=True)
        if "condition_label" in plot_dfb.columns:
            plot_dfb["condition_label"] = plot_dfb["condition_label"].astype(str)

        plot_labels = plot_dfb["condition_label"].tolist()[:12] if "condition_label" in plot_dfb.columns else []
        graph_rows = [{"y": lab, "y_order": i} for i, lab in enumerate(plot_labels)]


    return {
        "ok": True,
        "cond_count": int(cond_count),
        "batch_rows": int(batch_rows),
        "rep_sum": int(rep_sum),
        "total_acquisitions": int(total_acquisitions),
        "frames_sum_per_condition": int(frames_sum),
        "total_frames": int(total_frames),
        "labels": labels[:10],
        "param_summary": param_summary_df,
        "batch_table": preview_table,
        "graph_labels": [r["y"] for r in graph_rows],

    }

def build_run_preview_text_tree(
    loop_src: pd.DataFrame,
    batch_src: pd.DataFrame,
    *,
    max_branch: int = 2,
    max_batch: int = 6,
    max_lines: int = 120,
) -> str:
    """
    Text-only tree preview (ASCII) for the run:
      Run
       └─ Level 1: Param=val
           └─ Level 2: ...
               └─ Batch: BGonly, TGonly, ...
    Uses saved tables only.
    """

    # ---- loop levels ----
    loop_df = normalize_df(loop_src, LOOP_SCHEMA).copy()
    if "Enable" in loop_df.columns:
        loop_df["Enable"] = loop_df["Enable"].map(_to_bool)
    active = loop_df[loop_df["Enable"] == True].copy()
    if not active.empty:
        active = active.sort_values("Level")

    levels: Dict[int, List[dict]] = {}
    for _, row in active.iterrows():
        vals = parse_values(row.get("Values", ""), row.get("Parameter", ""))

        if vals:
            levels.setdefault(int(row.get("Level", 1)), []).append({
                "param": str(row.get("Parameter", "")),
                "vals": vals,
            })

    sorted_lvls = sorted(levels.keys())

    # ---- batch labels ----
    dfb = _normalize_batch_types(batch_src).copy()
    if "Run" in dfb.columns:
        dfb["Run"] = dfb["Run"].map(_to_bool)
        dfb = dfb[dfb["Run"] == True]

    batch_labels = []
    if not dfb.empty and "condition_label" in dfb.columns:
        batch_labels = [sanitize_filename(x) for x in dfb["condition_label"].astype(str).tolist()]
    shown_batch = batch_labels[:max_batch]
    batch_suffix = f" … (+{len(batch_labels)-len(shown_batch)} more)" if len(batch_labels) > len(shown_batch) else ""
    batch_line = "Batch: " + (", ".join(shown_batch) if shown_batch else "(none)") + batch_suffix

    # ---- helper formatting ----
    def fmt_param(p: str) -> str:
        return p.split("(")[0].strip()

    def fmt_val(v) -> str:
        try:
            return f"{float(v):g}"
        except Exception:
            return str(v)

    # Each level step is "zipped": if multiple params in same level, show them together
    # and assume equal lengths (otherwise show error).
    level_steps: List[List[str]] = []
    for lvl in sorted_lvls:
        specs = levels[lvl]
        lens = [len(s["vals"]) for s in specs]
        if len(set(lens)) > 1:
            return f"Run\n└─ ❌ Level {lvl} length mismatch: {lens}"

        # zipped tuples like (v1, v2, ...)
        zipped = list(zip(*[s["vals"] for s in specs]))
        # limit branch
        shown = zipped[:max_branch]
        more = len(zipped) - len(shown)

        step_labels = []
        for tup in shown:
            parts = []
            for i, s in enumerate(specs):
                parts.append(f"{fmt_param(s['param'])}={fmt_val(tup[i])}")
            step_labels.append(", ".join(parts))

        if more > 0:
            step_labels.append(f"(+{more} more)")

        level_steps.append(step_labels)

    # ---- build ASCII tree ----
    lines: List[str] = ["Run"]

    def add_subtree(prefix: str, lvl_idx: int):
        if len(lines) >= max_lines:
            lines.append(prefix + "└─ … (truncated)")
            return

        if lvl_idx >= len(level_steps):
            lines.append(prefix + "└─ " + batch_line)
            return

        lvl = sorted_lvls[lvl_idx]
        steps = level_steps[lvl_idx]

        # For each step at this level, render branch
        for i, label in enumerate(steps):
            is_last = (i == len(steps) - 1)
            branch = "└─ " if is_last else "├─ "
            lines.append(prefix + branch + f"L{lvl}: {label}")

            # If this is the "(+N more)" pseudo-node, don't go deeper
            if label.startswith("(+"):
                continue

            child_prefix = prefix + ("   " if is_last else "│  ")
            add_subtree(child_prefix, lvl_idx + 1)

            if len(lines) >= max_lines:
                lines.append(child_prefix + "└─ … (truncated)")
                return

    if not level_steps:
        lines.append("└─ " + batch_line)
    else:
        add_subtree("", 0)

    return "\n".join(lines)



def render_run_preview(ph):
    """Compact run preview based ONLY on st.session_state['run_preview']."""
    with ph.container():
        with st.expander("Run Preview (updates on Apply All)", expanded=False):
            
            rp = st.session_state.get("run_preview")

            if not rp:
                st.info("Click **Apply All** to generate the preview from saved tables.")
            elif not rp.get("ok", True):
                st.error(rp.get("error", "Preview error"))
            else:
                # Top: math
                cond = int(rp["cond_count"])
                br = int(rp["batch_rows"])
                rep = int(rp["rep_sum"])
                total_runs = int(rp["total_acquisitions"])
                total_frames = int(rp["total_frames"])

                st.markdown(
                    f"**Total sequences:** `{cond}`  \n"
                    f"**Enabled batch rows (Run=True):** `{br}`  \n"
                    f"**Total acquisitions (after When):** **{total_runs}**  \n"
                    f"**Total frames:** **{total_frames}**"
                )


                # Middle: loop summary
                ps = rp.get("param_summary")
                if isinstance(ps, pd.DataFrame) and not ps.empty:
                    st.caption("Loop parameters (why loop count is what it is):")
                    st.dataframe(ps[["parameter", "count", "values"]], width="stretch", hide_index=True)
                else:
                    st.caption("Loop parameters: (none enabled)")

                # Bottom: enabled batch table
                bt = rp.get("batch_table")
                if isinstance(bt, pd.DataFrame) and not bt.empty:
                    st.caption("Enabled electrical sweep rows:")
                    # Make spans readable
                    b2 = bt.copy()
                    if "Vbg_start" in b2.columns and "Vbg_stop" in b2.columns:
                        b2["Vbg"] = b2["Vbg_start"].map(lambda x: f"{x:g}") + " → " + b2["Vbg_stop"].map(lambda x: f"{x:g}")
                    if "Vtg_start" in b2.columns and "Vtg_stop" in b2.columns:
                        b2["Vtg"] = b2["Vtg_start"].map(lambda x: f"{x:g}") + " → " + b2["Vtg_stop"].map(lambda x: f"{x:g}")

                    show_cols = [c for c in ["label", "Vbg", "Vtg", "frames", "repeat"] if c in b2.columns]
                    st.dataframe(b2[show_cols], width="stretch", hide_index=True)
                else:
                    st.warning("No batch rows enabled (Run=True).")


            if rp and rp.get("ok", True):
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Loop conditions", rp.get("cond_count", 0))
                c2.metric("Enabled batch rows", rp.get("batch_rows", 0))
                c3.metric("Total acquisitions", rp.get("total_acquisitions", 0))
                c4.metric("Total frames", rp.get("total_frames", 0))


            if rp and rp.get("labels"):
                st.caption("Batch labels (first 10): " + ", ".join(rp["labels"]))

            if rp and rp.get("examples"):
                st.caption("Example loop conditions:")
                for s in rp["examples"]:
                    st.write(f"- {s}")


def sanitize_filename(s: str) -> str:
    s = str(s)
    for ch in INVALID_CHARS:
        s = s.replace(ch, "")
    return s.strip()


class SafeDict(dict):
    def __missing__(self, k):
        return f"{{{k}}}"


def unique_stem(out_dir: Path, stem: str) -> str:
    stem = sanitize_filename(stem)
    out_dir.mkdir(parents=True, exist_ok=True)

    max_n = 0
    if (out_dir / f"{stem}.csv").exists():
        max_n = 1

    pat = re.compile(re.escape(stem) + r"_(\d{3})\.csv$", re.IGNORECASE)
    for p in out_dir.glob(f"{stem}_*.csv"):
        m = pat.search(p.name)
        if m:
            max_n = max(max_n, int(m.group(1)))

    next_n = 1 if max_n == 0 else max_n + 1
    return f"{stem}_{next_n:03d}"


def parse_values(s: str, param: str | None = None):
    """
    Parse the Values cell.

    Default:
      "1,2,3" -> [1.0, 2.0, 3.0]

    Stage Position special:
      "(start,stop,n)" -> np.linspace(start, stop, n).tolist()

    IMPORTANT:
      "0,3600,15" stays as 3 literal values (NO linspace)
      Only "(0,3600,15)" triggers linspace.
    """
    if not s or not str(s).strip():
        return None

    raw = str(s).strip()
    p = str(param or "")
    is_stage = p.startswith("Stage Position")

    # Detect linspace shorthand ONLY if wrapped by parentheses
    linspace_mode = raw.startswith("(") and raw.endswith(")")
    if linspace_mode:
        raw = raw[1:-1].strip()

    try:
        clean = raw.replace(";", ",")
        nums = [float(x.strip()) for x in clean.split(",") if x.strip()]
    except ValueError:
        return None

    # Apply linspace only for Stage Position and only when using parentheses
    if is_stage and linspace_mode and len(nums) == 3:
        a, b, n = nums
        if float(n).is_integer() and int(n) >= 2:
            return np.linspace(float(a), float(b), int(n)).astype(float).tolist()

    return nums

def _outer_ctx_alias(ctx_vars: dict) -> dict:
    """Map outer-loop ctx_vars to short names for guards."""
    def _f(x):
        try:
            return float(x)
        except Exception:
            return None

    return {
        "center_nm":   _f(ctx_vars.get("Center Wavelength (nm)")),
        "exposure_ms": _f(ctx_vars.get("Exposure Time (ms)")),
        "stage_pos":   _f(ctx_vars.get("Stage Position")),
        "epf":         _f(ctx_vars.get("Accumulations (EPF)")),
        "rot1_deg":    _f(ctx_vars.get("Rotation1 Angle (deg)")),
        "rot2_deg":    _f(ctx_vars.get("Rotation2 Angle (deg)")),
    }


def _when_ok(when: str, outer: dict) -> bool:
    """
    Guard syntax (AND across clauses split by ';' or newline):
      center_nm=755,703
      exposure_ms>=500
      stage_pos==0
      rot1_deg!=85
      center_nm=755; exposure_ms=100,500
    """
    s = str(when or "").strip()
    if not s:
        return True

    # allow some short aliases too
    keymap = {
        "center": "center_nm",
        "centernm": "center_nm",
        "exp": "exposure_ms",
        "exposure": "exposure_ms",
        "stage": "stage_pos",
        "stagepos": "stage_pos",
        "rot1": "rot1_deg",
        "rot2": "rot2_deg",
    }

    parts = re.split(r"[;\n]+", s)
    tol = 1e-9

    def _to_num_list(rhs: str):
        out = []
        for t in rhs.split(","):
            t = t.strip()
            if not t:
                continue
            out.append(float(t))
        return out

    for part in parts:
        part = part.strip()
        if not part:
            continue

        m = re.search(r"(<=|>=|!=|==|=|<|>)", part)
        if not m:
            return False

        op = m.group(1)
        k = part[: m.start()].strip()
        rhs = part[m.end() :].strip()

        kn = re.sub(r"\s+", "", k.lower())
        var = keymap.get(kn, kn)  # allow "center" etc
        if var not in outer:
            return False

        v = outer.get(var, None)
        if v is None:
            return False

        # list: "755,703"
        if "," in rhs and op in ("=", "==", "!="):
            vals = _to_num_list(rhs)
            hit = any(abs(v - x) < tol for x in vals)
            if op in ("!=",):
                hit = not hit
            if not hit:
                return False
            continue

        # scalar numeric
        try:
            x = float(rhs)
        except Exception:
            return False

        if op in ("=", "=="):
            if not (abs(v - x) < tol):
                return False
        elif op == "!=":
            if abs(v - x) < tol:
                return False
        elif op == "<":
            if not (v < x):
                return False
        elif op == "<=":
            if not (v <= x):
                return False
        elif op == ">":
            if not (v > x):
                return False
        elif op == ">=":
            if not (v >= x):
                return False

    return True

def _parse_optional_float(x):
    """Return float if x looks like a number; otherwise None. Treat '', None, 'none', 'nan' as None."""
    if x is None:
        return None
    s = str(x).strip()
    if s == "" or s.lower() in ("none", "nan"):
        return None
    try:
        v = float(s)
        if math.isnan(v):
            return None
        return v
    except Exception:
        # non-empty but not parseable -> treat as invalid (caller can decide)
        return "INVALID"

def _check_vbias_mapping(batch_df: pd.DataFrame, devices) -> Optional[str]:
    """
    If any enabled row (Run=True) requests Vbias but IV has no Vbias role, return an error string.
    Also flags invalid (non-numeric) Vbias cells.
    """
    iv = (devices or {}).get("iv", None)
    has_vbias = bool(getattr(iv, "has_role", lambda *_: False)("Vbias")) if iv else False

    dfb = normalize_df(batch_df, BATCH_SCHEMA).copy()
    if "Run" in dfb.columns:
        dfb["Run"] = dfb["Run"].map(_to_bool)
        dfb = dfb[dfb["Run"] == True]

    if dfb.empty:
        return None

    needs_vbias = []
    invalid_vbias = []

    for idx, row in dfb.reset_index(drop=True).iterrows():
        vb_s = _parse_optional_float(row.get("Vbias_start", ""))
        vb_e = _parse_optional_float(row.get("Vbias_stop", ""))
        label = str(row.get("condition_label", f"row_{idx+1}")).strip() or f"row_{idx+1}"
        # invalid detection
        if vb_s == "INVALID" or vb_e == "INVALID":
            invalid_vbias.append((idx + 1, label, row.get("Vbias_start", ""), row.get("Vbias_stop", "")))
        else:
            # treat single-entry as constant bias
            if vb_s is None and vb_e is not None:
                vb_s = vb_e
            if vb_e is None and vb_s is not None:
                vb_e = vb_s

            if vb_s is not None and vb_e is not None:
                needs_vbias.append((idx + 1, label, float(vb_s), float(vb_e)))

    if invalid_vbias:
        show = ", ".join([f"#{r}({lab}) Vbias_start='{a}' Vbias_stop='{b}'" for r, lab, a, b in invalid_vbias[:6]])
        more = "" if len(invalid_vbias) <= 6 else f" … (+{len(invalid_vbias)-6} more)"
        return f"⛔ Invalid Vbias value(s): {show}{more}. Use numbers or leave blank."


    if needs_vbias and not has_vbias:
        show = ", ".join([f"#{r}({lab}) Vbias={a:g}→{b:g}V" for r, lab, a, b in needs_vbias[:6]])
        more = "" if len(needs_vbias) <= 6 else f" … (+{len(needs_vbias)-6} more)"
        return (
            f"⛔ Vbias is requested but **Vbias source is not mapped**. "
            f"Rows: {show}{more}. Map Vbias or clear those cells."
        )

    return None

def _check_gate_mapping(devices) -> Optional[str]:
    """
    Dual gate sweep ALWAYS includes Vbg/Vtg keys in params.
    If IV is missing or roles aren't mapped, block the run with a clear UI error.
    """
    iv = (devices or {}).get("iv", None)
    if iv is None:
        return "⛔ IV device not connected. Go to sidebar → IV Instruments (VISA) and map Vbg/Vtg sources."

    # Prefer has_role() if available, else fall back to role_map dict.
    role_map = getattr(iv, "role_map", {}) or {}

    missing = []
    for role in ("Vbg", "Vtg"):
        try:
            has = bool(getattr(iv, "has_role", lambda r: False)(role))
        except Exception:
            has = False

        if (not has) and (not role_map.get(role)):
            missing.append(role)

    if missing:
        miss = ", ".join(missing)
        return (
            f"⛔ Sweep requires **{miss}** role(s) but they are not mapped.\n\n"
            f"Fix: sidebar → IV Instruments (VISA) → set **{miss} source** to the correct Keithley address."
        )

    return None

def _vbias_block_from_row(row) -> str:
    vb_s = _parse_optional_float(row.get("Vbias_start", ""))
    vb_e = _parse_optional_float(row.get("Vbias_stop", ""))

    if vb_s in (None, "INVALID") and vb_e in (None, "INVALID"):
        return ""

    if vb_s == "INVALID" or vb_e == "INVALID":
        return ""

    # single-entry => constant
    if vb_s is None and vb_e is not None:
        vb_s = vb_e
    if vb_e is None and vb_s is not None:
        vb_e = vb_s

    if vb_s is None or vb_e is None:
        return ""

    vb_s = float(vb_s)
    vb_e = float(vb_e)

    if abs(vb_s - vb_e) < 1e-12:
        return f"Vbias{vb_s:g}V"
    return f"Vbias{vb_s:g}to{vb_e:g}V"



def _to_bool(x):
    if isinstance(x, bool):
        return x
    if pd.isna(x):
        return False
    if isinstance(x, (int, float)):
        return bool(int(x))
    s = str(x).strip().lower()
    return s in ("true", "1", "yes", "y", "on")

import inspect

def _filter_kwargs_for_callable(fn, kwargs: dict) -> dict:
    """Keep only kwargs that exist in fn signature (avoids TypeError)."""
    try:
        sig = inspect.signature(fn)
        return {k: v for k, v in kwargs.items() if k in sig.parameters}
    except Exception:
        return kwargs

def _call_compat(fn, *args, **kwargs):
    """Call fn with only supported kwargs; fallback to positional-only if needed."""
    if fn is None:
        return None
    kw = _filter_kwargs_for_callable(fn, dict(kwargs))
    return fn(*args, **kw)

def _has_role(iv, role: str) -> bool:
    try:
        return bool(getattr(iv, "has_role")(role))
    except Exception:
        return False

def ramp_all_to_zero(iv, *, ramp_step: float = 0.1, delay_s: float = 0.02, log_fn=None):
    """
    Best-effort ramp TG/BG/Bias to 0V across different IV adapter APIs.

    Fixes:
      1) Works with keyword-only adapters that use lowercase names (vbg/vtg/vbias).
      2) Logs best-effort readback BEFORE and AFTER ramp (current gates/bias).
      3) Still defensive: tries multiple APIs, then falls back.
    """
    def _log(msg: str):
        if callable(log_fn):
            try:
                log_fn(msg)
            except Exception:
                pass

    def _read_setup(role: str):
        # best-effort: iv.setup.get_single_x_value("Vbg"/"Vtg"/"Vbias")
        try:
            setup = getattr(iv, "setup", None)
            getx = getattr(setup, "get_single_x_value", None) if setup else None
            if callable(getx):
                return float(getx(role))
        except Exception:
            pass
        return None

    def _read_gates():
        # best-effort: iv.read_current_gates() OR setup channel read
        try:
            if hasattr(iv, "read_current_gates"):
                a, b = iv.read_current_gates()
                return float(a), float(b)
        except Exception:
            pass
        return _read_setup("Vbg"), _read_setup("Vtg")

    def _read_bias():
        # best-effort: iv.read_current_bias/read_bias_voltage/read_bias OR setup channel read
        for name in ("read_current_bias", "read_bias_voltage", "read_bias"):
            if hasattr(iv, name):
                try:
                    return float(getattr(iv, name)())
                except Exception:
                    pass
        return _read_setup("Vbias")

    if iv is None:
        _log("Stop ramp: IV device not present.")
        return

    # --- log readback BEFORE ---
    try:
        bg0, tg0 = _read_gates()
        vb0 = _read_bias()
        _log(f"Stop ramp (before): Vbg={bg0} V, Vtg={tg0} V, Vbias={vb0} V")
    except Exception:
        pass

    # 1) If IV adapter provides a single method, prefer it.
    if hasattr(iv, "ramp_all_to_zero"):
        try:
            _call_compat(getattr(iv, "ramp_all_to_zero"), ramp_step=ramp_step, delay_s=delay_s)
            _call_compat(getattr(iv, "ramp_all_to_zero"), ramp_step=0.0, delay_s=0.0)
            # --- log readback AFTER ---
            try:
                bg1, tg1 = _read_gates()
                vb1 = _read_bias()
                _log(f"Stop ramp (after):  Vbg={bg1} V, Vtg={tg1} V, Vbias={vb1} V")
            except Exception:
                pass
            return
        except Exception as e:
            _log(f"Stop ramp: iv.ramp_all_to_zero failed, falling back. err={e}")

    # 2) Bias → 0V (try set_bias / set_vbias)
    try:
        if _has_role(iv, "Vbias"):
            if hasattr(iv, "set_bias"):
                fn = getattr(iv, "set_bias")

                # Try positional first
                try:
                    _call_compat(fn, 0.0, ramp_step=ramp_step, delay_s=delay_s)
                    _call_compat(fn, 0.0, ramp_step=0.0, delay_s=0.0)
                except TypeError:
                    # Try common keyword spellings
                    for key in ("Vbias", "vbias"):
                        try:
                            _call_compat(fn, **{key: 0.0, "ramp_step": ramp_step, "delay_s": delay_s})
                            _call_compat(fn, **{key: 0.0, "ramp_step": 0.0, "delay_s": 0.0})
                            break
                        except Exception:
                            continue

            elif hasattr(iv, "set_vbias"):
                fn = getattr(iv, "set_vbias")
                try:
                    _call_compat(fn, 0.0, ramp_step=ramp_step, delay_s=delay_s)
                    _call_compat(fn, 0.0, ramp_step=0.0, delay_s=0.0)
                except TypeError:
                    for key in ("Vbias", "vbias"):
                        try:
                            _call_compat(fn, **{key: 0.0, "ramp_step": ramp_step, "delay_s": delay_s})
                            _call_compat(fn, **{key: 0.0, "ramp_step": 0.0, "delay_s": 0.0})
                            break
                        except Exception:
                            continue
    except Exception as e:
        _log(f"Stop ramp: bias ramp failed (continuing). err={e}")

    # 3) Gates → 0V (handle keyword-only, lowercase keywords, or positional APIs)
    try:
        if hasattr(iv, "set_gates"):
            vbg_ok = _has_role(iv, "Vbg")
            vtg_ok = _has_role(iv, "Vtg")
            fn = getattr(iv, "set_gates")

            try:
                sig = inspect.signature(fn)
                names = list(sig.parameters.keys())
                names_l = [n.lower() for n in names]
            except Exception:
                names = []
                names_l = []

            # Decide supported keyword spellings
            bg_key = None
            tg_key = None

            # Prefer exact matches first (keeps original casing)
            if "Vbg" in names:
                bg_key = "Vbg"
            elif "vbg" in names:
                bg_key = "vbg"
            elif "bg" in names:
                bg_key = "bg"
            elif "vbg" in names_l:
                bg_key = names[names_l.index("vbg")]
            elif "bg" in names_l:
                bg_key = names[names_l.index("bg")]

            if "Vtg" in names:
                tg_key = "Vtg"
            elif "vtg" in names:
                tg_key = "vtg"
            elif "tg" in names:
                tg_key = "tg"
            elif "vtg" in names_l:
                tg_key = names[names_l.index("vtg")]
            elif "tg" in names_l:
                tg_key = names[names_l.index("tg")]


            # Keyword attempt (works for keyword-only adapters)
            if bg_key or tg_key:
                kw1 = {"ramp_step": ramp_step, "delay_s": delay_s}
                if vbg_ok and bg_key:
                    kw1[bg_key] = 0.0
                if vtg_ok and tg_key:
                    kw1[tg_key] = 0.0
                _call_compat(fn, **kw1)

                kw2 = {"ramp_step": 0.0, "delay_s": 0.0}
                if vbg_ok and bg_key:
                    kw2[bg_key] = 0.0
                if vtg_ok and tg_key:
                    kw2[tg_key] = 0.0
                _call_compat(fn, **kw2)

            else:
                # Positional form: set_gates(vbg, vtg, ...)
                if vbg_ok and vtg_ok:
                    _call_compat(fn, 0.0, 0.0, ramp_step=ramp_step, delay_s=delay_s)
                    _call_compat(fn, 0.0, 0.0, ramp_step=0.0, delay_s=0.0)
                elif vbg_ok and not vtg_ok:
                    _log("Stop ramp: only Vbg mapped but set_gates looks positional; skipping gate ramp.")
                elif vtg_ok and not vbg_ok:
                    _log("Stop ramp: only Vtg mapped but set_gates looks positional; skipping gate ramp.")
                else:
                    _log("Stop ramp: no gate roles mapped; skipping gate ramp.")
    except Exception as e:
        _log(f"Stop ramp: gate ramp failed. err={e}")

    # --- log readback AFTER ---
    try:
        bg1, tg1 = _read_gates()
        vb1 = _read_bias()
        _log(f"Stop ramp (after):  Vbg={bg1} V, Vtg={tg1} V, Vbias={vb1} V")
    except Exception:
        pass

def to_header_list(h):
    if h is None:
        return []
    if isinstance(h, list):
        return h
    if isinstance(h, tuple):
        return list(h)
    try:
        return np.asarray(h).ravel().tolist()
    except Exception:
        return []


# -------------------- schemas --------------------
LOOP_SCHEMA = ["Enable", "Parameter", "Values", "Level"]
BATCH_SCHEMA = [
    "Run",
    "repeat",
    "MeasurePower",
    "When",  # NEW (outer-loop guard)
    "condition_label",
    "Vbg_start",
    "Vbg_stop",
    "Vtg_start",
    "Vtg_stop",
    "frames",
    "Vbias_start",
    "Vbias_stop",
]




def normalize_df(df: pd.DataFrame, schema):
    df = (df if isinstance(df, pd.DataFrame) else pd.DataFrame()).copy()
    for c in schema:
        if c not in df.columns:
            df[c] = np.nan
    return df[schema].reset_index(drop=True)


def _norm(df: pd.DataFrame, cols: List[str]) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame(columns=cols)
    return df.reindex(columns=cols).fillna("").astype(str)


def _equal(a: pd.DataFrame, b: pd.DataFrame, cols: List[str]) -> bool:
    return _norm(a, cols).equals(_norm(b, cols))


# -------------------- batch helpers --------------------
def _normalize_batch_types(df: pd.DataFrame) -> pd.DataFrame:
    df = normalize_df(df, BATCH_SCHEMA).copy()
    # NEW: keep When as a plain string (avoid NaN in editor / logic)
    if "When" in df.columns:
        df["When"] = df["When"].fillna("").astype(str)

    for c in ("Run", "MeasurePower"):
        if c in df.columns:
            df[c] = df[c].map(_to_bool)
    for c in ("repeat", "frames"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(1).astype(int)
    for c in ("Vbg_start", "Vbg_stop", "Vtg_start", "Vtg_stop"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    # IMPORTANT: bias is OPTIONAL -> do NOT fillna(0.0), keep blank as NaN/None-like
    for c in ("Vbias_start", "Vbias_stop"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")  # blanks -> NaN
    return df

def build_run_plan_from_saved(loop_src: pd.DataFrame, batch_src: pd.DataFrame):
    """Build final_sequence + enabled batch df + totals from SAVED tables."""
    # --- loop -> final_sequence (same order as run) ---
    loop_df = normalize_df(loop_src, LOOP_SCHEMA).copy()
    loop_df["Enable"] = loop_df["Enable"].map(_to_bool)
    loop_df["Level"] = pd.to_numeric(loop_df["Level"], errors="coerce").fillna(1).astype(int)

    active = loop_df[loop_df["Enable"] == True].copy().sort_values("Level")


    levels = {}
    for _, row in active.iterrows():
        vals = parse_values(row.get("Values", ""), row.get("Parameter", ""))

        if vals:
            levels.setdefault(int(row.get("Level", 1)), []).append({"p": row.get("Parameter"), "v": vals})

    level_combos = []
    for lvl in sorted(levels.keys()):
        specs = levels[lvl]
        if len(set(len(s["v"]) for s in specs)) > 1:
            raise ValueError(f"Level {lvl} length mismatch")
        zipped = zip(*[s["v"] for s in specs])
        level_combos.append([{s["p"]: val for s, val in zip(specs, z)} for z in zipped])

    final_sequence = [{}] if not level_combos else []
    if level_combos:
        for combo in itertools.product(*level_combos):
            ctx = {}
            for d in combo:
                ctx.update(d)
            final_sequence.append(ctx)

    # --- batch enabled only ---
    df_batch = _normalize_batch_types(batch_src).copy()
    if "Run" in df_batch.columns:
        df_batch["Run"] = df_batch["Run"].map(_to_bool)
        df_batch = df_batch[df_batch["Run"] == True].reset_index(drop=True)

    # total acquisitions depends on When + outer-loop ctx
    total_acq = 0
    for ctx in final_sequence:
        outer = _outer_ctx_alias(ctx)
        for _, r in df_batch.iterrows():
            if _when_ok(r.get("When", ""), outer):
                try:
                    total_acq += max(int(r.get("repeat", 1)), 1)
                except Exception:
                    total_acq += 1

    total_acq = max(int(total_acq), 0)
    return final_sequence, df_batch, total_acq



def make_tree_snapshot(loop_src: pd.DataFrame, batch_src: pd.DataFrame) -> dict:
    """Tree state stored in session_state; used for idle view + live updates."""
    try:
        final_sequence, df_batch, total_acq = build_run_plan_from_saved(loop_src, batch_src)
    except Exception:
        final_sequence, df_batch, total_acq = [], _normalize_batch_types(batch_src), 0

    return dict(
        final_sequence=final_sequence,
        df_batch=df_batch,
        done=0,                 # COMPLETED acquisitions
        total_acq=total_acq,
        current_seq_i=-1,       # -1 means "no current"
        current_label="",
        current_rep_i=0,
    )


def validate_sweep_steps(
    df: pd.DataFrame,
    *,
    max_volts_per_step: float = 0.5,
    vbg_limits: Optional[Tuple[float, float]] = None,
    vtg_limits: Optional[Tuple[float, float]] = None,
):
    """
    Returns a list[dict] with explicit safety violations.
    Each dict has: row, label, problem, fix
    """

    bad: List[dict] = []
    if df is None or df.empty:
        return bad

    df2 = _normalize_batch_types(df).copy()

    # Correct boolean filter (IMPORTANT: no 'is True')
    if "Run" in df2.columns:
        df2["Run"] = df2["Run"].map(_to_bool)
        df2 = df2[df2["Run"] == True]

    for i, row in df2.reset_index(drop=True).iterrows():
        rownum = i + 1
        label = str(row.get("condition_label", f"row_{rownum}"))

        try:
            vbg_s, vbg_e = float(row["Vbg_start"]), float(row["Vbg_stop"])
            vtg_s, vtg_e = float(row["Vtg_start"]), float(row["Vtg_stop"])
            frames = max(int(row.get("frames", 1)), 1)
        except Exception:
            bad.append({
                "row": rownum,
                "label": label,
                "problem": "Parse error: Vbg/Vtg/frames",
                "fix": "Use numbers for Vbg/Vtg and an integer for frames.",
            })

            continue

        # --- Global limit violations (start/stop must be inside) ---
        if vbg_limits is not None:
            mn, mx = vbg_limits
            if not (mn <= vbg_s <= mx and mn <= vbg_e <= mx):
                bad.append({
                    "row": rownum,
                    "label": label,
                    "problem": f"Vbg out of range [{mn:.1f},{mx:.1f}]",
                    "fix": f"Clamp Vbg to limits or adjust global limits.",
                })

        if vtg_limits is not None:
            mn, mx = vtg_limits
            if not (mn <= vtg_s <= mx and mn <= vtg_e <= mx):
                bad.append({
                    "row": rownum,
                    "label": label,
                    "problem": f"Vtg out of range [{mn:.1f},{mx:.1f}]",
                    "fix": f"Clamp Vtg to limits or adjust global limits.",
                })

        # --- Step-size violations ---
        dvbg = abs(vbg_e - vbg_s)
        dvtg = abs(vtg_e - vtg_s)
        denom = max(frames - 1, 1)  # frames=1 => one big step

        vbg_step = dvbg / denom
        vtg_step = dvtg / denom

        def min_frames_needed(delta_v: float, limit: float) -> int:
            if delta_v <= 0:
                return 1
            return int(math.ceil(delta_v / max(limit, 1e-12))) + 1

        if vbg_step > max_volts_per_step:
            need = min_frames_needed(dvbg, max_volts_per_step)
            bad.append({
                "row": rownum,
                "label": label,
                "problem": f"Vbg step {vbg_step:.2f} > {max_volts_per_step:.2f} (frames={frames})",
                "fix": f"Set frames ≥ {need} or reduce Vbg span.",
            })

        if vtg_step > max_volts_per_step:
            need = min_frames_needed(dvtg, max_volts_per_step)
            bad.append({
                "row": rownum,
                "label": label,
                "problem": f"Vtg step {vtg_step:.2f} > {max_volts_per_step:.2f} (frames={frames})",
                "fix": f"Set frames ≥ {need} or reduce Vtg span.",
            })

    return bad



# -------------------- loop helpers --------------------
def _compute_loop_view(loop_work: pd.DataFrame, *, sweep_mode: str, is_custom: bool) -> pd.DataFrame:
    df = (loop_work if isinstance(loop_work, pd.DataFrame) else pd.DataFrame()).copy()

    for c in ["Enable", "Parameter", "Values"]:
        if c not in df.columns:
            df[c] = "" if c != "Enable" else False

    df["Enable"] = df["Enable"].map(_to_bool)

    if is_custom:
        if "Level" not in df.columns:
            df["Level"] = 1
        df["Level"] = pd.to_numeric(df["Level"], errors="coerce").fillna(1).astype(int)
        # --- NEW: renumber enabled levels so min enabled Level == 1 ---
        enabled = df["Enable"].map(_to_bool)
        if enabled.any():
            shift = int(df.loc[enabled, "Level"].min()) - 1
            if shift > 0:
                df.loc[enabled, "Level"] = df.loc[enabled, "Level"] - shift
    else:
        if sweep_mode == "Grid Scan (Nested)":
            lvl, out = 1, []
            for en in df["Enable"].tolist():
                out.append(lvl)
                if bool(en):
                    lvl += 1
            df["Level"] = out
        else:
            df["Level"] = 1

    df = df.reindex(columns=LOOP_SCHEMA)
    return df.reset_index(drop=True)


def _exp_s_str(ms_val: float) -> str:
    s = float(ms_val) / 1000.0
    return f"{int(s)}" if float(s).is_integer() else f"{s:.3g}"

def _fmt_deg_for_name(v) -> str:
    """Format a (possibly None) degree value for filenames."""
    if v is None:
        return ""
    s = str(v).strip()
    if s == "" or s.lower() in ("none", "nan"):
        return ""
    try:
        return f"{float(s):g}"
    except Exception:
        return ""

# -------------------- quick generator math --------------------
def coupled_sweep_preview(
    *,
    ratio: float,
    op_mode: str,
    K: float,
    vtg_min: float,
    vtg_max: float,
    vbg_min: float,
    vbg_max: float,
    step_v: float,
    reverse: bool,
) -> Optional[dict]:
    op_mode = "-" if op_mode == "-" else "+"

    # ratio*Vtg - Vbg = K  -> Vbg = ratio*Vtg - K
    # ratio*Vtg + Vbg = K  -> Vbg = -ratio*Vtg + K
    slope = ratio if op_mode == "-" else -ratio
    offset = -K if op_mode == "-" else K

    points = []

    y_at_min = slope * vtg_min + offset
    if vbg_min <= y_at_min <= vbg_max:
        points.append((vtg_min, y_at_min))

    y_at_max = slope * vtg_max + offset
    if vbg_min <= y_at_max <= vbg_max:
        points.append((vtg_max, y_at_max))

    if abs(slope) > 1e-12:
        x_at_vbg_min = (vbg_min - offset) / slope
        if vtg_min <= x_at_vbg_min <= vtg_max and not any(abs(x_at_vbg_min - p[0]) < 1e-6 for p in points):
            points.append((x_at_vbg_min, vbg_min))

        x_at_vbg_max = (vbg_max - offset) / slope
        if vtg_min <= x_at_vbg_max <= vtg_max and not any(abs(x_at_vbg_max - p[0]) < 1e-6 for p in points):
            points.append((x_at_vbg_max, vbg_max))

    if len(points) < 2:
        return None

    points.sort(key=lambda p: p[0])
    p_start, p_stop = points[0], points[-1]
    if reverse:
        p_start, p_stop = p_stop, p_start

    max_dist = max(abs(p_stop[0] - p_start[0]), abs(p_stop[1] - p_start[1]))
    frames = max(int(round(max_dist / max(step_v, 1e-6))) + 1, 2)

    return {
        "Vtg_start": float(f"{p_start[0]:.4f}"),
        "Vtg_stop":  float(f"{p_stop[0]:.4f}"),
        "Vbg_start": float(f"{p_start[1]:.4f}"),
        "Vbg_stop":  float(f"{p_stop[1]:.4f}"),
        "frames": int(frames),
    }


# -------------------- UI main --------------------
def render(devices, wavelength_headers, extra_scalar_fields_order):
    st.session_state.setdefault("extra_scalar_fields_order", extra_scalar_fields_order)
    # st.session_state.pop("filename_pattern", None)

    PARAM_TYPES = [
        "Center Wavelength (nm)",
        "Exposure Time (ms)",
        "Stage Position",
        "Accumulations (EPF)",
        "Rotation1 Angle (deg)",
        "Rotation2 Angle (deg)",
    ]

    st.header("Dual Gate Sweep – Advanced Looper (v4)")

    # --- recovery: Streamlit rerun can interrupt a run and leave _run_active stuck True ---
    st.session_state.setdefault("_run_active", False)
    st.session_state.setdefault("_stop_now", False)

    iv_now = (devices or {}).get("iv", None)
    if st.session_state.get("_run_active", False) and iv_now is None:
        st.session_state["_run_active"] = False
        st.session_state["_stop_now"] = False
        ui_log("Recovered: _run_active was stuck True while IV is disconnected. Controls unlocked.")

    # init state
    if "loop_src" not in st.session_state:
        df = pd.DataFrame(
            [
                {"Enable": False, "Parameter": "Center Wavelength (nm)", "Values": "800, 850", "Level": 1},
                {"Enable": False, "Parameter": "Exposure Time (ms)", "Values": "100, 500", "Level": 1},
                {"Enable": False, "Parameter": "Stage Position", "Values": "0, 10", "Level": 2},
            ]
        )
        st.session_state.loop_src = normalize_df(df, LOOP_SCHEMA)

    if "loop_work" not in st.session_state:
        st.session_state.loop_work = st.session_state.loop_src.copy()

    if "batch_src" not in st.session_state:
        init_df = pd.DataFrame(
            [
                {
                    "Run": True,
                    "repeat": 1,
                    "MeasurePower": False,
                    "When": "",
                    "condition_label": "BGonly",
                    "Vbg_start": 0.0,
                    "Vbg_stop": 1.0,
                    "Vtg_start": 0.0,
                    "Vtg_stop": 0.0,
                    "frames": 11,
                    "Vbias_start": "",
                    "Vbias_stop": "",
                }
            ]
        )
        st.session_state.batch_src = _normalize_batch_types(init_df)

    if "batch_work" not in st.session_state:
        st.session_state.batch_work = st.session_state.batch_src.copy()

    st.session_state.setdefault("_run_block_msg", "")
    st.session_state.setdefault("_run_block_details", "")

    c_settings = st.container()
    c_qgen = st.container()
    c_edit = st.container()
    c_log = st.container()

    # --- settings
    with c_settings:
        with st.expander("Base Settings & Defaults", expanded=False):
            c1, c2, c3 = st.columns(3)
            c1.text_input("Sample name", "Sample", key="sample_name")
            c2.text_input("Subfolder", "Initial data", key="subfolder")
            c3.text_input("Tag", "p1", key="tag")

            m1, m2, m3, m4, m5 = st.columns(5)
            m1.text_input("Laser λ (nm)", "730", key="def_laser")
            m2.text_input("Power (µW)", "1", key="def_power")
            m3.text_input("Default Exp (ms)", "1000", key="def_exp")
            m4.text_input("Default Center (nm)", "885", key="def_center")
            m5.number_input("Default Accumulations (EPF)", 1, 1000, 1, key="def_epf")

            # st.text_input(
            #     "Filename pattern",
            #     # "${sample}$~${tag}$~$6KPL{laser_nm}nm{power_uw}uw{exp_s}sx{epf}$~${center_nm}nmc$~${cond_block}$",
            #     "${sample}$~${tag}$~$6KPL{laser_nm}nm{power_uw}uw{exp_s}sx{epf}$~${center_nm}nmc$~${rot_block}$~${cond_block}$",
            #     help="Vars: {sample}, {tag}, {laser_nm}, {power_uw}, {exp_s}, {epf}, {center_nm}, {cond_block}. Tip: $...$ blocks make parsing easier.",
            #     key="filename_pattern",
            # )
            DEFAULT_PATTERN = (
                "${sample}$~${tag}$~$1.8KPL{laser_nm}nm{power_uw}uw{exp_s}sx{epf}$~"
                "${center_nm}nmc$~${rot_block}$~${cond_block}$"
            )

            # initialize only once
            if "filename_pattern" not in st.session_state:
                st.session_state["filename_pattern"] = DEFAULT_PATTERN

            cpat1, cpat2 = st.columns([1, 1])
            if cpat1.button("Reset filename pattern"):
                st.session_state["filename_pattern"] = DEFAULT_PATTERN

            st.text_input(
                "Filename pattern",
                key="filename_pattern",
                help="Vars: {sample}, {tag}, {laser_nm}, {power_uw}, {exp_s}, {epf}, {center_nm}, {rot_block}, {cond_block}",
            )


            st.markdown("### Safety limits")
            s1, s2, s3 = st.columns(3)
            with s1:
                st.number_input("Vtg Min (V)", value=-5.0, step=0.1, key="safe_vtg_min")
                st.number_input("Vtg Max (V)", value=5.0, step=0.1, key="safe_vtg_max")
            with s2:
                st.number_input("Vbg Min (V)", value=-5.0, step=0.1, key="safe_vbg_min")
                st.number_input("Vbg Max (V)", value=5.0, step=0.1, key="safe_vbg_max")
            with s3:
                st.number_input("Max |ΔV| per step (V)", value=0.5, min_value=0.001, step=0.05, key="safe_max_v_per_step")

    out_dir = (
        BASE_OUT
        / st.session_state.get("sample_name", "Sample")
        / st.session_state.get("subfolder", "Initial data")
    )

    # --- quick generator
    with c_qgen:
        with st.expander("⚡ Quick Generator: Coupled Sweep (ratio * TG ± BG = K)", expanded=False):
            c_eq1, c_eq2, c_eq3, c_eq4, c_eq5 = st.columns([1.2, 0.4, 0.9, 0.4, 1.2])
            with c_eq1:
                ratio = st.number_input("Ratio", value=float(st.session_state.get("qgen_ratio", 0.8)), step=0.05, format="%.3f", key="qgen_ratio")
            with c_eq2:
                st.markdown("<div style='text-align:center;padding-top:35px'><b>× TG</b></div>", unsafe_allow_html=True)
            with c_eq3:
                op_mode = st.selectbox("Operator", ["-", "+"], key="qgen_op", help="'-' → Efield, '+' → Doping")
            with c_eq4:
                st.markdown("<div style='text-align:center;padding-top:35px'><b>± BG =</b></div>", unsafe_allow_html=True)
            with c_eq5:
                const_label = "Doping" if op_mode == "+" else "Efield"
                K = st.number_input(f"{const_label} (K)", value=float(st.session_state.get("qgen_K", 0.0)), step=0.5, key="qgen_K")

            st.write("**Safe box**")
            lim1, lim2, lim3 = st.columns([1.5, 1.5, 1.3])
            with lim1:
                vtg_min = st.number_input("Vtg Min", value=float(st.session_state.get("safe_vtg_min", -5.0)), key="qgen_vtg_min")
                vtg_max = st.number_input("Vtg Max", value=float(st.session_state.get("safe_vtg_max", 5.0)), key="qgen_vtg_max")
            with lim2:
                vbg_min = st.number_input("Vbg Min", value=float(st.session_state.get("safe_vbg_min", -5.0)), key="qgen_vbg_min")
                vbg_max = st.number_input("Vbg Max", value=float(st.session_state.get("safe_vbg_max", 5.0)), key="qgen_vbg_max")
            with lim3:
                max_step = float(st.session_state.get("safe_max_v_per_step", 0.5))
                step_raw = st.number_input(
                    "Step (V)", min_value=0.001, max_value=1.0,
                    value=float(st.session_state.get("qgen_step", 0.05)),
                    step=0.001, format="%.3f", key="qgen_step"
                )
                step_v = float(min(step_raw, max_step))
                reverse = st.checkbox("Reverse?", value=bool(st.session_state.get("qgen_reverse", False)), key="qgen_reverse")
                st.caption(f"Step is capped at max step: {max_step:g} V")

            op_char = "+" if op_mode == "+" else "-"
            auto_label = f"{float(ratio):g}TG{op_char}BG={float(K):g}"

            auto_on = st.checkbox("Auto label", value=bool(st.session_state.get("qgen_auto_label", True)), key="qgen_auto_label")
            if auto_on:
                st.session_state["qgen_row_label"] = auto_label

            

            r1, r2, r3, r4 = st.columns([1.4, 1, 1, 1.4])
            with r1:
                row_label = st.text_input("Row Label", key="qgen_row_label", disabled=auto_on)
            with r2:
                q_repeat = st.number_input("repeat", min_value=1, value=int(st.session_state.get("qgen_repeat", 1)), step=1, key="qgen_repeat")
            with r3:
                q_measpwr = st.checkbox("MeasurePower", value=bool(st.session_state.get("qgen_measpwr", False)), key="qgen_measpwr")
            with r4:
                q_vbias = st.text_input("Vbias (optional)", value=str(st.session_state.get("qgen_vbias", "")), key="qgen_vbias")

            if st.button("🔍 Calculate / Preview", key="btn_qgen_calc"):
                prev = coupled_sweep_preview(
                    ratio=float(ratio),
                    op_mode=str(op_mode),
                    K=float(K),
                    vtg_min=float(vtg_min),
                    vtg_max=float(vtg_max),
                    vbg_min=float(vbg_min),
                    vbg_max=float(vbg_max),
                    step_v=float(step_v),
                    reverse=bool(reverse),
                )
                if prev is None:
                    st.session_state.pop("preview_row", None)
                    st.error("❌ Line does not intersect the safe box. Adjust ratio/K/bounds.")
                else:
                    # --- Parse q_vbias into Vbias_start / Vbias_stop (supports: "0", "0,-1.7", "0->-1.7", "0 to -1.7") ---
                    vb_txt = str(q_vbias).strip()
                    vb_start_txt = ""
                    vb_stop_txt = ""

                    if vb_txt:
                        t = vb_txt.replace("→", "->")
                        t = re.sub(r"\s+to\s+", "->", t, flags=re.IGNORECASE)  # "a to b" -> "a->b"
                        t = t.replace(",", "->")                               # "a,b" -> "a->b"
                        parts = [p.strip() for p in t.split("->") if p.strip()]
                        if len(parts) == 1:
                            vb_start_txt = parts[0]
                            vb_stop_txt  = parts[0]
                        elif len(parts) >= 2:
                            vb_start_txt = parts[0]
                            vb_stop_txt  = parts[1]

                    st.session_state.preview_row = {
                        "Run": True,
                        "repeat": int(q_repeat),
                        "MeasurePower": bool(q_measpwr),
                        "condition_label": str(row_label).strip() if str(row_label).strip() else auto_label,
                        "Vbg_start": prev["Vbg_start"],
                        "Vbg_stop": prev["Vbg_stop"],
                        "Vtg_start": prev["Vtg_start"],
                        "Vtg_stop": prev["Vtg_stop"],
                        "frames": int(prev["frames"]),
                        "Vbias_start": vb_start_txt,
                        "Vbias_stop":  vb_stop_txt,
                    }

                    # st.session_state.preview_row = {
                    #     "Run": True,
                    #     "repeat": int(q_repeat),
                    #     "MeasurePower": bool(q_measpwr),
                    #     "condition_label": str(row_label).strip() if str(row_label).strip() else auto_label,
                    #     "Vbg_start": prev["Vbg_start"],
                    #     "Vbg_stop": prev["Vbg_stop"],
                    #     "Vtg_start": prev["Vtg_start"],
                    #     "Vtg_stop": prev["Vtg_stop"],
                    #     "frames": int(prev["frames"]),
                    #     "Vbias": str(q_vbias).strip(),
                    # }

            if "preview_row" in st.session_state and isinstance(st.session_state.preview_row, dict):
                p = st.session_state.preview_row
                st.markdown("---")
                st.markdown("##### 📊 Result Preview (Dual gate Sweep)")
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Vbg", f"{p['Vbg_start']} → {p['Vbg_stop']} V")
                m2.metric("Vtg", f"{p['Vtg_start']} → {p['Vtg_stop']} V")
                m3.metric("Frames", p["frames"])
                m4.metric("Label", p["condition_label"])

                a1, a2 = st.columns(2)
                if a1.button("➕ Add Row to SAVED Table", key="btn_qgen_add"):
                    src = _normalize_batch_types(st.session_state.batch_src)
                    new_row = _normalize_batch_types(pd.DataFrame([p]))
                    st.session_state.batch_src = _normalize_batch_types(pd.concat([src, new_row], ignore_index=True))
                    st.session_state.batch_work = st.session_state.batch_src.copy()
                    st.session_state.pop("preview_row", None)
                    st.success("Row added to saved table. (Table editor still needs Apply/Run for its own edits.)")
                if a2.button("Cancel", key="btn_qgen_cancel"):
                    st.session_state.pop("preview_row", None)

    # --- editors + run controls (FORM to avoid hiccups)
    with c_edit:
        st.subheader("Loop Builder + Inner Electrical Sweep")
        st.caption("Edits do NOT trigger reruns. Preview + checks update when you click a button below.")

        sweep_mode = st.radio(
            "Sweep Logic",
            ["Grid Scan (Nested)", "Synchronized (Zipped)", "Custom (Advanced)"],
            horizontal=True,
            key="sweep_mode_radio",
        )
        is_custom = (sweep_mode == "Custom (Advanced)")
        # ✅ Add this explanation here
        if sweep_mode == "Grid Scan (Nested)":
            st.caption("Grid Scan: nesting order follows the order of enabled rows (top → bottom).")
        elif sweep_mode == "Synchronized (Zipped)":
            st.caption("Zipped: all enabled parameters advance together by index (same length required).")
        elif sweep_mode == "Custom (Advanced)":
            st.caption("Custom: Level is nesting order. Same Level = ZIPPED together.")

        base_cols = {
            "Enable": st.column_config.CheckboxColumn("On", default=False, width="small"),
            "Parameter": st.column_config.SelectboxColumn("Parameter", options=PARAM_TYPES, width="medium"),
            "Values": st.column_config.TextColumn("Values (comma sep)", width="large"),
        }
        custom_cols = {
            **base_cols,
            "Level": st.column_config.SelectboxColumn(
                "Level",
                options=[1, 2, 3, 4, 5],
                help="1 = outermost. Same Level means parameters are ZIPPED together (must have same # of values).",
                width="small",
            ),
        }

        col_cfg = {
            "Run": st.column_config.CheckboxColumn("Run", default=True, width="small"),
            "repeat": st.column_config.NumberColumn("Rep", min_value=1, default=1),
            "MeasurePower": st.column_config.CheckboxColumn("Meas Pwr?", default=False),
            "When": st.column_config.TextColumn(
                "When (outer-loop)",
                help=(
                    "Examples: center_nm=755,703; exposure_ms>=500; stage_pos==0; rot1_deg!=85\n"
                    "Allowed keys: center_nm, exposure_ms, stage_pos, epf, rot1_deg, rot2_deg\n"
                    "Ops: =, ==, !=, <, <=, >, >=  |  AND by ';' or newline"
                ),
                # width="large",
            ),
            "condition_label": st.column_config.TextColumn("Label"),
            "Vbg_start": st.column_config.NumberColumn("Vbg Start", format="%.3f"),
            "Vbg_stop": st.column_config.NumberColumn("Vbg Stop", format="%.3f"),
            "Vtg_start": st.column_config.NumberColumn("Vtg Start", format="%.3f"),
            "Vtg_stop": st.column_config.NumberColumn("Vtg Stop", format="%.3f"),
            "frames": st.column_config.NumberColumn("Frames", min_value=1),
            "Vbias_start": st.column_config.NumberColumn(
                "Vbias Start (V)", format="%.3f", help="Leave blank to disable bias."
            ),
            "Vbias_stop": st.column_config.NumberColumn(
                "Vbias Stop (V)", format="%.3f", help="Leave blank to disable bias."
            ),


        }

        # We'll fill this *after* we know what happened (Run/Apply/etc)
        run_notice_ph = None

        with st.form("edit_form", clear_on_submit=False):
            st.markdown("### Loop Builder")

            loop_init_full = _compute_loop_view(
                st.session_state.loop_work,
                sweep_mode=sweep_mode,
                is_custom=is_custom,
            )

            loop_init = loop_init_full if is_custom else loop_init_full.drop(columns=["Level"], errors="ignore")


            loop_work = st.data_editor(
                loop_init,
                key="loop_editor",
                column_config=(custom_cols if is_custom else base_cols),
                column_order=(["Enable", "Parameter", "Values", "Level"] if is_custom else ["Enable", "Parameter", "Values"]),
                hide_index=True,
                num_rows="dynamic",
                width="stretch",
            )

            # --- Preview (updates when you click a button; no per-edit reruns)
            with st.expander("Show Sequence Preview", expanded=True):
                try:
                    loop_view_now = _compute_loop_view(loop_work, sweep_mode=sweep_mode, is_custom=is_custom)
                    active_rows = loop_view_now[loop_view_now["Enable"] == True].copy()
                    if active_rows.empty:
                        st.info("No parameters enabled.")
                    else:
                        active_rows = active_rows.sort_values("Level")
                        levels = {}
                        for _, row in active_rows.iterrows():
                            lvl = int(row["Level"])
                            vals = parse_values(row.get("Values", ""), row.get("Parameter", ""))

                            if vals:
                                levels.setdefault(lvl, []).append({"param": row["Parameter"], "vals": vals})

                        dot = [
                            "digraph G {",
                            "  rankdir=LR;",
                            '  node [shape=box, style="filled,rounded", fillcolor="#f0f2f6", fontname="Sans", fontsize=10];',
                            '  START [shape=ellipse, fillcolor="#d4edda", label="Start"];',
                        ]

                        MAX_BRANCHES = 3

                        def build_tree(pid, lidx):
                            sl = sorted(levels.keys())
                            if lidx >= len(sl):
                                return
                            curr = sl[lidx]
                            specs = levels[curr]
                            if len(set(len(s["vals"]) for s in specs)) > 1:
                                dot.append(f'"{pid}" -> "ERR_{curr}" [label="Len Mismatch"];')
                                dot.append(f'"ERR_{curr}" [shape=box, fillcolor="#ffcccc", label="Error: Level {curr}\\nLengths mismatch"];')
                                return

                            lens0 = len(specs[0]["vals"])
                            avals_iter = zip(*[s["vals"] for s in specs])
                            for i, tup in enumerate(itertools.islice(avals_iter, MAX_BRANCHES)):

                                lbl = "\\n".join(
                                    [f"{s['param'].split('(')[0].strip()}: {tup[si]}" for si, s in enumerate(specs)]
                                )
                                nid = f"{pid}_L{curr}_{i}"
                                dot.append(f'"{nid}" [label="{lbl}"];')
                                dot.append(f'"{pid}" -> "{nid}";')
                                build_tree(nid, lidx + 1)

                            if lens0  > MAX_BRANCHES:
                                dot.append(f'"{pid}_more" [label="... ({lens0 -MAX_BRANCHES} more)" shape=plaintext];')
                                dot.append(f'"{pid}" -> "{pid}_more" [style=dotted];')

                        build_tree("START", 0)
                        dot.append("}")
                        st.graphviz_chart("\n".join(dot))

                        total = 1
                        for lvl in levels:
                            total *= len(levels[lvl][0]["vals"])
                        st.caption(f"Total Sequences: {total}")

                except Exception as e:
                    st.error(f"Preview Error: {e}")

            st.markdown("### Inner Electrical Sweep")
            batch_init = normalize_df(st.session_state.batch_work, BATCH_SCHEMA).copy()

            # ✅ Force editor-friendly dtypes
            if "When" in batch_init.columns:
                batch_init["When"] = batch_init["When"].fillna("").astype(str)
            if "condition_label" in batch_init.columns:
                batch_init["condition_label"] = batch_init["condition_label"].fillna("").astype(str)

            batch_work = st.data_editor(
                batch_init,
                key="batch_editor",
                column_config=col_cfg,
                column_order=BATCH_SCHEMA,
                hide_index=True,
                num_rows="dynamic",
                width="stretch",
            )


            # buttons row
            b1, b2, b3 = st.columns([1.2, 1.2, 1.6])

            apply_all_btn   = b1.form_submit_button("Apply All")
            discard_all_btn = b2.form_submit_button("Discard All")
            run_btn         = b3.form_submit_button("Run Sequence", type="primary")


        # IMPORTANT: placeholders must be outside the form, otherwise updates can lag/stale
        run_notice_ph = st.empty()
        run_preview_ph = st.empty()
        # outside st.form, near the Run button area
        run_progress_ph = st.empty()
        run_progress_text_ph = st.empty()

        # -------- emergency stop state + button --------
                # -------- emergency stop state + button --------
        st.session_state.setdefault("_run_active", False)
        st.session_state.setdefault("_stop_now", False)

        iv_dev = (devices or {}).get("iv", None)
        run_active = bool(st.session_state.get("_run_active", False))

        if st.button(
            "🛑 STOP NOW (ramp TG/BG/Bias → 0V)",
            key="btn_stop_now",
            disabled=(iv_dev is None),
        ):
            # 1) Tell the running sweep to stop ASAP (stop_cb reads this)
            st.session_state["_stop_now"] = True
            ui_log("🛑 STOP pressed — Use Manual Gate Control to ramp outputs to 0V NOW.")

            # 2) Force ramp immediately even if a run is active.
            #    This guarantees we don't rely on the sweep step to do the ramp.
            try:
                ramp_all_to_zero(iv_dev, ramp_step=0.1, delay_s=0.02, log_fn=ui_log)
                ui_log("🛑 STOP: ramp command issued (see before/after readback above).")
            except Exception as e:
                ui_log(f"Stop ramp error (ignored): {e}")

            # 3) If not running, we can clear the stop flag right away.
            #    If running, keep it True so stop_cb stays True until the runner exits.
            if not run_active:
                st.session_state["_stop_now"] = False


        if st.button("🔓 UNLOCK reconnect (force clear run state)", key="btn_force_unlock"):
            st.session_state["_run_active"] = False
            st.session_state["_stop_now"] = False
            ui_log("Force unlocked run state (reconnect controls enabled).")
            st.rerun()


        # --------- process actions (one rerun per click; no per-edit hiccups) ---------
        clicked_any = apply_all_btn or discard_all_btn or run_btn
        if clicked_any:
            # Persist the buffered editor outputs from THIS submit
            loop_work_now  = normalize_df(loop_work, LOOP_SCHEMA)
            batch_work_now = normalize_df(batch_work, BATCH_SCHEMA)

            st.session_state.loop_work  = loop_work_now
            st.session_state.batch_work = batch_work_now

        # --- Apply All ---
        if apply_all_btn:
            loop_view_now = _compute_loop_view(loop_work_now, sweep_mode=sweep_mode, is_custom=is_custom)

            # Save both
            st.session_state.loop_src  = loop_view_now.copy()
            st.session_state.loop_work = loop_view_now.copy()

            st.session_state.batch_src  = _normalize_batch_types(batch_work_now)
            st.session_state.batch_work = st.session_state.batch_src.copy()

            st.session_state["run_preview"] = build_run_preview(
                st.session_state.loop_src,
                st.session_state.batch_src,
                max_examples=6,
            )
            st.session_state["run_preview_dirty"] = False

            st.session_state["tree_snapshot"] = make_tree_snapshot(st.session_state.loop_src, st.session_state.batch_src)

            vbias_issue = _check_vbias_mapping(st.session_state.batch_src, devices)

            with run_notice_ph.container():
                st.success("✅ Applied Loop + Table.")
                if vbias_issue:
                    st.error(vbias_issue)

        # --- Discard All ---
        if discard_all_btn:
            st.session_state.loop_work  = st.session_state.loop_src.copy()
            st.session_state.batch_work = st.session_state.batch_src.copy()

            st.session_state["run_preview"] = build_run_preview(st.session_state.loop_src, st.session_state.batch_src)
            st.session_state["tree_snapshot"] = make_tree_snapshot(st.session_state.loop_src, st.session_state.batch_src)
            vbias_issue = _check_vbias_mapping(st.session_state.batch_src, devices)
            with run_notice_ph.container():
                st.info("↩️ Discarded Loop + Table edits.")
                if vbias_issue:
                    st.error(vbias_issue)    


        if run_btn:
            # Always evaluate using the editor outputs from THIS submission
            loop_work_now = normalize_df(loop_work, LOOP_SCHEMA)
            batch_work_now = normalize_df(batch_work, BATCH_SCHEMA)

            # Keep work buffers in session_state (usually desired)
            st.session_state.loop_work = loop_work_now
            st.session_state.batch_work = batch_work_now

            # Build comparable loop view (adds Level in non-custom modes)
            loop_view_now = _compute_loop_view(loop_work_now, sweep_mode=sweep_mode, is_custom=is_custom)


            # Unsaved checks (compare against SAVED src)
            loop_unsaved = not _equal(loop_view_now, st.session_state.loop_src, LOOP_SCHEMA)

            batch_unsaved = not _equal(
                _normalize_batch_types(batch_work_now),
                _normalize_batch_types(st.session_state.batch_src),
                BATCH_SCHEMA,
            )

            # Default: do not start run
            st.session_state["_start_run"] = False

            if loop_unsaved or batch_unsaved:
                parts = []
                if loop_unsaved:
                    parts.append("Loop Builder")
                if batch_unsaved:
                    parts.append("Electrical Sweep Table")

                run_notice_ph.error(
                    f"⛔ Unsaved edits detected in: {', '.join(parts)}. "
                    f"Apply or Discard, then Run again."
                )
            else:
                # Build final_sequence from SAVED tables, then keep only rows that run at least once
                final_sequence_saved, df_batch_saved, _ = build_run_plan_from_saved(
                    st.session_state.loop_src,
                    st.session_state.batch_src,
                )

                df_effective = df_batch_saved.copy().reset_index(drop=True)
                if "When" in df_effective.columns and not df_effective.empty:
                    keep = []
                    for _, row in df_effective.iterrows():
                        w = row.get("When", "")
                        if not str(w).strip():
                            keep.append(True)
                        else:
                            ok_any = any(_when_ok(w, _outer_ctx_alias(ctx)) for ctx in final_sequence_saved)
                            keep.append(bool(ok_any))
                    df_effective = df_effective[pd.Series(keep)].reset_index(drop=True)

                # 1) Require Vbg/Vtg roles for dual gate sweep
                gate_issue = _check_gate_mapping(devices)
                if gate_issue:
                    run_notice_ph.error(gate_issue)
                    st.session_state["_start_run"] = False

                else:
                    # 2) Vbias is optional: only required if requested in table
                    vbias_issue = _check_vbias_mapping(df_effective, devices)
                    if vbias_issue:
                        run_notice_ph.error(vbias_issue)
                        st.session_state["_start_run"] = False
                    else:
                        # 3) Safety checks (only when saved)
                        max_step = float(st.session_state.get("safe_max_v_per_step", 0.5))
                        vbg_limits = (
                            float(st.session_state.get("safe_vbg_min", -5.0)),
                            float(st.session_state.get("safe_vbg_max", 5.0)),
                        )
                        vtg_limits = (
                            float(st.session_state.get("safe_vtg_min", -5.0)),
                            float(st.session_state.get("safe_vtg_max", 5.0)),
                        )

                        bad = validate_sweep_steps(
                            df_effective,
                            max_volts_per_step=max_step,
                            vbg_limits=vbg_limits,
                            vtg_limits=vtg_limits,
                        )

                        if bad:
                            with run_notice_ph.container():
                                st.error("Safety violation(s). Fix these rows:")
                                vdf = pd.DataFrame(bad)
                                show_cols = [c for c in ["row", "label", "problem", "fix"] if c in vdf.columns]
                                st.dataframe(vdf[show_cols], width="stretch", hide_index=True)
                        else:
                            run_notice_ph.success("✅ Starting run…")
                            st.session_state["_start_run"] = True
                            ui_log("Initiating run...")


        # Always render preview near Run (it only CHANGES when Apply All / Discard All updates run_preview)
        tree_live_ph = None

        with run_preview_ph.container():
            with st.expander("Run Preview (updates on Apply All)", expanded=False):
                tree_live_ph = st.empty()   # LIVE status tree (updates during run)
                # If no snapshot yet OR Apply/Discard changed the plan, rebuild snapshot from SAVED tables
                if ("tree_snapshot" not in st.session_state) or st.session_state.get("run_preview_dirty", False):
                    st.session_state["tree_snapshot"] = make_tree_snapshot(st.session_state.loop_src, st.session_state.batch_src)

                snap = st.session_state["tree_snapshot"]
                param_order = get_enabled_param_order(st.session_state.loop_src)
                # Always show HTML tree (idle = all todo)
                html_tree = build_run_status_tree_html(
                    snap["final_sequence"],
                    snap["df_batch"],
                    done=snap["done"],
                    total_acq=snap["total_acq"],
                    current_seq_i=snap["current_seq_i"],
                    current_label=snap["current_label"],
                    current_rep_i=snap["current_rep_i"],
                    max_seq_show=10,
                    param_order=param_order
                )

                tree_live_ph.markdown(html_tree, unsafe_allow_html=True)
                if st.session_state.get("run_preview_dirty", True) or ("run_preview" not in st.session_state):
                    st.session_state["run_preview"] = build_run_preview(
                        st.session_state.loop_src,
                        st.session_state.batch_src,
                        max_examples=6,
                    )
                    st.session_state["run_preview_dirty"] = False

                rp = st.session_state.get("run_preview")
                if not rp:
                    st.info("Click **Apply All** to generate the preview from saved tables.")
                elif not rp.get("ok", True):
                    st.error(rp.get("error", "Preview error"))
                else:
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Loop conditions", rp["cond_count"])
                    c2.metric("Enabled batch rows", rp["batch_rows"])
                    c3.metric("Total acquisitions", rp["total_acquisitions"])
                    c4.metric("Total frames", rp["total_frames"])

                    
                    
                        
                    # Store placeholder for run-time updates (closure-friendly)
                    st.session_state["_tree_live_enabled"] = True

                    if rp.get("examples"):
                        st.caption("Example loop conditions:")
                        for s in rp["examples"]:
                            st.write(f"- {s}")

    # --- log panel
    with c_log:
        st.markdown("---")
        with st.expander("Run log", expanded=True):
            st.session_state.active_log_box = st.empty()
            ui_log("")
            if st.button("Clear Log", key="btn_clear_log"):
                st.session_state.run_log = []
                ui_log("")

    # --- execution
    def run_and_log(tree_live_ph=None):
        def _stop_requested() -> bool:
            return bool(st.session_state.get("_stop_now", False))

        # progress bar helper
        def _fmt_ctx(ctx_vars: dict) -> str:
            """Compact loop context string."""
            # order matters for readability
            keys = [
                "Center Wavelength (nm)",
                "Exposure Time (ms)",
                "Stage Position",
                "Accumulations (EPF)",
                "Rotation1 Angle (deg)",
                "Rotation2 Angle (deg)",
            ]
            parts = []
            for k in keys:
                if k in ctx_vars and ctx_vars[k] is not None and str(ctx_vars[k]).strip() != "":
                    k0 = k.split("(")[0].strip()
                    try:
                        v = float(ctx_vars[k])
                        parts.append(f"{k0}={v:g}")
                    except Exception:
                        parts.append(f"{k0}={ctx_vars[k]}")
            return ", ".join(parts) if parts else "(no loop vars)"
        param_order = get_enabled_param_order(st.session_state.loop_src)


        lf6 = st.session_state.get("lf6")
        ui_log("Building sequence...")

        loop_df = st.session_state.loop_src.copy()
        loop_df["Enable"] = loop_df["Enable"].map(_to_bool)
        active = loop_df[loop_df["Enable"] == True].copy().sort_values("Level")

        levels = {}
        for _, row in active.iterrows():
            vals = parse_values(row.get("Values", ""), row.get("Parameter", ""))

            if vals:
                levels.setdefault(int(row.get("Level", 1)), []).append({"p": row.get("Parameter"), "v": vals})

        level_combos = []
        for lvl in sorted(levels.keys()):
            specs = levels[lvl]
            if len(set(len(s["v"]) for s in specs)) > 1:
                ui_log(f"Error: Level {lvl} length mismatch.")
                return
            zipped = zip(*[s["v"] for s in specs])
            level_combos.append([{s["p"]: val for s, val in zip(specs, z)} for z in zipped])

        final_sequence = [{}] if not level_combos else []
        if level_combos:
            for combo in itertools.product(*level_combos):
                ctx = {}
                for d in combo:
                    ctx.update(d)
                final_sequence.append(ctx)

        ui_log(f"Generated {len(final_sequence)} conditions.")

        df_batch = _normalize_batch_types(st.session_state.batch_src).copy()
        if "Run" in df_batch.columns:
            df_batch["Run"] = df_batch["Run"].apply(_to_bool)
            df_batch = df_batch[df_batch["Run"] == True]

        if df_batch.empty:
            ui_log("No batch rows enabled (checked).")
            return

        # -------- progress bar (replaces spinner) --------
        # total "acquisitions" = loop conditions × enabled batch rows × repeats
        total_conditions = len(final_sequence)

        # NEW: per-seq enabled batch ids (based on When) + exact totals
        df_batch = df_batch.reset_index(drop=True)
        batch_ids_per_seq: List[set] = []
        total_acq = 0
        total_frames_all = 0

        for ctx_vars in final_sequence:
            outer = _outer_ctx_alias(ctx_vars)
            enabled_ids = set()
            for b_i, row in df_batch.iterrows():
                if _when_ok(row.get("When", ""), outer):
                    enabled_ids.add(b_i)
                    reps = max(int(row.get("repeat", 1)), 1)
                    frs  = max(int(row.get("frames", 1)), 1)
                    total_acq += reps
                    total_frames_all += reps * frs
            batch_ids_per_seq.append(enabled_ids)

        if total_acq <= 0:
            ui_log("Nothing to run: all batch rows are skipped by 'When' guards.")
            return

        done = 0
        done_frames = 0
        total_frames_global = max(int(total_frames_all), 1)



        # keep snapshot in sync so the preview tree stays updated even after run ends
        # pick first allowed batch row for seq 0 (if any)
        first_label = ""
        if total_conditions > 0 and batch_ids_per_seq and batch_ids_per_seq[0]:
            first_id = sorted(list(batch_ids_per_seq[0]))[0]
            first_label = sanitize_filename(df_batch.iloc[first_id].get("condition_label", ""))

        st.session_state["tree_snapshot"] = dict(
            final_sequence=final_sequence,
            df_batch=df_batch.reset_index(drop=True).copy(),
            done=0,
            total_acq=total_acq,
            current_seq_i=0 if total_conditions > 0 else -1,
            current_label=first_label,
            current_rep_i=0,
        )


        # ---- init LIVE status tree (all TODO, first item highlighted) ----
        if tree_live_ph is not None:
            try:
                # Use the first *allowed* batch row for seq 0 (respects When)
                first_label = ""
                if total_conditions > 0 and batch_ids_per_seq and batch_ids_per_seq[0]:
                    first_id = sorted(list(batch_ids_per_seq[0]))[0]
                    first_label = sanitize_filename(df_batch.iloc[first_id].get("condition_label", ""))

                tree_live_ph.markdown(
                    build_run_status_tree_html(
                        final_sequence,
                        df_batch,
                        done=0,
                        total_acq=total_acq,
                        current_seq_i=0 if total_conditions > 0 else -1,
                        current_label=first_label,
                        current_rep_i=0,
                        max_seq_show=8,
                        param_order=param_order
                    ),
                    unsafe_allow_html=True,
                )
            except Exception:
                pass




        run_progress_text_ph.info("Preparing run…")

        supports_text = True
        try:
            bar = run_progress_ph.progress(0, text=f"Frames 0/{total_frames_global}")
        except TypeError:
            supports_text = False
            bar = run_progress_ph.progress(0)


        total = total_conditions

        for seq_i, ctx_vars in enumerate(final_sequence):
            ui_log(f"--- Sequence {seq_i + 1}/{total} ---")

            val_center = ctx_vars.get("Center Wavelength (nm)", float(st.session_state.get("def_center", 885)))
            val_exp    = ctx_vars.get("Exposure Time (ms)", float(st.session_state.get("def_exp", 1000)))
            val_stage  = ctx_vars.get("Stage Position")
            val_epf    = int(ctx_vars.get("Accumulations (EPF)", st.session_state.get("def_epf", 2)))
            val_rot1   = ctx_vars.get("Rotation1 Angle (deg)")
            val_rot2   = ctx_vars.get("Rotation2 Angle (deg)")

            # (your stage/rotation/LF config code stays the same)

            ctx_str = _fmt_ctx(ctx_vars)
            allowed_ids = batch_ids_per_seq[seq_i]
            for b_i, row in df_batch.iterrows():
                if b_i not in allowed_ids:
                    continue
                cond_label = sanitize_filename(row.get("condition_label", ""))
                vbias_block = _vbias_block_from_row(row)
                cond_block_for_name = cond_label + (f"_{vbias_block}" if vbias_block else "")

                # sweep params for display
                try:
                    vbg_s, vbg_e = float(row["Vbg_start"]), float(row["Vbg_stop"])
                    vtg_s, vtg_e = float(row["Vtg_start"]), float(row["Vtg_stop"])
                    frames = int(row.get("frames", 1))
                except Exception:
                    vbg_s = vbg_e = vtg_s = vtg_e = 0.0
                    frames = int(row.get("frames", 1) or 1)

                n_rep = max(int(row.get("repeat", 1)), 1)

                for r_i in range(n_rep):
                    # ---- your existing acquisition logic (ONCE per acquisition) ----
                    rep_suffix = f"_rep{r_i+1:02d}" if n_rep > 1 else ""

                    # measure power FIRST so filename reflects measured power
                    power_uw_str = str(st.session_state.get("def_power", "1"))
                    if _to_bool(row.get("MeasurePower", False)) and devices:
                        pm = devices.get("pm")
                        if pm:
                            try:
                                p_w = pm.get_power()
                                power_uw_str = f"{float(p_w) * 1e6:.3f}"
                                ui_log(f"  > Meas Power: {power_uw_str} uW")
                            except Exception as e:
                                ui_log(f"  > Power Meas Failed: {e}")

                    # fname_ctx = SafeDict(**ctx_vars)
                    # fname_ctx.update(
                    #     {
                    #         "sample": st.session_state.get("sample_name"),
                    #         "tag": st.session_state.get("tag"),
                    #         "laser_nm": st.session_state.get("def_laser"),
                    #         "power_uw": power_uw_str,
                    #         "exp_s": _exp_s_str(val_exp),
                    #         "cond_block": cond_block_for_name,
                    #         "center_nm": f"{float(val_center):.0f}",
                    #         "epf": f"{val_epf}",
                    #     }
                    # )
                    rot1_deg = _fmt_deg_for_name(val_rot1)
                    rot2_deg = _fmt_deg_for_name(val_rot2)

                    rot_parts = []
                    if rot1_deg:
                        rot_parts.append(f"R1_{rot1_deg}deg")
                    if rot2_deg:
                        rot_parts.append(f"R2_{rot2_deg}deg")
                    rot_block = "_".join(rot_parts)  # "" if neither is set

                    fname_ctx = SafeDict(**ctx_vars)
                    fname_ctx.update(
                        {
                            "sample": st.session_state.get("sample_name"),
                            "tag": st.session_state.get("tag"),
                            "laser_nm": st.session_state.get("def_laser"),
                            "power_uw": power_uw_str,
                            "exp_s": _exp_s_str(val_exp),
                            "cond_block": cond_block_for_name,
                            "center_nm": f"{float(val_center):.0f}",
                            "epf": f"{val_epf}",

                            # NEW: rotation placeholders for filename_pattern
                            "rot1_deg": rot1_deg,
                            "rot2_deg": rot2_deg,
                            "rot_block": rot_block,
                        }
                    )

                    try:
                        pat = st.session_state.get("filename_pattern")
                        ui_log(f"DEBUG filename_pattern = {st.session_state.get('filename_pattern')}")
                        stem_base = sanitize_filename(pat.format_map(fname_ctx)) + rep_suffix
                    except Exception as e:
                        ui_log(f"Filename Error: {e}")
                        stem_base = f"ERR_{cond_label}{rep_suffix}"

                    stem_final = unique_stem(out_dir, stem_base)

                    # ---- progress update (NOW we can show filename) ----
                    # show "current" progress BEFORE running
                    # At the start of this acquisition, show progress based on frames already completed
                    pct = int(100 * (done_frames / total_frames_global))
                    pct = min(max(pct, 0), 100)

                    if supports_text:
                        bar.progress(pct, text=f"Frames {done_frames}/{total_frames_global}")
                    else:
                        bar.progress(pct)



                    # markdown below (styled): filename + only missing sweep info
                    # run_progress_text_ph.markdown(
                    #     f"**Saving file:**\n\n"
                    #     f"```text\n{stem_final}.csv\n```\n"
                    #     f"Vbg `{vbg_s:g}→{vbg_e:g}` • Vtg `{vtg_s:g}→{vtg_e:g}` • frs `{frames}` • rep **{r_i+1}/{n_rep}**"
                    # )
                    vb_s = _parse_optional_float(row.get("Vbias_start", ""))
                    vb_e = _parse_optional_float(row.get("Vbias_stop", ""))

                    vb_txt = ""
                    if vb_s != "INVALID" and vb_e != "INVALID":
                        if vb_s is None and vb_e is not None:
                            vb_s = vb_e
                        if vb_e is None and vb_s is not None:
                            vb_e = vb_s
                        if vb_s is not None and vb_e is not None:
                            vb_s = float(vb_s); vb_e = float(vb_e)
                            vb_txt = f" • Vbias `{vb_s:g}→{vb_e:g} V`"


                    run_progress_text_ph.markdown(
                        f"**Saving file:**\n\n"
                        f"```text\n{stem_final}.csv\n```\n"
                        f"Vbg `{vbg_s:g}→{vbg_e:g}` • Vtg `{vtg_s:g}→{vtg_e:g}`{vb_txt} • frs `{frames}` • rep **{r_i+1}/{n_rep}**"
                    )



                    # ---- LIVE tree: highlight current (done = completed so far) ----
                    if tree_live_ph is not None:
                        try:

                            st.session_state["tree_snapshot"].update(
                                done=done,
                                total_acq=total_acq,
                                current_seq_i=seq_i,
                                current_label=cond_label,
                                current_rep_i=r_i,
                            )
                            snap = st.session_state["tree_snapshot"]
                            tree_live_ph.markdown(
                                build_run_status_tree_html(
                                    snap["final_sequence"],
                                    snap["df_batch"],
                                    done=snap["done"],
                                    total_acq=snap["total_acq"],
                                    current_seq_i=snap["current_seq_i"],
                                    current_label=snap["current_label"],
                                    current_rep_i=snap["current_rep_i"],
                                    max_seq_show=8,
                                    param_order=param_order
                                ),
                                unsafe_allow_html=True,
                            )
                        except Exception:
                            pass


                    # ---- build recipe and run ----
                    # recipe = [
                    #     {"step": "set_lightfield", "ms": float(val_exp), "center_nm": float(val_center), "epf": int(val_epf)}
                    # ]
                    recipe = []

                    # --- rotate first (if loop provided angles) ---
                    if val_rot1 is not None and str(val_rot1).strip() != "":
                        if devices and devices.get("rotation1"):
                            recipe.append({"step": "rotate_to", "which": "rotation1", "angle_deg": float(val_rot1)})
                        else:
                            ui_log("Rotation1 not connected; skipping rotate_to.")

                    if val_rot2 is not None and str(val_rot2).strip() != "":
                        if devices and devices.get("rotation2"):
                            recipe.append({"step": "rotate_to", "which": "rotation2", "angle_deg": float(val_rot2)})
                        else:
                            ui_log("Rotation2 not connected; skipping rotate_to.")


                    # --- then configure LF6 ---
                    recipe.append(
                        {"step": "set_lightfield", "ms": float(val_exp), "center_nm": float(val_center), "epf": int(val_epf)}
                    )
                    # --- optional bias ---
                    # vb = _parse_optional_float(row.get("Vbias", ""))
                    # if vb == "INVALID":
                    #     ui_log("Vbias parse warning (ignored).")
                    # elif vb is not None:
                    #     recipe.append({"step": "set_bias", "Vbias": float(vb)})

                    # NOTE: do NOT append a separate set_bias step here.
                    # Bias (if requested) will be handled inside the sweep step.

                    # --- then the sweep preset ---
                    params = dict(
                        Vbg_start=float(row["Vbg_start"]),
                        Vbg_stop=float(row["Vbg_stop"]),
                        Vtg_start=float(row["Vtg_start"]),
                        Vtg_stop=float(row["Vtg_stop"]),
                        frames=int(row["frames"]),
                        file_base=stem_final,
                    )

                    vb_s = _parse_optional_float(row.get("Vbias_start", ""))
                    vb_e = _parse_optional_float(row.get("Vbias_stop", ""))

                    if vb_s != "INVALID" and vb_e != "INVALID":
                        if vb_s is None and vb_e is not None:
                            vb_s = vb_e
                        if vb_e is None and vb_s is not None:
                            vb_e = vb_s
                        if vb_s is not None and vb_e is not None:
                            params["Vbias_start"] = float(vb_s)
                            params["Vbias_stop"]  = float(vb_e)
                    acq_frames = max(int(frames), 1)

                    def _frame_progress_cb(frame_i: int, frame_total_in_this_file: int):
                        # frame_i is 1..frame_total_in_this_file
                        nonlocal done_frames

                        pct = int(100 * ((done_frames + frame_i) / total_frames_global))
                        pct = min(max(pct, 0), 100)

                        try:
                            if supports_text:
                                bar.progress(pct, text=f"Frames {done_frames + frame_i}/{total_frames_global}")
                            else:
                                bar.progress(pct)
                        except Exception:
                            pass



                    # recipe += build_recipe_from_preset("dual_gate_sweep", params)
                    sweep_recipe = build_recipe_from_preset("dual_gate_sweep", params)

                    # attach callback to the sweep step(s)
                    for stp in sweep_recipe:
                        if stp.get("step") == "dual_gate_sweep":
                            stp["frame_progress_cb"] = _frame_progress_cb
                            stp["stop_cb"] = _stop_requested

                    recipe += sweep_recipe




                    ui_log(f"  > Saving: {stem_final}.csv")
                    ui_log(f"devices keys = {list((devices or {}).keys())}")
                    from app.steps.registry import list_steps
                    ui_log("Registered steps: " + ", ".join(list_steps()))

                    if st.session_state.get("_stop_now", False):
                        ui_log("🛑 STOP requested — ramping outputs to 0V...")
                        iv = (devices or {}).get("iv", None)

                        # IMPORTANT: do NOT swallow signature errors; use the compat ramp.
                        ramp_all_to_zero(iv, ramp_step=0.1, delay_s=0.02, log_fn=ui_log)

                        st.session_state["_stop_now"] = False
                        run_progress_text_ph.warning("🛑 Stopped. (Best-effort ramp to 0V issued.)")
                        return


                    iv = (devices or {}).get("iv", None)

                    try:
                        runner_singleton.run_recipe(
                            recipe,
                            devices,
                            to_header_list(wavelength_headers),
                            st.session_state.get("extra_scalar_fields_order", []),
                            str(out_dir),
                            stem_final,
                            progress_cb=ui_log,
                        )
                    except Exception as e:
                        # If your step raises StopRequested, treat as normal stop
                        if e.__class__.__name__ in ("StopRequested",):
                            ui_log("🛑 StopRequested caught — ramping outputs to 0V.")
                            ramp_all_to_zero(iv, ramp_step=0.1, delay_s=0.02, log_fn=ui_log)
                            st.session_state["_stop_now"] = False
                            run_progress_text_ph.warning("🛑 Stopped. (Ramp to 0V issued.)")
                            return

                        # Any other exception: still ramp to safe 0V, then re-raise so you see the traceback
                        ui_log(f"⛔ Run error: {e} — ramping outputs to 0V before raising.")
                        ramp_all_to_zero(iv, ramp_step=0.1, delay_s=0.02, log_fn=ui_log)
                        raise


                    done += 1
                    done_frames += acq_frames

                    # keep bar text consistent right after acquisition ends
                    pct = int(100 * (done_frames / total_frames_global))
                    pct = min(max(pct, 0), 100)
                    if supports_text:
                        bar.progress(pct, text=f"Frames {done_frames}/{total_frames_global}")
                    else:
                        bar.progress(pct)


                                    
                    if tree_live_ph is not None:
                        try:
                            st.session_state["tree_snapshot"].update(done=done)
                            snap = st.session_state["tree_snapshot"]
                            tree_live_ph.markdown(
                                build_run_status_tree_html(
                                    snap["final_sequence"],
                                    snap["df_batch"],
                                    done=snap["done"],
                                    total_acq=snap["total_acq"],
                                    current_seq_i=snap["current_seq_i"],
                                    current_label=snap["current_label"],
                                    current_rep_i=snap["current_rep_i"],
                                    max_seq_show=8,
                                    param_order=param_order
                                ),
                                unsafe_allow_html=True,
                            )
                        except Exception:
                            pass


        # finish
        if supports_text:
            bar.progress(100, text="✅ Run complete.")
        else:
            bar.progress(100)

        run_progress_text_ph.success("✅ Run complete.")
        ui_log("✅ All acquisitions complete.")

        if tree_live_ph is not None:
            st.session_state["tree_snapshot"].update(
                done=total_acq,
                current_seq_i=-1,
                current_label="",
                current_rep_i=0,
            )
            snap = st.session_state["tree_snapshot"]
            tree_live_ph.markdown(
                build_run_status_tree_html(
                    snap["final_sequence"],
                    snap["df_batch"],
                    done=snap["done"],
                    total_acq=snap["total_acq"],
                    current_seq_i=snap["current_seq_i"],
                    current_label=snap["current_label"],
                    current_rep_i=snap["current_rep_i"],
                    max_seq_show=8,
                    param_order=param_order
                ),
                unsafe_allow_html=True,
            )



    if st.session_state.get("_start_run"):
        st.session_state.pop("_start_run", None)

        # IMPORTANT: tells main_ui.py "do not rebuild IV right now"
        st.session_state["_run_active"] = True
        try:
            run_and_log(tree_live_ph)
        finally:
            st.session_state["_run_active"] = False
            st.session_state["_stop_now"] = False

        

