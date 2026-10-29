"""Desktop settings hierarchy assembled from the original source controls.

This presentation hook runs only for Quick, before scalar migration. Controls,
signals, configuration callbacks and session-state ownership remain unchanged.
"""
from PySide6.QtCore import QEvent, QObject, QTimer, Qt
from PySide6.QtWidgets import (
    QApplication, QBoxLayout, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QSizePolicy, QVBoxLayout, QWidget,
)


def _surface(widget):
    widget.setProperty("quickWorkspaceSurface", True)
    # Existing source groups have already been polished before this Quick-only
    # hook. Refresh the semantic selector after changing the property.
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def _group(title, name, *, surface=True):
    group = QGroupBox(title)
    group.setObjectName(name)
    if surface:
        _surface(group)
    # Preserve semantic section ownership through scalar form migration,
    # including single-column initial layouts and nested camera sections.
    group._quick_parallel_group = True
    group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
    form = QFormLayout(group)
    form.setContentsMargins(12, 12, 12, 12)
    form.setSpacing(8)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    return group, form


def _field(form, label, widget, *, width=140, above=False):
    hidden = widget.isHidden()
    # Replace legacy path-sized native minima with presentation hints. These
    # never constrain input length or change numeric range/precision.
    widget.setMinimumWidth(0)
    widget.setMaximumWidth(16777215)
    widget.setProperty("quickPreferredWidth", width)
    widget.setProperty("quickLabelAbove", above)
    form.addRow(label, widget)
    widget.setVisible(not hidden)


def _columns(*groups):
    row = QHBoxLayout()
    row.setProperty("quickSectionColumns", True)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(12)
    row.setAlignment(Qt.AlignmentFlag.AlignTop)
    for group in groups:
        row.addWidget(group, 1, Qt.AlignmentFlag.AlignTop)
    return row


class _SettingsFit(QObject):
    """Reflow native section columns while Quick forms measure their heights."""

    def __init__(self, panel, scroll, device_row, profile_row, lower_row):
        super().__init__(panel)
        self.scroll = scroll
        self.rows = device_row, profile_row, lower_row
        scroll.viewport().installEventFilter(self)
        self.fit()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.fit()
        return False

    def fit(self):
        width = self.scroll.viewport().width()
        for row, horizontal in zip(self.rows, (width >= 1100, width >= 660, width >= 1000)):
            direction = (QBoxLayout.Direction.LeftToRight if horizontal
                         else QBoxLayout.Direction.TopToBottom)
            if row.direction() != direction:
                row.setDirection(direction)
            for index in range(row.count()):
                row.setStretch(index, 1 if horizontal else 0)
        if width >= 1100:
            self.rows[0].setStretch(1, 2)


def configure_quick_settings(panel):
    """Group Settings into desktop sections, retaining the existing controls."""
    app = QApplication.instance()
    if app is None or not app.property("spectralQuick"):
        return {}
    existing = getattr(panel, "_quick_settings_layout", None)
    if existing is not None:
        return existing

    scroll = panel._categories.widget(0)
    body = scroll.widget()
    body_layout = body.layout()
    old_defaults = body_layout.itemAt(0).widget()
    mff = body_layout.itemAt(1).widget()
    body_layout.removeWidget(old_defaults)
    body_layout.removeWidget(mff)
    # Keep unused legacy captions/layouts alive without placing them in the
    # visible hierarchy. Original controls are moved below, never cloned.
    old_defaults.setParent(panel)
    old_defaults.hide()
    note = next(label for label in old_defaults.findChildren(QLabel)
                if label.text().startswith("These are shared settings"))

    acquisition, form = _group("Acquisition defaults", "QuickSettingsAcquisition")
    for label, name, width in (
        ("Exposure:", "_exposure", 145), ("Centre λ:", "_center", 140),
        ("Accumulations:", "_accum", 145),
    ):
        _field(form, label, getattr(panel, name), width=width)
    form.addRow(panel._apply_btn)
    form.addRow(panel._lf6_status)
    form.addRow(note)
    note.setParent(acquisition)
    note.show()

    connection, form = _group("Device connection", "QuickSettingsConnection")
    _field(form, "Andor SDK2 DLL folder:", panel._andor_sdk_dir, width=400, above=True)
    _field(form, "Shamrock DLL folder:", panel._andor_shamrock_dir, width=400, above=True)
    _field(form, "Shamrock index:", panel._andor_spec_index, width=120)

    profiles = []
    for role, label in (("si", "Si"), ("ingaas", "InGaAs")):
        profile, form = _group(f"{label} camera profile", f"QuickSettingsCamera_{role}", surface=False)
        # The shared surface supplies the outer inset. Keep subsection titles
        # and their editors on the same 12px content edge as adjacent panes.
        form.setContentsMargins(0, 4, 0, 4)
        _field(form, "Camera serial:", getattr(panel, f"_andor_{role}_serial"), width=500, above=True)
        _field(form, "Target temperature:", getattr(panel, f"_andor_{role}_temperature"), width=140, above=True)
        _field(form, "Fan:", getattr(panel, f"_andor_{role}_fan"), width=100, above=True)
        _field(form, "Output port:", getattr(panel, f"_andor_{role}_output_port"), width=135, above=True)
        cooler = getattr(panel, f"_andor_{role}_cooler")
        cooler.setProperty("quickPreferredWidth", 500)
        form.addRow(cooler)
        profiles.append(profile)

    shared, form = _group("Spectrograph and acquisition safety", "QuickSettingsShared")
    for label, name, width in (
        ("Camera shutter:", "_andor_shutter", 120),
        ("Shamrock shutter:", "_andor_shamrock_shutter", 130),
        ("Grating:", "_andor_grating", 100),
        ("Input slit:", "_andor_slit", 130),
        ("Safe disconnect:", "_andor_safe_disconnect_temperature", 140),
    ):
        _field(form, label, getattr(panel, name), width=width, above=True)
    form.addRow(panel._andor_invert_wl)
    form.addRow(panel._andor_discard_first)

    mff._quick_parallel_group = True
    _surface(mff)
    mff.layout().setContentsMargins(12, 12, 12, 12)
    mff.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
    for field in (panel._mff101_position1_path, panel._mff101_position2_path):
        field.setProperty("quickPreferredWidth", 210)
        field.setProperty("quickLabelAbove", True)
    for field in (panel._mff101_andor_position, panel._mff101_lightfield_position):
        field.setProperty("quickPreferredWidth", 130)
    profile_holder = QWidget()
    _surface(profile_holder)
    profile_row = _columns(*profiles)
    profile_row.setContentsMargins(12, 8, 12, 8)
    profile_holder.setLayout(profile_row)
    device_row = _columns(connection, profile_holder)
    # Device paths get one column and each camera gets one equivalent column.
    device_row.setStretch(0, 1)
    device_row.setStretch(1, 2)
    lower_row = _columns(shared, mff)
    body_layout.insertWidget(0, acquisition)
    body_layout.insertLayout(1, device_row)
    body_layout.insertLayout(2, lower_row)
    body_layout.setSpacing(12)
    body_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
    fit = _SettingsFit(panel, scroll, device_row, profile_row, lower_row)
    QTimer.singleShot(0, panel, fit.fit)

    # Sparse utility pages need a readable line length and useful field size,
    # not controls stretched across the entire desktop work area.
    for index, maximum in ((1, 1000), (2, 920)):
        utility_body = panel._categories.widget(index).widget()
        utility_body.layout().setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        for position in range(utility_body.layout().count()):
            widget = utility_body.layout().itemAt(position).widget()
            if widget is not None:
                widget.setMaximumWidth(maximum)
                _surface(widget)
                if widget.layout() is not None:
                    widget.layout().setContentsMargins(12, 12, 12, 12)
                widget._quick_parallel_group = True
    panel._base_out_edit.setProperty("quickPreferredWidth", 650)
    panel._base_out_edit.setProperty("quickLabelAbove", True)
    panel._notifications.url_edit.setProperty("quickPreferredWidth", 620)
    panel._notifications.url_edit.setProperty("quickLabelAbove", True)
    panel._notifications.name_edit.setProperty("quickPreferredWidth", 240)
    result = dict(acquisition=acquisition, connection=connection, profiles=tuple(profiles),
                  shared=shared, light_paths=mff, fit=fit, rows=(device_row, profile_row, lower_row))
    panel._quick_settings_layout = result
    return result
