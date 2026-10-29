"""Bounded, read-only review of the exact schedule used by Dual Gate.

No planner, widget, config or device is imported here. The schedule is borrowed
from the applied/draft plan; only the visible page is formatted for QML.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType

from PySide6.QtCore import QAbstractListModel, QModelIndex, Property, Qt, Signal, Slot


_ALIASES = {"Center Wavelength (nm)": "CW", "Exposure Time (ms)": "Exposure",
            "Accumulations (EPF)": "EPF", "Rotation1 Angle (deg)": "RotIn",
            "Rotation2 Angle (deg)": "RotOut", "Stage Position": "Stage",
            "Measurement setup": "Setup"}
_READABLE_LABELS = {**_ALIASES, "Center Wavelength (nm)": "Wavelength",
                    "Accumulations (EPF)": "Accum.", "Rotation1 Angle (deg)": "Rotation in",
                    "Rotation2 Angle (deg)": "Rotation out"}


def _enabled(value):
    if isinstance(value, (bool, int, float)):
        return bool(value) and (not isinstance(value, float) or math.isfinite(value))
    return str(value).strip().lower() not in {"false", "0", "no", "", "none", "nan"}


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _value(value):
    number = _number(value)
    return f"{number:g}" if number is not None else str(value if value is not None else "—")


def _positive(value):
    number = _number(value)
    return max(1, int(number)) if number is not None else 1


def _records(frame):
    return frame.to_dict("records") if hasattr(frame, "to_dict") else list(frame or ())


def _context(context, order=()):
    keys = list(dict.fromkeys([*order, *context]))
    return " · ".join(f"{_ALIASES.get(key, key)}={_value(context[key])}"
                      for key in keys if key in context) or "No loop variables"


def _gate(task):
    point = task.get("gate_point")
    if point is not None:
        return " · ".join(f"{axis} {_value(point[axis])} V" for axis in ("Vtg", "Vbg", "Vbias")
                          if _number(point.get(axis)) is not None)
    row = task.get("row", {})
    parts = []
    for axis in ("Vtg", "Vbg", "Vbias"):
        start, stop = _number(row.get(axis + "_start")), _number(row.get(axis + "_stop"))
        if start is None:
            continue
        stop = start if stop is None else stop
        span = f"{start:g}" if start == stop else f"{start:g} → {stop:g}"
        parts.append(f"{axis} {span} V")
    return " · ".join(parts) or "No gate targets"


def _same(a, b):
    """Compare requested values without NaN or container truth-value traps."""
    if a is b:
        return True
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        return a.keys() == b.keys() and all(_same(a[key], b[key]) for key in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    try:
        if math.isnan(a) and math.isnan(b):
            return True
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return bool(a == b)
    except (TypeError, ValueError):
        return False


def _setting_value(key, value):
    unit = {"Center Wavelength (nm)": "nm", "Exposure Time (ms)": "ms",
            "Rotation1 Angle (deg)": "°", "Rotation2 Angle (deg)": "°"}.get(key, "")
    number = _number(value)
    if number is not None and unit:
        return f"{number:g}{unit}" if unit == "°" else f"{number:g} {unit}"
    return _value(value)


def _gate_settings(task):
    point, row = task.get("gate_point"), task.get("row", {})
    values = []
    for axis in ("Vtg", "Vbg", "Vbias"):
        start = _number(point.get(axis)) if isinstance(point, Mapping) else _number(row.get(axis + "_start"))
        if start is None:
            continue
        stop = _number(row.get(axis + "_stop")) if point is None else start
        stop = start if stop is None else stop
        text = f"{start:g} V" if point is not None else f"{start:g} → {stop:g} V"
        values.append(dict(label=axis, value=text))
    return values


@dataclass(frozen=True, eq=False)
class _FlatSequenceIndex:
    """Read-only presentation index tied to one immutable compiled schedule."""

    schedule: object = field(repr=False)
    schedule_count: int
    sequence_count: int
    batch_count: int
    grouping: str
    lookup: Mapping = field(repr=False)
    prefix: tuple[int, ...] = field(repr=False)
    execution_positions: tuple[int, ...] = field(repr=False)

    def __deepcopy__(self, memo):
        # A reload copies the schedule and this index in one object graph.
        # Register first so either dictionary order (and ordinary cycles) keeps
        # the index bound to exactly that copied schedule. Index containers hold
        # only immutable primitive values and can safely be shared.
        duplicate = object.__new__(type(self))
        memo[id(self)] = duplicate
        for name in ("schedule_count", "sequence_count", "batch_count", "grouping",
                     "lookup", "prefix", "execution_positions"):
            object.__setattr__(duplicate, name, getattr(self, name))
        object.__setattr__(duplicate, "schedule", deepcopy(self.schedule, memo))
        return duplicate

    def matches(self, schedule, sequence_count, batch_count, grouping):
        return (self.schedule is schedule and self.schedule_count == len(schedule)
                and self.sequence_count == sequence_count and self.batch_count == batch_count
                and self.grouping == grouping and len(self.prefix) == len(schedule) + 1
                and len(self.execution_positions) == len(self.lookup))


def prepare_flat_sequence_index(schedule, *, sequence_count, batch_count,
                                grouping="loop_first", cancelled=None):
    """Build plain Python data in a worker, without creating a Qt object.

    ``batch_count`` counts enabled conditions. The caller keeps the compiled
    schedule immutable after publication; identity and dimensions are checked
    when a model borrows this index. No schedule or task dictionaries are copied.
    """
    lookup, prefix = {}, [0]
    for index, task in enumerate(schedule):
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError("Sequence index preparation cancelled")
        seq, row = int(task.get("seq_i", 0)), int(task.get("row_i", 0))
        ordinal = row * sequence_count + seq if grouping == "batch_first" else seq * batch_count + row
        lookup[ordinal] = index
        prefix.append(prefix[-1] + _positive(task.get("row", {}).get("repeat", 1)))
    if cancelled is not None and cancelled.is_set():
        raise InterruptedError("Sequence index preparation cancelled")
    return _FlatSequenceIndex(schedule=schedule, schedule_count=len(schedule),
                              sequence_count=sequence_count, batch_count=batch_count, grouping=grouping,
                              lookup=MappingProxyType(lookup), prefix=tuple(prefix),
                              execution_positions=tuple(sorted(lookup)))


class DualGateSequenceModel(QAbstractListModel):
    changed = Signal()
    overviewChanged = Signal()
    selectedDetailChanged = Signal()
    focusStepChanged = Signal()
    followingStepChanged = Signal()
    filtersChanged = Signal()
    nestingChanged = Signal()
    _ROLE_NAMES = ("step", "status", "statusKind", "condition", "loop", "gate",
                   "acquisition", "selected", "sourceIndex")
    _ROLES = {int(Qt.ItemDataRole.UserRole) + i + 1: name for i, name in enumerate(_ROLE_NAMES)}

    def __init__(self, parent=None, *, page_size=8):
        super().__init__(parent)
        self.page_size = min(200, max(1, int(page_size)))
        self._schedule = ()
        self._sequence = ()
        self._batch = []
        self._plan_key = None
        self._lookup = {}
        self._execution_positions = []
        self._prefix = [0]
        self._nested = False
        self._potential = 0
        self._page = 0
        self._follow = True
        self._show_skipped = False
        self._selected = 0
        self._rows = []
        self._detail = {}
        self._focus = {}
        self._following = {}
        self._plan = {}
        self._filters = []
        self._nesting = []
        self._error = ""
        self._published = None
        self._property_publications = {}

    def roleNames(self):
        return {role: name.encode() for role, name in self._ROLES.items()}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        name = self._ROLES.get(int(role), "step" if role == Qt.ItemDataRole.DisplayRole else "")
        return self._rows[index.row()].get(name)

    totalSteps = Property(int, lambda self: len(self._schedule), notify=changed)
    currentStep = Property(int, lambda self: int(self._plan.get("current_seq_i", -1)) + 1, notify=changed)
    previewState = Property(str, lambda self: self._plan.get("preview_state", "Applied sequence"), notify=changed)
    nesting = Property("QVariantList", lambda self: list(self._nesting), notify=nestingChanged)
    filters = Property("QVariantList", lambda self: list(self._filters), notify=filtersChanged)
    followCurrent = Property(bool, lambda self: self._follow, notify=changed)
    showSkipped = Property(bool, lambda self: self._show_skipped, notify=changed)
    supportsSkipped = Property(bool, lambda self: not self._nested and self._potential > len(self._schedule), notify=changed)
    page = Property(int, lambda self: self._page + 1, notify=changed)
    pageCount = Property(int, lambda self: max(1, (self._visible_total() + self.page_size - 1) // self.page_size), notify=changed)
    canPrevious = Property(bool, lambda self: self._page > 0, notify=changed)
    canNext = Property(bool, lambda self: self._page + 1 < self.pageCount, notify=changed)
    selectedDetail = Property("QVariantMap", lambda self: dict(self._detail), notify=selectedDetailChanged)
    focusStep = Property("QVariantMap", lambda self: dict(self._focus), notify=focusStepChanged)
    followingStep = Property("QVariantMap", lambda self: dict(self._following), notify=followingStepChanged)
    canPreviousStep = Property(bool, lambda self: bool(self._rows) and self._page * self.page_size + self._selected > 0, notify=changed)
    canNextStep = Property(bool, lambda self: bool(self._rows) and self._page * self.page_size + self._selected + 1 < self._visible_total(), notify=changed)
    jumpError = Property(str, lambda self: self._error, notify=changed)

    @Property("QVariantMap", notify=overviewChanged)
    def overview(self):
        return dict(phase=str(self._plan.get("run_outcome", "idle")).capitalize(),
                    planState=self.previewState,
                    stepsText=f"{len(self._schedule):,} {'spectra' if self._nested else 'sweeps'}",
                    filesText=f"{max(0, int(self._plan.get('done', 0))):,} / {max(0, int(self._plan.get('total_acq', 0))):,} files",
                    unitHint="Each step acquires one spectrum" if self._nested else "Each step runs a full gate sweep")

    @Property(str, notify=changed)
    def pageLabel(self):
        total = self._visible_total()
        start = self._page * self.page_size
        unit = "combinations" if self._show_skipped else "steps"
        return f"{start + 1 if total else 0:,}–{min(start + self.page_size, total):,} / {total:,} {unit} · page {self.page}/{self.pageCount}"

    @Property(str, notify=changed)
    def summary(self):
        done, total = max(0, int(self._plan.get("done", 0))), max(0, int(self._plan.get("total_acq", 0)))
        state = self._plan.get("run_outcome", "idle").capitalize()
        if self._nested:
            completed = self._completed_points()
            return f"{state} · {completed:,}/{len(self._schedule):,} spectra · {done:,}/{total:,} files"
        skipped = max(0, self._potential - len(self._schedule))
        return f"{state} · {len(self._schedule):,} sweep steps · {done:,}/{total:,} files · {skipped:,} skipped combinations"

    def _visible_total(self):
        return self._potential if self._show_skipped and not self._nested else len(self._schedule)

    def update_plan(self, final_sequence, df_batch, **kwargs):
        schedule = kwargs.get("acquisition_schedule")
        # The production page supplies its already compiled execution schedule.
        # Rebuilding a planner here would risk a different runnable order.
        schedule = schedule if schedule is not None else ()
        grouping = kwargs.get("acquisition_grouping", "loop_first")
        key = (id(schedule), id(final_sequence), id(df_batch), grouping)
        is_new = key != self._plan_key
        self._plan = dict(kwargs)
        if is_new:
            self._plan_key = key
            self._schedule, self._sequence = schedule, final_sequence
            definitions = _records(df_batch)
            self._batch = [row for row in definitions if _enabled(row.get("Run", True))]
            self._filters = [dict(condition=str(row.get("condition_label") or f"Row {index + 1}"),
                                  when=str(row.get("When", "") or "Always"),
                                  state="enabled" if _enabled(row.get("Run", True)) else "disabled")
                             for index, row in enumerate(definitions)]
            self._nested = bool(schedule) and bool(schedule[0].get("nested"))
            self._potential = len(final_sequence) * len(self._batch) if not self._nested else len(schedule)
            self._prefix = [0]
            self._lookup = {}
            self._execution_positions = []
            if not self._nested:
                prepared_index = kwargs.get("prepared_sequence_index")
                if (isinstance(prepared_index, _FlatSequenceIndex)
                        and prepared_index.matches(schedule, len(final_sequence), len(self._batch), grouping)):
                    self._lookup = prepared_index.lookup
                    self._prefix = prepared_index.prefix
                    self._execution_positions = prepared_index.execution_positions
                else:
                    for index, task in enumerate(schedule):
                        self._lookup[self._ordinal(task)] = index
                        self._prefix.append(self._prefix[-1] + _positive(task.get("row", {}).get("repeat", 1)))
                    self._execution_positions = sorted(self._lookup)
            order = schedule[0].get("execution_order", []) if self._nested else []
            self._nesting = ([str(level.get("label", level.get("id", level.get("kind", "")))) for level in order]
                             if order else (["Gate conditions", "Loop settings", "Repeat", "Gate points"]
                                            if grouping == "batch_first" else ["Loop settings", "Gate conditions", "Repeat", "Gate points"]))
            self._nesting.append("Acquire")
            self._page = self._selected = 0
            if self._nested:
                self._show_skipped = False
            self._error = ""
        if self._follow:
            current = max(0, int(kwargs.get("current_seq_i", -1)))
            if kwargs.get("run_outcome") == "completed":
                current = max(0, len(schedule) - 1)
            if current < len(schedule):
                ordinal = self._ordinal(schedule[current]) if self._show_skipped else current
                self._page, self._selected = divmod(ordinal, self.page_size)
        self._refresh()

    def _ordinal(self, task):
        seq, row = int(task.get("seq_i", 0)), int(task.get("row_i", 0))
        return (row * len(self._sequence) + seq if self._plan.get("acquisition_grouping") == "batch_first"
                else seq * len(self._batch) + row)

    def _entry(self, position):
        if not self._show_skipped:
            return position, self._schedule[position]
        source = self._lookup.get(position, -1)
        if source >= 0:
            return source, self._schedule[source]
        if self._plan.get("acquisition_grouping") == "batch_first":
            row, seq = divmod(position, len(self._sequence))
        else:
            seq, row = divmod(position, len(self._batch))
        return -1, dict(seq_i=seq, row_i=row, ctx=self._sequence[seq], row=self._batch[row])

    def _completed_points(self):
        if self._plan.get("run_outcome") == "completed":
            return len(self._schedule)
        value = self._plan.get("completed_points")
        return max(0, int(value if value is not None else max(0, int(self._plan.get("current_seq_i", -1)))))

    def _status(self, index):
        if index < 0:
            return "skipped", "Skipped · When"
        outcome = self._plan.get("run_outcome", "idle")
        if outcome == "completed":
            return "done", "Complete"
        complete = (index < self._completed_points() if self._nested else
                    int(self._plan.get("done", 0)) >= self._prefix[index + 1])
        if complete:
            return "done", "Complete"
        if index == int(self._plan.get("current_seq_i", -1)) and outcome in {"running", "failed", "stopped"}:
            return {"running": "now"}.get(outcome, outcome), outcome.capitalize()
        return "todo", "Pending"

    def _formatted(self, source, task):
        row, context = task.get("row", {}), task.get("ctx", {})
        label = str(row.get("condition_label") or f"Row {int(task.get('row_i', 0)) + 1}")
        if task.get("nested"):
            condition = f"{label} · repeat {int(task.get('repeat_i', 0)) + 1}/{_positive(task.get('repeat_total', 1))}"
            point = task.get("gate_point", {})
            acquisition = f"Point {int(point.get('point_i', 0)) + 1}/{_positive(point.get('point_total', 1))} · 1 spectrum"
        else:
            condition = label
            acquisition = f"{_positive(row.get('frames', 1)):,} frames × {_positive(row.get('repeat', 1)):,} repeats"
            if source == int(self._plan.get("current_seq_i", -1)) and self._plan.get("run_outcome") == "running":
                acquisition += (f" · repeat {int(self._plan.get('current_rep_i', 0)) + 1}"
                                f" · frame {int(self._plan.get('current_frame_i', 0))}/{int(self._plan.get('current_frame_total', 0))}")
        if _enabled(row.get("MeasurePower", False)):
            acquisition += " · measure power"
        kind, status = self._status(source)
        return dict(step=str(source + 1) if source >= 0 else "—", status=status, statusKind=kind,
                    condition=condition, loop=_context(context, self._plan.get("param_order") or ()),
                    gate=_gate(task), acquisition=acquisition, sourceIndex=source)

    def _condition_label(self, task):
        return str(task.get("row", {}).get("condition_label") or f"Row {int(task.get('row_i', 0)) + 1}")

    def _step_title(self, source):
        if source < 0:
            return "Skipped combination"
        return f"{'Spectrum' if self._nested else 'Sweep'} {source + 1:,} / {len(self._schedule):,}"

    def _position_text(self, task):
        if task.get("nested"):
            point = task.get("gate_point", {})
            return (f"Repeat {int(task.get('repeat_i', 0)) + 1} / {_positive(task.get('repeat_total', 1))}"
                    f" · Point {int(point.get('point_i', 0)) + 1} / {_positive(point.get('point_total', 1))}")
        row = task.get("row", {})
        return f"{_positive(row.get('frames', 1)):,} frames × {_positive(row.get('repeat', 1)):,} repeats"

    def _setting_fields(self, context):
        keys = dict.fromkeys([*(self._plan.get("param_order") or ()), *context])
        return [dict(label=_READABLE_LABELS.get(key, key), value=_setting_value(key, context[key]))
                for key in keys if key in context]

    def _order_levels(self, task):
        if task.get("nested"):
            return [level for level in task.get("execution_order", [])
                    if level.get("kind") in {"group", "conditions", "points"}]
        context = dict(kind="loop", label="Loop settings")
        condition = dict(kind="conditions", label="Condition")
        outer = [condition, context] if self._plan.get("acquisition_grouping") == "batch_first" else [context, condition]
        return [*outer, dict(kind="repeat"), dict(kind="points")]

    def _group_values(self, task, level):
        values = task.get("group_values", {}).get(level.get("id"))
        if isinstance(values, Mapping):
            return values
        context = task.get("ctx", {})
        return {key: context[key] for key in level.get("parameters", []) if key in context}

    def _order_path(self, source, task):
        """Describe this task's actual nesting without expanding any other task."""
        row, path = task.get("row", {}), []
        live = (source >= 0 and source == int(self._plan.get("current_seq_i", -1))
                and self._plan.get("run_outcome") in {"running", "failed", "stopped"})
        for level in self._order_levels(task):
            kind, label, value, detail, fields = level["kind"], "", "", "", []
            if kind == "group":
                fields = self._setting_fields(self._group_values(task, level))
                if len(fields) == 1:
                    label, value = fields[0]["label"], fields[0]["value"]
                    fields = []
                elif fields:
                    label, detail = "Linked settings", "Values move together"
                else:
                    label, value = str(level.get("label") or "Loop group"), "Not specified"
                occurrence = _number(task.get("group_instances", {}).get(level.get("id")))
                if occurrence is not None and occurrence >= 0:
                    detail = " · ".join(filter(None, (detail, f"Occurrence {int(occurrence) + 1}")))
            elif kind == "loop":
                label, fields = "Loop settings", self._setting_fields(task.get("ctx", {}))
                if not fields:
                    value = "No loop variables"
            elif kind == "conditions":
                label, value = "Condition", self._condition_label(task)
                if task.get("nested"):
                    detail = f"Repeat {int(task.get('repeat_i', 0)) + 1} / {_positive(task.get('repeat_total', 1))}"
            elif kind == "repeat":
                label = "Repeat"
                total = _positive(row.get("repeat", 1))
                value = f"{int(self._plan.get('current_rep_i', 0)) + 1} / {total}" if live else f"{total:,} repeats"
            elif kind == "points":
                label, fields = "Gate points", _gate_settings(task)
                if task.get("nested"):
                    point = task.get("gate_point", {})
                    value = f"Point {int(point.get('point_i', 0)) + 1} / {_positive(point.get('point_total', 1))}"
                elif live:
                    total = _positive(self._plan.get("current_frame_total") or row.get("frames", 1))
                    value = f"Frame {max(0, int(self._plan.get('current_frame_i', 0)))} / {total}"
                else:
                    value = f"{_positive(row.get('frames', 1)):,} points per sweep"
            path.append(dict(label=label, value=value, detail=detail, kind=kind, fields=fields))
        path.append(dict(label="Acquire", value="No acquisition" if source < 0 else "1 spectrum",
                         detail="Skipped by When" if source < 0 else "Power " + ("On" if _enabled(row.get("MeasurePower", False)) else "Off"),
                         kind="acquire", fields=[]))
        return path

    def _level_state(self, task, level):
        kind = level["kind"]
        if kind == "group":
            return (self._group_values(task, level), task.get("group_instances", {}).get(level.get("id")))
        if kind == "loop":
            return task.get("ctx", {})
        if kind == "conditions":
            return (task.get("row_i"), self._condition_label(task), task.get("repeat_i") if task.get("nested") else None)
        if kind == "points":
            return task.get("gate_point") if task.get("nested") else _gate_settings(task)
        if kind == "repeat":
            return _positive(task.get("row", {}).get("repeat", 1))
        return None

    def _transition_text(self, task, next_task, next_source, changes):
        """Name the outermost observed change; only explicit zero indices prove a return."""
        levels, next_levels = self._order_levels(task), self._order_levels(next_task)
        if [(item.get("kind"), item.get("id")) for item in levels] != [(item.get("kind"), item.get("id")) for item in next_levels]:
            return "Next execution order differs; review the next step"
        outer = next((index for index, level in enumerate(levels)
                      if not _same(self._level_state(task, level), self._level_state(next_task, next_levels[index]))), None)
        if outer is None:
            return "Next: " + (changes[0] if changes else "acquire at the same requested values")
        next_path = self._order_path(next_source, next_task)
        node = next_path[outer]
        value = node["value"] or ", ".join(f"{field['label']} {field['value']}" for field in node["fields"])
        text = f"Next {node['label']}: {value}"
        if node["kind"] == "conditions" and node["detail"]:
            text += " · " + node["detail"]
        if node["kind"] == "group" and _same(self._group_values(task, levels[outer]), self._group_values(next_task, next_levels[outer])):
            text += " (new occurrence, same values)"
        returns = []
        for index in range(outer, len(levels)):
            level = levels[index]
            if level["kind"] == "conditions" and task.get("nested"):
                before_row, after_row = _number(task.get("row_i")), _number(next_task.get("row_i"))
                if index > outer and before_row is not None and before_row > 0 and after_row == 0:
                    returns.append("Condition returns to Row 1")
                before, after = _number(task.get("repeat_i")), _number(next_task.get("repeat_i"))
                if before is not None and before > 0 and after == 0:
                    returns.append("Repeat returns to 1")
            if index <= outer:
                continue
            if level["kind"] == "points" and task.get("nested"):
                before = _number(task.get("gate_point", {}).get("point_i"))
                after = _number(next_task.get("gate_point", {}).get("point_i"))
                if before is not None and before > 0 and after == 0:
                    returns.append("Gate point returns to 1")
            elif level["kind"] == "group":
                before = _number(task.get("group_instances", {}).get(level.get("id")))
                after = _number(next_task.get("group_instances", {}).get(level.get("id")))
                if before is not None and before > 0 and after == 0:
                    name = next_path[index]["label"]
                    returns.append(f"{name} returns to its first occurrence")
        return " · ".join([text, *returns])

    def _changes(self, previous, task, *, readable=True):
        if previous is None:
            return []
        changes = []
        a, b = previous.get("ctx", {}), task.get("ctx", {})
        context_changed = not _same(a, b)
        missing = object()
        for key in dict.fromkeys([*a, *b]):
            if not _same(a.get(key, missing), b.get(key, missing)):
                label = (_READABLE_LABELS if readable else _ALIASES).get(key, key)
                av = _setting_value(key, a.get(key)) if readable else _value(a.get(key))
                bv = _setting_value(key, b.get(key)) if readable else _value(b.get(key))
                changes.append(f"{label}: {av} → {bv}")
        before_gate, after_gate = _gate_settings(previous), _gate_settings(task)
        # Compare raw targets too: rounded display strings must not suppress a change.
        before_targets = previous.get("gate_point") or {key: value for key, value in previous.get("row", {}).items() if key.startswith(("Vtg_", "Vbg_", "Vbias_"))}
        after_targets = task.get("gate_point") or {key: value for key, value in task.get("row", {}).items() if key.startswith(("Vtg_", "Vbg_", "Vbias_"))}
        axes = ("Vtg", "Vbg", "Vbias")
        for axis in axes:
            before = {key: value for key, value in before_targets.items() if key == axis or key.startswith(axis + "_")}
            after = {key: value for key, value in after_targets.items() if key == axis or key.startswith(axis + "_")}
            if not _same(before, after):
                av = next((item["value"] for item in before_gate if item["label"] == axis), "Not set")
                bv = next((item["value"] for item in after_gate if item["label"] == axis), "Not set")
                changes.append(f"{axis}: {av} → {bv}")
        before_point, after_point = previous.get("gate_point", {}), task.get("gate_point", {})
        if previous.get("nested") and task.get("nested") and not _same(
                (before_point.get("point_i", 0), before_point.get("point_total", 1)),
                (after_point.get("point_i", 0), after_point.get("point_total", 1))):
            changes.append(f"Point: {int(before_point.get('point_i', 0)) + 1}/{_positive(before_point.get('point_total', 1))}"
                           f" → {int(after_point.get('point_i', 0)) + 1}/{_positive(after_point.get('point_total', 1))}")
        if not _same(previous.get("row_i"), task.get("row_i")) or self._condition_label(previous) != self._condition_label(task):
            changes.append(f"Condition: {self._condition_label(previous)} → {self._condition_label(task)}")
        if not _same(previous.get("repeat_i"), task.get("repeat_i")):
            changes.append(f"Repeat: {int(previous.get('repeat_i', 0)) + 1} → {int(task.get('repeat_i', 0)) + 1}")
        before_row, after_row = previous.get("row", {}), task.get("row", {})
        for key, label in (("frames", "Frames"), ("repeat", "Repeats")):
            av, bv = _positive(before_row.get(key, 1)), _positive(after_row.get(key, 1))
            if av != bv:
                changes.append(f"{label}: {av} → {bv}")
        if not _same(previous.get("repeat_total", 1), task.get("repeat_total", 1)):
            changes.append(f"Repeat count: {_positive(previous.get('repeat_total', 1))} → {_positive(task.get('repeat_total', 1))}")
        av, bv = str(before_row.get("When", "") or "Always"), str(after_row.get("When", "") or "Always")
        if av != bv:
            changes.append(f"When: {av} → {bv}")
        av, bv = _enabled(before_row.get("MeasurePower", False)), _enabled(after_row.get("MeasurePower", False))
        if av != bv:
            changes.append(f"Power: {'On' if av else 'Off'} → {'On' if bv else 'Off'}")
        if not context_changed and not _same(previous.get("group_instances", {}), task.get("group_instances", {})):
            changes.append("New loop occurrence (same values)")
        return changes

    def _focus_step(self, source, task, formatted, changes):
        current = int(self._plan.get("current_seq_i", -1))
        outcome = self._plan.get("run_outcome", "idle")
        if source == current and outcome in {"running", "failed", "stopped"}:
            caption = {"running": "Current step", "failed": "Failed step", "stopped": "Stopped step"}[outcome]
        elif not self._follow:
            caption = "Selected step"
        elif outcome == "completed" and source == len(self._schedule) - 1:
            caption = "Last step"
        elif source == 0:
            caption = "First step"
        elif source == len(self._schedule) - 1:
            caption = "Last step"
        else:
            caption = "Selected step"
        settings = self._setting_fields(task.get("ctx", {}))
        acquisition = "1 spectrum" if task.get("nested") else "Full gate sweep · " + self._position_text(task)
        if source < 0:
            acquisition = "No acquisition · When rule did not match"
        if source == current and outcome == "running" and not task.get("nested"):
            acquisition += (f" · repeat {int(self._plan.get('current_rep_i', 0)) + 1}"
                            f" · frame {int(self._plan.get('current_frame_i', 0))}/{int(self._plan.get('current_frame_total', 0))}")
        acquisition += " · Power " + ("On" if _enabled(task.get("row", {}).get("MeasurePower", False)) else "Off")
        return dict(caption=caption, stepTitle=self._step_title(source), conditionLabel=self._condition_label(task),
                    status=formatted["status"], statusKind=formatted["statusKind"], positionText=self._position_text(task),
                    acquisitionText=acquisition, settings=settings, gates=_gate_settings(task), changes=changes,
                    orderPath=self._order_path(source, task))

    def _details(self, source, task, formatted, changes=None):
        path = []
        for level in task.get("execution_order", []):
            kind = level.get("kind")
            if kind == "group":
                values = task.get("group_values", {}).get(level.get("id"), {})
                path.append(_context(values))
            elif kind == "conditions":
                path.append(formatted["condition"])
            elif kind == "points":
                point = task.get("gate_point", {})
                path.append(f"Gate point {int(point.get('point_i', 0)) + 1}/{_positive(point.get('point_total', 1))}")
        if not path:
            path = ([formatted["condition"], formatted["loop"]] if self._plan.get("acquisition_grouping") == "batch_first"
                    else [formatted["loop"], formatted["condition"]])
        if changes is None:
            changes = self._changes(self._schedule[source - 1] if source > 0 else None, task, readable=False)
        rule = str(task.get("row", {}).get("When", "") or "Always")
        if source < 0:
            rule = f"Skipped: {rule} did not match this loop setting."
        return dict(title=f"Step {source + 1:,}" if source >= 0 else "Skipped combination", status=formatted["status"],
                    condition=formatted["condition"], loop=formatted["loop"], gate=formatted["gate"],
                    acquisition=formatted["acquisition"], path=" → ".join([*path, "Acquire"]), when=rule,
                    changes="\n".join(changes) if changes else ("Start of sequence" if source == 0 else
                              "No requested setting changes from previous step" if source > 0 else "No acquisition"),
                    pageRow=self._selected)

    def _refresh(self):
        self._page = min(max(0, self._page), self.pageCount - 1)
        start, total = self._page * self.page_size, self._visible_total()
        count = min(self.page_size, max(0, total - start))
        self._selected = min(max(0, self._selected), max(0, count - 1))
        rows, detail, focus, following = [], {}, {}, {}
        for offset in range(count):
            source, task = self._entry(start + offset)
            formatted = self._formatted(source, task)
            formatted["selected"] = offset == self._selected
            rows.append(formatted)
            if formatted["selected"]:
                previous = self._schedule[source - 1] if source > 0 else None
                changes = self._changes(previous, task)
                detail = self._details(source, task, formatted, self._changes(previous, task, readable=False))
                focus = self._focus_step(source, task, formatted, changes)
                next_source = source + 1
                if source < 0:
                    next_ordinal = bisect_right(self._execution_positions, start + offset)
                    next_source = (self._lookup[self._execution_positions[next_ordinal]]
                                   if next_ordinal < len(self._execution_positions) else len(self._schedule))
                if next_source < len(self._schedule):
                    next_task = self._schedule[next_source]
                    next_changes = self._changes(task, next_task)
                    following = dict(stepTitle=self._step_title(next_source),
                                     conditionLabel=self._condition_label(next_task),
                                     positionText=self._position_text(next_task), changes=next_changes,
                                     transitionText=self._transition_text(task, next_task, next_source, next_changes),
                                     unchangedText=("Other requested values unchanged" if next_changes
                                                    else "Requested values unchanged"))
        old = self._rows
        # Page changes replace only a bounded page; frame updates preserve QML
        # delegates and selection and notify only rows that actually changed.
        identity = lambda values: [(row["sourceIndex"], row["condition"], row["loop"], row["gate"]) for row in values]
        if identity(old) != identity(rows):
            self.beginResetModel()
            self._rows = rows
            self.endResetModel()
        else:
            self._rows = rows
            for index, (a, b) in enumerate(zip(old, rows)):
                if a != b:
                    self.dataChanged.emit(self.index(index), self.index(index), list(self._ROLES))
        self._detail = detail
        self._focus, self._following = focus, following
        overview = self.overview
        publication = (self.summary, self.previewState, self.currentStep, self.pageLabel,
                       self.pageCount, self._follow, self._show_skipped, self.supportsSkipped,
                       self._error, detail, self._filters, self._nesting,
                       overview, focus, following, self.canPreviousStep, self.canNextStep)
        # A shared notify signal makes QML turn every unchanged list/map into a
        # fresh JavaScript value. In particular, progress used to recreate the
        # loop-order and rule delegates, even while only a frame number changed.
        for name, value, signal in (
                ("overview", overview, self.overviewChanged),
                ("detail", detail, self.selectedDetailChanged),
                ("focus", focus, self.focusStepChanged),
                ("following", following, self.followingStepChanged),
                ("filters", self._filters, self.filtersChanged),
                ("nesting", self._nesting, self.nestingChanged)):
            if name not in self._property_publications or value != self._property_publications[name]:
                self._property_publications[name] = value
                signal.emit()
        if publication != self._published:
            self._published = publication
            self.changed.emit()

    @Slot(int)
    def selectRow(self, row):
        if 0 <= row < len(self._rows):
            self._follow = False
            self._selected = row
            self._refresh()

    @Slot(int)
    def goToPage(self, page):
        self._follow = False
        self._page, self._selected = max(0, min(int(page) - 1, self.pageCount - 1)), 0
        self._error = ""
        self._refresh()

    @Slot()
    def previousPage(self):
        self.goToPage(self._page)

    @Slot()
    def nextPage(self):
        self.goToPage(self._page + 2)

    def _move_step(self, delta):
        if not self._rows:
            return
        position = self._page * self.page_size + self._selected
        position = max(0, min(position + delta, self._visible_total() - 1))
        self._follow, self._error = False, ""
        self._page, self._selected = divmod(position, self.page_size)
        self._refresh()

    @Slot()
    def previousStep(self):
        self._move_step(-1)

    @Slot()
    def nextStep(self):
        self._move_step(1)

    @Slot(str)
    def goToStep(self, text):
        try:
            index = int(text.strip()) - 1
            if not 0 <= index < len(self._schedule):
                raise ValueError()
        except (ValueError, TypeError):
            self._error = f"Enter an execution step from 1 to {len(self._schedule):,}."
            self.changed.emit()
            return
        self._follow, self._error = False, ""
        position = self._ordinal(self._schedule[index]) if self._show_skipped else index
        self._page, self._selected = divmod(position, self.page_size)
        self._refresh()

    @Slot(bool)
    def setFollowCurrent(self, enabled):
        self._follow = bool(enabled)
        if self._follow and self._schedule:
            index = max(0, min(len(self._schedule) - 1, int(self._plan.get("current_seq_i", -1))))
            position = self._ordinal(self._schedule[index]) if self._show_skipped else index
            self._page, self._selected = divmod(position, self.page_size)
        self._refresh()

    @Slot(bool)
    def setShowSkipped(self, enabled):
        self._show_skipped = bool(enabled) and self.supportsSkipped
        self._page = self._selected = 0
        self._follow = False
        self._refresh()

    def clear(self):
        self._plan_key = None
        self.update_plan((), [], acquisition_schedule=())
