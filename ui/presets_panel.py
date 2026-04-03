# ui/presets_panel.py
# ──────────────────────────────────────────────────────────────────────────────
# Presets sweep panel.
#
# Loop table changes (vs original):
#   - "Parameter" column is now a QComboBox dropdown (not free text).
#   - "Level" renamed to "Group".
#   - Three loop modes selected via combobox above the table:
#       Synchronize  – each enabled row = its own loop level, nested from top
#                      to bottom (row 1 = outermost, row 2 = next inner, …).
#                      Result is a Cartesian product.
#       Zip          – all enabled rows are zipped together (must have equal
#                      number of values).
#       Customized   – user assigns a Group number per row.  Rows with the
#                      same Group are zipped; Groups are producted (same
#                      as the old Level system).
#   - Group column hidden in Synchronize and Zip modes; visible in Customized.
#
# Batch table changes:
#   - Column order: Run | When | MeasurePower | condition_label | repeat |
#     frames | Vbg_start | Vbg_stop | Vtg_start | Vtg_stop |
#     Vbias_start | Vbias_stop
#   - Column widths tuned so important fields are always visible.
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import itertools
import re
import sys
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from PySide6.QtCore import Qt, QThread, QObject, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QGroupBox, QLabel, QPushButton, QLineEdit,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QProgressBar, QTextEdit, QSizePolicy, QFormLayout,
    QCheckBox, QAbstractItemView, QComboBox,
    QCompleter, QStyledItemDelegate,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from utils.config import cfg
from ui.preview_widget import RunPlanTree

# ── Schema constants ───────────────────────────────────────────────────────────

# Ordered list of selectable loop parameters (shown in dropdown).
LOOP_PARAMS = [
    "Center Wavelength (nm)",
    "Exposure Time (ms)",
    "Accumulations (EPF)",
    "Rotation1 Angle (deg)",
    "Rotation2 Angle (deg)",
    "Stage Position",
]

# Loop table columns.  Group is hidden in Synchronize/Zip modes.
LOOP_SCHEMA = ["Enable", "Parameter", "Values", "Group"]

# Batch table columns – important filter/flag columns first.
BATCH_SCHEMA = [
    "Run", "When", "MeasurePower",
    "condition_label", "repeat", "frames",
    "Vbg_start", "Vbg_stop", "Vtg_start", "Vtg_stop",
    "Vbias_start", "Vbias_stop",
]

# Batch column widths (px).  -1 = stretch to fill.
_BATCH_COL_WIDTHS = {
    "Run":             40,
    "When":           110,
    "MeasurePower":    90,
    "condition_label": 100,
    "repeat":          50,
    "frames":          50,
    "Vbg_start":       70,
    "Vbg_stop":        70,
    "Vtg_start":       70,
    "Vtg_stop":        70,
    "Vbias_start":     75,
    "Vbias_stop":      75,
}

# Loop mode labels and their tooltips.
LOOP_MODES = {
    "Synchronize": (
        "Each enabled row is a separate loop level.\n"
        "Row 1 = outermost loop, row 2 = inner, …\n"
        "Result: Cartesian product of all enabled parameters."
    ),
    "Zip": (
        "All enabled rows are iterated together in lockstep.\n"
        "All must have the same number of values.\n"
        "Result: N sequences where N = number of values per row."
    ),
    "Customized": (
        "Rows with the same Group number are zipped together.\n"
        "Different Groups form a Cartesian product.\n"
        "Shows the Group column for manual assignment."
    ),
}

_INVALID_CHARS = r'<>:"/\|?*'

# ── Default table contents ─────────────────────────────────────────────────────

_DEFAULT_LOOP = pd.DataFrame([
    {"Enable": True,  "Parameter": "Center Wavelength (nm)", "Values": "860",  "Group": 1},
    {"Enable": False, "Parameter": "Exposure Time (ms)",     "Values": "2000", "Group": 1},
    {"Enable": False, "Parameter": "Accumulations (EPF)",    "Values": "1",    "Group": 1},
    {"Enable": False, "Parameter": "Rotation1 Angle (deg)",  "Values": "0",    "Group": 2},
    {"Enable": False, "Parameter": "Rotation2 Angle (deg)",  "Values": "0",    "Group": 2},
    {"Enable": False, "Parameter": "Stage Position",         "Values": "0",    "Group": 2},
])

_DEFAULT_BATCH = pd.DataFrame([{
    "Run": True, "When": "", "MeasurePower": False,
    "condition_label": "ZB_VB0", "repeat": 1, "frames": 1,
    "Vbg_start": 0.0, "Vbg_stop": 0.0,
    "Vtg_start": 0.0, "Vtg_stop": 0.0,
    "Vbias_start": "", "Vbias_stop": "",
}])


# ── Pure data helpers ──────────────────────────────────────────────────────────

def _to_bool(x) -> bool:
    if isinstance(x, bool): return x
    if isinstance(x, str):  return x.strip().lower() in ("true", "1", "yes", "x", "\u2713")
    try: return bool(int(x))
    except Exception: return False


def _sanitize(s: str) -> str:
    for ch in _INVALID_CHARS:
        s = s.replace(ch, "")
    return s.strip()


class _SafeDict(dict):
    def __missing__(self, k): return f"{{{k}}}"


def _parse_values(s: str, param: str = "") -> Optional[List[float]]:
    if not s or not str(s).strip():
        return None
    raw = str(s).strip()
    linspace_mode = raw.startswith("(") and raw.endswith(")")
    if linspace_mode:
        raw = raw[1:-1].strip()
    try:
        nums = [float(x.strip()) for x in raw.replace(";", ",").split(",") if x.strip()]
    except ValueError:
        return None
    if str(param).startswith("Stage Position") and linspace_mode and len(nums) == 3:
        a, b, n = nums
        if float(n).is_integer() and int(n) >= 2:
            return np.linspace(a, b, int(n)).tolist()
    return nums


def _normalize_loop(df: pd.DataFrame) -> pd.DataFrame:
    df = (df if isinstance(df, pd.DataFrame) else pd.DataFrame()).copy()
    for c in LOOP_SCHEMA:
        if c not in df.columns:
            df[c] = "" if c not in ("Group",) else 1
    df = df[LOOP_SCHEMA].reset_index(drop=True)
    df["Enable"] = df["Enable"].map(_to_bool)
    df["Group"]  = pd.to_numeric(df["Group"], errors="coerce").fillna(1).astype(int)
    return df


def _normalize_batch(df: pd.DataFrame) -> pd.DataFrame:
    df = (df if isinstance(df, pd.DataFrame) else pd.DataFrame()).copy()
    for c in BATCH_SCHEMA:
        if c not in df.columns:
            df[c] = ""
    df = df[BATCH_SCHEMA].reset_index(drop=True)
    df["Run"]          = df["Run"].map(_to_bool)
    df["MeasurePower"] = df["MeasurePower"].map(_to_bool)
    for c in ("repeat", "frames"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(1).astype(int)
    for c in ("Vbg_start", "Vbg_stop", "Vtg_start", "Vtg_stop"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    for c in ("Vbias_start", "Vbias_stop"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["When"] = df["When"].fillna("").astype(str)
    return df


def _when_ok(when_str: str, ctx: dict) -> bool:
    w = str(when_str).strip()
    if not w or w.lower() in ("always", "all", ""):
        return True
    try:
        ns = {re.sub(r"[^a-zA-Z0-9_]", "_", k): v for k, v in ctx.items()}
        return bool(eval(w, {"__builtins__": {}}, ns))  # noqa: S307
    except Exception:
        return True


def _outer_ctx(ctx: dict) -> dict:
    out = dict(ctx)
    for k, v in list(ctx.items()):
        short = k.split("(")[0].strip().replace(" ", "_")
        if short not in out:
            out[short] = v
    return out


def _build_plan(loop_df: pd.DataFrame, batch_df: pd.DataFrame,
                mode: str = "Synchronize"):
    """
    Return (final_sequence, enabled_df_batch, total_acq).

    mode:
      "Synchronize" – each enabled row = its own Level (Cartesian product,
                      ordered top-to-bottom in the table).
      "Zip"         – all enabled rows share Level 1 (zipped together).
      "Customized"  – use the Group column as Level.
    """
    loop = _normalize_loop(loop_df)
    active = loop[loop["Enable"]].reset_index(drop=True).copy()

    if mode == "Zip":
        active["Level"] = 1
    elif mode == "Synchronize":
        active["Level"] = range(1, len(active) + 1)
    else:  # Customized
        active["Level"] = active["Group"].fillna(1).astype(int)

    levels: Dict[int, List[dict]] = {}
    for _, row in active.iterrows():
        vals = _parse_values(str(row["Values"]), str(row["Parameter"]))
        if vals:
            levels.setdefault(int(row["Level"]), []).append(
                {"p": row["Parameter"], "v": vals}
            )

    level_combos = []
    for lvl in sorted(levels.keys()):
        specs = levels[lvl]
        lengths = [len(s["v"]) for s in specs]
        if len(set(lengths)) > 1:
            raise ValueError(
                f"Group {lvl}: value-count mismatch {lengths}. "
                "All rows in the same group must have the same number of values."
            )
        zipped = list(zip(*[s["v"] for s in specs]))
        level_combos.append([{s["p"]: v for s, v in zip(specs, z)} for z in zipped])

    if not level_combos:
        final_sequence = [{}]
    else:
        final_sequence = []
        for combo in itertools.product(*level_combos):
            ctx: dict = {}
            for d in combo:
                ctx.update(d)
            final_sequence.append(ctx)

    batch = _normalize_batch(batch_df)
    batch = batch[batch["Run"]].reset_index(drop=True)

    total_acq = 0
    for ctx in final_sequence:
        outer = _outer_ctx(ctx)
        for _, r in batch.iterrows():
            if _when_ok(r.get("When", ""), outer):
                total_acq += max(int(r.get("repeat", 1)), 1)

    return final_sequence, batch, max(total_acq, 0)


def _exp_s_str(ms: float) -> str:
    s = float(ms) / 1000.0
    if s >= 1.0:
        return f"{s:g}s"
    return f"{int(ms):d}ms"


def _fmt_deg(v) -> str:
    if v is None: return ""
    try: return f"{float(v):.1f}"
    except Exception: return ""


# ── Cell-widget helpers for the loop table ─────────────────────────────────────

def _make_check_cell(checked: bool) -> QWidget:
    """Return a centred-checkbox widget for use in table cells."""
    container = QWidget()
    lay = QHBoxLayout(container)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
    cb = QCheckBox()
    cb.setChecked(checked)
    lay.addWidget(cb)
    return container


def _cell_checked(widget: QWidget) -> bool:
    if widget is None:
        return False
    cb = widget.findChild(QCheckBox)
    return cb.isChecked() if cb else False


def _make_param_combo(current: str) -> QComboBox:
    combo = QComboBox()
    combo.addItems(LOOP_PARAMS)
    idx = combo.findText(current)
    combo.setCurrentIndex(max(0, idx))
    return combo


# ── When-column delegate ──────────────────────────────────────────────────────

def _param_to_expr_name(param: str) -> tuple[str, str]:
    """
    Return the (full_name, short_name) as they appear in the _when_ok namespace.

    _when_ok does:  re.sub(r"[^a-zA-Z0-9_]", "_", k)  on every ctx key.
    _outer_ctx adds: k.split("(")[0].strip().replace(" ", "_")  as a shorthand.
    """
    full  = re.sub(r"[^a-zA-Z0-9_]", "_", param)
    short = re.sub(r"[^a-zA-Z0-9_]", "_", param.split("(")[0].strip())
    return full, short


class _WhenDelegate(QStyledItemDelegate):
    """
    Cell editor for the 'When' column.

    Opens a QLineEdit with a QCompleter pre-populated from the loop table's
    current parameter names (both the full sanitised form and the short form
    used by _outer_ctx / _when_ok).  The user can still type any expression
    freely; the completer just shows valid name fragments.
    """

    def __init__(self, loop_table: QTableWidget, parent=None):
        super().__init__(parent)
        self._loop_table = loop_table

    def _completions(self) -> list[str]:
        seen: list[str] = []
        for r in range(self._loop_table.rowCount()):
            combo = self._loop_table.cellWidget(r, 1)
            if combo is None:
                continue
            full, short = _param_to_expr_name(combo.currentText())
            for name in (short, full):   # short first — easier to type
                if name and name not in seen:
                    seen.append(name)
        return seen

    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        completions = self._completions()
        if completions:
            completer = QCompleter(completions, editor)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            editor.setCompleter(completer)
        tip_names = "\n  ".join(completions) if completions else "(no loop parameters enabled)"
        editor.setToolTip(
            "Python expression — row runs when this evaluates to True.\n"
            "Leave blank to run unconditionally.\n\n"
            "Available parameter names:\n"
            f"  {tip_names}\n\n"
            "Examples:\n"
            "  Center_Wavelength__nm_ == 860\n"
            "  Rotation1_Angle_ > 45\n"
            "  Stage_Position != 0"
        )
        return editor

    def setEditorData(self, editor, index):
        editor.setText(index.data() or "")

    def setModelData(self, editor, model, index):
        model.setData(index, editor.text())


# ── Run worker ────────────────────────────────────────────────────────────────

class _RunWorker(QObject):
    log         = Signal(str)
    progress    = Signal(int, int)
    tree_update = Signal(int, str, int)
    finished    = Signal()
    error       = Signal(str)

    def __init__(
        self,
        final_sequence: List[dict],
        df_batch: pd.DataFrame,
        *,
        lf6_ctrl=None, smu_ctrl=None,
        rotation_ctrl=None, stage_ctrl=None, pm_ctrl=None,
        out_dir: Path,
        filename_pattern: str,
        sample: str, tag: str, laser_nm: str, power_uw: str,
        stop_event: threading.Event,
    ) -> None:
        super().__init__()
        self._seq      = final_sequence
        self._batch    = df_batch
        self._lf6      = lf6_ctrl
        self._smu      = smu_ctrl
        self._rot      = rotation_ctrl
        self._stage    = stage_ctrl
        self._pm       = pm_ctrl
        self._out_dir  = out_dir
        self._pattern  = filename_pattern
        self._sample   = sample
        self._tag      = tag
        self._laser_nm = laser_nm
        self._power_uw = power_uw
        self._stop     = stop_event

    @Slot()
    def run(self) -> None:
        from app.engine.csv_writer import CSVWriter

        total_acq = sum(
            max(int(r.get("repeat", 1)), 1)
            for ctx in self._seq
            for _, r in self._batch.iterrows()
            if _when_ok(r.get("When", ""), _outer_ctx(ctx))
        )
        done = 0

        try:
            for seq_i, ctx in enumerate(self._seq):
                if self._stop.is_set():
                    break

                center   = float(ctx.get("Center Wavelength (nm)", cfg.lf6.center_nm))
                exp_ms   = float(ctx.get("Exposure Time (ms)",     cfg.lf6.exposure_ms))
                accum    = int(ctx.get("Accumulations (EPF)",      cfg.lf6.accumulations))
                val_rot1  = ctx.get("Rotation1 Angle (deg)")
                val_rot2  = ctx.get("Rotation2 Angle (deg)")
                val_stage = ctx.get("Stage Position")

                if self._lf6 and self._lf6.is_connected:
                    self._lf6.apply_settings(exp_ms, center, accum)
                    time.sleep(0.15)

                if val_rot1 is not None and self._rot and self._rot.is_connected("rot1"):
                    self._rot.move_to("rot1", float(val_rot1))
                if val_rot2 is not None and self._rot and self._rot.is_connected("rot2"):
                    self._rot.move_to("rot2", float(val_rot2))
                if val_stage is not None and self._stage and self._stage.is_connected:
                    self._stage.adapter.move_to(float(val_stage))

                outer = _outer_ctx(ctx)
                for _, row in self._batch.iterrows():
                    if not _when_ok(row.get("When", ""), outer):
                        continue
                    if self._stop.is_set():
                        break

                    cond_label = _sanitize(str(row.get("condition_label", "")))
                    n_rep    = max(int(row.get("repeat", 1)), 1)
                    n_frames = max(int(row.get("frames",  1)), 1)
                    try:
                        vbg_s = float(row["Vbg_start"]); vbg_e = float(row["Vbg_stop"])
                        vtg_s = float(row["Vtg_start"]); vtg_e = float(row["Vtg_stop"])
                    except Exception:
                        vbg_s = vbg_e = vtg_s = vtg_e = 0.0

                    vbias_s = None if pd.isna(row.get("Vbias_start")) else float(row["Vbias_start"])
                    vbias_e = None if pd.isna(row.get("Vbias_stop"))  else float(row["Vbias_stop"])

                    self.tree_update.emit(seq_i, cond_label, 0)

                    for r_i in range(n_rep):
                        if self._stop.is_set():
                            break

                        self.tree_update.emit(seq_i, cond_label, r_i)
                        self.log.emit(
                            f"Seq {seq_i+1}/{len(self._seq)} | {cond_label} rep {r_i+1}/{n_rep}"
                        )

                        pwr_str = self._power_uw
                        if _to_bool(row.get("MeasurePower", False)):
                            if self._pm and self._pm.is_connected:
                                try:
                                    p_w = self._pm.adapter.get_power()
                                    pwr_str = f"{float(p_w)*1e6:.3f}"
                                    self.log.emit(f"  Power: {pwr_str} uW")
                                except Exception as e:
                                    self.log.emit(f"  Power read failed: {e}")

                        rot1_s = _fmt_deg(val_rot1)
                        rot2_s = _fmt_deg(val_rot2)
                        rot_parts = []
                        if rot1_s: rot_parts.append(f"R1_{rot1_s}deg")
                        if rot2_s: rot_parts.append(f"R2_{rot2_s}deg")
                        rot_block = "_".join(rot_parts)

                        vb_block = ""
                        if vbias_s is not None and vbias_e is not None:
                            vb_block = f"Vb{vbias_s:+.2g}to{vbias_e:+.2g}"
                        cond_full  = cond_label + (f"_{vb_block}" if vb_block else "")
                        rep_suffix = f"_rep{r_i+1:02d}" if n_rep > 1 else ""

                        fname_ctx = _SafeDict(**ctx)
                        fname_ctx.update({
                            "sample":    self._sample,    "tag":       self._tag,
                            "laser_nm":  self._laser_nm,  "power_uw":  pwr_str,
                            "exp_s":     _exp_s_str(exp_ms), "epf":    str(accum),
                            "center_nm": f"{center:.0f}", "rot1_deg":  rot1_s,
                            "rot2_deg":  rot2_s,          "rot_block": rot_block,
                            "cond_block": cond_full,
                        })

                        try:
                            stem_base = _sanitize(self._pattern.format_map(fname_ctx)) + rep_suffix
                        except Exception as e:
                            stem_base = f"ERR_{cond_full}{rep_suffix}"
                            self.log.emit(f"  Filename error: {e}")

                        stem_final = self._unique_stem(self._out_dir, stem_base)
                        self.log.emit(f"  -> {stem_final}.csv")

                        if self._smu and self._smu.is_connected:
                            dev = self._smu.device
                            try:
                                dev.set_gates(
                                    Vbg=vbg_s, Vtg=vtg_s,
                                    ramp_step=cfg.ramp.step_V,
                                    delay_s=cfg.ramp.delay_s,
                                    stop_cb=self._stop.is_set,
                                )
                                if vbias_s is not None:
                                    dev.set_bias(
                                        Vbias=vbias_s,
                                        ramp_step=cfg.ramp.vbias_step_V,
                                        delay_s=cfg.ramp.delay_s,
                                        stop_cb=self._stop.is_set,
                                    )
                            except Exception as e:
                                self.log.emit(f"  Gate set error: {e}")
                            time.sleep(cfg.ramp.settle_s)

                        if self._stop.is_set():
                            break

                        wl = np.array([]); cts = np.array([])
                        if self._lf6 and self._lf6.is_connected:
                            try:
                                wl, cts = self._lf6.adapter.acquire()
                            except Exception as e:
                                self.log.emit(f"  Acquire error: {e}")
                        else:
                            self.log.emit("  LF6 not connected — skipping acquire")

                        Ibg = Itg = Ib = None
                        Vbg_meas = Vtg_meas = float("nan")
                        if self._smu and self._smu.is_connected:
                            dev = self._smu.device
                            try:
                                Ibg, Itg, Ib = dev.read_currents()
                                Vbg_meas, Vtg_meas = dev.read_current_gates()
                            except Exception:
                                pass

                        if wl.size > 0:
                            try:
                                csv_path = self._out_dir / f"{stem_final}.csv"
                                csv_path.parent.mkdir(parents=True, exist_ok=True)
                                writer = CSVWriter(str(csv_path))
                                writer.set_wavelength_headers(wl.tolist())
                                row_data = {
                                    "Vbg_set": vbg_s, "Vbg_meas": Vbg_meas,
                                    "Vtg_set": vtg_s, "Vtg_meas": Vtg_meas,
                                    "Ibg": Ibg, "Itg": Itg, "Ibias": Ib,
                                }
                                if vbias_s is not None:
                                    row_data["Vbias_set"] = vbias_s
                                writer.write_row(row_data, cts.tolist())
                                writer.close()
                            except Exception as e:
                                self.log.emit(f"  CSV write error: {e}")

                        done += 1
                        self.progress.emit(done, total_acq)

            if self._smu and self._smu.is_connected:
                self.log.emit("Ramping all to zero…")
                try:
                    self._smu.device.ramp_all_to_zero(
                        ramp_step=cfg.ramp.step_V, delay_s=cfg.ramp.delay_s,
                    )
                except Exception as e:
                    self.log.emit(f"Ramp-to-zero error: {e}")

            self.log.emit("Run complete.")

        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()

    @staticmethod
    def _unique_stem(out_dir: Path, stem: str) -> str:
        out_dir.mkdir(parents=True, exist_ok=True)
        if not (out_dir / f"{stem}.csv").exists():
            return stem
        n = 1
        while (out_dir / f"{stem}_{n:03d}.csv").exists():
            n += 1
        return f"{stem}_{n:03d}"


# ── Loop table read / write ────────────────────────────────────────────────────

def _populate_loop_table(table: QTableWidget, df: pd.DataFrame) -> None:
    """Fill the loop table from a DataFrame, using cell widgets."""
    table.setRowCount(0)
    for r, row in df.iterrows():
        table.insertRow(r)
        table.setCellWidget(r, 0, _make_check_cell(_to_bool(row.get("Enable", False))))
        table.setCellWidget(r, 1, _make_param_combo(str(row.get("Parameter", LOOP_PARAMS[0]))))
        table.setItem(r, 2, QTableWidgetItem(str(row.get("Values", ""))))
        table.setItem(r, 3, QTableWidgetItem(str(int(row.get("Group", 1)))))


def _read_loop_table(table: QTableWidget) -> pd.DataFrame:
    rows = []
    for r in range(table.rowCount()):
        enabled = _cell_checked(table.cellWidget(r, 0))
        combo   = table.cellWidget(r, 1)
        param   = combo.currentText() if combo else LOOP_PARAMS[0]
        v_item  = table.item(r, 2)
        values  = v_item.text() if v_item else ""
        g_item  = table.item(r, 3)
        try:
            group = int(g_item.text()) if g_item and g_item.text().strip() else 1
        except ValueError:
            group = 1
        rows.append({"Enable": enabled, "Parameter": param, "Values": values, "Group": group})
    return pd.DataFrame(rows, columns=LOOP_SCHEMA) if rows else pd.DataFrame(columns=LOOP_SCHEMA)


# ── Batch table read / write ───────────────────────────────────────────────────

def _populate_batch_table(table: QTableWidget, df: pd.DataFrame) -> None:
    table.setRowCount(len(df))
    table.setColumnCount(len(BATCH_SCHEMA))
    table.setHorizontalHeaderLabels(BATCH_SCHEMA)
    for r, row in df.iterrows():
        for c, col in enumerate(BATCH_SCHEMA):
            val = row[col]
            text = "" if (isinstance(val, float) and pd.isna(val)) else str(val)
            table.setItem(r, c, QTableWidgetItem(text))
    # Apply column widths
    hdr = table.horizontalHeader()
    for c, col in enumerate(BATCH_SCHEMA):
        w = _BATCH_COL_WIDTHS.get(col, 80)
        hdr.resizeSection(c, w)
    hdr.setSectionResizeMode(len(BATCH_SCHEMA) - 1, QHeaderView.ResizeMode.Stretch)


def _read_batch_table(table: QTableWidget) -> pd.DataFrame:
    rows = []
    for r in range(table.rowCount()):
        row = {}
        for c, col in enumerate(BATCH_SCHEMA):
            item = table.item(r, c)
            row[col] = item.text() if item else ""
        rows.append(row)
    return pd.DataFrame(rows, columns=BATCH_SCHEMA) if rows else pd.DataFrame(columns=BATCH_SCHEMA)


# ── Main panel ────────────────────────────────────────────────────────────────

class PresetsPanel(QWidget):
    """
    Presets sweep panel.

    Usage:
        panel = PresetsPanel(lf6_ctrl=lf6, smu_ctrl=smu, ...)
    """

    def __init__(
        self,
        lf6_ctrl=None, smu_ctrl=None,
        rotation_ctrl=None, stage_ctrl=None, pm_ctrl=None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._lf6   = lf6_ctrl
        self._smu   = smu_ctrl
        self._rot   = rotation_ctrl
        self._stage = stage_ctrl
        self._pm    = pm_ctrl

        self._loop_src  = _normalize_loop(_DEFAULT_LOOP)
        self._batch_src = _normalize_batch(_DEFAULT_BATCH)

        self._run_thread: Optional[QThread]      = None
        self._run_worker: Optional[_RunWorker]   = None
        self._stop_event = threading.Event()

        self._final_seq: List[dict] = []
        self._df_batch:  pd.DataFrame = self._batch_src.copy()
        self._total_acq: int = 0

        self._build()
        self._refresh_tables()
        self._update_plan()

    # ── build UI ──────────────────────────────────────────────────────────────

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # ── top meta row ───────────────────────────────────────────────────
        meta = QHBoxLayout()
        self._sample_edit    = QLineEdit(); self._sample_edit.setPlaceholderText("Sample")
        self._sample_edit.setToolTip("Sample name — included in saved filenames.")
        self._tag_edit       = QLineEdit(); self._tag_edit.setPlaceholderText("Tag")
        self._tag_edit.setToolTip("Short run tag or note — appended to filenames.")
        self._laser_edit     = QLineEdit(); self._laser_edit.setPlaceholderText("Laser nm")
        self._laser_edit.setFixedWidth(80)
        self._laser_edit.setToolTip("Excitation laser wavelength in nm — recorded in filename.")
        self._power_edit     = QLineEdit(); self._power_edit.setPlaceholderText("Power µW")
        self._power_edit.setFixedWidth(90)
        self._power_edit.setToolTip(
            "Laser power in µW — recorded in filename.\n"
            "Overwritten by a live PM100D reading when MeasurePower is enabled."
        )
        self._subfolder_edit = QLineEdit(); self._subfolder_edit.setPlaceholderText("Subfolder")
        self._subfolder_edit.setToolTip("Optional subfolder created under the base output directory.")
        meta.addWidget(QLabel("Sample:")); meta.addWidget(self._sample_edit)
        meta.addWidget(QLabel("Tag:"));    meta.addWidget(self._tag_edit)
        meta.addWidget(QLabel("Laser:"));  meta.addWidget(self._laser_edit)
        meta.addWidget(QLabel("Power:"));  meta.addWidget(self._power_edit)
        meta.addWidget(QLabel("Sub:"));    meta.addWidget(self._subfolder_edit)
        root.addLayout(meta)

        # ── main splitter ──────────────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, stretch=1)

        # ── left: tables + apply/discard ──────────────────────────────────
        left = QWidget()
        lay_left = QVBoxLayout(left)
        lay_left.setContentsMargins(0, 0, 0, 0)

        # Loop table group
        loop_grp = QGroupBox("Loop table")
        loop_lay = QVBoxLayout(loop_grp)

        # Mode selector row
        mode_row = QHBoxLayout()
        _mode_lbl = QLabel("Loop mode:")
        _mode_lbl.setToolTip(
            "Controls how enabled loop rows are combined:\n"
            "  Synchronize — nested Cartesian product (row 1 = outermost)\n"
            "  Zip         — all rows stepped together in lockstep\n"
            "  Customized  — manual Group assignment; see Group column"
        )
        mode_row.addWidget(_mode_lbl)
        self._mode_combo = QComboBox()
        for mode, tip in LOOP_MODES.items():
            self._mode_combo.addItem(mode)
            self._mode_combo.setItemData(
                self._mode_combo.count() - 1, tip, Qt.ItemDataRole.ToolTipRole
            )
        self._mode_combo.setToolTip(LOOP_MODES["Synchronize"])
        self._mode_hint = QLabel("")
        self._mode_hint.setStyleSheet("color: gray; font-size: 10px;")
        self._mode_hint.setWordWrap(True)
        mode_row.addWidget(self._mode_combo)
        mode_row.addStretch()
        loop_lay.addLayout(mode_row)
        loop_lay.addWidget(self._mode_hint)

        # Loop table itself
        self._loop_table = QTableWidget(0, len(LOOP_SCHEMA))
        self._loop_table.setHorizontalHeaderLabels(LOOP_SCHEMA)
        self._loop_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._loop_table.verticalHeader().setVisible(False)
        # Column header tooltips
        _loop_hdr_tips = {
            "Enable":    "Check to include this row in the sweep.",
            "Parameter": "Instrument parameter to sweep over.",
            "Values":    (
                "Values to step through — comma-separated, e.g.  830, 860, 890\n"
                "Linspace shorthand: (start, stop, n)  e.g.  (830, 890, 7)\n"
                "  → generates n evenly-spaced values (linspace only supported\n"
                "    for Stage Position)."
            ),
            "Group":     (
                "Customized mode only.\n"
                "Rows sharing the same Group number are zipped (stepped together).\n"
                "Different Group numbers are Cartesian-producted."
            ),
        }
        for i, col in enumerate(LOOP_SCHEMA):
            item = self._loop_table.horizontalHeaderItem(i)
            if item and col in _loop_hdr_tips:
                item.setToolTip(_loop_hdr_tips[col])
        # Set column widths
        self._loop_table.horizontalHeader().resizeSection(0, 55)   # Enable
        self._loop_table.horizontalHeader().resizeSection(1, 180)  # Parameter
        self._loop_table.horizontalHeader().resizeSection(2, 120)  # Values
        self._loop_table.horizontalHeader().resizeSection(3, 55)   # Group
        self._loop_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        loop_btn_row = QHBoxLayout()
        self._loop_add_btn = QPushButton("+ Row"); self._loop_add_btn.setFixedWidth(60)
        self._loop_add_btn.setToolTip("Append a new empty row to the loop table.")
        self._loop_del_btn = QPushButton("- Row"); self._loop_del_btn.setFixedWidth(60)
        self._loop_del_btn.setToolTip("Delete the selected row(s) from the loop table.")
        loop_btn_row.addWidget(self._loop_add_btn)
        loop_btn_row.addWidget(self._loop_del_btn)
        loop_btn_row.addStretch()
        loop_lay.addWidget(self._loop_table)
        loop_lay.addLayout(loop_btn_row)
        lay_left.addWidget(loop_grp)

        # Batch table group
        batch_grp = QGroupBox("Batch table")
        batch_lay = QVBoxLayout(batch_grp)
        self._batch_table = QTableWidget(0, len(BATCH_SCHEMA))
        self._batch_table.setHorizontalHeaderLabels(BATCH_SCHEMA)
        self._batch_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._batch_table.verticalHeader().setVisible(False)
        self._batch_table.setMinimumHeight(90)
        # Batch column header tooltips
        _batch_hdr_tips = {
            "Run":             "Check to include this row in the batch.",
            "When":            (
                "Optional Python expression — row runs only when this evaluates to True.\n"
                "Leave blank (or 'always') to run unconditionally.\n"
                "Example:  Center_Wavelength__nm_ == 860"
            ),
            "MeasurePower":    (
                "If checked, the PM100D reads laser power just before acquisition.\n"
                "The measured value overwrites the manual Power field in the filename."
            ),
            "condition_label": "Short label appended to the filename to identify this batch condition.",
            "repeat":          "Number of times to repeat this acquisition (repetitions).",
            "frames":          "Number of frames per single acquisition call.",
            "Vbg_start":       "Back-gate voltage to ramp to at the start of this row (V).",
            "Vbg_stop":        "Back-gate voltage to ramp to at the end / for reset (V).",
            "Vtg_start":       "Top-gate voltage to ramp to at the start of this row (V).",
            "Vtg_stop":        "Top-gate voltage to ramp to at the end / for reset (V).",
            "Vbias_start":     "Source–drain bias at the start of the acquisition (V). Leave blank to skip.",
            "Vbias_stop":      "Source–drain bias ramp end value (V). Leave blank to skip.",
        }
        for i, col in enumerate(BATCH_SCHEMA):
            item = self._batch_table.horizontalHeaderItem(i)
            if item and col in _batch_hdr_tips:
                item.setToolTip(_batch_hdr_tips[col])
        # When-column delegate — autocomplete from loop table parameter names
        self._when_delegate = _WhenDelegate(self._loop_table, self._batch_table)
        _when_col = BATCH_SCHEMA.index("When")
        self._batch_table.setItemDelegateForColumn(_when_col, self._when_delegate)

        batch_btn_row = QHBoxLayout()
        self._batch_add_btn = QPushButton("+ Row"); self._batch_add_btn.setFixedWidth(60)
        self._batch_add_btn.setToolTip("Append a new empty row to the batch table.")
        self._batch_del_btn = QPushButton("- Row"); self._batch_del_btn.setFixedWidth(60)
        self._batch_del_btn.setToolTip("Delete the selected row(s) from the batch table.")
        batch_btn_row.addWidget(self._batch_add_btn)
        batch_btn_row.addWidget(self._batch_del_btn)
        batch_btn_row.addStretch()
        batch_lay.addWidget(self._batch_table)
        batch_lay.addLayout(batch_btn_row)
        lay_left.addWidget(batch_grp, stretch=1)

        apply_row = QHBoxLayout()
        self._apply_btn   = QPushButton("Apply")
        self._apply_btn.setToolTip(
            "Commit the current table edits and rebuild the run plan preview."
        )
        self._discard_btn = QPushButton("Discard")
        self._discard_btn.setToolTip("Revert the tables to the last applied state.")
        apply_row.addWidget(self._apply_btn)
        apply_row.addWidget(self._discard_btn)
        apply_row.addStretch()
        lay_left.addLayout(apply_row)

        splitter.addWidget(left)

        # ── right: tree + progress + run/stop + log ───────────────────────
        right = QWidget()
        lay_right = QVBoxLayout(right)
        lay_right.setContentsMargins(0, 0, 0, 0)

        self._summary_lbl = QLabel("Apply tables to update preview.")
        self._summary_lbl.setWordWrap(True)
        lay_right.addWidget(self._summary_lbl)

        self._tree = RunPlanTree()
        self._tree.setMinimumHeight(150)
        lay_right.addWidget(self._tree, stretch=1)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFormat("%v/%m acquisitions")
        lay_right.addWidget(self._progress)

        self._status_lbl = QLabel("Idle")
        self._status_lbl.setStyleSheet("color: gray;")
        lay_right.addWidget(self._status_lbl)

        run_row = QHBoxLayout()
        self._run_btn  = QPushButton("Run")
        self._run_btn.setToolTip(
            "Start the sweep.\n"
            "Click Apply first to lock in any table edits."
        )
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setToolTip(
            "Request a graceful stop after the current acquisition finishes.\n"
            "Voltages are ramped back to zero before the run exits."
        )
        self._stop_btn.setEnabled(False)
        self._stop_btn.setStyleSheet("color: red;")
        run_row.addWidget(self._run_btn)
        run_row.addWidget(self._stop_btn)
        run_row.addStretch()
        lay_right.addLayout(run_row)

        log_grp = QGroupBox("Log")
        log_lay = QVBoxLayout(log_grp)
        self._log_text = QTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setMaximumHeight(180)
        self._log_text.setStyleSheet("font-family: monospace; font-size: 11px;")
        clear_log_btn = QPushButton("Clear")
        clear_log_btn.setFixedWidth(55)
        clear_log_btn.clicked.connect(self._log_text.clear)
        log_hdr = QHBoxLayout()
        log_hdr.addWidget(QLabel("Run log"))
        log_hdr.addStretch()
        log_hdr.addWidget(clear_log_btn)
        log_lay.addLayout(log_hdr)
        log_lay.addWidget(self._log_text)
        lay_right.addWidget(log_grp)

        splitter.addWidget(right)
        splitter.setSizes([540, 460])

        # ── wire ──────────────────────────────────────────────────────────
        self._mode_combo.currentTextChanged.connect(self._on_mode_changed)
        self._apply_btn.clicked.connect(self._on_apply)
        self._discard_btn.clicked.connect(self._refresh_tables)
        self._loop_add_btn.clicked.connect(self._add_loop_row)
        self._loop_del_btn.clicked.connect(self._del_loop_row)
        self._batch_add_btn.clicked.connect(self._add_batch_row)
        self._batch_del_btn.clicked.connect(self._del_batch_row)
        self._run_btn.clicked.connect(self._on_run)
        self._stop_btn.clicked.connect(self._on_stop)

        # Initialise mode UI (sets hint label + Group column visibility)
        self._on_mode_changed(self._mode_combo.currentText())

    # ── mode ──────────────────────────────────────────────────────────────────

    @Slot(str)
    def _on_mode_changed(self, mode: str):
        # Update tooltip on the combo
        self._mode_combo.setToolTip(LOOP_MODES.get(mode, ""))
        # Update hint label
        hints = {
            "Synchronize": "Outer → inner nesting by row order.  Cartesian product.",
            "Zip":          "All enabled rows stepped together.  Must have equal value counts.",
            "Customized":   "Rows sharing the same Group number are zipped; groups are producted.",
        }
        self._mode_hint.setText(hints.get(mode, ""))
        # Show/hide the Group column
        show_group = (mode == "Customized")
        self._loop_table.setColumnHidden(3, not show_group)

    # ── table helpers ─────────────────────────────────────────────────────────

    def _refresh_tables(self):
        _populate_loop_table(self._loop_table, self._loop_src)
        _populate_batch_table(self._batch_table, self._batch_src)
        # Reapply mode (column visibility may have changed)
        self._on_mode_changed(self._mode_combo.currentText())

    @Slot()
    def _add_loop_row(self):
        r = self._loop_table.rowCount()
        self._loop_table.insertRow(r)
        self._loop_table.setCellWidget(r, 0, _make_check_cell(False))
        self._loop_table.setCellWidget(r, 1, _make_param_combo(LOOP_PARAMS[0]))
        self._loop_table.setItem(r, 2, QTableWidgetItem(""))
        self._loop_table.setItem(r, 3, QTableWidgetItem("1"))

    @Slot()
    def _del_loop_row(self):
        rows = {i.row() for i in self._loop_table.selectedIndexes()}
        for r in sorted(rows, reverse=True):
            self._loop_table.removeRow(r)

    @Slot()
    def _add_batch_row(self):
        r = self._batch_table.rowCount()
        self._batch_table.insertRow(r)
        defaults = {
            "Run": "True", "When": "", "MeasurePower": "False",
            "condition_label": "", "repeat": "1", "frames": "1",
            "Vbg_start": "0", "Vbg_stop": "0",
            "Vtg_start": "0", "Vtg_stop": "0",
            "Vbias_start": "", "Vbias_stop": "",
        }
        for c, col in enumerate(BATCH_SCHEMA):
            self._batch_table.setItem(r, c, QTableWidgetItem(defaults.get(col, "")))

    @Slot()
    def _del_batch_row(self):
        rows = {i.row() for i in self._batch_table.selectedIndexes()}
        for r in sorted(rows, reverse=True):
            self._batch_table.removeRow(r)

    # ── apply / plan ──────────────────────────────────────────────────────────

    @Slot()
    def _on_apply(self):
        self._loop_src  = _normalize_loop(_read_loop_table(self._loop_table))
        self._batch_src = _normalize_batch(_read_batch_table(self._batch_table))
        self._update_plan()

    def _update_plan(self):
        mode = self._mode_combo.currentText()
        try:
            seq, batch, total = _build_plan(self._loop_src, self._batch_src, mode=mode)
        except ValueError as exc:
            self._summary_lbl.setText(f"Plan error: {exc}")
            self._summary_lbl.setStyleSheet("color: red;")
            return
        self._summary_lbl.setStyleSheet("")
        self._final_seq  = seq
        self._df_batch   = batch
        self._total_acq  = total
        self._progress.setMaximum(max(total, 1))
        self._summary_lbl.setText(
            f"{len(seq)} sequence(s) × batch → {total} acquisition(s)  "
            f"[mode: {mode}]"
        )
        self._tree.update_plan(seq, batch, done=0, total_acq=total)

    # ── run / stop ────────────────────────────────────────────────────────────

    @Slot()
    def _on_run(self):
        if self._run_thread and self._run_thread.isRunning():
            return
        if not self._final_seq:
            self._log("No plan — click Apply first.")
            return

        sample    = self._sample_edit.text().strip() or "Sample"
        tag       = self._tag_edit.text().strip()
        subfolder = self._subfolder_edit.text().strip()
        laser_nm  = self._laser_edit.text().strip() or "0"
        power_uw  = self._power_edit.text().strip()  or "1"
        out_dir   = cfg.base_out / sample / subfolder if subfolder else cfg.base_out / sample

        self._stop_event.clear()
        self._run_thread = QThread(self)
        self._run_worker = _RunWorker(
            self._final_seq, self._df_batch,
            lf6_ctrl=self._lf6, smu_ctrl=self._smu,
            rotation_ctrl=self._rot, stage_ctrl=self._stage, pm_ctrl=self._pm,
            out_dir=out_dir,
            filename_pattern=cfg.filename.pattern,
            sample=sample, tag=tag, laser_nm=laser_nm, power_uw=power_uw,
            stop_event=self._stop_event,
        )
        self._run_worker.moveToThread(self._run_thread)
        self._run_thread.started.connect(self._run_worker.run)
        self._run_worker.log.connect(self._log)
        self._run_worker.progress.connect(self._on_progress)
        self._run_worker.tree_update.connect(self._on_tree_update)
        self._run_worker.error.connect(lambda e: self._log(f"ERROR: {e}"))
        self._run_worker.finished.connect(self._on_finished)
        self._run_worker.finished.connect(self._run_thread.quit)

        self._run_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._status_lbl.setText("Running…")
        self._status_lbl.setStyleSheet("color: orange;")
        self._run_thread.start()

    @Slot()
    def _on_stop(self):
        self._stop_event.set()
        self._status_lbl.setText("Stopping…")
        self._log("Stop requested.")

    @Slot(int, int)
    def _on_progress(self, done: int, total: int):
        self._progress.setMaximum(max(total, 1))
        self._progress.setValue(done)
        self._tree.update_plan(
            self._final_seq, self._df_batch,
            done=done, total_acq=total,
            current_seq_i=-1, current_label="", current_rep_i=0,
        )

    @Slot(int, str, int)
    def _on_tree_update(self, seq_i: int, label: str, rep_i: int):
        done = self._progress.value()
        self._tree.update_plan(
            self._final_seq, self._df_batch,
            done=done, total_acq=self._total_acq,
            current_seq_i=seq_i, current_label=label, current_rep_i=rep_i,
        )

    @Slot()
    def _on_finished(self):
        self._run_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._status_lbl.setText("Idle")
        self._status_lbl.setStyleSheet("color: gray;")

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_text.append(f"[{ts}] {msg}")
