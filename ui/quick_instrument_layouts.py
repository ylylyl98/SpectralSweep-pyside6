"""Quick-only grouping of existing instrument controls, before scalar migration.

Keep source widgets and their layouts alive: controller slots still own all
values, enabled states and hardware actions. These helpers only move layout
items into explicit presentation boundaries.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QBoxLayout, QFrame, QGroupBox, QLabel, QVBoxLayout, QWidget


def take_items(layout):
    items = []
    while layout.count():
        items.append(layout.takeAt(0))
    return items


def append_items(layout, items):
    for item in items:
        if item.widget() is not None:
            layout.addWidget(item.widget())
        elif item.layout() is not None:
            child = item.layout()
            child.setParent(None)
            layout.addLayout(child)
        else:
            layout.addItem(item)


def group_items(owner, title, items, name):
    group = QGroupBox(title, owner) if title else QWidget(owner)
    group.setObjectName(name)
    group.setProperty("quickSectionBoundary", True)
    layout = QVBoxLayout(group)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    layout.setAlignment(Qt.AlignmentFlag.AlignTop)
    append_items(layout, items)
    return group


def prefer_width(widget, width):
    widget.setProperty("quickPreferredWidth", width)


def bind_logical_rows(layout):
    """Keep an instrument's source rows from merging with the next operation."""
    if isinstance(layout, QBoxLayout) and layout.direction() in (
            QBoxLayout.Direction.LeftToRight, QBoxLayout.Direction.RightToLeft):
        layout.setProperty("quickRowBoundary", True)
    for index in range(layout.count()):
        item = layout.itemAt(index)
        child = item.layout()
        if child is None and item.widget() is not None:
            child = item.widget().layout()
        if child is not None:
            bind_logical_rows(child)


def _contains(item, source):
    if item.widget() is source:
        return True
    layout = item.layout()
    return bool(layout and any(_contains(layout.itemAt(i), source) for i in range(layout.count())))


def _take_for(items, *sources):
    selected = [item for item in items if any(_contains(item, source) for source in sources)]
    for item in selected:
        items.remove(item)
    return selected


def _configure_optical_section(section):
    kind = type(section).__name__
    if kind not in {"_LF6Section", "_PM100DSection", "MFF101Section"}:
        return None
    layout = section.layout()
    items = take_items(layout)
    if kind == "_LF6Section":
        setup_label = next(item.widget() for item in items
                           if isinstance(item.widget(), QLabel) and item.widget().text() == "Measurement setup:")
        setup_label.setBuddy(section._backend)
        connection = group_items(section, "", _take_for(items, setup_label, section._backend,
            section._setup_summary, section._connect_btn, section._status,
            section._connection_summary), "QuickDetectorConnection")
        detector = group_items(section, "", _take_for(items, section._temperature,
            section._temperature_detail, section._temperature_monitor,
            section._acquisition_condition, section._warm_disconnect_btn), "QuickDetectorReadback")
        # Connection first, current detector state next, optional setup last.
        layout.addWidget(connection)
        layout.addWidget(detector)
        append_items(layout, items)
        prefer_width(section._backend, 270)
        section._backend.setProperty("quickLabelAbove", True)
        for combo in (section._optical_profile, section._lf_route, section._winspec_route,
                      section._winspec_acquisition_backend, section._exp_combo):
            prefer_width(combo, 250)
            combo.setProperty("quickLabelAbove", True)
        prefer_width(section._winspec_host, 165)
        prefer_width(section._winspec_port, 72)
        section._temperature_refresh.setText("Read")
        section._temperature_refresh.setAccessibleName("Read detector temperature")
        section._temperature_refresh.setToolTip("Read the detector temperature.")
        return {"connection": connection, "readback": detector}
    if kind == "_PM100DSection":
        connection = group_items(section, "", _take_for(items, section._scan_btn,
            section._connect_btn, section._status), "QuickPowerMeterConnection")
        readback = group_items(section, "Power readout", _take_for(items, section._pwr_lbl,
            section._auto_chk), "QuickPowerMeterReadback")
        calibration = group_items(section, "Calibration", _take_for(items, section._wl_spn,
            section._factor_spn), "QuickPowerMeterCalibration")
        layout.addWidget(connection)
        layout.addWidget(readback)
        layout.addWidget(calibration)
        # The two labeled sections replace the original unlabeled separator.
        for item in items:
            if isinstance(item.widget(), QFrame) and item.widget().frameShape() == QFrame.Shape.HLine:
                item.widget().hide()
            else:
                append_items(layout, [item])
        prefer_width(section._device_combo, 205)
        section._scan_btn.setText("Scan")
        section._scan_btn.setAccessibleName("Scan power meters")
        section._scan_btn.setToolTip("Scan connected PM100D devices.")
        for field in (section._wl_spn, section._factor_spn, section._interval_spn):
            # Match the number editor's minimum: smaller hints force a label
            # above its input even when the sidebar has room for an inline row.
            prefer_width(field, 110)
        return {"connection": connection, "readback": readback, "calibration": calibration}
    # Keep flipper connection controls separate from its read/position actions.
    connect_row = next(item.layout() for item in items if _contains(item, section._connect))
    connect_row.removeWidget(section._read)
    positions = next(item.layout() for item in items if _contains(item, section._position1))
    positions.insertWidget(0, section._read)
    connection = group_items(section, "", _take_for(items, section._serial,
        section._connect, section._status), "QuickFlipperConnection")
    controls = group_items(section, "Optical path", items, "QuickFlipperPosition")
    layout.addWidget(connection)
    layout.addWidget(controls)
    prefer_width(section._serial, 200)
    section._serial.setProperty("quickLabelAbove", True)
    section._read.setText("Read")
    section._read.setToolTip("Read the current flipper position.")
    section._read.setAccessibleName("Read flipper position")
    return {"connection": connection, "position": controls}


def configure_instrument_layout(panel):
    """Rearrange only known instrument sections, once, before Quick migration."""
    if getattr(panel, "_quick_instrument_layout", None) is not None:
        return panel._quick_instrument_layout
    from ui.quick_instrument_motion import configure_motion_section
    from ui.quick_instrument_electrical import configure_electrical_section
    groups = {}
    for key, section in getattr(panel, "_sections", {}).items():
        result = (_configure_optical_section(section)
                  or configure_motion_section(section)
                  or configure_electrical_section(section))
        if result is not None:
            section.layout().setContentsMargins(4, 2, 4, 4)
            section.layout().setSpacing(6)
            section.layout().setAlignment(Qt.AlignmentFlag.AlignTop)
            bind_logical_rows(section.layout())
            groups[key] = result
    panel._quick_instrument_layout = groups
    return groups
