"""Quick-only electrical instrument grouping; original controls keep ownership.

The channel layouts remain alive so existing signal connections, readbacks and
hardware safety admission still run through the Widgets controllers.
"""
from ui.quick_instrument_layouts import (
    append_items, bind_logical_rows, group_items, prefer_width, take_items,
)


def _configure_smu(section):
    layout = section.layout()
    items = take_items(layout)
    limits = section._limits_group
    connection_items = [item for item in items if item.widget() is not limits]
    limit_items = [item for item in items if item.widget() is limits]
    connection = group_items(section, "", connection_items,
                             "QuickSMUConnection")
    layout.addWidget(connection)
    append_items(layout, limit_items)
    limits.setProperty("quickSectionBoundary", True)
    groups = {"connection": connection, "limits": limits}
    for role, combo in section._role_combos.items():
        prefer_width(combo, 208)
        channel = section._curr_comp_by_role[role].parentWidget()
        channel.setProperty("quickSectionBoundary", True)
        prefer_width(section._curr_comp_by_role[role], 142)
        prefer_width(section._volt_comp_by_role[role], 112)
        prefer_width(section._curr_range_by_role[role], 142)
        groups[f"limits_{role}"] = channel
    prefer_width(section._termination, 80)
    return groups


def _configure_manual(section):
    layout = section.layout()
    items = take_items(layout)
    channels = {role: section._target_spn[role].parentWidget() for role in section._CHANNELS}
    channel_ids = {id(channel) for channel in channels.values()}
    # Keep the original shared step and Read All / Zero All rows together.
    global_items = [item for item in items
                    if item.widget() is None or id(item.widget()) not in channel_ids
                    and item.widget() is not section._status_lbl]
    global_controls = group_items(section, "All channels", global_items, "QuickManualGlobal")
    layout.addWidget(global_controls)
    groups = {"global": global_controls}
    for role, channel in channels.items():
        channel.setProperty("quickSectionBoundary", True)
        layout.addWidget(channel)
        prefer_width(section._target_spn[role], 128)
        for button in (section._down_btn[role], section._up_btn[role]):
            prefer_width(button, 76)
        prefer_width(section._read_btn_by_role[role], 64)
        prefer_width(section._zero_btn_by_role[role], 64)
        groups[f"channel_{role}"] = channel
    layout.addWidget(section._status_lbl)
    prefer_width(section._step_spn, 128)
    return groups


def configure_electrical_section(section):
    """Group known sections once before scalar migration; ignore unknown types."""
    name = type(section).__name__
    configure = {"_SMUSection": _configure_smu,
                 "_ManualControlSection": _configure_manual}.get(name)
    if configure is None:
        return None
    existing = getattr(section, "_quick_electrical_layout", None)
    if existing is None:
        existing = configure(section)
        bind_logical_rows(section.layout())
        section._quick_electrical_layout = existing
    return existing
