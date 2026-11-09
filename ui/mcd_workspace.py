"""Stable presentation boundaries shared by the two MCD workspaces."""
from PySide6.QtWidgets import (
    QBoxLayout, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from ui.style_roles import set_style_role
from ui.workspace_pane import WorkspacePaneWidth


def mcd_control_actions(name, buttons):
    """An action band stays separate from telemetry and parameter fields."""
    row = QWidget()
    row.setObjectName(name)
    row.setProperty("quickSectionBoundary", True)
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    layout.setProperty("quickRowBoundary", True)
    for button in buttons:
        layout.addWidget(button)
    layout.addStretch(1)
    return row


def mcd_readouts(name, fields):
    """Two columns of caption/value cells, retained as native live labels."""
    surface = QWidget()
    surface.setObjectName(name)
    surface.setProperty("quickSectionBoundary", True)
    grid = QGridLayout(surface)
    grid.setContentsMargins(0, 4, 0, 4)
    grid.setHorizontalSpacing(16)
    grid.setVerticalSpacing(10)
    for index, (caption, value) in enumerate(fields):
        cell = QWidget(surface)
        cell.setMinimumWidth(0)
        layout = QVBoxLayout(cell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        label = caption if isinstance(caption, QLabel) else QLabel(caption)
        set_style_role(label, 'text', tone='secondary')
        value.setWordWrap(True)
        value.setMinimumWidth(0)
        value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(label)
        layout.addWidget(value)
        grid.addWidget(cell, index // 2, index % 2, 1, 2 if len(fields) == 1 else 1)
    grid.setColumnStretch(0, 1)
    grid.setColumnStretch(1, 1)
    return surface


def mcd_control_fields(name, fields):
    return _gate_pair(name, fields)


def populate_mcd_gate_entry(group, mode, label_a, input_a, label_b, input_b,
                            vbias, expansion_label, expansion, add, status):
    """Keep each batch input and its live caption together in both shells."""
    layout = QVBoxLayout(group)
    layout.setContentsMargins(8, 6, 8, 6)
    layout.setSpacing(4)
    def field_row(fields):
        row = QHBoxLayout()
        row.setProperty("quickRowBoundary", True)
        row.setSpacing(8)
        for label, editor, width in fields:
            form = QFormLayout()
            form.setContentsMargins(0, 0, 0, 0)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            label.setBuddy(editor)
            editor.setProperty("quickLabelAbove", True)
            editor.setProperty("quickPreferredWidth", width)
            form.addRow(label, editor)
            row.addLayout(form, 1)
        layout.addLayout(row)

    field_row(((QLabel("Input type"), mode, 220),))
    field_row(((label_a, input_a, 180), (label_b, input_b, 180)))
    field_row(((QLabel("Vbias"), vbias, 180), (expansion_label, expansion, 180)))
    actions = QHBoxLayout()
    actions.setProperty("quickRowBoundary", True)
    actions.addWidget(add)
    actions.addWidget(status, 1)
    layout.addLayout(actions)


def add_mcd_field_settings(form, start, stop, mode, step, tolerance, hold):
    """Identical acquisition controls and row boundaries on both magnet pages."""
    form.addRow(mcd_control_fields("McdAcquisitionMode", (("Acquisition", mode),)))
    form.addRow(mcd_control_fields("McdFieldEndpoints", (("Start field (T)", start), ("Stop field (T)", stop))))
    form.addRow(mcd_control_fields("McdPointSettings", (("Point step", step), ("Point tolerance", tolerance))))
    form.addRow(mcd_control_fields("McdStableDuration", (("Stable for", hold),)))


def _gate_pair(name, fields):
    """Two equal columns retain their labels in native and Quick forms."""
    row = QWidget()
    row.setObjectName(name)
    row.setProperty("quickSectionBoundary", True)
    grid = QGridLayout(row)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(12)
    for column, (caption, editor) in enumerate(fields):
        owner = QWidget(row)
        owner.setProperty("quickSectionBoundary", True)
        form = QFormLayout(owner)
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(4)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        editor.setMinimumWidth(140)
        editor.setMaximumWidth(240)
        editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        editor.setAccessibleName(caption)
        editor.setProperty("quickLabelAbove", True)
        editor.setProperty("quickPreferredWidth", 240)
        form.addRow(caption, editor)
        grid.addWidget(owner, 0, column)
        grid.setColumnStretch(column, 1)
    return row


def add_mcd_gate_settings(form, tg, bg, ratio, first, later):
    form.addRow(_gate_pair("McdGateWeighting", (("TG weight", tg), ("BG weight", bg))))
    readback = QWidget()
    readback.setObjectName("McdGateRatioReadback")
    readback.setProperty("quickSectionBoundary", True)
    readback_layout = QHBoxLayout(readback)
    readback_layout.setContentsMargins(0, 0, 0, 0)
    readback_layout.setSpacing(8)
    readback_layout.addWidget(QLabel("Gate ratio"))
    readback_layout.addWidget(ratio)
    readback_layout.addStretch(1)
    form.addRow(readback)
    form.addRow(_gate_pair("McdGateSettling", (
        ("First gate settle", first), ("Later gate settle", later),
    )))


def mcd_settings_pane(panel, scroll):
    pane = QWidget()
    pane.setObjectName("McdSettingsPane")
    pane.setAccessibleName("MCD measurement settings")
    pane.setMinimumWidth(440)
    scroll.setMinimumWidth(0)
    layout = QVBoxLayout(pane)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    header = QWidget(pane)
    header.setProperty("quickSectionBoundary", True)
    header_layout = QHBoxLayout(header)
    header_layout.setContentsMargins(8, 4, 8, 0)
    title = QLabel("Measurement settings")
    set_style_role(title, strong=True)
    panel._reset_layout_btn = QPushButton("Reset layout")
    panel._reset_layout_btn.setAccessibleName("Reset MCD layout")
    panel._reset_layout_btn.setToolTip("Restore pane widths and scroll position. Measurement settings are unchanged.")
    panel._reset_layout_btn.setProperty("quickPresentationAction", True)
    panel._reset_layout_btn.clicked.connect(lambda: reset_mcd_layout(panel))
    header_layout.addWidget(title)
    header_layout.addStretch(1)
    header_layout.addWidget(panel._reset_layout_btn)
    layout.addWidget(header)
    layout.addWidget(scroll, 1)
    panel._settings_pane = pane
    return pane


def configure_mcd_workspace(panel, groups, rows, preferred_width):
    # Section ownership must survive the scalar adapter, even when all of a
    # group's children happen to be supported by the same Quick form.
    for name, group in groups.items():
        group.setProperty("quickSectionBoundary", True)
        group.setProperty("mcdSection", True)
        group.setAccessibleName(group.title())
        if not group.objectName():
            group.setObjectName("Mcd" + name + "Section")
        group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
    for row in rows:
        row.setDirection(QBoxLayout.Direction.TopToBottom)
        for index in range(row.count()):
            row.setStretch(index, 0)
        row.setSpacing(12)
    panel._mcd_settings_groups = groups
    panel._mcd_pane_width = WorkspacePaneWidth(panel, panel._splitter, preferred_width)


def reset_mcd_layout(panel):
    """Reset view geometry only; never commit, discard or alter a recipe."""
    panel._mcd_pane_width.reset()
    panel._plot_log_splitter.setSizes([520, 140])
    scroll = getattr(panel, "_settings_scroll", getattr(panel, "_scroll", None))
    scroll.verticalScrollBar().setValue(0)
    scroll.horizontalScrollBar().setValue(0)
