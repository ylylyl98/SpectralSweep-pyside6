# ui/preview_widget.py
# ──────────────────────────────────────────────────────────────────────────────
# Reusable run-plan tree widget.
#
# Mirrors build_run_status_tree_html() from app/ui_streamlit/views/presets.py
# but rendered as a QTreeWidget instead of an HTML string.
#
# Three visual states (match Streamlit colours):
#   done  → gray, ✓ prefix
#   now   → amber background, ▶ prefix, bold
#   todo  → light text, • prefix
#
# Usage:
#   tree = RunPlanTree()
#   tree.update_plan(
#       final_sequence=[{"Center Wavelength (nm)": 860, ...}, ...],
#       df_batch=df,       # pandas DataFrame with batch rows
#       done=5,
#       total_acq=20,
#       current_seq_i=1,
#       current_label="ZB_VB0",
#       current_rep_i=0,
#   )
#
# Call update_plan() from the sweep worker's progress callback.
# Safe to call from a non-GUI thread if wrapped in a signal.
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QBrush, QFont
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QWidget

try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False


# ── Colour palette (matches Streamlit tree) ────────────────────────────────────
_CLR_DONE_FG  = QColor("#6E7681")
_CLR_TODO_FG  = QColor("#8B949E")
_CLR_NOW_FG   = QColor("#111827")
_CLR_NOW_BG   = QColor("#fef3c7")   # amber-100
_CLR_HDR_FG   = QColor("#8B949E")


def _brush(c: QColor) -> QBrush:
    return QBrush(c)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "✓", "x")
    try:
        return bool(int(v))
    except Exception:
        return False


def _when_ok(when_str: str, ctx: dict) -> bool:
    """
    Evaluate the 'When' condition string against a sequence context dict.
    Returns True if the condition is empty or passes.
    Supports simple expressions like "Exposure Time (ms) == 2000".
    """
    w = str(when_str).strip()
    if not w or w.lower() in ("always", "all", ""):
        return True
    try:
        # Build a safe local namespace from the ctx dict
        ns = {re.sub(r"[^a-zA-Z0-9_]", "_", k): v for k, v in ctx.items()}
        return bool(eval(w, {"__builtins__": {}}, ns))  # noqa: S307
    except Exception:
        return True   # unknown → include


def _outer_ctx_alias(ctx: dict) -> dict:
    """Return ctx with common key aliases for 'When' evaluation."""
    out = dict(ctx)
    for k, v in list(ctx.items()):
        simple = k.split("(")[0].strip().replace(" ", "_")
        if simple not in out:
            out[simple] = v
    return out


def _vbias_block(row) -> str:
    """Derive a vbias filename component from a batch row."""
    try:
        vs = float(row.get("Vbias_start", 0) or 0)
        ve = float(row.get("Vbias_stop",  0) or 0)
        if vs != 0 or ve != 0:
            return f"Vb{vs:+.2g}to{ve:+.2g}"
    except Exception:
        pass
    return ""


def _fmt_ctx(ctx: dict, param_order: Optional[List[str]] = None) -> str:
    default_keys = ["Center Wavelength (nm)", "Exposure Time (ms)", "Stage Position"]
    keys = param_order or default_keys
    parts = []
    for k in keys:
        v = ctx.get(k)
        if v is None or str(v).strip() == "":
            continue
        k0 = k.split("(")[0].strip()
        try:
            parts.append(f"{k0}={float(v):g}")
        except Exception:
            parts.append(f"{k0}={v}")
    return ", ".join(parts) if parts else "(no loop vars)"


# ── Item factory ──────────────────────────────────────────────────────────────

def _make_item(
    text: str,
    state: str,          # "done" | "now" | "todo"
    parent: Optional[QTreeWidgetItem] = None,
) -> QTreeWidgetItem:
    prefix = {"done": "✓ ", "now": "▶ ", "todo": "• "}[state]
    item = QTreeWidgetItem([prefix + text]) if parent is None else QTreeWidgetItem(parent, [prefix + text])

    if state == "done":
        item.setForeground(0, _brush(_CLR_DONE_FG))
    elif state == "now":
        item.setForeground(0, _brush(_CLR_NOW_FG))
        item.setBackground(0, _brush(_CLR_NOW_BG))
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
    else:
        item.setForeground(0, _brush(_CLR_TODO_FG))

    return item


# ── Main widget ───────────────────────────────────────────────────────────────

class RunPlanTree(QTreeWidget):
    """
    Tree view of a run plan.  Call update_plan() to refresh.

    Parameters mirror build_run_status_tree_html() from the Streamlit app.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setAnimated(False)
        self.setIndentation(16)
        self.setMinimumHeight(120)

    # ── public API ────────────────────────────────────────────────────────────

    def update_plan(
        self,
        final_sequence: List[dict],
        df_batch,                        # pandas DataFrame or list-of-dicts
        *,
        done: int = 0,
        total_acq: int = 0,
        current_seq_i: int = -1,
        current_label: str = "",
        current_rep_i: int = 0,
        max_seq_show: int = 12,
        param_order: Optional[List[str]] = None,
    ) -> None:
        self.clear()

        # ── normalise df_batch ────────────────────────────────────────────────
        if _HAS_PANDAS and isinstance(df_batch, pd.DataFrame):
            dfb = df_batch.copy()
            if "Run" in dfb.columns:
                dfb["Run"] = dfb["Run"].map(_to_bool)
                dfb = dfb[dfb["Run"]].reset_index(drop=True)
        else:
            # list-of-dicts fallback
            rows = [r for r in (df_batch or []) if _to_bool(r.get("Run", True))]
            dfb = rows   # we'll treat as list of dicts below

        def _row(i):
            if _HAS_PANDAS and isinstance(dfb, pd.DataFrame):
                return dfb.iloc[i].to_dict()
            return dfb[i] if i < len(dfb) else {}

        def _row_count():
            if _HAS_PANDAS and isinstance(dfb, pd.DataFrame):
                return len(dfb)
            return len(dfb)

        # ── per-sequence plans ────────────────────────────────────────────────
        seq_plans: List[List[Tuple[str, int, int]]] = []
        acq_counts: List[int] = []

        for ctx in final_sequence:
            outer = _outer_ctx_alias(ctx)
            plan: List[Tuple[str, int, int]] = []
            n = 0
            for row_i in range(_row_count()):
                r = _row(row_i)
                if _when_ok(r.get("When", ""), outer):
                    lab = str(r.get("condition_label", f"cond{row_i}"))
                    reps = max(int(r.get("repeat", 1) or 1), 1)
                    plan.append((lab, reps, row_i))
                    n += reps
            seq_plans.append(plan)
            acq_counts.append(n)

        prefix_sums = [0]
        for n in acq_counts:
            prefix_sums.append(prefix_sums[-1] + n)

        # absolute index of currently running acquisition
        current_abs = prefix_sums[current_seq_i] if 0 <= current_seq_i < len(prefix_sums) - 1 else -1
        if 0 <= current_seq_i < len(seq_plans):
            off = 0
            for lab, reps, _ in seq_plans[current_seq_i]:
                if lab == current_label:
                    current_abs = prefix_sums[current_seq_i] + off + min(max(current_rep_i, 0), reps - 1)
                    break
                off += reps

        # ── header item ───────────────────────────────────────────────────────
        n_seq = len(final_sequence)
        hdr = QTreeWidgetItem([f"Run  {done}/{total_acq}  ({n_seq} sequence{'s' if n_seq != 1 else ''})"])
        hdr.setForeground(0, _brush(_CLR_HDR_FG))
        font = hdr.font(0)
        font.setBold(True)
        hdr.setFont(0, font)
        self.addTopLevelItem(hdr)
        hdr.setExpanded(True)

        if n_seq == 0:
            _make_item("No sequences (loop disabled?)", "todo", hdr)
            return

        # ── windowed sequence range ───────────────────────────────────────────
        if n_seq <= max_seq_show:
            start, end = 0, n_seq
        else:
            c = max(current_seq_i, 0)
            start = max(0, min(c - max_seq_show // 2, n_seq - max_seq_show))
            end = min(n_seq, start + max_seq_show)

        for seq_i in range(start, end):
            ctx = final_sequence[seq_i]
            seq_text = f"Seq {seq_i + 1}: {_fmt_ctx(ctx, param_order)}"

            s0, s1 = prefix_sums[seq_i], prefix_sums[seq_i + 1]
            if done >= s1:
                seq_state = "done"
            elif done <= s0:
                seq_state = "todo"
            else:
                seq_state = "now"

            seq_item = _make_item(seq_text, seq_state, hdr)
            seq_item.setExpanded(seq_state in ("now", "todo"))

            plan = seq_plans[seq_i]
            acq_abs = s0

            for b_i, (lab, reps, row_i) in enumerate(plan):
                r = _row(row_i)
                vb = _vbias_block(r) if isinstance(r, dict) else ""
                cond_name = lab + (f"_{vb}" if vb else "")

                for r_i in range(reps):
                    if acq_abs < done:
                        acq_state = "done"
                    elif acq_abs == current_abs and done < total_acq:
                        acq_state = "now"
                    else:
                        acq_state = "todo"

                    leaf_text = f"{cond_name}  ({r_i + 1}/{reps})"
                    _make_item(leaf_text, acq_state, seq_item)
                    acq_abs += 1

        if end < n_seq:
            more = QTreeWidgetItem(hdr, [f"  … +{n_seq - end} more sequences"])
            more.setForeground(0, _brush(_CLR_TODO_FG))

        self.expandAll()

    def clear_plan(self) -> None:
        """Reset to empty state (before a run starts)."""
        self.clear()
