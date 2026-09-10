"""Small, hardware independent editor for a batch of gate conditions."""
from __future__ import annotations

import copy
import math
from typing import Any, Mapping

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QSizePolicy, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from utils.config import cfg
from utils.mcd_common import (
    MODE_DOPING_EFIELD, MODE_DIRECT, mcd_coordinates, parse_numeric_spec,
    validate_gate_conditions, vtg_vbg_from_doping_efield,
)
from utils.motion_conditions import build_conditions, expand_sequence, resolve_rotation_plan

_ROW_ROLE = Qt.ItemDataRole.UserRole
_FALSE_TEXT = {"0", "false", "no", "off", "unchecked"}


def _number(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _format(value: Any) -> str:
    return f"{float(value):.10g}"


class _RotationPlanProxy:
    """Compatibility facade for callers that historically used ``rot_plan``."""

    def __init__(self, owner: "MotionConditionsWidget") -> None:
        self.owner = owner

    def text(self) -> str:
        return self.owner._selected_rotation_edit().text()

    def setText(self, value: str) -> None:
        self.owner._selected_rotation_edit().setText(str(value))


class MotionConditionsWidget(QWidget):
    """Compact condition calculator and editable condition table."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None, *, voltage_limit: float | None = None) -> None:
        super().__init__(parent)
        self._configured_voltage_limit = voltage_limit
        self._undo: list[list[dict[str, Any]]] = []
        self._busy = False
        self._editing_row: int | None = None
        self._inner_sweep_axis = "stage"
        self._build_ui()
        self._connect_signals()
        self._update_labels()
        self._input_changed()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(4)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(3)
        self._mode_combo = QComboBox()
        self._mode_combo.addItem("Doping / E-field", MODE_DOPING_EFIELD)
        self._mode_combo.addItem("Vtg / Vbg", MODE_DIRECT)
        self.mode = self._mode_combo
        self._ratio_spin = QDoubleSpinBox()
        self._ratio_spin.setRange(-1000.0, 1000.0)
        self._ratio_spin.setDecimals(6)
        self._ratio_spin.setValue(1.0)
        self.ratio = self._ratio_spin
        self._ratio_equation_label = QLabel("D=Vtg+r·Vbg; F=Vtg−r·Vbg")
        self.ratio.setToolTip(self._ratio_equation_label.text())
        self._ratio_equation_label.hide()
        self._a_label = QLabel()
        self._b_label = QLabel()
        self._input_a_edit = QLineEdit("0")
        self._input_b_edit = QLineEdit("0")
        self.a = self._input_a_edit
        self.b = self._input_b_edit
        for edit in (self.a, self.b):
            edit.setPlaceholderText("scalar, [list], or (start, stop, step)")
        coordinate_row = QHBoxLayout()
        coordinate_row.setSpacing(4)
        coordinate_row.addWidget(self.mode, 2)
        coordinate_row.addWidget(QLabel("r:"))
        coordinate_row.addWidget(self.ratio, 1)
        form.addRow("Coordinates:", coordinate_row)
        form.addRow(self._a_label, self.a)
        form.addRow(self._b_label, self.b)
        self._expansion_label = QLabel("Pairing:")
        self._expansion_combo = QComboBox()
        self._expansion_combo.addItem("Paired (broadcast scalar)", "paired")
        self._expansion_combo.addItem("Every combination", "grid")
        self.expansion = self._expansion_combo
        form.addRow(self._expansion_label, self.expansion)
        root.addLayout(form)
        self._preview_label = QLabel()
        self._preview_label.setWordWrap(True)
        root.addWidget(self._preview_label)

        self._advanced_group = QGroupBox("Advanced")
        self._advanced_group.setCheckable(True)
        self._advanced_group.setChecked(False)
        advanced_body = QWidget()
        advanced = QFormLayout(advanced_body)
        advanced.setContentsMargins(6, 2, 6, 2)
        advanced.setSpacing(3)
        self._bias_spin = QDoubleSpinBox()
        self._bias_spin.setRange(-1000.0, 1000.0)
        self._bias_spin.setDecimals(6)
        self.bias = self._bias_spin
        advanced.addRow("Vbias:", self.bias)
        advanced_container = QVBoxLayout(self._advanced_group)
        advanced_container.setContentsMargins(0, 0, 0, 0)
        advanced_container.addWidget(advanced_body)
        self._advanced_body = advanced_body
        self._advanced_body.setVisible(False)
        self._sequence_group = QGroupBox("Outer rotation / sequence")
        self._sequence_group.setToolTip("Optional rotations between complete sweeps. Choose the per-point scan axis at the top of Motion Sweep.")
        sequence_form = QFormLayout(self._sequence_group)
        sequence_form.setContentsMargins(6, 2, 6, 2)
        sequence_form.setSpacing(3)
        self._rotation_axis_combo = QComboBox()
        self._rotation_axis_combo.addItem("None / keep current", "")
        self._rotation_axis_combo.addItem("Rot1", "rot1")
        self._rotation_axis_combo.addItem("Rot2", "rot2")
        self.rot_axis = self._rotation_axis_combo
        self._rot1_plan_edit = QLineEdit("Keep current")
        self._rot2_plan_edit = QLineEdit("Keep current")
        self.rot1_plan = self._rot1_plan_edit
        self.rot2_plan = self._rot2_plan_edit
        self._rotation_plan_edit = self._rot1_plan_edit
        self.rot_plan = _RotationPlanProxy(self)
        # The selector remains as a hidden compatibility control.  Both
        # independent plans are visible, so users can prepare either axis.
        self.rot_axis.setVisible(False)
        rotation_row = QHBoxLayout()
        rotation_row.setSpacing(4)
        rotation_row.addWidget(self.rot1_plan, 1)
        rotation_row.addWidget(QLabel("Rot2:"))
        rotation_row.addWidget(self.rot2_plan, 1)
        for edit in (self.rot1_plan, self.rot2_plan):
            edit.setToolTip("Keep current, Fixed: angle, or an angle list / range")
        sequence_form.addRow("Rot1:", rotation_row)
        self._rotation_settle_spin = QDoubleSpinBox()
        self._rotation_settle_spin.setRange(0.0, 3600.0)
        self._rotation_settle_spin.setDecimals(3)
        self._rotation_settle_spin.setSuffix(" s")
        self.rotation_settle = self._rotation_settle_spin
        self._order_combo = QComboBox()
        self._order_combo.addItem("Gate first", "gate-first")
        self._order_combo.addItem("Rotation first", "rotation-first")
        self.order = self._order_combo
        self._repeats_spin = QSpinBox()
        self._repeats_spin.setRange(1, 1000)
        self.repeats = self._repeats_spin
        order_row = QHBoxLayout()
        order_row.setSpacing(4)
        order_row.addWidget(self.order, 2)
        order_row.addWidget(QLabel("Repeats:"))
        order_row.addWidget(self.repeats, 1)
        sequence_form.addRow("Order:", order_row)
        timing_row = QHBoxLayout()
        timing_row.setSpacing(5)
        timing_row.addWidget(self.rotation_settle)
        sequence_form.addRow("Settle:", timing_row)
        root.addWidget(self._advanced_group)
        root.addWidget(self._sequence_group)

        self._add_button = QPushButton("Add conditions")
        self._duplicate_button = QPushButton("Duplicate")
        self._remove_button = QPushButton("Remove")
        self._up_button = QPushButton("Move up")
        self._down_button = QPushButton("Move down")
        self._undo_button = QPushButton("Undo")
        self._edit_button = QPushButton("Edit")
        self._update_button = QPushButton("Update")
        self._cancel_button = QPushButton("Cancel")
        buttons = QGridLayout()
        buttons.setHorizontalSpacing(3)
        buttons.setVerticalSpacing(3)
        for index, button in enumerate((self._add_button, self._edit_button, self._update_button,
                                        self._cancel_button, self._duplicate_button, self._remove_button,
                                        self._up_button, self._down_button, self._undo_button)):
            button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            buttons.addWidget(button, index // 3, index % 3)
        root.addLayout(buttons)
        self._table = QTableWidget(0, 7)
        self._table.setHorizontalHeaderLabels(["Use", "Condition", "D", "F", "Vtg", "Vbg", "Vbias"])
        # Reserve room for five submitted conditions in the narrow control
        # column so the table remains usable without opening a child tab.
        self._table.setMinimumHeight(180)
        self._table.setMaximumHeight(240)
        self._table.setMinimumWidth(0)
        self._table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._table.setAlternatingRowColors(True)
        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        for column, width in ((0, 34), (1, 70), (2, 42), (3, 42), (4, 50), (5, 50), (6, 50)):
            header.setSectionResizeMode(column, header.ResizeMode.Interactive)
            self._table.setColumnWidth(column, width)
        header.setSectionResizeMode(1, header.ResizeMode.Stretch)
        self.table = self._table
        root.addWidget(self.table)
        self._status_label = QLabel()
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #b3261e;")
        root.addWidget(self._status_label)

    def _connect_signals(self) -> None:
        self.mode.currentIndexChanged.connect(self._update_labels)
        self.a.textChanged.connect(self._input_changed)
        self.b.textChanged.connect(self._input_changed)
        self.expansion.currentIndexChanged.connect(self._input_changed)
        self.ratio.valueChanged.connect(self._ratio_changed)
        self.bias.valueChanged.connect(self._input_changed)
        self.rot_axis.currentIndexChanged.connect(self._rotation_changed)
        self.rot1_plan.textChanged.connect(self._rotation_changed)
        self.rot2_plan.textChanged.connect(self._rotation_changed)
        self.rotation_settle.valueChanged.connect(self._rotation_changed)
        self.order.currentIndexChanged.connect(self._sequence_changed)
        self.repeats.valueChanged.connect(self._sequence_changed)
        self._advanced_group.toggled.connect(self._sequence_changed)
        self._advanced_group.toggled.connect(self._advanced_body.setVisible)
        self._add_button.clicked.connect(self.add_batch)
        self._duplicate_button.clicked.connect(self.duplicate)
        self._remove_button.clicked.connect(self.remove)
        self._up_button.clicked.connect(lambda: self.reorder(-1))
        self._down_button.clicked.connect(lambda: self.reorder(1))
        self._undo_button.clicked.connect(self.undo_add)
        self._edit_button.clicked.connect(self.begin_edit)
        self._update_button.clicked.connect(self.update_row)
        self._cancel_button.clicked.connect(self.cancel_edit)
        self.table.itemChanged.connect(self._item_changed)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self._update_button.setEnabled(False); self._cancel_button.setEnabled(False)

    def _voltage_limit(self) -> float:
        if self._configured_voltage_limit is not None:
            return abs(_number(self._configured_voltage_limit, "Voltage compliance"))
        return abs(_number(getattr(getattr(cfg, "smu", None), "volt_compliance_V", 20.0), "Voltage compliance"))

    def _update_labels(self, *_: Any) -> None:
        direct = self.mode.currentData() == MODE_DIRECT
        self._a_label.setText("Vtg values:" if direct else "Doping values:")
        self._b_label.setText("Vbg values:" if direct else "E-field values:")
        self._set_derived_flags()
        self._input_changed()

    def _set_derived_flags(self) -> None:
        direct = self.mode.currentData() == MODE_DIRECT
        for row in range(self.table.rowCount()):
            for column in (2, 3, 4, 5):
                item = self.table.item(row, column)
                if item is None:
                    continue
                editable = (column in (4, 5)) if direct else (column in (2, 3))
                flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                if editable:
                    flags |= Qt.ItemFlag.ItemIsEditable
                item.setFlags(flags)

    def _parsed_inputs(self) -> tuple[list[float], list[float]]:
        direct = self.mode.currentData() == MODE_DIRECT
        first, second = (("Vtg", "Vbg") if direct else ("Doping", "E-field"))
        return parse_numeric_spec(self.a.text(), first), parse_numeric_spec(self.b.text(), second)

    def _pending(self) -> list[dict[str, Any]]:
        return build_conditions(self.mode.currentData(), self.a.text(), self.b.text(),
                                self.expansion.currentData(), self.ratio.value(), self.bias.value(),
                                voltage_limit=self._voltage_limit())

    def _input_changed(self, *_: Any) -> None:
        try:
            values_a, values_b = self._parsed_inputs()
            show_pairing = len(values_a) > 1 and len(values_b) > 1
            self._expansion_label.setVisible(show_pairing)
            self.expansion.setVisible(show_pairing)
            rows = self._pending()
            self._add_button.setEnabled(True)
            self._add_button.setText(f"Add {len(rows)} condition" + ("" if len(rows) == 1 else "s"))
            sample_a = ", ".join(_format(value) for value in values_a[:4])
            sample_b = ", ".join(_format(value) for value in values_b[:4])
            suffix_a, suffix_b = (", …" if len(values_a) > 4 else ""), (", …" if len(values_b) > 4 else "")
            names = ("Vtg", "Vbg") if self.mode.currentData() == MODE_DIRECT else ("D", "F")
            self._preview_label.setText(f"Pending: {names[0]}=[{sample_a}{suffix_a}], {names[1]}=[{sample_b}{suffix_b}] → {len(rows)} condition(s)")
            self._status_label.clear()
        except Exception as exc:
            self._add_button.setEnabled(False)
            self._add_button.setText("Add conditions")
            self._preview_label.setText("Pending: invalid")
            self._status_label.setText(str(exc))
            # Keep pairing available when both arrays parsed successfully but
            # paired expansion is invalid (for example unequal list lengths),
            # so the user can switch to Every combination.
            if "values_a" not in locals() or "values_b" not in locals():
                self._expansion_label.setVisible(False)
                self.expansion.setVisible(False)
        self.changed.emit()

    # --------------------------------------------------------------- row data
    @staticmethod
    def _condition_bundle(row: Mapping[str, Any]) -> dict[str, Any]:
        condition = copy.deepcopy(dict(row))
        return {"condition": condition, "provenance": copy.deepcopy(condition.get("provenance", {})), "edited": False}

    def _append_row(self, row: Mapping[str, Any], *, label: str | None = None) -> None:
        index = self.table.rowCount()
        self.table.insertRow(index)
        values = ["", label if label is not None else f"Condition {index + 1}",
                  _format(row.get("doping_v", 0.0)), _format(row.get("efield_v", 0.0)),
                  _format(row.get("vtg_v", 0.0)), _format(row.get("vbg_v", 0.0)),
                  _format(row.get("vbias_v", 0.0))]
        bundle = self._condition_bundle(row)
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            if column == 0:
                item.setCheckState(Qt.CheckState.Checked if row.get("enabled", True) else Qt.CheckState.Unchecked)
            if column == 1:
                item.setData(_ROW_ROLE, copy.deepcopy(bundle))
            self.table.setItem(index, column, item)

    def _row_bundle(self, row: int) -> dict[str, Any]:
        item = self.table.item(row, 1)
        data = item.data(_ROW_ROLE) if item is not None else None
        return copy.deepcopy(data) if isinstance(data, dict) else {"condition": {}, "provenance": {}, "edited": False}

    def _is_enabled(self, row: int) -> bool:
        item = self.table.item(row, 0)
        return bool(item and item.checkState() == Qt.CheckState.Checked and item.text().strip().lower() not in _FALSE_TEXT)

    def _read_row(self, row: int) -> dict[str, Any]:
        values = []
        for column, label in zip(range(2, 7), ("D", "F", "Vtg", "Vbg", "Vbias")):
            item = self.table.item(row, column)
            if item is None:
                raise ValueError(f"Condition row {row + 1} is incomplete ({label})")
            values.append(_number(item.text().strip(), f"Condition row {row + 1} {label}"))
        bundle = self._row_bundle(row)
        if bundle.get("error"):
            raise ValueError(f"Condition row {row + 1}: {bundle['error']}")
        expected_d, expected_f = mcd_coordinates(values[2], values[3], self.ratio.value())
        if abs(expected_d - values[0]) > 1e-7 or abs(expected_f - values[1]) > 1e-7:
            raise ValueError(f"Condition row {row + 1} D/F do not match Vtg/Vbg at r={self.ratio.value():g}")
        label_item = self.table.item(row, 1)
        return {"enabled": self._is_enabled(row), "mode": MODE_DIRECT,
                "label": label_item.text() if label_item is not None else "",
                "input_a": values[2], "input_b": values[3], "gate_ratio": self.ratio.value(),
                "doping_v": values[0], "efield_v": values[1],
                "vtg_v": values[2], "vbg_v": values[3],
                "vbias_v": values[4], "provenance": self._row_bundle(row).get("provenance", {})}

    def _snapshot(self) -> list[dict[str, Any]]:
        snapshot = []
        for row in range(self.table.rowCount()):
            values = [self.table.item(row, column).text() if self.table.item(row, column) else "" for column in range(7)]
            item = self.table.item(row, 0)
            snapshot.append({"values": values, "checked": bool(item and item.checkState() == Qt.CheckState.Checked),
                             "bundle": self._row_bundle(row)})
        return copy.deepcopy(snapshot)

    def _restore(self, snapshot: list[Any]) -> None:
        self._busy = True
        try:
            with QSignalBlocker(self.table):
                self.table.setRowCount(0)
                for entry in snapshot or []:
                    if isinstance(entry, dict) and "values" in entry:
                        values = list(entry.get("values", [])); bundle = copy.deepcopy(entry.get("bundle", {}))
                        checked = bool(entry.get("checked", True))
                    else:
                        values = list(entry) if isinstance(entry, (list, tuple)) else []
                        checked = bool(values and str(values[0]).strip().lower() not in _FALSE_TEXT)
                        bundle = {"condition": {}, "provenance": {}, "edited": False}
                    values.extend([""] * (7 - len(values)))
                    row = self.table.rowCount(); self.table.insertRow(row)
                    for column in range(7):
                        # The checkbox is the source of truth; keeping the
                        # legacy text out of this column avoids a distracting
                        # literal ``1`` beside every checkbox.
                        item = QTableWidgetItem("" if column == 0 else str(values[column]))
                        if column == 0:
                            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                        if column == 1:
                            item.setData(_ROW_ROLE, bundle)
                        self.table.setItem(row, column, item)
        finally:
            self._busy = False

    def _push_undo(self) -> None:
        self._undo.append(self._snapshot())
        if len(self._undo) > 50:
            self._undo.pop(0)

    # --------------------------------------------------------------- mutations
    def add_batch(self) -> None:
        try:
            rows = self._pending()
        except Exception:
            self._input_changed(); return
        self._push_undo(); self._busy = True
        try:
            with QSignalBlocker(self.table):
                for row in rows: self._append_row(row)
        finally:
            self._busy = False
        self._set_derived_flags()
        self._input_changed()

    def _selection_changed(self) -> None:
        if self._busy or self.table.currentRow() < 0:
            return
        self.begin_edit()

    def begin_edit(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        try:
            self._editing_row = row
            self.mode.setCurrentIndex(self.mode.findData(MODE_DIRECT))
            self.a.setText(self.table.item(row, 4).text())
            self.b.setText(self.table.item(row, 5).text())
            self.bias.setValue(float(self.table.item(row, 6).text()))
            self._add_button.setEnabled(False); self._update_button.setEnabled(True); self._cancel_button.setEnabled(True)
            self._status_label.setText(f"Editing condition row {row + 1}; Update applies one scalar condition.")
        except Exception as exc:
            self._status_label.setText(str(exc))

    def update_row(self) -> None:
        row = self._editing_row
        if row is None or row >= self.table.rowCount():
            return
        try:
            pending = self._pending()
            if len(pending) != 1:
                raise ValueError("Update requires one scalar condition; use Add for multiple values")
            condition = pending[0]; label = self.table.item(row, 1).text() if self.table.item(row, 1) else ""
            self._push_undo(); self._busy = True
            with QSignalBlocker(self.table):
                for column, value in ((2, condition.get("doping_v", 0.0)), (3, condition.get("efield_v", 0.0)),
                                      (4, condition.get("vtg_v", 0.0)), (5, condition.get("vbg_v", 0.0)),
                                      (6, condition.get("vbias_v", 0.0))):
                    self.table.item(row, column).setText(_format(value))
                item = self.table.item(row, 1)
                if item is not None:
                    item.setData(_ROW_ROLE, self._condition_bundle({**condition, "enabled": self._is_enabled(row)}))
            self._busy = False; self._editing_row = None
            self._update_button.setEnabled(False); self._cancel_button.setEnabled(False); self._add_button.setEnabled(True)
            self._input_changed()
        except Exception as exc:
            self._busy = False; self._status_label.setText(str(exc))

    def cancel_edit(self) -> None:
        self._editing_row = None
        self._update_button.setEnabled(False); self._cancel_button.setEnabled(False); self._add_button.setEnabled(True)
        self._status_label.clear(); self._input_changed()

    def add_row(self) -> None:
        """Append one editable direct row for callers building a table manually."""
        self._push_undo()
        row = {"enabled": True, "mode": MODE_DIRECT, "input_a": 0.0, "input_b": 0.0,
               "vtg_v": 0.0, "vbg_v": 0.0, "doping_v": 0.0, "efield_v": 0.0,
               "vbias_v": 0.0, "provenance": {"mode": MODE_DIRECT, "edited": True}}
        self._busy = True
        try:
            with QSignalBlocker(self.table):
                self._append_row(row, label=f"Condition {self.table.rowCount() + 1}")
        finally:
            self._busy = False
        self.table.selectRow(self.table.rowCount() - 1)
        self.changed.emit()
        try:
            self.conditions(enabled_only=False)
        except Exception as exc:
            self._status_label.setText(str(exc))

    def duplicate(self) -> None:
        row = self.table.currentRow()
        if row < 0: return
        self._push_undo(); rows = self._snapshot(); rows.append(copy.deepcopy(rows[row]))
        self._restore(rows); self.table.selectRow(self.table.rowCount() - 1); self.changed.emit()

    def remove(self) -> None:
        row = self.table.currentRow()
        if row < 0: return
        self._push_undo(); self._busy = True
        try:
            with QSignalBlocker(self.table): self.table.removeRow(row)
        finally: self._busy = False
        self.changed.emit()

    def reorder(self, delta: int) -> None:
        row = self.table.currentRow(); target = row + int(delta)
        if row < 0 or target < 0 or target >= self.table.rowCount(): return
        self._push_undo(); rows = self._snapshot(); rows.insert(target, rows.pop(row)); self._restore(rows)
        self.table.selectRow(target); self.changed.emit()

    def undo_add(self) -> None:
        if self._undo:
            self._restore(self._undo.pop()); self.changed.emit()

    # ------------------------------------------------------------- row editing
    def _set_numeric_row(self, row: int, values: Mapping[int, float]) -> None:
        self._busy = True
        try:
            with QSignalBlocker(self.table):
                for column, value in values.items():
                    if self.table.item(row, column) is not None:
                        self.table.item(row, column).setText(_format(value))
                item = self.table.item(row, 1)
                if item is not None:
                    bundle = self._row_bundle(row); bundle["edited"] = True; bundle.pop("error", None)
                    bundle["provenance"] = {"mode": MODE_DIRECT,
                        "input_a": _number(self.table.item(row, 4).text(), "Vtg"),
                        "input_b": _number(self.table.item(row, 5).text(), "Vbg"),
                        "gate_ratio": self.ratio.value(), "equation": "Manually edited table row", "edited": True}
                    item.setData(_ROW_ROLE, bundle)
        finally:
            self._busy = False

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self._busy: return
        row, column = item.row(), item.column()
        if column in (0, 1):
            self.changed.emit(); return
        if column not in (2, 3, 4, 5, 6): return
        try:
            values = [_number(self.table.item(row, index).text(), ("D", "F", "Vtg", "Vbg", "Vbias")[index - 2]) for index in range(2, 7)]
            if column in (2, 3):
                values[2], values[3] = vtg_vbg_from_doping_efield(values[0], values[1], self.ratio.value())
                derived = {4: values[2], 5: values[3]}
            elif column in (4, 5):
                values[0], values[1] = mcd_coordinates(values[2], values[3], self.ratio.value())
                derived = {2: values[0], 3: values[1]}
            else:
                derived = {}
            self._set_numeric_row(row, {**derived, column: values[column - 2]})
            validate_gate_conditions([self._read_row(row)], self._voltage_limit())
            self._status_label.clear()
        except Exception as exc:
            bundle = self._row_bundle(row); bundle["error"] = str(exc)
            label_item = self.table.item(row, 1)
            if label_item is not None:
                label_item.setData(_ROW_ROLE, bundle)
            self._status_label.setText(f"Condition row {row + 1}: {exc}")
        self.changed.emit()

    def _ratio_changed(self, *_: Any) -> None:
        if self._busy: return
        self._busy = True
        try:
            with QSignalBlocker(self.table):
                for row in range(self.table.rowCount()):
                    try:
                        vtg = _number(self.table.item(row, 4).text(), f"Condition row {row + 1} Vtg")
                        vbg = _number(self.table.item(row, 5).text(), f"Condition row {row + 1} Vbg")
                        doping, efield = mcd_coordinates(vtg, vbg, self.ratio.value())
                        self.table.item(row, 2).setText(_format(doping)); self.table.item(row, 3).setText(_format(efield))
                        bundle = self._row_bundle(row); bundle.pop("error", None)
                        self.table.item(row, 1).setData(_ROW_ROLE, bundle)
                    except Exception as exc:
                        bundle = self._row_bundle(row); bundle["error"] = str(exc)
                        self.table.item(row, 1).setData(_ROW_ROLE, bundle)
                        self._status_label.setText(f"Condition row {row + 1}: {exc}")
        finally:
            self._busy = False
        self._input_changed()

    # --------------------------------------------------------------- extraction
    def conditions(self, *, enabled_only: bool = True) -> list[dict[str, Any]]:
        result, errors = [], []
        for row in range(self.table.rowCount()):
            try:
                condition = self._read_row(row)
                validate_gate_conditions([condition], self._voltage_limit())
                if not enabled_only or condition["enabled"]: result.append(condition)
            except Exception as exc:
                errors.append(str(exc))
        if errors: raise ValueError("; ".join(errors))
        return result

    def _selected_rotation_edit(self, axis: str | None = None) -> QLineEdit:
        return self.rot2_plan if (axis or self.rot_axis.currentData()) == "rot2" else self.rot1_plan

    def rotation_plan(self, axis: str | None = None, current: Mapping[str, float] | None = None) -> dict[str, Any]:
        selected = axis or self.rot_axis.currentData() or "rot1"
        if selected not in ("rot1", "rot2") or selected == self._inner_sweep_axis:
            return {"mode": "keep", "axis": selected, "values": [], "requested": None}
        return resolve_rotation_plan(self._selected_rotation_edit(selected).text(), current or {}, axis=selected)

    def rotation_plans(self, current: Mapping[str, float] | None = None) -> dict[str, dict[str, Any]]:
        current = current or {}
        return {axis: self.rotation_plan(axis, current) for axis in ("rot1", "rot2")}

    def set_inner_sweep_axis(self, axis: str) -> None:
        """Exclude the per-point sweep axis without overwriting saved outer plans."""
        changed = axis != self._inner_sweep_axis
        self._inner_sweep_axis = axis
        for name, edit in (("rot1", self.rot1_plan), ("rot2", self.rot2_plan)):
            active = name == axis
            edit.setEnabled(not active)
            edit.setToolTip(
                f"{name} is swept above; this saved outer plan is ignored."
                if active else "Keep current, Fixed: angle, or an angle list / range"
            )
        self._sequence_group.setTitle(
            f"Outer rotation / sequence ({axis} swept above)"
            if axis in ("rot1", "rot2") else "Outer rotation / sequence"
        )
        if changed:
            self.changed.emit()

    def sequence(self, rotations: Any = (0.0,), rotation2: Any = None) -> list[dict[str, int]]:
        return expand_sequence(self.conditions(), rotations, rotation2=rotation2,
                               order=self.order.currentData(), repeats=self.repeats.value())

    def state(self) -> dict[str, Any]:
        return {"mode": self.mode.currentData(), "input_a": self.a.text(), "input_b": self.b.text(),
                "expansion": self.expansion.currentData(), "ratio": self.ratio.value(), "vbias": self.bias.value(),
                "rotation_axis": self.rot_axis.currentData(), "rotation_plan": self.rot_plan.text(),
                "rotation_plans": {"rot1": self.rot1_plan.text(), "rot2": self.rot2_plan.text()},
                "rotation_settle_s": self.rotation_settle.value(), "order": self.order.currentData(),
                "repeats": self.repeats.value(), "rows": self._snapshot()}

    def restore_state(self, state: Mapping[str, Any]) -> None:
        self._busy = True
        try:
            for combo, key in ((self.mode, "mode"), (self.expansion, "expansion"),
                               (self.rot_axis, "rotation_axis"), (self.order, "order")):
                index = combo.findData(state.get(key))
                if index >= 0: combo.setCurrentIndex(index)
            self.a.setText(str(state.get("input_a", "0"))); self.b.setText(str(state.get("input_b", "0")))
            self.ratio.setValue(float(state.get("ratio", 1.0))); self.bias.setValue(float(state.get("vbias", 0.0)))
            plans = state.get("rotation_plans", {})
            self.rot1_plan.setText(str(plans.get("rot1", "Keep current")) if isinstance(plans, Mapping) else "Keep current")
            self.rot2_plan.setText(str(plans.get("rot2", "Keep current")) if isinstance(plans, Mapping) else "Keep current")
            if "rotation_plans" not in state and "rotation_plan" in state:
                self._selected_rotation_edit().setText(str(state.get("rotation_plan")))
            self.rotation_settle.setValue(float(state.get("rotation_settle_s", 0.0)))
            self.repeats.setValue(int(state.get("repeats", 1))); self._restore(list(state.get("rows", [])))
        finally:
            self._busy = False
        self._set_derived_flags()
        self._input_changed()

    def _rotation_changed(self, *_: Any) -> None:
        self.changed.emit()

    def _sequence_changed(self, *_: Any) -> None:
        self.changed.emit()


__all__ = ["MotionConditionsWidget"]
