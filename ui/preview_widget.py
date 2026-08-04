# ui/preview_widget.py
# ──────────────────────────────────────────────────────────────────────────────
# Reusable run-plan tree widget for the PySide6 presets workflow.
#
# It renders the planned acquisition sequence directly in Qt so operators can
# track pending, active, and completed runs from the desktop UI.
#
# Three visual states:
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

from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QModelIndex, QSize, Qt
from PySide6.QtGui import QColor, QBrush, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)
from utils.filename_builder import build_condition_display_label
from utils.when_condition import evaluate_when_expression

try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False


# ── Colour palette (matches Streamlit tree) ────────────────────────────────────
_CLR_DONE_FG  = QColor("#166534")
_CLR_DONE_BG  = QColor("#DCFCE7")
_CLR_TODO_FG  = QColor("#4B5563")
_CLR_NOW_FG   = QColor("#111827")
_CLR_NOW_BG   = QColor("#fef3c7")   # amber-100
_CLR_HDR_FG   = QColor("#8B949E")
_CLR_ROW_FG   = QColor("#1D4ED8")
_CLR_ROW_BG   = QColor("#DBEAFE")
_CLR_LOOP_FG  = QColor("#6D28D9")
_CLR_LOOP_BG  = QColor("#EDE9FE")
_CLR_FAILED_FG = QColor("#991B1B")
_CLR_FAILED_BG = QColor("#FEE2E2")
_CLR_STOPPED_FG = QColor("#92400E")
_CLR_STOPPED_BG = QColor("#FEF3C7")

_NODE_ID_ROLE = int(Qt.ItemDataRole.UserRole)
_NODE_KIND_ROLE = _NODE_ID_ROLE + 1
_BASE_DETAIL_ROLE = _NODE_KIND_ROLE + 1


def _brush(c: QColor) -> QBrush:
    return QBrush(c)


def _entity_icon(kind: str) -> QIcon:
    color = _CLR_ROW_FG if kind == "batch" else _CLR_LOOP_FG
    pixmap = QPixmap(10, 10)
    pixmap.fill(color)
    return QIcon(pixmap)


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
    return evaluate_when_expression(when_str, ctx)


def _outer_ctx_alias(ctx: dict) -> dict:
    """Return ctx with common key aliases for 'When' evaluation."""
    out = dict(ctx)
    for k, v in list(ctx.items()):
        simple = k.split("(")[0].strip().replace(" ", "_")
        if simple not in out:
            out[simple] = v
    return out


def _fmt_sweep_range(r: dict) -> str:
    """Compact sweep-range string for a batch row, e.g. 'Vbg:-10→10  Vtg:0  Vbias:-0.5→0.5  20pts'."""
    parts = []
    for axis, start_key, stop_key in (
        ("Vbg",   "Vbg_start",   "Vbg_stop"),
        ("Vtg",   "Vtg_start",   "Vtg_stop"),
        ("Vbias", "Vbias_start", "Vbias_stop"),
    ):
        try:
            sv = r.get(start_key)
            ev = r.get(stop_key)
            if sv is None or str(sv).strip() in ("", "nan"):
                continue
            s = float(sv)
            e = float(ev) if ev is not None and str(ev).strip() not in ("", "nan") else s
            if axis == "Vbias" and abs(s) < 1e-12 and abs(e) < 1e-12:
                continue   # skip zero-bias (usually unused)
            if abs(s - e) < 1e-12:
                parts.append(f"{axis}:{s:g}")
            else:
                parts.append(f"{axis}:{s:g}→{e:g}")
        except Exception:
            pass
    try:
        frames = int(r.get("frames", 1) or 1)
        parts.append(f"{frames}pt{'s' if frames != 1 else ''}")
    except Exception:
        pass
    return "  ".join(parts)


def _fmt_ctx(ctx: dict, param_order: Optional[List[str]] = None) -> str:
    default_keys = ["Center Wavelength (nm)", "Exposure Time (ms)", "Stage Position"]
    keys = param_order or default_keys
    aliases = {
        "Center Wavelength (nm)": "CW",
        "Exposure Time (ms)": "Exp",
        "Accumulations (EPF)": "EPF",
        "Rotation1 Angle (deg)": "R1",
        "Rotation2 Angle (deg)": "R2",
        "Stage Position": "Stage",
    }
    parts = []
    for k in keys:
        v = ctx.get(k)
        if v is None or str(v).strip() == "":
            continue
        k0 = aliases.get(k, k.split("(")[0].strip())
        try:
            value = f"{float(v):g}"
        except Exception:
            value = f"{v}"
        parts.append(f"{k0}={value}")
    return ", ".join(parts) if parts else "(no loop vars)"


def _fmt_value(value, *, decimals: int = 4) -> str:
    if value is None or str(value).strip().lower() in ("", "nan", "none"):
        return "—"
    try:
        return f"{float(value):.{decimals}f}"
    except Exception:
        return str(value)


def _condition_name(row: dict, row_i: int) -> str:
    return build_condition_display_label(
        str(row.get("condition_label", "")),
        row.get("Vbias_start"),
        row.get("Vbias_stop"),
    ) or f"Batch row {row_i + 1}"


def _make_plain_item(
    text: str,
    parent: Optional[QTreeWidgetItem] = None,
    *,
    bold: bool = False,
    color: Optional[QColor] = None,
    node_id: str = "",
    kind: str = "",
) -> QTreeWidgetItem:
    item = QTreeWidgetItem([text]) if parent is None else QTreeWidgetItem(parent, [text])
    item.setToolTip(0, text)
    if bold:
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
    if color is not None:
        item.setForeground(0, _brush(color))
    if node_id:
        item.setData(0, _NODE_ID_ROLE, node_id)
    if kind:
        item.setData(0, _NODE_KIND_ROLE, kind)
    if kind in ("batch", "loop"):
        item.setIcon(0, _entity_icon(kind))
    return item


def _make_skipped_item(text: str, parent: QTreeWidgetItem) -> QTreeWidgetItem:
    item = _make_plain_item(f"⊘ {text}", parent, color=_CLR_DONE_FG)
    font = item.font(0)
    font.setItalic(True)
    item.setFont(0, font)
    return item


def _make_status_item(
    text: str,
    state: str,
    parent: QTreeWidgetItem,
    *,
    kind: str,
    node_id: str,
) -> QTreeWidgetItem:
    prefix = {
        "done": "✓ ",
        "now": "▶ ",
        "todo": "○ ",
        "failed": "✕ ",
        "stopped": "■ ",
    }.get(state, "○ ")
    suffix = {
        "now": " · RUNNING",
        "failed": " · FAILED",
        "stopped": " · STOPPED",
    }.get(state, "")
    item = _make_plain_item(
        prefix + text + suffix,
        parent,
        bold=state in ("now", "failed", "stopped"),
        node_id=node_id,
        kind=kind,
    )
    if state == "done":
        foreground, background = _CLR_DONE_FG, _CLR_DONE_BG
    elif state == "now":
        foreground, background = _CLR_NOW_FG, _CLR_NOW_BG
    elif state == "failed":
        foreground, background = _CLR_FAILED_FG, _CLR_FAILED_BG
    elif state == "stopped":
        foreground, background = _CLR_STOPPED_FG, _CLR_STOPPED_BG
    elif kind == "batch":
        foreground, background = _CLR_ROW_FG, _CLR_ROW_BG
    elif kind == "loop":
        foreground, background = _CLR_LOOP_FG, _CLR_LOOP_BG
    else:
        foreground, background = _CLR_TODO_FG, QColor("#F3F4F6")
    item.setForeground(0, _brush(foreground))
    item.setBackground(0, _brush(background))
    return item


# ── Item factory ──────────────────────────────────────────────────────────────

def _make_item(
    text: str,
    state: str,          # "done" | "now" | "todo"
    parent: Optional[QTreeWidgetItem] = None,
) -> QTreeWidgetItem:
    prefix = {"done": "✓ ", "now": "▶ ", "todo": "• "}[state]
    item = QTreeWidgetItem([prefix + text]) if parent is None else QTreeWidgetItem(parent, [prefix + text])
    item.setToolTip(0, text)

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
        self.setColumnCount(4)
        self.setHeaderLabels(["Status", "Batch row", "Loop setting", "Measurement details"])
        self.setHeaderHidden(False)
        header = self.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(44)
        self.setAnimated(False)
        self.setIndentation(12)
        self.setMinimumHeight(120)
        self.setWordWrap(True)
        self.setUniformRowHeights(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._semantic_tree_built = False
        self._flat_signature = None
        self._flat_steps: Dict[int, Dict[str, object]] = {}
        self._flat_groups: List[Dict[str, object]] = []
        self._flat_root: Optional[QTreeWidgetItem] = None
        self._fit_columns_to_viewport()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_columns_to_viewport()

    def _fit_columns_to_viewport(self) -> None:
        """Keep all four meanings visible inside the narrow preview pane."""
        width = max(self.viewport().width(), 320)
        status_width = 46
        loop_width = 88
        batch_width = max(112, min(148, int(width * 0.27)))
        self.setColumnWidth(0, status_width)
        self.setColumnWidth(1, batch_width)
        self.setColumnWidth(2, loop_width)

    def _expanded_node_ids(self) -> set[str]:
        expanded: set[str] = set()

        def visit(item: QTreeWidgetItem) -> None:
            node_id = item.data(0, _NODE_ID_ROLE)
            if node_id and item.isExpanded():
                expanded.add(str(node_id))
            for child_i in range(item.childCount()):
                visit(item.child(child_i))

        for top_i in range(self.topLevelItemCount()):
            visit(self.topLevelItem(top_i))
        return expanded

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
        current_frame_i: int = 0,
        current_frame_total: int = 0,
        max_seq_show: int = 12,
        param_order: Optional[List[str]] = None,
        acquisition_schedule: Optional[Sequence[Dict[str, object]]] = None,
        acquisition_grouping: str = "loop_first",
        loop_definition=None,
        loop_mode: str = "",
        run_outcome: str = "idle",
    ) -> None:
        if acquisition_schedule is not None:
            expanded_node_ids = (
                self._expanded_node_ids() if self._semantic_tree_built else None
            )
            self._update_flat_schedule(
                list(acquisition_schedule),
                final_sequence=final_sequence,
                df_batch=df_batch,
                done=done,
                total_acq=total_acq,
                current_step_i=current_seq_i,
                current_rep_i=current_rep_i,
                current_frame_i=current_frame_i,
                current_frame_total=current_frame_total,
                param_order=param_order,
                acquisition_grouping=acquisition_grouping,
                loop_definition=loop_definition,
                loop_mode=loop_mode,
                expanded_node_ids=expanded_node_ids,
                run_outcome=run_outcome,
            )
            self._semantic_tree_built = True
            return

        self.clear()
        self._semantic_tree_built = False
        self._flat_signature = None
        self._flat_steps.clear()
        self._flat_groups.clear()
        self._flat_root = None

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

        # Absolute index of currently running acquisition. Prefer the file
        # counter because condition labels are allowed to repeat.
        current_abs = -1
        if 0 <= current_seq_i < len(prefix_sums) - 1 and prefix_sums[current_seq_i] <= done < prefix_sums[current_seq_i + 1]:
            current_abs = int(done)
        elif 0 <= current_seq_i < len(seq_plans):
            current_abs = prefix_sums[current_seq_i]
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
                cond_name = build_condition_display_label(
                    lab,
                    r.get("Vbias_start") if isinstance(r, dict) else None,
                    r.get("Vbias_stop") if isinstance(r, dict) else None,
                )
                cond_name = cond_name or f"cond{row_i}"
                rng = _fmt_sweep_range(r) if isinstance(r, dict) else ""

                for r_i in range(reps):
                    if acq_abs < done:
                        acq_state = "done"
                    elif acq_abs == current_abs and done < total_acq:
                        acq_state = "now"
                    else:
                        acq_state = "todo"

                    rep_tag = f"({r_i + 1}/{reps})"
                    leaf_text = f"{cond_name}  {rep_tag}" if not rng else f"{cond_name}  {rep_tag}  [{rng}]"
                    if (
                        acq_state == "now"
                        and current_frame_total
                        and int(current_frame_total) > 1
                    ):
                        frame_i = max(0, min(int(current_frame_i), int(current_frame_total)))
                        leaf_text = f"{leaf_text} - frame {frame_i}/{int(current_frame_total)}"
                    _make_item(leaf_text, acq_state, seq_item)
                    acq_abs += 1

        if end < n_seq:
            more = QTreeWidgetItem(hdr, [f"  … +{n_seq - end} more sequences"])
            more.setForeground(0, _brush(_CLR_TODO_FG))

        self.expandAll()

    def _update_flat_schedule(
        self,
        schedule: List[Dict[str, object]],
        *,
        final_sequence: List[dict],
        df_batch,
        done: int,
        total_acq: int,
        current_step_i: int,
        current_rep_i: int,
        current_frame_i: int,
        current_frame_total: int,
        param_order: Optional[List[str]],
        acquisition_grouping: str,
        loop_definition,
        loop_mode: str,
        expanded_node_ids: Optional[set[str]],
        run_outcome: str,
    ) -> None:
        """Render a compact checklist with row and loop context side by side."""
        if _HAS_PANDAS and isinstance(df_batch, pd.DataFrame):
            frame = df_batch.copy()
            if "Run" in frame.columns:
                frame["Run"] = frame["Run"].map(_to_bool)
                frame = frame[frame["Run"]].reset_index(drop=True)
            batch_rows = [frame.iloc[i].to_dict() for i in range(len(frame))]
        else:
            batch_rows = [
                dict(row)
                for row in (df_batch or [])
                if _to_bool(dict(row).get("Run", True))
            ]

        definition_rows: List[dict] = []
        if _HAS_PANDAS and isinstance(loop_definition, pd.DataFrame):
            definition_rows = [
                loop_definition.iloc[i].to_dict()
                for i in range(len(loop_definition))
                if _to_bool(loop_definition.iloc[i].get("Enable", False))
            ]
        elif isinstance(loop_definition, (list, tuple)):
            definition_rows = [
                dict(row)
                for row in loop_definition
                if _to_bool(dict(row).get("Enable", False))
            ]

        counts = [
            max(int(dict(task.get("row", {})).get("repeat", 1) or 1), 1)
            for task in schedule
        ]
        prefix_sums = [0]
        for count in counts:
            prefix_sums.append(prefix_sums[-1] + count)

        task_map = {
            (int(task.get("seq_i", -1)), int(task.get("row_i", -1))): task_i
            for task_i, task in enumerate(schedule)
        }
        pairs = (
            (
                (seq_i, row_i)
                for row_i in range(len(batch_rows))
                for seq_i in range(len(final_sequence))
            )
            if acquisition_grouping == "batch_first"
            else (
                (seq_i, row_i)
                for seq_i in range(len(final_sequence))
                for row_i in range(len(batch_rows))
            )
        )
        combinations: List[Dict[str, object]] = []
        for seq_i, row_i in pairs:
            ctx = dict(final_sequence[seq_i])
            row = dict(batch_rows[row_i])
            when_ok = _when_ok(row.get("When", ""), _outer_ctx_alias(ctx))
            task_i = task_map.get((seq_i, row_i)) if when_ok else None
            combinations.append(
                {
                    "seq_i": seq_i,
                    "row_i": row_i,
                    "ctx": ctx,
                    "row": row,
                    "task_i": task_i,
                    "applicable": task_i is not None,
                }
            )

        signature = repr(
            (
                acquisition_grouping,
                loop_mode,
                final_sequence,
                batch_rows,
                definition_rows,
                [
                    (
                        task.get("seq_i"),
                        task.get("row_i"),
                        dict(task.get("row", {})).get("repeat", 1),
                    )
                    for task in schedule
                ],
            )
        )

        def set_bold(item: QTreeWidgetItem, column: int = 0) -> None:
            font = item.font(column)
            font.setBold(True)
            item.setFont(column, font)

        def set_entity_style(item: QTreeWidgetItem, column: int, kind: str) -> None:
            fg = _CLR_ROW_FG if kind == "batch" else _CLR_LOOP_FG
            bg = _CLR_ROW_BG if kind == "batch" else _CLR_LOOP_BG
            item.setForeground(column, _brush(fg))
            item.setBackground(column, _brush(bg))
            set_bold(item, column)

        def sweep_text(row: dict) -> str:
            try:
                frames = max(int(row.get("frames", 1) or 1), 1)
            except Exception:
                frames = 1
            parts = []
            for axis in ("Vtg", "Vbg", "Vbias"):
                start = row.get(f"{axis}_start")
                stop = row.get(f"{axis}_stop")
                if start is None or str(start).strip().lower() in ("", "nan", "none"):
                    if axis == "Vbias":
                        parts.append("Vbias not used")
                    continue
                text = f"{axis} {_fmt_value(start)} → {_fmt_value(stop)} V"
                try:
                    delta = abs(float(stop) - float(start)) / max(frames - 1, 1)
                    text += f" (Δ {_fmt_value(delta)} V)"
                except Exception:
                    pass
                parts.append(text)
            return "  |  ".join(parts)

        def compact_sweep_text(row: dict) -> str:
            parts = []
            for axis in ("Vtg", "Vbg", "Vbias"):
                start = row.get(f"{axis}_start")
                stop = row.get(f"{axis}_stop")
                if start is None or str(start).strip().lower() in ("", "nan", "none"):
                    continue
                try:
                    start_text = f"{float(start):g}"
                except Exception:
                    start_text = str(start)
                try:
                    stop_text = f"{float(stop):g}"
                except Exception:
                    stop_text = str(stop)
                parts.append(f"{axis} {start_text}→{stop_text} V")
            return " · ".join(parts) or "No gate sweep"

        def base_detail(ctx: dict, row: dict) -> str:
            try:
                frames = max(int(row.get("frames", 1) or 1), 1)
            except Exception:
                frames = 1
            try:
                repeats = max(int(row.get("repeat", 1) or 1), 1)
            except Exception:
                repeats = 1
            power = "Yes" if _to_bool(row.get("MeasurePower", False)) else "No"
            return (
                f"{compact_sweep_text(row)}\n"
                f"Loop: {_fmt_ctx(ctx, param_order)}\n"
                f"{frames} frames · Repeat {repeats} · Power {power}"
            )

        def full_detail(ctx: dict, row: dict) -> str:
            try:
                frames = max(int(row.get("frames", 1) or 1), 1)
            except Exception:
                frames = 1
            try:
                repeats = max(int(row.get("repeat", 1) or 1), 1)
            except Exception:
                repeats = 1
            power = "Yes" if _to_bool(row.get("MeasurePower", False)) else "No"
            return (
                f"Loop setting: {_fmt_ctx(ctx, param_order)}\n"
                f"{sweep_text(row)}\n"
                f"Frames: {frames} · Repeat: {repeats} · Measure power: {power}"
            )

        if signature != self._flat_signature or self._flat_root is None:
            self.clear()
            self._flat_steps.clear()
            self._flat_groups.clear()

            skipped_count = sum(not bool(entry["applicable"]) for entry in combinations)
            root = QTreeWidgetItem(["", "", "", ""])
            root.setData(0, _NODE_ID_ROLE, "run")
            set_bold(root)
            self.addTopLevelItem(root)
            root.setExpanded(True)
            self.setFirstColumnSpanned(
                self.indexOfTopLevelItem(root), QModelIndex(), True
            )
            self._flat_root = root

            order_text = (
                "Batch row → Loop settings"
                if acquisition_grouping == "batch_first"
                else "Loop setting → Batch rows"
            )
            execution = QTreeWidgetItem(
                root,
                [
                    f"Execution order · {order_text} · "
                    f"{skipped_count} skipped by When",
                    "",
                    "",
                    "",
                ],
            )
            execution.setData(0, _NODE_ID_ROLE, "execution")
            set_bold(execution)
            execution.setForeground(0, _brush(QColor("#111827")))
            execution.setExpanded(True)
            self.setFirstColumnSpanned(
                root.indexOfChild(execution), self.indexFromItem(root), True
            )

            outer_key = "row_i" if acquisition_grouping == "batch_first" else "seq_i"
            outer_count = len(batch_rows) if outer_key == "row_i" else len(final_sequence)
            for outer_i in range(outer_count):
                entries = [entry for entry in combinations if int(entry[outer_key]) == outer_i]
                runnable = [entry for entry in entries if entry["applicable"]]
                group = QTreeWidgetItem(["", "", "", ""])
                group.setData(0, _NODE_ID_ROLE, f"execution:{outer_key}:{outer_i}")
                if acquisition_grouping == "batch_first":
                    row = batch_rows[outer_i]
                    group_kind = "batch"
                    group_title = (
                        f"[ROW {outer_i + 1}] {_condition_name(row, outer_i)}"
                    )
                else:
                    group_kind = "loop"
                    group_title = (
                        f"[LOOP {outer_i + 1}] "
                        f"{_fmt_ctx(final_sequence[outer_i], param_order)}"
                    )
                group_title += (
                    f" · {len(runnable)} runnable step(s)"
                    f" · {len(entries) - len(runnable)} skipped"
                )
                group.setText(0, f"○  {group_title}")
                group.setToolTip(0, group_title)
                group.setSizeHint(0, QSize(360, 36))
                group.setForeground(
                    0, _brush(_CLR_ROW_FG if group_kind == "batch" else _CLR_LOOP_FG)
                )
                group.setBackground(
                    0, _brush(_CLR_ROW_BG if group_kind == "batch" else _CLR_LOOP_BG)
                )
                set_bold(group)
                execution.addChild(group)
                self.setFirstColumnSpanned(
                    execution.indexOfChild(group), self.indexFromItem(execution), True
                )
                group.setExpanded(True)
                group_record = {
                    "item": group,
                    "task_indices": [int(entry["task_i"]) for entry in runnable],
                    "title": group_title,
                    "kind": group_kind,
                }
                self._flat_groups.append(group_record)

                for entry in entries:
                    seq_i = int(entry["seq_i"])
                    row_i = int(entry["row_i"])
                    ctx = dict(entry["ctx"])
                    row = dict(entry["row"])
                    row_label = f"[ROW {row_i + 1}] {_condition_name(row, row_i)}"
                    loop_label = f"[LOOP {seq_i + 1}]"
                    detail = base_detail(ctx, row)
                    if not entry["applicable"]:
                        when_text = str(row.get("When", "")).strip() or "Always"
                        item = QTreeWidgetItem(
                            ["⊘", row_label, loop_label, f"Skipped: When ({when_text}) is False\n{detail}"],
                        )
                        item.setForeground(0, _brush(_CLR_TODO_FG))
                        item.setForeground(3, _brush(_CLR_TODO_FG))
                        font = item.font(3)
                        font.setItalic(True)
                        item.setFont(3, font)
                    else:
                        task_i = int(entry["task_i"])
                        item = QTreeWidgetItem(
                            [f"○ {task_i + 1}", row_label, loop_label, detail]
                        )
                        item.setData(0, _NODE_ID_ROLE, f"step:{task_i}")
                        item.setData(3, _BASE_DETAIL_ROLE, detail)
                        self._flat_steps[task_i] = {
                            "item": item,
                            "repeats": counts[task_i],
                            "prefix_start": prefix_sums[task_i],
                            "prefix_end": prefix_sums[task_i + 1],
                        }
                    set_entity_style(item, 1, "batch")
                    set_entity_style(item, 2, "loop")
                    item.setSizeHint(3, QSize(240, 84))
                    item.setTextAlignment(
                        0,
                        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                    )
                    for column in (1, 2, 3):
                        item.setTextAlignment(
                            column,
                            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                        )
                    item.setToolTip(1, row_label)
                    item.setToolTip(2, f"{loop_label} {_fmt_ctx(ctx, param_order)}")
                    item.setToolTip(3, full_detail(ctx, row))
                    group.addChild(item)

            if loop_mode == "Zip":
                resolution_text = f"Zip · {len(final_sequence)} zipped setting(s)"
            elif loop_mode == "Synchronize":
                factor_counts = []
                for row in definition_rows:
                    parameter = str(row.get("Parameter", ""))
                    values = []
                    for ctx in final_sequence:
                        value = ctx.get(parameter)
                        if value is not None and value not in values:
                            values.append(value)
                    if values:
                        factor_counts.append(len(values))
                factor_text = " × ".join(str(value) for value in factor_counts)
                resolution_text = "Cartesian"
                if factor_text:
                    resolution_text += f" · {factor_text} = {len(final_sequence)} setting(s)"
                else:
                    resolution_text += f" · {len(final_sequence)} setting(s)"
            elif loop_mode == "Customized":
                resolution_text = f"Customized zipped groups · {len(final_sequence)} setting(s)"
            else:
                resolution_text = f"{len(final_sequence)} resolved setting(s)"

            loop_inputs = QTreeWidgetItem(
                root, ["Plan inputs", "", "Loop variables", resolution_text]
            )
            loop_inputs.setData(0, _NODE_ID_ROLE, "loop-definitions")
            set_bold(loop_inputs)
            set_entity_style(loop_inputs, 2, "loop")
            loop_inputs.setExpanded(
                bool(expanded_node_ids and "loop-definitions" in expanded_node_ids)
            )
            for definition_i, row in enumerate(definition_rows):
                parameter = str(row.get("Parameter", "Loop parameter"))
                values = str(row.get("Values", "")).strip() or "—"
                suffix = ""
                if loop_mode == "Customized":
                    suffix = f" · Group {row.get('Group', 1)}"
                elif loop_mode == "Zip":
                    suffix = " · zipped"
                child = QTreeWidgetItem(
                    ["", "", parameter, f"Input: {values}{suffix}"]
                )
                set_entity_style(child, 2, "loop")
                loop_inputs.addChild(child)

            batch_inputs = QTreeWidgetItem(
                root,
                ["Plan inputs", f"Batch sweep definitions ({len(batch_rows)})", "", ""],
            )
            batch_inputs.setData(0, _NODE_ID_ROLE, "batch-definitions")
            set_bold(batch_inputs)
            set_entity_style(batch_inputs, 1, "batch")
            batch_inputs.setExpanded(
                bool(expanded_node_ids and "batch-definitions" in expanded_node_ids)
            )
            for row_i, row in enumerate(batch_rows):
                child = QTreeWidgetItem(
                    ["", f"[ROW {row_i + 1}] {_condition_name(row, row_i)}", "", sweep_text(row)]
                )
                set_entity_style(child, 1, "batch")
                batch_inputs.addChild(child)

            self._flat_signature = signature

        self._update_flat_status(
            done=done,
            total_acq=total_acq,
            current_step_i=current_step_i,
            current_rep_i=current_rep_i,
            current_frame_i=current_frame_i,
            current_frame_total=current_frame_total,
            run_outcome=run_outcome,
        )

    def _update_flat_status(
        self,
        *,
        done: int,
        total_acq: int,
        current_step_i: int,
        current_rep_i: int,
        current_frame_i: int,
        current_frame_total: int,
        run_outcome: str,
    ) -> None:
        """Update status cells without rebuilding the operator's checklist."""
        outcome_text = {
            "running": "Running",
            "completed": "Complete",
            "stopped": "Stopped",
            "failed": "Failed",
        }.get(run_outcome, "Ready")
        if self._flat_root is not None:
            skipped = sum(
                1
                for group in self._flat_groups
                for child_i in range(group["item"].childCount())
                if group["item"].child(child_i).text(0) == "⊘"
            )
            self._flat_root.setText(
                0,
                f"{outcome_text} · {done}/{total_acq} files · "
                f"{len(self._flat_steps)} runnable step(s) · {skipped} skipped",
            )

        active_item: Optional[QTreeWidgetItem] = None
        state_colors = {
            "done": (_CLR_DONE_FG, _CLR_DONE_BG),
            "now": (_CLR_NOW_FG, _CLR_NOW_BG),
            "failed": (_CLR_FAILED_FG, _CLR_FAILED_BG),
            "stopped": (_CLR_STOPPED_FG, _CLR_STOPPED_BG),
            "todo": (_CLR_TODO_FG, QColor("#F9FAFB")),
        }
        symbols = {
            "done": "✓",
            "now": "▶",
            "failed": "✕",
            "stopped": "■",
            "todo": "○",
        }

        def step_state(task_i: int, start: int, end: int) -> str:
            if done >= end:
                return "done"
            if task_i == current_step_i and run_outcome == "failed":
                return "failed"
            if task_i == current_step_i and run_outcome == "stopped":
                return "stopped"
            if task_i == current_step_i and run_outcome == "running":
                return "now"
            if run_outcome == "running" and start <= done < end:
                return "now"
            return "todo"

        states: Dict[int, str] = {}
        for task_i, record in self._flat_steps.items():
            item = record["item"]
            start = int(record["prefix_start"])
            end = int(record["prefix_end"])
            repeats = int(record["repeats"])
            state = step_state(task_i, start, end)
            states[task_i] = state
            suffix = {
                "now": "  RUNNING",
                "failed": "  FAILED",
                "stopped": "  STOPPED",
            }.get(state, "")
            item.setText(0, f"{symbols[state]} {task_i + 1}{suffix}")
            foreground, background = state_colors[state]
            item.setForeground(0, _brush(foreground))
            item.setBackground(0, _brush(background))
            item.setForeground(3, _brush(foreground if state != "todo" else _CLR_TODO_FG))
            item.setBackground(3, _brush(background))
            font = item.font(0)
            font.setBold(state in ("now", "failed", "stopped"))
            item.setFont(0, font)

            tokens = []
            for rep_i in range(repeats):
                absolute_i = start + rep_i
                if absolute_i < done:
                    token = f"✓{rep_i + 1}"
                elif state == "now" and rep_i == max(0, min(current_rep_i, repeats - 1)):
                    token = f"▶{rep_i + 1}"
                else:
                    token = f"○{rep_i + 1}"
                tokens.append(token)
            progress = f"Files: {' '.join(tokens)}"
            if state == "now" and current_frame_total > 1:
                frame_i = max(0, min(current_frame_i, current_frame_total))
                progress += f" · frame {frame_i}/{current_frame_total}"
            base = str(item.data(3, _BASE_DETAIL_ROLE) or "")
            item.setText(3, f"{base} · {progress}")
            if state in ("now", "failed", "stopped"):
                active_item = item

        for group in self._flat_groups:
            item = group["item"]
            task_indices = list(group["task_indices"])
            group_states = [states[task_i] for task_i in task_indices]
            if group_states and all(state == "done" for state in group_states):
                state = "done"
            elif "failed" in group_states:
                state = "failed"
            elif "stopped" in group_states:
                state = "stopped"
            elif "now" in group_states:
                state = "now"
            else:
                state = "todo"
            item.setText(0, f"{symbols[state]}  {group['title']}")
            if state == "todo":
                foreground = (
                    _CLR_ROW_FG if group["kind"] == "batch" else _CLR_LOOP_FG
                )
                background = (
                    _CLR_ROW_BG if group["kind"] == "batch" else _CLR_LOOP_BG
                )
            else:
                foreground, background = state_colors[state]
            item.setForeground(0, _brush(foreground))
            item.setBackground(0, _brush(background))

        if active_item is not None:
            self.scrollToItem(active_item, QAbstractItemView.ScrollHint.PositionAtCenter)

    def clear_plan(self) -> None:
        """Reset to empty state (before a run starts)."""
        self.clear()
        self._semantic_tree_built = False
        self._flat_signature = None
        self._flat_steps.clear()
        self._flat_groups.clear()
        self._flat_root = None
