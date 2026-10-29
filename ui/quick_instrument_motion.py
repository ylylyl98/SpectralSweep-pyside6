"""Quick-only presentation boundaries for the existing motion controls.

Move layout items, never replace the source controls: connection state,
backend visibility and all motion interlocks remain owned by InstrumentPanel.
"""
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QVBoxLayout

from ui.quick_instrument_layouts import (
    append_items, group_items, prefer_width, take_items,
)


def _form_rows(items):
    """Keep existing action rows together through the Quick form translator."""
    form = QFormLayout()
    form.setContentsMargins(0, 0, 0, 0)
    form.setSpacing(4)
    for item in items:
        if item.widget() is not None:
            form.addRow(item.widget())
        elif item.layout() is not None:
            row = item.layout()
            row.setParent(None)
            form.addRow(row)
        else:
            # The audited motion sections have no spacers at these boundaries.
            row = QHBoxLayout()
            row.addItem(item)
            form.addRow(row)
    return form


def _group_rows(owner, title, items, name):
    group = group_items(owner, title, [], name)
    group.layout().addLayout(_form_rows(items))
    return group


def _configure_position_section(section, *, rotation):
    layout = section.layout()
    items = take_items(layout)
    # Both source sections end with the same three daily action rows.
    daily = items[-3:]
    connection = items[:6] if rotation else items[:5]
    groups = {
        "connection": _group_rows(
            section, "", connection, "QuickMotionConnection"),
        "motion": _group_rows(
            section, "Position and motion", daily, "QuickMotionDaily"),
    }
    if rotation:
        # The source controller continues to show/hide this exact group when
        # the selected backend changes. Do not wrap it in a disclosure.
        profile = section._profile_group
        profile.setProperty("quickSectionBoundary", True)
        profile_items = take_items(profile.layout())
        profile.layout().addLayout(_form_rows(profile_items))
        groups["profile"] = profile
        # Item 6 is the profile and item 7 the old separator. Moving the
        # profile beneath daily actions avoids interrupting the move workflow.
        unused = items[7:-3]
    else:
        # The source range hint belongs with position and motion.
        append_items(groups["motion"].layout(), [items[5]])
        unused = items[6:-3]
    for item in unused:
        if item.widget() is not None:
            item.widget().hide()
    section._quick_motion_unused_items = unused
    for group in groups.values():
        layout.addWidget(group)

    prefer_width(section._type_combo, 230)
    prefer_width(section._addr_combo, 216)
    prefer_width(section._axis_combo, 76)
    prefer_width(section._target_spn, 122)
    prefer_width(section._jog_spn, 122)
    prefer_width(section._pos_lbl, 96)
    prefer_width(section._read_pos_btn, 64)
    prefer_width(section._move_btn, 64)
    prefer_width(section._home_btn, 64)
    prefer_width(section._jog_neg_btn, 46)
    prefer_width(section._jog_pos_btn, 46)
    for label in (section._status, getattr(section, "_mapping_hint", None),
                  getattr(section, "_range_hint", None)):
        if label is not None:
            label.setWordWrap(True)
            prefer_width(label, 10000)
    if rotation:
        section._addr_combo.setMinimumWidth(0)
        # Leave room for the three-digit percentage, its unit and steppers.
        prefer_width(section._velocity_pct, 118)
        prefer_width(section._acceleration_pct, 118)
        prefer_width(section._save_profile_btn, 190)
        prefer_width(section._profile_status, 10000)
        # A separate scan option reads naturally below the resource selector.
        address_layout = connection[1].layout()
        address_items = take_items(address_layout)
        append_items(address_layout, [address_items[2], address_items[0]])
        connection_form = groups["connection"].layout().itemAt(0).layout()
        connection_form.insertRow(2, section._asrl_chk)
        section._quick_motion_scan_item = address_items[1]
    return groups


def _configure_esp32(section):
    layout = section.layout()
    items = take_items(layout)
    # Source order: connection, status, distance/speed, jog, endpoints, STOP,
    # readbacks, advanced disclosure and its independently controlled body.
    groups = {
        "connection": _group_rows(
            section, "", items[:2], "QuickImagingStageConnection"),
        "motion": group_items(
            section, "Position and motion", [], "QuickImagingStageDaily"),
    }
    daily = groups["motion"].layout()
    # Keep the editable port on one line and the two connection actions
    # together below it at either supported sidebar width.
    port_layout = items[0].layout()
    port_items = take_items(port_layout)
    port_rows = QVBoxLayout()
    append_items(port_rows, port_items[:1])
    connection_actions = QHBoxLayout()
    append_items(connection_actions, port_items[1:])
    port_rows.addLayout(connection_actions)
    port_layout.addLayout(port_rows)
    append_items(daily, items[7:11])
    append_items(daily, items[2:7])
    # Full directional wording is safety information; each direction gets
    # its own available-width row instead of competing for half the sidebar.
    jog_layout = items[3].layout()
    jog_items = take_items(jog_layout)
    jog_rows = QVBoxLayout()
    append_items(jog_rows, jog_items)
    jog_layout.addLayout(jog_rows)
    section._advanced.setProperty("quickSectionBoundary", True)
    section._advanced_btn.setProperty("quickDisclosure", True)
    section._advanced_btn.setProperty("quickPreferredWidth", 10000)
    for group in groups.values():
        layout.addWidget(group)
    append_items(layout, items[11:])
    section.setMaximumWidth(16777215)
    prefer_width(section._port, 10000)
    prefer_width(section._refresh, 74)
    prefer_width(section._connect, 94)
    prefer_width(section._preset, 178)
    prefer_width(section._speed, 140)
    prefer_width(section._apply_freq, 100)
    for widget in (section._status, section._position, section._reference,
                   section._limit, section._frequency_readout,
                   section._jog_minus, section._jog_plus, section._goto_zero,
                   section._goto_maximum, section._stop, section._target_range,
                   section._clear_ref, section._save_max):
        prefer_width(widget, 10000)
        if hasattr(widget, "setWordWrap"):
            widget.setWordWrap(True)
    return groups


def configure_motion_section(section):
    """Group an audited motion section once; unknown widgets are untouched."""
    existing = getattr(section, "_quick_motion_layout", None)
    if existing is not None:
        return existing
    name = type(section).__name__
    if name == "_RotationBlock":
        groups = _configure_position_section(section, rotation=True)
    elif name == "_StageSection":
        groups = _configure_position_section(section, rotation=False)
    elif name == "_ESP32ImagingStageSection":
        groups = _configure_esp32(section)
    else:
        return None
    section._quick_motion_layout = groups
    return groups
