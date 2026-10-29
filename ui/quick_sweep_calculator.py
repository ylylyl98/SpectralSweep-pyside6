"""Keep calculator equations, bounds and outputs in explicit native grids.

Quick edits the original scalar controls inside each cell. The calculator still
owns all validation, generated rows, state restoration and instrument interlocks.
"""
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)

from ui.style_roles import set_style_role


def _boundary(widget):
    widget.setProperty("quickSectionBoundary", True)
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
    return widget


def _label(text, *, muted=False):
    label = QLabel(text)
    label.setProperty("quickSectionBoundary", True)
    label.setWordWrap(True)
    if muted:
        set_style_role(label, 'text', tone='muted')
    return label


def _field(source, name, *, minimum=0):
    hidden = source.isHidden()
    cell = _boundary(QWidget())
    cell.setObjectName("QuickCalculator" + name.replace(" ", ""))
    cell.setMinimumWidth(minimum)
    layout = QVBoxLayout(cell)
    layout.setContentsMargins(0, 0, 0, 0)
    source.setMinimumWidth(0)
    source.setMaximumWidth(16777215)
    source.setProperty("quickPreferredWidth", 10000)
    source.setAccessibleName(name)
    layout.addWidget(source)
    source.setVisible(not hidden)
    return cell


def _section(title, name):
    group = _boundary(QGroupBox(title))
    group.setObjectName(name)
    layout = QVBoxLayout(group)
    layout.setContentsMargins(0, 2, 0, 0)
    layout.setSpacing(5)
    layout.setAlignment(Qt.AlignmentFlag.AlignTop)
    return group, layout


def _row(layout, caption, source, name):
    grid = QGridLayout()
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(6)
    label = _label(caption)
    label.setBuddy(source)
    grid.addWidget(label, 0, 0)
    grid.addWidget(_field(source, name), 0, 1)
    grid.setColumnStretch(1, 1)
    layout.addLayout(grid)


def _matrix(layout, headings, rows, *, minimum):
    grid = QGridLayout()
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(6)
    grid.setVerticalSpacing(4)
    for column, title in enumerate(headings, 1):
        grid.addWidget(_label(title, muted=True), 0, column)
        grid.setColumnStretch(column, 1)
    for row, (axis, low, high) in enumerate(rows, 1):
        grid.addWidget(_label(axis), row, 0)
        for column, source in enumerate((low, high), 1):
            grid.addWidget(_field(source, f"{axis} {headings[column - 1]}", minimum=minimum), row, column)
    layout.addLayout(grid)
    return grid


class CalculatorLayout(QObject):
    def __init__(self, panel):
        calc = panel._sweep_calc
        super().__init__(calc)
        self.calc = calc
        self.viewport = panel._workflow_scroll.viewport()
        self.columns = None

        # The title is the disclosure control, so no group-title band is needed.
        calc.layout().setContentsMargins(0, 0, 0, 0)
        calc.layout().setSpacing(4)
        calc._toggle.setProperty("quickDisclosure", True)
        calc._body.setProperty("quickSectionBoundary", True)
        parking = QWidget(calc)
        parking.setObjectName("QuickCalculatorLegacyLayout")
        parking.setLayout(calc._body.layout())
        parking.hide()
        self.parking = parking
        self.grid = QGridLayout(calc._body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(8)

        definition, definition_layout = _section("Sweep definition", "QuickCalculatorDefinition")
        equation = _label("")
        def update_equation(*args):
            equation.setText(f"TG {calc._op_combo.currentText()} r × BG = constant")
        calc._op_combo.currentTextChanged.connect(update_equation)
        update_equation()
        definition_layout.addWidget(equation)
        operation = QGridLayout()
        operation.setHorizontalSpacing(8)
        operation.setVerticalSpacing(3)
        for column, (title, source) in enumerate((("Sign", calc._op_combo), ("Ratio r", calc._ratio_spin))):
            operation.addWidget(_label(title, muted=True), 0, column)
            operation.addWidget(_field(source, title, minimum=60 if column == 0 else 110), 1, column)
        operation.setColumnStretch(1, 1)
        definition_layout.addLayout(operation)
        _row(definition_layout, "Constant(s)", calc._constant_edit, "Condition constants")
        _row(definition_layout, "Vbg step", calc._vbg_step_spin, "Vbg step")
        _row(definition_layout, "Fixed Vbias", calc._vbias_spin, "Fixed Vbias")
        bias = QHBoxLayout()
        bias.setSpacing(6)
        include = _field(calc._include_vbias_chk, "Include fixed Vbias", minimum=96)
        include.setMaximumWidth(110)
        bias.addWidget(include)
        calc._vbias_badge.setProperty("quickSectionBoundary", True)
        calc._vbias_badge.setWordWrap(False)
        bias.addWidget(calc._vbias_badge)
        bias.addStretch(1)
        definition_layout.addLayout(bias)
        definition_layout.addWidget(_label("D = TG + r·BG\nF = TG − r·BG", muted=True))

        limits, limits_layout = _section("Range limits", "QuickCalculatorLimits")
        self.limits_grid = _matrix(limits_layout, ("Minimum (V)", "Maximum (V)"), (
            ("Vtg", calc._vtg_min_spin, calc._vtg_max_spin),
            ("Vbg", calc._vbg_min_spin, calc._vbg_max_spin),
            ("D", calc._doping_min_spin, calc._doping_max_spin),
            ("F", calc._efield_min_spin, calc._efield_max_spin),
        ), minimum=126)
        limits_layout.addWidget(_label("Vtg / Vbg: gate voltages\nD / F: doping and E-field", muted=True))

        results, results_layout = _section("Calculated sweep", "QuickCalculatorResults")
        self.result_grid = QGridLayout()
        self.result_grid.setContentsMargins(0, 0, 0, 0)
        self.result_grid.setSpacing(8)
        results_layout.addLayout(self.result_grid)
        self.result_blocks = (_boundary(QWidget()), _boundary(QWidget()))
        endpoint_layout, output_layout = (QVBoxLayout(block) for block in self.result_blocks)
        for layout in (endpoint_layout, output_layout):
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(5)
            layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.results_grid = _matrix(endpoint_layout, ("Start (V)", "Stop (V)"), (
            ("Vtg", calc._vtg_start_spin, calc._vtg_stop_spin),
            ("Vbg", calc._vbg_start_spin, calc._vbg_stop_spin),
        ), minimum=110)
        calc._derived_range_lbl.setProperty("quickSectionBoundary", True)
        endpoint_layout.addWidget(calc._derived_range_lbl)
        counts = QGridLayout()
        counts.setHorizontalSpacing(8)
        counts.setVerticalSpacing(3)
        for column, (title, source) in enumerate((("Frames", calc._frames_spin), ("Repeat", calc._repeat_spin))):
            counts.addWidget(_label(title, muted=True), 0, column)
            counts.addWidget(_field(source, title, minimum=110), 1, column)
            counts.setColumnStretch(column, 1)
        output_layout.addLayout(counts)
        steps = QHBoxLayout()
        for source in (calc._vtg_step_lbl, calc._vbg_step_lbl):
            source.setProperty("quickSectionBoundary", True)
            source.setWordWrap(True)
            steps.addWidget(source)
        output_layout.addLayout(steps)
        _row(output_layout, "Label", calc._condition_edit, "Condition label")
        output_layout.addWidget(_field(calc._add_btn, "Add calculated rows to Batch"))
        calc._status_lbl.setProperty("quickSectionBoundary", True)
        output_layout.addWidget(calc._status_lbl)

        self.sections = (definition, limits, results)
        self.preview = getattr(calc._multi_preview, "_quick_editor", calc._multi_preview)
        hidden = self.preview.isHidden()
        self.grid.addWidget(self.preview, 1, 0, 1, 3)
        self.preview.setVisible(not hidden)
        self.viewport.installEventFilter(self)
        calc.expanded_changed.connect(self.fit)
        self.fit()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.fit()
        return False

    def fit(self, *args):
        available = self.viewport.width() - 24  # Workflow surface's two insets.
        columns = 3 if available >= 940 else 2 if available >= 650 else 1
        if columns == self.columns:
            return
        self.columns = columns
        for section in self.sections:
            self.grid.removeWidget(section)
        self.grid.removeWidget(self.preview)
        for index, section in enumerate(self.sections):
            if columns == 2 and index == 2:
                self.grid.addWidget(section, 1, 0, 1, 2, Qt.AlignmentFlag.AlignTop)
            else:
                self.grid.addWidget(section, index // columns, index % columns, Qt.AlignmentFlag.AlignTop)
        for column in range(3):
            self.grid.setColumnStretch(column, int(column < columns))
        self.grid.addWidget(self.preview, (len(self.sections) + columns - 1) // columns, 0, 1, columns)
        for block in self.result_blocks:
            self.result_grid.removeWidget(block)
        self.result_grid.addWidget(self.result_blocks[0], 0, 0, Qt.AlignmentFlag.AlignTop)
        self.result_grid.addWidget(self.result_blocks[1], 0 if columns == 2 else 1,
                                   1 if columns == 2 else 0, Qt.AlignmentFlag.AlignTop)
        self.result_grid.setColumnStretch(0, 1)
        self.result_grid.setColumnStretch(1, int(columns == 2))


def configure_sweep_calculator(panel):
    existing = getattr(panel, "_quick_calculator_layout", None)
    if existing is None:
        existing = panel._quick_calculator_layout = CalculatorLayout(panel)
    return existing
